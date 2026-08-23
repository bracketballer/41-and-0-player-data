# On-Floor Lineup Attribution

This document answers T1 of the offensive-projection initiative
(bracketballer/41-and-0-player-data#4): whether player_shot_events.raw_payload
can reconstruct who was on the floor for a shot. The original
scripts/analysis/extract_onfloor_lineups.py report measures coverage and
game-level set consistency. Time-aligned validation is produced by
scripts/analysis/validate_onfloor_lineups.py from a cached CBBD
starter/substitution bundle.

raw_payload.onFloor is a flat array of ten {id, name, team} entries (both teams
combined for that moment), where id is CBBD's athlete id. This database uses
that same id as players.id directly. The 2025–26 ranked-roster release
publishes full-roster identities, memberships, and position mappings; those
roster-only identities remain is_fantasy_eligible = false and are available
for attribution only.

## Coverage statistics

Coverage is measured per shot event on the defensive side (the team not
shooting), with offensive coverage included as supporting context. An event
counts as covered only if all five on-floor entries for that side resolve to a
players row. An event with no onFloor data counts as uncovered.

| Season | Defensive coverage | Offensive coverage | Events |
|---|---|---|---|
| 2024 | 37.59% (126,593 / 336,739) | 40.14% (135,156 / 336,739) | 336,739 |
| 2025 | 34.87% (122,510 / 351,363) | 37.52% (131,821 / 351,363) | 351,363 |
| 2026 (2025–26) | **93.55% (491,065 / 524,922)** | **97.29% (510,687 / 524,922)** | 524,922 |

The 2026 full-roster release (ranked-rosters-2026-08-23.2) clears the coverage
acceptance bar. The 2024 and 2025 numbers remain historical baselines until
those seasons receive the same full-roster treatment.

## Game-level set consistency (not temporal accuracy)

The original comparison joins each resolved defensive five to every distinct
five recorded for that game/team in team_game_lineups. It is now named
game-lineup set consistency because it accepts any five that appeared
somewhere in the game:

| Season | Comparable events | Set mismatches | Set mismatch rate | No stored lineup |
|---|---:|---:|---:|---:|
| 2024 | 35,481 | 0 | **0.00%** | 91,112 |
| 2025 | 36,530 | 0 | **0.00%** | 85,980 |
| 2026 | 53,881 | 0 | **0.00%** | 66,403 |

This is a same-vendor diagnostic, not an event-level accuracy metric.
onFloor and team_game_lineups plausibly share CBBD's underlying pipeline, and
the aggregate lineup table has no period/clock interval. The 0.00% result must
not be used as a release gate.

## Temporal validation

src/bracketballer_data/lineup_validation.py converts each substitution response
into a player stint and indexes those stints by game, team, period, and elapsed
seconds. A shot is temporally comparable only when:

- its onFloor side is an exactly-five, fully resolved set;
- its period and seconds-remaining clock are valid; and
- the substitution source produces exactly five active, resolved players at
  that instant.

Substitution boundaries sharing the shot's clock are reported as
ambiguous_boundary; the validator never guesses whether the shot occurred
before or after the swap. Malformed stints, missing reference rows, and
non-five states are separate statuses and are excluded from the
match/mismatch denominator.

Build a source bundle once (requires CBBD_API_KEY), then run validation
offline:

~~~bash
python -m scripts.analysis.fetch_cbbd_lineup_sources --season 2026 \
    --database-url "host=localhost dbname=bracketballer_dev user=postgres"
python -m scripts.analysis.validate_onfloor_lineups --season 2026 \
    --database-url "host=localhost dbname=bracketballer_dev user=postgres"
~~~

This fixes the missing time-window comparison and can expose extraction or
substitution-order bugs, but it remains a same-vendor consistency check.

## Independent accuracy audit

The no-cost independent layer is a deterministic 150-event audit across at
least 20 games. scripts/analysis/sample_onfloor_audit.py creates a review
manifest with four strata: substitution boundaries (60), ordinary regulation
(60), overtime (15), and late/free-throw/dead-ball sequences (15). Reviewers
fill the expected defensive five from an official NCAA/school record when it
contains the substitution detail, or from timestamped full-game video when it
does not. CBBD endpoints are not accepted as the expected answer.

The manifest stores factual IDs, evidence links/locators, and review metadata;
it does not copy video or copyrighted play-by-play text. A second reviewer
checks every mismatch/ambiguous label and a deterministic 20% sample of
matches. The audit passes only with at least 135 independently verifiable
events, at least 95% exact-five accuracy, a Wilson 95% lower bound of at least
90%, and no systematic substitution-boundary failure. Until that manifest is
completed, temporal accuracy is provisional.

## Guidance and fallback

1. 2025–26 has sufficient coverage for lineup attribution (93.55% defensive,
   97.29% offensive), subject to residual unresolved events.
2. The original coverage gap was roster completeness, not an onFloor shape or
   ID-namespace problem.
3. Use lineup-level attribution only for events with a valid five, a valid
   temporal reference, and no ambiguity after the temporal/audit gates pass.
4. Until those gates pass, use team-level defensive profiles for unverified or
   ambiguous events. For older seasons, use the team-level fallback for all
   events until full-roster coverage is re-measured.

Team-level fallback order:

- team_game_lineups.team_stats / opponent_stats;
- opponent_team_season_contexts when no team-game aggregate exists.

## Method

- Scripts: scripts/analysis/extract_onfloor_lineups.py,
  scripts/analysis/fetch_cbbd_lineup_sources.py,
  scripts/analysis/validate_onfloor_lineups.py, and
  scripts/analysis/sample_onfloor_audit.py.
- Pure logic: src/bracketballer_data/lineup_attribution.py and
  src/bracketballer_data/lineup_validation.py.
- Tests: tests/test_lineup_attribution.py and tests/test_lineup_validation.py.
- known_player_ids is the full players.id set, not season-scoped.
- Coverage denominators include every shot event side, including empty
  onFloor arrays.

## Reproducing this report

~~~bash
python -m scripts.analysis.extract_onfloor_lineups --first 2024 --last 2026 \
    --report data/reports/lineups/onfloor_coverage.csv

# After downloading the 2025–26 source bundle:
python -m scripts.analysis.validate_onfloor_lineups --season 2026 \
    --report data/reports/lineups/onfloor_temporal_2026.json
~~~

The reports are written under data/, which is gitignored. The source bundle is
also local cache data and can be rebuilt from its manifest.
