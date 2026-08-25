# Offensive projection backtest (issue #14)

This document is the preregistration and final report for the T12 merge gate.
The protocol below is committed before the canonical database run. The result
section is appended only after that run completes.

## Preregistered protocol

- **Holdout:** final games from 2024–2026, ordered by `(start_date, game_id)`.
  A game is eligible after both teams have five earlier final games in the same
  season. All profile, usage, and defensive-unit inputs stop immediately before
  the held-out game.
- **Target:** observed offensive-five/defensive-five/game cells resolved from
  `onFloor` and matching stored five-player lineups. Cells need at least five
  classified field-goal attempts. Actual PPS is classified field-goal points
  divided by classified FGA.
- **Matchup candidate:** the T11 projection engine with prior-only player
  profiles and defensive concession tilts. Prior usage is shooting-possession
  share (`FGA + 0.44 × FTA`) and is passed through the production usage
  renormalization.
- **Naive baseline:** the same profiles, usage, shrinkage, and usage-elasticity
  adjustment with every defensive tilt set to zero. This is the pre-registered
  opponent-agnostic player-season-average baseline.
- **Primary metric:** FGA-weighted mean absolute error,
  `sum(FGA × abs(predicted_PPS − observed_PPS)) / sum(FGA)`.
- **Bootstrap:** 10,000 paired game-cluster replicates, stratified within
  season, seed `20260824`. Relative improvement is
  `1 − matchup_MAE / baseline_MAE`.
- **Pass rule:** at least 30 holdout games and 500 classified FGA; point
  improvement at least 5%; and the paired-bootstrap 95% lower bound above 0%.
  A failure or insufficient sample blocks Sprint 3.
- **Intervals:** report count-weighted and FGA-weighted empirical coverage and
  median width as diagnostics only. Interval coverage is not a second gate
  because T11 intervals represent parameter uncertainty, not shot sampling
  noise.
- **Reviewer:** GauravR0 must independently review the protocol and result
  before the gate is accepted. GitHub comments are drafted in the result report
  but are not posted by this implementation.

The canonical read-only command is:

```bash
PYTHONPATH=src python -m scripts.analysis.backtest_offensive_projection \
  --report data/reports/offensive-projection-backtest/issue-0014.json
```

The report below `data/reports/` is database-derived and ignored by Git. It
contains the source digest, coverage, exclusions, per-season diagnostics,
bootstrap result, and gate decision.

## Result

Pending the canonical database run and independent review.

## Ready-to-post update

### Player-data #14

> T12 backtest result: pending the canonical read-only run and GauravR0 review.

### Frontend #4 §10

> Holdout, baseline, metric, margin, interval handling, and reviewer are
> preregistered in `docs/OFFENSIVE_PROJECTION_BACKTEST.md`; the gate result will
> be added after the rolling 2024–2026 run.
