"""Compute the issue #10 defensive-lineup zone-concession artifact.

The command is read-only with respect to PostgreSQL.  It exports a verified
ticket-release source directory; the ticket handler is the only publication
path for ``defense_lineup_zone_concession`` rows.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.defensive_lineup_concession import (
    CONFIDENCE_FLOOR,
    FIELD_GOAL_ZONES,
    LINEUP_SHRINKAGE_PRIOR_ATTEMPTS,
    MINUTES_SHARE_THRESHOLD,
    MODEL_VERSION,
    TEAM_SHRINKAGE_PRIOR_ATTEMPTS,
    DefensiveLineupConcession,
    DefensiveLineupEvidence,
    LeagueSeasonBaseline,
    TeamSeasonBaseline,
    build_concession_profiles,
    canonical_lineup_hash,
)
from bracketballer_data.lineup_attribution import resolve_event_side
from bracketballer_data.shot_zones import ShotCoordinate, classify_shot, infer_attacking_baskets
from scripts.analysis.analyze_projection_viability import _finite, _integer
from scripts.compute.compute_shot_location_profiles import MODEL_CONFIGURATION as PLAYER_MODEL_CONFIGURATION


FIRST_SEASON = 2026
LAST_SEASON = 2026
DATASET = "defense_lineup_zone_concession"
ARTIFACT_FILE = "concessions.jsonl.gz"
METADATA_FILE = "metadata.json"


def model_configuration(version: str = MODEL_VERSION) -> dict[str, Any]:
    """Return the full inactive candidate configuration with T8 settings."""

    configuration = copy.deepcopy(PLAYER_MODEL_CONFIGURATION)
    configuration["model_version"] = version
    configuration["defense_concession"] = {
        "minutes_share_threshold": MINUTES_SHARE_THRESHOLD,
        "team_shrinkage_prior_attempts": TEAM_SHRINKAGE_PRIOR_ATTEMPTS,
        "lineup_shrinkage_prior_attempts": LINEUP_SHRINKAGE_PRIOR_ATTEMPTS,
        "confidence_floor": CONFIDENCE_FLOOR,
        "confidence_formula": (
            "100 * classified_attempts / (classified_attempts + 50) * "
            "min(1, classified_attempts / opponent_field_goals_attempted)"
        ),
        "possessions_source": "team_game_lineups.opponent_stats.possessions",
        "attribution": "exact_valid_five_onfloor_to_same_game_stored_lineup",
        "independent_lineup_audit_passed": False,
        "lineup_profile_seasons": [2026],
        "team_level_fallback_seasons": [2024, 2025],
        "evidence_status_policy": {
            "available": "confidence_at_least_floor_and_independent_audit_passed",
            "provisional": "usable_evidence_but_confidence_floor_or_audit_gate_unmet",
            "unavailable": "no_usable_unit_evidence",
        },
    }
    return configuration


def _nested_number(value: Any, *keys: str) -> float:
    current = value
    for key in keys:
        if not isinstance(current, Mapping):
            return 0.0
        current = current.get(key)
    number = _finite(current)
    return max(number or 0.0, 0.0)


def _direction_inputs(conn: Any, season: int) -> list[ShotCoordinate]:
    with conn.cursor(name=f"issue_0010_direction_{season}") as cursor:
        cursor.itersize = 10_000
        cursor.execute(
            """
            SELECT source_play_id, game_id, team_id, opponent_id, period, location_x
            FROM player_shot_events
            WHERE season = %s
              AND shot_range IS DISTINCT FROM 'free_throw'
            ORDER BY source_play_id
            """,
            (season,),
        )
        return [
            ShotCoordinate(
                event_id=int(source_play_id),
                game_id=int(game_id),
                team_id=_integer(team_id),
                opponent_id=_integer(opponent_id),
                period=_integer(period),
                location_x=_finite(location_x),
            )
            for source_play_id, game_id, team_id, opponent_id, period, location_x in cursor
        ]


def _lineup_rows(
    conn: Any, first: int, last: int
) -> tuple[
    dict[tuple[int, int, int, frozenset[int]], str],
    dict[tuple[int, int, str], DefensiveLineupEvidence],
]:
    """Load stored lineup games and aggregate their exposure statistics."""

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
                     lineup.lineup_hash, lineup.total_seconds, lineup.opponent_stats
            ORDER BY lineup.season, lineup.team_id, lineup.lineup_hash, lineup.game_id
            """,
            (first, last),
        )
        rows = cursor.fetchall()

    lookup: dict[tuple[int, int, int, frozenset[int]], str] = {}
    aggregates: dict[tuple[int, int, str], dict[str, Any]] = {}
    for season, game_id, team_id, lineup_hash, total_seconds, opponent_stats, player_ids in rows:
        sorted_ids, canonical_hash = canonical_lineup_hash(player_ids)
        if str(lineup_hash) != canonical_hash:
            raise ValueError(f"stored lineup hash mismatch: {lineup_hash} != {canonical_hash}")
        key = (int(season), int(game_id), int(team_id), frozenset(sorted_ids))
        existing = lookup.get(key)
        if existing is not None and existing != canonical_hash:
            raise ValueError(f"conflicting stored lineup identity: {key}")
        lookup[key] = canonical_hash
        aggregate_key = (int(season), int(team_id), canonical_hash)
        aggregate = aggregates.setdefault(
            aggregate_key,
            {
                "season": int(season),
                "team_id": int(team_id),
                "lineup_hash": canonical_hash,
                "player_ids": sorted_ids,
                "total_seconds": 0.0,
                "possessions": 0.0,
                "opponent_fga": 0.0,
                "zone_attempts": {zone: 0 for zone in FIELD_GOAL_ZONES},
            },
        )
        aggregate["total_seconds"] += _nested_number(total_seconds)
        aggregate["possessions"] += _nested_number(opponent_stats, "possessions")
        aggregate["opponent_fga"] += _nested_number(
            opponent_stats, "fieldGoals", "attempted"
        )
    evidence = {
        key: DefensiveLineupEvidence(**value) for key, value in aggregates.items()
    }
    return lookup, evidence


