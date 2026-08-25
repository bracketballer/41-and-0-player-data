"""Compute the pregame offensive-lineup projection matrix.

The command is a dry run unless ``--apply`` is supplied.  ``--output-dir``
emits the immutable JSONL artifact consumed by the issue #13 ticket handler;
``--apply`` publishes the same rows directly with an audited transaction.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.defensive_lineup_concession import EVIDENCE_STATUSES
from bracketballer_data.import_audit import begin_import_run, mark_failed, mark_validated, pipeline_commit
from bracketballer_data.matchup_assignment import DefensiveMatchupPlayer
from bracketballer_data.offensive_projection import (
    CONFIDENCE_FLOOR,
    INTERVAL_DRAW_COUNT,
    INTERVAL_SEED,
    MODEL_VERSION,
    OffensivePlayerEvidence,
    DefensiveUnitEvidence,
    ProjectionRow,
    V1_DEFENSE_TILT_STANDARD_ERROR,
    V1_USAGE_EFFICIENCY_STANDARD_ERROR,
    project_offensive_matrix,
)
from bracketballer_data.paths import REPO_ROOT
from bracketballer_data.shot_location_profiles import FIELD_GOAL_ZONES, ShotLocationProfile
from bracketballer_data.usage_efficiency import V1_USAGE_EFFICIENCY_SLOPE


DATASET = "lineup_offensive_projections"
ARTIFACT_FILE = "projections.jsonl.gz"
METADATA_FILE = "metadata.json"


def model_configuration(version: str = MODEL_VERSION) -> dict[str, Any]:
    return {
        "model_version": version,
        "projection": {
            "formula": "softmax(log(base_share) + lambda * defense_tilt)",
            "lambda_model": "global",
            "lambda_coefficients": {zone: 0.4620905146480455 for zone in FIELD_GOAL_ZONES},
            "lambda_standard_error": V1_DEFENSE_TILT_STANDARD_ERROR,
            "usage_coefficient": V1_USAGE_EFFICIENCY_SLOPE,
            "usage_standard_error": V1_USAGE_EFFICIENCY_STANDARD_ERROR,
            "usage_delta_cap": 5.0,
            "interval": {
                "method": "posterior_simulation",
                "draws": INTERVAL_DRAW_COUNT,
                "seed": INTERVAL_SEED,
                "quantiles": [0.025, 0.975],
            },
            "confidence": {
                "floor": CONFIDENCE_FLOOR,
                "player_reliability": "100 * attempts / (attempts + 50)",
                "aggregation": "minimum_defense_and_five_offensive_players",
            },
            "assignment": "heuristic-matchup-v1_single_deterministic_assignment",
            "precompute": "pregame opponent selection after nightly profile refresh",
            "activation": "inactive_until_backtest_gate",
        },
    }


def _number(value: Any, *, name: str, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _load_offensive_players(conn: Any, season: int, team_id: int, model_version: str) -> list[OffensivePlayerEvidence]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT profile.player_id, profile.zone, profile.attempts,
                   profile.makes, profile.attempt_share,
                   profile.posterior_alpha, profile.posterior_beta,
                   profile.adjusted_pps, ps.usage, membership.height
            FROM player_shot_location_profiles profile
            JOIN player_seasons ps
              ON ps.player_id = profile.player_id AND ps.season = profile.season
             AND ps.team_id = %s AND ps.source_active
            JOIN team_roster_memberships membership
              ON membership.player_id = profile.player_id
             AND membership.team_id = ps.team_id AND membership.season = ps.season
             AND membership.source_active
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = ps.team_id AND eligibility.season = ps.season
            WHERE profile.model_version = %s AND profile.season = %s
              AND ps.minutes >= 100
            ORDER BY profile.player_id, profile.zone
            """,
            (team_id, model_version, season),
        )
        profile_rows = cursor.fetchall()
        cursor.execute(
            """
            WITH eligible AS (
                SELECT id AS player_id,
                       40.0 * SUM(COALESCE(assists, 0)) / NULLIF(SUM(minutes), 0) AS assists_per40,
                       CASE WHEN SUM(COALESCE(turnovers, 0)) > 0
                            THEN SUM(COALESCE(assists, 0))::float8 / SUM(turnovers)
                            ELSE SUM(COALESCE(assists, 0))::float8 END AS assist_turnover_ratio,
                       SUM(COALESCE(usage, 0) * COALESCE(minutes, 0))
                         / NULLIF(SUM(minutes), 0) AS usage
                FROM players
                WHERE season = %s AND source_active
                  AND porpag > 0 AND dporpag > 0
                GROUP BY id
                HAVING SUM(COALESCE(games, 0)) > 18
            ), ranked AS (
                SELECT player_id,
                       100 * percent_rank() OVER (ORDER BY assists_per40) AS assists_percentile,
                       100 * percent_rank() OVER (ORDER BY assist_turnover_ratio) AS assist_turnover_percentile,
                       100 * percent_rank() OVER (ORDER BY usage) AS usage_percentile
                FROM eligible
            )
            SELECT player_id,
                   0.50 * assists_percentile + 0.30 * assist_turnover_percentile
                   + 0.20 * usage_percentile AS handler_score
            FROM ranked
            """,
            (season,),
        )
        handler_scores = {int(player_id): float(score) for player_id, score in cursor.fetchall()}

    grouped: dict[int, dict[str, Any]] = {}
    for row in profile_rows:
        player_id, zone, attempts, makes, share, alpha, beta, adjusted_pps, usage, height = row
        entry = grouped.setdefault(
            int(player_id),
            {"usage": _number(usage, name="usage"), "height": _number(height, name="height"), "profiles": {}},
        )
        if entry["usage"] is None or entry["usage"] <= 0:
            raise ValueError(f"player {player_id} has unavailable usage")
        entry["profiles"][str(zone)] = ShotLocationProfile(
            player_id=int(player_id),
            season=season,
            zone=str(zone),
            attempts=int(attempts),
            makes=int(makes),
            attempt_share=float(share),
            posterior_alpha=float(alpha),
            posterior_beta=float(beta),
            adjusted_pps=float(adjusted_pps),
        )
    players: list[OffensivePlayerEvidence] = []
    for player_id, entry in sorted(grouped.items()):
        if set(entry["profiles"]) != set(FIELD_GOAL_ZONES):
            raise ValueError(f"player {player_id} does not have five location profiles")
        players.append(
            OffensivePlayerEvidence(
                player_id=player_id,
                usage=float(entry["usage"]),
                handler_score=handler_scores.get(player_id, 0.0),
                height=entry["height"],
                profiles=entry["profiles"],
            )
        )
    if len(players) < 5:
        raise ValueError("offense team has fewer than five eligible rotation players")
    return players


