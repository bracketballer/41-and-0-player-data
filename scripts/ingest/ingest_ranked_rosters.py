"""Download and ingest AP Top 25 + Virginia Tech roster evidence.

Use --download first to create a resumable, release-specific source bundle.
Subsequent dry-run and --apply invocations read only that local bundle and do
not call CBBD. A team remains eligible once it appears in any AP rank 1-25
response; Virginia Tech (CBBD team 340) is always included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

try:  # Optional until a network or database phase is requested.
    import cbbd
    from cbbd.rest import ApiException
    _CBBD_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in lightweight tooling
    class _MissingCbbd:
        class ApiClient:  # pragma: no cover - only a patch target in offline tests
            pass

        class Configuration:
            pass

        class RankingsApi:
            pass

        class TeamsApi:
            pass

        class StatsApi:
            pass

        class GamesApi:
            pass

        class LineupsApi:
            pass

    cbbd = _MissingCbbd()  # type: ignore[assignment]
    _CBBD_AVAILABLE = False

    class ApiException(Exception):
        status = None

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:  # pragma: no cover - pure bundle validation needs no driver
    class _MissingPsycopg:
        @staticmethod
        def connect(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("psycopg2 is required for ranked-roster publication")

    psycopg2 = _MissingPsycopg()  # type: ignore[assignment]

    def Json(value: Any) -> Any:
        return value

    def execute_values(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("psycopg2 is required for ranked-roster publication")

try:
    from pydantic import ValidationError
except ImportError:  # pragma: no cover - optional SDK dependency
    class ValidationError(Exception):
        pass

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.import_audit import (
    begin_import_run,
    mark_failed,
    mark_validated,
    pipeline_commit,
)
from bracketballer_data.ranked_rosters import (
    FIRST_SUPPORTED_SEASON,
    build_eligible_teams,
    normalize_positions,
    validate_ap_top25_coverage,
    validate_roster_coverage,
)
from scripts.ingest.reconcile_torvik_ids import match_all

from bracketballer_data.paths import RAW_RANKED_ROSTERS, REPO_ROOT

ARCHIVE_FORMAT_VERSION = 3
GAME_WINDOW_DAYS = 14
PLAYER_HISTORY_FIRST_SEASON = 2005


def json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "json"):
        try:
            return json.loads(value.json(by_alias=True))
        except TypeError:
            return json.loads(value.json())
    data = value.to_dict() if hasattr(value, "to_dict") else value
    return json.loads(json.dumps(data, default=json_default))


def call_with_retries(call: Callable[[], Any], retries: int) -> Any:
    for attempt in range(1, retries + 1):
        try:
            return call()
        except Exception as error:
            status = getattr(error, "status", None)
            retryable = not isinstance(error, (ApiException, ValidationError)) or (
                isinstance(error, ApiException)
                and (status is None or status == 429 or status >= 500)
            )
            if attempt == retries or not retryable:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1)) + random.random())
    raise AssertionError("retry loop exited")


def source_directory(
    season: int,
    release_version: str,
    override: Path | None = None,
) -> Path:
    """Return the immutable local source directory for one release."""
    if override is not None:
        return override
    if (
        not release_version
        or release_version in {".", ".."}
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in release_version
        )
    ):
        raise ValueError(
            "release-version may contain only letters, numbers, '.', '_', and '-'"
        )
    return RAW_RANKED_ROSTERS / str(season) / release_version


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read cached CBBD response {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    """Atomically publish one cache file so interrupted writes are ignored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.part")
    temporary.write_text(
        json.dumps(value, default=json_default, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def cached_rows(
    path: Path,
    fetch: Callable[[], Any],
    retries: int,
    label: str,
) -> list[dict[str, Any]]:
    """Load an atomic JSON checkpoint or fetch and checkpoint it once."""
    if path.exists():
        rows = read_json(path)
        if not isinstance(rows, list):
            raise ValueError(f"cached CBBD response is not a list: {path}")
        print(f"{label}: using {len(rows)} cached row(s)", flush=True)
        return rows
    response = call_with_retries(fetch, retries)
    rows = [payload(row) for row in response or []]
    write_json(path, rows)
    print(f"{label}: downloaded {len(rows)} row(s) -> {path}", flush=True)
    return rows


def raw_lineup_rows(
    lineups_api: Any,
    game_id: int,
    retries: int,
) -> list[dict[str, Any]]:
    """Fetch lineup JSON without the SDK's overly strict response models."""

    response = call_with_retries(
        lambda: lineups_api.get_lineup_stats_by_game_with_http_info(
            game_id=game_id,
            _preload_content=False,
            _request_timeout=(10, 120),
        ),
        retries,
    )
    try:
        raw = response.raw_data
        if hasattr(raw, "read"):
            raw = raw.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        rows = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"lineups for game {game_id} are not valid JSON") from error
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"lineups for game {game_id} are not a JSON row list")
    return rows


def supplement_rosters_from_lineups(
    rosters: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
) -> int:
    """Add evidence-backed season memberships absent from the roster snapshot."""

    rosters_by_team = {int(roster["teamId"]): roster for roster in rosters}
    memberships = {
        (int(roster["teamId"]), int(player["id"]))
        for roster in rosters
        for player in roster.get("players", [])
    }
    inferred = 0
    for lineup in lineups:
        team_id = int(lineup["teamId"])
        roster = rosters_by_team.get(team_id)
        if roster is None:
            continue
        for athlete in lineup.get("athletes") or []:
            membership = (team_id, int(athlete["id"]))
            if membership in memberships:
                continue
            roster.setdefault("players", []).append(
                {
                    "id": int(athlete["id"]),
                    "name": athlete["name"],
                    "sourceId": None,
                    "_membershipEvidence": "lineup",
                }
            )
            memberships.add(membership)
            inferred += 1
    return inferred


