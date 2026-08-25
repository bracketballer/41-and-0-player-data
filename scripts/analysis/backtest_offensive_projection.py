"""Run issue #14's preregistered offensive-projection backtest.

The command opens PostgreSQL read-only, walks final games in chronological
order, and builds every model input from earlier games in that season.  It
writes only an aggregate JSON report below ``data/reports``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.defensive_lineup_concession import (
    DefensiveLineupEvidence,
    LeagueSeasonBaseline,
    TeamSeasonBaseline,
    build_concession_profiles,
)
from bracketballer_data.lineup_attribution import resolve_event_side
from bracketballer_data.matchup_assignment import DefensiveMatchupPlayer
from bracketballer_data.offensive_projection import (
    INTERVAL_DRAW_COUNT,
    INTERVAL_SEED,
    DefensiveUnitEvidence,
    OffensivePlayerEvidence,
    project_offensive_matrix,
)
from bracketballer_data.shot_location_profiles import (
    FIELD_GOAL_ZONES,
    PlayerShotLocationInput,
    SeasonShotLocationBaseline,
    ShotLocationProfile,
    ZoneCount,
    build_shot_location_profiles,
)
from bracketballer_data.shot_zones import ShotCoordinate, classify_shot, infer_attacking_baskets
from bracketballer_data.offensive_projection_backtest import (
    BacktestCell,
    BacktestProtocol,
    evaluate_gate,
)


DEFAULT_REPORT = Path("data/reports/offensive-projection-backtest/issue-0014.json")
FREE_THROW_RANGE = "free_throw"
POINT_ZONES = {"rim", "short_mid", "long_mid"}


@dataclass(frozen=True, slots=True)
class Game:
    season: int
    game_id: int
    start_date: Any
    home_team_id: int
    away_team_id: int

    @property
    def sort_key(self) -> tuple[Any, int]:
        return self.start_date, self.game_id


@dataclass(frozen=True, slots=True)
class Shot:
    source_play_id: int
    season: int
    game_id: int
    game_start_date: Any
    team_id: int | None
    opponent_id: int | None
    player_id: int
    period: int | None
    made: bool | None
    location_x: float | None
    location_y: float | None
    shot_range: str | None
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StoredLineup:
    season: int
    game_id: int
    team_id: int
    lineup_hash: str
    player_ids: tuple[int, ...]
    total_seconds: float
    possessions: float
    opponent_fga: float


@dataclass
class UnitState:
    season: int
    team_id: int
    lineup_hash: str
    player_ids: tuple[int, ...]
    total_seconds: float = 0.0
    possessions: float = 0.0
    opponent_fga: float = 0.0
    zone_attempts: dict[str, int] = field(
        default_factory=lambda: {zone: 0 for zone in FIELD_GOAL_ZONES}
    )


@dataclass
class SeasonState:
    league_counts: dict[str, int] = field(
        default_factory=lambda: {zone: 0 for zone in FIELD_GOAL_ZONES}
    )
    league_makes: dict[str, int] = field(
        default_factory=lambda: {zone: 0 for zone in FIELD_GOAL_ZONES}
    )
    team_counts: dict[int, dict[str, int]] = field(default_factory=dict)
    player_counts: dict[tuple[int, int], dict[str, ZoneCount]] = field(default_factory=dict)
    player_usage: dict[tuple[int, int], float] = field(default_factory=dict)
    team_usage: dict[int, float] = field(default_factory=dict)
    units: dict[tuple[int, str], UnitState] = field(default_factory=dict)


@dataclass
class TargetCell:
    season: int
    game_id: int
    offense_team_id: int
    defense_team_id: int
    offensive_lineup_hash: str
    defensive_lineup_hash: str
    offensive_player_ids: tuple[int, ...]
    defensive_player_ids: tuple[int, ...]
    fga: int = 0
    points: int = 0


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nested_number(value: Any, *keys: str) -> float:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return 0.0
        current = current.get(key)
    return max(_finite(current) or 0.0, 0.0)


def _lineup_hash(player_ids: Iterable[int]) -> tuple[tuple[int, ...], str]:
    ids = tuple(sorted(int(player_id) for player_id in player_ids))
    if len(ids) != 5 or len(set(ids)) != 5 or any(player_id <= 0 for player_id in ids):
        raise ValueError(f"invalid five-player lineup: {ids}")
    return ids, "-".join(str(player_id) for player_id in ids)


def _zero_counts() -> dict[str, int]:
    return {zone: 0 for zone in FIELD_GOAL_ZONES}


def _zero_zone_counts() -> dict[str, ZoneCount]:
    return {zone: ZoneCount() for zone in FIELD_GOAL_ZONES}


def _load_eligible_teams(conn: Any, first: int, last: int) -> set[tuple[int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT team_id, season
            FROM team_season_eligibility
            WHERE season BETWEEN %s AND %s
            ORDER BY season, team_id
            """,
            (first, last),
        )
        return {(int(team_id), int(season)) for team_id, season in cursor.fetchall()}


