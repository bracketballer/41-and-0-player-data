"""Pure scoring and gate calculations for issue #14.

The database-facing analysis command is deliberately kept separate from this
module.  This file owns the preregistered protocol, paired game bootstrap, and
the deterministic comparison between the matchup model and its neutral-defense
baseline.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np


FIRST_SEASON = 2024
LAST_SEASON = 2026
WARMUP_GAMES = 5
MIN_CELL_FGA = 5
MIN_HOLDOUT_GAMES = 30
MIN_HOLDOUT_FGA = 500
PASSING_MARGIN = 0.05
BOOTSTRAP_REPLICATES = 10_000
BOOTSTRAP_SEED = 20260824


@dataclass(frozen=True, slots=True)
class BacktestProtocol:
    """Immutable issue #14 preregistration.

    The analysis CLI does not expose these values as flags.  A result therefore
    cannot be made to pass by changing a threshold after the data has been
    inspected.
    """

    first_season: int = FIRST_SEASON
    last_season: int = LAST_SEASON
    warmup_games: int = WARMUP_GAMES
    minimum_cell_fga: int = MIN_CELL_FGA
    minimum_holdout_games: int = MIN_HOLDOUT_GAMES
    minimum_holdout_fga: int = MIN_HOLDOUT_FGA
    passing_margin: float = PASSING_MARGIN
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES
    bootstrap_seed: int = BOOTSTRAP_SEED

    def __post_init__(self) -> None:
        if self.first_season > self.last_season:
            raise ValueError("first_season cannot exceed last_season")
        if self.warmup_games < 1 or self.minimum_cell_fga < 1:
            raise ValueError("warmup and minimum cell FGA must be positive")
        if self.minimum_holdout_games < 1 or self.minimum_holdout_fga < 1:
            raise ValueError("minimum holdout size must be positive")
        if not math.isfinite(self.passing_margin) or not 0 < self.passing_margin < 1:
            raise ValueError("passing_margin must be in (0, 1)")
        if self.bootstrap_replicates < 1 or self.bootstrap_seed < 0:
            raise ValueError("bootstrap settings are invalid")

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BacktestCell:
    """One observed offensive-five/defensive-five/game holdout cell."""

    season: int
    game_id: int
    offense_team_id: int
    defense_team_id: int
    offensive_lineup_hash: str
    defensive_lineup_hash: str
    fga: int
    observed_pps: float
    matchup_pps: float
    baseline_pps: float
    interval_low: float
    interval_high: float
    evidence_status: str

    def __post_init__(self) -> None:
        if self.fga < 1:
            raise ValueError("backtest cell FGA must be positive")
        values = (
            self.observed_pps,
            self.matchup_pps,
            self.baseline_pps,
            self.interval_low,
            self.interval_high,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("backtest cell values must be finite")
        if self.interval_low > self.interval_high:
            raise ValueError("backtest interval is reversed")
        if not self.interval_low <= self.matchup_pps <= self.interval_high:
            raise ValueError("matchup point estimate must lie in its interval")
        if self.evidence_status not in {"available", "provisional"}:
            raise ValueError("backtest cells require scored evidence")


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    weighted_mae: float
    total_fga: int
    cell_count: int
    game_count: int


@dataclass(frozen=True, slots=True)
class BootstrapSummary:
    relative_improvement: float
    lower_95: float
    upper_95: float
    replicates: int
    seed: int


@dataclass(frozen=True, slots=True)
class GateDecision:
    status: str
    reason: str
    matchup: ErrorSummary
    baseline: ErrorSummary
    bootstrap: BootstrapSummary | None
    passing_margin: float
    interval_coverage_count_weighted: float
    interval_coverage_fga_weighted: float
    interval_width_median: float

    def as_dict(self) -> dict[str, object]:
        output = asdict(self)
        return output


def _validate_cells(cells: Iterable[BacktestCell]) -> list[BacktestCell]:
    rows = list(cells)
    seen: set[tuple[int, int, str, str]] = set()
    for row in rows:
        key = (
            row.season,
            row.game_id,
            row.offensive_lineup_hash,
            row.defensive_lineup_hash,
        )
        if key in seen:
            raise ValueError(f"duplicate backtest cell: {key}")
        seen.add(key)
    return rows


def _summary(cells: Sequence[BacktestCell], *, matchup: bool) -> ErrorSummary:
    if not cells:
        return ErrorSummary(weighted_mae=float("nan"), total_fga=0, cell_count=0, game_count=0)
    weights = np.asarray([row.fga for row in cells], dtype=float)
    predictions = np.asarray(
        [row.matchup_pps if matchup else row.baseline_pps for row in cells],
        dtype=float,
    )
    observed = np.asarray([row.observed_pps for row in cells], dtype=float)
    return ErrorSummary(
        weighted_mae=float(np.average(np.abs(predictions - observed), weights=weights)),
        total_fga=int(weights.sum()),
        cell_count=len(cells),
        game_count=len({(row.season, row.game_id) for row in cells}),
    )


def weighted_mae(cells: Iterable[BacktestCell], *, matchup: bool) -> float:
    """Return FGA-weighted absolute error for one candidate."""

    rows = _validate_cells(cells)
    value = _summary(rows, matchup=matchup).weighted_mae
    if not math.isfinite(value):
        raise ValueError("cannot compute MAE without scored cells")
    return value


def _game_sums(
    cells: Sequence[BacktestCell], *, matchup: bool
) -> dict[tuple[int, int], tuple[float, float]]:
    """Return ``(absolute-error·FGA, FGA)`` totals grouped by game."""

    output: dict[tuple[int, int], tuple[float, float]] = {}
    for row in cells:
        prediction = row.matchup_pps if matchup else row.baseline_pps
        error, weight = output.get((row.season, row.game_id), (0.0, 0.0))
        output[(row.season, row.game_id)] = (
            error + abs(prediction - row.observed_pps) * row.fga,
            weight + row.fga,
        )
    return output


def paired_game_bootstrap(
    cells: Iterable[BacktestCell],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> BootstrapSummary:
    """Bootstrap relative MAE improvement by resampling games within season."""

    rows = _validate_cells(cells)
    if not rows:
        raise ValueError("cannot bootstrap an empty holdout")
    if replicates < 1 or seed < 0:
        raise ValueError("invalid bootstrap settings")
    matchup_by_game = _game_sums(rows, matchup=True)
    baseline_by_game = _game_sums(rows, matchup=False)
    if set(matchup_by_game) != set(baseline_by_game):
        raise ValueError("matchup and baseline game groups differ")

    rng = np.random.default_rng(seed)
    matchup_error = np.zeros(replicates, dtype=float)
    baseline_error = np.zeros(replicates, dtype=float)
    seasons: dict[int, list[tuple[int, int]]] = {}
    for key in sorted(matchup_by_game):
        seasons.setdefault(key[0], []).append(key)
    for keys in seasons.values():
        indices = rng.integers(0, len(keys), size=(replicates, len(keys)))
        matchup_errors = np.asarray([matchup_by_game[key][0] for key in keys])
        baseline_errors = np.asarray([baseline_by_game[key][0] for key in keys])
        weights = np.asarray([matchup_by_game[key][1] for key in keys])
        matchup_error += matchup_errors[indices].sum(axis=1)
        baseline_error += baseline_errors[indices].sum(axis=1)
        # Both candidates use the same observed FGA denominator for every
        # resampled game, so the denominator cancels in relative improvement.
        del weights
    relative = 1.0 - np.divide(
        matchup_error,
        np.maximum(baseline_error, np.finfo(float).tiny),
    )
    low, high = np.quantile(relative, (0.025, 0.975))
    return BootstrapSummary(
        relative_improvement=float(1.0 - _summary(rows, matchup=True).weighted_mae / _summary(rows, matchup=False).weighted_mae),
        lower_95=float(low),
        upper_95=float(high),
        replicates=replicates,
        seed=seed,
    )


def interval_diagnostics(cells: Sequence[BacktestCell]) -> tuple[float, float, float]:
    """Return count-weighted coverage, FGA-weighted coverage, and median width."""

    if not cells:
        raise ValueError("cannot evaluate intervals without scored cells")
    covered = np.asarray(
        [row.interval_low <= row.observed_pps <= row.interval_high for row in cells],
        dtype=float,
    )
    weights = np.asarray([row.fga for row in cells], dtype=float)
    widths = np.asarray([row.interval_high - row.interval_low for row in cells], dtype=float)
    return (
        float(covered.mean()),
        float(np.average(covered, weights=weights)),
        float(np.median(widths)),
    )


def evaluate_gate(
    cells: Iterable[BacktestCell],
    *,
    protocol: BacktestProtocol = BacktestProtocol(),
) -> GateDecision:
    """Evaluate the preregistered merge gate without touching a database."""

    rows = _validate_cells(cells)
    matchup = _summary(rows, matchup=True)
    baseline = _summary(rows, matchup=False)
    if not rows:
        raise ValueError("cannot evaluate an empty holdout")
    count_coverage, fga_coverage, median_width = interval_diagnostics(rows)
    if matchup.game_count < protocol.minimum_holdout_games:
        return GateDecision(
            status="FAIL",
            reason=f"only {matchup.game_count} holdout games; {protocol.minimum_holdout_games} required",
            matchup=matchup,
            baseline=baseline,
            bootstrap=None,
            passing_margin=protocol.passing_margin,
            interval_coverage_count_weighted=count_coverage,
            interval_coverage_fga_weighted=fga_coverage,
            interval_width_median=median_width,
        )
    if matchup.total_fga < protocol.minimum_holdout_fga:
        return GateDecision(
            status="FAIL",
            reason=f"only {matchup.total_fga} holdout FGA; {protocol.minimum_holdout_fga} required",
            matchup=matchup,
            baseline=baseline,
            bootstrap=None,
            passing_margin=protocol.passing_margin,
            interval_coverage_count_weighted=count_coverage,
            interval_coverage_fga_weighted=fga_coverage,
            interval_width_median=median_width,
        )
    if baseline.weighted_mae <= 0:
        raise ValueError("baseline MAE must be positive")
    bootstrap = paired_game_bootstrap(
        rows,
        replicates=protocol.bootstrap_replicates,
        seed=protocol.bootstrap_seed,
    )
    if bootstrap.relative_improvement >= protocol.passing_margin and bootstrap.lower_95 > 0:
        status = "PASS"
        reason = "matchup model clears the preregistered margin and paired bootstrap bound"
    else:
        status = "FAIL"
        reason = "matchup model does not clear the preregistered margin and bootstrap bound"
    return GateDecision(
        status=status,
        reason=reason,
        matchup=matchup,
        baseline=baseline,
        bootstrap=bootstrap,
        passing_margin=protocol.passing_margin,
        interval_coverage_count_weighted=count_coverage,
        interval_coverage_fga_weighted=fga_coverage,
        interval_width_median=median_width,
    )


__all__ = [
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "BacktestCell",
    "BacktestProtocol",
    "BootstrapSummary",
    "ErrorSummary",
    "GateDecision",
    "evaluate_gate",
    "interval_diagnostics",
    "paired_game_bootstrap",
    "weighted_mae",
]
