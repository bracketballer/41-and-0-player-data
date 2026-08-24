# Production Data Release Runbook

Large sports data is versioned and audited independently from Flyway. Every
release records its pipeline commit, immutable version, source URI/checksum,
season/model range, staged and published row counts, validations, status, and
timestamps in `data_import_runs`.

`sql/bootstrap/load_core_data.sql` is local bootstrap only. Its
`TRUNCATE ... CASCADE` makes it permanently forbidden against staging or
production.

## Core schools, players, and positions

Store the three source CSVs under an immutable object-storage version and
record their object checksums. First run the publisher without `--apply`:

```bash
export DATABASE_URL=<staging-ingestion-url>
export PIPELINE_COMMIT=<full-git-commit>

python -m scripts.publish.publish_core_dataset \
  --schools data/processed/core/schools.csv \
  --players data/processed/core/processed_players.csv \
  --positions data/processed/core/player_positions.csv \
  --release-version core-2026-07-23.1 \
  --first-season 2005 \
  --last-season 2026
```

Review duplicate, foreign-key, enum, required-value, season, and 50–150% row
count bounds. Re-run with `--apply` only after review. Publication atomically
upserts source columns, refreshes position mappings for included players, and
marks missing schools/players inactive. It never deletes referenced players.

Rehearse at full volume in staging. Production requires a recent recoverable
backup, the exact same inputs/commit, and manual approval.

## CBBD shot events

```bash
export DATABASE_URL=<staging-ingestion-url>
export CBBD_API_KEY=<secret>
export PIPELINE_COMMIT=<full-git-commit>

python -m scripts.ingest.ingest_cbbd_shots_bulk \
  --first 2020 \
  --last 2026 \
  --release-version cbbd-shots-2026-07-23.1 \
  --refresh
```

Each season gets a separate audit row. The job stages all dates, creates a
deterministic checksum, verifies IDs/season/player references and row-count
bounds, then replaces that season and finishes the audit row in one publish
transaction. A fetch, validation, or publication failure leaves the prior
season visible and records a failed run.

## Shooting model

Use a new immutable model version; never overwrite an activated version.

```bash
python -m scripts.compute.compute_shooting_ability_profiles \
  --first 2020 --last 2026 --version shooting-v2

python -m scripts.compute.compute_contextual_shooting_signals \
  --first 2020 --last 2026 --version shooting-v2
```

The first command stores an inactive candidate and opens its audit record. The
second generates game/contextual profiles, requires exactly four zones and one
contextual profile per ability profile, activates the version, deactivates the
old version, and marks the audit published in the same transaction. Failure
cannot expose a partial candidate.

## Validation and rollback

Capture source and target manifests with:

```bash
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -f ../fastify/database/operations/database_manifest.sql > manifest.txt
```

Compare manifests, API responses, query latency, active model, and season
aggregates. To roll back normal bad data, republish the prior immutable artifact
as a new audited release. Do not restore the whole database unless data is
catastrophically corrupted or lost.

Exceptional corrections require a reviewed Git-tracked idempotent script,
dry-run output, an exact expected row count, one transaction, staging rehearsal,
a fresh backup, and an audit row. Interactive ad-hoc production SQL is
prohibited.

## AP Top 25 + Virginia Tech rosters and defensive characteristics

Ensure the Fastify environment has the required migrations applied and keep the
CBBD SDK pinned to this repository's requirements. First download the API
responses into a resumable, release-specific local bundle:

```bash
export CBBD_API_KEY=<secret>

python -m scripts.ingest.ingest_ranked_rosters \
  --season 2026 \
  --release-version ranked-rosters-2026-07-26.1 \
  --download
```

Every response is checkpointed below
`data/raw/ranked_rosters/<season>/<release-version>/`. If the download is
interrupted, run the same command again and it resumes from the remaining API
calls. `manifest.json` is written last and marks a complete, checksummed source
bundle. Lineup endpoints are preserved as raw JSON so nullable source fields do
not get rejected by the SDK's response models. Use a new release version when
fresh source data is required.

Once the download completes, validation and publication are fully offline and
do not require `CBBD_API_KEY`. Review the dry-run first, then publish the exact
same bundle with `--apply`:

```bash
python -m scripts.ingest.ingest_ranked_rosters \
  --season 2026 \
  --release-version ranked-rosters-2026-07-26.1

python -m scripts.ingest.ingest_ranked_rosters \
  --season 2026 \
  --release-version ranked-rosters-2026-07-26.1 \
  --apply
```

