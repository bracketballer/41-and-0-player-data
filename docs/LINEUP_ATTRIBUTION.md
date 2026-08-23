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
| 2024 | 37.59% (126,593 / 336,739) | 40.14% (135,156 / 336,739) | 336,739 |
| 2025 | 34.87% (122,510 / 351,363) | 37.52% (131,821 / 351,363) | 351,363 |
| 2026 (2025–26) | 22.91% (120,284 / 524,922) | 26.50% (139,112 / 524,922) | 524,922 |

Coverage is well below the 90% bar the AC uses, and it declines season over
season rather than improving. That direction rules out "on-floor data
simply isn't populated yet for older seasons" as the explanation — `onFloor`
itself is populated at a similar 10-entries-per-event rate across 2024–2026
(verified by direct query; see Method). The shortfall is on the resolution
side: `players` does not yet contain every player who appears in `onFloor`,
and the gap is largest for the current (2025–26) season, consistent with
`players` population lagging behind the most recent season's roster churn.

These numbers moved up from an earlier measurement taken against a database
with no `team_game_lineups` rows at all (2024: 29.03%, 2025: 25.05%, 2026:
11.94%) — running `ingest_ranked_rosters.py` for all three seasons (see
below) also backfills `players` rows via each season's rosters, which lifted
resolution coverage as a side effect. The gap is still large; this is not
close to the 90% bar.

## Disagreement rate analysis

**Now computable for all three seasons**, after backfilling
`team_game_lineups` (and its dependencies `college_games` and
`team_roster_memberships`) via `scripts/ingest/ingest_ranked_rosters.py
--season {2024,2025,2026}` against this database. That job previously could
not complete a run: issue #16 (CBBD returning `null`
`defenseRating`/`netRating` on some lineup segments crashed the whole
season's fetch) and its follow-up #17 (the resulting known-skipped games
then hard-failed a later validation gate) both blocked ingestion. Both are
fixed and merged; skipped/unavailable team-game lineup evidence is now
excluded and reported (`known_skipped_game_lineups` /
`unavailable_lineup_game_teams`: 486 for 2024, 357 for 2025, 195 for 2026)
rather than aborting the run.

| Season | Comparable events | Disagreements | Disagreement rate | No stored lineup to compare against |
|---|---|---|---|---|
| 2024 | 35,481 | 0 | **0.00%** | 91,112 |
| 2025 | 36,530 | 0 | **0.00%** | 85,980 |
| 2026 | 53,881 | 0 | **0.00%** | 66,403 |

The "no stored lineup to compare against" counts are large mainly because
`team_game_lineups` only covers the AP Top 25 + Virginia Tech eligible
cohort per season (51–53 teams) — a valid-five defensive event for any game
involving a non-eligible team has nothing to compare against, independent of
data quality.

**Read this 0.00% with caution, not as a clean pass.** The comparison logic
(`compute_disagreement_stats` in `src/bracketballer_data/lineup_attribution.py`)
is unit tested against synthetic fixtures that *do* produce nonzero
disagreement rates (see `tests/test_lineup_attribution.py`), so this isn't a
case of the comparison being unable to detect a mismatch in principle. But an
exact 0.00% across three independently-ingested seasons and 125,892 combined
comparable events is more consistent than a genuine independent
cross-check usually produces. The likely explanation: `onFloor` (embedded in
`player_shot_events.raw_payload`) and `team_game_lineups` plausibly both
derive from the same underlying CBBD play-by-play/lineup-tracking pipeline
rather than being independently sourced — so this measures "does CBBD agree
with itself," not "does our extraction match an independent ground truth."
Per the ticket's own testing plan (issue #4 comment, layer 4), this warrants
a manual spot-check — hand-verify a handful of "comparable" events against
CBBD's own box score/play-by-play UI directly — before treating 0%
disagreement as validating the extraction logic. That spot-check has not
been done yet.

## Guidance on attributing shots to defensive lineups

1. **Do not treat `onFloor` as reliably resolvable today.** At 23–38%
   event-level coverage, most shots still cannot be attributed to a specific
   five-man defensive unit. Any downstream consumer (T4/T5/T8) that assumes
   near-complete resolution will silently drop the majority of events.
2. **The coverage gap is a `players` roster-completeness problem, not a
   payload or join-logic problem.** `onFloor` itself is present and shaped
   as expected across 2024–2026 (10 entries, `{id, name, team}`, ids in the
   same namespace as `players.id`). Closing the gap means ensuring every
   player who appears on a box score — not just shot-takers — gets a
   `players` row, likely by broadening whatever ingestion currently seeds
   `players` to include full rosters, not only players who logged an
   attempt.
3. **Coverage is worse, not better, for the current season.** Any consumer
   using this for in-season (2025–26) work should expect the least reliable
   attribution of the three seasons measured.
4. **Disagreement rate is measured now, but treat it as weak evidence, not
   strong validation**, for the circularity reason above. A 0% disagreement
   rate does not license trusting attribution on its own — coverage is the
   binding constraint regardless (see Fallback below), and the disagreement
   number should be re-examined after a manual spot-check rather than cited
   as-is.

## Fallback: team-level profiles (all three seasons are below 90%)

Defensive coverage measures 37.59% (2024), 34.87% (2025), and 22.91% (2026
/ 2025–26) — all far under the 90% threshold, though the AC's 90% bar is
specifically scoped to 2025–26. Per the acceptance criteria, event-level
shot-to-lineup attribution should not be used for 2025–26 data yet, and the
same conclusion holds for 2024/2025 by the same measure. Until `players`
roster-completeness closes this gap, downstream consumers should fall back
to team-level profiles instead of lineup-level attribution:

- Aggregate shot outcomes at the **team-game** level using
  `team_game_lineups.team_stats` / `opponent_stats` (now populated for all
  three seasons, for the eligible cohort) rather than the specific five-man
  unit on the floor for a given shot.
- Where a team-game aggregate isn't available either (e.g. a non-eligible
  team), fall back further to the season-level opponent context already in
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
- `team_game_lineups` population: `scripts/ingest/ingest_ranked_rosters.py
  --season <year> --download`, then a dry run (no flags), then `--apply`,
  once per season (2024, 2025, 2026). Requires `CBBD_API_KEY` for the
  `--download` phase against seasons without an already-cached local bundle
  or checked-in release; dry-run/`--apply` read only the cached bundle and
  do not call CBBD.

## Reproducing this report

```bash
python -m scripts.analysis.extract_onfloor_lineups --first 2024 --last 2026 \
    --report data/reports/lineups/onfloor_coverage.csv
```

Requires `DATABASE_URL` (or `PSQL_*`/`DEV_DB`) pointed at a database with
`player_shot_events` populated; see `docs/DEVELOPMENT_SNAPSHOTS.md` to
restore one locally, and the `team_game_lineups` population step above for
a real (non-`n/a`) disagreement rate. The report CSV is written under
`data/`, which is gitignored — regenerate it rather than expecting it to be
checked in.