def _load_games(conn: Any, first: int, last: int) -> dict[int, Game]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, season, start_date, home_team_id, away_team_id
            FROM college_games
            WHERE season BETWEEN %s AND %s
              AND status = 'final'
            ORDER BY season, start_date, id
            """,
            (first, last),
        )
        rows = cursor.fetchall()
    return {
        int(game_id): Game(
            season=int(season),
            game_id=int(game_id),
            start_date=start_date,
            home_team_id=int(home_team_id),
            away_team_id=int(away_team_id),
        )
        for game_id, season, start_date, home_team_id, away_team_id in rows
    }


def _load_shots(conn: Any, first: int, last: int) -> dict[int, list[Shot]]:
    with conn.cursor(name="issue_0014_shots") as cursor:
        cursor.itersize = 10_000
        cursor.execute(
            """
            SELECT source_play_id, season, game_id, game_start_date,
                   team_id, opponent_id, player_id, period, made,
                   location_x, location_y, shot_range, raw_payload
            FROM player_shot_events
            WHERE season BETWEEN %s AND %s
            ORDER BY season, game_start_date, game_id, source_play_id
            """,
            (first, last),
        )
        by_game: dict[int, list[Shot]] = defaultdict(list)
        for row in cursor:
            (
                source_play_id,
                season,
                game_id,
                game_start_date,
                team_id,
                opponent_id,
                player_id,
                period,
                made,
                location_x,
                location_y,
                shot_range,
                raw_payload,
            ) = row
            by_game[int(game_id)].append(
                Shot(
                    source_play_id=int(source_play_id),
                    season=int(season),
                    game_id=int(game_id),
                    game_start_date=game_start_date,
                    team_id=_integer(team_id),
                    opponent_id=_integer(opponent_id),
                    player_id=int(player_id),
                    period=_integer(period),
                    made=None if made is None else bool(made),
                    location_x=_finite(location_x),
                    location_y=_finite(location_y),
                    shot_range=None if shot_range is None else str(shot_range),
                    raw_payload=raw_payload if isinstance(raw_payload, Mapping) else {},
                )
            )
    return dict(by_game)


def _load_lineups(
    conn: Any,
    first: int,
    last: int,
    eligible: set[tuple[int, int]],
) -> tuple[
    dict[tuple[int, int, int, frozenset[int]], str],
    dict[int, list[StoredLineup]],
]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT lineup.season, lineup.game_id, lineup.team_id,
                   lineup.lineup_hash, lineup.total_seconds,
                   lineup.opponent_stats,
                   array_agg(lineup_player.player_id ORDER BY lineup_player.ordinal)
            FROM team_game_lineups lineup
            JOIN team_game_lineup_players lineup_player
              ON lineup_player.lineup_id = lineup.id
            WHERE lineup.season BETWEEN %s AND %s
            GROUP BY lineup.id, lineup.season, lineup.game_id, lineup.team_id,
                     lineup.lineup_hash, lineup.total_seconds, lineup.opponent_stats
            ORDER BY lineup.season, lineup.game_id, lineup.team_id, lineup.lineup_hash
            """,
            (first, last),
        )
        rows = cursor.fetchall()

    lookup: dict[tuple[int, int, int, frozenset[int]], str] = {}
    by_game: dict[int, list[StoredLineup]] = defaultdict(list)
    for season, game_id, team_id, lineup_hash, seconds, opponent_stats, player_ids in rows:
        season_value = int(season)
        team_value = int(team_id)
        if (team_value, season_value) not in eligible:
            continue
        ids, expected_hash = _lineup_hash(player_ids)
        if str(lineup_hash) != expected_hash:
            raise ValueError(f"stored lineup hash mismatch: {lineup_hash} != {expected_hash}")
        key = (season_value, int(game_id), team_value, frozenset(ids))
        if key in lookup and lookup[key] != expected_hash:
            raise ValueError(f"conflicting stored lineup identity: {key}")
        lookup[key] = expected_hash
        by_game[int(game_id)].append(
            StoredLineup(
                season=season_value,
                game_id=int(game_id),
                team_id=team_value,
                lineup_hash=expected_hash,
                player_ids=ids,
                total_seconds=max(float(seconds or 0.0), 0.0),
                possessions=_nested_number(opponent_stats, "possessions"),
                opponent_fga=_nested_number(opponent_stats, "fieldGoals", "attempted"),
            )
        )
    return lookup, dict(by_game)


