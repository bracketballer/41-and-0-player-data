"""Vectorized offensive-lineup projections.

The database jobs own loading and publishing.  This module owns the pure,
deterministic calculation used by those jobs and by the precompute tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .defense_tilt import FIELD_GOAL_ZONES, V1_DEFENSE_TILT_COEFFICIENTS
from .defensive_lineup_concession import EVIDENCE_STATUSES, canonical_lineup_hash
from .matchup_assignment import (
    DefensiveMatchupPlayer,
    MatchupAssignment,
    OffensiveMatchupPlayer,
    assign_matchups,
)
from .shot_location_profiles import ShotLocationProfile
from .usage_efficiency import (
    PPS_MAXIMUM,
    PPS_MINIMUM,
    V1_USAGE_EFFICIENCY_SLOPE,
    lineups_for_rotation,
    renormalize_usage,
    score_usage_adjusted_lineups,
)


MODEL_VERSION = "shot-location-v1"
INTERVAL_DRAW_COUNT = 2_000
INTERVAL_SEED = 20260824
INTERVAL_LOW_QUANTILE = 0.025
INTERVAL_HIGH_QUANTILE = 0.975
CONFIDENCE_FLOOR = 45.0
SHARE_PRIOR_ATTEMPTS = 0.5
ACCURACY_PRIOR_ATTEMPTS = 50.0

# The fitted standard errors are recorded in the T4/T9 reports.  Keeping the
# values here makes the simulation contract explicit and reproducible without
# requiring a database fit during a pregame calculation.
V1_DEFENSE_TILT_STANDARD_ERROR = 0.0102109263
V1_USAGE_EFFICIENCY_STANDARD_ERROR = 0.0017495433


def _finite(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _canonical_ids(player_ids: Sequence[int], *, label: str) -> tuple[int, ...]:
    values = tuple(int(player_id) for player_id in player_ids)
    if len(values) != 5 or len(set(values)) != 5 or any(value <= 0 for value in values):
        raise ValueError(f"{label} must contain five unique positive player IDs")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class OffensivePlayerEvidence:
    """One eligible offensive player and their five location-profile rows."""

    player_id: int
    usage: float
    handler_score: float
    height: float | None
    profiles: Mapping[str, ShotLocationProfile]


@dataclass(frozen=True, slots=True)
class DefensiveUnitEvidence:
    """One realistic defensive unit and its concession profile."""

    team_id: int
    season: int
    player_ids: tuple[int, ...]
    zone_tilts: Mapping[str, float | None]
    confidence: float | None
    evidence_status: str
    matchup_players: tuple[DefensiveMatchupPlayer, ...]

    @property
    def lineup_hash(self) -> str:
        return canonical_lineup_hash(self.player_ids)[1]


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    """One row for ``lineup_offensive_projections``."""

    season: int
    model_version: str
    offense_team_id: int
    offensive_player_ids: tuple[int, ...]
    defense_team_id: int
    defensive_player_ids: tuple[int, ...]
    projected_pps: float | None
    interval_low: float | None
    interval_high: float | None
    confidence: float | None
    evidence_status: str
    matchup_assignment: tuple[dict[str, object], ...] | None

    @property
    def offensive_lineup_hash(self) -> str:
        return "-".join(str(value) for value in self.offensive_player_ids)

    @property
    def defensive_lineup_hash(self) -> str:
        return "-".join(str(value) for value in self.defensive_player_ids)


def _validate_profiles(player: OffensivePlayerEvidence) -> tuple[np.ndarray, np.ndarray, int]:
    if player.player_id <= 0:
        raise ValueError("offensive player_id must be positive")
    usage = _finite(player.usage, name="usage")
    if usage <= 0 or usage > 100:
        raise ValueError("offensive usage must be in (0, 100]")
    handler_score = _finite(player.handler_score, name="handler_score")
    if not 0 <= handler_score <= 100:
        raise ValueError("handler_score must be in [0, 100]")
    if player.height is not None and (_finite(player.height, name="height") <= 0):
        raise ValueError("height must be positive when supplied")

    shares: list[float] = []
    efficiencies: list[float] = []
    attempts = 0
    if set(player.profiles) != set(FIELD_GOAL_ZONES):
        raise ValueError("offensive player must have exactly five location zones")
    for zone in FIELD_GOAL_ZONES:
        profile = player.profiles[zone]
        if profile.zone != zone:
            raise ValueError(f"profile zone mismatch for {zone}")
        share = _finite(profile.attempt_share, name=f"{zone} attempt_share")
        pps = _finite(profile.adjusted_pps, name=f"{zone} adjusted_pps")
        if not 0 <= share <= 1 or not 0 <= pps <= PPS_MAXIMUM:
            raise ValueError("profile values are outside their permitted ranges")
        if profile.attempts < 0 or profile.makes < 0 or profile.makes > profile.attempts:
            raise ValueError("profile counts are invalid")
        if profile.posterior_alpha <= 0 or profile.posterior_beta <= 0:
            raise ValueError("profile posterior parameters must be positive")
        shares.append(share)
        efficiencies.append(pps)
        attempts += int(profile.attempts)
    if not math.isclose(sum(shares), 1.0, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("profile attempt shares must sum to one")
    return np.asarray(shares, dtype=float), np.asarray(efficiencies, dtype=float), attempts


def _validate_defensive_unit(unit: DefensiveUnitEvidence) -> tuple[np.ndarray, bool]:
    player_ids = _canonical_ids(unit.player_ids, label="defensive lineup")
    if tuple(unit.player_ids) != player_ids:
        raise ValueError("defensive player IDs must already be sorted")
    if unit.evidence_status not in EVIDENCE_STATUSES:
        raise ValueError(f"unknown defensive evidence status: {unit.evidence_status}")
    if unit.confidence is not None:
        confidence = _finite(unit.confidence, name="defensive confidence")
        if not 0 <= confidence <= 100:
            raise ValueError("defensive confidence must be in [0, 100]")
    if set(unit.zone_tilts) != set(FIELD_GOAL_ZONES):
        raise ValueError("defensive unit must have exactly five zone tilts")
    usable = unit.evidence_status != "unavailable" and unit.confidence is not None
    tilts: list[float] = []
    for zone in FIELD_GOAL_ZONES:
        value = unit.zone_tilts[zone]
        if value is None:
            usable = False
            tilts.append(0.0)
            continue
        number = _finite(value, name=f"{zone} defense tilt")
        tilts.append(number)
    if len(unit.matchup_players) != 5:
        usable = False
    return np.asarray(tilts, dtype=float), usable


def _assignment(
    players: Sequence[OffensivePlayerEvidence], unit: DefensiveUnitEvidence
) -> MatchupAssignment:
    return assign_matchups(
        [
            OffensiveMatchupPlayer(
                player_id=player.player_id,
                handler_score=player.handler_score,
                height=player.height,
            )
            for player in players
        ],
        unit.matchup_players,
    )


def _share_concentrations(shares: np.ndarray, attempts: np.ndarray) -> np.ndarray:
    concentration = attempts[:, None] + SHARE_PRIOR_ATTEMPTS
    values = shares * concentration
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("invalid shot-share posterior concentration")
    return values


def _posterior_scores(
    *,
    base_shares: np.ndarray,
    pps: np.ndarray,
    attempts: np.ndarray,
    posterior_alpha: np.ndarray,
    posterior_beta: np.ndarray,
    tilts: np.ndarray,
    lineup_indices: np.ndarray,
    usage_rates: np.ndarray,
    usage_coefficient: float,
    lambda_coefficients: np.ndarray,
    lambda_standard_error: float,
    usage_standard_error: float,
    draws: int,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    player_count = base_shares.shape[0]
    share_alpha = _share_concentrations(base_shares, attempts)
    draw_shares = np.stack(
        [rng.dirichlet(share_alpha[index], size=draws) for index in range(player_count)],
        axis=1,
    )
    draw_accuracy = np.stack(
        [
            rng.beta(
                posterior_alpha[index], posterior_beta[index], size=(draws, len(FIELD_GOAL_ZONES))
            )
            for index in range(player_count)
        ],
        axis=1,
    )
    draw_pps = np.clip(draw_accuracy * np.asarray([2.0, 2.0, 2.0, 3.0, 3.0]), 0.0, 3.0)
    draw_lambdas = rng.normal(
        lambda_coefficients[None, :], lambda_standard_error, size=(draws, len(FIELD_GOAL_ZONES))
    )
    draw_betas = rng.normal(usage_coefficient, usage_standard_error, size=draws)

    logits = (
        np.log(np.maximum(draw_shares[:, None, :, :], np.finfo(float).tiny))
        + draw_lambdas[:, None, None, :] * tilts[None, :, None, :]
    )
    logits -= logits.max(axis=-1, keepdims=True)
    adjusted_shares = np.exp(logits)
    adjusted_shares /= adjusted_shares.sum(axis=-1, keepdims=True)
    player_efficiencies = np.sum(adjusted_shares * draw_pps[:, None, :, :], axis=-1)

    weights, _, deltas = renormalize_usage(usage_rates, lineup_indices)
    adjusted = np.clip(
        player_efficiencies[:, :, lineup_indices]
        + draw_betas[:, None, None, None] * deltas[None, None, :, :],
        PPS_MINIMUM,
        PPS_MAXIMUM,
    )
    return np.sum(adjusted * weights[None, None, :, :], axis=-1)


def project_offensive_matrix(
    players: Sequence[OffensivePlayerEvidence],
    defensive_units: Sequence[DefensiveUnitEvidence],
    *,
    season: int,
    offense_team_id: int,
    model_version: str = MODEL_VERSION,
    usage_coefficient: float = V1_USAGE_EFFICIENCY_SLOPE,
    lambda_coefficients: Mapping[str, float] = V1_DEFENSE_TILT_COEFFICIENTS,
    lambda_standard_error: float = V1_DEFENSE_TILT_STANDARD_ERROR,
    usage_standard_error: float = V1_USAGE_EFFICIENCY_STANDARD_ERROR,
    interval_draws: int = INTERVAL_DRAW_COUNT,
    interval_seed: int = INTERVAL_SEED,
) -> tuple[tuple[tuple[int, ...], ...], tuple[ProjectionRow, ...]]:
    """Compute every offensive lineup against every realistic defensive unit."""

    if len(players) < 5:
        raise ValueError("at least five offensive players are required")
    if not defensive_units:
        raise ValueError("at least one defensive unit is required")
    if isinstance(interval_draws, bool) or int(interval_draws) <= 0:
        raise ValueError("interval_draws must be a positive integer")
    if isinstance(interval_seed, bool) or int(interval_seed) < 0:
        raise ValueError("interval_seed must be a non-negative integer")
    lambda_values = np.asarray(
        [_finite(lambda_coefficients[zone], name=f"{zone} lambda") for zone in FIELD_GOAL_ZONES],
        dtype=float,
    )
    lambda_standard_error = _finite(lambda_standard_error, name="lambda_standard_error")
    usage_standard_error = _finite(usage_standard_error, name="usage_standard_error")
    if lambda_standard_error < 0 or usage_standard_error < 0:
        raise ValueError("standard errors must be non-negative")
    usage_coefficient = _finite(usage_coefficient, name="usage_coefficient")

    by_id: dict[int, OffensivePlayerEvidence] = {}
    for player in players:
        if player.player_id in by_id:
            raise ValueError(f"duplicate offensive player: {player.player_id}")
        _validate_profiles(player)
        by_id[player.player_id] = player
    ordered_players = tuple(by_id[player_id] for player_id in sorted(by_id))
    player_ids = tuple(player.player_id for player in ordered_players)
    lineup_ids = lineups_for_rotation(player_ids)
    player_index = {player_id: index for index, player_id in enumerate(player_ids)}
    lineup_indices = np.asarray(
        [[player_index[player_id] for player_id in lineup] for lineup in lineup_ids], dtype=int
    )

    base_shares = np.asarray([_validate_profiles(player)[0] for player in ordered_players])
    pps = np.asarray([_validate_profiles(player)[1] for player in ordered_players])
    attempts = np.asarray(
        [sum(int(player.profiles[zone].attempts) for zone in FIELD_GOAL_ZONES) for player in ordered_players],
        dtype=float,
    )
    posterior_alpha = np.asarray(
        [[float(player.profiles[zone].posterior_alpha) for zone in FIELD_GOAL_ZONES] for player in ordered_players]
    )
    posterior_beta = np.asarray(
        [[float(player.profiles[zone].posterior_beta) for zone in FIELD_GOAL_ZONES] for player in ordered_players]
    )
    usage_rates = np.asarray([player.usage for player in ordered_players], dtype=float)
    player_reliability = 100.0 * attempts / (attempts + ACCURACY_PRIOR_ATTEMPTS)
    point_coefficients = np.asarray(lambda_values, dtype=float)

    ordered_units = tuple(
        sorted(defensive_units, key=lambda unit: (unit.team_id, unit.lineup_hash))
    )
    unit_tilts = []
    unit_usable: list[bool] = []
    for unit in ordered_units:
        tilts, usable = _validate_defensive_unit(unit)
        unit_tilts.append(tilts)
        unit_usable.append(usable)
    usable_indices = [index for index, usable in enumerate(unit_usable) if usable]
    point_scores = np.zeros((len(usable_indices), len(lineup_ids)), dtype=float)
    interval_scores = np.zeros((len(usable_indices), int(interval_draws), len(lineup_ids)), dtype=float)
    assignments: dict[tuple[int, int], MatchupAssignment] = {}
    for unit_position, unit_index in enumerate(usable_indices):
        unit = ordered_units[unit_index]
        assignment_rows = []
        for lineup_position, lineup in enumerate(lineup_ids):
            lineup_players = tuple(ordered_players[player_index[player_id]] for player_id in lineup)
            assignment = _assignment(lineup_players, unit)
            assignments[(unit_position, lineup_position)] = assignment
            assignment_rows.append(assignment)
        del assignment_rows

    if usable_indices:
        usable_tilts = np.asarray([unit_tilts[index] for index in usable_indices], dtype=float)
        logits = np.log(np.maximum(base_shares[None, :, :], np.finfo(float).tiny))
        logits = logits + point_coefficients[None, None, :] * usable_tilts[:, None, :]
        logits -= logits.max(axis=-1, keepdims=True)
        shares = np.exp(logits)
        shares /= shares.sum(axis=-1, keepdims=True)
        efficiencies = np.sum(shares * pps[None, :, :], axis=-1)
        point_scores = score_usage_adjusted_lineups(
            efficiencies,
            usage_rates,
            lineup_indices,
            usage_coefficient,
        )
        interval_scores = _posterior_scores(
            base_shares=base_shares,
            pps=pps,
            attempts=attempts,
            posterior_alpha=posterior_alpha,
            posterior_beta=posterior_beta,
            tilts=usable_tilts,
            lineup_indices=lineup_indices,
            usage_rates=usage_rates,
            usage_coefficient=usage_coefficient,
            lambda_coefficients=point_coefficients,
            lambda_standard_error=lambda_standard_error,
            usage_standard_error=usage_standard_error,
            draws=int(interval_draws),
            seed=int(interval_seed),
        )

    rows: list[ProjectionRow] = []
    for unit_index, unit in enumerate(ordered_units):
        usable_position = usable_indices.index(unit_index) if unit_index in usable_indices else None
        for lineup_position, lineup in enumerate(lineup_ids):
            assignment = (
                assignments[(usable_position, lineup_position)]
                if usable_position is not None
                else None
            )
            if usable_position is None:
                rows.append(
                    ProjectionRow(
                        season=season,
                        model_version=model_version,
                        offense_team_id=offense_team_id,
                        offensive_player_ids=lineup,
                        defense_team_id=unit.team_id,
                        defensive_player_ids=unit.player_ids,
                        projected_pps=None,
                        interval_low=None,
                        interval_high=None,
                        confidence=None,
                        evidence_status="unavailable",
                        matchup_assignment=None,
                    )
                )
                continue
            confidence = min(
                float(unit.confidence),
                *(float(player_reliability[player_index[player_id]]) for player_id in lineup),
            )
            limitations = list(assignment.limitations)
            limitations.extend(
                limitation
                for pair in assignment.pairs
                for limitation in pair.limitations
            )
            status = (
                "available"
                if unit.evidence_status == "available"
                and confidence >= CONFIDENCE_FLOOR
                and not limitations
                else "provisional"
            )
            draws_for_row = interval_scores[usable_position, :, lineup_position]
            projected = float(point_scores[usable_position, lineup_position])
            interval_low = float(np.quantile(draws_for_row, INTERVAL_LOW_QUANTILE))
            interval_high = float(np.quantile(draws_for_row, INTERVAL_HIGH_QUANTILE))
            # The V36 check constraint requires the point estimate to be inside
            # its advertised interval even when posterior inputs are rounded.
            interval_low = min(interval_low, projected)
            interval_high = max(interval_high, projected)
            rows.append(
                ProjectionRow(
                    season=season,
                    model_version=model_version,
                    offense_team_id=offense_team_id,
                    offensive_player_ids=lineup,
                    defense_team_id=unit.team_id,
                    defensive_player_ids=unit.player_ids,
                    projected_pps=round(projected, 12),
                    interval_low=round(interval_low, 12),
                    interval_high=round(interval_high, 12),
                    confidence=round(confidence, 12),
                    evidence_status=status,
                    matchup_assignment=tuple(assignment.as_json()),
                )
            )
    return lineup_ids, tuple(rows)


__all__ = [
    "ACCURACY_PRIOR_ATTEMPTS",
    "CONFIDENCE_FLOOR",
    "DefensiveUnitEvidence",
    "INTERVAL_DRAW_COUNT",
    "INTERVAL_SEED",
    "MODEL_VERSION",
    "OffensivePlayerEvidence",
    "ProjectionRow",
    "V1_DEFENSE_TILT_STANDARD_ERROR",
    "V1_USAGE_EFFICIENCY_STANDARD_ERROR",
    "project_offensive_matrix",
]
