"""Bulk-ingest raw CBBD shooting plays by game date.

This is the preferred historical backfill path. It discovers actual game dates
with bounded Games API requests, fetches all shooting plays once per date, and
filters them to the union of the legacy fantasy population and eligible lineup
roster player-seasons. Date responses can be downloaded and verified before a
separate local apply. Each pending player-season is reconciled atomically after
every date has succeeded, so a failed API request cannot expose partial rows.

Examples:
    python -m scripts.ingest.ingest_cbbd_shots_bulk --first 2024 --last 2026 \
        --release-version issue-0006-shots-2026.1 --download
    python -m scripts.ingest.ingest_cbbd_shots_bulk --first 2024 --last 2026 \
        --release-version issue-0006-shots-2026.1 --apply --skip-score
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import cbbd
import psycopg2
from cbbd.rest import ApiException
from psycopg2.extras import Json, execute_values

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.import_audit import (
    begin_import_run,
    mark_failed,
    mark_validated,
    pipeline_commit,
)
from bracketballer_data.shooting_data import (
    EVENT_DB_COLUMNS,
    event_record,
    normalize_eligible_plays,
)
from bracketballer_data.paths import REPO_ROOT
from scripts.compute.compute_shooter_signals import recompute_shooter_profiles
from scripts.ingest.ingest_cbbd_shots import export_events, infer_export_format, model_payload


FIRST_SEASON = 2020
LAST_SEASON = 2026
DEFAULT_WORKERS = 2
DEFAULT_RETRIES = 4
GAME_CALENDAR_WINDOW_DAYS = 14
STAGE_TABLE = "player_shot_events_bulk_stage"
BUNDLE_FORMAT_VERSION = 1
DEFAULT_BUNDLE_ROOT = REPO_ROOT / "data" / "raw" / "cbbd_shots"

_thread_local = threading.local()


def _apis(access_token: str) -> tuple[Any, Any]:
    games_api = getattr(_thread_local, "games_api", None)
    plays_api = getattr(_thread_local, "plays_api", None)
    if games_api is None or plays_api is None:
        configuration = cbbd.Configuration(access_token=access_token)
        client = cbbd.ApiClient(configuration)
        _thread_local.api_client = client
        games_api = cbbd.GamesApi(client)
        plays_api = cbbd.PlaysApi(client)
        _thread_local.games_api = games_api
        _thread_local.plays_api = plays_api
    return games_api, plays_api


def _retryable(error: Exception) -> bool:
    if not isinstance(error, ApiException):
        return True
    if "monthly call quota exceeded" in str(error).lower():
        return False
    status = getattr(error, "status", None)
    return status == 429 or status is None or status >= 500


def call_with_retries(call: Callable[[], Any], max_retries: int) -> Any:
    for attempt in range(1, max_retries + 1):
        try:
            return call()
        except Exception as error:
            if attempt == max_retries or not _retryable(error):
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1)) + random.random())
    raise AssertionError("retry loop exited unexpectedly")


def fetch_game_dates(
    access_token: str, season: int, max_retries: int
) -> list[date]:
    """Discover all game dates in bounded windows to avoid CBBD's 3,000-row cap."""
    games_api, _ = _apis(access_token)
    season_start = datetime(season - 1, 10, 1, tzinfo=timezone.utc)
    season_end = datetime(season, 5, 15, tzinfo=timezone.utc)
    game_dates: set[date] = set()
    window_start = season_start
    while window_start < season_end:
        window_end = min(
            season_end,
            window_start + timedelta(days=GAME_CALENDAR_WINDOW_DAYS),
        )
        games = call_with_retries(
            lambda start=window_start, end=window_end: games_api.get_games(
                season=season,
                start_date_range=start,
                end_date_range=end,
                _request_timeout=(10, 120),
            ),
            max_retries,
        )
        game_dates.update(game.start_date.date() for game in games or [])
        window_start = window_end
    return sorted(game_dates)