def _source_digest(
    *,
    league_counts: Mapping[int, Mapping[str, int]],
    team_counts: Mapping[tuple[int, int], Mapping[str, int]],
    units: Mapping[tuple[int, int, str], DefensiveLineupEvidence],
) -> str:
    digest = hashlib.sha256()
    payload = {
        "league": [
            (season, tuple((zone, counts[zone]) for zone in FIELD_GOAL_ZONES))
            for season, counts in sorted(league_counts.items())
        ],
        "teams": [
            (season, team_id, tuple((zone, counts[zone]) for zone in FIELD_GOAL_ZONES))
            for (season, team_id), counts in sorted(team_counts.items())
        ],
        "units": [
            (
                season,
                team_id,
                lineup_hash,
                unit.total_seconds,
                unit.possessions,
                unit.opponent_fga,
            )
            for (season, team_id, lineup_hash), unit in sorted(units.items())
        ],
    }
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()


def _row_json(row: DefensiveLineupConcession) -> dict[str, Any]:
    return {
        "model_version": MODEL_VERSION,
        "season": row.season,
        "team_id": row.team_id,
        "lineup_hash": row.lineup_hash,
        "lineup_player_ids": list(row.player_ids),
        "zone": row.zone,
        "zone_concession_tilt": row.zone_concession_tilt,
        "possessions": row.possessions,
        "confidence": row.confidence,
        "evidence_status": row.evidence_status,
        "minutes_share": row.minutes_share,
        "classified_attempts": row.classified_attempts,
        "opponent_fga": row.opponent_fga,
        "attribution_coverage": row.attribution_coverage,
    }


