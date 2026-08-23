# Development database snapshots

Development snapshots contain the complete non-sensitive sports-data state
needed by the application, including players, schools, rosters, college games,
lineups, opponent contexts, defensive data, shot events, and derived models.
They intentionally contain no users, application games, queues, draft picks,
comments, votes, user labels, saved labels, or published lineups. Synthetic
application records can be created separately for workflow testing.

The snapshot format is PostgreSQL 16 custom-format, data-only, compressed with
Zstandard. The target schema must be created by the Fastify repository's
Flyway migrations through V34 before the data is restored.

## PostgreSQL client tools

Creating and restoring snapshots shells out to `pg_dump`/`pg_restore`
directly (not through psycopg2), and both must resolve on `PATH` as
PostgreSQL **16** — `postgres_client_major()` rejects any other major
version. macOS and Linux typically get these from the OS's
`postgresql-client`/`postgresql@16` package independently of Docker.

Windows has no equivalent: there is no official standalone PostgreSQL 16
client-tools installer, and the full server installer bundles a mismatched
major version (17/18) alongside a server you likely don't want running
locally. If your local PostgreSQL only exists as the `postgres:16` Docker
container from the Fastify repository's `docker-compose.yml`, that
container already has a correct `pg_dump`/`pg_restore` — you just need
something on `PATH` named `pg_dump`/`pg_restore` that forwards to it (e.g.
`docker exec <container> pg_dump/pg_restore ...`, copying any local
archive file into the container first with `docker cp` since these tools
read from the local filesystem of wherever they run).

One added wrinkle if you build such a shim: on Windows, Python's
`subprocess` module resolves a bare command name (e.g. `"pg_restore"`,
as `development_snapshot.py` calls it) by asking `CreateProcess` to find
it, and `CreateProcess` only auto-appends `.exe` — unlike `cmd.exe` /
`shutil.which`, it does **not** consult `PATHEXT` to find a `.bat` or
`.cmd` script. A shim placed on `PATH` must therefore be a real `.exe`
(a `.bat`/`.cmd` file resolves fine when a human runs `pg_restore` in a
shell, but silently fails with `WinError 2` when this repo's Python
scripts invoke it). A tiny native launcher compiled with `csc.exe`
(bundled with the .NET Framework on any Windows install, no extra
tooling required) that shells out to the Docker-based script above is
enough.

## Spaces setup

Create a private DigitalOcean Space in `nyc3` named
`bracketballer-development-snapshots`. Keep public file listing and the CDN
disabled. Configure lifecycle expiration for the
`development-snapshots/` prefix after 90 days and incomplete multipart uploads
after one day.

Create a bucket-scoped Spaces access key with read/write/delete permission for
the publisher. Keep the key in the operator's password manager or an approved
secret store; do not commit it or place it in a shared shell history.

DigitalOcean Spaces configuration is optional for local creation and restore.
Set these values in the ignored `.env` only when publishing or sharing:

```text
DO_SPACES_REGION=nyc3
DO_SPACES_BUCKET=bracketballer-development-snapshots
DO_SPACES_ACCESS_KEY_ID=<publisher-key>
DO_SPACES_SECRET_ACCESS_KEY=<publisher-secret>
```

`./scripts/setup-local.sh` creates `.env` when absent and appends only missing
Spaces settings when it already exists. Existing database and API settings are
preserved; real Spaces credentials must be entered through an approved secret
channel. The optional `DO_SPACES_ENDPOINT` may be omitted (the region endpoint
is inferred), and a bucket hostname is normalized safely if it is supplied.

## Create and publish

Use a unique release version. Creation reads the source database in one
repeatable-read snapshot, verifies PostgreSQL 16 and Flyway V34, writes the
archive, checksum, and JSON manifest locally, and refuses to overwrite an
existing archive.

```bash
.venv/bin/python -m scripts.publish.publish_development_snapshot \
  create --release-version 2026-08-23.1

.venv/bin/python -m scripts.publish.publish_development_snapshot \
  upload \
  --archive data/exports/development/bracketballer-development-data-v34-2026-08-23.1.dump \
  --manifest data/exports/development/bracketballer-development-data-v34-2026-08-23.1.dump.json
```

The upload validates the archive, manifest, and checksum sidecar before making
any network request, then publishes the archive and checksum first and the
manifest last. Object keys are immutable and are never overwritten: an
interrupted retry skips objects with matching SHA-256/release metadata and
rejects mismatches (including legacy objects that lack the metadata). Use a new
release version when republishing an object created by an older uploader. The
manifest records the source Flyway checksums, source commit, exact row counts,
archive size, SHA-256, and object keys.

`scripts/setup-local.sh` uses the developer restore command when it finds a
pristine, local database migrated through V34. The restore discovers the newest
complete release directly from Spaces. Give the developer a read-only,
bucket-scoped Spaces key and add
the following settings to the developer's ignored `.env`:

```text
DO_SPACES_REGION=nyc3
DO_SPACES_BUCKET=bracketballer-development-snapshots
DO_SPACES_ACCESS_KEY_ID=<developer-read-key>
DO_SPACES_SECRET_ACCESS_KEY=<developer-read-secret>
```

The setup and restore scripts do not require a manifest path, object key,
release version, or download URL from the developer. They list published
manifests, download the newest complete snapshot, verify it, restore it, and
remove its temporary files. After configuring `.env`, starting PostgreSQL, and
running the Fastify migrations through V34, rerun setup:

```bash
./scripts/setup-local.sh
```

The standalone restore remains available:

```bash
.venv/bin/python scripts/restore_development_snapshot.py
```

## Restore locally

Create a fresh PostgreSQL 16 database, apply the Fastify Flyway migrations
through V34, and set its connection string in `DATABASE_URL` (or configure the
existing `PSQL_*` settings). The command above uses that target automatically.
It refuses a nonempty or schema-mismatched target.

For an operator who already has local snapshot artifacts, the explicit form is
still available:

```bash
.venv/bin/python scripts/restore_development_snapshot.py \
  --archive snapshot.dump \
  --manifest snapshot.dump.json \
  --target-database-url '<empty-target-database-url>'
```

The restore command refuses a target containing anything beyond known Flyway
seed data, requires the target Flyway history and checksums to match the
manifest, restores in one transaction, runs `ANALYZE`, and compares every
allowlisted table count. It also verifies that all sensitive application tables
remain empty and that the seven system lineup labels remain unchanged.

Target contents are checked across every table in `public` (except
`flyway_schema_history`), not just the snapshot allowlist. Flyway migration V28
creates seven system rows in `lineup_labels`; those exact seed rows are the only
permitted pre-existing data. They are neither removed nor included in the
archive. Any sports data, user-created label, or other application row makes an
initial restore fail safely.

Docker and native PostgreSQL servers use the same archive and restore command;
only the target database URL and provisioning steps differ.

## Snapshot contents

The explicit allowlist is maintained in
`src/bracketballer_data/development_snapshot.py`. Adding a new sports-data
table requires a deliberate code and test change. A snapshot archive is kept
under ignored `data/` storage and must never be committed to Git or Git LFS.

Publish a new full snapshot after a material sports-data release so new
developers do not need to replay old deltas. Use a new immutable release
version; setup automatically chooses the newest complete manifest. The checked-
in data-release runner still runs afterward. It skips releases already recorded
as published and otherwise converges the database, including recording the
audit row when a full snapshot already contains that release's sports rows.