def bundle_directory(
    first: int, last: int, release_version: str, source_dir: Path | None = None
) -> Path:
    root = source_dir or DEFAULT_BUNDLE_ROOT / f"{first}-{last}"
    return root / release_version


def _bundle_manifest_path(source_dir: Path) -> Path:
    return source_dir / "manifest.json"


def _read_bundle_manifest(source_dir: Path) -> dict[str, Any]:
    path = _bundle_manifest_path(source_dir)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid shot bundle manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"shot bundle manifest must be an object: {path}")
    return value


def _write_bundle_manifest(source_dir: Path, manifest: dict[str, Any]) -> None:
    temporary = source_dir / ".manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, _bundle_manifest_path(source_dir))


def _write_bundle_date(source_dir: Path, season: int, game_date: date, plays: list[dict[str, Any]]) -> tuple[str, str]:
    season_dir = source_dir / str(season)
    season_dir.mkdir(parents=True, exist_ok=True)
    destination = season_dir / f"{game_date.isoformat()}.json.gz"
    temporary = destination.with_name(f".{destination.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as output:
        json.dump(plays, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
    os.replace(temporary, destination)
    return str(destination.relative_to(source_dir)), sha256_file(destination)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_bundle(
    access_token: str,
    first: int,
    last: int,
    workers: int,
    max_retries: int,
    source_dir: Path,
    release_version: str,
) -> dict[str, Any]:
    """Download and resume complete date responses without touching PostgreSQL."""
    source_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_bundle_manifest(source_dir)
    if manifest and (
        manifest.get("format_version") != BUNDLE_FORMAT_VERSION
        or manifest.get("release_version") != release_version
        or manifest.get("first_season") != first
        or manifest.get("last_season") != last
    ):
        raise ValueError("shot bundle manifest does not match requested release")
    manifest = {
        "format_version": BUNDLE_FORMAT_VERSION,
        "release_version": release_version,
        "first_season": first,
        "last_season": last,
        "seasons": manifest.get("seasons", {}),
    }
    for season in range(first, last + 1):
        game_dates = fetch_game_dates(access_token, season, max_retries)
        season_manifest = manifest["seasons"].setdefault(str(season), {"dates": {}})
        season_manifest["dates"] = season_manifest.get("dates", {})
        season_manifest["game_dates"] = [game_date.isoformat() for game_date in game_dates]
        pending_dates: list[date] = []
        for game_date in game_dates:
            entry = season_manifest["dates"].get(game_date.isoformat())
            if not isinstance(entry, dict):
                pending_dates.append(game_date)
                continue
            relative = source_dir / str(entry.get("file", ""))
            if (
                not relative.is_file()
                or entry.get("sha256") != sha256_file(relative)
            ):
                pending_dates.append(game_date)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    fetch_date_raw_plays,
                    access_token,
                    game_date,
                    season,
                    max_retries,
                ): game_date
                for game_date in pending_dates
            }
            for future in as_completed(futures):
                game_date = futures[future]
                plays = future.result()
                relative, checksum = _write_bundle_date(
                    source_dir, season, game_date, plays
                )
                season_manifest["dates"][game_date.isoformat()] = {
                    "file": relative,
                    "sha256": checksum,
                    "plays": len(plays),
                }
                _write_bundle_manifest(source_dir, manifest)
                print(
                    f"{season}: downloaded {game_date} "
                    f"({len(plays):,} shooting plays)"
                )
        _write_bundle_manifest(source_dir, manifest)
    return manifest


