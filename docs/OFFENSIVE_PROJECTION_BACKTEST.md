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

Canonical read-only run completed on 2026-08-24. A second identical run
produced the same report bytes (SHA-256
`09527373269013b3c0e13b9a311889fecf7d739cb53f7c5412e399526c2f8b3f`).

| Quantity | Result |
|---|---:|
| Eligible team-seasons | 148 |
| Final games / post-warmup games | 4,111 / 1,017 |
| Scored holdout games / cells / FGA | 625 / 2,811 / 20,978 |
| Matchup FGA-weighted MAE | 0.3450624512 PPS |
| Neutral-baseline FGA-weighted MAE | 0.3463281204 PPS |
| Relative improvement | 0.3654538% |
| Paired bootstrap 95% interval | [0.1906364%, 0.5356870%] |
| Interval coverage (count / FGA weighted) | 1.8499% / 2.3167% |

The result is **FAIL**: the matchup model improves point error slightly, but
does not reach the preregistered 5% margin. The interval diagnostic is also far
below nominal coverage, as expected for intervals that do not include shot
sampling noise; it is descriptive and does not change the gate decision.

Per-season relative improvements were 0.2947% (2024), 0.4584% (2025), and
0.2910% (2026). The full exclusion breakdown and source digest are in the
ignored machine-readable report at
`data/reports/offensive-projection-backtest/issue-0014.json`.

Sprint 3 remains blocked: the projection model must not be activated and T13–T16
must not ship from this result. GauravR0 review is still required before the
gate is formally accepted.

## Ready-to-post update

### Player-data #14

> T12 backtest result: FAIL. The rolling 2024–2026 holdout scored 625 games,
> 2,811 cells, and 20,978 FGA. Matchup MAE improved 0.3655% over the neutral
> player-season baseline (95% paired game bootstrap interval [0.1906%,
> 0.5357%]), below the preregistered 5% margin. Sprint 3 remains blocked.

### Frontend #4 §10

> T12 §10 is answered in `docs/OFFENSIVE_PROJECTION_BACKTEST.md`: the rolling
> 2024–2026 holdout and neutral-defense player-season baseline were evaluated
> with a 5% MAE gate, interval coverage was diagnostic-only, and GauravR0 is the
> required reviewer. The result is FAIL, so Sprint 3 remains blocked.
