"""Safely stage, validate, and publish schools/players/positions CSVs.

The command is a dry run unless --apply is supplied. Unlike the destructive
sql/bootstrap/load_core_data.sql bootstrap, it never truncates source tables or
cascades into games and analytics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bracketballer_data.paths import REPO_ROOT

import psycopg2

from bracketballer_data.dataset_release import checksum_files, validate_row_bounds
from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.import_audit import (
    begin_import_run,
    mark_failed,
    mark_validated,
    pipeline_commit,
)


PLAYER_SOURCE_TABLE_SQL = """
CREATE TEMP TABLE core_players_source (
    season INTEGER, season_label TEXT, team_id INTEGER, team TEXT,
    conference TEXT, athlete_id INTEGER, athlete_source_id BIGINT, name TEXT,
    position TEXT, games INTEGER, starts INTEGER, minutes INTEGER,
    points INTEGER, turnovers INTEGER, fouls INTEGER, assists INTEGER,
    steals INTEGER, blocks INTEGER, usage DOUBLE PRECISION,
    offensive_rating DOUBLE PRECISION, porpag DOUBLE PRECISION,
    effective_field_goal_pct DOUBLE PRECISION,
    true_shooting_pct DOUBLE PRECISION,
    assists_turnover_ratio DOUBLE PRECISION,
    free_throw_rate DOUBLE PRECISION,
    offensive_rebound_pct DOUBLE PRECISION,
    field_goals_pct DOUBLE PRECISION, field_goals_attempted INTEGER,
    field_goals_made INTEGER, two_point_field_goals_pct DOUBLE PRECISION,
    two_point_field_goals_attempted INTEGER,
    two_point_field_goals_made INTEGER,
    three_point_field_goals_pct DOUBLE PRECISION,
    three_point_field_goals_attempted INTEGER,
    three_point_field_goals_made INTEGER,
    free_throws_pct DOUBLE PRECISION, free_throws_attempted INTEGER,
    free_throws_made INTEGER, rebounds_total INTEGER,
    rebounds_defensive INTEGER, rebounds_offensive INTEGER,
    win_shares_total_per40 DOUBLE PRECISION,
    win_shares_total DOUBLE PRECISION,
    win_shares_defensive DOUBLE PRECISION,
    win_shares_offensive DOUBLE PRECISION,
    defensive_rating DOUBLE PRECISION, net_rating DOUBLE PRECISION,
    ppg DOUBLE PRECISION, rpg DOUBLE PRECISION, apg DOUBLE PRECISION,
    tiebreaker_score DOUBLE PRECISION
) ON COMMIT PRESERVE ROWS
"""

PLAYER_TARGET_COLUMNS = (
    "id", "athlete_source_id", "name", "school_id", "conference", "season",
    "games", "starts", "minutes", "points", "turnovers", "fouls", "assists",
    "steals", "blocks", "usage", "offensive_rating", "defensive_rating",
    "net_rating", "porpag", "effective_field_goal_pct", "true_shooting_pct",
    "assists_turnover_ratio", "free_throw_rate", "offensive_rebound_pct",
    "field_goals_pct", "field_goals_attempted", "field_goals_made",
    "two_point_field_goals_pct", "two_point_field_goals_attempted",
    "two_point_field_goals_made", "three_point_field_goals_pct",
    "three_point_field_goals_attempted", "three_point_field_goals_made",
    "free_throws_pct", "free_throws_attempted", "free_throws_made",
    "rebounds_total", "rebounds_defensive", "rebounds_offensive",
    "win_shares_total_per40", "win_shares_total", "win_shares_defensive",
    "win_shares_offensive", "ppg", "rpg", "apg", "tiebreaker_score",
)

PLAYER_SOURCE_EXPRESSIONS = (
    "athlete_id", "athlete_source_id", "name", "team_id", "conference", "season",
    "games", "starts", "minutes", "points", "turnovers", "fouls", "assists",
    "steals", "blocks", "usage", "offensive_rating", "defensive_rating",
    "net_rating", "porpag", "effective_field_goal_pct", "true_shooting_pct",
    "assists_turnover_ratio", "free_throw_rate", "offensive_rebound_pct",
    "field_goals_pct", "field_goals_attempted", "field_goals_made",
    "two_point_field_goals_pct", "two_point_field_goals_attempted",
    "two_point_field_goals_made", "three_point_field_goals_pct",
    "three_point_field_goals_attempted", "three_point_field_goals_made",
    "free_throws_pct", "free_throws_attempted", "free_throws_made",
    "rebounds_total", "rebounds_defensive", "rebounds_offensive",
    "win_shares_total_per40", "win_shares_total", "win_shares_defensive",
    "win_shares_offensive", "ppg", "rpg", "apg", "tiebreaker_score",
)

REQUIRED_PLAYER_COLUMNS = (
    "season", "team_id", "conference", "athlete_id", "name", "games", "starts",
    "minutes", "points", "turnovers", "fouls", "assists", "steals", "blocks",
    "field_goals_attempted", "field_goals_made",
    "two_point_field_goals_attempted", "two_point_field_goals_made",
    "three_point_field_goals_attempted", "three_point_field_goals_made",
    "free_throws_attempted", "free_throws_made", "rebounds_total",
    "rebounds_defensive", "rebounds_offensive", "ppg", "rpg", "apg",
    "tiebreaker_score",
)


def stage_csvs(conn: Any, schools: Path, players: Path, positions: Path) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE core_schools_source (id INTEGER, name TEXT) ON COMMIT PRESERVE ROWS"
        )
        cursor.execute(PLAYER_SOURCE_TABLE_SQL)
        cursor.execute(
            """
            CREATE TEMP TABLE core_positions_source (
                player_id INTEGER, position TEXT
            ) ON COMMIT PRESERVE ROWS
            """
        )
        for table, path in (
            ("core_schools_source", schools),
            ("core_players_source", players),
            ("core_positions_source", positions),
        ):
            with path.open("r", encoding="utf-8", newline="") as source:
                cursor.copy_expert(
                    f"COPY {table} FROM STDIN WITH (FORMAT csv, HEADER true)",
                    source,
                )
    conn.commit()


def validate_stage(
    conn: Any,
    expected_first_season: int | None,
    expected_last_season: int | None,
) -> tuple[dict[str, int], dict[str, Any]]:
    required_nulls = " OR ".join(f"{column} IS NULL" for column in REQUIRED_PLAYER_COLUMNS)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                (SELECT COUNT(*)::int FROM core_schools_source),
                (SELECT COUNT(*)::int FROM core_players_source),
                (SELECT COUNT(*)::int FROM core_positions_source),
                (SELECT COUNT(*)::int FROM (
                    SELECT id FROM core_schools_source GROUP BY id HAVING COUNT(*) > 1
                ) duplicate_schools),
                (SELECT COUNT(*)::int FROM (
                    SELECT athlete_id FROM core_players_source
                    GROUP BY athlete_id HAVING COUNT(*) > 1
                ) duplicate_players),
                (SELECT COUNT(*)::int FROM (
                    SELECT player_id, position FROM core_positions_source
                    GROUP BY player_id, position HAVING COUNT(*) > 1
                ) duplicate_positions),
                (SELECT COUNT(*)::int FROM core_players_source p
                 LEFT JOIN core_schools_source s ON s.id = p.team_id
                 WHERE s.id IS NULL),
                (SELECT COUNT(*)::int FROM core_positions_source position
                 LEFT JOIN core_players_source p ON p.athlete_id = position.player_id
                 WHERE p.athlete_id IS NULL),
                (SELECT COUNT(*)::int FROM core_positions_source
                 WHERE position NOT IN (
                    SELECT enumlabel FROM pg_enum
                    WHERE enumtypid = 'player_position'::regtype
                 )),
                (SELECT COUNT(*)::int FROM core_players_source
                 WHERE {required_nulls}),
                (SELECT MIN(season)::int FROM core_players_source),
                (SELECT MAX(season)::int FROM core_players_source),
                (SELECT COUNT(*)::int FROM schools WHERE source_active),
                (SELECT COUNT(*)::int FROM players WHERE source_active)
            """
        )
        (
            school_count, player_count, position_count, duplicate_schools,
            duplicate_players, duplicate_positions, missing_schools,
            missing_players, invalid_positions, required_null_rows,
            first_season, last_season, active_schools, active_players,
        ) = cursor.fetchone()

    failures = {
        "duplicate_schools": duplicate_schools,
        "duplicate_players": duplicate_players,
        "duplicate_positions": duplicate_positions,
        "players_missing_schools": missing_schools,
        "positions_missing_players": missing_players,
        "invalid_positions": invalid_positions,
        "players_missing_required_values": required_null_rows,
    }
    if any(failures.values()):
        raise ValueError(f"Core dataset integrity checks failed: {failures}")
    if expected_first_season is not None and first_season != expected_first_season:
        raise ValueError(f"Expected first season {expected_first_season}, got {first_season}")
    if expected_last_season is not None and last_season != expected_last_season:
        raise ValueError(f"Expected last season {expected_last_season}, got {last_season}")

    counts = {
        "schools": school_count,
        "players": player_count,
        "player_position_maps": position_count,
    }
    validations = {
        **failures,
        "first_season": first_season,
        "last_season": last_season,
        "school_row_bounds": validate_row_bounds("schools", school_count, active_schools),
        "player_row_bounds": validate_row_bounds("players", player_count, active_players),
    }
    return counts, validations