def _bundle_season_entries(
    source_dir: Path, first: int, last: int
) -> dict[int, list[tuple[date, Path]]]:
    """Validate bundle metadata/checksums without loading raw plays."""
    manifest = _read_bundle_manifest(source_dir)
    if (
        manifest.get("format_version") != BUNDLE_FORMAT_VERSION
        or manifest.get("first_season") != first
        or manifest.get("last_season") != last
    ):
        raise ValueError("shot bundle is missing or has incompatible metadata")
    result: dict[int, list[tuple[date, Path]]] = {}
    for season in range(first, last + 1):
        season_manifest = manifest.get("seasons", {}).get(str(season))
        if not isinstance(season_manifest, dict):
            raise ValueError(f"shot bundle is missing season {season}")
        game_dates = season_manifest.get("game_dates")
        dates = season_manifest.get("dates")
        if not isinstance(game_dates, list) or not isinstance(dates, dict):
            raise ValueError(f"shot bundle season {season} is incomplete")
        season_rows: list[tuple[date, Path]] = []
        for date_text in game_dates:
            entry = dates.get(date_text)
            if not isinstance(entry, dict):
                raise ValueError(f"shot bundle is missing {season} {date_text}")
            path = source_dir / str(entry.get("file", ""))
            if not path.is_file() or sha256_file(path) != entry.get("sha256"):
                raise ValueError(f"shot bundle checksum failed for {path}")
            season_rows.append((date.fromisoformat(str(date_text)), path))
        result[season] = season_rows
    return result


def iter_bundle_dates(
    source_dir: Path, season: int, entries: list[tuple[date, Path]]
):
    """Yield one verified raw date response at a time."""
    for game_date, path in entries:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as source:
                plays = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read shot bundle file {path}: {error}") from error
        if not isinstance(plays, list):
            raise ValueError(f"shot bundle file must contain a list: {path}")
        yield game_date, plays


def load_bundle_dates(
    source_dir: Path, first: int, last: int
) -> dict[int, list[tuple[date, list[dict[str, Any]]]]]:
    """Load a bundle eagerly for small callers/tests; ingestion streams it."""
    entries = _bundle_season_entries(source_dir, first, last)
    return {
        season: list(iter_bundle_dates(source_dir, season, season_entries))
        for season, season_entries in entries.items()
    }


def fetch_date_plays(
    access_token: str,
    game_date: date,
    eligible_player_ids: set[int],
    season: int,
    max_retries: int,
) -> list[dict[str, Any]]:
    _, plays_api = _apis(access_token)
    query_date = datetime.combine(game_date, datetime_time.min, timezone.utc)
    plays = call_with_retries(
        lambda: plays_api.get_plays_by_date(
            var_date=query_date,
            shooting_plays_only=True,
            _request_timeout=(10, 120),
        ),
        max_retries,
    )
    raw_plays = [model_payload(play) for play in plays or []]
    return normalize_eligible_plays(raw_plays, eligible_player_ids, season)


def fetch_date_raw_plays(
    access_token: str,
    game_date: date,
    season: int,
    max_retries: int,
) -> list[dict[str, Any]]:
    """Fetch a complete date response for the download-first workflow."""
    _, plays_api = _apis(access_token)
    query_date = datetime.combine(game_date, datetime_time.min, timezone.utc)
    plays = call_with_retries(
        lambda: plays_api.get_plays_by_date(
            var_date=query_date,
            shooting_plays_only=True,
            _request_timeout=(10, 120),
        ),
        max_retries,
    )
    return [model_payload(play) for play in plays or []]


ELIGIBLE_PLAYER_SEASONS_SQL = """
WITH legacy AS (
    SELECT p.id AS player_id, p.season
    FROM players p
    WHERE p.season = %s
), lineup AS (
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
    WHERE membership.season = %s
      AND membership.source_active
      AND season_row.minutes >= 100
)
SELECT player_id, season FROM legacy
UNION
SELECT player_id, season FROM lineup
"""


def eligible_player_seasons(conn: Any, season: int) -> set[tuple[int, int]]:
    """Return the legacy population plus eligible lineup-strategy players."""
    with conn.cursor() as cursor:
        cursor.execute(ELIGIBLE_PLAYER_SEASONS_SQL, (season, season))
        return {(int(player_id), int(row_season)) for player_id, row_season in cursor.fetchall()}


