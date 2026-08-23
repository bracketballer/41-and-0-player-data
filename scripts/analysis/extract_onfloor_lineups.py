"""T1: extract and validate on-floor lineup state from stored shot payloads.

Reads `player_shot_events.raw_payload` for a season range, resolves each
event's on-floor athlete ids against `players`, and compares the resolved
defensive five-man set against `team_game_lineups` /
`team_game_lineup_players`. Produces per-season coverage and disagreement
statistics for bracketballer/41-and-0-player-data#4.

Coverage and disagreement are reported for the defensive side (the team
*not* shooting) since that is what the issue's "attributing shots to
specific defensive lineups" deliverable needs; offensive-side coverage is
included as supporting context.

Example:
    python -m scripts.analysis.extract_onfloor_lineups
    python -m scripts.analysis.extract_onfloor_lineups --first 2024 --last 2026 \
        --report data/reports/lineups/onfloor_coverage.csv
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.lineup_attribution import (
    CoverageStat,
    DisagreementStat,
    compute_coverage_stats,
    compute_disagreement_stats,
    resolve_event_side,
)

FIRST_SEASON = 2024
LAST_SEASON = 2026
DEFAULT_REPORT = Path("data/reports/lineups/onfloor_coverage.csv")


def load_known_player_ids(conn: Any) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM players")
        return {row[0] for row in cursor.fetchall()}


def load_stored_lineups(
    conn: Any, first: int, last: int
) -> dict[tuple[int, int], list[frozenset[int]]]:
    """Map (game_id, team_id) -> the distinct 5-man sets recorded in
    team_game_lineups for that game/team, across the season range."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT lineup.id, lineup.game_id, lineup.team_id, lineup_player.player_id
            FROM team_game_lineups lineup
            JOIN team_game_lineup_players lineup_player
              ON lineup_player.lineup_id = lineup.id
            WHERE lineup.season BETWEEN %s AND %s
            """,
            (first, last),
        )
        rows = cursor.fetchall()

    players_by_lineup: dict[int, set[int]] = defaultdict(set)
    key_by_lineup: dict[int, tuple[int, int]] = {}
    for lineup_id, game_id, team_id, player_id in rows:
        players_by_lineup[lineup_id].add(player_id)
        key_by_lineup[lineup_id] = (game_id, team_id)

    stored: dict[tuple[int, int], list[frozenset[int]]] = defaultdict(list)
    for lineup_id, player_ids in players_by_lineup.items():
        stored[key_by_lineup[lineup_id]].append(frozenset(player_ids))
    return stored


def iter_shot_events(conn: Any, first: int, last: int):
    with conn.cursor(name="onfloor_shot_events") as cursor:
        cursor.itersize = 5000
        cursor.execute(
            """
            SELECT season, game_id, team_id, opponent_id, raw_payload
            FROM player_shot_events
            WHERE season BETWEEN %s AND %s
            """,
            (first, last),
        )
        for row in cursor:
            yield row


def write_report(
    report_path: Path,
    defense_coverage: dict[int, CoverageStat],
    offense_coverage: dict[int, CoverageStat],
    disagreement: dict[int, DisagreementStat],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    seasons = sorted(set(defense_coverage) | set(offense_coverage) | set(disagreement))
    with report_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "season",
                "defense_events",
                "defense_valid_five",
                "defense_coverage_pct",
                "offense_events",
                "offense_valid_five",
                "offense_coverage_pct",
                "comparable_games",
                "disagreements",
                "disagreement_rate_pct",
                "no_reference_count",
            ]
        )
        for season in seasons:
            cov = defense_coverage.get(season)
            ocov = offense_coverage.get(season)
            dis = disagreement.get(season)
            writer.writerow(
                [
                    season,
                    cov.team_sides_seen if cov else 0,
                    cov.valid_five_count if cov else 0,
                    _pct(cov.coverage_pct if cov else None),
                    ocov.team_sides_seen if ocov else 0,
                    ocov.valid_five_count if ocov else 0,
                    _pct(ocov.coverage_pct if ocov else None),
                    dis.comparable_count if dis else 0,
                    dis.disagreement_count if dis else 0,
                    _pct(dis.disagreement_rate_pct if dis else None),
                    dis.no_reference_count if dis else 0,
                ]
            )


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def run(first: int, last: int, report_path: Path) -> None:
    load_env_file()
    conn = psycopg2.connect(connection_dsn())
    try:
        known_player_ids = load_known_player_ids(conn)
        stored_lineups = load_stored_lineups(conn, first, last)

        defense_inputs = []
        offense_inputs = []
        disagreement_inputs = []
        event_count = 0

        for season, game_id, team_id, opponent_id, raw_payload in iter_shot_events(
            conn, first, last
        ):
            event_count += 1
            defense = resolve_event_side(raw_payload, "defense", known_player_ids)
            offense = resolve_event_side(raw_payload, "offense", known_player_ids)
            defense_inputs.append((season, defense))
            offense_inputs.append((season, offense))
            if defense.is_valid_five:
                stored = stored_lineups.get((game_id, opponent_id))
                disagreement_inputs.append((season, defense.resolved_player_ids, stored))
    finally:
        conn.close()

    defense_coverage = compute_coverage_stats(defense_inputs)
    offense_coverage = compute_coverage_stats(offense_inputs)
    disagreement = compute_disagreement_stats(disagreement_inputs)

    write_report(report_path, defense_coverage, offense_coverage, disagreement)

    print(f"Processed {event_count} shot events ({first}-{last}); report -> {report_path}")
    for season in sorted(defense_coverage):
        cov = defense_coverage[season]
        pct = _pct(cov.coverage_pct) or "n/a"
        print(
            f"  season {season}: defensive coverage {pct}% "
            f"({cov.valid_five_count}/{cov.team_sides_seen} events)"
        )
        dis = disagreement.get(season)
        if dis is not None:
            dis_pct = _pct(dis.disagreement_rate_pct) or "n/a"
            print(
                f"    disagreement vs team_game_lineups: {dis_pct}% "
                f"({dis.disagreement_count}/{dis.comparable_count} comparable; "
                f"{dis.no_reference_count} had no stored lineup to compare against)"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    run(args.first, args.last, args.report)


if __name__ == "__main__":
    main()
