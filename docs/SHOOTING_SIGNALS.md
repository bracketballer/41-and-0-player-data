# Shooting Signals and Role Labels

This document describes the application-facing signals derived from CBBD shot
events. All scores are relative to players in the same season and use the active
shooting model (`shooting-v1`). A score of 90 means approximately the 90th
percentile; it does not mean 90% shooting or a 90% probability.

## Core shooting profile

The core profile is stored in `player_shooting_ability_profiles`, with one
supporting row per zone in `player_shooting_zone_profiles`.

- **Shooting ability** combines shot-making above expectation (45%), spacing
  (35%), three-level versatility (15%), and free-throw ability (5%).
- **Efficiency** ranks expected points per 100 shooting possessions.
- **Shot-making** measures points above the season average while holding the
  player's shot mix constant.
- **Spacing** combines the probability that three-point accuracy is no worse
  than two percentage points below average with meaningful three-point volume.
- **Versatility** measures credible scoring threats at the rim, from two-point
  jumper range, and from three.
- **Self-creation** is a proxy based on unassisted made jumpers and threes. CBBD
  does not reliably identify whether missed shots were assisted, so this is not
  a true pull-up or isolation efficiency metric.
- **Confidence** combines event-to-box-score coverage with attempt-weighted
  Bayesian reliability.

Zone accuracy is adjusted toward the attempt-weighted season average:

```text
adjustedAccuracy = (makes + 50 * seasonAverage) / (attempts + 50)
```

## Contextual signals

Contextual signals are stored in `player_contextual_shooting_profiles`. The
supporting game rows in `player_shooting_game_profiles` make each summary
auditable and allow the signals to be recomputed without another API request.

### Clutch shooting

A clutch attempt occurs in the final five minutes of regulation or in overtime
when the score before the attempt is within five points.

```text
clutchDelta = 100 * (actualPoints - expectedPlayerPoints) / shootingPossessions
reliability = clutchPossessions / (clutchPossessions + 20)
clutchRaw = clutchDelta * reliability
```

`clutch_score` is the season percentile of `clutchRaw`. Expected points use the
player's full-season adjusted accuracy in each zone. `clutch_delta_p10` and
`clutch_delta_p90` come from deterministic Beta-posterior simulation.

`clutch_confidence` is reliability multiplied by event coverage. A strong score
with low confidence should be treated as promising evidence, not an established
trait.

### Consistency and volatility

Each game receives an execution residual:

```text
gameExecution = 100 * (actualPoints - expectedPlayerPoints) / shootingPossessions
```

Only games with at least five shooting possessions enter the stability sample.

- **Median game execution** is the typical game-level residual.
- **Execution MAD** is the median absolute deviation around that median.
- **Shooting floor/ceiling** are the 20th and 80th percentile game residuals.
- **Above-expectation game rate** is the share of qualifying games above zero.
- **Consistency score** ranks negative MAD, so a higher score means less
  game-to-game variation.
- **Volatility score** is `100 - consistency_score`.

Consistency describes stability, not quality. A player can be consistently
poor, so labels requiring consistency also require strong shooting ability.

### Shot selection and execution

Shot selection values the player's field-goal mix using season-average zone
accuracy rather than the player's results:

```text
selectionValue = 100 * sum(zoneAttempts * pointValue * leagueZoneAccuracy) / FGA
```

`selection_score` is the same-season percentile. The core shot-making score
represents execution. Together they distinguish good decision-makers from
difficult-shot makers.

### Rim pressure

```text
rimPressurePer40 = 40 * (rimAttempts + 0.44 * FTA) / minutes
```

`rim_pressure_score` is its season percentile. This measures pressure volume;
rim finishing accuracy remains a separate core zone metric.

### Scalability

Scalability estimates how readily a shooting profile fits beside other talented
players without requiring that the player dominate possession:

```text
40% spacing
20% efficiency
20% assisted-perimeter-make-rate percentile
10% versatility
10% core confidence
```

Because assistance is only dependable on made shots, this is an off-ball role
proxy—not measured catch-and-shoot efficiency or actual lineup synergy.

### Postseason performance

Postseason events are identified by CBBD's `seasonType = postseason` field.
The signal measures points above the player's normal zone expectations:

```text
postseasonRaw = postseasonDelta * postseasonPossessions / (postseasonPossessions + 30)
```

`postseason_score` is its season percentile and `postseason_confidence` includes
the same reliability and coverage adjustment. Players with no postseason sample
receive `null`, not an average score.

### Matchup resistance

Opponent shot defense is estimated from the event corpus as points allowed above
or below season-average zone expectations, shrunk with a 200-possession prior.
The strongest quartile of opponents in each season defines the difficult-opponent
sample.

```text
matchupRaw = strongOpponentDelta * possessions / (possessions + 30)
```

`matchup_resistance_score` is the season percentile. This is an event-corpus
opponent adjustment, not a complete team defensive rating; the current
`team_seasons` table contains no defensive data.

## Role labels

Labels are deliberately multi-valued: one player can be both an elite spacer
and a self-creating shooter. They are stored as a JSON array in `role_labels`.

| Label | Qualification |
|---|---|
| `ELITE_SPACER` | Spacing score at least 90 |
| `HIGH_VOLUME_SHOOTER` | At least 4 three-point attempts per 40 and spacing score at least 70 |
| `THREE_LEVEL_SCORER` | Versatility at least 75 and threat score at least 0.50 at rim, jumper, and three |
| `RIM_ATTACKER` | Rim-pressure score at least 75 |
| `MIDRANGE_SPECIALIST` | Jumpers are at least 25% of FGA, at least 3 per 40, and adjusted accuracy is at or above the zone baseline |
| `SELF_CREATING_SHOOTER` | Self-creation at least 80 and shot-making at least 60 |
| `SCALABLE_SHOOTER` | Scalability score at least 80 |
| `VOLATILE_GUNNER` | Volatility at least 80 and at least 4 three-point attempts per 40 |
| `CONSISTENT_SHOOTER` | Consistency at least 80 and overall shooting ability at least 70 |
| `CLUTCH_SCORER` | Clutch score at least 80 with confidence at least 25 |
| `POSTSEASON_RISER` | Postseason score at least 80 with confidence at least 20 |
| `MATCHUP_RESISTANT` | Matchup-resistance score at least 80 with confidence at least 20 |
| `FREE_THROW_WEAPON` | Free-throw score at least 90 and at least 3 FTA per 40 |

## Interpretation rules

1. Always show score and confidence together for clutch, postseason, and matchup
   signals.
2. Do not interpret a percentile as a raw percentage.
3. Prefer the adjusted zone accuracy over raw percentage for player comparison.
4. Treat `SELF_CREATING_SHOOTER` and scalability as proxies because assisted
   status is incomplete on misses.
5. Coordinates should only support additional shot-location models in seasons
   with acceptable coordinate coverage.
6. On-floor data is available beginning primarily in 2024, so no cross-season
   label currently claims observed lineup synergy. See
   `LINEUP_ATTRIBUTION.md` for measured coverage and the current fallback
   guidance for shot-to-lineup attribution.
7. Complete-roster composition is evaluated at request time by Fastify from
   these stored player features; see `TEAM_LINEUP_SIGNALS.md`.
