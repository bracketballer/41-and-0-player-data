# Offensive Lineup Projection (v1) — Ticket Breakdown & Implementation Handoff

> **How to use this document.** This is an executable handoff for an implementing agent.
> Sections 1–3 are context you should read before touching anything. Section 4 is the ticket set
> to file in GitHub Issues. Section 5 is the exact filing procedure. Section 6 is verification.
>
> **Do not implement the feature itself from this document.** The tickets in Sprint 0 are gating
> research; Ticket 5 returns an explicit GO/NO-GO that decides whether Sprints 1–3 happen at all.
> Your first task is to file the tickets, not to write model code.

---

## 1. Context

`bracketballer/bracketballer-frontend#3` proposes a coach-facing signal: given the opponent's
defensive five on the floor, project which of our offensive lineups produces the most points per
shot, ranked by marginal delta. `bracketballer/bracketballer-frontend#4` is a requirements
checklist that must be answered before implementation, with §1 (data availability) and §2 (sample
size) marked gating.

Neither issue is executable as written. `#3` is a design memo; `#4` is a question list with most
answers blank. This document converts both into a dependency-ordered set of GitHub Issues sized
for agile iteration, filed in the repo that owns each piece of code, tracked from `#3` as the
epic.

### Decisions already taken

| Decision | Value |
|---|---|
| Filing | Draft here, then file with `gh` on approval |
| Repo layout | Per-repo filing, `bracketballer-frontend#3` as the cross-repo epic |
| "Our rotation" | A **real team's actual rotation**. Virginia Tech (CBBD team 340) is the pinned anchor. Not fantasy lineups. |
| Shot-corpus coverage gap | Gets its own gating ticket (T3), rather than being papered over with baseline profiles |

### The model, restated from `#3`

```
share_ij    ∝  exp( log(player_i base share in zone j) + λ · defense_tilt_j )
proj_PPS_i  =  Σ_j  share_ij × player_i PPS in zone j
```

Two non-negotiables from `#3`, both encoded as tests in the ticket set below:

1. **Usage renormalization is required in v1.** Without it the optimizer returns the five
   highest-usage scorers for every opponent — the same answer regardless of which defense is on
   the floor, defeating the premise.
2. **Do not take the max over matchup assignments.** The defense picks the matchups; maximizing
   over them inflates every projection.

---

## 2. What already exists — reuse, do not rebuild

Exploration of the three repos turned up substantially more foundation than `#4` assumes. Repo
paths below are relative to each repo root; **PD** = `41-and-0-player-data` (this repo),
**FS** = `fastify`, **FE** = `bracketballer-frontend`.

| Asset | Location | Relevance |
|---|---|---|
| `team_game_lineups` + `team_game_lineup_players` | FS `database/migrations/V32__add_ranked_rosters_and_characteristics.sql:217-248` | Real 5-man units per game with `total_seconds`, `team_stats`, `opponent_stats`, ratings. Ingested from CBBD `LineupsApi.get_lineup_stats_by_game` (PD `scripts/ingest/ingest_ranked_rosters.py:115-194`, insert at `:810+`). **Largely answers §1 "reconstruct lineup state" and §7 "their realistic defensive lineups" without ingesting substitutions at all.** |
| Full CBBD play object retained | PD `src/bracketballer_data/shooting_data.py:110` — `"raw_payload": dict(raw)` | The `onFloor` data §1 audited (87.07% valid in 2024, 97.69% 2025, 96.01% 2026) is **already in the database**. This is an extraction job, not a re-ingestion. |
| 4 categorical zones | PD `src/bracketballer_data/shooting_ability.py:18-20` — `rim` / `jumper` / `three_pointer` / `free_throw`, derived from CBBD `shot.range` | Existing zones are **not coordinate-derived**. The proposed 5-zone scheme (corner 3 vs above-break 3) is new work. `location_x` / `location_y` are stored but unvalidated. |
| Versioned-model pattern | FS `shooting_model_versions` (V26), `defensive_model_versions` (V32) | Copy exactly for the new model version, including the one-active-row partial unique index idiom. |
| Bayesian zone shrinkage | PD `src/bracketballer_data/shooting_ability.py:189-215` — `adjusted = (makes + 50·seasonAvg) / (attempts + 50)` | Reuse for §4 shrinkage rather than inventing a new estimator. |
| Defensive characteristics | FS `player_characteristic_scores` (V32); PD `src/bracketballer_data/defensive_characteristics.py` | `RIM_PROTECTOR`, `DEFENSIVE_DISRUPTOR`, `DROP_COMPATIBLE_BIG` plus `height` and `center_role_share` in `PlayerDefensiveEvidence` directly serve §6's "best perimeter defender, size-ordered". |
| Lineup optimize surface | FS `src/routes/shooting/optimize.ts`; FE `app/simulator/hooks/data/useReadLineupOptimization.ts` | Established route + hook + TypeBox pattern to mirror for the new endpoint. |
| Import-run auditing | PD `src/bracketballer_data/import_audit.py` — `begin_import_run` / `mark_validated` / `mark_failed` | Every new compute job must use it, matching `scripts/compute/compute_defensive_characteristics.py`. |
| Virginia Tech anchor | PD `src/bracketballer_data/ranked_rosters.py:55-78`; CBBD team 340 | Permanently in `team_season_eligibility` alongside AP Top 25. This is "our team" for v1. |

