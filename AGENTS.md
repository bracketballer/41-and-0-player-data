# PostgreSQL schema ownership

The sibling Fastify repository owns PostgreSQL schema changes. Add or modify
Flyway migrations under `../fastify/database/migrations`, keep
`../fastify/prisma/schema.prisma` synchronized, run
`npm --prefix ../fastify run generate:entities`, and include every resulting
generated change under `../fastify/src/lib/entities` in the same work.

Do not manually edit files under `../fastify/src/lib/entities`; that directory
is owned by the Fastify repository's `scripts/generate-entities.ts`.

This repository owns ingestion, model computation, and audited dataset
publication. Do not add a second migration set here.

# Ticketed schema-plus-data synchronization

When a change requires both a Fastify Flyway migration and a dependent data
backfill or seed, ship the synchronization unit with the same ticket:

- Add a handler under `scripts/dev_sync/tickets/` named
  `issue_<zero-padded-ticket-number>_<slug>.py`. The handler must be
  idempotent, validate its extracted artifact before opening a write
  transaction, and expose the ticketed release-handler contract.
- Add a descriptor under `releases/tickets/` with the ticket number, explicit
  release sequence, dataset/import version, immutable DigitalOcean Spaces
  object keys, SHA-256 checksums, expected row counts, and the exact Fastify
  commit/Flyway checksums it requires.
- Publish the prepared data artifact with the generic ticket-release publisher
  before committing the descriptor. Upload the archive and checksum first and
  the publication manifest last; never overwrite a mismatched existing object.
- Keep schema changes backward-compatible with the data handler. Flyway and
  data publication are separate transactions, so a failed handler must leave a
  visible audit record and be safe to rerun.

The managed post-checkout, post-merge, and post-rewrite hooks apply checked-in
ticket descriptors only to local/loopback databases. They migrate and verify
the pinned Fastify schema before downloading or applying data, skip matching
published releases, and refuse remote targets. Automatic downloads use
read-only Spaces credentials; use the explicit manual command for staging or
production. Ticket numbers identify ownership; required Flyway version and
release sequence determine execution order.
