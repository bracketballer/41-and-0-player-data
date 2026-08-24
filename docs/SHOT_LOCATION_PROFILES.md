# Player shot-location profiles (`shot-location-v1`)

The coordinate-derived profile is the first input to the offensive lineup
projection model. It is separate from the legacy categorical
`shooting-v1` profile and uses the five zones defined in `docs/SHOT_ZONES.md`:

```text
rim, short_mid, long_mid, corner_three, above_break_three
```

## Population and evidence

The profile population is the distinct player-season union of active roster
memberships on eligible teams whose `player_seasons.minutes >= 100`. The
minimum classified-attempt threshold is zero. Every eligible player receives
five rows, including a player with no classified coordinates; that player's
rows contain the league-season prior and raw counts of zero. Raw counts and
posterior parameters remain visible so consumers can distinguish prior-only
evidence from a well-sampled profile.

Events are restricted to the current season. A same-season transfer is pooled
by the stable player ID. Freshmen and transfers without prior data use the same
league-season prior; no prior-season blend is used in v1.

## Estimator

For zone `j`, let `n_j` and `m_j` be the player's classified attempts and makes,
and let `L_j` and `M_j` be all-corpus classified league-season counts.

```text
leagueShare_j = L_j / sum(L)
attemptShare_j = (n_j + 0.5 * leagueShare_j) / (sum(n) + 0.5)

leagueAccuracy_j = clamp(M_j / L_j, 0.01, 0.99)
posteriorAlpha_j = m_j + 50 * leagueAccuracy_j
posteriorBeta_j  = (n_j - m_j) + 50 * (1 - leagueAccuracy_j)
adjustedPPS_j    = pointValue_j * posteriorAlpha_j /
                   (posteriorAlpha_j + posteriorBeta_j)
```

The 0.5-attempt composition prior prevents zero logs in the downstream defense
tilt adjustment. The 50-attempt Beta prior matches the established shooting
ability estimator. All current-season games have equal weight; there is no
recency decay in v1.

## Computation and publication

```bash
PYTHONPATH=src python -m scripts.compute.compute_shot_location_profiles \
  --first 2024 --last 2026 --version shot-location-v1
```

The command is dry-run by default. `--apply` creates an audited, immutable,
inactive model candidate in Fastify V36's
`player_shot_location_profiles` table. The candidate remains inactive until
the downstream defensive, usage, matchup, and projection jobs have completed.
During an active season, rerun the computation nightly after shot ingestion;
use a new immutable version for each published refresh.