### The blocker `#4` does not name

PD `scripts/publish/process_players.py:21-22` sets `MIN_GAMES = 10` and `MIN_PPG = 8.0`, applied
at `:117`, keeping one best season per athlete. PD
`scripts/ingest/ingest_cbbd_shots_bulk.py:143-146` (`eligible_players`) inherits that filter
verbatim by selecting straight from `players`.

A real 10-man rotation will therefore have shot events for roughly half its players — and the
missing half is systematically the bench, which is exactly who a substitution decision turns on.
This gates the whole feature and is **Ticket 3**.

### Repo ownership rules that constrain implementation

From PD `AGENTS.md`:

- PostgreSQL schema changes belong in **FS** `database/migrations` as Flyway migrations.
- `FS prisma/schema.prisma` must stay synchronized; run `npm --prefix ../fastify run generate:entities`.
- Every resulting change under `FS src/lib/entities` ships in the same work. **Never hand-edit
  that directory** — it is generated by `FS scripts/generate-entities.ts`.
- PD owns ingestion, model computation, and audited dataset publication. Do not add a second
  migration set to PD.

From FE `PRODUCT.md`:

- Unavailable and provisional evidence are normal product states and **must never be represented
  as zero**.
- Defensive characteristics are informational and must not change optimizer weights without a
  contract change.
- The frontend contract is generated from the backend OpenAPI document.

---

## 3. Sprint structure

16 issues across 4 sprints, with two hard gates.

```
T1 ─┐
T2 ─┼─> T4 ─> T5 (GO/NO-GO) ─> T6 ─> T7 ─> T9 ─┐
T3 ─┘                           │              ├─> T11 ─> T12 (MERGE GATE) ─> T13 ─> T14 ─> T15 ─> T16
                                └─> T8 ─> T10 ─┘
```

- **Sprint 0** is three parallel tracks (T1, T2, T3) converging on T4 → T5.
- **Gate 1** is T5: feature viability. Sprints 1–3 do not begin until it returns GO.
- **Gate 2** is T12: the backtest. If the matchup-adjusted projection does not beat the naive
  baseline by a pre-registered margin, Sprint 3 does not ship.

---

## 4. Ticket set

Each ticket below is ready to become an issue body. Keep the section headings — they are what
makes the tickets reviewable against `#4`.

---

### Sprint 0 — Gating research

Answers `#4` §1–§3. **No production code ships in this sprint.**

---

#### T1 · PD · Extract and validate on-floor lineup state from stored shot payloads

