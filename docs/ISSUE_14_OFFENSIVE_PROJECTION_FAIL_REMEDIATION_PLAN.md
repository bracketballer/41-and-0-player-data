# Offensive Projection T12 Failure Remediation

**Handoff document for the next coding agent**  
**Related ticket:** `bracketballer/41-and-0-player-data#14` (T12 merge gate)  
**Current model:** `shot-location-v1`  
**Status:** v1 failed the preregistered gate; downstream coach-facing work remains blocked.

## 1. Current result and immediate mitigation

Issue #14 is a binding failure, not a threshold-tuning problem. The canonical rolling
2024–2026 run produced:

| Measure | Result |
|---|---:|
| Eligible games | 625 |
| Scored cells | 2,811 |
| Classified FGA | 20,978 |
| Matchup MAE | 0.3450624512 |
| Neutral-baseline MAE | 0.3463281204 |
| Relative improvement | 0.3654538% |
| Paired game-bootstrap 95% interval | [0.1906364%, 0.5356870%] |
| Required improvement | 5% |
| Interval coverage (count/FGA weighted) | 1.8499% / 2.3167% |

The current gain is approximately 13.7 times smaller than the required reduction. Keep
`docs/OFFENSIVE_PROJECTION_BACKTEST.md`, its report artifact, protocol, seed, and result
unchanged as the audit record.

Until a replacement passes validation:

- Do not activate or publish `shot-location-v1` to consumers.
- Do not implement or unblock T13–T16 (Fastify endpoint, frontend surface, ranking UI,
  or rollout).
- Do not lower the 5% margin, replace the baseline, or reinterpret the existing FAIL as
  a pass.
- Close or mark #14 as completed-with-FAIL only after the recorded reviewer has approved
  the reproducible report; track remediation in a separate follow-up ticket.

## 2. Root cause to address

The v1 engine changes only the player's zone attempt shares:

```text
share_ij ∝ exp(log(player_i_base_share_j) + λ × defense_tilt_j)
projected_PPS_i = Σ_j share_ij × player_i_PPS_j
```

The heuristic matchup assignment is serialized metadata and does not alter the score.
There is no defender-player assignment data in the current CBBD corpus, and v1 has no
within-zone make-probability suppression. Exact defensive-unit evidence is also sparse:
2024–2025 require team-level fallback, while the independent temporal lineup audit is
still pending. The current intervals describe parameter draws only, which explains their
roughly 2% empirical coverage against noisy observed cells.

## 3. Existing-data v2 implementation

Create a separate, versioned v2 research path. Do not mutate v1 constants or make the v1
backtest report depend on v2 code.

### Model

1. Preserve the v1 shot-share shift as one candidate component.
2. Add a leakage-safe zone make-probability model. Use each offensive player's pregame
   posterior accuracy as the baseline and estimate partially pooled defensive effects by
   season/zone:
   - defense team-zone effect;
   - additive on-floor defender-zone effects;
   - exact defensive-unit residual for sufficiently observed units.
3. Shrink effects through league → team → defender/unit evidence. A novel or thin unit
   must fall back deterministically to better-supported levels.
4. Keep heuristic assignments as descriptive metadata. Do not hand-assign defender
   effects or give the heuristic scoring weight without fitted evidence or a new tracking
   source.
5. Compare these preregistered candidates:
   - neutral baseline;
   - v1 share-shift only;
   - accuracy suppression only;
   - combined share shift and accuracy suppression.

Use the simplest candidate within one standard error of the best candidate, only if it
improves every development season and reaches at least 5% pooled rolling-CV improvement
with a positive paired-bootstrap lower bound. Otherwise stop the v2 attempt before any
production work.

### Data quality prerequisites

- Complete the independent 150-event lineup audit. Do not mark attribution `available`
  until it meets the documented exact-five and confidence requirements.
- Backfill full rosters for 2024 and 2025 and recompute coverage. Until then, use the
  documented team-level fallback for historical seasons.
- Recompute all v2 features prior to each target game using only information available
  before that game. Future shots, lineup outcomes, or post-game aggregates must not enter
  a target projection.
- If a data backfill or model seed is published, use an immutable ticketed release with
  validated artifacts, checksums, row counts, and an idempotent `scripts/dev_sync/tickets/`
  handler. Do not add a second migration set to this repository.

### Intervals

Replace the current parameter-only intervals with calibrated predictive intervals that
include parameter uncertainty and finite-shot outcome noise. Fit calibration only on
training data, using game-clustered and FGA-stratified residuals (for example, conformal
residual correction). The nominal 95% interval must be checked separately from point-score
accuracy.

During research, keep the interval definition explicit: expected PPS uncertainty and
realized-cell predictive uncertainty are different quantities. The backtest interval
must be the latter when measuring coverage; the eventual UI must label whichever quantity
it exposes.

## 4. Validation and release gates

### Development phase

Use 2024–2026 only for chronological, prior-only nested cross-validation. Freeze before
holdout scoring:

- feature definitions and fallback order;
- hyperparameters and model-selection rule;
- baseline and inclusion/exclusion rules;
- bootstrap and random seeds;
- interval calibration procedure;
- model artifact checksum and input-release versions.

Report every candidate's pooled and per-season MAE, bootstrap interval, interval coverage,
sample counts, exclusions, and deterministic artifact checksum. Add secondary ranking or
marginal-delta diagnostics only as evidence about coach usefulness; they cannot replace
the primary MAE gate.

### Untouched 2027 holdout

After the v2 artifact is frozen, evaluate it once on the completed 2027 season using the
same baseline and protocol as issue #14, with the season label changed to 2027 and the
same five-game warmup and minimum-cell rules. Do not tune against these results.

The v2 production gate requires all of the following:

- at least 30 eligible games and 500 classified FGA;
- at least 5% FGA-weighted MAE improvement over the neutral baseline;
- season-stratified paired game-bootstrap 95% lower bound above zero;
- nominal 95% interval FGA-weighted coverage between 90% and 98%;
- review and approval of the frozen protocol and report by GauravR0.

If any condition fails or the sample is insufficient, keep T13–T16 blocked. A subsequent
attempt requires a new model version and a new untouched holdout; never revise the v1 or
v2 result after seeing it.

## 5. Interfaces, storage, and tests

During research, keep the public API and Fastify schema unchanged. The existing
`lineup_offensive_projections` table is already versioned by `model_version` and can hold
passing v2 rows. Only after a holdout PASS should production projection publication be
planned. If a later production design requires new persisted defensive-effect fields,
the Fastify repository must own the Flyway migration, Prisma update, generated entities,
and any dependent ticketed data release.

The v2 implementation must include tests for:

- temporal leakage prevention;
- hierarchy fallback and shrinkage for novel/thin units;
- valid probability/PPS bounds and player-order invariance;
- synthetic recovery of known defensive effects;
- interval calibration and coverage;
- deterministic repeated runs and artifact checksums;
- regression protection proving the v1 failure report remains unchanged.

No licensed matchup-tracking source, internal-only coach preview, or frontend/service
activation is part of this remediation path. If exact player-to-player matchup effects
are eventually required, treat acquisition or labeling of tracking data as a separate
project rather than inferring assignments from the current heuristic.