def _load_known_player_ids(conn: Any) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM players")
        return {int(player_id) for (player_id,) in cursor.fetchall()}


def _directions(shots_by_game: Mapping[int, list[Shot]]) -> dict[int, Any]:
    output: dict[int, Any] = {}
    for events in shots_by_game.values():
        coordinates = [
            ShotCoordinate(
                event_id=event.source_play_id,
                game_id=event.game_id,
                team_id=event.team_id,
                opponent_id=event.opponent_id,
                period=event.period,
                location_x=event.location_x,
            )
            for event in events
            if event.shot_range != FREE_THROW_RANGE
        ]
        inference = infer_attacking_baskets(coordinates)
        output.update(inference.baskets)
    return output


def _field_goal_zone(event: Shot, directions: Mapping[int, Any]) -> str | None:
    if event.shot_range == FREE_THROW_RANGE:
        return None
    return classify_shot(
        event.location_x,
        event.location_y,
        directions.get(event.source_play_id),
    )


def _lineup_for_event(
    event: Shot,
    side: str,
    known_player_ids: set[int],
    lookup: Mapping[tuple[int, int, int, frozenset[int]], str],
) -> tuple[tuple[int, ...], str] | None:
    team_id = event.team_id if side == "offense" else event.opponent_id
    if team_id is None:
        return None
    resolution = resolve_event_side(event.raw_payload, side, known_player_ids)
    if not resolution.is_valid_five:
        return None
    ids = frozenset(resolution.resolved_player_ids)
    lineup_hash = lookup.get((event.season, event.game_id, int(team_id), ids))
    if lineup_hash is None:
        return None
    return tuple(sorted(ids)), lineup_hash


