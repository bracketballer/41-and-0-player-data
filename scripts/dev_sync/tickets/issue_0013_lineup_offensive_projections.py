"""Validate and publish the immutable issue #13 projection artifact."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any

from bracketballer_data.defensive_lineup_concession import EVIDENCE_STATUSES, canonical_lineup_hash
from bracketballer_data.offensive_projection import MODEL_VERSION


ARTIFACT_FILE = "projections.jsonl.gz"
METADATA_FILE = "metadata.json"
DATASET = "lineup_offensive_projections"


def _finite(value: Any, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _row_identity(row: dict[str, Any], *, field: str) -> tuple[int, ...]:
    values = row.get(field)
    if not isinstance(values, list):
        raise ValueError(f"{field} must be an array")
    identity = tuple(int(value) for value in values)
    if len(identity) != 5 or tuple(sorted(identity)) != identity or len(set(identity)) != 5 or any(value <= 0 for value in identity):
        raise ValueError(f"{field} must contain five sorted unique positive IDs")
    return identity


def _validate_row(row: dict[str, Any], metadata: dict[str, Any]) -> tuple[int, str, int, str, int, str]:
    if not isinstance(row, dict):
        raise ValueError("projection artifact rows must be objects")
    season = int(row["season"])
    version = str(row["model_version"])
    offense_team = int(row["offense_team_id"])
    defense_team = int(row["defense_team_id"])
    offense_ids = _row_identity(row, field="offensive_player_ids")
    defense_ids = _row_identity(row, field="defensive_player_ids")
    offense_hash = str(row.get("offensive_lineup_hash"))
    defense_hash = str(row.get("defensive_lineup_hash"))
    if offense_hash != canonical_lineup_hash(offense_ids)[1] or defense_hash != canonical_lineup_hash(defense_ids)[1]:
        raise ValueError("lineup hash does not match sorted player IDs")
    if version != str(metadata["model_version"]):
        raise ValueError("row model version differs from metadata")
    status = str(row["evidence_status"])
    if status not in EVIDENCE_STATUSES:
        raise ValueError(f"invalid projection evidence status: {status}")
    assignment = row.get("matchup_assignment")
    projected = row.get("projected_pps")
    low = row.get("interval_low")
    high = row.get("interval_high")
    confidence = row.get("confidence")
    if status == "unavailable":
        if any(value is not None for value in (projected, low, high, confidence, assignment)):
            raise ValueError("unavailable projections must have null result fields")
    else:
        if assignment is None or not isinstance(assignment, list) or len(assignment) != 5:
            raise ValueError("scored projections require five matchup pairs")
        if any(value is None for value in (projected, low, high, confidence)):
            raise ValueError("scored projections require result fields")
        projected_number = _finite(projected, name="projected_pps")
        low_number = _finite(low, name="interval_low")
        high_number = _finite(high, name="interval_high")
        confidence_number = _finite(confidence, name="confidence")
        if not 0 <= projected_number <= 3 or not 0 <= low_number <= 3 or not 0 <= high_number <= 3:
            raise ValueError("projection PPS values must be in [0, 3]")
        if low_number > high_number or not low_number <= projected_number <= high_number:
            raise ValueError("projection point estimate must be inside its interval")
        if not 0 <= confidence_number <= 100:
            raise ValueError("projection confidence must be in [0, 100]")
    return season, version, offense_team, offense_hash, defense_team, defense_hash


def validate_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate all artifact bytes before a write transaction is opened."""

    metadata_path = root / METADATA_FILE
    artifact_path = root / ARTIFACT_FILE
    if not metadata_path.is_file() or not artifact_path.is_file():
        raise FileNotFoundError("issue #13 artifact is missing metadata or rows")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("dataset") != DATASET:
        raise ValueError("issue #13 artifact dataset is incorrect")
    if metadata.get("model_version") != descriptor.get("model_version", MODEL_VERSION):
        raise ValueError("issue #13 artifact model version is incorrect")
    expected_season = int(descriptor["first_season"])
    if int(metadata.get("season")) != expected_season or int(descriptor["last_season"]) != expected_season:
        raise ValueError("issue #13 artifact season is incorrect")
    expected_offense = int(descriptor["offense_team_id"])
    expected_defense = int(descriptor["defense_team_id"])
    rows: list[dict[str, Any]] = []
    keys: set[tuple[int, str, int, str, int, str]] = set()
    with gzip.open(artifact_path, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid issue #13 JSON at line {line_number}") from error
            if int(row.get("season")) != expected_season:
                raise ValueError("artifact row season differs from descriptor")
            if int(row["offense_team_id"]) != expected_offense or int(row["defense_team_id"]) != expected_defense:
                raise ValueError("artifact row team identity differs from descriptor")
            key = _validate_row(row, metadata)
            if key in keys:
                raise ValueError(f"duplicate projection row: {key}")
            keys.add(key)
            rows.append(row)
    expected_rows = descriptor.get("expected", {}).get("row_counts", {}).get(DATASET)
    if expected_rows is not None and len(rows) != int(expected_rows):
        raise ValueError(f"expected {expected_rows} projection rows, got {len(rows)}")
    if not rows:
        raise ValueError("issue #13 artifact contains no projection rows")
    counts = {
        DATASET: len(rows),
        "offensive_lineups": len({row["offensive_lineup_hash"] for row in rows}),
        "defensive_units": len({row["defensive_lineup_hash"] for row in rows}),
    }
    return {"metadata": metadata, "rows": rows, "counts": counts}


def _verify_source_dependencies(conn: Any, descriptor: dict[str, Any]) -> None:
    for dependency in descriptor.get("source_dependencies", []):
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM data_import_runs WHERE dataset = %s AND import_version = %s",
                (dependency["dataset"], dependency["import_version"]),
            )
            row = cursor.fetchone()
        if row is None or row[0] != "published":
            raise ValueError(f"missing published source dependency: {dependency}")


def apply(conn: Any, root: Path, descriptor: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    """Publish rows idempotently in the runner-owned transaction."""

    from psycopg2.extras import Json, execute_values

    _verify_source_dependencies(conn, descriptor)
    metadata = prepared["metadata"]
    rows = prepared["rows"]
    version = str(metadata["model_version"])
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT configuration, is_active FROM shot_location_model_versions WHERE version = %s FOR UPDATE",
            (version,),
        )
        model = cursor.fetchone()
        if model is None:
            raise ValueError(f"model version is missing: {version}")
        if model[1]:
            raise ValueError("issue #13 cannot modify an active model version")
        configuration = dict(model[0] or {})
        projection_configuration = metadata["configuration"]["projection"]
        if configuration.get("offensive_projection") not in (None, projection_configuration):
            raise ValueError("existing offensive projection configuration conflicts with artifact")
        configuration["offensive_projection"] = projection_configuration
        cursor.execute(
            "UPDATE shot_location_model_versions SET configuration = %s WHERE version = %s",
            (Json(configuration), version),
        )
        values = [
            (
                row["season"], row["model_version"], row["offense_team_id"],
                row["offensive_lineup_hash"], row["offensive_player_ids"],
                row["defense_team_id"], row["defensive_lineup_hash"],
                row["defensive_player_ids"], row["projected_pps"],
                row["interval_low"], row["interval_high"], row["confidence"],
                row["evidence_status"], Json(row["matchup_assignment"]) if row["matchup_assignment"] is not None else None,
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
    return {
        "row_counts": prepared["counts"],
        "validation": {"model_version": version, "published_projection_rows": len(rows)},
    }


__all__ = ["apply", "validate_artifact"]