**Why.** `#4` §1 asks whether we can reconstruct who was on the floor at every event and what the
error rate is. The audit referenced in `#4` suggests the answer is already sitting in our
database: `player_shot_events.raw_payload` stores the complete CBBD play object
(`src/bracketballer_data/shooting_data.py:110`), including `onFloor`. No CBBD refetch is needed.

**Scope.**
- Read `onFloor` out of `player_shot_events.raw_payload` across 2024–2026.
- Resolve on-floor athlete IDs to our `players` / `player_seasons` rows.
- Cross-validate against `team_game_lineups` / `team_game_lineup_players` for the same game and
  time window.

**Deliverables.**
- Per-season coverage table: share of shot events with a valid, resolvable, exactly-five on-floor
  set, for both teams.
- Disagreement rate against `team_game_lineups`.
- Written recommendation on whether shots can be reliably attributed to a specific defensive five.

**Acceptance criteria.**
- Numbers written into `#4` §1 bullets 2–3.
- A documented fallback path (team-level defensive profiles only) if valid attribution falls below
  the agreed threshold. **Proposed threshold: 90% of 2025–26 shot events.**

**Dependencies.** None. Start here.

**Answers `#4`:** §1 bullets 2–3, and the §1 decision point.

---

#### T2 · PD · Coordinate frame normalization and v1 zone scheme decision

**Why.** The existing four zones are categorical buckets from CBBD's `shot.range` field
(`src/bracketballer_data/shooting_ability.py:18-20`), not coordinate-derived. The 5-zone scheme
`#3` proposes — rim / short mid / long mid / corner 3 / above-break 3 — cannot be built from them.
`#4` §1 also flags that coordinate units are undocumented and attacking direction is not
identified.

**Scope.**
- Validate the observed `location_x` 0–940 / `location_y` 0–500 range as a 94×50 ft court scaled
  by 10.
- Infer attacking direction per possession and reflect all shots into a common frame.
- Quantify per-season coordinate coverage **by conference** — `#4` §1 notes this is unaudited.
- Decide zone count and polygon definitions.