def _target_cells(
    game: Game,
    events: Iterable[Shot],
    *,
    directions: Mapping[int, Any],
    known_player_ids: set[int],
    lookup: Mapping[tuple[int, int, int, frozenset[int]], str],
    eligible: set[tuple[int, int]],
    exclusions: dict[str, int],
) -> list[TargetCell]:
    grouped: dict[tuple[int, int, str, int, str], TargetCell] = {}
    for event in events:
        if event.shot_range == FREE_THROW_RANGE or event.made is None:
            continue
        if event.team_id is None or event.opponent_id is None:
            exclusions["missing_team_ids"] += 1
            continue
        if (event.team_id, game.season) not in eligible or (event.opponent_id, game.season) not in eligible:
            exclusions["ineligible_matchup"] += 1
            continue
        zone = _field_goal_zone(event, directions)
        if zone is None:
            exclusions["unclassified_target_shots"] += 1
            continue
        offense = _lineup_for_event(event, "offense", known_player_ids, lookup)
        defense = _lineup_for_event(event, "defense", known_player_ids, lookup)
        if offense is None or defense is None:
            exclusions["unresolved_target_lineups"] += 1
            continue
        offensive_ids, offensive_hash = offense
        defensive_ids, defensive_hash = defense
        key = (event.team_id, offensive_hash, event.opponent_id, defensive_hash)
        cell = grouped.setdefault(
            key,
            TargetCell(
                season=game.season,
                game_id=game.game_id,
                offense_team_id=event.team_id,
                defense_team_id=event.opponent_id,
                offensive_lineup_hash=offensive_hash,
                defensive_lineup_hash=defensive_hash,
                offensive_player_ids=offensive_ids,
                defensive_player_ids=defensive_ids,
            ),
        )
        cell.fga += 1
        cell.points += 3 if zone not in POINT_ZONES else 2
        if event.made is False:
            cell.points -= 3 if zone not in POINT_ZONES else 2
    return list(grouped.values())


def _ensure_player_counts(state: SeasonState, key: tuple[int, int]) -> dict[str, ZoneCount]:
    return state.player_counts.setdefault(key, _zero_zone_counts())


def _update_state(
    state: SeasonState,
    game: Game,
    events: Iterable[Shot],
    lineups: Iterable[StoredLineup],
    *,
    directions: Mapping[int, Any],
    known_player_ids: set[int],
    lookup: Mapping[tuple[int, int, int, frozenset[int]], str],
) -> None:
    for lineup in lineups:
        unit = state.units.setdefault(
            (lineup.team_id, lineup.lineup_hash),
            UnitState(
                season=game.season,
                team_id=lineup.team_id,
                lineup_hash=lineup.lineup_hash,
                player_ids=lineup.player_ids,
            ),
        )
        unit.total_seconds += lineup.total_seconds
        unit.possessions += lineup.possessions
        unit.opponent_fga += lineup.opponent_fga

    for event in events:
        if event.team_id is None or event.player_id <= 0:
            continue
        usage_weight = 0.44 if event.shot_range == FREE_THROW_RANGE else 1.0
        player_key = (event.team_id, event.player_id)
        state.player_usage[player_key] = state.player_usage.get(player_key, 0.0) + usage_weight
        state.team_usage[event.team_id] = state.team_usage.get(event.team_id, 0.0) + usage_weight
        if event.shot_range == FREE_THROW_RANGE or event.made is None:
            continue
        zone = _field_goal_zone(event, directions)
        if zone is None:
            continue
        state.league_counts[zone] += 1
        state.league_makes[zone] += int(bool(event.made))
        if event.opponent_id is not None:
            team_counts = state.team_counts.setdefault(event.opponent_id, _zero_counts())
            team_counts[zone] += 1
        player_counts = _ensure_player_counts(state, player_key)
        previous = player_counts[zone]
        player_counts[zone] = ZoneCount(
            attempts=previous.attempts + 1,
            makes=previous.makes + int(bool(event.made)),
        )
        if event.opponent_id is None:
            continue
        defense = _lineup_for_event(event, "defense", known_player_ids, lookup)
        if defense is None:
            continue
        _player_ids, lineup_hash = defense
        unit = state.units.get((event.opponent_id, lineup_hash))
        if unit is not None:
            unit.zone_attempts[zone] += 1


