"""Hierarchical zone-concession profiles for defensive five-player units.

The estimator is deliberately independent from PostgreSQL.  The compute job
turns stored shot events and lineup aggregates into the small evidence objects
defined here, and this module applies the versioned statistical contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from .shot_zones import FIELD_GOAL_ZONES


MODEL_VERSION = "shot-location-v1"
MINUTES_SHARE_THRESHOLD = 0.02
TEAM_SHRINKAGE_PRIOR_ATTEMPTS = 50.0
LINEUP_SHRINKAGE_PRIOR_ATTEMPTS = 50.0
CONFIDENCE_FLOOR = 45.0
EVIDENCE_STATUSES = ("available", "provisional", "unavailable")


def _counts(zones: Mapping[str, int], *, label: str) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for zone in FIELD_GOAL_ZONES:
        value = zones.get(zone, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} zone count is invalid: {zone}={value!r}")
        normalized[zone] = value
    extra = set(zones) - set(FIELD_GOAL_ZONES)
    if extra:
        raise ValueError(f"{label} contains unknown zones: {sorted(extra)}")
    return normalized


def canonical_lineup_hash(player_ids: Iterable[int]) -> tuple[tuple[int, ...], str]:
    """Return the sorted five-player identity and its database hash."""

    values = tuple(sorted(int(player_id) for player_id in player_ids))
    if len(values) != 5 or len(set(values)) != 5 or any(value <= 0 for value in values):
        raise ValueError(f"a defensive lineup must contain five unique positive IDs: {values}")
    return values, "-".join(str(value) for value in values)


@dataclass(frozen=True, slots=True)
class LeagueSeasonBaseline:
    season: int
    zone_attempts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class TeamSeasonBaseline:
    season: int
    team_id: int
    zone_attempts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DefensiveLineupEvidence:
    """Aggregated evidence for one stored five across a team-season."""

    season: int
    team_id: int
    lineup_hash: str
    player_ids: tuple[int, ...]
    total_seconds: float
    possessions: float
    opponent_fga: float
    zone_attempts: Mapping[str, int]

    @property
    def classified_attempts(self) -> int:
        return sum(_counts(self.zone_attempts, label="lineup").values())


@dataclass(frozen=True, slots=True)
class DefensiveLineupConcession:
    season: int
    team_id: int
    lineup_hash: str
    player_ids: tuple[int, ...]
    zone: str
    zone_concession_tilt: float | None
    possessions: float
    confidence: float | None
    evidence_status: str
    minutes_share: float
    classified_attempts: int
    opponent_fga: float
    attribution_coverage: float


def _finite_nonnegative(value: float, *, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and non-negative: {value!r}")
    return number


def _shares(counts: Mapping[str, int], prior: float, target: Mapping[str, float]) -> dict[str, float]:
    if not math.isfinite(prior) or prior <= 0:
        raise ValueError("shrinkage prior must be positive and finite")
    normalized = _counts(counts, label="baseline")
    total = sum(normalized.values())
    if total <= 0:
        raise ValueError("zone baseline must contain at least one attempt")
    target_total = sum(float(target[zone]) for zone in FIELD_GOAL_ZONES)
    if not math.isclose(target_total, 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("shrinkage target shares must sum to one")
    return {
        zone: (normalized[zone] + prior * float(target[zone])) / (total + prior)
        for zone in FIELD_GOAL_ZONES
    }


def _baseline_shares(baseline: LeagueSeasonBaseline | TeamSeasonBaseline) -> dict[str, float]:
    counts = _counts(baseline.zone_attempts, label="season baseline")
    total = sum(counts.values())
    if total <= 0:
        raise ValueError(f"season {baseline.season} baseline has no classified attempts")
    return {zone: counts[zone] / total for zone in FIELD_GOAL_ZONES}


def select_realistic_units(
    units: Iterable[DefensiveLineupEvidence],
    *,
    minutes_share_threshold: float = MINUTES_SHARE_THRESHOLD,
) -> list[tuple[DefensiveLineupEvidence, float]]:
    """Select all units at or above a team-season minutes-share threshold."""

    if not math.isfinite(minutes_share_threshold) or not 0 < minutes_share_threshold <= 1:
        raise ValueError("minutes_share_threshold must be in (0, 1]")
    rows = list(units)
    seen: set[tuple[int, int, str]] = set()
    totals: dict[tuple[int, int], float] = {}
    for row in rows:
        player_ids, canonical_hash = canonical_lineup_hash(row.player_ids)
        if tuple(row.player_ids) != player_ids:
            raise ValueError(f"lineup player IDs must already be sorted: {row.player_ids}")
        if row.lineup_hash != canonical_hash:
            raise ValueError(
                f"lineup hash does not match player IDs: {row.lineup_hash} != {canonical_hash}"
            )
        key = (int(row.season), int(row.team_id), row.lineup_hash)
        if key in seen:
            raise ValueError(f"duplicate defensive lineup evidence: {key}")
        seen.add(key)
        totals[(key[0], key[1])] = totals.get((key[0], key[1]), 0.0) + _finite_nonnegative(
            row.total_seconds, label="total_seconds"
        )
    selected: list[tuple[DefensiveLineupEvidence, float]] = []
    for row in sorted(rows, key=lambda item: (item.season, item.team_id, item.lineup_hash)):
        denominator = totals[(row.season, row.team_id)]
        if denominator <= 0:
            continue
        share = _finite_nonnegative(row.total_seconds, label="total_seconds") / denominator
        if share >= minutes_share_threshold:
            selected.append((row, share))
    return selected


def build_concession_profiles(
    league_baselines: Iterable[LeagueSeasonBaseline],
    team_baselines: Iterable[TeamSeasonBaseline],
    units: Iterable[DefensiveLineupEvidence],
    *,
    minutes_share_threshold: float = MINUTES_SHARE_THRESHOLD,
    team_shrinkage_prior_attempts: float = TEAM_SHRINKAGE_PRIOR_ATTEMPTS,
    lineup_shrinkage_prior_attempts: float = LINEUP_SHRINKAGE_PRIOR_ATTEMPTS,
    confidence_floor: float = CONFIDENCE_FLOOR,
    independent_audit_passed: bool = False,
) -> list[DefensiveLineupConcession]:
    """Build five concession rows for every realistic defensive unit.

    The unit profile is shrunk first toward its team-season profile and then
    the team-season profile is shrunk toward the league-season profile.  A
    pending independent lineup audit keeps otherwise well-supported rows
    provisional; no evidence-poor unit is converted into a zero tilt.
    """

    if isinstance(independent_audit_passed, bool) is False:
        raise ValueError("independent_audit_passed must be boolean")
    if not math.isfinite(confidence_floor) or not 0 <= confidence_floor <= 100:
        raise ValueError("confidence_floor must be in [0, 100]")
    league_by_season: dict[int, dict[str, float]] = {}
    league_counts: dict[int, dict[str, int]] = {}
    for baseline in league_baselines:
        season = int(baseline.season)
        if season in league_by_season:
            raise ValueError(f"duplicate league baseline: {season}")
        counts = _counts(baseline.zone_attempts, label=f"league {season}")
        league_counts[season] = counts
        league_by_season[season] = _baseline_shares(baseline)

    team_shares: dict[tuple[int, int], dict[str, float]] = {}
    for baseline in team_baselines:
        key = (int(baseline.season), int(baseline.team_id))
        if key in team_shares:
            raise ValueError(f"duplicate team baseline: {key}")
        league_share = league_by_season.get(key[0])
        if league_share is None:
            raise ValueError(f"missing league baseline for team baseline: {key}")
        counts = _counts(baseline.zone_attempts, label=f"team {key}")
        team_shares[key] = _shares(counts, team_shrinkage_prior_attempts, league_share)

    output: list[DefensiveLineupConcession] = []
    for evidence, minutes_share in select_realistic_units(
        units, minutes_share_threshold=minutes_share_threshold
    ):
        key = (evidence.season, evidence.team_id)
        league_share = league_by_season.get(evidence.season)
        team_share = team_shares.get(key)
        if league_share is None or team_share is None:
            raise ValueError(f"missing baseline for defensive unit: {key}")
        zone_counts = _counts(evidence.zone_attempts, label=f"lineup {evidence.lineup_hash}")
        classified_attempts = sum(zone_counts.values())
        possessions = _finite_nonnegative(evidence.possessions, label="possessions")
        opponent_fga = _finite_nonnegative(evidence.opponent_fga, label="opponent_fga")
        if opponent_fga > 0:
            attribution_coverage = min(1.0, classified_attempts / opponent_fga)
        else:
            attribution_coverage = 0.0
        reliability = (
            classified_attempts
            / (classified_attempts + lineup_shrinkage_prior_attempts)
            if classified_attempts > 0
            else 0.0
        )
        confidence = 100.0 * reliability * attribution_coverage
        usable = classified_attempts > 0 and possessions > 0 and opponent_fga > 0
        status = (
            "unavailable"
            if not usable
            else "available"
            if independent_audit_passed and confidence >= confidence_floor
            else "provisional"
        )
        unit_share = (
            _shares(zone_counts, lineup_shrinkage_prior_attempts, team_share)
            if usable
            else None
        )
        for zone in FIELD_GOAL_ZONES:
            tilt = (
                math.log(unit_share[zone] / league_share[zone])
                if status != "unavailable" and unit_share is not None
                else None
            )
            output.append(
                DefensiveLineupConcession(
                    season=evidence.season,
                    team_id=evidence.team_id,
                    lineup_hash=evidence.lineup_hash,
                    player_ids=tuple(evidence.player_ids),
                    zone=zone,
                    zone_concession_tilt=tilt,
                    possessions=possessions,
                    confidence=confidence if status != "unavailable" else None,
                    evidence_status=status,
                    minutes_share=minutes_share,
                    classified_attempts=classified_attempts,
                    opponent_fga=opponent_fga,
                    attribution_coverage=attribution_coverage,
                )
            )
    return output


__all__ = [
    "CONFIDENCE_FLOOR",
    "DefensiveLineupConcession",
    "DefensiveLineupEvidence",
    "EVIDENCE_STATUSES",
    "FIELD_GOAL_ZONES",
    "LeagueSeasonBaseline",
    "LINEUP_SHRINKAGE_PRIOR_ATTEMPTS",
    "MINUTES_SHARE_THRESHOLD",
    "MODEL_VERSION",
    "TEAM_SHRINKAGE_PRIOR_ATTEMPTS",
    "TeamSeasonBaseline",
    "build_concession_profiles",
    "canonical_lineup_hash",
    "select_realistic_units",
]
