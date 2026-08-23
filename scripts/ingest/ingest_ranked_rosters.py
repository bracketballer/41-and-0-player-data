"""Ingest AP Top 25 + Virginia Tech rosters and game-lineup evidence.

The command fetches a complete season candidate, validates it, and is a dry run
unless --apply is supplied. A team remains eligible once it appears in any AP
rank 1-25 response; Virginia Tech (CBBD team 340) is always included.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import cbbd
import psycopg2
from cbbd.rest import ApiException
from psycopg2.extras import Json, execute_values
from pydantic import ValidationError

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
    validate_roster_coverage,
)
from scripts.ingest.reconcile_torvik_ids import match_all

from bracketballer_data.paths import REPO_ROOT
GAME_WINDOW_DAYS = 14
PLAYER_HISTORY_FIRST_SEASON = 2005


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
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
            retryable = not isinstance(error, ApiException) or (
                status is None or status == 429 or status >= 500
            )
            if attempt == retries or not retryable:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1)) + random.random())
    raise AssertionError("retry loop exited")


def fetch_games(games_api: Any, season: int, retries: int) -> list[dict[str, Any]]:
    start = datetime(season - 1, 10, 1, tzinfo=timezone.utc)
    end = datetime(season, 5, 15, tzinfo=timezone.utc)
    games: dict[int, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        window_end = min(end, cursor + timedelta(days=GAME_WINDOW_DAYS))
        rows = call_with_retries(
            lambda left=cursor, right=window_end: games_api.get_games(
                season=season,
                start_date_range=left,
                end_date_range=right,
                _request_timeout=(10, 120),
            ),
            retries,
        )
        for row in rows or []:
            item = payload(row)
            games[int(item["id"])] = item
        cursor = window_end
    return sorted(games.values(), key=lambda row: int(row["id"]))


def fetch_candidate(
    access_token: str,
    season: int,
    retries: int,
) -> dict[str, Any]:
    configuration = cbbd.Configuration(access_token=access_token)
    with cbbd.ApiClient(configuration) as client:
        rankings_api = cbbd.RankingsApi(client)
        teams_api = cbbd.TeamsApi(client)
        stats_api = cbbd.StatsApi(client)
        games_api = cbbd.GamesApi(client)
        lineups_api = cbbd.LineupsApi(client)

        rankings = [
            payload(row)
            for row in call_with_retries(
                lambda: rankings_api.get_rankings(
                    season=season,
                    poll_type="ap",
                    _request_timeout=(10, 120),
                ),
                retries,
            )
            or []
        ]
        eligible = build_eligible_teams(rankings, season)
        eligible_ids = {row.team_id for row in eligible}

        roster_responses = call_with_retries(
            lambda: teams_api.get_team_roster(
                season=season,
                _request_timeout=(10, 120),
            ),
            retries,
        )
        rosters = [
            payload(row)
            for row in roster_responses or []
            if int(payload(row)["teamId"]) in eligible_ids
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
            rows = call_with_retries(
                lambda selected=candidate_season: stats_api.get_player_season_stats(
                    season=selected,
                    _request_timeout=(10, 180),
                ),
                retries,
            )
            player_seasons.extend(
                item
                for item in (payload(row) for row in rows or [])
                if int(item["athleteId"]) in roster_player_ids
            )

        games = [
            game
            for game in fetch_games(games_api, season, retries)
            if int(game["homeTeamId"]) in eligible_ids
            or int(game["awayTeamId"]) in eligible_ids
        ]
        final_games = [
            game for game in games if str(game.get("status")) == "final"
        ]
        lineups: list[dict[str, Any]] = []
        skipped_game_ids: list[int] = []
        for index, game in enumerate(final_games, start=1):
            try:
                rows = call_with_retries(
                    lambda game_id=int(game["id"]): lineups_api.get_lineup_stats_by_game(
                        game_id=game_id,
                        _request_timeout=(10, 120),
                    ),
                    retries,
                )
            except ValidationError:
                # CBBD occasionally returns a lineup segment with a null
                # defenseRating/netRating (very-low-sample lineups), which the
                # cbbd client's typed model rejects outright. That crashes
                # deserialization for the whole game's lineup list, not just
                # the offending row, so the only unit we can skip is the game.
                skipped_game_ids.append(int(game["id"]))
                continue
            for row in rows or []:
                item = payload(row)
                if int(item["teamId"]) in eligible_ids:
                    item["_gameId"] = int(game["id"])
                    item["_season"] = season
                    lineups.append(item)
            if index % 50 == 0:
                print(f"lineups: fetched {index}/{len(final_games)} final games")
        if skipped_game_ids:
            print(
                f"lineups: skipped {len(skipped_game_ids)} game(s) with unparseable "
                f"lineup ratings: {skipped_game_ids}"
            )

        opponent_ids: set[int] = set()
        for game in games:
            home_team_id = int(game["homeTeamId"])
            away_team_id = int(game["awayTeamId"])
            if home_team_id in eligible_ids:
                opponent_ids.add(away_team_id)
            if away_team_id in eligible_ids:
                opponent_ids.add(home_team_id)
        team_stats = call_with_retries(
            lambda: stats_api.get_team_season_stats(
                season=season,
                _request_timeout=(10, 180),
            ),
            retries,
        )
        opponent_contexts = [
            payload(row)
            for row in team_stats or []
            if int(payload(row)["teamId"]) in opponent_ids
        ]
    return {
        "rankings": rankings,
        "eligible": eligible,
        "rosters": rosters,
        "player_seasons": player_seasons,
        "games": games,
        "lineups": lineups,
        "opponent_contexts": opponent_contexts,
        "skipped_lineup_game_ids": skipped_game_ids,
    }


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
    all_missing_game_lineups = expected_game_teams - set(lineups_by_game_team)
    # A game already logged in fetch_candidate as skipped for the known
    # null-rating CBBD bug (#16) is an expected gap, not a validation
    # failure -- only an *unexplained* missing lineup should hard-fail.
    known_missing_game_lineups = sorted(
        pair for pair in all_missing_game_lineups if pair[0] in skipped_lineup_game_ids
    )
    missing_game_lineups = sorted(
        pair for pair in all_missing_game_lineups if pair[0] not in skipped_lineup_game_ids
    )
    reconciliation_failures: list[dict[str, Any]] = []
    for (game_id, team_id), lineups in lineups_by_game_team.items():
        game = game_by_id[game_id]
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
            reconciliation_failures.append(
                {
                    "game_id": game_id,
                    "team_id": team_id,
                    "lineup_seconds": seconds,
                    "lineup_points": points,
                    "game_points": expected_points,
                }
            )
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
        "unresolved_lineup_players": 0,
        "invalid_lineups": 0,
        "missing_game_lineups": 0,
        "reconciliation_failures": 0,
        "known_skipped_game_lineups": len(known_missing_game_lineups),
    }


def candidate_checksum(candidate: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    serializable = {
        key: [
            row.__dict__ if hasattr(row, "__dict__") else row
            for row in candidate[key]
        ]
        for key in (
            "eligible",
            "rosters",
            "player_seasons",
            "games",
            "lineups",
            "opponent_contexts",
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


def ensure_known_schools(conn: Any, candidate: dict[str, Any]) -> None:
    eligible_ids = {row.team_id for row in candidate["eligible"]}
    season_team_ids = {
        int(row["teamId"]) for row in candidate["player_seasons"]
    }
    required = eligible_ids | season_team_ids
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM schools WHERE id = ANY(%s)", (list(required),))
        known = {row[0] for row in cursor.fetchall()}
    missing = sorted(required - known)
    if missing:
        raise ValueError(f"Schools are missing required CBBD ids: {missing}")


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

    with conn.cursor() as cursor:
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
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.season < FIRST_SUPPORTED_SEASON:
        parser.error("season must be 2024 or later")
    if args.max_retries < 1:
        parser.error("--max-retries must be positive")

    load_env_file()
    access_token = os.environ.get("CBBD_API_KEY")
    if not access_token:
        raise SystemExit("Missing CBBD_API_KEY")
    candidate = fetch_candidate(
        access_token,
        args.season,
        args.max_retries,
    )
    validation = validate_candidate(candidate)
    checksum = candidate_checksum(candidate)
    print(json.dumps({"validation": validation, "checksum": checksum}, indent=2))
    if not args.apply:
        print("DRY RUN: no database rows changed")
        return

    conn = psycopg2.connect(connection_dsn())
    conn.autocommit = False
    run_id: int | None = None
    try:
        ensure_known_schools(conn, candidate)
        commit = pipeline_commit(REPO_ROOT)
        run_id = begin_import_run(
            conn,
            dataset="ap_top25_vt_rosters_lineups",
            import_version=args.release_version,
            commit=commit,
            source_uri=f"cbbd://rankings+rosters+games+lineups?season={args.season}",
            first_season=args.season,
            last_season=args.season,
            metadata={"virginia_tech_team_id": 340},
        )
        counts = {
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