def _build_offensive_players(
    state: SeasonState,
    season: int,
    team_id: int,
    player_ids: tuple[int, ...],
) -> list[OffensivePlayerEvidence] | None:
    league_counts = state.league_counts
    if not any(league_counts.values()):
        return None
    try:
        inputs = [
            PlayerShotLocationInput(
                player_id=player_id,
                season=season,
                zones=state.player_counts.get((team_id, player_id), _zero_zone_counts()),
            )
            for player_id in player_ids
        ]
        profiles = build_shot_location_profiles(
            inputs,
            [SeasonShotLocationBaseline(season=season, zones={
                zone: ZoneCount(attempts=league_counts[zone], makes=state.league_makes[zone])
                for zone in FIELD_GOAL_ZONES
            })],
        )
    except (TypeError, ValueError):
        return None
    by_player: dict[int, dict[str, ShotLocationProfile]] = defaultdict(dict)
    for profile in profiles:
        by_player[profile.player_id][profile.zone] = profile
    team_usage = state.team_usage.get(team_id, 0.0)
    output: list[OffensivePlayerEvidence] = []
    for player_id in player_ids:
        usage = 100.0 * state.player_usage.get((team_id, player_id), 0.0) / team_usage if team_usage else 0.0
        if usage <= 0:
            return None
        output.append(
            OffensivePlayerEvidence(
                player_id=player_id,
                usage=usage,
                handler_score=50.0,
                height=None,
                profiles=by_player[player_id],
            )
        )
    return output


def _build_defensive_unit(
    state: SeasonState,
    season: int,
    team_id: int,
    lineup_hash: str,
) -> DefensiveUnitEvidence | None:
    unit_state = state.units.get((team_id, lineup_hash))
    team_counts = state.team_counts.get(team_id)
    if unit_state is None or team_counts is None:
        return None
    if not any(state.league_counts.values()) or not any(team_counts.values()):
        return None
    try:
        rows = build_concession_profiles(
            [LeagueSeasonBaseline(season=season, zone_attempts=state.league_counts)],
            [TeamSeasonBaseline(season=season, team_id=team_id, zone_attempts=team_counts)],
            [DefensiveLineupEvidence(
                season=season,
                team_id=team_id,
                lineup_hash=lineup_hash,
                player_ids=unit_state.player_ids,
                total_seconds=unit_state.total_seconds,
                possessions=unit_state.possessions,
                opponent_fga=unit_state.opponent_fga,
                zone_attempts=unit_state.zone_attempts,
            )],
            independent_audit_passed=False,
        )
    except ValueError:
        return None
    if not rows:
        return None
    by_zone = {row.zone: row for row in rows}
    if set(by_zone) != set(FIELD_GOAL_ZONES):
        return None
    confidence_values = [row.confidence for row in rows if row.confidence is not None]
    status = "unavailable" if any(row.evidence_status == "unavailable" for row in rows) else "provisional"
    if status == "unavailable" or not confidence_values:
        return None
    return DefensiveUnitEvidence(
        team_id=team_id,
        season=season,
        player_ids=unit_state.player_ids,
        zone_tilts={zone: by_zone[zone].zone_concession_tilt for zone in FIELD_GOAL_ZONES},
        confidence=min(confidence_values),
        evidence_status=status,
        matchup_players=tuple(
            DefensiveMatchupPlayer(
                player_id=player_id,
                defensive_disruptor_score=None,
                drop_compatible_big_score=None,
                center_role_share=None,
                height=None,
            )
            for player_id in unit_state.player_ids
        ),
    )


