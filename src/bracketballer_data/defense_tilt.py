"""Team-level estimation of defensive shot-location tilt.

The estimator uses a multinomial log-linear model over the coordinate-derived
five-zone contract.  Profiles used for a game exclude that game's observations
so the fitted coefficient cannot learn from its own response.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np

from .shot_zones import FIELD_GOAL_ZONES, ShotZone

ZONE_ORDER: tuple[ShotZone, ...] = FIELD_GOAL_ZONES
ZONE_COUNT = len(ZONE_ORDER)
PROFILE_SMOOTHING = 0.5

# Fitted from the 2024-2026 coordinate-derived shot export.  The per-zone
# mapping repeats the selected global coefficient so downstream projection
# code has one stable interface even when v1 selects the simpler model.
V1_DEFENSE_TILT_MODEL = "global"
V1_DEFENSE_TILT_COEFFICIENTS: Mapping[ShotZone, float] = {
    "rim": 0.4620905146480455,
    "short_mid": 0.4620905146480455,
    "long_mid": 0.4620905146480455,
    "corner_three": 0.4620905146480455,
    "above_break_three": 0.4620905146480455,
}


@dataclass(frozen=True, slots=True)
class TeamGameZoneCounts:
    season: int
    game_id: int
    offense_team_id: int
    defense_team_id: int
    counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.counts) != ZONE_COUNT:
            raise ValueError(f"expected {ZONE_COUNT} zone counts")
        if any(count < 0 for count in self.counts):
            raise ValueError("zone counts cannot be negative")
        if self.offense_team_id == self.defense_team_id:
            raise ValueError("offense and defense teams must differ")


@dataclass(frozen=True, slots=True)
class TiltFeatures:
    counts: np.ndarray
    baseline: np.ndarray
    tilt: np.ndarray
    games: tuple[int, ...]
    seasons: tuple[int, ...]

    @property
    def totals(self) -> np.ndarray:
        return self.counts.sum(axis=1)


@dataclass(frozen=True, slots=True)
class CoefficientFit:
    coefficients: tuple[float, ...]
    standard_errors: tuple[float, ...]
    log_likelihood: float
    observations: int
    attempts: int

    @property
    def coefficient(self) -> float:
        if len(self.coefficients) != 1:
            raise AttributeError("fit has per-zone coefficients")
        return self.coefficients[0]

    @property
    def standard_error(self) -> float:
        if len(self.standard_errors) != 1:
            raise AttributeError("fit has per-zone standard errors")
        return self.standard_errors[0]


def _as_counts(row: TeamGameZoneCounts) -> np.ndarray:
    return np.asarray(row.counts, dtype=float)


def build_leave_one_game_out_features(
    observations: Sequence[TeamGameZoneCounts],
    *,
    smoothing: float = PROFILE_SMOOTHING,
) -> TiltFeatures:
    """Create baseline and defense-tilt features without target leakage."""

    if smoothing <= 0 or not math.isfinite(smoothing):
        raise ValueError("smoothing must be positive and finite")
    if not observations:
        raise ValueError("at least one team-game observation is required")

    rows = [_as_counts(row) for row in observations]
    league: dict[int, np.ndarray] = defaultdict(lambda: np.zeros(ZONE_COUNT))
    offense: dict[tuple[int, int], np.ndarray] = defaultdict(lambda: np.zeros(ZONE_COUNT))
    defense: dict[tuple[int, int], np.ndarray] = defaultdict(lambda: np.zeros(ZONE_COUNT))
    for row, counts in zip(observations, rows):
        league[row.season] += counts
        offense[(row.season, row.offense_team_id)] += counts
        defense[(row.season, row.defense_team_id)] += counts

    output_counts: list[np.ndarray] = []
    output_baselines: list[np.ndarray] = []
    output_tilts: list[np.ndarray] = []
    output_games: list[int] = []
    output_seasons: list[int] = []
    for row, counts in zip(observations, rows):
        league_excluding = league[row.season] - counts
        league_total = league_excluding.sum()
        if league_total <= 0:
            continue
        league_share = (league_excluding + smoothing) / (
            league_total + smoothing * ZONE_COUNT
        )
        offense_excluding = offense[(row.season, row.offense_team_id)] - counts
        defense_excluding = defense[(row.season, row.defense_team_id)] - counts
        offense_total = offense_excluding.sum()
        defense_total = defense_excluding.sum()
        if offense_total <= 0 or defense_total <= 0:
            continue
        baseline = (offense_excluding + smoothing * league_share) / (
            offense_total + smoothing
        )
        concession = (defense_excluding + smoothing * league_share) / (
            defense_total + smoothing
        )
        output_counts.append(counts)
        output_baselines.append(baseline)
        output_tilts.append(np.log(concession / league_share))
        output_games.append(row.game_id)
        output_seasons.append(row.season)

    if not output_counts:
        raise ValueError("no observations have usable leave-one-game-out profiles")
    return TiltFeatures(
        counts=np.asarray(output_counts, dtype=float),
        baseline=np.asarray(output_baselines, dtype=float),
        tilt=np.asarray(output_tilts, dtype=float),
        games=tuple(output_games),
        seasons=tuple(output_seasons),
    )


def _probabilities(features: TiltFeatures, coefficients: np.ndarray) -> np.ndarray:
    logits = np.log(features.baseline) + features.tilt * coefficients
    logits -= logits.max(axis=1, keepdims=True)
    probabilities = np.exp(logits)
    return probabilities / probabilities.sum(axis=1, keepdims=True)


def _log_likelihood(features: TiltFeatures, coefficients: np.ndarray) -> float:
    probabilities = _probabilities(features, coefficients)
    return float(np.sum(features.counts * np.log(np.maximum(probabilities, 1e-300))))


def _scores(features: TiltFeatures, probabilities: np.ndarray) -> np.ndarray:
    residuals = features.counts - features.totals[:, None] * probabilities
    return residuals * features.tilt


def _information(features: TiltFeatures, probabilities: np.ndarray) -> np.ndarray:
    matrix = np.zeros((probabilities.shape[1], probabilities.shape[1]))
    for counts, probability, tilt in zip(features.counts, probabilities, features.tilt):
        total = counts.sum()
        covariance = np.diag(probability) - np.outer(probability, probability)
        design = np.diag(tilt)
        matrix += total * design @ covariance @ design
    return matrix


def _global_information(features: TiltFeatures, probabilities: np.ndarray) -> float:
    information = 0.0
    for counts, probability, tilt in zip(features.counts, probabilities, features.tilt):
        total = counts.sum()
        mean = float(np.dot(probability, tilt))
        variance = float(np.dot(probability, (tilt - mean) ** 2))
        information += total * variance
    return information


def _cluster_scores(features: TiltFeatures, probabilities: np.ndarray) -> np.ndarray:
    scores = _scores(features, probabilities)
    grouped: dict[int, np.ndarray] = {}
    for game, score in zip(features.games, scores):
        grouped[game] = grouped.get(game, np.zeros(scores.shape[1])) + score
    return np.asarray(list(grouped.values()))


def _global_cluster_scores(features: TiltFeatures, probabilities: np.ndarray) -> np.ndarray:
    residuals = features.counts - features.totals[:, None] * probabilities
    scores = (residuals * features.tilt).sum(axis=1)
    grouped: dict[int, float] = {}
    for game, score in zip(features.games, scores):
        grouped[game] = grouped.get(game, 0.0) + float(score)
    return np.asarray(list(grouped.values()), dtype=float)


def _fit(features: TiltFeatures, dimension: int, *, max_iterations: int = 100) -> CoefficientFit:
    coefficients = np.ones(dimension, dtype=float)
    current = _log_likelihood(features, coefficients)
    for _ in range(max_iterations):
        probabilities = _probabilities(features, coefficients)
        if dimension == 1:
            scores = np.asarray([float(_scores(features, probabilities).sum())])
            information = np.asarray([[_global_information(features, probabilities)]])
        else:
            scores = _scores(features, probabilities).sum(axis=0)
            information = _information(features, probabilities)
        try:
            step = np.linalg.solve(information + np.eye(dimension) * 1e-10, scores)
        except np.linalg.LinAlgError as error:
            raise ValueError("defense-tilt information matrix is singular") from error
        if float(np.max(np.abs(step))) < 1e-9:
            break
        scale = 1.0
        while scale >= 1e-6:
            candidate = coefficients + scale * step
            candidate_likelihood = _log_likelihood(features, candidate)
            if candidate_likelihood >= current:
                coefficients = candidate
                current = candidate_likelihood
                break
            scale *= 0.5
        else:
            break

    probabilities = _probabilities(features, coefficients)
    if dimension == 1:
        information = np.asarray([[_global_information(features, probabilities)]])
    else:
        information = _information(features, probabilities)
    bread = np.linalg.pinv(information)
    if dimension == 1:
        clusters = _global_cluster_scores(features, probabilities)[:, None]
    else:
        clusters = _cluster_scores(features, probabilities)
    meat = clusters.T @ clusters
    covariance = bread @ meat @ bread
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    return CoefficientFit(
        coefficients=tuple(float(value) for value in coefficients),
        standard_errors=tuple(float(value) for value in standard_errors),
        log_likelihood=current,
        observations=len(features.games),
        attempts=int(features.totals.sum()),
    )


def fit_global_lambda(features: TiltFeatures) -> CoefficientFit:
    """Fit one coefficient shared by all zones."""

    return _fit(features, 1)


def fit_per_zone_lambda(features: TiltFeatures) -> CoefficientFit:
    """Fit one coefficient per zone."""

    return _fit(features, ZONE_COUNT)


def predict_log_loss(features: TiltFeatures, fit: CoefficientFit) -> float:
    coefficients = np.asarray(fit.coefficients, dtype=float)
    if len(coefficients) == 1:
        coefficients = np.repeat(coefficients, ZONE_COUNT)
    return float(-_log_likelihood(features, coefficients) / max(int(features.totals.sum()), 1))


def select_model(
    global_fit: CoefficientFit,
    per_zone_fit: CoefficientFit,
    held_out_global_losses: Sequence[float],
    held_out_per_zone_losses: Sequence[float],
    *,
    minimum_improvement: float = 0.01,
) -> str:
    """Prefer per-zone only with a consistent, material held-out gain."""

    if len(held_out_global_losses) != len(held_out_per_zone_losses) or not held_out_global_losses:
        raise ValueError("held-out loss arrays must be non-empty and equal length")
    global_mean = float(np.mean(held_out_global_losses))
    per_zone_mean = float(np.mean(held_out_per_zone_losses))
    improves_every_fold = all(
        per_zone <= baseline
        for per_zone, baseline in zip(held_out_per_zone_losses, held_out_global_losses)
    )
    material_gain = per_zone_mean <= global_mean * (1.0 - minimum_improvement)
    if improves_every_fold and material_gain:
        return "per_zone"
    return "global"


__all__ = [
    "CoefficientFit",
    "PROFILE_SMOOTHING",
    "TeamGameZoneCounts",
    "TiltFeatures",
    "V1_DEFENSE_TILT_COEFFICIENTS",
    "V1_DEFENSE_TILT_MODEL",
    "ZONE_ORDER",
    "build_leave_one_game_out_features",
    "fit_global_lambda",
    "fit_per_zone_lambda",
    "predict_log_loss",
    "select_model",
]