def eligible_players(conn: Any, season: int) -> set[int]:
    return {player_id for player_id, _row_season in eligible_player_seasons(conn, season)}


def completed_player_seasons(conn: Any, season: int) -> set[tuple[int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT player_id, season
            FROM player_shooting_ingestion_status
            WHERE season = %s AND status IN ('success', 'no_data')
            """,
            (season,),
        )
        return {(int(player_id), int(row_season)) for player_id, row_season in cursor.fetchall()}


def season_is_complete(conn: Any, season: int) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH eligible AS (
                """ + ELIGIBLE_PLAYER_SEASONS_SQL + """
            )
            SELECT COUNT(*) AS players,
                   COUNT(status.player_id) FILTER (
                       WHERE status.status IN ('success', 'no_data')
                   ) AS completed
            FROM eligible
            LEFT JOIN player_shooting_ingestion_status status
              ON status.player_id = eligible.player_id
             AND status.season = eligible.season
            """,
            (season, season),
        )
        players, completed = cursor.fetchone()
        return players > 0 and players == completed


def prepare_stage(conn: Any) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TEMP TABLE IF NOT EXISTS {STAGE_TABLE}
                (LIKE player_shot_events INCLUDING ALL)
                ON COMMIT PRESERVE ROWS
            """
        )
        cursor.execute(f"TRUNCATE {STAGE_TABLE}")
    conn.commit()


def stage_events(conn: Any, events: list[dict[str, Any]]) -> int:
    if not events:
        return 0
    columns = ", ".join(EVENT_DB_COLUMNS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in EVENT_DB_COLUMNS
        if column != "source_play_id"
    )
    raw_index = EVENT_DB_COLUMNS.index("raw_payload")
    records = []
    for event in events:
        values = list(event_record(event))
        values[raw_index] = Json(event["raw_payload"])
        records.append(tuple(values))
    with conn.cursor() as cursor:
        execute_values(
            cursor,
            f"""
            INSERT INTO {STAGE_TABLE} ({columns}) VALUES %s
            ON CONFLICT (source_play_id) DO UPDATE SET {updates}
            """,
            records,
            page_size=500,
        )
    conn.commit()
    return len(records)


def stage_checksum(
    conn: Any, season: int, target_player_ids: set[int]
) -> str:
    digest = hashlib.sha256()
    for player_id in sorted(target_player_ids):
        digest.update(f"target:{player_id}:{season}\n".encode())
    with conn.cursor(name=f"shot_stage_checksum_{season}") as cursor:
        cursor.itersize = 1000
        cursor.execute(
            f"""
            SELECT source_play_id, raw_payload::text
            FROM {STAGE_TABLE}
            WHERE season = %s
            ORDER BY source_play_id
            """,
            (season,),
        )
        for source_play_id, raw_payload in cursor:
            digest.update(str(source_play_id).encode())
            digest.update(b"\0")
            digest.update(raw_payload.encode())
            digest.update(b"\n")
    return digest.hexdigest()


def validate_stage(
    conn: Any,
    season: int,
    target_player_ids: set[int],
    eligible_player_ids: set[int],
) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                COUNT(*)::int,
                COUNT(DISTINCT stage.source_play_id)::int,
                COUNT(*) FILTER (WHERE stage.season <> %s)::int,
                COUNT(*) FILTER (WHERE player.id IS NULL)::int,
                COUNT(*) FILTER (
                    WHERE stage.player_id <> ALL(%s::int[])
                )::int
            FROM {STAGE_TABLE} stage
            LEFT JOIN players player
              ON player.id = stage.player_id
            WHERE stage.season = %s OR stage.season <> %s
            """,
            (season, list(target_player_ids), season, season),
        )
        staged, unique_ids, wrong_season, orphan_players, outside_targets = cursor.fetchone()
    results = {
        "staged_rows": staged,
        "unique_source_play_ids": unique_ids,
        "wrong_season_rows": wrong_season,
        "orphan_player_rows": orphan_players,
        "outside_target_rows": outside_targets,
        "target_player_seasons": len(target_player_ids),
        "eligible_player_seasons": len(eligible_player_ids),
    }
    if unique_ids != staged or wrong_season or orphan_players or outside_targets:
        raise ValueError(f"Season {season} failed stage integrity checks: {results}")
    return results


