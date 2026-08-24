"""Apply the issue #9 coordinate-derived shot-location profile release."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any

from psycopg2.extras import Json, execute_values


FORMAT_VERSION = 1
PROFILE_FILE = "profiles.jsonl.gz"
METADATA_FILE = "metadata.json"
ZONES = ("rim", "short_mid", "long_mid", "corner_three", "above_break_three")


def _read_metadata(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {root / METADATA_FILE}: {error}") from error
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported issue #9 artifact format")
    return value


def _read_profiles(path: Path) -> list[dict[str, Any]]:
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
        raise ValueError(f"cannot read {path}: {error}") from error
    return rows


def _int(value: Any, field: str, row: dict[str, Any]) -> int:
    if isinstance(value, bool):
        raise ValueError(f"issue #9 {field} must be an integer: {row}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #9 {field} must be an integer: {row}") from error


def _float(value: Any, field: str, row: dict[str, Any]) -> float:
    if isinstance(value, bool):
        raise ValueError(f"issue #9 {field} must be numeric: {row}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #9 {field} must be numeric: {row}") from error
    if not math.isfinite(number):
        raise ValueError(f"issue #9 {field} must be finite: {row}")
    return number


def validate_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate all rows before the ticket runner opens a write transaction."""

    profile_path = root / PROFILE_FILE
    metadata_path = root / METADATA_FILE
    if not profile_path.is_file() or not metadata_path.is_file():
        raise ValueError("issue #9 artifact is missing profiles.jsonl.gz or metadata.json")
    metadata = _read_metadata(root)
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    if metadata.get("first_season") != first or metadata.get("last_season") != last:
        raise ValueError("issue #9 artifact season range does not match descriptor")
    model_version = metadata.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise ValueError("issue #9 artifact model version is missing")

    rows = _read_profiles(profile_path)
    seen: set[tuple[int, int, str]] = set()
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    normalized: list[dict[str, Any]] = []
    for row in rows:
        try:
            player_id = _int(row["player_id"], "player_id", row)
            season = _int(row["season"], "season", row)
            version = str(row["model_version"])
            zone = str(row["zone"])
            attempts = _int(row["attempts"], "attempts", row)
            makes = _int(row["makes"], "makes", row)
            attempt_share = _float(row["attempt_share"], "attempt_share", row)
            posterior_alpha = _float(row["posterior_alpha"], "posterior_alpha", row)
            posterior_beta = _float(row["posterior_beta"], "posterior_beta", row)
            adjusted_pps = _float(row["adjusted_pps"], "adjusted_pps", row)
        except KeyError as error:
            raise ValueError(f"issue #9 profile row is missing {error.args[0]}: {row}") from error
        key = (player_id, season, zone)
        if version != model_version or zone not in ZONES or key in seen:
            raise ValueError(f"duplicate or mismatched issue #9 profile row: {row}")
        if not first <= season <= last or player_id <= 0:
            raise ValueError(f"issue #9 profile row is outside descriptor range: {row}")
        if attempts < 0 or makes < 0 or makes > attempts:
            raise ValueError(f"issue #9 profile counts are invalid: {row}")
        if not 0.0 <= attempt_share <= 1.0:
            raise ValueError(f"issue #9 attempt share is outside [0, 1]: {row}")
        if posterior_alpha <= 0.0 or posterior_beta <= 0.0:
            raise ValueError(f"issue #9 posterior parameters must be positive: {row}")
        if not 0.0 <= adjusted_pps <= 3.0:
            raise ValueError(f"issue #9 adjusted PPS is outside [0, 3]: {row}")
        seen.add(key)
        normalized_row = {
            "player_id": player_id,
            "season": season,
            "model_version": version,
            "zone": zone,
            "attempts": attempts,
            "makes": makes,
            "attempt_share": attempt_share,
            "posterior_alpha": posterior_alpha,
            "posterior_beta": posterior_beta,
            "adjusted_pps": adjusted_pps,
        }
        normalized.append(normalized_row)
        grouped.setdefault((player_id, season), []).append(normalized_row)

    for pair, pair_rows in grouped.items():
        if {row["zone"] for row in pair_rows} != set(ZONES):
            raise ValueError(f"issue #9 player-season does not have five zones: {pair}")
        share_total = sum(float(row["attempt_share"]) for row in pair_rows)
        if not math.isclose(share_total, 1.0, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError(f"issue #9 attempt shares do not sum to one: {pair}")

    counts = {
        "eligible_player_seasons": len(grouped),
        "player_shot_location_profiles": len(normalized),
        "profiles_with_five_zones": sum(len(value) == 5 for value in grouped.values()),
    }
    for key, value in counts.items():
        if metadata.get(key) != value:
            raise ValueError(f"issue #9 metadata mismatch for {key}")
    expected_counts = descriptor.get("expected", {}).get("row_counts", {})
    for key, value in expected_counts.items():
        if counts.get(key) != value:
            raise ValueError(f"issue #9 descriptor row-count mismatch for {key}")
    return {"metadata": metadata, "rows": normalized, "pairs": set(grouped), "counts": counts}


def _eligible_player_seasons(conn: Any, first: int, last: int) -> set[tuple[int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ps.player_id, ps.season
            FROM player_seasons ps
            JOIN team_roster_memberships membership
              ON membership.player_id = ps.player_id
             AND membership.team_id = ps.team_id
             AND membership.season = ps.season
             AND membership.source_active
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = ps.team_id
             AND eligibility.season = ps.season
            WHERE ps.season BETWEEN %s AND %s
              AND ps.source_active
              AND ps.minutes >= 100
            """,
            (first, last),
        )
        return {(int(player_id), int(season)) for player_id, season in cursor.fetchall()}


def apply(
    conn: Any, root: Path, descriptor: dict[str, Any], prepared: dict[str, Any]
) -> dict[str, Any]:
    metadata = prepared["metadata"]
    rows = prepared["rows"]
    first = int(descriptor["first_season"])
    last = int(descriptor["last_season"])
    expected_pairs = set(prepared["pairs"])
    actual_pairs = _eligible_player_seasons(conn, first, last)
    if actual_pairs != expected_pairs:
        raise ValueError(
            "issue #9 eligibility mismatch; "
            f"missing={sorted(actual_pairs - expected_pairs)[:20]}, "
            f"extra={sorted(expected_pairs - actual_pairs)[:20]}"
        )

    model_version = str(metadata["model_version"])
    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("issue #9 artifact configuration is missing")
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
            cursor.execute(
                """
                INSERT INTO shot_location_model_versions
                    (version, is_active, configuration)
                VALUES (%s, FALSE, %s)
                """,
                (model_version, Json(configuration)),
            )
        elif existing[0] != configuration:
            raise ValueError("issue #9 model configuration conflicts with the database")
        elif existing[1]:
            raise ValueError("issue #9 cannot modify an active model version")

        values = [
            (
                row["player_id"],
                row["season"],
                model_version,
                row["zone"],
                row["attempts"],
                row["makes"],
                row["attempt_share"],
                row["posterior_alpha"],
                row["posterior_beta"],
                row["adjusted_pps"],
            )
            for row in rows
        ]
        execute_values(
            cursor,
            """
            INSERT INTO player_shot_location_profiles
                (player_id, season, model_version, zone, attempts, makes,
                 attempt_share, posterior_alpha, posterior_beta, adjusted_pps)
            VALUES %s
            ON CONFLICT (player_id, season, model_version, zone) DO UPDATE SET
                attempts = EXCLUDED.attempts,
                makes = EXCLUDED.makes,
                attempt_share = EXCLUDED.attempt_share,
                posterior_alpha = EXCLUDED.posterior_alpha,
                posterior_beta = EXCLUDED.posterior_beta,
                adjusted_pps = EXCLUDED.adjusted_pps,
                computed_at = now()
            """,
            values,
            page_size=1000,
        )

    return {
        "row_counts": prepared["counts"],
        "validation": {
            "eligible_player_seasons": len(actual_pairs),
            "published_player_shot_location_profiles": len(rows),
            "model_version": model_version,
        },
    }
