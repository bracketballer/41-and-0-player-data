"""Apply the issue #10 defensive-lineup concession release."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any, Mapping

from psycopg2.extras import Json, execute_values

from bracketballer_data.defensive_lineup_concession import (
    CONFIDENCE_FLOOR,
    FIELD_GOAL_ZONES,
    LINEUP_SHRINKAGE_PRIOR_ATTEMPTS,
    MINUTES_SHARE_THRESHOLD,
    canonical_lineup_hash,
)


FORMAT_VERSION = 1
CONCESSION_FILE = "concessions.jsonl.gz"
METADATA_FILE = "metadata.json"
ZONES = tuple(FIELD_GOAL_ZONES)


def _configuration_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and all(
                key in actual and _configuration_contains(actual[key], value)
                for key, value in expected.items()
            )
        )
    return actual == expected


def _read_metadata(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read issue #10 metadata: {error}") from error
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported issue #10 artifact format")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number} must contain an object")
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read issue #10 concessions: {error}") from error
    return rows


def _int(value: Any, field: str, row: Mapping[str, Any]) -> int:
    if isinstance(value, bool):
        raise ValueError(f"issue #10 {field} must be an integer: {row}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #10 {field} must be an integer: {row}") from error


def _float(value: Any, field: str, row: Mapping[str, Any], *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"issue #10 {field} must be numeric: {row}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #10 {field} must be numeric: {row}") from error
    if not math.isfinite(number):
        raise ValueError(f"issue #10 {field} must be finite: {row}")
    return number


def _validate_configuration(metadata: dict[str, Any]) -> dict[str, Any]:
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("issue #10 model configuration is missing")
    defense = configuration.get("defense_concession")
    if not isinstance(defense, dict):
        raise ValueError("issue #10 defense-concession configuration is missing")
    if float(defense.get("minutes_share_threshold", -1)) != MINUTES_SHARE_THRESHOLD:
        raise ValueError("issue #10 minutes-share threshold is not 2 percent")
    if float(defense.get("lineup_shrinkage_prior_attempts", -1)) != LINEUP_SHRINKAGE_PRIOR_ATTEMPTS:
        raise ValueError("issue #10 lineup shrinkage prior is not 50 attempts")
    if float(defense.get("confidence_floor", -1)) != CONFIDENCE_FLOOR:
        raise ValueError("issue #10 confidence floor is not 45")
    return configuration


def validate_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate every row and status before opening a write transaction."""

    if not (root / CONCESSION_FILE).is_file() or not (root / METADATA_FILE).is_file():
        raise ValueError("issue #10 artifact is missing concessions.jsonl.gz or metadata.json")
    metadata = _read_metadata(root)
    if metadata.get("dataset") != "defense_lineup_zone_concession":
        raise ValueError("issue #10 artifact dataset is invalid")
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    if metadata.get("first_season") != first or metadata.get("last_season") != last:
        raise ValueError("issue #10 artifact season range does not match descriptor")
    model_version = metadata.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise ValueError("issue #10 artifact model version is missing")
    configuration = _validate_configuration(metadata)
    defense_config = configuration["defense_concession"]
    audit_passed = bool(defense_config.get("independent_lineup_audit_passed", False))

    rows = _read_rows(root / CONCESSION_FILE)
    seen: set[tuple[int, int, str, str]] = set()
    grouped: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    normalized: list[dict[str, Any]] = []
    for row in rows:
        try:
            season = _int(row["season"], "season", row)
            team_id = _int(row["team_id"], "team_id", row)
            lineup_hash = str(row["lineup_hash"])
            player_ids_value = row["lineup_player_ids"]
            if not isinstance(player_ids_value, list):
                raise ValueError("lineup_player_ids must be an array")
            player_ids = tuple(_int(value, "lineup_player_ids", row) for value in player_ids_value)
            canonical_ids, canonical_hash = canonical_lineup_hash(player_ids)
            zone = str(row["zone"])
            version = str(row["model_version"])
            tilt = _float(row["zone_concession_tilt"], "zone_concession_tilt", row, nullable=True)
            possessions = _float(row["possessions"], "possessions", row)
            confidence = _float(row["confidence"], "confidence", row, nullable=True)
            status = str(row["evidence_status"])
            minutes_share = _float(row["minutes_share"], "minutes_share", row)
            classified_attempts = _int(row["classified_attempts"], "classified_attempts", row)
            opponent_fga = _float(row["opponent_fga"], "opponent_fga", row)
            coverage = _float(row["attribution_coverage"], "attribution_coverage", row)
        except KeyError as error:
            raise ValueError(f"issue #10 row is missing {error.args[0]}: {row}") from error
        if version != model_version or zone not in ZONES:
            raise ValueError(f"issue #10 row has mismatched version or zone: {row}")
        if not first <= season <= last or team_id <= 0:
            raise ValueError(f"issue #10 row is outside descriptor range: {row}")
        if lineup_hash != canonical_hash or player_ids != canonical_ids:
            raise ValueError(f"issue #10 lineup identity is not canonical: {row}")
        if (season, team_id, lineup_hash, zone) in seen:
            raise ValueError(f"duplicate issue #10 concession row: {row}")
        if possessions is None or possessions < 0 or opponent_fga is None or opponent_fga < 0:
            raise ValueError(f"issue #10 evidence counts are invalid: {row}")
        if classified_attempts < 0 or minutes_share is None or not 0 <= minutes_share <= 1:
            raise ValueError(f"issue #10 lineup evidence is invalid: {row}")
        if coverage is None or not 0 <= coverage <= 1:
            raise ValueError(f"issue #10 attribution coverage is invalid: {row}")
        if status not in {"available", "provisional", "unavailable"}:
            raise ValueError(f"issue #10 evidence status is invalid: {row}")
        if minutes_share + 1e-12 < MINUTES_SHARE_THRESHOLD:
            raise ValueError(f"issue #10 row is below the realistic-unit threshold: {row}")
        expected_coverage = min(1.0, classified_attempts / opponent_fga) if opponent_fga else 0.0
        if not math.isclose(coverage, expected_coverage, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError(f"issue #10 attribution coverage does not match counts: {row}")
        usable = classified_attempts > 0 and possessions > 0 and opponent_fga > 0
        if not usable:
            expected_status = "unavailable"
        elif audit_passed and (confidence or 0.0) >= CONFIDENCE_FLOOR:
            expected_status = "available"
        else:
            expected_status = "provisional"
        if status != expected_status:
            raise ValueError(f"issue #10 evidence status is inconsistent with the audit policy: {row}")
        if not usable:
            if tilt is not None or confidence is not None:
                raise ValueError("issue #10 unavailable evidence must retain null tilt and confidence")
        else:
            expected_confidence = 100.0 * classified_attempts / (classified_attempts + LINEUP_SHRINKAGE_PRIOR_ATTEMPTS) * coverage
            if confidence is None or not 0 <= confidence <= 100 or not math.isclose(
                confidence, expected_confidence, rel_tol=0.0, abs_tol=1e-8
            ):
                raise ValueError(f"issue #10 confidence does not match evidence: {row}")
            if tilt is None:
                raise ValueError("issue #10 usable evidence must have a finite tilt")
        normalized_row = {
            "model_version": version,
            "season": season,
            "team_id": team_id,
            "lineup_hash": lineup_hash,
            "lineup_player_ids": list(canonical_ids),
            "zone": zone,
            "zone_concession_tilt": tilt,
            "possessions": possessions,
            "confidence": confidence,
            "evidence_status": status,
            "minutes_share": minutes_share,
            "classified_attempts": classified_attempts,
            "opponent_fga": opponent_fga,
            "attribution_coverage": coverage,
        }
        seen.add((season, team_id, lineup_hash, zone))
        normalized.append(normalized_row)
        grouped.setdefault((season, team_id, lineup_hash), []).append(normalized_row)

    for key, unit_rows in grouped.items():
        if {row["zone"] for row in unit_rows} != set(ZONES):
            raise ValueError(f"issue #10 unit does not have five zones: {key}")
        first_row = unit_rows[0]
        for row in unit_rows[1:]:
            for field in (
                "lineup_player_ids",
                "possessions",
                "confidence",
                "evidence_status",
                "minutes_share",
                "classified_attempts",
                "opponent_fga",
                "attribution_coverage",
            ):
                if row[field] != first_row[field]:
                    raise ValueError(f"issue #10 unit-level field differs across zones: {key}")

    unit_count = len(grouped)
    counts = {
        "teams": len({(key[0], key[1]) for key in grouped}),
        "realistic_units": unit_count,
        "defense_lineup_zone_concession": len(normalized),
        "profiles_with_five_zones": sum(len(value) == 5 for value in grouped.values()),
    }
    for key, value in counts.items():
        if metadata.get(key) != value:
            raise ValueError(f"issue #10 metadata mismatch for {key}")
    status_counts = {
        status: sum(row["evidence_status"] == status for row in normalized)
        for status in ("available", "provisional", "unavailable")
    }
    if metadata.get("status_counts") != status_counts:
        raise ValueError("issue #10 metadata status counts do not match rows")
    expected_counts = descriptor.get("expected", {}).get("row_counts", {})
    for key, value in expected_counts.items():
        if counts.get(key) != value:
            raise ValueError(f"issue #10 descriptor row-count mismatch for {key}")
    return {
        "metadata": metadata,
        "configuration": configuration,
        "rows": normalized,
        "units": grouped,
        "counts": counts,
    }


def _verify_source_dependencies(conn: Any, descriptor: dict[str, Any]) -> None:
    for dependency in descriptor.get("source_dependencies", []):
        if not isinstance(dependency, dict):
            raise ValueError("issue #10 source dependency is malformed")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM data_import_runs
                WHERE dataset = %s AND import_version = %s
                ORDER BY id DESC LIMIT 1
                """,
                (dependency["dataset"], dependency["import_version"]),
            )
            row = cursor.fetchone()
        if row is None or row[0] != "published":
            raise ValueError(
                "issue #10 requires published source release "
                f"{dependency['dataset']}:{dependency['import_version']}"
            )


def _verify_lineup_source(conn: Any, prepared: dict[str, Any], first: int, last: int) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            WITH lineup_source AS (
                SELECT lineup.id, lineup.season, lineup.team_id,
                       lineup.lineup_hash, lineup.total_seconds,
                       lineup.opponent_stats
                FROM team_game_lineups lineup
                JOIN team_season_eligibility eligibility
                  ON eligibility.team_id = lineup.team_id
                 AND eligibility.season = lineup.season
                WHERE lineup.season BETWEEN %s AND %s
            )
            SELECT source.season, source.team_id, source.lineup_hash,
                   SUM(source.total_seconds),
                   SUM(COALESCE((source.opponent_stats->>'possessions')::double precision, 0)),
                   SUM(COALESCE((source.opponent_stats->'fieldGoals'->>'attempted')::double precision, 0)),
                   array_agg(DISTINCT lineup_player.player_id ORDER BY lineup_player.player_id)
            FROM lineup_source source
            JOIN team_game_lineup_players lineup_player
              ON lineup_player.lineup_id = source.id
            GROUP BY source.season, source.team_id, source.lineup_hash
            """,
            (first, last),
        )
        source_rows = cursor.fetchall()
    actual: dict[tuple[int, int, str], dict[str, Any]] = {}
    totals: dict[tuple[int, int], float] = {}
    for season, team_id, lineup_hash, seconds, possessions, opponent_fga, player_ids in source_rows:
        ids, canonical_hash = canonical_lineup_hash(player_ids)
        if lineup_hash != canonical_hash:
            raise ValueError(f"issue #10 source lineup hash mismatch: {lineup_hash}")
        if len(ids) != 5:
            continue
        key = (int(season), int(team_id), str(lineup_hash))
        actual[key] = {
            "player_ids": list(ids),
            "seconds": float(seconds or 0),
            "possessions": float(possessions or 0),
            "opponent_fga": float(opponent_fga or 0),
        }
        totals[(int(season), int(team_id))] = totals.get((int(season), int(team_id)), 0.0) + float(seconds or 0)
    expected_keys = set(prepared["units"])
    selected_keys = {
        key
        for key, row in actual.items()
        if totals[(key[0], key[1])] > 0
        and row["seconds"] / totals[(key[0], key[1])] >= MINUTES_SHARE_THRESHOLD
    }
    if selected_keys != expected_keys:
        raise ValueError(
            "issue #10 realistic-unit source mismatch; "
            f"missing={sorted(selected_keys - expected_keys)[:10]}, "
            f"extra={sorted(expected_keys - selected_keys)[:10]}"
        )
    for key, rows in prepared["units"].items():
        source = actual.get(key)
        if source is None:
            raise ValueError(f"issue #10 source lineup is missing: {key}")
        row = rows[0]
        if row["lineup_player_ids"] != source["player_ids"]:
            raise ValueError(f"issue #10 source lineup players differ: {key}")
        expected_minutes_share = source["seconds"] / totals[(key[0], key[1])]
        if not math.isclose(
            float(row["minutes_share"]), expected_minutes_share, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError(f"issue #10 source minutes share differs: {key}")
        for field, source_field in (
            ("possessions", "possessions"),
            ("opponent_fga", "opponent_fga"),
        ):
            if not math.isclose(float(row[field]), source[source_field], rel_tol=0.0, abs_tol=1e-8):
                raise ValueError(f"issue #10 source {field} differs: {key}")


def apply(conn: Any, root: Path, descriptor: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    """Write the immutable artifact idempotently in the runner transaction."""

    metadata = prepared["metadata"]
    configuration = prepared["configuration"]
    rows = prepared["rows"]
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    model_version = str(metadata["model_version"])
    _verify_source_dependencies(conn, descriptor)
    _verify_lineup_source(conn, prepared, first, last)

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT configuration, is_active
            FROM shot_location_model_versions
            WHERE version = %s
            FOR UPDATE
            """,
            (model_version,),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise ValueError(f"issue #10 model version is missing: {model_version}")
        existing_configuration, is_active = existing
        if is_active:
            raise ValueError("issue #10 cannot modify an active model version")
        base_configuration = dict(configuration)
        defense_configuration = base_configuration.pop("defense_concession")
        if not _configuration_contains(existing_configuration, base_configuration):
            raise ValueError("issue #10 base model configuration conflicts with the database")
        if "defense_concession" in existing_configuration and not _configuration_contains(
            existing_configuration["defense_concession"], defense_configuration
        ):
            raise ValueError("issue #10 defense-concession configuration conflicts with the database")
        merged_configuration = dict(existing_configuration)
        merged_defense_configuration = dict(
            existing_configuration.get("defense_concession", {})
        )
        merged_defense_configuration.update(defense_configuration)
        merged_configuration["defense_concession"] = merged_defense_configuration
        cursor.execute(
            "UPDATE shot_location_model_versions SET configuration = %s WHERE version = %s",
            (Json(merged_configuration), model_version),
        )

        cursor.execute(
            """
            SELECT DISTINCT season, team_id, lineup_hash
            FROM defense_lineup_zone_concession
            WHERE model_version = %s AND season BETWEEN %s AND %s
            """,
            (model_version, first, last),
        )
        existing_units = {(int(season), int(team_id), str(lineup_hash)) for season, team_id, lineup_hash in cursor.fetchall()}
        if existing_units - set(prepared["units"]):
            raise ValueError("issue #10 database contains extra immutable concession units")
        values = [
            (
                row["team_id"],
                row["season"],
                model_version,
                row["lineup_hash"],
                row["lineup_player_ids"],
                row["zone"],
                row["zone_concession_tilt"],
                row["possessions"],
                row["confidence"],
                row["evidence_status"],
            )
            for row in rows
        ]
        execute_values(
            cursor,
            """
            INSERT INTO defense_lineup_zone_concession
                (team_id, season, model_version, lineup_hash, lineup_player_ids,
                 zone, zone_concession_tilt, possessions, confidence, evidence_status)
            VALUES %s
            ON CONFLICT (team_id, season, model_version, lineup_hash, zone) DO UPDATE SET
                lineup_player_ids = EXCLUDED.lineup_player_ids,
                zone_concession_tilt = EXCLUDED.zone_concession_tilt,
                possessions = EXCLUDED.possessions,
                confidence = EXCLUDED.confidence,
                evidence_status = EXCLUDED.evidence_status,
                computed_at = now()
            """,
            values,
            page_size=1000,
        )
    return {
        "row_counts": prepared["counts"],
        "validation": {
            "model_version": model_version,
            "published_defense_lineup_zone_concession": len(rows),
            "evidence_status": metadata["status_counts"],
        },
    }
