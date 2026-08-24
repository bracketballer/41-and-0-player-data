"""Usage normalization and efficiency elasticity for lineup projections.

The module is intentionally independent of PostgreSQL.  The compute command
turns ``player_seasons`` rows into :class:`UsageObservation` values, while
the projection engine can use the normalized usage and elasticity helpers
without knowing anything about database layout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

import numpy as np


MODEL_VERSION = "usage-renormalization-v1"
MIN_SEASONS_PER_PLAYER = 2
MAX_USAGE_DELTA = 5.0
PPS_MINIMUM = 0.0
PPS_MAXIMUM = 3.0
CONFIDENCE_Z = 1.96

# Fit from the 2024–2026 eligible player-season population.  The fitting
# command and the aggregate sample are recorded in docs/USAGE_RENORMALIZATION.md.
V1_USAGE_EFFICIENCY_SLOPE = -0.0015802291118340028


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """One aggregated eligible player-season used by the slope fit.

    ``usage`` is the source standard USG% in percentage-point units.  The
    response is represented by ``true_shooting_pct`` as a fraction and is
    converted to points per shooting possession (``2 * TS%``) by the fit.
    """

    player_id: int
    season: int
    usage: float
    true_shooting_pct: float
    field_goals_attempted: int
    free_throws_attempted: int

    @property
    def shooting_possessions(self) -> float:
        return float(self.field_goals_attempted) + 0.44 * float(
            self.free_throws_attempted
        )


@dataclass(frozen=True, slots=True)
class UsageSlopeFit:
    """Weighted fixed-effects fit and clustered uncertainty summary."""

    coefficient: float
    standard_error: float
    confidence_low: float
    confidence_high: float
    observations: int
    players: int
    seasons: tuple[int, ...]
    weighted_shooting_possessions: float


def _finite(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _validate_observations(
    observations: Iterable[UsageObservation],
) -> list[UsageObservation]:
    rows = list(observations)
    if not rows:
        raise ValueError("at least one usage observation is required")

    seen: set[tuple[int, int]] = set()
    for row in rows:
        key = (int(row.player_id), int(row.season))
        if key in seen:
            raise ValueError(f"duplicate player-season observation: {key}")
        seen.add(key)
        if row.player_id <= 0 or row.season <= 0:
            raise ValueError("player_id and season must be positive")
        usage = _finite(row.usage, name="usage")
        ts = _finite(row.true_shooting_pct, name="true_shooting_pct")
        if usage <= 0 or usage > 100:
            raise ValueError("usage must be in (0, 100]")
        if not 0 < ts <= 1:
            raise ValueError("true_shooting_pct must be in (0, 1]")
        if isinstance(row.field_goals_attempted, bool) or row.field_goals_attempted <= 0:
            raise ValueError("field_goals_attempted must be a positive integer")
        if isinstance(row.free_throws_attempted, bool) or row.free_throws_attempted < 0:
            raise ValueError("free_throws_attempted must be a non-negative integer")
        if not math.isfinite(row.shooting_possessions) or row.shooting_possessions <= 0:
            raise ValueError("shooting possessions must be positive and finite")

    by_player: dict[int, int] = {}
    for row in rows:
        by_player[row.player_id] = by_player.get(row.player_id, 0) + 1
    repeated = [
        player_id
        for player_id, count in by_player.items()
        if count >= MIN_SEASONS_PER_PLAYER
    ]
    if len(repeated) < 3:
        raise ValueError(
            "at least three players with two eligible seasons are required"
        )
    filtered = [row for row in rows if by_player[row.player_id] >= MIN_SEASONS_PER_PLAYER]
    if len({row.season for row in filtered}) < 2:
        raise ValueError("at least two seasons are required")
    return sorted(filtered, key=lambda row: (row.player_id, row.season))


def _fixed_effect_design(rows: Sequence[UsageObservation]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    players = sorted({row.player_id for row in rows})
    seasons = sorted({row.season for row in rows})
    player_index = {player_id: index for index, player_id in enumerate(players)}
    season_index = {season: index for index, season in enumerate(seasons)}

    # Intercept + player effects (reference player omitted) + season effects
    # (reference season omitted) + usage.  The final column is always beta.
    parameter_count = 1 + len(players) - 1 + len(seasons) - 1 + 1
    design = np.zeros((len(rows), parameter_count), dtype=float)
    target = np.empty(len(rows), dtype=float)
    weights = np.empty(len(rows), dtype=float)
    for row_index, row in enumerate(rows):
        design[row_index, 0] = 1.0
        offset = 1
        if player_index[row.player_id] > 0:
            design[row_index, offset + player_index[row.player_id] - 1] = 1.0
        offset += len(players) - 1
        if season_index[row.season] > 0:
            design[row_index, offset + season_index[row.season] - 1] = 1.0
        design[row_index, -1] = row.usage
        target[row_index] = 2.0 * row.true_shooting_pct
        weights[row_index] = row.shooting_possessions
    return design, target, weights


def fit_usage_slope(observations: Iterable[UsageObservation]) -> UsageSlopeFit:
    """Fit the usage elasticity with player and season fixed effects.

    The fit is weighted by shooting possessions and uses a player-clustered
    sandwich covariance.  A caller decides whether the resulting coefficient
    is acceptable for publication; this function reports either sign without
    silently forcing the estimate negative.
    """

    rows = _validate_observations(observations)
    design, target, weights = _fixed_effect_design(rows)
    weighted_design = design * np.sqrt(weights)[:, None]
    weighted_target = target * np.sqrt(weights)
    coefficients, _, rank, _ = np.linalg.lstsq(
        weighted_design, weighted_target, rcond=None
    )
    if rank < design.shape[1]:
        raise ValueError("usage fixed-effects design is rank deficient")

    residuals = target - design @ coefficients
    information = design.T @ (weights[:, None] * design)
    bread = np.linalg.pinv(information)
    cluster_scores: list[np.ndarray] = []
    for player_id in sorted({row.player_id for row in rows}):
        mask = np.asarray([row.player_id == player_id for row in rows])
        cluster_scores.append(
            np.sum(
                design[mask] * (weights[mask] * residuals[mask])[:, None], axis=0
            )
        )
    meat = np.asarray(cluster_scores).T @ np.asarray(cluster_scores)
    covariance = bread @ meat @ bread
    variance = max(float(covariance[-1, -1]), 0.0)
    standard_error = math.sqrt(variance)
    coefficient = float(coefficients[-1])
    return UsageSlopeFit(
        coefficient=coefficient,
        standard_error=standard_error,
        confidence_low=coefficient - CONFIDENCE_Z * standard_error,
        confidence_high=coefficient + CONFIDENCE_Z * standard_error,
        observations=len(rows),
        players=len({row.player_id for row in rows}),
        seasons=tuple(sorted({row.season for row in rows})),
        weighted_shooting_possessions=float(weights.sum()),
    )


def lineups_for_rotation(player_ids: Iterable[int]) -> tuple[tuple[int, ...], ...]:
    """Return deterministic five-player combinations for a rotation."""

    ids = tuple(sorted({int(player_id) for player_id in player_ids}))
    if len(ids) < 5:
        raise ValueError("a rotation must contain at least five unique players")
    return tuple(combinations(ids, 5))


def renormalize_usage(
    usage_rates: Sequence[float] | np.ndarray,
    lineup_indices: Sequence[Sequence[int]] | np.ndarray,
    *,
    max_delta: float = MAX_USAGE_DELTA,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return lineup weights, normalized usage, and capped usage deltas."""

    usage = np.asarray(usage_rates, dtype=float)
    if usage.ndim != 1 or not usage.size or not np.isfinite(usage).all() or (usage <= 0).any():
        raise ValueError("usage_rates must be finite and positive")
    if not math.isfinite(max_delta) or max_delta <= 0:
        raise ValueError("max_delta must be finite and positive")
    lineups = np.asarray(lineup_indices, dtype=int)
    if lineups.ndim != 2 or lineups.shape[1] != 5 or not lineups.size:
        raise ValueError("lineup_indices must have shape (n, 5)")
    if lineups.min() < 0 or lineups.max() >= usage.size:
        raise ValueError("lineup index is outside usage_rates")
    if any(len(set(map(int, lineup))) != 5 for lineup in lineups):
        raise ValueError("each lineup must contain five unique players")
    lineup_usage = usage[lineups]
    totals = lineup_usage.sum(axis=1)
    if not np.isfinite(totals).all() or (totals <= 0).any():
        raise ValueError("lineup usage totals must be positive and finite")
    normalized = 100.0 * lineup_usage / totals[:, None]
    deltas = np.clip(normalized - lineup_usage, -max_delta, max_delta)
    weights = normalized / 100.0
    return weights, normalized, deltas


