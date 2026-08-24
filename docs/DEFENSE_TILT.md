# Defense tilt fit (defense-tilt-v1)

This document records the reproducible λ fit required by issue #7. It is a
team-level research estimate for the offensive lineup projection work; it is
not a defensive rating and it does not publish raw CBBD payloads.

## Model

For each classified field-goal observation, the game is excluded from the
offense baseline and defense concession profile used to describe that same
observation. Profiles use 0.5 league-centered pseudo-attempts per zone:

```text
league_j = smoothed league share in zone j
base_gj  = smoothed offense-season share excluding game g
def_gj   = smoothed defense-season concession share excluding game g
tilt_gj  = log(def_gj / league_j)

p_gj ∝ exp(log(base_gj) + λ · tilt_gj)
```

The likelihood is multinomial over the five coordinate-derived zones:
`rim`, `short_mid`, `long_mid`, `corner_three`, and `above_break_three`.
Standard errors use game-clustered sandwich covariance.

## Fit used by v1

The source was the local `shots_2020_2026.jsonl.gz` export, restricted to
2024–2026 field goals with valid coordinates, resolved attacking direction,
and team/opponent identifiers.

| Quantity | Value |
| --- | ---: |
| Team-game observations | 32,506 |
| Games represented | 17,526 |
| Classified attempts | 838,934 |
| Usable leave-one-game-out observations | 32,063 |
| Usable attempts | 829,319 |
| Selected model | Global λ |

The global fit is:

```text
λ = 0.4620905146
SE = 0.0102109263
```

The per-zone diagnostic fit was:

| Zone | λ_j | SE |
| --- | ---: | ---: |
| rim | 0.5750979258 | 0.0245620097 |
| short_mid | 0.5143245854 | 0.0243612016 |
| long_mid | 0.3174266128 | 0.0174659101 |
| corner_three | 0.4248150318 | 0.0223991986 |
| above_break_three | 0.3967258244 | 0.0273981184 |

Leave-one-season-out log loss was 1.3963287 / 1.3874058 / 1.3940608 for the
global model and 1.3961914 / 1.3872763 / 1.3939697 for the per-zone model in
2024 / 2025 / 2026 respectively. The per-zone improvement is consistent but
less than the preregistered 1% material-gain threshold, so v1 uses the global
coefficient for every zone.

The application constant is
`src/bracketballer_data/defense_tilt.py:V1_DEFENSE_TILT_COEFFICIENTS` and
currently maps all five zones to `0.4620905146`.

## Reproduction

```bash
PYTHONPATH=src python3 -m scripts.compute.fit_defense_tilt \
  --first 2024 --last 2026 \
  --shots-export data/exports/shots/shots_2020_2026.jsonl.gz \
  --report data/reports/defense-tilt/defense-tilt-fit.json
```

The report under `data/reports/` is aggregate-only and intentionally ignored
by Git. The script also supports a read-only PostgreSQL source by omitting
`--shots-export`.

## Limitations

The current export is the selected-player corpus used by the existing shot
ingestion pipeline, not the full eligible-team roster corpus. Issue #6 must
expand that corpus before downstream lineup profiles treat this estimate as a
complete team signal. Free throws, missing coordinates, unresolved direction,
and events without both team identifiers are excluded rather than assigned to
a fallback zone.
