"""Pure calculations for the offensive-projection viability gate.

This module deliberately contains no database or network code.  The ticket
analysis script turns database rows into the small arrays accepted here, which
makes the sample-size gate reproducible and straightforward to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

import numpy as np

from .shot_zones import FIELD_GOAL_ZONES


ZONE_COUNT = len(FIELD_GOAL_ZONES)
ZONE_POINT_VALUES = np.asarray((2.0, 2.0, 2.0, 3.0, 3.0), dtype=float)
DEFAULT_LAMBDA = 0.4620905146480455
DEFAULT_PROFILE_SMOOTHING = 0.5
DEFAULT_ACCURACY_PRIOR_ATTEMPTS = 50.0


@dataclass(frozen=True, slots=True)
class BootstrapGate:
    """Summary of the observed-vs-null spread comparison."""

    observed_median: float
    observed_low: float
    observed_high: float
    null_median: float
    null_high: float
    recommendation: str


def classify_gate(observed_low: float, observed_high: float, null_high: float) -> str:
    """Apply the preregistered issue #8 recommendation rule."""

    values = (observed_low, observed_high, null_high)
    if not all(np.isfinite(value) for value in values):
        raise ValueError("gate bounds must be finite")
    if observed_low > null_high:
        return "GO"
    if observed_high <= null_high:
        return "NO-GO"
    return "SHIP AS EXPLORATION TOOL"