def _load_defensive_units(conn: Any, season: int, team_id: int, model_version: str) -> list[DefensiveUnitEvidence]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT team_id, season, lineup_hash, lineup_player_ids, zone,
                   zone_concession_tilt, confidence, evidence_status
            FROM defense_lineup_zone_concession
            WHERE team_id = %s AND season = %s AND model_version = %s
            ORDER BY lineup_hash, zone
            """,
            (team_id, season, model_version),
        )
        concession_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT pcs.player_id, pcs.characteristic_key, pcs.score,
                   membership.height
            FROM player_characteristic_scores pcs
            LEFT JOIN team_roster_memberships membership
              ON membership.player_id = pcs.player_id
             AND membership.team_id = pcs.team_id AND membership.season = pcs.season
             AND membership.source_active
            JOIN defensive_model_versions version
              ON version.version = pcs.model_version AND version.is_active
            WHERE pcs.team_id = %s AND pcs.season = %s
              AND pcs.characteristic_key IN ('DEFENSIVE_DISRUPTOR', 'DROP_COMPATIBLE_BIG')
            ORDER BY pcs.player_id, pcs.characteristic_key
            """,
            (team_id, season),
        )
        characteristic_rows = cursor.fetchall()

    by_unit: dict[str, dict[str, Any]] = {}
    for row in concession_rows:
        unit_team, unit_season, lineup_hash, player_ids, zone, tilt, confidence, status = row
        entry = by_unit.setdefault(
            str(lineup_hash),
            {
                "team_id": int(unit_team),
                "season": int(unit_season),
                "player_ids": tuple(int(value) for value in player_ids),
                "tilts": {},
                "confidence": confidence,
                "status": str(status),
            },
        )
        entry["tilts"][str(zone)] = None if tilt is None else float(tilt)
        if confidence is not None:
            entry["confidence"] = (
                float(confidence)
                if entry["confidence"] is None
                else min(float(entry["confidence"]), float(confidence))
            )
        if status == "unavailable":
            entry["status"] = "unavailable"
        elif entry["status"] == "available" and status == "provisional":
            entry["status"] = "provisional"

    characteristics: dict[int, dict[str, float]] = defaultdict(dict)
    heights: dict[int, float | None] = {}
    for player_id, key, score, height in characteristic_rows:
        characteristics[int(player_id)][str(key)] = float(score)
        heights[int(player_id)] = None if height is None else float(height)

    units: list[DefensiveUnitEvidence] = []
    for entry in by_unit.values():
        matchup_players = tuple(
            DefensiveMatchupPlayer(
                player_id=player_id,
                defensive_disruptor_score=characteristics[player_id].get("DEFENSIVE_DISRUPTOR"),
                drop_compatible_big_score=characteristics[player_id].get("DROP_COMPATIBLE_BIG"),
                center_role_share=None,
                height=heights.get(player_id),
            )
            for player_id in entry["player_ids"]
        )
        units.append(
            DefensiveUnitEvidence(
                team_id=entry["team_id"],
                season=entry["season"],
                player_ids=entry["player_ids"],
                zone_tilts=entry["tilts"],
                confidence=entry["confidence"],
                evidence_status=entry["status"],
                matchup_players=matchup_players,
            )
        )
    if not units:
        raise ValueError("opponent has no realistic defensive units")
    return units


