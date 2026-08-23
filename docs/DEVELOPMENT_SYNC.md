# Local development synchronization

The player-data repository installs managed Git hooks through
`scripts/setup-local.sh`. Git has no dedicated `post-pull` hook, so the sync
runs after `post-merge` (merge-based pulls), `post-rewrite` (rebased pulls),
and `post-checkout` (branch switches).

Each release descriptor may declare a `schema_dependency`. The sync fetches the
declared Fastify ref, verifies that it contains the exact commit, creates a
temporary detached worktree, builds Fastify's Flyway image from that worktree,
and migrates the configured local database. Only after the required Flyway
checksums are present does it invoke the audited data-release runner.

The active sibling checkout is never switched, stashed, cleaned, or pulled.
Set `FASTIFY_REPO_PATH` when Fastify is not at `../fastify`. The automatic path
accepts only loopback/local PostgreSQL hosts; shared or remote databases must
be handled manually with the explicit data-release command.

Manual invocation from the repository root:

```bash
.venv/bin/python -m scripts.dev_sync.sync_after_git_update
```

Missing local configuration or Spaces credentials produces a safe skip. A
configured local database with a declared schema dependency requires Git,
Docker, and a reachable Fastify origin; migration, checksum, artifact, or
audited-publication failures are reported as errors and never silently ignored.