def _score_cell(
    state: SeasonState,
    target: TargetCell,
    *,
    protocol: BacktestProtocol,
) -> BacktestCell | None:
    if target.fga < protocol.minimum_cell_fga:
        return None
    players = _build_offensive_players(
        state,
        target.season,
        target.offense_team_id,
        target.offensive_player_ids,
    )
    unit = _build_defensive_unit(
        state,
        target.season,
        target.defense_team_id,
        target.defensive_lineup_hash,
    )
    if players is None or unit is None:
        return None
    try:
        _lineups, matchup_rows = project_offensive_matrix(
            players,
            [unit],
            season=target.season,
            offense_team_id=target.offense_team_id,
            interval_draws=INTERVAL_DRAW_COUNT,
            interval_seed=INTERVAL_SEED,
        )
        neutral_unit = DefensiveUnitEvidence(
            team_id=unit.team_id,
            season=unit.season,
            player_ids=unit.player_ids,
            zone_tilts={zone: 0.0 for zone in FIELD_GOAL_ZONES},
            confidence=unit.confidence,
            evidence_status=unit.evidence_status,
            matchup_players=unit.matchup_players,
        )
        _neutral_lineups, baseline_rows = project_offensive_matrix(
            players,
            [neutral_unit],
            season=target.season,
            offense_team_id=target.offense_team_id,
            interval_draws=1,
            interval_seed=INTERVAL_SEED,
        )
    except (TypeError, ValueError, FloatingPointError):
        return None
    matchup = matchup_rows[0]
    baseline = baseline_rows[0]
    if (
        matchup.projected_pps is None
        or matchup.interval_low is None
        or matchup.interval_high is None
        or baseline.projected_pps is None
    ):
        return None
    return BacktestCell(
        season=target.season,
        game_id=target.game_id,
        offense_team_id=target.offense_team_id,
        defense_team_id=target.defense_team_id,
        offensive_lineup_hash=target.offensive_lineup_hash,
        defensive_lineup_hash=target.defensive_lineup_hash,
        fga=target.fga,
        observed_pps=target.points / target.fga,
        matchup_pps=matchup.projected_pps,
        baseline_pps=baseline.projected_pps,
        interval_low=matchup.interval_low,
        interval_high=matchup.interval_high,
        evidence_status=matchup.evidence_status,
    )


def _source_digest(
    games: Mapping[int, Game],
    shots_by_game: Mapping[int, list[Shot]],
    lineups_by_game: Mapping[int, list[StoredLineup]],
) -> str:
    digest = hashlib.sha256()
    for game_id in sorted(games):
        game = games[game_id]
        digest.update(f"game:{game.season}:{game_id}:{game.start_date}\n".encode())
        for event in shots_by_game.get(game_id, []):
            digest.update(
                f"shot:{event.source_play_id}:{event.player_id}:{event.team_id}:{event.opponent_id}:{event.made}:{event.location_x}:{event.location_y}:{event.shot_range}\n".encode()
            )
        for lineup in lineups_by_game.get(game_id, []):
            digest.update(
                f"lineup:{lineup.team_id}:{lineup.lineup_hash}:{lineup.total_seconds}:{lineup.possessions}:{lineup.opponent_fga}\n".encode()
            )
    return digest.hexdigest()


def _season_summary(cells: list[BacktestCell], season: int) -> dict[str, Any]:
    rows = [cell for cell in cells if cell.season == season]
    if not rows:
        return {"season": season, "cells": 0, "games": 0, "fga": 0}
    total_fga = sum(cell.fga for cell in rows)
    matchup_error = sum(cell.fga * abs(cell.matchup_pps - cell.observed_pps) for cell in rows) / total_fga
    baseline_error = sum(cell.fga * abs(cell.baseline_pps - cell.observed_pps) for cell in rows) / total_fga
    return {
        "season": season,
        "cells": len(rows),
        "games": len({cell.game_id for cell in rows}),
        "fga": total_fga,
        "matchup_weighted_mae": matchup_error,
        "baseline_weighted_mae": baseline_error,
        "relative_improvement": 1.0 - matchup_error / baseline_error if baseline_error else None,
    }


