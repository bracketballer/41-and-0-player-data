# Usage renormalization (`usage-renormalization-v1`)

This document records the usage model required by issue #11. It is the
elasticity layer consumed by the later lineup projection engine; it does not
choose matchup assignments or publish projection rows.

## Population and fit

The source population is the T7 eligible rotation population: active roster
memberships on `team_season_eligibility` teams with
`player_seasons.minutes >= 100`, restricted to seasons 2024–2026. Same-season
transfer rows are aggregated by stable player ID. Usage is the source
`player_seasons.usage` standard USG% in percentage-point units, weighted by
minutes when multiple team rows are combined.

The fit keeps only player-seasons with non-null usage and positive field-goal
attempts, and only players represented in at least two eligible seasons. The
efficiency response is recomputed from box-score totals as points per shooting
possession:

```text
efficiency_it = 2 × true_shooting_pct_it
              = points_it / (FGA_it + 0.44 × FTA_it)
```

The fitted model is a weighted fixed-effects regression:

```text
efficiency_it = player_effect_i + season_effect_t
              + β × usage_it + error_it
```

Rows are weighted by `FGA + 0.44 × FTA`; uncertainty is a player-clustered
sandwich standard error. The fit used 746 player-seasons from 335 players,
with 190,929.8 weighted shooting possessions across seasons 2024–2026.

| Quantity | Value |
| --- | ---: |
| β (PPS per USG percentage point) | -0.0015802291 |
| Clustered standard error | 0.0017495433 |
| 95% interval | [-0.0050093340, 0.0018488758] |

The point estimate is negative, so it is accepted for v1. The interval is
retained as a warning about uncertainty; the model does not silently force a
negative coefficient when a future refresh changes its sign.

## Lineup renormalization contract

For a lineup `L`, baseline usage is renormalized to the lineup's 100-point
usage budget:

```text
normalized_usage_i = 100 × baseline_usage_i / Σ(k∈L) baseline_usage_k
usage_delta_i      = clip(normalized_usage_i - baseline_usage_i, -5, +5)
adjusted_pps_i     = clip(base_pps_i + β × usage_delta_i, 0, 3)
lineup_pps         = Σ(i∈L) (normalized_usage_i / 100) × adjusted_pps_i
```

The ±5 percentage-point cap limits the efficiency correction; the normalized
weights still sum to one. Missing, zero, or non-finite usage is unavailable
evidence and must not be converted to zero. The pure scoring function accepts
one row of player efficiencies per opponent defensive unit and returns every
lineup score deterministically.

## Opponent-divergence sanity test

The unit suite evaluates all `C(10, 5) = 252` lineups for two fixed synthetic
defensive units. The winning lineup must differ between opponents, each winner
must lead the runner-up by at least 0.01 PPS, and all scores must be finite.
This is a build failure if it regresses.

## Reproduction

The database command is read-only and writes only an aggregate report under
the ignored `data/reports/` tree:

```bash
PYTHONPATH=src python3 -m scripts.compute.fit_usage_efficiency \
  --first 2024 --last 2026 \
  --report data/reports/usage-efficiency/usage-efficiency-fit.json
```

The application constant is
`src/bracketballer_data/usage_efficiency.py:V1_USAGE_EFFICIENCY_SLOPE`.