def _row_dict(row: ProjectionRow) -> dict[str, Any]:
    return {
        "season": row.season,
        "model_version": row.model_version,
        "offense_team_id": row.offense_team_id,
        "offensive_lineup_hash": row.offensive_lineup_hash,
        "offensive_player_ids": list(row.offensive_player_ids),
        "defense_team_id": row.defense_team_id,
        "defensive_lineup_hash": row.defensive_lineup_hash,
        "defensive_player_ids": list(row.defensive_player_ids),
        "projected_pps": row.projected_pps,
        "interval_low": row.interval_low,
        "interval_high": row.interval_high,
        "confidence": row.confidence,
        "evidence_status": row.evidence_status,
        "matchup_assignment": list(row.matchup_assignment) if row.matchup_assignment is not None else None,
    }


def _source_sha256(rows: list[ProjectionRow]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update((json.dumps(_row_dict(row), sort_keys=True, separators=(",", ":")) + "\n").encode())
    return digest.hexdigest()


def write_artifact(output_dir: Path, metadata: dict[str, Any], rows: list[ProjectionRow]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_dir / ARTIFACT_FILE, "wt", encoding="utf-8", newline="\n") as target:
        for row in rows:
            target.write(json.dumps(_row_dict(row), sort_keys=True) + "\n")
    (output_dir / METADATA_FILE).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _publish(conn: Any, rows: list[ProjectionRow], version: str, run_id: int, configuration: dict[str, Any]) -> None:
    from psycopg2.extras import Json, execute_values

    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT is_active, configuration FROM shot_location_model_versions WHERE version = %s FOR UPDATE",
            (version,),
        )
        model = cursor.fetchone()
        if model is None:
            raise ValueError(f"model version is missing: {version}")
        if model[0]:
            raise ValueError("cannot modify an active model version")
        merged = dict(model[1] or {})
        existing_projection = merged.get("offensive_projection")
        if existing_projection is not None and existing_projection != configuration["projection"]:
            raise ValueError("existing offensive projection configuration conflicts with this release")
        merged["offensive_projection"] = configuration["projection"]
        cursor.execute("UPDATE shot_location_model_versions SET configuration = %s WHERE version = %s", (Json(merged), version))
        values = [
            (
                row.season, row.model_version, row.offense_team_id,
                row.offensive_lineup_hash, list(row.offensive_player_ids),
                row.defense_team_id, row.defensive_lineup_hash,
                list(row.defensive_player_ids), row.projected_pps,
                row.interval_low, row.interval_high, row.confidence,
                row.evidence_status, Json(row.matchup_assignment) if row.matchup_assignment is not None else None,
            )
            for row in rows
        ]
        execute_values(
            cursor,
            """
            INSERT INTO lineup_offensive_projections
                (season, model_version, offense_team_id, offensive_lineup_hash,
                 offensive_player_ids, defense_team_id, defensive_lineup_hash,
                 defensive_player_ids, projected_pps, interval_low, interval_high,
                 confidence, evidence_status, matchup_assignment)
            VALUES %s
            ON CONFLICT (season, model_version, offense_team_id, offensive_lineup_hash,
                         defense_team_id, defensive_lineup_hash) DO UPDATE SET
                projected_pps = EXCLUDED.projected_pps,
                interval_low = EXCLUDED.interval_low,
                interval_high = EXCLUDED.interval_high,
                confidence = EXCLUDED.confidence,
                evidence_status = EXCLUDED.evidence_status,
                matchup_assignment = EXCLUDED.matchup_assignment,
                computed_at = now()
            """,
            values,
            page_size=1_000,
        )
        cursor.execute(
            """
            UPDATE data_import_runs
            SET status = 'published', published_row_counts = %s::jsonb,
                published_at = now(), finished_at = now()
            WHERE id = %s AND status = 'validated'
            """,
            (json.dumps({"lineup_offensive_projections": len(rows)}, sort_keys=True), run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"import run {run_id} is not validated")
    conn.commit()


def run(args: argparse.Namespace) -> dict[str, Any]:
    import psycopg2

    load_env_file()
    conn = psycopg2.connect(args.database_url or connection_dsn())
    conn.set_session(readonly=not args.apply)
    run_id: int | None = None
    started = time.perf_counter()
    configuration = model_configuration(args.version)
    release_version = args.release_version or (
        f"issue-0013-offensive-projections-{args.season}-"
        f"{args.offense_team_id}-{args.defense_team_id}.1"
    )
    try:
        if args.apply:
            run_id = begin_import_run(
                conn,
                dataset=DATASET,
                import_version=release_version,
                commit=pipeline_commit(REPO_ROOT),
                source_uri=f"postgres://projection-inputs?season={args.season}&offense_team={args.offense_team_id}&defense_team={args.defense_team_id}",
                first_season=args.season,
                last_season=args.season,
                model_version=args.version,
                metadata=configuration,
            )
        players = _load_offensive_players(conn, args.season, args.offense_team_id, args.version)
        units = _load_defensive_units(conn, args.season, args.defense_team_id, args.version)
        _lineups, rows_tuple = project_offensive_matrix(
            players,
            units,
            season=args.season,
            offense_team_id=args.offense_team_id,
            model_version=args.version,
            interval_draws=args.interval_draws,
            interval_seed=args.interval_seed,
        )
        rows = list(rows_tuple)
        summary = {
            "status": "dry_run" if not args.apply else "published",
            "dataset": DATASET,
            "release_version": release_version,
            "model_version": args.version,
            "season": args.season,
            "offense_team_id": args.offense_team_id,
            "defense_team_id": args.defense_team_id,
            "offensive_players": len(players),
            "defensive_units": len(units),
            "projection_rows": len(rows),
            "status_counts": {status: sum(row.evidence_status == status for row in rows) for status in EVIDENCE_STATUSES},
            "source_sha256": _source_sha256(rows),
            "configuration": configuration,
            "elapsed_seconds": time.perf_counter() - started,
        }
        if args.output_dir:
            write_artifact(args.output_dir, summary, rows)
        if not args.apply:
            conn.rollback()
            return summary
        mark_validated(
            conn,
            run_id,
            source_sha256=summary["source_sha256"],
            staged_row_counts={DATASET: len(rows)},
            validation_results=summary,
        )
        _publish(conn, rows, args.version, run_id, configuration)
        return summary
    except Exception as error:
        if args.apply and run_id is not None:
            mark_failed(conn, run_id, error)
        else:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--offense-team-id", type=int, required=True)
    parser.add_argument("--defense-team-id", type=int, required=True)
    parser.add_argument("--version", default=MODEL_VERSION)
    parser.add_argument("--release-version")
    parser.add_argument("--interval-draws", type=int, default=INTERVAL_DRAW_COUNT)
    parser.add_argument("--interval-seed", type=int, default=INTERVAL_SEED)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