The download builds the permanent union of all AP ranks 1–25 returned for the
season plus Virginia Tech (CBBD team 340), fetches complete rosters, retains
each selected athlete's CBBD history back to 2005, and backfills every
finalized eligible-team game from season start. For current-season bundles,
every populated D1 roster is retained separately as identity, membership, and
position evidence so lineup resolution can include non-ranked opponents;
roster-only identities remain outside the fantasy catalog. Offline validation
fails before publication for missing/undersized rosters, absent eligible-team
lineup results, non-five-player units, unresolved roster athletes, or
unreconciled seconds/points. Torvik matching is written to `player_seasons`;
unmatched records remain explicit and do not receive fabricated priors.

Historical player-season schools outside the current Division I core dataset
are inserted as audited reference rows in the same publication transaction.
Eligible AP/roster teams must already exist in the canonical schools dataset.

Raw lineup responses remain in the source bundle. Only team/game lineup sets
that reconcile to plausible game time and the official score enter the
candidate; partial, unrelated-team, and internally inconsistent CBBD responses
are recorded as unavailable evidence in the manifest and validation report.

During an active season, schedule this publisher daily with a unique immutable
release version (for example, a UTC run timestamp). This is deliberately more
frequent than the required weekly AP/roster refresh and ensures newly ranked
teams are backfilled on the next run. Complete seasons need only be rerun for a
source correction.

### Developer delta distribution

The ranked-roster candidate is distributed as a verified delta, never as a
second PostgreSQL snapshot. After the ingestion commit is reviewed, create the
small archive and record the exact successful Flyway V34 checksum from the
publishing database:

```bash
python -m scripts.publish.publish_ranked_roster_delta create \
  --candidate-dir data/raw/ranked_rosters/2026/ranked-rosters-2026-08-23.1 \
  --season 2026 --release-version ranked-rosters-2026-08-23.1 \
  --flyway-v34-checksum 34=-1973679461
python -m scripts.publish.publish_ranked_roster_delta upload \
  --archive data/exports/data-releases/ranked-rosters-2026-08-23.1.tar.gz \
  --manifest data/exports/data-releases/ranked-rosters-2026-08-23.1.json
```

The archive contains only `candidate.json` and its source manifest. The
archive checksum and publication manifest are uploaded first/last in that
order, and existing matching objects are safely skipped on resume. Existing
mismatched objects are never overwritten. Commit the resulting exact object
keys and checksums under `releases/`; local Git hooks apply only descriptors
present in the checked-out branch. The manual fallback is:

```bash
python -m scripts.apply_pending_data_releases
```

Descriptors that depend on schema changes also contain a
`schema_dependency` with the Fastify repository, a stable ref, the exact
Fastify commit, and the required Flyway checksums. Ticketed descriptors live
under `releases/tickets/`, name a handler under
`scripts/dev_sync/tickets/issue_<ticket>_<slug>.py`, and carry an explicit
release sequence. The post-merge/post-rewrite and post-checkout hooks fetch
each pinned commit into a temporary detached worktree, run the Fastify-owned
Flyway image, verify the resulting schema history, and only then download and
apply the data release. Ticketed releases are ordered by Flyway version and
release sequence; ticket numbers are traceability metadata. The active
`../fastify` checkout is never switched, stashed, cleaned, or pulled. Set
`FASTIFY_REPO_PATH` when the sibling repository is elsewhere.

Hooks require a configured `.venv`, a local/loopback database, and Docker when
a schema dependency is present. Already-published matching releases are
skipped before network access. Pending ticketed releases require read-only
Spaces credentials; legacy releases retain their historical safe skip when
Spaces is not configured. Remote targets are always skipped by hooks and
require the explicit manual `--allow-remote` data-release command. Corrupt artifacts,
missing or mismatched Fastify commits, Flyway checksum mismatches, missing
eligible canonical schools, and failed audited applications stop the hook
visibly. A lock avoids concurrent applications, while an already-published
matching audit row is a no-op.

Compute the initial model in shadow mode without activating it:

```bash
python -m scripts.compute.compute_defensive_characteristics \
  --first 2024 --last 2026 \
  --model-version defense-v1-shadow \
  --release-version defense-v1-shadow-2026-07-26.1 \
  --apply
```

The shadow version includes provisional `LIKELY_DROP_ANCHOR` scores marked
`SCHEME_LABEL_NOT_VALIDATED`. Manually review clear drop, switch, and zone
examples and calculate precision on the reviewed audit set. Activation is
blocked unless the reported precision is at least 0.80:

```bash
python -m scripts.compute.compute_defensive_characteristics \
  --first 2024 --last 2026 \
  --model-version defense-v1-shadow \
  --release-version defense-v1-activation-2026-07-27.1 \
  --scheme-validation-precision 0.84 \
  --activate --apply
```

If the audit misses the threshold, publish a new model version with
`--disable-scheme-label --activate`; that exposes the measurable traits and
`DROP_COMPATIBLE_BIG` without claiming inferred drop coverage. Active model
versions are immutable.
