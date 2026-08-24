# Coordinate-derived shot zones (shot-location-v1)

This document records the coordinate audit and the v1 zone contract for
`41-and-0-player-data#5`. The audit uses field-goal events only. Free throws
remain available to usage models, but they are not part of a location share.

## Decisions

The v1 classifier emits exactly five symmetric zones:

```text
rim, short_mid, long_mid, corner_three, above_break_three
```

The model is restricted to 2024 and later. The existing four categorical
`shooting-v1` zones are unchanged; this module is a separate coordinate-based
contract for the offensive-projection work.

Coordinates are interpreted as a full NCAA court scaled by ten: `x=0..940`
and `y=0..500` become `0..94` and `0..50` feet. The canonical attacking basket
is at `(5.25, 25)` feet. Right-basket possessions reflect `x` into that frame;
`abs(y - 25)` makes the zones left/right symmetric.

Hard polygons are used for v1. Spatial smoothing and latent shot bases are
deferred. The three-point geometry follows the [official NCAA court
diagram](https://ncaaorg.s3.amazonaws.com/championships/sports/basketball/rules/common/PRXBB_CourtDiagram.pdf):

- `rim`: distance from the hoop through 4 feet;
- `short_mid`: greater than 4 through 14 feet;
- `long_mid`: greater than 14 feet and inside the three-point line;
- `corner_three`: outside the straight 21'7 7/8" corner line up to its
  intersection with the 22'1 3/4" arc;
- `above_break_three`: outside the 22'1 3/4" arc.

The straight/arc transition is `x=9.8808498103` feet from the attacked
baseline. Exact three-point boundaries are threes; the intersection belongs to
`corner_three`.

And-ones and shooting fouls keep their field-goal event and are classified by
the field-goal location. Any resulting free throws remain separate. Events
with missing, non-finite, out-of-court, or unresolved-direction coordinates
return no zone and are never silently assigned to a fallback bucket.

## Attacking direction

CBBD does not provide an explicit attacked basket. The audit treats each shot
as an end-of-possession observation and solves one orientation per game:

1. Each located `x` votes for the left or right half of the full court.
2. The two possible period-one assignments are scored against all votes.
3. Opponents are assigned opposite baskets in the same period.
4. Period two and every overtime period use the opposite basket from period
   one.
5. The winning orientation is applied to every event for that team and period,
   including backcourt heaves. Ties or games with malformed team/period data
   remain unresolved.

This avoids the nearest-basket error that would reflect a heave into the wrong
half. `src/bracketballer_data/shot_zones.py` exposes the pure
`infer_attacking_baskets` helper and `classify_shot` classifier.

## Corpus audit

The report was generated from the checked-in
`data/exports/shots/shots_2020_2026.jsonl.gz` export. Coverage is valid
coordinate pairs divided by field-goal attempts; free throws are excluded.

| Season | Field-goal attempts | Located | Coordinate coverage | Classified | Missing | Unresolved direction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2020 | 181,310 | 127,862 | 70.5212% | 127,860 | 53,448 | 2 |
| 2021 | 137,484 | 103,742 | 75.4575% | 103,742 | 33,742 | 0 |
| 2022 | 177,828 | 142,707 | 80.2500% | 142,707 | 35,121 | 0 |
| 2023 | 202,450 | 173,661 | 85.7797% | 173,661 | 28,789 | 0 |
| 2024 | 251,009 | 226,827 | 90.3661% | 226,787 | 24,182 | 40 |
| 2025 | 263,033 | 259,754 | 98.7534% | 259,754 | 3,279 | 0 |
| 2026 | 384,380 | 352,393 | 91.6783% | 352,393 | 31,987 | 0 |

No located event was outside the observed court bounds. Across 2024–2026,
838,961 events had a usable x-direction vote; 420 disagreed with the inferred
game orientation (0.0501%). These disagreements are retained as diagnostics
because they can represent heaves or source-coordinate noise, not necessarily
direction errors. There were 42 valid-coordinate events in unresolved games.

Among 1,386,904 classified events whose categorical source range was either
two-point or three-point, 1,383,479 agreed with the coordinate-derived
two/three geometry (99.753%). The 3,425 disagreements remain visible in the
audit report for review rather than being corrected from `shot_range`.

### Coverage by conference

The conference is taken from each raw CBBD payload. Each cell is
`located/field-goal attempts (coverage)`; `—` means that conference was not
present in that season's selected-player corpus. The 2024–2026 table is the v1
coverage view requested by the requirements checklist.

| Conference | 2024 | 2025 | 2026 |
| --- | --- | --- | --- |
| A-10 | 7,826/8,145 (96.08%) | 8,821/8,869 (99.46%) | 9,062/11,272 (80.39%) |
| ACC | 6,306/8,514 (74.07%) | 12,556/12,899 (97.34%) | 16,888/17,277 (97.75%) |
| ASUN | 8,422/9,975 (84.43%) | 10,971/11,078 (99.03%) | 14,655/15,519 (94.43%) |
| Am. East | 5,704/5,926 (96.25%) | 7,999/8,078 (99.02%) | 9,499/10,035 (94.66%) |
| American | 9,978/11,382 (87.66%) | 9,106/9,270 (98.23%) | 10,186/12,747 (79.91%) |
| Big 12 | 4,993/7,189 (69.45%) | 8,144/8,293 (98.20%) | 15,482/16,143 (95.91%) |
| Big East | 5,159/6,242 (82.65%) | 8,876/8,928 (99.42%) | 8,954/9,088 (98.53%) |
| Big Sky | 7,103/7,423 (95.69%) | 8,177/8,329 (98.18%) | 11,832/12,743 (92.85%) |
| Big South | 5,290/5,655 (93.55%) | 5,868/5,939 (98.80%) | 10,232/10,840 (94.39%) |
| Big Ten | 7,289/8,417 (86.60%) | 10,690/10,773 (99.23%) | 15,479/16,187 (95.63%) |
| Big West | 6,319/6,625 (95.38%) | 9,321/9,396 (99.20%) | 13,362/14,181 (94.22%) |
| CAA | 10,711/11,096 (96.53%) | 10,627/10,748 (98.87%) | 12,636/13,339 (94.73%) |
| CUSA | 4,966/5,152 (96.39%) | 7,501/7,571 (99.08%) | 11,785/12,962 (90.92%) |
| Horizon | 8,135/8,684 (93.68%) | 9,288/9,390 (98.91%) | 12,583/13,537 (92.95%) |
| Indep. | 970/1,016 (95.47%) | — | — |
| Ivy | 4,524/4,832 (93.63%) | 4,689/4,777 (98.16%) | 7,562/7,836 (96.50%) |
| MAAC | 7,478/7,842 (95.36%) | 10,347/10,507 (98.48%) | 12,597/13,191 (95.50%) |
| MAC | 7,847/8,659 (90.62%) | 9,001/9,062 (99.33%) | 13,829/14,901 (92.81%) |
| MEAC | 2,544/2,938 (86.59%) | 8,474/8,536 (99.27%) | 6,089/7,207 (84.49%) |
| MVC | 7,976/8,479 (94.07%) | 10,580/10,750 (98.42%) | 8,455/11,486 (73.61%) |
| Mountain West | 6,458/6,920 (93.32%) | 7,811/7,874 (99.20%) | 9,768/11,013 (88.70%) |
| NEC | 6,556/7,002 (93.63%) | 7,471/7,525 (99.28%) | 9,419/10,172 (92.60%) |
| OVC | 8,856/9,162 (96.66%) | 8,253/8,310 (99.31%) | 9,989/11,129 (89.76%) |
| Pac-12 | 8,422/10,300 (81.77%) | — | — |
| Patriot | 5,392/5,568 (96.84%) | 6,571/6,687 (98.27%) | 10,434/11,163 (93.47%) |
| SEC | 6,576/8,689 (75.68%) | 8,583/8,948 (95.92%) | 15,202/16,261 (93.49%) |
| SWAC | 7,811/8,320 (93.88%) | 7,368/7,442 (99.01%) | 13,273/14,299 (92.82%) |
| SoCon | 8,461/8,870 (95.39%) | 6,223/6,289 (98.95%) | 10,885/11,480 (94.82%) |
| Southland | 7,585/7,917 (95.81%) | 6,903/6,995 (98.68%) | 11,157/12,025 (92.78%) |
| Summit | 6,691/7,433 (90.02%) | 7,542/7,600 (99.24%) | 9,792/11,062 (88.52%) |
| Sun Belt | 11,214/11,601 (96.66%) | 10,616/10,690 (99.31%) | 14,582/15,291 (95.36%) |
| WAC | 7,631/7,947 (96.02%) | 4,468/4,512 (99.02%) | 6,491/7,013 (92.56%) |
| WCC | 5,634/7,089 (79.48%) | 6,909/6,968 (99.15%) | 10,234/12,977 (78.86%) |
| `<missing>` | — | — | 0/4 (0.00%) |

The corpus is still the selected-player population used by the existing
pipeline, not a complete roster corpus. T3 must expand ingestion before
downstream player and lineup profiles claim full-team coverage.

## Reproduction

Offline audit from the checked-in export:

```bash
PYTHONPATH=src python -m scripts.analysis.audit_shot_zones \
  --first 2020 --last 2026 \
  --shots-export data/exports/shots/shots_2020_2026.jsonl.gz \
  --report data/reports/shot-zones/shot-zone-audit.json
```

Against PostgreSQL, omit `--shots-export`; the script reads
`player_shot_events` in a read-only session and uses the repository's normal
`.env` connection settings. The report is aggregate-only and lives under the
ignored `data/reports` tree.

Unit coverage is in `tests/test_shot_zones.py`:

```bash
PYTHONPATH=src python -m unittest tests.test_shot_zones -v
```