def score_usage_adjusted_lineups(
    player_efficiencies: Sequence[Sequence[float]] | np.ndarray,
    usage_rates: Sequence[float] | np.ndarray,
    lineup_indices: Sequence[Sequence[int]] | np.ndarray,
    coefficient: float,
    *,
    max_delta: float = MAX_USAGE_DELTA,
) -> np.ndarray:
    """Score every lineup for each opponent defensive unit.

    ``player_efficiencies`` has shape ``(defensive_unit, player)`` and is the
    zone/matchup projection produced upstream.  This function applies only
    usage renormalization, making it the stable interface consumed by T11.
    """

    efficiencies = np.asarray(player_efficiencies, dtype=float)
    usage = np.asarray(usage_rates, dtype=float)
    if efficiencies.ndim != 2 or efficiencies.shape[1] != usage.size or not efficiencies.size:
        raise ValueError("player_efficiencies must have shape (defenses, players)")
    if not np.isfinite(efficiencies).all() or (
        (efficiencies < PPS_MINIMUM).any() or (efficiencies > PPS_MAXIMUM).any()
    ):
        raise ValueError("player efficiencies must be finite values in [0, 3]")
    coefficient = _finite(coefficient, name="coefficient")
    weights, _, deltas = renormalize_usage(
        usage, lineup_indices, max_delta=max_delta
    )
    lineups = np.asarray(lineup_indices, dtype=int)
    adjusted = np.clip(
        efficiencies[:, lineups] + coefficient * deltas[None, :, :],
        PPS_MINIMUM,
        PPS_MAXIMUM,
    )
    return np.sum(adjusted * weights[None, :, :], axis=2)


__all__ = [
    "CONFIDENCE_Z",
    "MAX_USAGE_DELTA",
    "MODEL_VERSION",
    "MIN_SEASONS_PER_PLAYER",
    "V1_USAGE_EFFICIENCY_SLOPE",
    "UsageObservation",
    "UsageSlopeFit",
    "fit_usage_slope",
    "lineups_for_rotation",
    "renormalize_usage",
    "score_usage_adjusted_lineups",
]