def publish_season(
    conn: Any,
    season: int,
    run_id: int,
    target_player_ids: set[int],
) -> int:
    """Reconcile pending player-seasons without touching unrelated seasons."""
    if not target_player_ids:
        raise ValueError(f"Season {season} has no target player-seasons")
    columns = ", ".join(EVENT_DB_COLUMNS)
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in EVENT_DB_COLUMNS
        if column != "source_play_id"
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM player_shot_events
                WHERE season = %s AND player_id = ANY(%s::int[])
                """,
                (season, list(target_player_ids)),
            )
            cursor.execute(
                f"""
                INSERT INTO player_shot_events ({columns})
                SELECT {columns} FROM {STAGE_TABLE}
                WHERE season = %s
                ON CONFLICT (source_play_id) DO UPDATE SET
                    {updates},
                    updated_at = now()
                """,
                (season,),
            )
            cursor.execute(
                f"""
                SELECT
                    COUNT(*)::int,
                    COUNT(DISTINCT player_id)::int
                FROM {STAGE_TABLE}
                WHERE season = %s
                """,
                (season,),
            )
            published, successful = cursor.fetchone()
            no_data = len(target_player_ids) - successful
            cursor.execute(
                f"""
                INSERT INTO player_shooting_ingestion_status
                    (player_id, season, status, event_count, error_message)
                SELECT
                    target.player_id,
                    %s,
                    CASE WHEN COUNT(stage.source_play_id) > 0
                         THEN 'success' ELSE 'no_data' END,
                    COUNT(stage.source_play_id)::INTEGER,
                    NULL
                FROM unnest(%s::int[]) AS target(player_id)
                LEFT JOIN {STAGE_TABLE} stage
                  ON stage.player_id = target.player_id
                 AND stage.season = %s
                GROUP BY target.player_id
                ON CONFLICT (player_id, season) DO UPDATE SET
                    status = EXCLUDED.status,
                    event_count = EXCLUDED.event_count,
                    attempt_count = player_shooting_ingestion_status.attempt_count + 1,
                    error_message = NULL,
                    fetched_at = now()
                """,
                (season, list(target_player_ids), season),
            )
            cursor.execute(
                """
                UPDATE data_import_runs
                SET status = 'published',
                    published_row_counts = jsonb_build_object(
                        'player_shot_events', %s::int,
                        'player_seasons', %s::int,
                        'success', %s::int,
                        'no_data', %s::int
                    ),
                    published_at = now(),
                    finished_at = now()
                WHERE id = %s AND status = 'validated'
                """,
                (published, len(target_player_ids), successful, no_data, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"Import run {run_id} is not validated")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return published


def ingest_season(
    conn: Any,
    access_token: str,
    season: int,
    workers: int,
    max_retries: int,
    run_id: int,
    refresh: bool = False,
    bundle_source_dir: Path | None = None,
    bundle_entries: list[tuple[date, Path]] | None = None,
) -> tuple[int, int]:
    eligible_pairs = eligible_player_seasons(conn, season)
    completed_pairs = completed_player_seasons(conn, season)
    target_pairs = eligible_pairs if refresh else eligible_pairs - completed_pairs
    target_player_ids = {player_id for player_id, _row_season in target_pairs}
    if not target_pairs:
        print(f"{season}: all eligible player-seasons are complete; skipping")
        return 0, 0
    if not target_player_ids:
        print(f"{season}: no database players; skipping")
        return 0, 0
    game_dates = (
        [game_date for game_date, _path in bundle_entries]
        if bundle_entries is not None
        else fetch_game_dates(access_token, season, max_retries)
    )
    if not game_dates:
        raise RuntimeError(f"CBBD returned no games for season {season}")

    prepare_stage(conn)
    staged = 0
    if bundle_entries is not None:
        if bundle_source_dir is None:
            raise ValueError("bundle_source_dir is required with bundle_entries")
        for completed, (game_date, raw_plays) in enumerate(
            iter_bundle_dates(bundle_source_dir, season, bundle_entries), start=1
        ):
            events = normalize_eligible_plays(raw_plays, target_player_ids, season)
            staged += stage_events(conn, events)
            if completed % 20 == 0 or completed == len(game_dates):
                print(
                    f"{season}: dates {completed}/{len(game_dates)}, "
                    f"staged events={staged:,} (latest {game_date})"
                )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            date_iterator = iter(game_dates)
            futures = {}

            def submit_next() -> bool:
                try:
                    game_date = next(date_iterator)
                except StopIteration:
                    return False
                future = executor.submit(
                    fetch_date_plays,
                    access_token,
                    game_date,
                    target_player_ids,
                    season,
                    max_retries,
                )
                futures[future] = game_date
                return True

            for _ in range(workers):
                if not submit_next():
                    break

            completed = 0
            while futures:
                future = next(as_completed(futures))
                game_date = futures.pop(future)
                events = future.result()
                staged += stage_events(conn, events)
                completed += 1
                submit_next()
                if completed % 20 == 0 or completed == len(game_dates):
                    print(
                        f"{season}: dates {completed}/{len(game_dates)}, "
                        f"staged events={staged:,} (latest {game_date})"
                    )

    validation = validate_stage(
        conn,
        season,
        target_player_ids,
        {player_id for player_id, _row_season in eligible_pairs},
    )
    checksum = stage_checksum(conn, season, target_player_ids)
    mark_validated(
        conn,
        run_id,
        source_sha256=checksum,
        staged_row_counts={
            "player_shot_events": validation["staged_rows"],
            "player_seasons": len(target_pairs),
        },
        validation_results=validation,
    )
    published = publish_season(conn, season, run_id, target_player_ids)
    return len(game_dates), published


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--export", type=Path)
    parser.add_argument("--format", choices=("jsonl", "csv"))
    parser.add_argument("--skip-score", action="store_true")
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Local download-first bundle; defaults to data/raw/cbbd_shots/<range>/<release>",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download or resume date responses, then exit without database writes",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply a validated local bundle (or use direct API mode when no bundle is supplied)",
    )
    parser.add_argument(
        "--release-version",
        help="Immutable release prefix; defaults to timestamp plus pipeline commit",
    )
    args = parser.parse_args()
    if args.first > args.last:
        parser.error("--first cannot be greater than --last")
    if args.workers < 1 or args.max_retries < 1:
        parser.error("--workers and --max-retries must be positive")
    if args.download and args.apply:
        parser.error("--download and --apply are separate phases")

    load_env_file()
    commit = pipeline_commit(REPO_ROOT)
    release_version = args.release_version or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + f"-{commit[:12]}"
    )
    source_dir = args.source_dir
    if source_dir is None and args.release_version:
        source_dir = bundle_directory(args.first, args.last, release_version)

    if args.download:
        access_token = os.environ.get("CBBD_API_KEY")
        if not access_token:
            raise SystemExit("Missing CBBD_API_KEY environment variable")
        source_dir = source_dir or bundle_directory(args.first, args.last, release_version)
        manifest = download_bundle(
            access_token,
            args.first,
            args.last,
            args.workers,
            args.max_retries,
            source_dir,
            release_version,
        )
        print(
            json.dumps(
                {
                    "source_dir": str(source_dir),
                    "format_version": manifest["format_version"],
                    "seasons": sorted(manifest["seasons"]),
                },
                indent=2,
            )
        )
        print("DOWNLOAD COMPLETE: no database rows changed")
        return

    bundle_entries = None
    if source_dir is not None:
        bundle_manifest = _read_bundle_manifest(source_dir)
        bundle_release = bundle_manifest.get("release_version")
        if not isinstance(bundle_release, str):
            raise SystemExit("Local shot bundle has no release_version")
        if args.release_version and bundle_release != release_version:
            raise SystemExit(
                f"Local shot bundle release {bundle_release!r} does not match "
                f"requested {release_version!r}"
            )
        if not args.release_version:
            release_version = bundle_release
        bundle_entries = _bundle_season_entries(source_dir, args.first, args.last)
    elif not args.apply:
        raise SystemExit(
            "Use --download followed by --apply with a local bundle, "
            "or pass --apply for direct API mode"
        )

    access_token = os.environ.get("CBBD_API_KEY", "")
    if bundle_entries is None and not access_token:
        raise SystemExit("Missing CBBD_API_KEY environment variable")

    conn = psycopg2.connect(connection_dsn())
    conn.autocommit = False
    try:
        if not args.apply:
            report: dict[str, Any] = {}
            for season in range(args.first, args.last + 1):
                eligible = eligible_player_seasons(conn, season)
                completed = completed_player_seasons(conn, season)
                target = eligible if args.refresh else eligible - completed
                target_ids = {player_id for player_id, _row_season in target}
                staged = 0
                unique_ids: set[int] = set()
                for _game_date, raw_plays in iter_bundle_dates(
                    source_dir, season, bundle_entries[season]
                ):
                    events = normalize_eligible_plays(raw_plays, target_ids, season)
                    staged += len(events)
                    unique_ids.update(event["source_play_id"] for event in events)
                report[str(season)] = {
                    "eligible_player_seasons": len(eligible),
                    "pending_player_seasons": len(target),
                    "staged_events": staged,
                    "unique_source_play_ids": len(unique_ids),
                }
            print(json.dumps({"source_dir": str(source_dir), "validation": report}, indent=2))
            print("DRY RUN: no database rows changed")
            return

        total_dates = total_events = 0
        for season in range(args.first, args.last + 1):
            if not eligible_player_seasons(conn, season):
                print(f"{season}: no eligible player-seasons; skipping")
                continue
            if not args.refresh and season_is_complete(conn, season):
                print(f"{season}: already complete; skipping")
                continue
            run_id = begin_import_run(
                conn,
                dataset="cbbd_player_shot_events",
                import_version=f"{release_version}-s{season}",
                commit=commit,
                source_uri=(
                    f"cbbd-bundle://shots/{args.first}-{args.last}/{release_version}"
                    if bundle_entries is not None
                    else f"cbbd://plays?season={season}"
                ),
                first_season=season,
                last_season=season,
                metadata={
                    "workers": args.workers,
                    "max_retries": args.max_retries,
                    "source_dir": str(source_dir) if source_dir else None,
                },
            )
            try:
                dates, events = ingest_season(
                    conn,
                    access_token,
                    season,
                    args.workers,
                    args.max_retries,
                    run_id,
                    refresh=args.refresh,
                    bundle_source_dir=source_dir if bundle_entries is not None else None,
                    bundle_entries=bundle_entries[season] if bundle_entries is not None else None,
                )
            except Exception as error:
                mark_failed(conn, run_id, error)
                raise
            total_dates += dates
            total_events += events
            print(f"{season}: published {events:,} events from {dates} dates")

        if not args.skip_score:
            profiles = recompute_shooter_profiles(conn, args.first, args.last)
            print(f"Recomputed {profiles:,} shooting profiles")
        if args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True)
            file_format = infer_export_format(args.export, args.format)
            exported = export_events(
                conn, args.export, args.first, args.last, file_format
            )
            print(f"Exported {exported:,} events to {args.export}")
        print(
            f"Done. Published {total_events:,} events from "
            f"{total_dates} CBBD date responses."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