**Recommended answers for §3** (confirm or revise, don't leave blank):
- Five zones as proposed in `#3`.
- Left/right **symmetric** — splitting doubles cell count against an already-thin sample.
- Hard polygons for v1; defer spatial smoothing and latent shot bases.
- Free throws excluded from the share model but retained for the usage model.
- The 2019–20 three-point line change is moot: restrict v1 to 2024+, which
  `team_season_eligibility` already enforces (`season >= 2024`).
- And-ones / shooting fouls: attribute the field-goal attempt to its zone; free throws stay
  separate.

**Deliverables.**
- `docs/SHOT_ZONES.md` with polygon definitions and the coverage audit.
- `src/bracketballer_data/shot_zones.py` — a pure, unit-tested classifier function.

**Acceptance criteria.**
- Classifier round-trips a hand-checked sample of known shots to expected zones.
- `#4` §3 filled in completely.

**Dependencies.** None. Runs parallel to T1.

**Answers `#4`:** §3 in full; §1 bullet 4.

---

#### T3 · PD · Expand shot-event corpus to full eligible-team rosters *(gating)*

**Why.** `players` is filtered to ≥10 games and ≥8.0 PPG, one best season per athlete
(`scripts/publish/process_players.py:21-22`, applied at `:117`), and shot ingestion inherits that
filter (`scripts/ingest/ingest_cbbd_shots_bulk.py:143-146`). A real 10-man rotation will have
profiles for roughly half its players, and the missing half is the bench — precisely the players a
substitution decision is about. Without this, the feature cannot evaluate 252 lineups.

**Scope.**
- Introduce an explicit eligibility source for shot ingestion covering all rostered players on
  team-seasons in `team_season_eligibility`, rather than widening the global `players` filter.
- Backfill shot events for those players from CBBD.

**Critical constraint.** The fantasy simulator and community surfaces depend on the current
`players` population. Widening the global filter would regress them. Keep the two populations
distinct.

**Acceptance criteria.**
- Every player with ≥100 minutes on a 2024+ eligible team-season has shot events ingested.
- Existing simulator and community behavior unchanged — FS test suite green, PD
  `python -m unittest discover -s tests` green.

**Dependencies.** None, but blocks T7 and T12.

**Answers `#4`:** §4 minimum-attempts question becomes answerable; unblocks §7.

---

#### T4 · PD · Fit λ (defense tilt) via team-level regression

**Why.** `#3` is explicit: *"Estimate λ first, before any UI work."* λ controls how much a defense
actually shifts an offense's shot distribution, and its magnitude determines whether the projected
spread across 252 lineups is meaningful or entirely noise.

**Scope.**
- Team-level regression: observed per-game zone distribution ~ offense season baseline + defense
  season concession profile.
- Suggested form: `logit(share_gj) ~ log(offense_baseline_j) + λ · defense_tilt_j`.
- Decide global λ vs per-zone λ_j for v1.

**Deliverables.**
- Fitted λ, per-zone variation, standard errors.
- A checked-in script under `scripts/compute/` making the fit reproducible.
- The fitted value hardcoded as a named constant, with the fit recorded in `docs/`.

**Acceptance criteria.**
- λ and per-zone variation written into `#4` §2.
- Fit reproducible from the checked-in script.

**Dependencies.** T2 (needs zones).

**Answers `#4`:** §2 bullet 3.

---

#### T5 · PD · Sample-size reality check and projected-spread analysis *(GO/NO-GO GATE)*

**Why.** `#4` §2 is gating: if the projected spread across our 252 lineups sits inside the noise
band, the feature has nothing to say and everything downstream is wasted.

**Scope — compute all four §2 quantities.**
- Median FGA per rotation player per season, by zone.
- Median opponent FGA faced by their top-10 defensive lineups.
- Implied spread in projected PPS across our 252 lineups.
- That spread compared against a bootstrapped noise band.

**Acceptance criteria.**
- An explicit written recommendation: **GO**, **NO-GO**, or **SHIP AS EXPLORATION TOOL**.
- The `#4` §2 decision point answered in the issue.
- **Sprints 1–3 do not begin until this ticket closes.**

**Dependencies.** T1, T2, T4.

**Answers `#4`:** §2 in full.

---

### Sprint 1 — Model foundations

---

#### T6 · FS · Flyway migrations for location-zone and matchup-projection tables

**Why.** The new model needs storage that the existing shooting tables cannot provide: zone
definitions are different, and the precomputed matrix is a new object entirely.

**Scope — new `V35`+ migrations.**
- `shot_location_model_versions` — mirror `shooting_model_versions` (V26) exactly, including the
  partial unique index enforcing one active version.
- `player_shot_location_profiles` — per player-season-zone: attempts, makes, attempt share,
  posterior alpha/beta, adjusted PPS.
- `defense_lineup_zone_concession` — per team-season-lineup: zone concession tilt, possessions,
  confidence.
- `lineup_offensive_projections` — the precomputed matrix.

**Process requirements (from `AGENTS.md`).**
- Migrations in `fastify/database/migrations`.
- Keep `fastify/prisma/schema.prisma` synchronized.
- Run `npm --prefix ../fastify run generate:entities`.
- Commit every resulting change under `fastify/src/lib/entities` in the same PR. Never hand-edit
  that directory.

**Acceptance criteria.**
- Migrations apply clean against a disposable database.
- Generated entities committed alongside.
- Storage estimate for the precomputed matrix recorded, per team per season.

**Dependencies.** T2, T5.

**Answers `#4`:** §8 schema-changes, Flyway-plan, and storage-estimate bullets.

---

#### T7 · PD · Player location-zone shot-share and PPS profiles (`shot-location-v1`)

**Why.** The model's first input: each player's share of attempts by zone and points per shot in
each zone.

**Scope.**
- New `scripts/compute/compute_shot_location_profiles.py`, following the established shape of
  `scripts/compute/compute_shooting_ability_profiles.py`: `--first` / `--last` / `--version`
  arguments, dry-run unless `--apply`, wrapped in `import_audit`.
- Reuse the shrinkage form already in `src/bracketballer_data/shooting_ability.py:189-215` rather
  than inventing a new estimator.
- Keep pure logic in `src/bracketballer_data/`, I/O in `scripts/` — matching the existing split.

**Answer `#4` §4 while implementing.** Minimum attempts for a profile; shrinkage target (team,
position, or league average); current-season-only vs prior-season blend; freshmen and transfers
with no prior data; in-season recompute cadence; recency weighting and decay rate.

**Acceptance criteria.**
- Profiles computed for every 2024+ eligible-team rotation player.
- Unit tests for the pure profile builder in `tests/`.
- `#4` §4 filled in.

**Dependencies.** T2, T3, T6.

**Answers `#4`:** §4 in full.

---

#### T8 · PD · Opponent defensive-lineup zone concession profiles

**Why.** The model's second input: how each opposing five tilts the zone distribution of the
offense it faces.

**Scope.**
- Per defensive five, compute the zone distribution conceded relative to league baseline, shrunk
  toward the team-season profile and then league.
- Use T1's attribution. If T1 recommended the fallback, compute team-level profiles instead and
  say so explicitly in the model docs.
- Identify "their ~10 realistic defensive units" from `team_game_lineups.total_seconds`.
  **Recommend a minutes-share threshold rather than a fixed top-N** — rotation depth varies by
  team and a fixed N misrepresents both ends.

**Acceptance criteria.**
- Concession profiles with possession counts and confidence for eligible team-seasons.
- Units below the confidence floor are flagged, **never silently zeroed** — FE `PRODUCT.md`
  requires that unavailable and provisional evidence are never represented as zero.

**Dependencies.** T1, T2, T6.

**Answers `#4`:** §7 "their realistic defensive lineups" bullet.

---

#### T9 · PD · Usage renormalization and efficiency-vs-usage slope

**Why.** `#3` marks this not deferrable. Without it the optimizer returns our five highest-usage
scorers every time, for every opponent — the same answer regardless of which defense is on the
floor.

**Scope.**
- Fit the efficiency-vs-usage slope on the eligible population. `player_seasons.usage` already
  exists (V32).
- Divide each player's baseline usage by the lineup sum; adjust projected efficiency by the fitted
  slope.
- Cap the renormalization so extreme lineups cannot produce absurd projections.

**Acceptance criteria.**
- **The §5 sanity test is implemented as a test, not a note.** The optimizer must return
  materially different lineups for different opponent defensive units. This test failing is a
  build failure, not a warning.
- Fitted slope and the sample it was fitted on recorded in `docs/`.

**Dependencies.** T7.

**Answers `#4`:** §5 in full.

---

### Sprint 2 — Engine and validation gate

---

#### T10 · PD · Heuristic matchup assignment

**Why.** `#3` specifies a default heuristic and explicitly forbids maximizing over assignments,
because the defense picks the matchups and maximizing inflates every projection.

**Scope.**
- Default rule: their best perimeter defender on our primary handler, size-ordered from there.
- Source "best perimeter defender" from the existing `player_characteristic_scores`
  (`DEFENSIVE_DISRUPTOR`, inverse `DROP_COMPATIBLE_BIG`) plus `height` and `center_role_share`
  already carried in `PlayerDefensiveEvidence`
  (`src/bracketballer_data/defensive_characteristics.py`).
- Size feasibility: **recommend a penalty, not a hard block** — hard blocks produce empty result
  sets on unusual but legal lineups.

**Acceptance criteria.**
- A test asserting a **single deterministic assignment** is produced — i.e. proving no max over
  assignments is being taken.
- `#4` §6 written out explicitly, including the metric and threshold defining "best perimeter
  defender", and confirming user-specified override is v2.

**Dependencies.** T7, T8.

**Answers `#4`:** §6 in full.

---

#### T11 · PD · Vectorized projection engine and pregame precompute job

**Why.** The core computation, plus the precompute that makes it usable in-game. `#3`: *"The
moment this is useful is thirty seconds after they sub, not two minutes later."*

**Scope.**
- Implement `share_ij ∝ exp(log(base_share_ij) + λ · defense_tilt_j)` and
  `proj_PPS_i = Σ_j share_ij · PPS_ij`, plus the uncertainty interval.
- ~30k evaluations (252 lineups × ~120 assignments), NumPy-vectorized — no heuristic or
  approximate search needed.
- Precompute the full 252 × ~10-defensive-unit matrix pregame into
  `lineup_offensive_projections`, so in-game use is an indexed lookup.

**Acceptance criteria.**
- Full matrix for one team-season computes in under a second.
- Results deterministic for a fixed model version.
- Job wrapped in `import_audit`, consistent with other compute jobs.

**Dependencies.** T4, T9, T10.

**Answers `#4`:** §8 precomputation-trigger, caching, and where-compute-runs bullets.

---

#### T12 · PD · Backtest against naive baseline *(MERGE GATE)*

**Why.** `#3`: *"If the matchup-adjusted projection doesn't beat that baseline, the feature has no
signal. This is roughly one afternoon of work and should gate merge."*

**Scope.**
- Baseline: ignore the opponent entirely; use player season averages.
- **Pre-register the passing margin before running the backtest.** Registering afterward is not a
  test.

**Answer `#4` §10 while implementing.** Holdout definition (which games, which seasons); exactly
what the naive baseline computes; the margin that counts as passing; whether interval calibration
is checked in v1 or deferred; who reviews the backtest before merge.

**Acceptance criteria.**
- Written backtest report in `docs/`.
- **If the projection does not beat baseline by the pre-registered margin, Sprint 3 does not
  ship.**

**Dependencies.** T3, T11.

**Answers `#4`:** §10 in full.

---

### Sprint 3 — Service and surface

---

#### T13 · FS · Offensive projection endpoint and OpenAPI contract

**Why.** Exposes the precomputed matrix to the frontend.

**Scope.**
- New route mirroring `src/routes/shooting/optimize.ts`: TypeBox schemas, `withTypeProvider`,
  `Type.Ref` responses, matching error-shape conventions.
- Suggested: `POST /lineups/offensive-projection`, taking our rotation and the opponent's
  defensive five, returning marginal-delta-ranked swaps with intervals and confidence.
- **Indexed lookup against the precomputed matrix — no model computation in the request path.**

**Acceptance criteria.**
- `openapi.json` regenerated; FE contract in `libraries/data/models/api.ts` regenerated from it.
- Response field naming uses **"offensive projection"**, never "best lineup". `#3` is explicit
  that this is cheap to get right now and painful to rename after adoption.

**Dependencies.** T6, T11, T12 passing.

**Answers `#4`:** §8 API-contract bullet.

---

#### T14 · FE · Opponent defensive-lineup selection

**Why.** The user needs to tell the tool which five are on the floor.

**Scope.**
- Extend the existing simulator surface rather than building a new one. It already uses
  `@hello-pangea/dnd` (`package.json:22`), and
  `app/simulator/hooks/data/useReadLineupOptimization.ts` is the hook pattern to mirror.
- Dropdown selection of the opponent's realistic defensive units, ordered by minutes.
- **Live auto-detection from game state is out of v1.**

**Acceptance criteria.**
- Selecting an opponent unit re-ranks within the interaction budget `#3`'s premise requires —
  useful thirty seconds after a substitution.
- `#4` §9 answered for selection mechanism and mobile/tablet support.

**Dependencies.** T13.

**Answers `#4`:** §9 bullets 1–2, 7.

---

#### T15 · FE · Marginal-delta ranking with uncertainty interval

**Why.** `#3`: *"Coaches are deciding whether to sub, not building a lineup from scratch."*

**Scope.**
- Rank by marginal delta, not absolute best. Format: "swap X for Y, +0.04 PPS against this
  defense."
- Render the interval so it reads as uncertainty, not decoration.
- Surface label is **"Offensive projection"**. The tool makes no net judgment in v1.
- Low-confidence units carry a warning consistent with the existing `LOW_CONFIDENCE` /
  `MISSING_DATA` treatment described in PD `docs/TEAM_LINEUP_SIGNALS.md`.

**Acceptance criteria.**
- **No string in the surface claims a net or defensive judgment.** Review the copy explicitly
  against this.
- Provisional and unavailable evidence render as such, never as zero (FE `PRODUCT.md`).
- `#4` §9 filled in.

**Dependencies.** T13, T14.

**Answers `#4`:** §9 bullets 3–6.

---

#### T16 · PD/FE · Signal documentation and rollout

**Why.** Every other model in this codebase has a signal document. This one needs the same, plus
`#4`'s unanswered scope and rollout sections.

**Scope.**
- New PD `docs/OFFENSIVE_PROJECTION.md`, in the pattern of `docs/SHOOTING_SIGNALS.md` and
  `docs/TEAM_LINEUP_SIGNALS.md`: model definition, fitted λ, zone scheme, confidence semantics,
  and an explicit limitations section.
- Update PD `docs/DATA_RELEASES.md` with the new compute jobs.

**Answer `#4` §11 and §12.** Confirm every deferral; name the first user; name the tools they use
today (Synergy, Hudl, ShotQuality); state what would make them distrust it permanently; describe
how feedback on whether recommendations were acted on gets collected.

**Acceptance criteria.**
- `#4` §11 and §12 filled in; `#4` closed.

**Dependencies.** T12, T15.

**Answers `#4`:** §11, §12.

---

## 5. Filing procedure

1. **Create the 16 issues** with `gh issue create --repo bracketballer/<repo>`. Each body carries
   the same sections used above: Why / Scope / Deliverables / Acceptance criteria / Dependencies /
   Answers `#4`. Reference dependencies by full `owner/repo#number` so they resolve cross-repo.

2. **Label.** Only the nine GitHub default labels exist in all three repos today, and no
   milestones exist anywhere.
   - `enhancement` for T6–T16
   - `question` for T1, T2, T4, T5 (research spikes)
   - `enhancement` + `help wanted` for T3
   - **Propose** adding `gating` and `sprint-0` labels rather than assuming them.
   - **Do not create milestones silently.** If sprints should be milestones, flag it and ask.

3. **Edit `bracketballer-frontend#3`** to add a cross-repo epic checklist linking all 16 by full
   `owner/repo#n` reference, so GitHub renders it as a tracked list.

4. **Comment on `bracketballer-frontend#4`** mapping each checklist section to the ticket that
   answers it, and recording the four findings from Section 2 as inline answers where they already
   resolve a question:
   - `team_game_lineups` already exists and is ingested → §1 lineup reconstruction, §7
   - `onFloor` is already stored in `raw_payload` → §1
   - existing zones are categorical, not coordinate-derived → §3
   - the ≥10G / ≥8.0 PPG corpus filter → §4, §7, and a gap §4 does not currently name

5. **Known tooling issue.** `gh issue view` currently fails against these repos with a
   Projects-classic GraphQL deprecation error. Use `gh api repos/<owner>/<repo>/issues/<n>` for
   reads.

---

## 6. Verification

Filing is the deliverable, so verification means confirming tracker state is correct and readable.

```bash
# All three repos show the expected new tickets
gh issue list --repo bracketballer/41-and-0-player-data  --state open
gh issue list --repo bracketballer/fastify               --state open
gh issue list --repo bracketballer/bracketballer-frontend --state open

# The epic checklist resolves all 16 cross-repo references
gh api repos/bracketballer/bracketballer-frontend/issues/3 --jq .body

# The checklist mapping comment landed
gh api repos/bracketballer/bracketballer-frontend/issues/4/comments --jq '.[].body'
```

Then spot-check two ticket bodies in the web UI to confirm code spans and dependency links render.

**No code changes are made by this document.** Implementation verification lives inside individual
tickets — most importantly **T9's optimizer-varies-by-opponent test** and **T12's backtest**, which
are the two acceptance criteria `#3` treats as non-negotiable.
