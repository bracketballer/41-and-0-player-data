# Defensive-lineup zone concession (`shot-location-v1`)

This is the second input to the offensive lineup projection model. It describes
how a stored defensive five changes the zone distribution of the offense it
faces. The profile is a concession tilt, not a defensive rating.

## Population and realistic units

Issue #10 currently publishes the 2025–26 season only. A stored lineup is a
realistic unit when its aggregated `team_game_lineups.total_seconds` is at
least 2% of that eligible team-season's total five-player lineup seconds. The
threshold is inclusive and adapts to rotation depth instead of forcing every
team into a fixed top-N list.

The 2026 source distribution contains 44 eligible teams and 460 selected units:

| Statistic | Result |
|---|---:|
| Median units per team | 10 |
| P05 / P95 units per team | 6 / 14 |
| Median selected minutes share | 55.22% |
| P05 selected minutes share | 31.59% |

Units retain their sorted player IDs and canonical hash. Possessions come from
the summed `team_game_lineups.opponent_stats.possessions` field. The source
opponent field-goal attempts are retained as the denominator for attribution
coverage.

## Evidence and estimator

Only classified coordinate field goals with a valid, exactly-five defensive
`onFloor` set that exactly matches the same-game stored lineup contribute to a
unit. The current 2026 run contains 428,182 field-goal events, 393,768
classified events, and 52,530 exact matched unit events.

For zone `j`, let `L_j` be league counts, `T_j` team-season counts, and `U_j`
the defensive-unit counts. Both hierarchy levels use a 50-attempt prior:

```text
leagueShare_j = L_j / sum(L)
teamShare_j   = (T_j + 50 * leagueShare_j) / (sum(T) + 50)
unitShare_j   = (U_j + 50 * teamShare_j) / (sum(U) + 50)
tilt_j        = log(unitShare_j / leagueShare_j)
```

The stored table contains one row per unit and zone, with possessions,
confidence, and an evidence status. Confidence is:

```text
confidence = 100 * U / (U + 50) * min(1, U / opponentFGA)
```

where `U` is the unit's classified matched FGA count. The v1 confidence floor
is 45. Missing evidence is represented with `unavailable` plus null tilt and
confidence; it is never represented as a zero tilt.

## Audit status

The independent T1 lineup-accuracy audit is still pending. Therefore this
release marks every usable 2026 unit `provisional`, including units whose
numeric confidence is above 45. A future immutable release may mark rows
`available` only after that audit passes and the confidence floor is met.

The 2024 and 2025 seasons remain a team-level fallback because their full-roster
lineup attribution has not cleared the same coverage requirements. They are not
encoded as synthetic five-player rows in `defense_lineup_zone_concession`.

## Reproduction and publication

The read-only compute command exports the ticket artifact:

```bash
PYTHONPATH=src python -m scripts.compute.compute_defense_lineup_concession \
  --first 2026 --last 2026 \
  --version shot-location-v1 \
  --output-dir data/exports/ticket-input/issue-0010
```

The generic ticket-release publisher creates and uploads the immutable archive;
the checked-in issue #10 handler validates it before writing V36 rows. The
model version remains inactive until the downstream projection tables are
complete.
