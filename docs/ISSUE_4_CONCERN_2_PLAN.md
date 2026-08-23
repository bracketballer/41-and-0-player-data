# Issue #4 Concern #2: 2025–26 Full-Roster Backfill

## Summary

Backfill all populated 2025–26 D1 rosters—5,643 players in the current cached CBBD response—into player identities, roster memberships, and normalized roster-position mappings. Newly created roster players will be explicitly ineligible for the fantasy game, while existing fantasy-player eligibility is preserved.

## Implementation Changes

### Fantasy eligibility

- Add `players.is_fantasy_eligible BOOLEAN NOT NULL DEFAULT FALSE` in the next Fastify Flyway migration.
- Backfill the flag to `TRUE` for the existing curated fantasy population identified by the current ranked-eligibility predicate: `source_active`, positive PORPAG/DPORPAG, a headshot, and more than 18 games.
- Keep the current PORPAG, DPORPAG, headshot, games-played, and `source_active` checks alongside the flag in Fastify. The flag becomes the explicit catalog gate; the existing checks remain data-readiness requirements.
- Update the core player publisher to set the flag to `TRUE` for players present in the curated core dataset and `FALSE` when a player is retired from that dataset.
- Ensure roster ingestion never writes the flag: new player rows receive the database default of `FALSE`, and conflict updates preserve the existing value.
- Synchronize Fastify’s Prisma schema, run `npm --prefix ../fastify run generate:entities`, and commit all generated entity changes.
- Do not add the flag to HTTP responses; this is an internal selection contract.

### Full-roster publication

- Extend the ranked-roster candidate format with a separate `all_rosters` collection while retaining the existing eligible-team `rosters` collection for AP Top 25/Virginia Tech lineup processing.
- Populate `all_rosters` from the non-empty 2025–26 CBBD roster responses; reject duplicate teams, conflicting athlete identities, undersized rosters, and season mismatches. Reconcile roster school IDs/names through the existing audited reference-school path.
- Bump the candidate format and keep existing format-v3 releases readable and applicable.
- Publish all-roster data transactionally:
  - Upsert minimal `players` identity fields without overwriting best-season statistics or fantasy eligibility.
  - Replace active 2025–26 memberships for every included team and upsert `team_roster_memberships`.
  - Rebuild `team_roster_position_maps` for touched memberships using the existing position normalizer.
  - Leave unknown positions unmapped while retaining the membership.
  - Keep `player_seasons`, lineup evidence, and defensive computations scoped to the existing ranked-eligible cohort.
- Produce a new immutable 2025–26 ranked-roster release and descriptor using the existing audited delta/publication mechanism.
- Make the release idempotent and ordering-safe: applying it before or after the eligibility migration must yield the same eligibility state.

### Coverage verification and documentation

- Measure the pre-publication 2025–26 baseline and classify unresolved `onFloor` IDs by roster match, missing roster record, malformed payload, and identity conflict.
- Apply the release to a disposable/staging database and rerun lineup attribution for season 2026 only.
- Require defensive coverage of at least 90% to resolve concern #2; report offensive coverage as supporting context.
- If coverage remains below 90%, keep the documented team-level fallback active and publish the unresolved-ID report rather than treating the concern as closed.
- Update the lineup-attribution documentation and issue #4 with the new 2025–26 counts, release version, coverage result, and remaining anomaly categories.

## Test Plan

- Migration tests cover the false default, promotion of existing curated rows, and exclusion of sparse roster-only rows.
- Fastify tests cover flag-false exclusion, existing flag-true behavior, incomplete data guards, and consistent use across rankings, pools, school listings, and verdict scoring.
- Pipeline tests cover candidate formats v3/v4, deterministic full-roster publication, idempotence, conflict preservation, transfers, duplicate IDs, unknown positions, empty responses, and school conflicts.
- Run the player-data unit suite and compile/import checks, Fastify tests/typecheck/lint, migration smoke tests, and two identical lineup-attribution runs.

## Assumptions

- Only newly created roster identities default to ineligible; existing players retain their eligibility.
- Eligibility is an explicit stored flag, not a generated expression.
- The backfill covers all populated 2025–26 D1 rosters, not only ranked teams or athletes already seen in `onFloor`.
- Concern #1’s disagreement-rate validation and pre-2025–26 roster backfills remain out of scope.

## Implementation Result

- Published and uploaded audited release `ranked-rosters-2026-08-23.2` (candidate
  format v4). It contains 5,645 full-roster memberships: 5,643 from the cached
  CBBD rosters plus 2 memberships inferred from lineup evidence.
- Applied Fastify V35 (`is_fantasy_eligible`) and the release to the development
  database. All 5,645 2026 memberships and 10,863 active position-map rows are
  present; roster-only sparse identities remain ineligible.
- Re-measured 2026 attribution: defensive coverage is 93.55% (491,065 of
  524,922 events), offensive coverage is 97.29%, and the 53,881 comparable
  events have a 0.00% disagreement rate.
- The V29-to-V35 upgrade fixture passed in disposable PostgreSQL 16, and the
  player-data and Fastify test/typecheck/lint suites passed.