def lineup_reconciliation_failure(
    game: dict[str, Any],
    team_id: int,
    lineups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    expected_points = (
        game.get("homePoints")
        if int(game["homeTeamId"]) == team_id
        else game.get("awayPoints")
    )
    seconds = sum(float(lineup.get("totalSeconds") or 0) for lineup in lineups)
    points = sum(
        int((lineup.get("teamStats") or {}).get("points") or 0)
        for lineup in lineups
    )
    if seconds < 2300 or seconds > 4000 or (
        expected_points is not None and points != int(expected_points)
    ):
        return {
            "game_id": int(game["id"]),
            "team_id": team_id,
            "reason": "failed_reconciliation",
            "lineup_seconds": seconds,
            "lineup_points": points,
            "game_points": expected_points,
        }
    return None


def select_reconciled_lineups(
    games: list[dict[str, Any]],
    lineups: list[dict[str, Any]],
    eligible_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep reliable lineup evidence and audit unavailable team/game pairs."""

    games_by_id = {int(game["id"]): game for game in games}
    expected_pairs = {
        (int(game["id"]), team_id)
        for game in games
        if str(game.get("status")) == "final"
        for team_id in (int(game["homeTeamId"]), int(game["awayTeamId"]))
        if team_id in eligible_ids
    }
    lineups_by_pair: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for lineup in lineups:
        pair = (int(lineup["_gameId"]), int(lineup["teamId"]))
        lineups_by_pair.setdefault(pair, []).append(lineup)

    unavailable = [
        {
            "game_id": game_id,
            "team_id": team_id,
            "reason": "missing_source_rows",
        }
        for game_id, team_id in sorted(expected_pairs - set(lineups_by_pair))
    ]
    rejected_pairs: set[tuple[int, int]] = set()
    for (game_id, team_id), rows in sorted(lineups_by_pair.items()):
        failure = lineup_reconciliation_failure(
            games_by_id[game_id],
            team_id,
            rows,
        )
        if failure is not None:
            unavailable.append(failure)
            rejected_pairs.add((game_id, team_id))

    selected = [
        lineup
        for lineup in lineups
        if (int(lineup["_gameId"]), int(lineup["teamId"])) not in rejected_pairs
    ]
    return selected, unavailable


def fetch_games(
    games_api: Any,
    season: int,
    retries: int,
    source_dir: Path,
) -> list[dict[str, Any]]:
    start = datetime(season - 1, 10, 1, tzinfo=timezone.utc)
    end = datetime(season, 5, 15, tzinfo=timezone.utc)
    games: dict[int, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        window_end = min(end, cursor + timedelta(days=GAME_WINDOW_DAYS))
        cache_path = (
            source_dir
            / "games"
            / f"{cursor.date().isoformat()}_{window_end.date().isoformat()}.json"
        )
        rows = cached_rows(
            cache_path,
            lambda left=cursor, right=window_end: games_api.get_games(
                season=season,
                start_date_range=left,
                end_date_range=right,
                _request_timeout=(10, 120),
            ),
            retries,
            f"games {cursor.date()} to {window_end.date()}",
        )
        for item in rows:
            games[int(item["id"])] = item
        cursor = window_end
    return sorted(games.values(), key=lambda row: int(row["id"]))


def fetch_candidate(
    access_token: str,
    season: int,
    retries: int,
    source_dir: Path,
    release_version: str,
) -> dict[str, Any]:
    if not _CBBD_AVAILABLE:
        raise RuntimeError("cbbd is required for --download; install requirements.txt")
    manifest_path = source_dir / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if (
            isinstance(manifest, dict)
            and manifest.get("format_version") == ARCHIVE_FORMAT_VERSION
        ):
            print(f"download: using complete source bundle {source_dir}", flush=True)
            return load_candidate_bundle(source_dir, season, release_version)
        print(
            f"download: upgrading source bundle to format "
            f"{ARCHIVE_FORMAT_VERSION}; valid checkpoints will be reused",
            flush=True,
        )

    configuration = cbbd.Configuration(access_token=access_token)
    with cbbd.ApiClient(configuration) as client:
        rankings_api = cbbd.RankingsApi(client)
        teams_api = cbbd.TeamsApi(client)
        stats_api = cbbd.StatsApi(client)
        games_api = cbbd.GamesApi(client)
        lineups_api = cbbd.LineupsApi(client)

        rankings = cached_rows(
            source_dir / "rankings.json",
            lambda: rankings_api.get_rankings(
                season=season,
                poll_type="ap",
                _request_timeout=(10, 120),
            ),
            retries,
            "rankings",
        )
        eligible = build_eligible_teams(rankings, season)
        try:
            validate_ap_top25_coverage(eligible)
        except ValueError as error:
            observed_poll_types = sorted(
                {
                    str(row.get("pollType", row.get("poll_type", "")))
                    for row in rankings
                }
            )
            raise ValueError(
                f"{error}; ranking_rows={len(rankings)}, "
                f"observed_poll_types={observed_poll_types}"
            ) from error
        eligible_ids = {row.team_id for row in eligible}

        roster_responses = cached_rows(
            source_dir / "rosters.json",
            lambda: teams_api.get_team_roster(
                season=season,
                _request_timeout=(10, 120),
            ),
            retries,
            "rosters",
        )
        rosters = [
            row
            for row in roster_responses
            if int(row["teamId"]) in eligible_ids
        ]
        roster_player_ids = {
            int(player["id"])
            for roster in rosters
            for player in roster.get("players", [])
        }

        player_seasons: list[dict[str, Any]] = []
        # Athlete IDs are stable across transfers. Keep the eligible roster
        # narrow while retaining each selected athlete's complete CBBD history.
        # Historical rows are priors only; lineup/scheme evidence still starts
        # with the 2024 season.
        for candidate_season in range(PLAYER_HISTORY_FIRST_SEASON, season + 1):
            cache_path = source_dir / "player_seasons" / f"{candidate_season}.json"
            if cache_path.exists():
                selected_rows = read_json(cache_path)
                if not isinstance(selected_rows, list):
                    raise ValueError(
                        f"cached player-season response is not a list: {cache_path}"
                    )
                print(
                    f"player seasons {candidate_season}: using "
                    f"{len(selected_rows)} cached row(s)",
                    flush=True,
                )
            else:
                rows = call_with_retries(
                    lambda selected=candidate_season: stats_api.get_player_season_stats(
                        season=selected,
                        _request_timeout=(10, 180),
                    ),
                    retries,
                )
                selected_rows = [
                    item
                    for item in (payload(row) for row in rows or [])
                    if int(item["athleteId"]) in roster_player_ids
                ]
                write_json(cache_path, selected_rows)
                print(
                    f"player seasons {candidate_season}: downloaded "
                    f"{len(selected_rows)} eligible row(s) -> {cache_path}",
                    flush=True,
                )
            player_seasons.extend(
                item
                for item in selected_rows
                if int(item["athleteId"]) in roster_player_ids
            )

        games = [
            game
            for game in fetch_games(games_api, season, retries, source_dir)
            if int(game["homeTeamId"]) in eligible_ids
            or int(game["awayTeamId"]) in eligible_ids
        ]
        final_games = [
            game for game in games if str(game.get("status")) == "final"
        ]
        lineups: list[dict[str, Any]] = []
        skipped_game_ids: list[int] = []
        for index, game in enumerate(final_games, start=1):
            game_id = int(game["id"])
            participant_ids = {
                int(game["homeTeamId"]),
                int(game["awayTeamId"]),
            }
            cache_path = source_dir / "lineups" / f"{game_id}.json"
            if cache_path.exists():
                cached = read_json(cache_path)
                if not isinstance(cached, dict):
                    raise ValueError(f"cached lineup response is invalid: {cache_path}")
            if (
                not cache_path.exists()
                or cached.get("status") == "skipped_validation_error"
            ):
                cached = {
                    "status": "ok",
                    "transport": "raw_json",
                    "rows": raw_lineup_rows(lineups_api, game_id, retries),
                }
                write_json(cache_path, cached)
            if cached.get("status") != "ok" or not isinstance(
                cached.get("rows"), list
            ):
                raise ValueError(f"cached lineup response is invalid: {cache_path}")
            if not cached["rows"]:
                skipped_game_ids.append(game_id)
            else:
                for item in cached["rows"]:
                    if (
                        int(item["teamId"]) in eligible_ids
                        and int(item["teamId"]) in participant_ids
                    ):
                        item["_gameId"] = game_id
                        item["_season"] = season
                        lineups.append(item)
            if index % 25 == 0 or index == len(final_games):
                print(
                    f"lineups: checkpointed {index}/{len(final_games)} final games",
                    flush=True,
                )
        if skipped_game_ids:
            print(
                f"lineups: CBBD returned no lineup rows for "
                f"{len(skipped_game_ids)} game(s): {skipped_game_ids}"
            )

        lineups, unavailable_lineup_game_teams = select_reconciled_lineups(
            games,
            lineups,
            eligible_ids,
        )
        if unavailable_lineup_game_teams:
            reason_counts: dict[str, int] = {}
            for row in unavailable_lineup_game_teams:
                reason = str(row["reason"])
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            print(
                "lineups: excluded unavailable team/game evidence "
                f"{reason_counts}",
                flush=True,
            )

        inferred_roster_players = supplement_rosters_from_lineups(rosters, lineups)
        if inferred_roster_players:
            print(
                f"rosters: added {inferred_roster_players} player-team membership(s) "
                "from lineup evidence",
                flush=True,
            )

        opponent_ids: set[int] = set()
        for game in games:
            home_team_id = int(game["homeTeamId"])
            away_team_id = int(game["awayTeamId"])
            if home_team_id in eligible_ids:
                opponent_ids.add(away_team_id)
            if away_team_id in eligible_ids:
                opponent_ids.add(home_team_id)
        team_stats = cached_rows(
            source_dir / "team_stats.json",
            lambda: stats_api.get_team_season_stats(
                season=season,
                _request_timeout=(10, 180),
            ),
            retries,
            "team stats",
        )
        opponent_contexts = [
            row
            for row in team_stats
            if int(row["teamId"]) in opponent_ids
        ]
    candidate = {
        "rankings": rankings,
        "eligible": eligible,
        "rosters": rosters,
        "player_seasons": player_seasons,
        "games": games,
        "lineups": lineups,
        "opponent_contexts": opponent_contexts,
        "skipped_lineup_game_ids": skipped_game_ids,
        "unavailable_lineup_game_teams": unavailable_lineup_game_teams,
    }
    write_candidate_bundle(source_dir, candidate, season, release_version)
    return candidate


def validate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    eligible_ids = {row.team_id for row in candidate["eligible"]}
    roster_team_ids = [
        int(roster["teamId"])
        for roster in candidate["rosters"]
        for _player in roster.get("players", [])
    ]
    roster_validation = validate_roster_coverage(
        eligible_ids,
        roster_team_ids,
    )
    roster_memberships = {
        (int(player["id"]), int(roster["teamId"]))
        for roster in candidate["rosters"]
        for player in roster.get("players", [])
    }
    unresolved: list[dict[str, int]] = []
    invalid_lineups: list[dict[str, Any]] = []
    lineups_by_game_team: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for lineup in candidate["lineups"]:
        athletes = lineup.get("athletes") or []
        if len(athletes) != 5:
            invalid_lineups.append(
                {
                    "game_id": lineup["_gameId"],
                    "team_id": lineup["teamId"],
                    "athletes": len(athletes),
                }
            )
        for athlete in athletes:
            key = (int(athlete["id"]), int(lineup["teamId"]))
            if key not in roster_memberships:
                unresolved.append(
                    {
                        "game_id": int(lineup["_gameId"]),
                        "team_id": int(lineup["teamId"]),
                        "player_id": int(athlete["id"]),
                    }
                )
        lineups_by_game_team.setdefault(
            (int(lineup["_gameId"]), int(lineup["teamId"])),
            [],
        ).append(lineup)

    game_by_id = {int(game["id"]): game for game in candidate["games"]}
    expected_game_teams = {
        (int(game["id"]), team_id)
        for game in candidate["games"]
        if str(game.get("status")) == "final"
        for team_id in (int(game["homeTeamId"]), int(game["awayTeamId"]))
        if team_id in eligible_ids
    }
    skipped_lineup_game_ids = set(candidate.get("skipped_lineup_game_ids") or [])
    unavailable_lineup_pairs = {
        (int(row["game_id"]), int(row["team_id"]))
        for row in candidate.get("unavailable_lineup_game_teams") or []
    }
    all_missing_game_lineups = expected_game_teams - set(lineups_by_game_team)
    # CBBD legitimately returns an empty lineup array for a small number of
    # games. Keep those source gaps explicit; only an unexplained missing
    # lineup should hard-fail.
    known_missing_game_lineups = sorted(
        pair
        for pair in all_missing_game_lineups
        if pair[0] in skipped_lineup_game_ids or pair in unavailable_lineup_pairs
    )
    missing_game_lineups = sorted(
        pair
        for pair in all_missing_game_lineups
        if pair[0] not in skipped_lineup_game_ids and pair not in unavailable_lineup_pairs
    )
    reconciliation_failures: list[dict[str, Any]] = []
    for (game_id, team_id), lineups in lineups_by_game_team.items():
        failure = lineup_reconciliation_failure(
            game_by_id[game_id],
            team_id,
            lineups,
        )
        if failure is not None:
            reconciliation_failures.append(failure)
    if (
        unresolved
        or invalid_lineups
        or missing_game_lineups
        or reconciliation_failures
    ):
        raise ValueError(
            "Lineup validation failed: "
            f"unresolved={unresolved[:20]}, "
            f"invalid={invalid_lineups[:20]}, "
            f"missing_game_lineups={missing_game_lineups[:20]}, "
            f"reconciliation={reconciliation_failures[:20]}"
        )
    return {
        **roster_validation,
        "player_seasons": len(candidate["player_seasons"]),
        "college_games": len(candidate["games"]),
        "final_games": sum(
            str(game.get("status")) == "final" for game in candidate["games"]
        ),
        "team_game_lineups": len(candidate["lineups"]),
        "opponent_contexts": len(candidate["opponent_contexts"]),
        "lineup_inferred_roster_players": sum(
            player.get("_membershipEvidence") == "lineup"
            for roster in candidate["rosters"]
            for player in roster.get("players", [])
        ),
        "unresolved_lineup_players": 0,
        "invalid_lineups": 0,
        "missing_game_lineups": 0,
        "reconciliation_failures": 0,
        "known_skipped_game_lineups": len(known_missing_game_lineups),
        "unavailable_lineup_game_teams": len(unavailable_lineup_pairs),
    }


def candidate_checksum(candidate: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    serializable = {
        key: [
            row.__dict__ if hasattr(row, "__dict__") else row
            for row in candidate[key]
        ]
        for key in (
            "rankings",
            "eligible",
            "rosters",
            "player_seasons",
            "games",
            "lineups",
            "opponent_contexts",
            "skipped_lineup_game_ids",
            "unavailable_lineup_game_teams",
        )
    }
    digest.update(
        json.dumps(
            serializable,
            default=json_default,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return digest.hexdigest()


def write_candidate_bundle(
    source_dir: Path,
    candidate: dict[str, Any],
    season: int,
    release_version: str,
) -> None:
    """Publish the assembled candidate and its completion manifest last."""
    candidate_keys = (
        "rankings",
        "rosters",
        "player_seasons",
        "games",
        "lineups",
        "opponent_contexts",
        "skipped_lineup_game_ids",
        "unavailable_lineup_game_teams",
    )
    write_json(
        source_dir / "candidate.json",
        {key: candidate[key] for key in candidate_keys},
    )
    checksum = candidate_checksum(candidate)
    write_json(
        source_dir / "manifest.json",
        {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "status": "complete",
            "season": season,
            "release_version": release_version,
            "candidate_file": "candidate.json",
            "candidate_sha256": checksum,
            "completed_at": datetime.now(timezone.utc),
            "counts": {
                "rankings": len(candidate["rankings"]),
                "eligible_teams": len(candidate["eligible"]),
                "rosters": len(candidate["rosters"]),
                "player_seasons": len(candidate["player_seasons"]),
                "games": len(candidate["games"]),
                "lineups": len(candidate["lineups"]),
                "opponent_contexts": len(candidate["opponent_contexts"]),
                "skipped_lineup_games": len(
                    candidate["skipped_lineup_game_ids"]
                ),
                "unavailable_lineup_game_teams": len(
                    candidate["unavailable_lineup_game_teams"]
                ),
            },
        },
    )
    print(f"download: complete source bundle -> {source_dir}", flush=True)


def load_candidate_bundle(
    source_dir: Path,
    season: int,
    release_version: str,
) -> dict[str, Any]:
    """Load and verify a complete source bundle without calling CBBD."""
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"source bundle is incomplete or missing: {manifest_path}; "
            "run this command with --download first"
        )
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"source bundle manifest is invalid: {manifest_path}")
    expected_manifest = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "status": "complete",
        "season": season,
        "release_version": release_version,
        "candidate_file": "candidate.json",
    }
    for key, expected in expected_manifest.items():
        if manifest.get(key) != expected:
            raise ValueError(
                f"source bundle manifest {key} does not match: "
                f"expected={expected!r}, actual={manifest.get(key)!r}"
            )
    raw_candidate = read_json(source_dir / "candidate.json")
    if not isinstance(raw_candidate, dict):
        raise ValueError("source bundle candidate must be a JSON object")
    required_keys = {
        "rankings",
        "rosters",
        "player_seasons",
        "games",
        "lineups",
        "opponent_contexts",
        "skipped_lineup_game_ids",
        "unavailable_lineup_game_teams",
    }
    missing = sorted(required_keys - set(raw_candidate))
    if missing:
        raise ValueError(f"source bundle candidate is missing keys: {missing}")
    candidate = dict(raw_candidate)
    candidate["eligible"] = build_eligible_teams(candidate["rankings"], season)
    validate_ap_top25_coverage(candidate["eligible"])
    actual_checksum = candidate_checksum(candidate)
    if manifest.get("candidate_sha256") != actual_checksum:
        raise ValueError(
            "source bundle candidate checksum does not match its manifest"
        )
    return candidate


def candidate_reference_school_names(
    candidate: dict[str, Any],
) -> dict[int, str]:
    names: dict[int, str] = {}
    for row in candidate["player_seasons"]:
        team_id = int(row["teamId"])
        name = str(row.get("team") or "").strip()
        if not name:
            raise ValueError(f"Player-season team {team_id} has no school name")
        previous = names.setdefault(team_id, name)
        if previous != name:
            raise ValueError(
                f"CBBD team {team_id} has conflicting names: "
                f"{previous!r} and {name!r}"
            )
    return names


def candidate_school_names(candidate: dict[str, Any]) -> dict[int, str]:
    """Collect source school labels for both current and historical rows."""

    names = candidate_reference_school_names(candidate)
    for roster in candidate["rosters"]:
        team_id = int(roster["teamId"])
        name = str(roster.get("team") or "").strip()
        if not name:
            continue
        previous = names.setdefault(team_id, name)
        if previous != name:
            raise ValueError(
                f"CBBD team {team_id} has conflicting names: "
                f"{previous!r} and {name!r}"
            )
    by_name: dict[str, int] = {}
    for team_id, name in names.items():
        previous_id = by_name.setdefault(name, team_id)
        if previous_id != team_id:
            raise ValueError(
                f"CBBD school name {name!r} is used by ids "
                f"{previous_id} and {team_id}"
            )
    return names


def missing_reference_schools(
    conn: Any,
    candidate: dict[str, Any],
) -> list[tuple[int, str]]:
    """Return non-eligible historical schools that must be inserted."""

    eligible_ids = {row.team_id for row in candidate["eligible"]}
    reference_names = candidate_school_names(candidate)
    required = eligible_ids | set(reference_names)
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, name FROM schools WHERE id = ANY(%s) OR name = ANY(%s)",
            (list(required), list(reference_names.values())),
        )
        existing = cursor.fetchall()
    known_ids = {int(row[0]) for row in existing}
    missing_eligible = sorted(eligible_ids - known_ids)
    if missing_eligible:
        raise ValueError(
            f"Eligible schools are missing required CBBD ids: {missing_eligible}"
        )

    existing_names = {str(name): int(team_id) for team_id, name in existing}
    additions: list[tuple[int, str]] = []
    for team_id in sorted(set(reference_names) - known_ids):
        name = reference_names[team_id]
        conflicting_id = existing_names.get(name)
        if conflicting_id is not None and conflicting_id != team_id:
            raise ValueError(
                f"School name {name!r} already belongs to CBBD id {conflicting_id}, "
                f"not {team_id}"
            )
        additions.append((team_id, name))
    return additions


def player_season_values(row: dict[str, Any]) -> tuple[Any, ...]:
    field_goals = row.get("fieldGoals") or {}
    twos = row.get("twoPointFieldGoals") or {}
    threes = row.get("threePointFieldGoals") or {}
    free_throws = row.get("freeThrows") or {}
    rebounds = row.get("rebounds") or {}
    win_shares = row.get("winShares") or {}
    return (
        int(row["athleteId"]),
        int(row["teamId"]),
        int(row["season"]),
        row.get("seasonLabel"),
        row.get("conference"),
        str(row.get("athleteSourceId") or ""),
        row.get("position"),
        row.get("games"),
        row.get("starts"),
        row.get("minutes"),
        row.get("points"),
        row.get("turnovers"),
        row.get("fouls"),
        row.get("assists"),
        row.get("steals"),
        row.get("blocks"),
        row.get("usage"),
        row.get("offensiveRating"),
        row.get("defensiveRating"),
        row.get("netRating"),
        row.get("PORPAG"),
        row.get("effectiveFieldGoalPct"),
        row.get("trueShootingPct"),
        row.get("assistsTurnoverRatio"),
        row.get("freeThrowRate"),
        row.get("offensiveReboundPct"),
        field_goals.get("pct"),
        field_goals.get("attempted"),
        field_goals.get("made"),
        twos.get("pct"),
        twos.get("attempted"),
        twos.get("made"),
        threes.get("pct"),
        threes.get("attempted"),
        threes.get("made"),
        free_throws.get("pct"),
        free_throws.get("attempted"),
        free_throws.get("made"),
        rebounds.get("total"),
        rebounds.get("defensive"),
        rebounds.get("offensive"),
        win_shares.get("totalPer40"),
        win_shares.get("total"),
        win_shares.get("defensive"),
        win_shares.get("offensive"),
    )


def reconcile_torvik_player_seasons(
    cursor: Any,
    player_ids: list[int],
) -> dict[str, int]:
    """Attach Torvik evidence to the normalized player-team-season rows."""
    cursor.execute(
        """
        SELECT player_season.id, player.name, school.name, player_season.season
        FROM player_seasons player_season
        JOIN players player ON player.id = player_season.player_id
        JOIN schools school ON school.id = player_season.team_id
        WHERE player_season.player_id = ANY(%s)
          AND player_season.season >= 2008
        """,
        (player_ids,),
    )
    player_seasons = cursor.fetchall()
    cursor.execute(
        """
        SELECT torvik_id, player, team, season
        FROM player_defensive_stats
        WHERE source_active
        """
    )
    defensive_rows = cursor.fetchall()
    matches, method_counts, unmatched, _review = match_all(
        player_seasons,
        defensive_rows,
    )
    cursor.execute(
        """
        UPDATE player_seasons
        SET torvik_id = NULL, torvik_match_method = NULL, updated_at = now()
        WHERE player_id = ANY(%s)
          AND (torvik_id IS NOT NULL OR torvik_match_method IS NOT NULL)
        """,
        (player_ids,),
    )
    if matches:
        execute_values(
            cursor,
            """
            UPDATE player_seasons player_season
            SET torvik_id = matched.torvik_id,
                torvik_match_method = matched.method,
                updated_at = now()
            FROM (VALUES %s) AS matched(
                player_season_id, torvik_id, method
            )
            WHERE player_season.id = matched.player_season_id
            """,
            matches,
            template="(%s, %s, %s)",
            page_size=500,
        )
    return {
        "candidate_player_seasons": len(player_seasons),
        "matched_player_seasons": len(matches),
        "unmatched_player_seasons": len(unmatched),
        **{
            f"matched_{method}": count
            for method, count in sorted(method_counts.items())
        },
    }


def publish_candidate(
    conn: Any,
    candidate: dict[str, Any],
    season: int,
    run_id: int,
    counts: dict[str, Any],
    reference_schools: list[tuple[int, str]],
) -> None:
    eligible_ids = [row.team_id for row in candidate["eligible"]]
    roster_players = [
        (roster, player)
        for roster in candidate["rosters"]
        for player in roster.get("players", [])
    ]
    all_player_names: dict[int, tuple[str, str | None]] = {}
    for _roster, player in roster_players:
        all_player_names[int(player["id"])] = (
            player["name"],
            str(player.get("sourceId")) if player.get("sourceId") else None,
        )
    for row in candidate["player_seasons"]:
        all_player_names.setdefault(
            int(row["athleteId"]),
            (row["name"], str(row.get("athleteSourceId") or "") or None),
        )
    source_school_names = candidate_school_names(candidate)
    school_values = [
        (team_id, name, True, datetime.now(timezone.utc))
        for team_id, name in sorted(source_school_names.items())
    ]
    for team_id, name in reference_schools:
        if team_id not in source_school_names:
            school_values.append((team_id, name, True, datetime.now(timezone.utc)))

    with conn.cursor() as cursor:
        if school_values:
            execute_values(
                cursor,
                """
                INSERT INTO schools (id, name, source_active, source_updated_at)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    source_active = TRUE,
                    source_updated_at = EXCLUDED.source_updated_at
                """,
                school_values,
            )
        execute_values(
            cursor,
            """
            INSERT INTO players (id, name, source_id, source_active, source_updated_at)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                source_id = COALESCE(EXCLUDED.source_id, players.source_id),
                source_active = TRUE,
                source_updated_at = now()
            """,
            [
                (player_id, name, source_id, True, datetime.now(timezone.utc))
                for player_id, (name, source_id) in all_player_names.items()
            ],
            page_size=500,
        )
        execute_values(
            cursor,
            """
            INSERT INTO team_season_eligibility (
                team_id, season, reasons, first_poll_week,
                first_poll_date, peak_rank, source_updated_at
            ) VALUES %s
            ON CONFLICT (team_id, season) DO UPDATE SET
                reasons = (
                    SELECT ARRAY_AGG(DISTINCT reason ORDER BY reason)
                    FROM UNNEST(
                        team_season_eligibility.reasons || EXCLUDED.reasons
                    ) reason
                ),
                first_poll_week = CASE
                    WHEN team_season_eligibility.first_poll_week IS NULL
                        THEN EXCLUDED.first_poll_week
                    WHEN EXCLUDED.first_poll_week IS NULL
                        THEN team_season_eligibility.first_poll_week
                    ELSE LEAST(
                        team_season_eligibility.first_poll_week,
                        EXCLUDED.first_poll_week
                    )
                END,
                first_poll_date = CASE
                    WHEN team_season_eligibility.first_poll_date IS NULL
                        THEN EXCLUDED.first_poll_date
                    WHEN EXCLUDED.first_poll_date IS NULL
                        THEN team_season_eligibility.first_poll_date
                    ELSE LEAST(
                        team_season_eligibility.first_poll_date,
                        EXCLUDED.first_poll_date
                    )
                END,
                peak_rank = CASE
                    WHEN team_season_eligibility.peak_rank IS NULL
                        THEN EXCLUDED.peak_rank
                    WHEN EXCLUDED.peak_rank IS NULL
                        THEN team_season_eligibility.peak_rank
                    ELSE LEAST(
                        team_season_eligibility.peak_rank,
                        EXCLUDED.peak_rank
                    )
                END,
                source_updated_at = now()
            """,
            [
                (
                    row.team_id,
                    row.season,
                    list(row.reasons),
                    row.first_poll_week,
                    row.first_poll_date,
                    row.peak_rank,
                    datetime.now(timezone.utc),
                )
                for row in candidate["eligible"]
            ],
        )
        cursor.execute(
            """
            UPDATE team_roster_memberships
            SET source_active = FALSE, updated_at = now()
            WHERE season = %s AND team_id = ANY(%s)
            """,
            (season, eligible_ids),
        )
        execute_values(
            cursor,
            """
            INSERT INTO team_roster_memberships (
                player_id, team_id, season, source_player_id, jersey,
                raw_position, height, weight, date_of_birth,
                source_start_season, source_end_season,
                source_active, first_seen_at, last_seen_at, updated_at
            ) VALUES %s
            ON CONFLICT (player_id, team_id, season) DO UPDATE SET
                source_player_id = EXCLUDED.source_player_id,
                jersey = EXCLUDED.jersey,
                raw_position = EXCLUDED.raw_position,
                height = EXCLUDED.height,
                weight = EXCLUDED.weight,
                date_of_birth = EXCLUDED.date_of_birth,
                source_start_season = EXCLUDED.source_start_season,
                source_end_season = EXCLUDED.source_end_season,
                source_active = TRUE,
                last_seen_at = now(),
                updated_at = now()
            """,
            [
                (
                    int(player["id"]),
                    int(roster["teamId"]),
                    season,
                    str(player.get("sourceId") or "") or None,
                    player.get("jersey"),
                    player.get("position"),
                    player.get("height"),
                    player.get("weight"),
                    player.get("dateOfBirth"),
                    player.get("startSeason"),
                    player.get("endSeason"),
                    True,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                )
                for roster, player in roster_players
            ],
            page_size=500,
        )
        cursor.execute(
            """
            DELETE FROM team_roster_position_maps position
            USING team_roster_memberships membership
            WHERE position.roster_membership_id = membership.id
              AND membership.season = %s
              AND membership.team_id = ANY(%s)
            """,
            (season, eligible_ids),
        )
        # Lineup rows are source-owned and have no independent application
        # lifecycle column. Replace only the eligible season/team scope; the
        # foreign-key cascade removes its five-player child rows. Other teams,
        # seasons, and all application-owned tables remain untouched.
        cursor.execute(
            """
            DELETE FROM team_game_lineups
            WHERE season = %s AND team_id = ANY(%s)
            """,
            (season, eligible_ids),
        )
        cursor.execute(
            """
            SELECT id, player_id, team_id
            FROM team_roster_memberships
            WHERE season = %s AND team_id = ANY(%s) AND source_active
            """,
            (season, eligible_ids),
        )
        membership_ids = {
            (player_id, team_id): membership_id
            for membership_id, player_id, team_id in cursor.fetchall()
        }
        position_rows = [
            (membership_ids[(int(player["id"]), int(roster["teamId"]))], position)
            for roster, player in roster_players
            for position in normalize_positions(player.get("position"))
        ]
        if position_rows:
            execute_values(
                cursor,
                """
                INSERT INTO team_roster_position_maps (
                    roster_membership_id, position
                ) VALUES %s
                ON CONFLICT DO NOTHING
                """,
                position_rows,
            )

        player_season_columns = """
            player_id, team_id, season, season_label, conference,
            athlete_source_id, source_position, games, starts, minutes,
            points, turnovers, fouls, assists, steals, blocks, usage,
            offensive_rating, defensive_rating, net_rating, porpag,
            effective_field_goal_pct, true_shooting_pct,
            assists_turnover_ratio, free_throw_rate, offensive_rebound_pct,
            field_goals_pct, field_goals_attempted, field_goals_made,
            two_point_field_goals_pct, two_point_field_goals_attempted,
            two_point_field_goals_made, three_point_field_goals_pct,
            three_point_field_goals_attempted, three_point_field_goals_made,
            free_throws_pct, free_throws_attempted, free_throws_made,
            rebounds_total, rebounds_defensive, rebounds_offensive,
            win_shares_total_per40, win_shares_total, win_shares_defensive,
            win_shares_offensive
        """
        execute_values(
            cursor,
            f"""
            INSERT INTO player_seasons ({player_season_columns})
            VALUES %s
            ON CONFLICT (player_id, team_id, season) DO UPDATE SET
                season_label = EXCLUDED.season_label,
                conference = EXCLUDED.conference,
                athlete_source_id = EXCLUDED.athlete_source_id,
                source_position = EXCLUDED.source_position,
                games = EXCLUDED.games,
                starts = EXCLUDED.starts,
                minutes = EXCLUDED.minutes,
                points = EXCLUDED.points,
                turnovers = EXCLUDED.turnovers,
                fouls = EXCLUDED.fouls,
                assists = EXCLUDED.assists,
                steals = EXCLUDED.steals,
                blocks = EXCLUDED.blocks,
                usage = EXCLUDED.usage,
                offensive_rating = EXCLUDED.offensive_rating,
                defensive_rating = EXCLUDED.defensive_rating,
                net_rating = EXCLUDED.net_rating,
                porpag = EXCLUDED.porpag,
                effective_field_goal_pct = EXCLUDED.effective_field_goal_pct,
                true_shooting_pct = EXCLUDED.true_shooting_pct,
                assists_turnover_ratio = EXCLUDED.assists_turnover_ratio,
                free_throw_rate = EXCLUDED.free_throw_rate,
                offensive_rebound_pct = EXCLUDED.offensive_rebound_pct,
                field_goals_pct = EXCLUDED.field_goals_pct,
                field_goals_attempted = EXCLUDED.field_goals_attempted,
                field_goals_made = EXCLUDED.field_goals_made,
                two_point_field_goals_pct = EXCLUDED.two_point_field_goals_pct,
                two_point_field_goals_attempted =
                    EXCLUDED.two_point_field_goals_attempted,
                two_point_field_goals_made = EXCLUDED.two_point_field_goals_made,
                three_point_field_goals_pct =
                    EXCLUDED.three_point_field_goals_pct,
                three_point_field_goals_attempted =
                    EXCLUDED.three_point_field_goals_attempted,
                three_point_field_goals_made =
                    EXCLUDED.three_point_field_goals_made,
                free_throws_pct = EXCLUDED.free_throws_pct,
                free_throws_attempted = EXCLUDED.free_throws_attempted,
                free_throws_made = EXCLUDED.free_throws_made,
                rebounds_total = EXCLUDED.rebounds_total,
                rebounds_defensive = EXCLUDED.rebounds_defensive,
                rebounds_offensive = EXCLUDED.rebounds_offensive,
                win_shares_total_per40 = EXCLUDED.win_shares_total_per40,
                win_shares_total = EXCLUDED.win_shares_total,
                win_shares_defensive = EXCLUDED.win_shares_defensive,
                win_shares_offensive = EXCLUDED.win_shares_offensive,
                source_active = TRUE,
                source_updated_at = now(),
                updated_at = now()
            """,
            [player_season_values(row) for row in candidate["player_seasons"]],
            page_size=500,
        )
        torvik_validation = reconcile_torvik_player_seasons(
            cursor,
            sorted(all_player_names),
        )
        counts["torvik_reconciliation"] = torvik_validation

        execute_values(
            cursor,
            """
            INSERT INTO college_games (
                id, source_id, season, season_type, start_date, status,
                neutral_site, conference_game, home_team_id, home_team,
                home_points, away_team_id, away_team, away_points, raw_payload
            ) VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                home_points = EXCLUDED.home_points,
                away_points = EXCLUDED.away_points,
                raw_payload = EXCLUDED.raw_payload,
                source_updated_at = now()
            """,
            [
                (
                    int(game["id"]),
                    str(game["sourceId"]),
                    int(game["season"]),
                    str(game["seasonType"]),
                    game["startDate"],
                    str(game["status"]),
                    bool(game.get("neutralSite")),
                    bool(game.get("conferenceGame")),
                    int(game["homeTeamId"]),
                    game["homeTeam"],
                    game.get("homePoints"),
                    int(game["awayTeamId"]),
                    game["awayTeam"],
                    game.get("awayPoints"),
                    Json(game),
                )
                for game in candidate["games"]
            ],
            page_size=500,
        )
        for lineup in candidate["lineups"]:
            cursor.execute(
                """
                INSERT INTO team_game_lineups (
                    game_id, team_id, season, lineup_hash, total_seconds,
                    pace, offense_rating, defense_rating, net_rating,
                    team_stats, opponent_stats, raw_payload
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (game_id, team_id, lineup_hash) DO UPDATE SET
                    total_seconds = EXCLUDED.total_seconds,
                    pace = EXCLUDED.pace,
                    offense_rating = EXCLUDED.offense_rating,
                    defense_rating = EXCLUDED.defense_rating,
                    net_rating = EXCLUDED.net_rating,
                    team_stats = EXCLUDED.team_stats,
                    opponent_stats = EXCLUDED.opponent_stats,
                    raw_payload = EXCLUDED.raw_payload,
                    source_updated_at = now()
                RETURNING id
                """,
                (
                    lineup["_gameId"],
                    lineup["teamId"],
                    lineup["_season"],
                    lineup["idHash"],
                    lineup["totalSeconds"],
                    lineup.get("pace"),
                    lineup.get("offenseRating"),
                    lineup.get("defenseRating"),
                    lineup.get("netRating"),
                    Json(lineup["teamStats"]),
                    Json(lineup["opponentStats"]),
                    Json({key: value for key, value in lineup.items() if not key.startswith("_")}),
                ),
            )
            lineup_id = cursor.fetchone()[0]
            cursor.execute(
                "DELETE FROM team_game_lineup_players WHERE lineup_id = %s",
                (lineup_id,),
            )
            execute_values(
                cursor,
                """
                INSERT INTO team_game_lineup_players (
                    lineup_id, player_id, ordinal
                ) VALUES %s
                """,
                [
                    (lineup_id, int(athlete["id"]), ordinal)
                    for ordinal, athlete in enumerate(
                        lineup["athletes"],
                        start=1,
                    )
                ],
            )

        execute_values(
            cursor,
            """
            INSERT INTO opponent_team_season_contexts (
                team_id, season, team, games, pace,
                offense_rating, raw_payload
            ) VALUES %s
            ON CONFLICT (team_id, season) DO UPDATE SET
                team = EXCLUDED.team,
                games = EXCLUDED.games,
                pace = EXCLUDED.pace,
                offense_rating = EXCLUDED.offense_rating,
                raw_payload = EXCLUDED.raw_payload,
                source_updated_at = now()
            """,
            [
                (
                    int(row["teamId"]),
                    int(row["season"]),
                    row["team"],
                    int(row["games"]),
                    row.get("pace"),
                    (row.get("teamStats") or {}).get("rating"),
                    Json(row),
                )
                for row in candidate["opponent_contexts"]
            ],
            page_size=500,
        )
        cursor.execute(
            """
            UPDATE data_import_runs
            SET status = 'published',
                published_row_counts = %s,
                published_at = now(),
                finished_at = now()
            WHERE id = %s AND status = 'validated'
            """,
            (Json(counts), run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"Import run {run_id} is not validated")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument(
        "--source-dir",
        type=Path,
        help=(
            "Local source bundle directory; defaults to "
            "data/raw/ranked_rosters/<season>/<release-version>"
        ),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download or resume the local source bundle, then exit",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.season < FIRST_SUPPORTED_SEASON:
        parser.error("season must be 2024 or later")
    if args.max_retries < 1:
        parser.error("--max-retries must be positive")
    if args.download and args.apply:
        parser.error("--download and --apply are separate phases")

    try:
        source_dir = source_directory(
            args.season,
            args.release_version,
            args.source_dir,
        )
    except ValueError as error:
        parser.error(str(error))

    load_env_file()
    if args.download:
        access_token = os.environ.get("CBBD_API_KEY")
        manifest_path = source_dir / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        current_bundle = (
            isinstance(manifest, dict)
            and manifest.get("format_version") == ARCHIVE_FORMAT_VERSION
        )
        if not access_token and not current_bundle:
            raise SystemExit("Missing CBBD_API_KEY")
        candidate = fetch_candidate(
            access_token or "",
            args.season,
            args.max_retries,
            source_dir,
            args.release_version,
        )
        manifest = read_json(source_dir / "manifest.json")
        print(
            json.dumps(
                {
                    "source_dir": str(source_dir),
                    "candidate_sha256": candidate_checksum(candidate),
                    "counts": manifest["counts"],
                },
                indent=2,
            )
        )
        print("DOWNLOAD COMPLETE: no database rows changed")
        return

    candidate = load_candidate_bundle(
        source_dir,
        args.season,
        args.release_version,
    )
    validation = validate_candidate(candidate)
    checksum = candidate_checksum(candidate)
    print(
        json.dumps(
            {
                "source_dir": str(source_dir),
                "validation": validation,
                "checksum": checksum,
            },
            indent=2,
        )
    )
    if not args.apply:
        print("DRY RUN: no database rows changed")
        return

    conn = psycopg2.connect(connection_dsn())
    conn.autocommit = False
    run_id: int | None = None
    try:
        reference_schools = missing_reference_schools(conn, candidate)
        if reference_schools:
            validation["reference_schools_to_insert"] = [
                {"team_id": team_id, "name": name}
                for team_id, name in reference_schools
            ]
            print(
                "Historical reference schools to insert: "
                f"{validation['reference_schools_to_insert']}"
            )
        commit = pipeline_commit(REPO_ROOT)
        run_id = begin_import_run(
            conn,
            dataset="ap_top25_vt_rosters_lineups",
            import_version=args.release_version,
            commit=commit,
            source_uri=(
                f"cbbd-bundle://ranked-rosters/{args.season}/"
                f"{args.release_version}"
            ),
            first_season=args.season,
            last_season=args.season,
            metadata={
                "virginia_tech_team_id": 340,
                "source_dir": str(source_dir),
                "candidate_sha256": checksum,
            },
        )
        counts = {
            "schools": len(reference_schools),
            "team_season_eligibility": len(candidate["eligible"]),
            "team_roster_memberships": validation["roster_players"],
            "player_seasons": len(candidate["player_seasons"]),
            "college_games": len(candidate["games"]),
            "team_game_lineups": len(candidate["lineups"]),
            "opponent_team_season_contexts": len(
                candidate["opponent_contexts"]
            ),
        }
        mark_validated(
            conn,
            run_id,
            source_sha256=checksum,
            staged_row_counts=counts,
            validation_results=validation,
        )
        publish_candidate(
            conn,
            candidate,
            args.season,
            run_id,
            counts,
            reference_schools,
        )
        print(f"Published season {args.season} release {args.release_version}")
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
