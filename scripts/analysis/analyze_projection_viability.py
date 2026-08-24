"""Run issue #8's sample-size and projected-spread viability gate.

The command is intentionally read-only.  It reads the already published
2024+ shot, roster, and lineup evidence from PostgreSQL and writes only an
aggregate JSON report below ``data/reports``.

Example::

    PYTHONPATH=src python -m scripts.analysis.analyze_projection_viability \
        --first 2024 --last 2026 --projection-season 2026 \
        --anchor-team 340 --bootstrap-replicates 2000
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import numpy as np

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.lineup_attribution import resolve_event_side
from bracketballer_data.projection_viability import (
    DEFAULT_LAMBDA,
    FIELD_GOAL_ZONES,
    bootstrap_gate,
    lineups_for_rotation,
    median_zone_attempts,
    projected_lineups,
)
from bracketballer_data.shot_zones import (
    ShotCoordinate,
    classify_shot,
    infer_attacking_baskets,
)


FIRST_SEASON = 2024
LAST_SEASON = 2026
PROJECTION_SEASON = 2026
ANCHOR_TEAM = 340
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20260824
DEFAULT_REPORT = Path("data/reports/projection-viability/issue-0008.json")
FREE_THROW_RANGE = "free_throw"
ZONE_INDEX = {zone: index for index, zone in enumerate(FIELD_GOAL_ZONES)}


@dataclass(frozen=True, slots=True)
class RotationPlayer:
    player_id: int
    team_id: int
    season: int
    name: str
    minutes: int
    games: int
    field_goals_attempted: int


@dataclass(frozen=True, slots=True)
class ShotRow:
    source_play_id: int
    season: int
    game_id: int
    team_id: int | None
    opponent_id: int | None
    player_id: int
    period: int | None
    made: bool
    location_x: float | None
    location_y: float | None
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LineupGame:
    season: int
    game_id: int
    team_id: int
    lineup_hash: str
    player_ids: frozenset[int]
    total_seconds: float
    opponent_fga: float


@dataclass
class LineupAggregate:
    season: int
    team_id: int
    lineup_hash: str
    player_ids: frozenset[int]
    total_seconds: float = 0.0
    opponent_fga: float = 0.0
    games: list[int] = field(default_factory=list)


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
    number = _finite(current)
    return max(number or 0.0, 0.0)


def load_rotation_players(
    conn: Any, first: int, last: int
) -> dict[tuple[int, int], RotationPlayer]:
    """Load the T3 rotation population without widening the fantasy catalog."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ps.player_id, ps.team_id, ps.season,
                   COALESCE(p.name, 'player-' || ps.player_id::text),
                   COALESCE(ps.minutes, 0), COALESCE(ps.games, 0),
                   COALESCE(ps.field_goals_attempted, 0)
            FROM player_seasons ps
            JOIN team_roster_memberships membership
              ON membership.player_id = ps.player_id
             AND membership.team_id = ps.team_id
             AND membership.season = ps.season
             AND membership.source_active
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = ps.team_id
             AND eligibility.season = ps.season
            LEFT JOIN players p ON p.id = ps.player_id
            WHERE ps.season BETWEEN %s AND %s
              AND ps.source_active
              AND ps.minutes >= 100
            ORDER BY ps.season, ps.team_id, ps.player_id
            """,
            (first, last),
        )
        rows = cursor.fetchall()
    output: dict[tuple[int, int], RotationPlayer] = {}
    for player_id, team_id, season, name, minutes, games, fga in rows:
        key = (int(season), int(player_id))
        if key in output:
            raise ValueError(f"duplicate eligible rotation player {key}")
        output[key] = RotationPlayer(
            player_id=int(player_id),
            team_id=int(team_id),
            season=int(season),
            name=str(name),
            minutes=int(minutes),
            games=int(games),
            field_goals_attempted=int(fga),
        )
    return output


def load_known_player_ids(conn: Any) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM players")
        return {int(row[0]) for row in cursor.fetchall()}


def load_lineups(
    conn: Any, first: int, last: int
) -> tuple[list[LineupGame], dict[tuple[int, int, frozenset[int]], str]]:
    """Load valid five-player lineup games and their exact-match lookup."""

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
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = lineup.team_id
             AND eligibility.season = lineup.season
            WHERE lineup.season BETWEEN %s AND %s
            GROUP BY lineup.id, lineup.season, lineup.game_id, lineup.team_id,
                     lineup.lineup_hash, lineup.total_seconds,
                     lineup.opponent_stats
            ORDER BY lineup.season, lineup.team_id, lineup.lineup_hash,
                     lineup.game_id
            """,
            (first, last),
        )
        rows = cursor.fetchall()

    games: list[LineupGame] = []
    lookup: dict[tuple[int, int, frozenset[int]], str] = {}
    for season, game_id, team_id, lineup_hash, seconds, opponent_stats, player_ids in rows:
        players = frozenset(int(player_id) for player_id in player_ids)
        if len(players) != 5:
            continue
        row = LineupGame(
            season=int(season),
            game_id=int(game_id),
            team_id=int(team_id),
            lineup_hash=str(lineup_hash),
            player_ids=players,
            total_seconds=float(seconds or 0.0),
            opponent_fga=_nested_number(opponent_stats, "fieldGoals", "attempted"),
        )
        games.append(row)
        lookup.setdefault((row.season, row.game_id, row.team_id, players), row.lineup_hash)
    return games, lookup