def publish(conn: Any, run_id: int, counts: dict[str, int]) -> None:
    target_columns = ", ".join(PLAYER_TARGET_COLUMNS)
    source_columns = ", ".join(PLAYER_SOURCE_EXPRESSIONS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in PLAYER_TARGET_COLUMNS
        if column != "id"
    )
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO schools (id, name, source_active, source_updated_at)
            SELECT id, name, TRUE, now() FROM core_schools_source
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                source_active = TRUE,
                source_updated_at = now()
            """
        )
        cursor.execute(
            """
            UPDATE schools school
            SET source_active = FALSE, source_updated_at = now()
            WHERE school.source_active
              AND NOT EXISTS (
                  SELECT 1 FROM core_schools_source source WHERE source.id = school.id
              )
            """
        )
        cursor.execute(
            f"""
            INSERT INTO players (
                {target_columns}, is_fantasy_eligible, source_active,
                source_updated_at
            )
            SELECT {source_columns}, TRUE, TRUE, now()
            FROM core_players_source
            ON CONFLICT (id) DO UPDATE SET
                {updates},
                is_fantasy_eligible = TRUE,
                source_active = TRUE,
                source_updated_at = now()
            """
        )
        cursor.execute(
            """
            UPDATE players player
            SET is_fantasy_eligible = FALSE,
                source_active = EXISTS (
                    SELECT 1
                    FROM team_roster_memberships membership
                    WHERE membership.player_id = player.id
                      AND membership.source_active
                ),
                source_updated_at = now()
            WHERE player.source_active
              AND NOT EXISTS (
                  SELECT 1 FROM core_players_source source
                  WHERE source.athlete_id = player.id
              )
            """
        )
        cursor.execute(
            """
            DELETE FROM player_position_maps position
            WHERE EXISTS (
                SELECT 1 FROM core_players_source source
                WHERE source.athlete_id = position.player_id
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO player_position_maps (player_id, position)
            SELECT player_id, position::player_position
            FROM core_positions_source
            """
        )
        cursor.execute(
            """
            UPDATE data_import_runs
            SET status = 'published',
                published_row_counts = %s::jsonb,
                published_at = now(),
                finished_at = now()
            WHERE id = %s AND status = 'validated'
            """,
            (json.dumps(counts, sort_keys=True), run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Import run {run_id} is not validated")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schools", type=Path, required=True)
    parser.add_argument("--players", type=Path, required=True)
    parser.add_argument("--positions", type=Path, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--first-season", type=int)
    parser.add_argument("--last-season", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    files = [args.schools.resolve(), args.players.resolve(), args.positions.resolve()]
    for path in files:
        if not path.is_file():
            parser.error(f"Missing input file: {path}")

    load_env_file()
    commit = pipeline_commit(REPO_ROOT)
    checksum = checksum_files(files)
    conn = psycopg2.connect(connection_dsn())
    conn.autocommit = False
    run_id: int | None = None
    try:
        stage_csvs(conn, *files)
        counts, validations = validate_stage(
            conn, args.first_season, args.last_season
        )
        print(json.dumps({"counts": counts, "validations": validations}, indent=2))
        if not args.apply:
            conn.rollback()
            print("DRY RUN: validation passed; no production rows changed")
            return

        run_id = begin_import_run(
            conn,
            dataset="core_sports_dataset",
            import_version=args.release_version,
            commit=commit,
            source_uri="files://schools+players+positions",
            first_season=validations["first_season"],
            last_season=validations["last_season"],
            metadata={"files": [path.name for path in files]},
        )
        mark_validated(
            conn,
            run_id,
            source_sha256=checksum,
            staged_row_counts=counts,
            validation_results=validations,
        )
        publish(conn, run_id, counts)
        print(f"Published core dataset release {args.release_version} (run {run_id})")
    except Exception as error:
        if run_id is not None:
            mark_failed(conn, run_id, error)
        else:
            conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
