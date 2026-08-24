"""Pure calculations for coordinate-derived player shot-location profiles.

The database job supplies classified field-goal counts.  This module keeps the
profile contract independent from PostgreSQL so the estimator can be tested
with small, deterministic fixtures and reused by later projection jobs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

from .shot_zones import FIELD_GOAL_ZONES, ShotZone
from .shooting_ability import PRIOR_ATTEMPTS as ACCURACY_PRIOR_ATTEMPTS


MODEL_VERSION = "shot-location-v1"
MIN_ROTATION_MINUTES = 100
SHARE_PRIOR_ATTEMPTS = 0.5
POINT_VALUES: Mapping[ShotZone, float] = {
    "rim": 2.0,
    "short_mid": 2.0,
    "long_mid": 2.0,
    "corner_three": 3.0,
    "above_break_three": 3.0,
}


@dataclass(frozen=True, slots=True)
class ZoneCount:
    attempts: int = 0
    makes: int = 0


@dataclass(frozen=True, slots=True)
class PlayerShotLocationInput:
    """Classified current-season counts for one player-season."""

    player_id: int
    season: int
    zones: Mapping[str, ZoneCount]


@dataclass(frozen=True, slots=True)
class SeasonShotLocationBaseline:
    """All-corpus classified counts used as that season's league prior."""

    season: int
    zones: Mapping[str, ZoneCount]


@dataclass(frozen=True, slots=True)
class ShotLocationProfile:
    player_id: int
    season: int
    zone: ShotZone
    attempts: int
    makes: int
    attempt_share: float
    posterior_alpha: float
    posterior_beta: float
    adjusted_pps: float


def _zone_count(zones: Mapping[str, ZoneCount], zone: ShotZone) -> ZoneCount:
    count = zones.get(zone, ZoneCount())
    if not isinstance(count, ZoneCount):
        raise TypeError(f"zone {zone!r} must contain a ZoneCount")
    if (
        isinstance(count.attempts, bool)
        or isinstance(count.makes, bool)
        or not isinstance(count.attempts, int)
        or not isinstance(count.makes, int)
    ):
        raise ValueError(f"zone {zone!r} counts must be integers")
    if count.attempts < 0 or count.makes < 0 or count.makes > count.attempts:
        raise ValueError(f"zone {zone!r} has invalid attempts/makes")
    return count


def _baseline_accuracy(count: ZoneCount, zone: ShotZone, season: int) -> float:
    if count.attempts <= 0:
        raise ValueError(f"season {season} has no baseline attempts in {zone}")
    raw = count.makes / count.attempts
    # Match the established shooting-ability estimator: retain positive Beta
    # parameters even if a finite source sample is all makes or all misses.
    return min(0.99, max(0.01, raw))


def build_shot_location_profiles(
    inputs: Iterable[PlayerShotLocationInput],
    baselines: Iterable[SeasonShotLocationBaseline],
    *,
    share_prior_attempts: float = SHARE_PRIOR_ATTEMPTS,
    accuracy_prior_attempts: float = ACCURACY_PRIOR_ATTEMPTS,
) -> list[ShotLocationProfile]:
    """Build five shrunk zone rows for every eligible player-season.

    Profiles are current-season only.  A player with no classified attempts is
    still emitted with the league-season share and PPS prior, which keeps the
    eligible rotation population addressable without pretending that the
    player supplied evidence.
    """

    if not math.isfinite(share_prior_attempts) or share_prior_attempts <= 0:
        raise ValueError("share_prior_attempts must be positive and finite")
    if not math.isfinite(accuracy_prior_attempts) or accuracy_prior_attempts <= 0:
        raise ValueError("accuracy_prior_attempts must be positive and finite")

    baseline_by_season: dict[int, SeasonShotLocationBaseline] = {}
    for baseline in baselines:
        season = int(baseline.season)
        if season in baseline_by_season:
            raise ValueError(f"duplicate season baseline: {season}")
        baseline_by_season[season] = baseline

    input_by_key: dict[tuple[int, int], PlayerShotLocationInput] = {}
    for item in inputs:
        key = (int(item.season), int(item.player_id))
        if key in input_by_key:
            raise ValueError(f"duplicate player-season input: {key}")
        input_by_key[key] = item

    output: list[ShotLocationProfile] = []
    for (season, player_id), item in sorted(input_by_key.items()):
        baseline = baseline_by_season.get(season)
        if baseline is None:
            raise ValueError(f"missing season baseline: {season}")

        baseline_counts = {
            zone: _zone_count(baseline.zones, zone) for zone in FIELD_GOAL_ZONES
        }
        baseline_total = sum(count.attempts for count in baseline_counts.values())
        if baseline_total <= 0:
            raise ValueError(f"season {season} has no classified baseline attempts")
        baseline_shares = {
            zone: count.attempts / baseline_total
            for zone, count in baseline_counts.items()
        }
        baseline_accuracy = {
            zone: _baseline_accuracy(count, zone, season)
            for zone, count in baseline_counts.items()
        }

        counts = {zone: _zone_count(item.zones, zone) for zone in FIELD_GOAL_ZONES}
        player_total = sum(count.attempts for count in counts.values())
        share_denominator = player_total + share_prior_attempts
        for zone in FIELD_GOAL_ZONES:
            count = counts[zone]
            alpha = count.makes + accuracy_prior_attempts * baseline_accuracy[zone]
            beta = (
                count.attempts
                - count.makes
                + accuracy_prior_attempts * (1.0 - baseline_accuracy[zone])
            )
            adjusted_accuracy = alpha / (alpha + beta)
            adjusted_pps = POINT_VALUES[zone] * adjusted_accuracy
            output.append(
                ShotLocationProfile(
                    player_id=player_id,
                    season=season,
                    zone=zone,
                    attempts=count.attempts,
                    makes=count.makes,
                    attempt_share=(
                        count.attempts + share_prior_attempts * baseline_shares[zone]
                    )
                    / share_denominator,
                    posterior_alpha=alpha,
                    posterior_beta=beta,
                    adjusted_pps=adjusted_pps,
                )
            )

    return output


__all__ = [
    "ACCURACY_PRIOR_ATTEMPTS",
    "FIELD_GOAL_ZONES",
    "MIN_ROTATION_MINUTES",
    "MODEL_VERSION",
    "POINT_VALUES",
    "PlayerShotLocationInput",
    "SeasonShotLocationBaseline",
    "SHARE_PRIOR_ATTEMPTS",
    "ShotLocationProfile",
    "ZoneCount",
    "build_shot_location_profiles",
]