def _write_artifact(output_dir: Path, metadata: dict[str, Any], rows: list[DefensiveLineupConcession]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / ARTIFACT_FILE
    with artifact_path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            for row in rows:
                line = json.dumps(_row_json(row), sort_keys=True, separators=(",", ":")) + "\n"
                compressed.write(line.encode("utf-8"))
    (output_dir / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run(
    *,
    first: int = FIRST_SEASON,
    last: int = LAST_SEASON,
    version: str = MODEL_VERSION,
    output_dir: Path | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    if first != 2026 or last != 2026:
        raise ValueError("issue #10 currently publishes provisional lineup profiles for season 2026 only")
    if not version or version != MODEL_VERSION:
        raise ValueError(f"issue #10 requires model version {MODEL_VERSION!r}")
    load_env_file()
    conn = psycopg2.connect(database_url or connection_dsn())
    conn.set_session(readonly=True)
    try:
        known_player_ids = _load_known_player_ids(conn)
        lineup_lookup, units = _lineup_rows(conn, first, last)
        league_counts = {
            season: {zone: 0 for zone in FIELD_GOAL_ZONES}
            for season in range(first, last + 1)
        }
        team_counts: dict[tuple[int, int], dict[str, int]] = defaultdict(
            lambda: {zone: 0 for zone in FIELD_GOAL_ZONES}
        )
        diagnostics: dict[str, Any] = {
            "seasons": {
                str(season): {
                    "field_goal_events": 0,
                    "classified_events": 0,
                    "unclassified_events": 0,
                    "valid_defensive_five_events": 0,
                    "matched_unit_events": 0,
                }
                for season in range(first, last + 1)
            }
        }
        for season in range(first, last + 1):
            directions = infer_attacking_baskets(_direction_inputs(conn, season)).baskets
            with conn.cursor(name=f"issue_0010_events_{season}") as cursor:
                cursor.itersize = 10_000
                cursor.execute(
                    """
                    SELECT source_play_id, game_id, team_id, opponent_id, player_id,
                           period, made, location_x, location_y, raw_payload
                    FROM player_shot_events
                    WHERE season = %s
                      AND shot_range IS DISTINCT FROM 'free_throw'
                    ORDER BY source_play_id
                    """,
                    (season,),
                )
                for (
                    source_play_id,
                    game_id,
                    team_id,
                    opponent_id,
                    player_id,
                    period,
                    made,
                    location_x,
                    location_y,
                    raw_payload,
                ) in cursor:
                    del team_id, player_id, period, made
                    stats = diagnostics["seasons"][str(season)]
                    stats["field_goal_events"] += 1
                    zone = classify_shot(
                        _finite(location_x),
                        _finite(location_y),
                        directions.get(int(source_play_id)) if source_play_id is not None else None,
                    )
                    if zone is None:
                        stats["unclassified_events"] += 1
                        continue
                    stats["classified_events"] += 1
                    league_counts[season][zone] += 1
                    defense_id = _integer(opponent_id)
                    if defense_id is not None:
                        team_counts[(season, defense_id)][zone] += 1
                    if not isinstance(raw_payload, Mapping) or defense_id is None:
                        continue
                    defense = resolve_event_side(raw_payload, "defense", known_player_ids)
                    if not defense.is_valid_five:
                        continue
                    stats["valid_defensive_five_events"] += 1
                    lookup_key = (
                        season,
                        int(game_id),
                        defense_id,
                        defense.resolved_player_ids,
                    )
                    lineup_hash = lineup_lookup.get(lookup_key)
                    if lineup_hash is None:
                        continue
                    stats["matched_unit_events"] += 1
                    units[(season, defense_id, lineup_hash)].zone_attempts[zone] += 1

        league_baselines = [
            LeagueSeasonBaseline(season=season, zone_attempts=counts)
            for season, counts in sorted(league_counts.items())
        ]
        team_baselines = [
            TeamSeasonBaseline(season=season, team_id=team_id, zone_attempts=counts)
            for (season, team_id), counts in sorted(team_counts.items())
            if (season, team_id) in {(key[0], key[1]) for key in units}
        ]
        rows = build_concession_profiles(
            league_baselines,
            team_baselines,
            units.values(),
            minutes_share_threshold=MINUTES_SHARE_THRESHOLD,
            team_shrinkage_prior_attempts=TEAM_SHRINKAGE_PRIOR_ATTEMPTS,
            lineup_shrinkage_prior_attempts=LINEUP_SHRINKAGE_PRIOR_ATTEMPTS,
            confidence_floor=CONFIDENCE_FLOOR,
            independent_audit_passed=False,
        )
    finally:
        conn.close()

    unit_keys = {(row.season, row.team_id, row.lineup_hash) for row in rows}
    status_counts = {
        status: sum(row.evidence_status == status for row in rows)
        for status in ("available", "provisional", "unavailable")
    }
    team_unit_counts: dict[tuple[int, int], int] = defaultdict(int)
    for season, team_id, _lineup_hash in unit_keys:
        team_unit_counts[(season, team_id)] += 1
    metadata: dict[str, Any] = {
        "format_version": 1,
        "dataset": DATASET,
        "model_version": version,
        "first_season": first,
        "last_season": last,
        "configuration": model_configuration(version),
        "source_import_versions": {
            "shots": "issue-0006-shots-2026.1-s2026",
            "ranked_rosters": "ranked-rosters-2026-08-23.2",
            "player_profiles": "issue-0009-shot-location-2026.1",
        },
        "realistic_units": len(unit_keys),
        "defense_lineup_zone_concession": len(rows),
        "teams": len(team_unit_counts),
        "profiles_with_five_zones": len(unit_keys),
        "status_counts": status_counts,
        "units_per_team": {
            f"{season}:{team_id}": count
            for (season, team_id), count in sorted(team_unit_counts.items())
        },
        "diagnostics": diagnostics,
        "source_sha256": _source_digest(
            league_counts=league_counts,
            team_counts=team_counts,
            units=units,
        ),
    }
    if output_dir is not None:
        _write_artifact(output_dir, metadata, rows)
    return {"status": "dry_run", **metadata}


def _load_known_player_ids(conn: Any) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM players")
        return {int(row[0]) for row in cursor.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--version", default=MODEL_VERSION)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                first=args.first,
                last=args.last,
                version=args.version,
                output_dir=args.output_dir,
                database_url=args.database_url,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
