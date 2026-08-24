# Offensive lineup projections (`shot-location-v1`)

Issue #13 precomputes the offensive projection matrix consumed by the later
Fastify endpoint. It evaluates every eligible five-player combination for one
offense/opponent matchup against every realistic defensive unit for that
opponent. The persisted row is an indexed lookup; no projection calculation
runs in the request path.

## Point estimate

For player `i` and zone `j`, the defense-adjusted share is:

```text
share_ij = softmax_j(log(base_share_ij) + λ_j * defense_tilt_j)
player_PPS_i = sum_j share_ij * adjusted_PPS_ij
```

The existing usage model then renormalizes the five-player usage budget and
applies the fitted efficiency-vs-usage slope. `assign_matchups` produces one
deterministic defensive assignment per lineup/unit pair; alternate assignments
are never scored or maximized.

## Interval and evidence

The point estimate is accompanied by a deterministic 95% posterior interval.
The precompute uses 2,000 draws with seed `20260824`, sampling each player's
zone shares from the stored composition posterior, zone accuracy from the
stored Beta posterior, and the fitted λ and usage slope from their reported
normal uncertainties. Inputs are canonically ordered before sampling, so a
fixed model version and source digest produce byte-stable results.

Confidence is the minimum of the defensive-unit confidence and the five
offensive-player reliabilities:

```text
player_reliability = 100 * classified_attempts / (classified_attempts + 50)
```

Rows with unavailable required inputs retain lineup identity but have null
scores, intervals, confidence, and assignment. Scored rows are `provisional`
when the defense is provisional, confidence is below 45, a player is prior-only
or thin, or matchup evidence carries limitations. Only fully supported rows
are `available`; missing evidence is never represented as zero.

## Compute and publication

Dry-run the precompute for a matchup with:

```bash
PYTHONPATH=src python -m scripts.compute.compute_lineup_offensive_projections \
  --season 2026 --offense-team-id 340 --defense-team-id 41 \
  --version shot-location-v1 \
  --output-dir data/exports/ticket-input/issue-0013
```

The job is intended to run after nightly profile refresh or explicit pregame
opponent selection. `--apply` records an `import_audit` run and writes the
matrix transactionally. The ticket handler validates the same gzip JSONL
artifact before writing and is the preferred immutable development-release
path. The model version remains inactive until the T12 backtest gate passes.

For a ten-player rotation and ten defensive units, the matrix contains 2,520
rows (`C(10, 5) × 10`). The pure in-memory computation is benchmarked separately
from PostgreSQL and artifact I/O.
