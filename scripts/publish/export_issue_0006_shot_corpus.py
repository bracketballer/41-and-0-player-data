"""Export the non-fantasy shot-event delta required by issue #6.

The base development snapshot already contains the legacy ``players.season``
shot corpus. This exporter emits only eligible roster player-seasons that are
outside that legacy population, plus their completed ingestion statuses.
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.shooting_data import EVENT_DB_COLUMNS


FORMAT_VERSION = 1


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, default=_json_default, sort_keys=True))
            output.write("\n")


def export_delta(conn: Any, first: int, last: int, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH lineup AS (
                SELECT DISTINCT membership.player_id, membership.season
                FROM team_roster_memberships membership
                JOIN team_season_eligibility eligibility
                  ON eligibility.team_id = membership.team_id
                 AND eligibility.season = membership.season
                JOIN player_seasons season_row
                  ON season_row.player_id = membership.player_id
                 AND season_row.team_id = membership.team_id
                 AND season_row.season = membership.season
                 AND season_row.source_active
                WHERE membership.season BETWEEN %s AND %s
                  AND membership.source_active
                  AND season_row.minutes >= 100
            )
            SELECT lineup.player_id, lineup.season,
                   status.status, status.event_count
            FROM lineup
            JOIN player_shooting_ingestion_status status
              ON status.player_id = lineup.player_id
             AND status.season = lineup.season
            WHERE NOT EXISTS (
                SELECT 1
                FROM players legacy
                WHERE legacy.id = lineup.player_id
                  AND legacy.season = lineup.season
            )
              AND status.status IN ('success', 'no_data')
            ORDER BY lineup.season, lineup.player_id
            """,
            (first, last),
        )
        statuses = [
            {
                "player_id": int(player_id),
                "season": int(season),
                "status": str(status),
                "event_count": int(event_count),
            }
            for player_id, season, status, event_count in cursor.fetchall()
        ]

        cursor.execute(
            f"""
            WITH lineup AS (
                SELECT DISTINCT membership.player_id, membership.season
                FROM team_roster_memberships membership
                JOIN team_season_eligibility eligibility
                  ON eligibility.team_id = membership.team_id
                 AND eligibility.season = membership.season
                JOIN player_seasons season_row
                  ON season_row.player_id = membership.player_id
                 AND season_row.team_id = membership.team_id
                 AND season_row.season = membership.season
                 AND season_row.source_active
                WHERE membership.season BETWEEN %s AND %s
                  AND membership.source_active
                  AND season_row.minutes >= 100
            )
            SELECT {', '.join(f'e.{column}' for column in EVENT_DB_COLUMNS)}
            FROM player_shot_events e
            JOIN lineup
              ON lineup.player_id = e.player_id
             AND lineup.season = e.season
            WHERE NOT EXISTS (
                SELECT 1
                FROM players legacy
                WHERE legacy.id = e.player_id
                  AND legacy.season = e.season
            )
            ORDER BY e.season, e.player_id, e.game_start_date NULLS LAST,
                     e.game_id, e.source_play_id
            """,
            (first, last),
        )
        events = []
        for row in cursor.fetchall():
            events.append(
                {
                    column: value
                    for column, value in zip(EVENT_DB_COLUMNS, row)
                }
            )

    event_path = destination / "events.jsonl.gz"
    status_path = destination / "statuses.jsonl"
    metadata_path = destination / "metadata.json"
    _write_jsonl_gz(event_path, events)
    with status_path.open("w", encoding="utf-8") as output:
        for row in statuses:
            output.write(json.dumps(row, sort_keys=True) + "\n")

    metadata = {
        "format_version": FORMAT_VERSION,
        "first_season": first,
        "last_season": last,
        "expanded_player_seasons": len(statuses),
        "event_rows": len(events),
        "success_player_seasons": sum(row["status"] == "success" for row in statuses),
        "no_data_player_seasons": sum(row["status"] == "no_data" for row in statuses),
        "seasons": {
            str(season): {
                "player_seasons": sum(row["season"] == season for row in statuses),
                "events": sum(row["season"] == season for row in events),
            }
            for season in range(first, last + 1)
        },
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=2024)
    parser.add_argument("--last", type=int, default=2026)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.first > args.last:
        parser.error("--first cannot be greater than --last")
    load_env_file()
    conn = psycopg2.connect(connection_dsn())
    try:
        print(json.dumps(export_delta(conn, args.first, args.last, args.output_dir), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
