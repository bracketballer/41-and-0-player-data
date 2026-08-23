# On-Floor Lineup Attribution

This document answers T1 of the offensive-projection initiative
(bracketballer/41-and-0-player-data#4): whether `player_shot_events.raw_payload`
can reconstruct who was on the floor for a shot, and how that reconstruction
compares against `team_game_lineups`. It is produced by
`scripts/analysis/extract_onfloor_lineups.py`; see "Reproducing this report"
below to regenerate the numbers.

`raw_payload.onFloor` is a flat array of ten `{id, name, team}` entries (both
teams combined for that moment), where `id` is CBBD's athlete id. This
database uses that same id as `players.id` directly — there is no separate
mapping table. A player only has a `players` row if they were themselves
ingested as a shot-taker; an on-floor teammate who never attempted a shot may
have no row to resolve against.

## Coverage statistics

Coverage is measured per shot event on the **defensive** side (the team not
shooting), since that is what shot-to-defense attribution needs. Offensive
coverage is included as supporting context. An event counts as covered only
if all 5 on-floor entries for that side resolve to a `players` row (an event
with no `onFloor` data at all — true for essentially all pre-2024 rows —
counts as uncovered, not excluded).

| Season | Defensive coverage | Offensive coverage | Events |
|---|---|---|---|
| 2024 | 29.03% (97,758 / 336,739) | 31.37% (105,623 / 336,739) | 336,739 |
| 2025 | 25.05% (88,009 / 351,363) | 28.31% (99,454 / 351,363) | 351,363 |
| 2026 (2025–26) | 11.94% (62,691 / 524,922) | 15.68% (82,316 / 524,922) | 524,922 |

Coverage is well below the 90% bar the AC uses, and it declines season over
season rather than improving. That direction rules out "on-floor data
simply isn't populated yet for older seasons" as the explanation — `onFloor`
itself is populated at a similar 10-entries-per-event rate across 2024–2026
(verified by direct query; see Method). The shortfall is on the resolution
side: `players` does not yet contain every player who appears in `onFloor`,
and the gap is largest for the current (2025–26) season, consistent with
`players` population lagging behind the most recent season's roster churn.

## Disagreement rate analysis

**Not currently computable.** `team_game_lineups` (and its dependencies
`college_games` and `team_roster_memberships`) hold **zero rows** in the
database this report was generated against — `scripts/ingest/ingest_ranked_rosters.py`,
the only job that populates them, has not been run in this environment. The
comparison logic (`compute_disagreement_stats` in
`src/bracketballer_data/lineup_attribution.py`) is implemented and unit
tested against synthetic fixtures, and the extraction script already joins
against `team_game_lineups`/`team_game_lineup_players` correctly (see
`no_reference_count` in the report: 97,758 / 88,009 / 62,691 for 2024–2026 —
every single valid-five defensive event had no stored lineup to compare
against). Re-run `scripts/analysis/extract_onfloor_lineups.py` after
`ingest_ranked_rosters.py` has populated `team_game_lineups` for the target
database (or against a database where it already has) to get a real
disagreement-rate number.

## Guidance on attributing shots to defensive lineups

1. **Do not treat `onFloor` as reliably resolvable today.** At 12–29%
   event-level coverage, most shots cannot be attributed to a specific
   five-man defensive unit right now. Any downstream consumer (T4/T5/T8)
   that assumes near-complete resolution will silently drop the majority of
   events.
2. **The gap is a `players` roster-completeness problem, not a payload or
   join-logic problem.** `onFloor` itself is present and shaped as expected
   across 2024–2026 (10 entries, `{id, name, team}`, ids in the same
   namespace as `players.id`). Closing the gap means ensuring every player
   who appears on a box score — not just shot-takers — gets a `players` row,
   likely by broadening whatever ingestion currently seeds `players` to
   include full rosters, not only players who logged an attempt.
3. **Coverage is worse, not better, for the current season.** Any consumer
   using this for in-season (2025–26) work should expect the least reliable
   attribution of the three seasons measured.
4. **Once coverage improves, re-validate against `team_game_lineups` before
   trusting attribution**, since the disagreement rate — the actual measure
   of whether a resolved five-man set matches the ground-truth lineup table
   — has not yet been measured at all (see above).

## Fallback: team-level profiles (2025–26 coverage is below 90%)

2025–26 (season 2026) defensive coverage measures **11.94%**, far under the
90% threshold. Per the acceptance criteria, event-level shot-to-lineup
attribution should not be used for 2025–26 data yet. Until `players`
roster-completeness closes this gap, downstream consumers should fall back
to team-level profiles instead of lineup-level attribution:

- Aggregate shot outcomes at the **team-game** level using
  `team_game_lineups.team_stats` / `opponent_stats` (once populated) rather
  than the specific five-man unit on the floor for a given shot.
- Where a team-game aggregate isn't available either, fall back further to
  the season-level opponent context already in
  `opponent_team_season_contexts`, consistent with `SHOOTING_SIGNALS.md`'s
  existing note that `matchup_resistance_score` is "an event-corpus opponent
  adjustment, not a complete team defensive rating."
- Re-evaluate this fallback once `players` coverage is re-measured; do not
  assume it is still needed without re-running this report.

## Method

- Script: `scripts/analysis/extract_onfloor_lineups.py`
- Pure logic (resolution, coverage, disagreement math): `src/bracketballer_data/lineup_attribution.py`
- Tests: `tests/test_lineup_attribution.py` (21 cases, including fixtures
  sampled from real `player_shot_events` rows)
- `known_player_ids` is the full `players.id` set (not season-scoped), so a
  player who resolves is resolved regardless of which season their
  `players` row was attached to.
- A defensive/offensive side always contributes to the per-season
  denominator, including events with an empty `onFloor` array, so the
  coverage percentage is "share of all shot events," matching the AC
  wording, not "share of events where onFloor happened to be present."

## Reproducing this report

```bash
python -m scripts.analysis.extract_onfloor_lineups --first 2024 --last 2026 \
    --report data/reports/lineups/onfloor_coverage.csv
```

Requires `DATABASE_URL` (or `PSQL_*`/`DEV_DB`) pointed at a database with
`player_shot_events` populated; see `docs/DEVELOPMENT_SNAPSHOTS.md` to
restore one locally. The report CSV is written under `data/`, which is
gitignored — regenerate it rather than expecting it to be checked in.
