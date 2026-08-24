# Issue #6 shot-event corpus

Shot ingestion has two deliberately separate populations:

- the existing `players.season` population used by fantasy and community
  surfaces; and
- active roster memberships on eligible 2024+ team-seasons whose matching
  `player_seasons.minutes` is at least 100, used by lineup strategy.

The ingestion selector is the union of those populations. Roster-only player
identities remain outside the fantasy catalog.

## Download and apply

Download the CBBD date responses once, with resumable local files:

```bash
PYTHONPATH=src python -m scripts.ingest.ingest_cbbd_shots_bulk \
  --first 2024 --last 2026 \
  --release-version issue-0006-shots-2026.1 \
  --download
```

Validate the bundle without changing PostgreSQL:

```bash
PYTHONPATH=src python -m scripts.ingest.ingest_cbbd_shots_bulk \
  --first 2024 --last 2026 \
  --release-version issue-0006-shots-2026.1
```

Apply the local bundle to the development database. Only pending
player-seasons are reconciled; existing fantasy shot rows are retained:

```bash
PYTHONPATH=src python -m scripts.ingest.ingest_cbbd_shots_bulk \
  --first 2024 --last 2026 \
  --release-version issue-0006-shots-2026.1 \
  --skip-score --apply
```

Successful empty CBBD responses are recorded as `no_data`. That is complete
ingestion, while actual event coverage is reported separately.

## Ticket release

After applying the local backfill, export only the lineup-strategy expansion:

```bash
PYTHONPATH=src python -m scripts.publish.export_issue_0006_shot_corpus \
  --first 2024 --last 2026 \
  --output-dir data/exports/ticket-input/issue-0006
```

The resulting ticket artifact contains shot events and statuses for roster
player-seasons outside the legacy fantasy population. The checked-in ticket
descriptor and managed development hooks distribute it to local databases;
the handler verifies that the issue #4 roster/eligibility data is present
before applying it.
