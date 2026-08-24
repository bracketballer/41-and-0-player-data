# Heuristic matchup assignment (`heuristic-matchup-v1`)

This model produces one deterministic assignment for an offensive five and a
defensive five. It is intentionally independent of projected PPS: the defense
chooses the matchups, so the caller must not maximize over alternate
assignments.

## Rules

The offensive primary handler is the player with the highest existing handler
score. That score is the established lineup-signal metric:

```text
50% assists percentile + 30% assist/turnover percentile + 20% usage percentile
```

For every defender, v1 computes:

```text
perimeterScore = 0.75 × DEFENSIVE_DISRUPTOR
               + 0.25 × (100 − DROP_COMPATIBLE_BIG)
```

`perimeterScore >= 70` is strong-perimeter evidence. The highest-scoring
defender is assigned to the primary handler. If no defender reaches 70, the
highest score is still used and the assignment is marked
`PERIMETER_THRESHOLD_FALLBACK`; unusual but legal lineups never produce an
empty result. Ties use lower `center_role_share`, then smaller handler-size
mismatch, then player ID.

The remaining four defenders are paired with the remaining four offensive
players by minimizing total soft size cost:

```text
sizePenalty = max(0, abs(offensiveHeight − defensiveHeight) − 3 inches)
```

The three-inch allowance is a penalty grace, not a hard constraint. Equal-cost
solutions use lexicographic player-ID order. Missing heights are median-imputed
only for deterministic ordering; serialized size fields remain null and carry
`MISSING_HEIGHT`.

Missing defensive characteristic scores use neutral evidence (50), with an
explicit limitation. They are never silently converted to zero.

## Output and scope

The result contains exactly five pairs, canonically ordered by offensive player
ID. Each pair records both player IDs, primary/perimeter flags, size evidence,
and limitations; this is the five-element JSON array accepted by
`lineup_offensive_projections.matchup_assignment`.

Observed switching, drop, hedge, and matchup-tendency data are not available
in v1. User-specified assignment overrides are deferred to v2.