def load_direction_inputs(
    conn: Any, first: int, last: int
) -> list[ShotCoordinate]:
    """Read only the lightweight fields needed for direction inference."""

    with conn.cursor(name="issue_0008_direction") as cursor:
        cursor.itersize = 10000
        cursor.execute(
            """
            SELECT source_play_id, season, game_id, team_id, opponent_id,
                   player_id, period, location_x
            FROM player_shot_events
            WHERE season BETWEEN %s AND %s
              AND shot_range IS DISTINCT FROM %s
            ORDER BY source_play_id
            """,
            (first, last, FREE_THROW_RANGE),
        )
        coordinates: list[ShotCoordinate] = []
        for row in cursor:
            source_id, season, game_id, team_id, opponent_id, player_id, period, x = row
            event_id = int(source_id)
            team = _integer(team_id)
            opponent = _integer(opponent_id)
            period_number = _integer(period)
            x_value = _finite(x)
            coordinates.append(
                ShotCoordinate(event_id, int(game_id), team, opponent, period_number, x_value)
            )
    return coordinates


def iter_database_shots(conn: Any, season: int) -> Iterator[ShotRow]:
    """Stream one season's raw payloads after direction is known."""

    with conn.cursor(name=f"issue_0008_shots_{season}") as cursor:
        cursor.itersize = 10000
        cursor.execute(
            """
            SELECT source_play_id, season, game_id, team_id, opponent_id,
                   player_id, period, made, location_x, location_y, raw_payload
            FROM player_shot_events
            WHERE season = %s
              AND shot_range IS DISTINCT FROM %s
            ORDER BY source_play_id
            """,
            (season, FREE_THROW_RANGE),
        )
        for row in cursor:
            source_id, row_season, game_id, team_id, opponent_id, player_id, period, made, x, y, raw = row
            yield ShotRow(
                int(source_id),
                int(row_season),
                int(game_id),
                _integer(team_id),
                _integer(opponent_id),
                int(player_id),
                _integer(period),
                bool(made),
                _finite(x),
                _finite(y),
                raw if isinstance(raw, Mapping) else {},
            )


def _top_lineups(
    lineup_games: Iterable[LineupGame],
) -> tuple[dict[tuple[int, int, str], LineupAggregate], dict[tuple[int, int], list[LineupAggregate]]]:
    aggregates: dict[tuple[int, int, str], LineupAggregate] = {}
    for row in lineup_games:
        key = (row.season, row.team_id, row.lineup_hash)
        aggregate = aggregates.setdefault(
            key,
            LineupAggregate(row.season, row.team_id, row.lineup_hash, row.player_ids),
        )
        if aggregate.player_ids != row.player_ids:
            raise ValueError(f"lineup hash maps to conflicting player sets: {key}")
        aggregate.total_seconds += row.total_seconds
        aggregate.opponent_fga += row.opponent_fga
        aggregate.games.append(row.game_id)
    by_team: dict[tuple[int, int], list[LineupAggregate]] = defaultdict(list)
    for aggregate in aggregates.values():
        by_team[(aggregate.season, aggregate.team_id)].append(aggregate)
    top_by_team: dict[tuple[int, int], list[LineupAggregate]] = {}
    for key, rows in by_team.items():
        rows.sort(key=lambda row: (-row.total_seconds, row.lineup_hash))
        top_by_team[key] = rows[:10]
    return aggregates, top_by_team


