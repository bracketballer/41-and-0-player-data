"""Fit the v1 efficiency-versus-usage slope.

The command reads eligible ``player_seasons`` rows from PostgreSQL in a
read-only session and emits aggregate diagnostics only.  The accepted v1
constant is checked into the pure model module after review.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.usage_efficiency import (
    MAX_USAGE_DELTA,
    MODEL_VERSION,
    UsageObservation,
    fit_usage_slope,
)


FIRST_SEASON = 2024
LAST_SEASON = 2026
MIN_ROTATION_MINUTES = 100
DEFAULT_REPORT = Path("data/reports/usage-efficiency/usage-efficiency-fit.json")


USAGE_ROWS_SQL = """
WITH eligible_keys AS (
    SELECT DISTINCT ps.player_id, ps.team_id, ps.season
    FROM player_seasons ps
    JOIN team_roster_memberships membership
      ON membership.player_id = ps.player_id
     AND membership.team_id = ps.team_id
     AND membership.season = ps.season
     AND membership.source_active
    JOIN team_season_eligibility eligibility
      ON eligibility.team_id = ps.team_id
     AND eligibility.season = ps.season
    WHERE ps.season BETWEEN %s AND %s
      AND ps.source_active
      AND ps.minutes >= %s
), aggregated AS (
    SELECT ps.player_id,
           ps.season,
           SUM(COALESCE(ps.minutes, 0)) AS minutes,
           SUM(COALESCE(ps.field_goals_attempted, 0)) AS field_goals_attempted,
           SUM(COALESCE(ps.free_throws_attempted, 0)) AS free_throws_attempted,
           SUM(COALESCE(ps.points, 0)) AS points,
           SUM(
               CASE
                   WHEN ps.usage IS NOT NULL AND ps.minutes IS NOT NULL
                       THEN ps.usage * ps.minutes
                   ELSE 0
               END
           ) / NULLIF(
               SUM(CASE WHEN ps.usage IS NOT NULL THEN COALESCE(ps.minutes, 0) ELSE 0 END),
               0
           ) AS usage
    FROM player_seasons ps
    JOIN eligible_keys eligible
      ON eligible.player_id = ps.player_id
     AND eligible.team_id = ps.team_id
     AND eligible.season = ps.season
    WHERE ps.source_active
    GROUP BY ps.player_id, ps.season
)
SELECT player_id,
       season,
       usage,
       points,
       field_goals_attempted,
       free_throws_attempted,
       minutes
FROM aggregated
WHERE usage IS NOT NULL
  AND field_goals_attempted > 0
  AND (2.0 * (field_goals_attempted + 0.44 * free_throws_attempted)) > 0
ORDER BY player_id, season
"""


def load_usage_observations(
    conn: Any, first: int, last: int
) -> list[UsageObservation]:
    """Load one aggregated observation per eligible player-season."""

    with conn.cursor() as cursor:
        cursor.execute(USAGE_ROWS_SQL, (first, last, MIN_ROTATION_MINUTES))
        rows = cursor.fetchall()
    observations: list[UsageObservation] = []
    for (
        player_id,
        season,
        usage,
        points,
        field_goals_attempted,
        free_throws_attempted,
        _minutes,
    ) in rows:
        fga = int(field_goals_attempted)
        fta = int(free_throws_attempted)
        shooting_possessions = fga + 0.44 * fta
        true_shooting_pct = float(points) / (2.0 * shooting_possessions)
        observations.append(
            UsageObservation(
                player_id=int(player_id),
                season=int(season),
                usage=float(usage),
                true_shooting_pct=true_shooting_pct,
                field_goals_attempted=fga,
                free_throws_attempted=fta,
            )
        )
    return observations


def fit_report(observations: list[UsageObservation], first: int, last: int) -> dict[str, Any]:
    """Return the aggregate, reviewable fit report."""

    fit = fit_usage_slope(observations)
    accepted = fit.coefficient < 0.0
    return {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "status": "accepted" if accepted else "rejected_nonnegative_slope",
        "first_season": first,
        "last_season": last,
        "configuration": {
            "population": "active eligible-team memberships with player_seasons.minutes >= 100",
            "usage_definition": "player_seasons.usage standard USG%, in percentage-point units",
            "efficiency_target": "2 * true_shooting_pct (points per shooting possession)",
            "weight": "field_goals_attempted + 0.44 * free_throws_attempted",
            "regression": "efficiency_target ~ player_fixed_effect + season_fixed_effect + usage",
            "uncertainty": "player-clustered sandwich standard error",
            "max_usage_delta": MAX_USAGE_DELTA,
        },
        "observations": fit.observations,
        "players": fit.players,
        "seasons": list(fit.seasons),
        "weighted_shooting_possessions": fit.weighted_shooting_possessions,
        "coefficient": fit.coefficient,
        "standard_error": fit.standard_error,
        "confidence_low": fit.confidence_low,
        "confidence_high": fit.confidence_high,
    }


def run(
    *,
    first: int,
    last: int,
    report_path: Path,
    database_url: str | None = None,
) -> dict[str, Any]:
    if first > last:
        raise ValueError("first season cannot exceed last season")
    load_env_file()
    conn = psycopg2.connect(database_url or connection_dsn())
    conn.set_session(readonly=True, autocommit=True)
    try:
        observations = load_usage_observations(conn, first, last)
    finally:
        conn.close()
    report = fit_report(observations, first, last)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote usage-efficiency fit -> {report_path}")
    print(
        f"observations={report['observations']} players={report['players']} "
        f"seasons={report['seasons']}"
    )
    print(
        f"coefficient={report['coefficient']:.10f} "
        f"SE={report['standard_error']:.10f}"
    )
    if report["status"] != "accepted":
        raise ValueError(
            "usage-efficiency slope is non-negative; v1 publication is rejected"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    run(
        first=args.first,
        last=args.last,
        report_path=args.report,
        database_url=args.database_url,
    )


if __name__ == "__main__":
    main()
