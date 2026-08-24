# Issue #8: Sample-size reality check and projected-spread gate

This document records the reproducible result for
`bracketballer/41-and-0-player-data#8` and answers §2 of the offensive-lineup
projection checklist before any downstream production model work begins.

## Data contract

- The analysis reads `player_shot_events`, `player_seasons`,
  `team_roster_memberships`, `team_season_eligibility`, and
  `team_game_lineups` in a read-only PostgreSQL session.
- Rotation players are active eligible roster memberships whose
  `player_seasons.minutes >= 100`. The 2026 Virginia Tech anchor is team 340;
  its ten qualifying players produce exactly `C(10, 5) = 252` lineups.
- Field goals are classified with the five coordinate-derived zones from
  `docs/SHOT_ZONES.md`; free throws and unclassifiable coordinates are
  excluded from zone profiles and remain visible in the coverage counts.
- Defensive units are the top ten five-player hashes per eligible team-season,
  ranked by summed `team_game_lineups.total_seconds`. Opponent FGA is read from
  each lineup game's `opponent_stats.fieldGoals.attempted`.

## Reproduction

Use the repository environment and the local development database:

```bash
PYTHONPATH=src \
  /home/namtran/miniforge3/envs/41-and-0-repo/bin/python \
  -m scripts.analysis.analyze_projection_viability \
  --first 2024 --last 2026 --projection-season 2026 \
  --anchor-team 340 --bootstrap-replicates 2000 \
  --bootstrap-seed 20260824 \
  --report data/reports/projection-viability/issue-0008.json
```

The checked-in script writes only aggregate diagnostics. The report below
`data/reports/` is intentionally ignored because it is database-derived.

## §2 quantities

### Median FGA per eligible rotation player-season, by zone

| Season | Rim | Short mid | Long mid | Corner 3 | Above-break 3 | Rotation rows |
|---|---:|---:|---:|---:|---:|---:|
| 2024 | 35.0 | 32.0 | 4.0 | 9.0 | 33.0 | 529 |
| 2025 | 44.0 | 45.0 | 6.0 | 11.0 | 50.0 | 516 |
| 2026 | 44.5 | 40.0 | 4.0 | 12.0 | 52.0 | 442 |

Zero-attempt zone cells are retained in each median. These are classified
coordinate FGA, not a replacement for the raw categorical shot-range totals.

### Median opponent FGA faced by top-ten defensive units

| Season | Units | Median FGA | P05 | P95 |
|---|---:|---:|---:|---:|
| 2024 | 510 | 56.0 | 8.0 | 315.1 |
| 2025 | 510 | 56.0 | 25.0 | 237.75 |
| 2026 | 440 | 66.0 | 33.0 | 290.1 |

The broad tails are expected because the unit statistic is aggregated over
different numbers of games and minutes; they are a reason to retain sample
counts and confidence in later T8 profiles rather than treating every unit as
equally reliable.

## Projected spread and noise gate

The viability proxy uses normalized FGA-per-minute as lineup usage, the
existing 50-attempt accuracy prior, league-centered 0.5-attempt composition
smoothing, and the fitted global defense-tilt coefficient
`λ = 0.4620905146480455`. It applies the defense tilt to each player’s zone
share and reports the max-minus-min PPS across all 252 Virginia Tech lineups.
It intentionally does not include T9 usage elasticity or T10 matchup
assignment.

For 2026, 430 eligible opponent defensive units were available after excluding
Virginia Tech. The direct unit spread had median `0.161813` PPS (P05
`0.144597`, P95 `0.182646`).

The observed interval uses 2,000 game-row bootstrap replicates within each
player and defensive unit. The null bootstrap preserves every observed sample
size but draws player and defensive zone compositions from the pooled league
composition, retaining the 252-lineup multiple-comparison effect.

| Quantity | Result |
|---|---:|
| Observed median spread | 0.174174 PPS |
| Observed 95% interval | [0.123896, 0.226506] PPS |
| Null median spread | 0.064811 PPS |
| Null 95th percentile | 0.095048 PPS |
| Bootstrap seed / replicates | 20260824 / 2,000 |

The preregistered rule is:

- **GO** when the observed 95% lower bound is above the null 95th percentile;
- **NO-GO** when the observed 95% upper bound is at or below the null 95th
  percentile;
- otherwise **SHIP AS EXPLORATION TOOL**.

`0.123896 > 0.095048`, so the explicit recommendation is **GO**. Only this
GO result unlocks the downstream projection sprints.

## Coverage limitation

The 2026 corpus has 428,182 field-goal events, of which 393,768 are classified
and 379,724 contain a resolved five-player defensive `onFloor` set. Exact
matches to an eligible stored `team_game_lineups` unit account for 52,530
classified events. The gate therefore establishes that the observed player
and unit sample sizes produce spread above the finite-sample null; it does not
claim that every NCAA team has equally complete lineup-level evidence. T8 must
carry these coverage and confidence fields forward and must not represent an
unavailable unit as zero evidence.