def _empty_zone() -> np.ndarray:
    return np.zeros(len(FIELD_GOAL_ZONES), dtype=float)


def _event_zone(event: ShotRow, baskets: Mapping[int, Any]) -> str | None:
    return classify_shot(event.location_x, event.location_y, baskets.get(event.source_play_id))


def _percentile_summary(values: np.ndarray) -> dict[str, float | int | None]:
    if values.size == 0:
        return {"count": 0, "median": None, "p05": None, "p95": None}
    p05, median, p95 = np.quantile(values, (0.05, 0.5, 0.95))
    return {
        "count": int(values.size),
        "median": float(median),
        "p05": float(p05),
        "p95": float(p95),
    }


def run(
    *,
    first: int = FIRST_SEASON,
    last: int = LAST_SEASON,
    projection_season: int = PROJECTION_SEASON,
    anchor_team: int = ANCHOR_TEAM,
    report_path: Path = DEFAULT_REPORT,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    database_url: str | None = None,
) -> dict[str, Any]:
    if first > last:
        raise ValueError("first season cannot exceed last season")
    if not first <= projection_season <= last:
        raise ValueError("projection season must be inside the requested range")
    load_env_file()
    try:
        import psycopg2
    except ImportError as error:  # pragma: no cover - environment dependent
        raise RuntimeError("psycopg2 is required for the viability analysis") from error

    conn = psycopg2.connect(database_url or connection_dsn())
    conn.set_session(readonly=True)
    rotation = load_rotation_players(conn, first, last)
    known_player_ids = load_known_player_ids(conn)
    lineup_games, lineup_lookup = load_lineups(conn, first, last)
    anchor_rotation_keys = {
        key
        for key, row in rotation.items()
        if row.season == projection_season and row.team_id == anchor_team
    }
    player_counts: dict[tuple[int, int], np.ndarray] = {
        key: _empty_zone() for key in rotation
    }
    player_makes: dict[tuple[int, int], np.ndarray] = {
        key: _empty_zone() for key in rotation
    }
    player_game_counts: dict[tuple[int, int], dict[int, tuple[np.ndarray, np.ndarray]]] = defaultdict(dict)
    league_counts: dict[int, np.ndarray] = defaultdict(_empty_zone)
    league_makes: dict[int, np.ndarray] = defaultdict(_empty_zone)
    unit_game_counts: dict[tuple[int, int, str], dict[int, tuple[np.ndarray, np.ndarray]]] = defaultdict(dict)
    unit_counts: dict[tuple[int, int, str], np.ndarray] = defaultdict(_empty_zone)
    coverage: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    try:
        for season in range(first, last + 1):
            direction_rows = load_direction_inputs(conn, season, season)
            directions = infer_attacking_baskets(direction_rows)
            for event in iter_database_shots(conn, season):
                coverage[season]["field_goal_events"] += 1
                zone = _event_zone(event, directions.baskets)
                if zone is None:
                    coverage[season]["unclassified_events"] += 1
                    continue
                coverage[season]["classified_events"] += 1
                zone_index = ZONE_INDEX[zone]
                league_counts[season][zone_index] += 1
                if event.made:
                    league_makes[season][zone_index] += 1

                player_key = (season, event.player_id)
                if player_key in rotation:
                    player_counts[player_key][zone_index] += 1
                    if event.made:
                        player_makes[player_key][zone_index] += 1
                    if player_key in anchor_rotation_keys:
                        game_attempts, game_makes = player_game_counts[player_key].setdefault(
                            event.game_id, (_empty_zone(), _empty_zone())
                        )
                        game_attempts[zone_index] += 1
                        if event.made:
                            game_makes[zone_index] += 1

                defense = resolve_event_side(event.raw_payload, "defense", known_player_ids)
                if defense.is_valid_five and event.opponent_id is not None:
                    lineup_hash = lineup_lookup.get(
                        (season, event.game_id, event.opponent_id, defense.resolved_player_ids)
                    )
                    if lineup_hash is not None:
                        unit_key = (season, event.opponent_id, lineup_hash)
                        unit_counts[unit_key][zone_index] += 1
                        if season == projection_season:
                            game_attempts, game_makes = unit_game_counts[unit_key].setdefault(
                                event.game_id, (_empty_zone(), _empty_zone())
                            )
                            game_attempts[zone_index] += 1
                            if event.made:
                                game_makes[zone_index] += 1
                        coverage[season]["defense_matched_events"] += 1
                if defense.is_valid_five:
                    coverage[season]["defense_valid_five_events"] += 1
                else:
                    coverage[season]["defense_unresolved_events"] += 1
    finally:
        conn.close()

    medians = median_zone_attempts(
        (season, player_id, player_counts[(season, player_id)])
        for season, player_id in sorted(rotation)
    )
    aggregates, top_by_team = _top_lineups(lineup_games)

    defensive_medians: dict[int, dict[str, float | int | None]] = {}
    top_unit_report: dict[int, list[dict[str, Any]]] = {}
    for season in range(first, last + 1):
        units = [
            unit
            for key, rows in top_by_team.items()
            if key[0] == season
            for unit in rows
        ]
        values = np.asarray([unit.opponent_fga for unit in units], dtype=float)
        defensive_medians[season] = _percentile_summary(values)
        top_unit_report[season] = [
            {
                "team_id": unit.team_id,
                "lineup_hash": unit.lineup_hash,
                "total_seconds": unit.total_seconds,
                "opponent_fga": unit.opponent_fga,
                "classified_fga": int(unit_counts[(season, unit.team_id, unit.lineup_hash)].sum()),
            }
            for key, rows in sorted(top_by_team.items())
            if key[0] == season
            for unit in rows
        ]

    anchor_players = [
        row for key, row in sorted(rotation.items())
        if row.season == projection_season and row.team_id == anchor_team
    ]
    if len(anchor_players) != 10:
        raise ValueError(
            f"expected exactly ten anchor rotation players, found {len(anchor_players)}"
        )
    player_ids = [row.player_id for row in anchor_players]
    lineups = lineups_for_rotation(player_ids)
    player_index = {player_id: index for index, player_id in enumerate(player_ids)}
    lineup_indices = np.asarray(
        [[player_index[player_id] for player_id in lineup] for lineup in lineups], dtype=int
    )
    player_attempt_array = np.asarray(
        [player_counts[(projection_season, player_id)] for player_id in player_ids], dtype=float
    )
    player_make_array = np.asarray(
        [player_makes[(projection_season, player_id)] for player_id in player_ids], dtype=float
    )
    season_league_counts = league_counts[projection_season]
    season_league_makes = league_makes[projection_season]
    league_share = season_league_counts / max(float(season_league_counts.sum()), 1.0)
    league_accuracy = np.divide(
        season_league_makes,
        np.maximum(season_league_counts, 1.0),
    )
    usage_rates = np.asarray(
        [
            max(
                float(row.field_goals_attempted) / max(float(row.minutes), 1.0),
                1e-6,
            )
            for row in anchor_players
        ],
        dtype=float,
    )

    projection_units: list[LineupAggregate] = [
        unit
        for key, rows in sorted(top_by_team.items())
        if key[0] == projection_season and key[1] != anchor_team
        for unit in rows
    ]
    if not projection_units:
        raise ValueError("no eligible defensive units are available for projection")
    defense_attempt_array = np.asarray(
        [
            sum(
                (
                    unit_game_counts[(projection_season, unit.team_id, unit.lineup_hash)].get(
                        game, (_empty_zone(), _empty_zone())
                    )[0]
                    for game in unit.games
                ),
                _empty_zone(),
            )
            for unit in projection_units
        ],
        dtype=float,
    )
    _, direct_spreads = projected_lineups(
        player_attempt_array,
        player_make_array,
        defense_attempt_array,
        lineup_indices,
        usage_rates,
        league_share=league_share,
        league_accuracy=league_accuracy,
        coefficient=DEFAULT_LAMBDA,
    )

    player_bootstrap_rows = []
    for player_id in player_ids:
        rows = list(player_game_counts[(projection_season, player_id)].values())
        if not rows:
            rows = [(_empty_zone(), _empty_zone())]
        player_bootstrap_rows.append(rows)
    defense_bootstrap_rows = []
    for unit in projection_units:
        rows = [
            unit_game_counts[(projection_season, unit.team_id, unit.lineup_hash)].get(
                game, (_empty_zone(), _empty_zone())
            )
            for game in unit.games
        ]
        if not rows:
            rows = [(_empty_zone(), _empty_zone())]
        defense_bootstrap_rows.append(rows)
    gate = bootstrap_gate(
        player_bootstrap_rows,
        defense_bootstrap_rows,
        lineup_indices,
        usage_rates,
        league_share=league_share,
        league_accuracy=league_accuracy,
        coefficient=DEFAULT_LAMBDA,
        replicates=bootstrap_replicates,
        seed=bootstrap_seed,
    )

    report: dict[str, Any] = {
        "schema_version": 1,
        "source": "player_shot_events and team_game_lineups",
        "first_season": first,
        "last_season": last,
        "projection_season": projection_season,
        "anchor_team_id": anchor_team,
        "zone_order": list(FIELD_GOAL_ZONES),
        "lambda": DEFAULT_LAMBDA,
        "rotation_definition": "eligible active roster membership with player_seasons.minutes >= 100",
        "rotation_player_count_by_season": {
            str(season): sum(row.season == season for row in rotation.values())
            for season in range(first, last + 1)
        },
        "median_fga_by_zone": {
            str(season): {
                zone: value for zone, value in zip(FIELD_GOAL_ZONES, values)
            }
            for season, values in medians.items()
        },
        "median_opponent_fga_top_ten_units": {
            str(season): values for season, values in defensive_medians.items()
        },
        "top_ten_unit_count_by_season": {
            str(season): len(rows) for season, rows in top_unit_report.items()
        },
        "top_unit_sample": {
            str(season): rows[:20] for season, rows in top_unit_report.items()
        },
        "coverage": {
            str(season): dict(values) for season, values in sorted(coverage.items())
        },
        "projection": {
            "anchor_players": [
                {
                    "player_id": row.player_id,
                    "name": row.name,
                    "minutes": row.minutes,
                    "games": row.games,
                    "field_goals_attempted": row.field_goals_attempted,
                }
                for row in anchor_players
            ],
            "lineup_count": len(lineups),
            "defensive_unit_count": len(projection_units),
            "direct_spread": _percentile_summary(direct_spreads),
            "defensive_units_with_zero_classified_fga": int(
                np.sum(defense_attempt_array.sum(axis=1) == 0)
            ),
        },
        "bootstrap": {
            "replicates": bootstrap_replicates,
            "seed": bootstrap_seed,
            "observed_median_spread": gate.observed_median,
            "observed_95_percent_interval": [gate.observed_low, gate.observed_high],
            "null_median_spread": gate.null_median,
            "null_95_percentile": gate.null_high,
            "decision_rule": "GO if observed 95% lower bound > null 95th percentile; NO-GO if observed 95% upper bound <= null 95th percentile; otherwise SHIP AS EXPLORATION TOOL",
            "recommendation": gate.recommendation,
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote issue #8 viability report -> {report_path}")
    print(f"anchor rotation={len(anchor_players)} players; lineups={len(lineups)}")
    print(f"defensive units={len(projection_units)}")
    print(
        f"observed median spread={gate.observed_median:.6f} "
        f"95%=[{gate.observed_low:.6f}, {gate.observed_high:.6f}]"
    )
    print(f"null 95th percentile={gate.null_high:.6f}")
    print(f"recommendation={gate.recommendation}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--projection-season", type=int, default=PROJECTION_SEASON)
    parser.add_argument("--anchor-team", type=int, default=ANCHOR_TEAM)
    parser.add_argument("--bootstrap-replicates", type=int, default=BOOTSTRAP_REPLICATES)
    parser.add_argument("--bootstrap-seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    run(
        first=args.first,
        last=args.last,
        projection_season=args.projection_season,
        anchor_team=args.anchor_team,
        report_path=args.report,
        bootstrap_replicates=args.bootstrap_replicates,
        bootstrap_seed=args.bootstrap_seed,
        database_url=args.database_url,
    )


if __name__ == "__main__":
    main()