def _zone_array(value: Sequence[float] | np.ndarray, *, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.shape != (ZONE_COUNT,):
        raise ValueError(f"{name} must contain {ZONE_COUNT} zones")
    if not np.isfinite(array).all() or (array < 0).any():
        raise ValueError(f"{name} must contain finite non-negative values")
    return array


def _matrix(value: Sequence[Sequence[float]] | np.ndarray, *, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if array.ndim != 2 or array.shape[1] != ZONE_COUNT:
        raise ValueError(f"{name} must have shape (n, {ZONE_COUNT})")
    if not np.isfinite(array).all() or (array < 0).any():
        raise ValueError(f"{name} must contain finite non-negative values")
    return array


def lineups_for_rotation(player_ids: Iterable[int]) -> tuple[tuple[int, ...], ...]:
    """Return deterministic five-player combinations for a rotation."""

    ids = tuple(sorted({int(player_id) for player_id in player_ids}))
    if len(ids) < 5:
        raise ValueError("a rotation must contain at least five unique players")
    return tuple(combinations(ids, 5))


def median_zone_attempts(
    rows: Iterable[tuple[int, int, Sequence[int] | np.ndarray]],
) -> dict[int, tuple[float, ...]]:
    """Compute per-season zone medians, retaining zero-attempt cells.

    Each input row is ``(season, player_id, counts)``.  Duplicate player-season
    keys are rejected so a caller cannot accidentally weight one player twice.
    """

    values: dict[tuple[int, int], np.ndarray] = {}
    for season, player_id, counts in rows:
        key = (int(season), int(player_id))
        if key in values:
            raise ValueError(f"duplicate player-season row: {key}")
        values[key] = _zone_array(counts, name="counts")
    by_season: dict[int, list[np.ndarray]] = {}
    for (season, _), counts in values.items():
        by_season.setdefault(season, []).append(counts)
    return {
        season: tuple(float(value) for value in np.median(np.asarray(rows), axis=0))
        for season, rows in sorted(by_season.items())
    }


def smoothed_share(
    counts: Sequence[float] | np.ndarray,
    prior_share: Sequence[float] | np.ndarray,
    *,
    pseudo_attempts: float = DEFAULT_PROFILE_SMOOTHING,
) -> np.ndarray:
    """Shrink a composition toward a prior without creating zero logs."""

    values = _zone_array(counts, name="counts")
    prior = _zone_array(prior_share, name="prior_share")
    if pseudo_attempts <= 0 or not np.isfinite(pseudo_attempts):
        raise ValueError("pseudo_attempts must be positive and finite")
    prior_total = float(prior.sum())
    if prior_total <= 0:
        raise ValueError("prior_share must have a positive total")
    prior = prior / prior_total
    total = float(values.sum())
    return (values + pseudo_attempts * prior) / (total + pseudo_attempts)


def _profile_arrays(
    attempts: np.ndarray,
    makes: np.ndarray,
    league_share: np.ndarray,
    league_accuracy: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    attempts = _matrix(attempts, name="attempts")
    makes = _matrix(makes, name="makes")
    if attempts.shape != makes.shape:
        raise ValueError("attempts and makes must have identical shapes")
    league_share = _zone_array(league_share, name="league_share")
    league_accuracy = _zone_array(league_accuracy, name="league_accuracy")
    if (league_accuracy > 1).any():
        raise ValueError("league_accuracy cannot exceed one")
    if (makes > attempts).any():
        raise ValueError("makes cannot exceed attempts")
    shares = np.asarray(
        [smoothed_share(row, league_share) for row in attempts], dtype=float
    )
    accuracy_prior = DEFAULT_ACCURACY_PRIOR_ATTEMPTS
    accuracy = (makes + accuracy_prior * league_accuracy) / (
        attempts + accuracy_prior
    )
    pps = accuracy * ZONE_POINT_VALUES
    return shares, pps


def projected_lineups(
    player_attempts: Sequence[Sequence[float]] | np.ndarray,
    player_makes: Sequence[Sequence[float]] | np.ndarray,
    defense_attempts: Sequence[Sequence[float]] | np.ndarray,
    lineup_indices: Sequence[Sequence[int]] | np.ndarray,
    usage_rates: Sequence[float] | np.ndarray,
    *,
    league_share: Sequence[float] | np.ndarray,
    league_accuracy: Sequence[float] | np.ndarray,
    coefficient: float = DEFAULT_LAMBDA,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(unit_by_lineup_projections, unit_spreads)``.

    The viability proxy uses normalized FGA-per-minute as usage.  The later
    usage-renormalization ticket may replace that weighting, but the gate must
    remain useful before that production model exists.
    """

    player_attempts = _matrix(player_attempts, name="player_attempts")
    player_makes = _matrix(player_makes, name="player_makes")
    defense_attempts = _matrix(defense_attempts, name="defense_attempts")
    if player_attempts.shape != player_makes.shape:
        raise ValueError("player attempts and makes must have identical shapes")
    player_count = player_attempts.shape[0]
    lineups = np.asarray(lineup_indices, dtype=int)
    if lineups.ndim != 2 or lineups.shape[1] != 5:
        raise ValueError("lineup_indices must have shape (n, 5)")
    if lineups.size and (lineups.min() < 0 or lineups.max() >= player_count):
        raise ValueError("lineup index is outside the player profile array")
    usage = np.asarray(usage_rates, dtype=float)
    if usage.shape != (player_count,) or not np.isfinite(usage).all() or (usage <= 0).any():
        raise ValueError("usage_rates must be positive and match the player count")
    if not np.isfinite(coefficient):
        raise ValueError("coefficient must be finite")

    player_shares, player_pps = _profile_arrays(
        player_attempts, player_makes, np.asarray(league_share, dtype=float), np.asarray(league_accuracy, dtype=float)
    )
    league_share = _zone_array(league_share, name="league_share")
    league_share = league_share / league_share.sum()
    defense_shares = np.asarray(
        [smoothed_share(row, league_share) for row in defense_attempts], dtype=float
    )
    tilts = np.log(np.maximum(defense_shares, 1e-15) / np.maximum(league_share, 1e-15))
    adjusted = player_shares[None, :, :] * np.exp(coefficient * tilts[:, None, :])
    adjusted /= adjusted.sum(axis=2, keepdims=True)
    player_projection = np.sum(adjusted * player_pps[None, :, :], axis=2)

    lineup_usage = usage[lineups]
    lineup_weights = lineup_usage / lineup_usage.sum(axis=1, keepdims=True)
    lineup_projection = np.sum(
        player_projection[:, lineups] * lineup_weights[None, :, :], axis=2
    )
    spreads = lineup_projection.max(axis=1) - lineup_projection.min(axis=1)
    return lineup_projection, spreads


def _resample_game_rows(
    rows: Sequence[tuple[np.ndarray, np.ndarray]], rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    if not rows:
        raise ValueError("at least one game row is required")
    attempts = np.asarray([_zone_array(row[0], name="game attempts") for row in rows])
    makes = np.asarray([_zone_array(row[1], name="game makes") for row in rows])
    if (makes > attempts).any():
        raise ValueError("game makes cannot exceed attempts")
    indices = rng.integers(0, len(rows), size=len(rows))
    return attempts[indices].sum(axis=0), makes[indices].sum(axis=0)


def bootstrap_gate(
    player_game_rows: Sequence[Sequence[tuple[np.ndarray, np.ndarray]]],
    defense_game_rows: Sequence[Sequence[tuple[np.ndarray, np.ndarray]]],
    lineup_indices: Sequence[Sequence[int]] | np.ndarray,
    usage_rates: Sequence[float] | np.ndarray,
    *,
    league_share: Sequence[float] | np.ndarray,
    league_accuracy: Sequence[float] | np.ndarray,
    coefficient: float = DEFAULT_LAMBDA,
    replicates: int = 2000,
    seed: int = 20260824,
) -> BootstrapGate:
    """Compare a clustered-bootstrap spread with a sample-size null band.

    The observed distribution resamples game rows independently within each
    player and defensive unit.  The null keeps every observed sample size but
    draws all player and defensive zone compositions from the pooled league
    composition, preserving the 252-lineup multiple-comparison effect.
    """

    if replicates < 100:
        raise ValueError("at least 100 bootstrap replicates are required")
    if len(player_game_rows) == 0 or len(defense_game_rows) == 0:
        raise ValueError("player and defense game rows are required")
    rng = np.random.default_rng(seed)
    league_share_array = _zone_array(league_share, name="league_share")
    league_share_array /= league_share_array.sum()
    league_accuracy_array = _zone_array(league_accuracy, name="league_accuracy")
    if (league_accuracy_array > 1).any():
        raise ValueError("league_accuracy cannot exceed one")
    player_totals = np.asarray(
        [sum((_zone_array(row[0], name="game attempts") for row in rows), np.zeros(ZONE_COUNT)).sum() for rows in player_game_rows],
        dtype=int,
    )
    defense_totals = np.asarray(
        [sum((_zone_array(row[0], name="game attempts") for row in rows), np.zeros(ZONE_COUNT)).sum() for rows in defense_game_rows],
        dtype=int,
    )

    observed = np.empty(replicates, dtype=float)
    null = np.empty(replicates, dtype=float)
    for index in range(replicates):
        player_attempts = []
        player_makes = []
        for rows in player_game_rows:
            attempts, makes = _resample_game_rows(rows, rng)
            player_attempts.append(attempts)
            player_makes.append(makes)
        defense_attempts = []
        for rows in defense_game_rows:
            attempts, _ = _resample_game_rows(rows, rng)
            defense_attempts.append(attempts)
        _, spreads = projected_lineups(
            player_attempts,
            player_makes,
            defense_attempts,
            lineup_indices,
            usage_rates,
            league_share=league_share_array,
            league_accuracy=league_accuracy_array,
            coefficient=coefficient,
        )
        observed[index] = float(np.median(spreads))

        null_player_attempts = [
            rng.multinomial(int(total), league_share_array) for total in player_totals
        ]
        null_player_makes = [
            rng.binomial(attempts, league_accuracy_array)
            for attempts in null_player_attempts
        ]
        null_defense_attempts = [
            rng.multinomial(int(total), league_share_array) for total in defense_totals
        ]
        _, null_spreads = projected_lineups(
            null_player_attempts,
            null_player_makes,
            null_defense_attempts,
            lineup_indices,
            usage_rates,
            league_share=league_share_array,
            league_accuracy=league_accuracy_array,
            coefficient=coefficient,
        )
        null[index] = float(np.median(null_spreads))

    observed_low, observed_high = np.quantile(observed, (0.025, 0.975))
    null_high = float(np.quantile(null, 0.95))
    recommendation = classify_gate(float(observed_low), float(observed_high), null_high)
    return BootstrapGate(
        observed_median=float(np.median(observed)),
        observed_low=float(observed_low),
        observed_high=float(observed_high),
        null_median=float(np.median(null)),
        null_high=null_high,
        recommendation=recommendation,
    )


__all__ = [
    "BootstrapGate",
    "DEFAULT_LAMBDA",
    "DEFAULT_PROFILE_SMOOTHING",
    "FIELD_GOAL_ZONES",
    "ZONE_COUNT",
    "ZONE_POINT_VALUES",
    "bootstrap_gate",
    "classify_gate",
    "lineups_for_rotation",
    "median_zone_attempts",
    "projected_lineups",
    "smoothed_share",
]