def run(
    *,
    report_path: Path = DEFAULT_REPORT,
    database_url: str | None = None,
) -> dict[str, Any]:
    protocol = BacktestProtocol()
    load_env_file()
    try:
        import psycopg2
    except ImportError as error:  # pragma: no cover - environment dependent
        raise RuntimeError("psycopg2 is required for the backtest") from error

    conn = psycopg2.connect(database_url or connection_dsn())
    conn.set_session(readonly=True)
    try:
        eligible = _load_eligible_teams(conn, protocol.first_season, protocol.last_season)
        games = _load_games(conn, protocol.first_season, protocol.last_season)
        shots_by_game = _load_shots(conn, protocol.first_season, protocol.last_season)
        lookup, lineups_by_game = _load_lineups(
            conn,
            protocol.first_season,
            protocol.last_season,
            eligible,
        )
        known_player_ids = _load_known_player_ids(conn)
    finally:
        conn.close()

    directions = _directions(shots_by_game)
    states: dict[int, SeasonState] = defaultdict(SeasonState)
    prior_games: dict[tuple[int, int], set[int]] = defaultdict(set)
    cells: list[BacktestCell] = []
    exclusions: dict[str, int] = defaultdict(int)
    considered_games = 0
    warmup_games = 0

    ordered_games = sorted(games.values(), key=lambda game: game.sort_key)
    for game in ordered_games:
        events = shots_by_game.get(game.game_id, [])
        if not events:
            exclusions["games_without_shots"] += 1
            continue
        team_pair = (game.home_team_id, game.away_team_id)
        if not all((team_id, game.season) in eligible for team_id in team_pair):
            exclusions["games_without_two_eligible_teams"] += 1
        else:
            eligible_teams = [team_id for team_id in team_pair]
            ready = all(
                len(prior_games[(game.season, team_id)]) >= protocol.warmup_games
                for team_id in eligible_teams
            )
            if not ready:
                warmup_games += 1
            else:
                considered_games += 1
                targets = _target_cells(
                    game,
                    events,
                    directions=directions,
                    known_player_ids=known_player_ids,
                    lookup=lookup,
                    eligible=eligible,
                    exclusions=exclusions,
                )
                for target in targets:
                    if target.fga < protocol.minimum_cell_fga:
                        exclusions["cells_below_minimum_fga"] += 1
                        continue
                    scored = _score_cell(states[game.season], target, protocol=protocol)
                    if scored is None:
                        exclusions["cells_without_prior_model_inputs"] += 1
                    else:
                        cells.append(scored)
        _update_state(
            states[game.season],
            game,
            events,
            lineups_by_game.get(game.game_id, []),
            directions=directions,
            known_player_ids=known_player_ids,
            lookup=lookup,
        )
        for team_id in team_pair:
            prior_games[(game.season, team_id)].add(game.game_id)

    if not cells:
        raise ValueError("the preregistered backtest produced no scored cells")
    decision = evaluate_gate(cells, protocol=protocol)
    report: dict[str, Any] = {
        "schema_version": 1,
        "dataset": "offensive_projection_backtest",
        "protocol": protocol.as_dict(),
        "source": {
            "tables": [
                "college_games",
                "player_shot_events",
                "team_game_lineups",
                "team_game_lineup_players",
                "team_season_eligibility",
            ],
            "source_sha256": _source_digest(games, shots_by_game, lineups_by_game),
            "read_only": True,
        },
        "coverage": {
            "eligible_team_seasons": len(eligible),
            "final_games": len(games),
            "considered_games_after_warmup": considered_games,
            "warmup_games": warmup_games,
            "scored_cells": len(cells),
            "scored_fga": sum(cell.fga for cell in cells),
        },
        "exclusions": dict(sorted(exclusions.items())),
        "per_season": [
            _season_summary(cells, season)
            for season in range(protocol.first_season, protocol.last_season + 1)
        ],
        "gate": decision.as_dict(),
        "review": {
            "required_reviewer": "GauravR0",
            "status": "pending_independent_review",
            "github_update": "draft_only",
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": decision.status, "report": str(report_path), "reason": decision.reason}, sort_keys=True))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    run(report_path=args.report, database_url=args.database_url)


if __name__ == "__main__":
    main()
