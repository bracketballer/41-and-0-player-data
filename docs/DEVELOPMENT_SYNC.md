# Local development synchronization

The player-data repository installs managed Git hooks through
`scripts/setup-local.sh`. Git has no dedicated `post-pull` hook, so the sync
runs after `post-merge` (merge-based pulls), `post-rewrite` (rebased pulls),
and `post-checkout` (branch switches).

Each legacy release descriptor may declare a `schema_dependency`. Ticketed
descriptors under `releases/tickets/` additionally name a ticket handler,
required Flyway version, and explicit release sequence. The sync fetches each
declared Fastify ref, verifies that it contains the exact commit, creates a
temporary detached worktree, builds Fastify's Flyway image from that worktree,
and migrates the configured local database. Only after the required Flyway
checksums are present does it download, verify, and apply data. Ticketed
releases execute by Flyway version and release sequence; the ticket number is
for ownership and traceability.

The active sibling checkout is never switched, stashed, cleaned, or pulled.
Set `FASTIFY_REPO_PATH` when Fastify is not at `../fastify`. The automatic path
accepts only loopback/local PostgreSQL hosts; shared or remote databases must
be handled manually with the explicit data-release command.

Manual invocation from the repository root:

```bash
.venv/bin/python -m scripts.dev_sync.sync_after_git_update

# Ticketed releases only; add --allow-remote only for an explicitly reviewed target.
.venv/bin/python -m scripts.dev_sync.ticket_runner
```

Missing local database configuration produces a safe skip. Already-published
matching releases are also skipped before network access. A configured local
database with a pending ticketed release requires read-only Spaces credentials;
missing credentials, migration, checksum, artifact, handler, or
audited-publication failures are reported as errors and never silently
ignored. Legacy releases retain their historical safe skip when Spaces is not
configured.

## Ticketed release authoring

Create a handler named `issue_<zero-padded-ticket-number>_<slug>.py` under
`scripts/dev_sync/tickets/`. It must validate the extracted artifact without
writing, then apply its data in the transaction supplied by the runner and
return row counts plus validation details. Use a new release sequence for each
immutable publication; do not reuse a published import version.

The generic publisher creates the archive, checksum, publication manifest, and
descriptor:

```bash
python -m scripts.publish.publish_ticket_release create \
  --source-dir data/exports/ticket-input \
  --ticket 42 --sequence 1 \
  --handler scripts.dev_sync.tickets.issue_0042_example \
  --dataset example_dataset --release-version example-2026.1 \
  --first-season 2024 --last-season 2026 \
  --pipeline-commit "$PIPELINE_COMMIT" \
  --schema-repository bracketballer/fastify --schema-ref develop \
  --schema-commit "$FASTIFY_COMMIT" --flyway-version 36 \
  --flyway-checksum 35=-1973679461 --flyway-checksum 36=123456789

python -m scripts.publish.publish_ticket_release upload \
  --archive data/exports/ticket-releases/issue-0042-example-2026.1.tar.gz \
  --checksum data/exports/ticket-releases/issue-0042-example-2026.1.tar.gz.sha256 \
  --manifest data/exports/ticket-releases/issue-0042-example-2026.1.tar.json
```

Commit the generated descriptor and handler only after reviewing the aggregate
validation output. Automatic hooks use the local read-only Spaces credentials;
staging and production use an explicit, reviewed invocation.
