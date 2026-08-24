"""Apply the issue #6 lineup-strategy shot-event delta."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from psycopg2.extras import Json, execute_values

from bracketballer_data.shooting_data import EVENT_DB_COLUMNS, event_record


FORMAT_VERSION = 1
EVENT_FILE = "events.jsonl.gz"
STATUS_FILE = "statuses.jsonl"
METADATA_FILE = "metadata.json"


def _read_jsonl(path: Path, *, compressed: bool = False) -> list[dict[str, Any]]:
    opener = gzip.open if compressed else open
    try:
        with opener(path, "rt", encoding="utf-8") as source:
            rows = []
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number} must contain an object")
                rows.append(value)
            return rows
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error


def _read_metadata(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {root / METADATA_FILE}: {error}") from error
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported issue #6 artifact format")
    return value


def validate_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete artifact before a database transaction is opened."""
    for name in (EVENT_FILE, STATUS_FILE, METADATA_FILE):
        if not (root / name).is_file():
            raise ValueError(f"issue #6 artifact is missing {name}")
    metadata = _read_metadata(root)
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    if metadata.get("first_season") != first or metadata.get("last_season") != last:
        raise ValueError("issue #6 artifact season range does not match descriptor")

    statuses = _read_jsonl(root / STATUS_FILE)
    events = _read_jsonl(root / EVENT_FILE, compressed=True)
    status_map: dict[tuple[int, int], dict[str, Any]] = {}
    for row in statuses:
        try:
            key = (int(row["player_id"]), int(row["season"]))
            status = str(row["status"])
            event_count = int(row["event_count"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid issue #6 status row: {row}") from error
        if key in status_map or status not in {"success", "no_data"}:
            raise ValueError(f"duplicate or invalid issue #6 status row: {row}")
        if event_count < 0 or (status == "success") != (event_count > 0):
            raise ValueError(f"issue #6 status/event count mismatch: {row}")
        if not first <= key[1] <= last:
            raise ValueError(f"issue #6 status season is outside descriptor range: {row}")
        status_map[key] = {
            "player_id": key[0],
            "season": key[1],
            "status": status,
            "event_count": event_count,
        }

    event_map: dict[int, dict[str, Any]] = {}
    event_counts: dict[tuple[int, int], int] = {}
    for row in events:
        if any(column not in row for column in EVENT_DB_COLUMNS):
            raise ValueError("issue #6 event row is missing a database column")
        try:
            source_play_id = int(row["source_play_id"])
            key = (int(row["player_id"]), int(row["season"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid issue #6 event row: {row}") from error
        if source_play_id in event_map:
            raise ValueError(f"duplicate issue #6 source_play_id {source_play_id}")
        if not first <= key[1] <= last or key not in status_map:
            raise ValueError(f"issue #6 event has no matching status: {row}")
        if not isinstance(row["raw_payload"], dict):
            raise ValueError("issue #6 raw_payload must be an object")
        event_map[source_play_id] = row
        event_counts[key] = event_counts.get(key, 0) + 1

    for key, status in status_map.items():
        if event_counts.get(key, 0) != status["event_count"]:
            raise ValueError(
                f"issue #6 status event_count does not match events for {key}"
            )

    calculated = {
        "expanded_player_seasons": len(statuses),
        "event_rows": len(events),
        "success_player_seasons": sum(row["status"] == "success" for row in statuses),
        "no_data_player_seasons": sum(row["status"] == "no_data" for row in statuses),
    }
    for key, value in calculated.items():
        if metadata.get(key) != value:
            raise ValueError(f"issue #6 metadata mismatch for {key}")
    expected_counts = descriptor.get("expected", {}).get("row_counts", {})
    if expected_counts:
        for key, value in calculated.items():
            if expected_counts.get(key) != value:
                raise ValueError(f"issue #6 descriptor row-count mismatch for {key}")

    return {
        "metadata": metadata,
        "statuses": list(status_map.values()),
        "events": list(event_map.values()),
        "counts": calculated,
    }


def _eligible_expanded_pairs(conn: Any, first: int, last: int) -> set[tuple[int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
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
              AND NOT EXISTS (
                  SELECT 1 FROM players legacy
                  WHERE legacy.id = membership.player_id
                    AND legacy.season = membership.season
              )
            """,
            (first, last),
        )
        return {(int(player_id), int(season)) for player_id, season in cursor.fetchall()}


def apply(
    conn: Any, root: Path, descriptor: dict[str, Any], prepared: dict[str, Any]
) -> dict[str, Any]:
    statuses = prepared["statuses"]
    events = prepared["events"]
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    expected_pairs = {(row["player_id"], row["season"]) for row in statuses}
    actual_pairs = _eligible_expanded_pairs(conn, first, last)
    if actual_pairs != expected_pairs:
        missing = sorted(actual_pairs - expected_pairs)[:20]
        extra = sorted(expected_pairs - actual_pairs)[:20]
        raise ValueError(
            f"issue #6 eligibility mismatch; missing={missing}, extra={extra}"
        )

    columns = ", ".join(EVENT_DB_COLUMNS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in EVENT_DB_COLUMNS
        if column != "source_play_id"
    )
    records = []
    for event in events:
        values = list(event_record(event))
        values[EVENT_DB_COLUMNS.index("raw_payload")] = Json(event["raw_payload"])
        records.append(tuple(values))

    with conn.cursor() as cursor:
        if events:
            source_ids = [int(event["source_play_id"]) for event in events]
            cursor.execute(
                """
                SELECT source_play_id, player_id, season
                FROM player_shot_events
                WHERE source_play_id = ANY(%s::bigint[])
                """,
                (source_ids,),
            )
            conflicts = [
                (int(source_play_id), int(player_id), int(season))
                for source_play_id, player_id, season in cursor.fetchall()
                if (int(player_id), int(season)) not in expected_pairs
            ]
            if conflicts:
                raise ValueError(
                    f"issue #6 source-play conflicts with unrelated rows: {conflicts[:20]}"
                )
        if expected_pairs:
            execute_values(
                cursor,
                """
                DELETE FROM player_shot_events event
                USING (VALUES %s) AS target(player_id, season)
                WHERE event.player_id = target.player_id
                  AND event.season = target.season
                """,
                sorted(expected_pairs),
            )
        if records:
            execute_values(
                cursor,
                f"""
                INSERT INTO player_shot_events ({columns}) VALUES %s
                ON CONFLICT (source_play_id) DO UPDATE SET {updates}, updated_at = now()
                """,
                records,
                page_size=500,
            )
        execute_values(
            cursor,
            """
            INSERT INTO player_shooting_ingestion_status
                (player_id, season, status, event_count, error_message)
            VALUES %s
            ON CONFLICT (player_id, season) DO UPDATE SET
                status = EXCLUDED.status,
                event_count = EXCLUDED.event_count,
                attempt_count = player_shooting_ingestion_status.attempt_count + 1,
                error_message = NULL,
                fetched_at = now()
            """,
            [
                (row["player_id"], row["season"], row["status"], row["event_count"], None)
                for row in statuses
            ],
        )
        if expected_pairs:
            result = execute_values(
                cursor,
                """
                SELECT COUNT(*)::int
                FROM player_shot_events event
                JOIN (VALUES %s) AS pairs(player_id, season)
                  ON pairs.player_id = event.player_id
                 AND pairs.season = event.season
                """,
                sorted(expected_pairs),
                page_size=max(1000, len(expected_pairs)),
                fetch=True,
            )
            published_events = result[0][0]
            published_pairs = len(expected_pairs)
        else:
            published_events = published_pairs = 0

    return {
        "row_counts": {
            **prepared["counts"],
            "player_shot_events": published_events,
            "player_seasons": published_pairs,
        },
        "validation": {
            "eligible_expanded_pairs": len(actual_pairs),
            "published_player_seasons": published_pairs,
            "published_events": published_events,
        },
    }
