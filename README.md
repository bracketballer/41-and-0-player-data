# Bracketballer player data

This repository owns the Bracketballer data pipeline: CBBD and Torvik
ingestion, player-season preparation, roster and lineup data, shooting and
defensive models, and audited dataset publication. The Fastify repository owns
the PostgreSQL schema and canonical Flyway migrations.

## Setup

Requirements are Python 3.12+, Git, and PostgreSQL access for database jobs.
Docker is required when reproducing the disposable database checks in the
Fastify repository. Restoring or creating a development snapshot additionally
requires PostgreSQL 16 client tools (`psql`, `pg_dump`, `pg_restore`) on
`PATH`; see the Windows note in
[docs/DEVELOPMENT_SNAPSHOTS.md](docs/DEVELOPMENT_SNAPSHOTS.md#postgresql-client-tools)
if they are not already installed.

```bash
./scripts/setup-local.sh
# If prompted, edit .env, start the local database, run the Fastify baseline
# migrations through V34, and rerun the same setup command. The managed Git
# hook then upgrades schema-dependent releases from the pinned Fastify commit.
```

The setup script creates `.venv`, installs the pinned dependencies and this
package in editable mode, creates `.env` from `.env.example` when needed, and
adds any missing DigitalOcean Spaces settings to an existing `.env` without
changing its database values. It always enforces private mode (`0600`) for
`.env`; it never generates or retrieves secrets. Blank Spaces credentials are
reported as a warning because Spaces is optional for local-only work. When a
local database has been freshly migrated through V34 and read-only Spaces
credentials are configured, setup downloads and verifies the newest complete
development snapshot, restores it without replacing existing data, and then
applies any newer checked-in data-release descriptors. Schema-dependent
releases use the same local-only synchronization path as the Git hooks.
Rerunning setup is safe; remote and already initialized databases are never
restored automatically.

## Layout

| Path | Purpose |
| --- | --- |
| `src/bracketballer_data` | Reusable database, matching, release, and model code |
| `scripts/ingest` | External API ingestion and reconciliation jobs |
| `scripts/compute` | Shooting and defensive computation jobs |
| `scripts/publish` | Core-data preparation and audited publication |
| `scripts/dev_sync` | Schema-aware local post-Git database synchronization |
| `data` | Ignored raw inputs, caches, exports, and review reports |
| `sql/bootstrap` | Destructive local-only data bootstrap |
| `sql/review` | Manual validation and write-back SQL |
| `tests` | Unit tests for pure pipeline components |
| `docs` | Data release and signal documentation |

The large local artifacts under `data/` are intentionally ignored by Git.
They can be recreated by the ingestion jobs and should be treated as local
pipeline state, not source code.

## Common commands

Run commands from the repository root with the virtual-environment interpreter:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m scripts.publish.process_players
.venv/bin/python -m scripts.ingest.ingest_cbbd_shots_bulk --first 2020 --last 2026
.venv/bin/python -m scripts.compute.compute_shooting_ability_profiles --first 2020 --last 2026 --version shooting-v2
```

Read [docs/DATA_RELEASES.md](docs/DATA_RELEASES.md) before publishing data.
For onboarding a developer with the current sports database, read
[docs/DEVELOPMENT_SNAPSHOTS.md](docs/DEVELOPMENT_SNAPSHOTS.md).
The `--apply` commands write to PostgreSQL; rehearse them against a disposable
or staging database and never use the destructive bootstrap against production.

Schema changes belong in [the Fastify repository](../fastify), under
`database/migrations`, and must remain synchronized with its Prisma schema and
generated entities.

## Git workflow

Use the shared `feature/*` or `bugfix/*` → `develop` →
`release/YYYY.M.DD` → `master` workflow documented in
[DEVELOPMENT.md](DEVELOPMENT.md). Ordinary commits use the
`#<issue-id> <issue title>` subject format.
