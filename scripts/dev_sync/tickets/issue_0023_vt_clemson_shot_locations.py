"""Validate and apply the issue #23 Virginia Tech/Clemson pilot release."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any, Mapping

try:
    from psycopg2.extras import Json, execute_values
except ModuleNotFoundError:  # pragma: no cover - dependency is present in sync environments
    Json = None  # type: ignore[assignment,misc]
    execute_values = None  # type: ignore[assignment]

from bracketballer_data.shot_zones import FIELD_GOAL_ZONES, ShotCoordinate, classify_shot, infer_attacking_baskets


FORMAT_VERSION = 1
SEASON = 2026
TEAM_IDS = (52, 340)
MIN_ROTATION_MINUTES = 100
SOURCE_PROFILE_VERSION = "shot-location-v1"
MODEL_VERSION = "shot-location-v1-2026-vt-clemson"
RELEASE_VERSION = "issue-0023-vt-clemson-shot-locations-2026.1"
PROFILE_FILE = "profiles.jsonl.gz"
EVENT_FILE = "event_locations.jsonl.gz"
METADATA_FILE = "metadata.json"
ZONES = tuple(FIELD_GOAL_ZONES)
STATUSES = ("mapped", "missing_coordinates", "invalid_coordinates", "unresolved_direction")
PROFILE_FIELDS = (
    "player_id",
    "season",
    "model_version",
    "zone",
    "attempts",
    "makes",
    "attempt_share",
    "posterior_alpha",
    "posterior_beta",
    "adjusted_pps",
)
EVENT_FIELDS = (
    "source_play_id",
    "player_id",
    "season",
    "game_id",
    "team_id",
    "opponent_id",
    "period",
    "made",
    "shot_range",
    "location_x",
    "location_y",
    "model_version",
    "mapping_status",
    "normalized_x",
    "normalized_y",
    "zone",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number} must contain an object")
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read issue #23 rows {path}: {error}") from error
    return rows


def _read_metadata(root: Path) -> dict[str, Any]:
    try:
        metadata = json.loads((root / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read issue #23 metadata: {error}") from error
    if not isinstance(metadata, dict) or metadata.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported issue #23 artifact format")
    return metadata


def _int(value: Any, field: str, row: Mapping[str, Any]) -> int:
    if isinstance(value, bool):
        raise ValueError(f"issue #23 {field} must be an integer: {row}")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #23 {field} must be an integer: {row}") from error


def _float(value: Any, field: str, row: Mapping[str, Any], *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"issue #23 {field} must be numeric: {row}")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"issue #23 {field} must be numeric: {row}") from error
    if not math.isfinite(number):
        raise ValueError(f"issue #23 {field} must be finite: {row}")
    return number


def _source_coordinate(value: Any, field: str, row: Mapping[str, Any]) -> Any:
    """Parse a source coordinate while retaining non-finite values as evidence."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"issue #23 {field} must be numeric: {row}")
    try:
        return float(value)
    except (TypeError, ValueError):
        # A malformed string is still an explicitly invalid source coordinate;
        # retaining it lets the artifact explain why it was excluded.
        return value


def _source_invalid(value: Any, *, upper: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    return not math.isfinite(float(value)) or not 0.0 <= float(value) <= upper


def _expected_count(metadata: Mapping[str, Any], key: str) -> int | None:
    value = metadata.get("row_counts", {}).get(key)
    if value is None:
        value = metadata.get(key)
    if value is None:
        return None
    return _int(value, key, metadata)


def _configuration_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _configuration_contains(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def _validate_profile_rows(rows: list[dict[str, Any]], metadata: Mapping[str, Any]) -> tuple[list[dict[str, Any]], set[tuple[int, int]]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        missing = [field for field in PROFILE_FIELDS if field not in row]
        if missing:
            raise ValueError(f"issue #23 profile row is missing {missing[0]}")
        player_id = _int(row["player_id"], "player_id", row)
        season = _int(row["season"], "season", row)
        version = str(row["model_version"])
        zone = str(row["zone"])
        attempts = _int(row["attempts"], "attempts", row)
        makes = _int(row["makes"], "makes", row)
        attempt_share = _float(row["attempt_share"], "attempt_share", row)
        alpha = _float(row["posterior_alpha"], "posterior_alpha", row)
        beta = _float(row["posterior_beta"], "posterior_beta", row)
        adjusted_pps = _float(row["adjusted_pps"], "adjusted_pps", row)
        key = (player_id, season, zone)
        if key in seen or version != MODEL_VERSION or zone not in ZONES:
            raise ValueError(f"duplicate, out-of-scope, or mismatched issue #23 profile row: {row}")
        if player_id <= 0 or season != SEASON:
            raise ValueError(f"issue #23 profile row is outside the locked scope: {row}")
        if attempts < 0 or makes < 0 or makes > attempts:
            raise ValueError(f"issue #23 profile counts are invalid: {row}")
        if attempt_share is None or not 0.0 <= attempt_share <= 1.0:
            raise ValueError(f"issue #23 attempt share is outside [0, 1]: {row}")
        if alpha is None or beta is None or alpha <= 0.0 or beta <= 0.0:
            raise ValueError(f"issue #23 posterior parameters are invalid: {row}")
        if adjusted_pps is None or not 0.0 <= adjusted_pps <= 3.0:
            raise ValueError(f"issue #23 adjusted PPS is outside [0, 3]: {row}")
        value = {
            "player_id": player_id,
            "season": season,
            "model_version": version,
            "zone": zone,
            "attempts": attempts,
            "makes": makes,
            "attempt_share": attempt_share,
            "posterior_alpha": alpha,
            "posterior_beta": beta,
            "adjusted_pps": adjusted_pps,
        }
        seen.add(key)
        normalized.append(value)
        grouped.setdefault((player_id, season), []).append(value)

    pairs = set(grouped)
    # The pilot population is locked to the 20 active rotation player-seasons.
    if len(pairs) != 20:
        raise ValueError(f"issue #23 requires exactly 20 eligible player-seasons, got {len(pairs)}")
    if set(metadata.get("player_ids", [])) != {player_id for player_id, _season in pairs}:
        raise ValueError("issue #23 profile player IDs do not match metadata scope")
    for pair, pair_rows in grouped.items():
        if {row["zone"] for row in pair_rows} != set(ZONES):
            raise ValueError(f"issue #23 player-season does not have five zones: {pair}")
        if not math.isclose(sum(float(row["attempt_share"]) for row in pair_rows), 1.0, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError(f"issue #23 attempt shares do not sum to one: {pair}")
    return normalized, pairs


def _validate_event_rows(rows: list[dict[str, Any]], metadata: Mapping[str, Any], pairs: set[tuple[int, int]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    statuses = {status: 0 for status in STATUSES}
    player_ids = {pair[0] for pair in pairs}
    for row in rows:
        missing = [field for field in EVENT_FIELDS if field not in row]
        if "mapping_status" not in row and "status" in row:
            missing = [field for field in missing if field != "mapping_status"]
        if missing:
            raise ValueError(f"issue #23 event row is missing {missing[0]}")
        source_play_id = _int(row["source_play_id"], "source_play_id", row)
        player_id = _int(row["player_id"], "player_id", row)
        season = _int(row["season"], "season", row)
        game_id = _int(row["game_id"], "game_id", row)
        team_id = _int(row["team_id"], "team_id", row)
        opponent_id = row["opponent_id"]
        if opponent_id is not None:
            opponent_id = _int(opponent_id, "opponent_id", row)
        period = row["period"]
        if period is not None:
            period = _int(period, "period", row)
        status = str(row.get("mapping_status", row.get("status")))
        version = str(row["model_version"])
        if source_play_id <= 0 or source_play_id in seen:
            raise ValueError(f"duplicate or invalid issue #23 source_play_id: {row}")
        if version != MODEL_VERSION or season != SEASON or team_id not in TEAM_IDS or player_id not in player_ids or (player_id, season) not in pairs:
            raise ValueError(f"issue #23 event is outside the locked scope: {row}")
        if game_id <= 0 or status not in STATUSES:
            raise ValueError(f"issue #23 event identity/status is invalid: {row}")
        if not isinstance(row["made"], bool):
            raise ValueError(f"issue #23 event made value must be boolean: {row}")
        if row["shot_range"] is not None and not isinstance(row["shot_range"], str):
            raise ValueError(f"issue #23 event shot_range must be text or null: {row}")
        if row["shot_range"] == "free_throw":
            raise ValueError("issue #23 event artifact must contain field goals only")
        source_x = _source_coordinate(row["location_x"], "location_x", row)
        source_y = _source_coordinate(row["location_y"], "location_y", row)
        source_missing = source_x is None or source_y is None
        source_invalid = (not source_missing) and (
            _source_invalid(source_x, upper=940.0)
            or _source_invalid(source_y, upper=500.0)
        )
        basket = row.get("attacking_basket")
        basket_supplied = "attacking_basket" in row
        if basket not in {"left", "right", None}:
            raise ValueError(f"issue #23 attacking basket is invalid: {row}")
        if source_missing and status != "missing_coordinates":
            raise ValueError("issue #23 status precedence requires missing_coordinates")
        if source_invalid and status != "invalid_coordinates":
            raise ValueError("issue #23 status precedence requires invalid_coordinates")
        if basket_supplied and not source_missing and not source_invalid and basket is None and status != "unresolved_direction":
            raise ValueError("issue #23 valid coordinates without direction must be unresolved_direction")
        normalized_x = _float(row["normalized_x"], "normalized_x", row, nullable=True)
        normalized_y = _float(row["normalized_y"], "normalized_y", row, nullable=True)
        zone = row["zone"]
        if status == "mapped":
            if source_missing or source_invalid or (basket_supplied and basket is None) or normalized_x is None or normalized_y is None or str(zone) not in ZONES:
                raise ValueError(f"issue #23 mapped event lacks valid evidence: {row}")
            if not 0.0 <= normalized_x <= 94.0 or not 0.0 <= normalized_y <= 50.0:
                raise ValueError(f"issue #23 mapped geometry is outside the court: {row}")
            reproduced = (
                classify_shot(source_x, source_y, basket)
                if basket is not None
                else classify_shot(normalized_x * 10.0, normalized_y * 10.0, "left")
            )
            if reproduced != zone:
                raise ValueError(f"issue #23 mapped zone does not reproduce geometry: {row}")
        else:
            if normalized_x is not None or normalized_y is not None or zone is not None:
                raise ValueError(f"issue #23 excluded event must not carry mapped evidence: {row}")
        value = {
            "source_play_id": source_play_id,
            "player_id": player_id,
            "season": season,
            "game_id": game_id,
            "team_id": team_id,
            "opponent_id": opponent_id,
            "period": period,
            "made": row["made"],
            "shot_range": row["shot_range"],
            "location_x": source_x,
            "location_y": source_y,
            "model_version": version,
            "mapping_status": status,
            "normalized_x": normalized_x,
            "normalized_y": normalized_y,
            "zone": str(zone) if zone is not None else None,
            "attacking_basket": basket,
        }
        seen.add(source_play_id)
        statuses[status] += 1
        normalized.append(value)
    return normalized, statuses


def validate_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    """Validate every byte-derived row before opening a write transaction."""

    for name in (PROFILE_FILE, EVENT_FILE, METADATA_FILE):
        if not (root / name).is_file():
            raise ValueError(f"issue #23 artifact is missing {name}")
    metadata = _read_metadata(root)
    if metadata.get("dataset") != "player_shot_event_locations":
        raise ValueError("issue #23 artifact dataset is invalid")
    if descriptor.get("release_version", RELEASE_VERSION) != RELEASE_VERSION:
        raise ValueError("issue #23 descriptor release version is not locked")
    if descriptor.get("model_version", MODEL_VERSION) != MODEL_VERSION:
        raise ValueError("issue #23 descriptor model version is not locked")
    if metadata.get("release_version") != RELEASE_VERSION or metadata.get("model_version") != MODEL_VERSION:
        raise ValueError("issue #23 artifact release/model version is invalid")
    source_releases = metadata.get("source_releases")
    if not isinstance(source_releases, dict) or source_releases.get("shot_corpus") != "issue-0006-shots-2026.1" or source_releases.get("audited_profiles") != "issue-0009-shot-location-2026.1":
        raise ValueError("issue #23 source release pins are missing or invalid")
    if source_releases.get("audited_profile_model_version", SOURCE_PROFILE_VERSION) != SOURCE_PROFILE_VERSION:
        raise ValueError("issue #23 audited profile model version is invalid")
    source_versions = metadata.get("source_release_versions")
    if source_versions is not None and (
        not isinstance(source_versions, dict)
        or source_versions.get("shot_corpus") != "issue-0006-shots-2026.1"
        or source_versions.get("audited_profiles") != "issue-0009-shot-location-2026.1"
    ):
        raise ValueError("issue #23 source release version aliases are invalid")
    source_checksums = metadata.get("source_release_checksums")
    if (
        not isinstance(source_checksums, dict)
        or any(
            not isinstance(source_checksums.get(key), str)
            or len(source_checksums[key]) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in source_checksums[key])
            for key in ("shot_corpus_archive_sha256", "audited_profiles_archive_sha256")
        )
    ):
        raise ValueError("issue #23 source release checksums are missing or invalid")
    configuration = metadata.get("configuration")
    expected_configuration = {
        "model_version": MODEL_VERSION,
        "base_model_version": SOURCE_PROFILE_VERSION,
        "zone_scheme": list(ZONES),
        "season": SEASON,
        "team_ids": list(TEAM_IDS),
        "rotation_minimum_minutes": MIN_ROTATION_MINUTES,
        "active_roster_membership": True,
        "direction_inference": "all_field_goals_in_relevant_game",
        "activation": "profile_only",
    }
    if not all(configuration.get(key) == value for key, value in expected_configuration.items()):
        raise ValueError("issue #23 model configuration is not the locked pilot configuration")
    if metadata.get("first_season") != SEASON or metadata.get("last_season") != SEASON:
        raise ValueError("issue #23 artifact season is not locked to 2026")
    if sorted(metadata.get("team_ids", [])) != sorted(TEAM_IDS) or any(isinstance(team, bool) or not isinstance(team, int) for team in metadata.get("team_ids", [])):
        raise ValueError("issue #23 artifact team scope is invalid")
    if (
        not isinstance(metadata.get("player_ids"), list)
        or len(metadata["player_ids"]) != 20
        or len(set(metadata["player_ids"])) != 20
        or any(isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0 for player_id in metadata["player_ids"])
    ):
        raise ValueError("issue #23 artifact must name 20 unique player IDs")
    membership_keys: set[tuple[int, int, int]] = set()
    memberships = metadata.get("eligible_roster_memberships")
    if memberships is not None:
        if not isinstance(memberships, list) or len(memberships) != 20:
            raise ValueError("issue #23 artifact must name the 20 eligible roster memberships")
        for membership in memberships:
            if not isinstance(membership, dict):
                raise ValueError("issue #23 roster membership is malformed")
            team_id = _int(membership.get("team_id"), "team_id", membership)
            player_id = _int(membership.get("player_id"), "player_id", membership)
            season = _int(membership.get("season"), "season", membership)
            minutes = _float(membership.get("minutes"), "minutes", membership)
            key = (team_id, player_id, season)
            if key in membership_keys or team_id not in TEAM_IDS or season != SEASON or minutes is None or minutes < MIN_ROTATION_MINUTES:
                raise ValueError("issue #23 roster membership is outside the locked scope")
            membership_keys.add(key)
    source_sha = metadata.get("source_sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_sha):
        raise ValueError("issue #23 source SHA-256 is missing or malformed")
    profiles, pairs = _validate_profile_rows(_read_jsonl(root / PROFILE_FILE), metadata)
    if membership_keys and {(player_id, season) for _team_id, player_id, season in membership_keys} != pairs:
        raise ValueError("issue #23 roster memberships do not match profile player-seasons")
    excluded = metadata.get("excluded_sub_rotation_players")
    if excluded is not None:
        if not isinstance(excluded, list) or len(excluded) != 7:
            raise ValueError("issue #23 must retain the seven sub-100-minute players as unavailable")
        for item in excluded:
            if not isinstance(item, dict) or item.get("availability") != "unavailable_below_100_minutes":
                raise ValueError("issue #23 excluded roster player is malformed")
            team_id = _int(item.get("team_id"), "team_id", item)
            season = _int(item.get("season"), "season", item)
            minutes = _float(item.get("minutes"), "minutes", item)
            if team_id not in TEAM_IDS or season != SEASON or minutes is None or minutes >= MIN_ROTATION_MINUTES:
                raise ValueError("issue #23 excluded roster player is outside the locked scope")
    events, statuses = _validate_event_rows(_read_jsonl(root / EVENT_FILE), metadata, pairs)
    team_totals: dict[str, int] = {str(team): 0 for team in TEAM_IDS}
    team_mapped: dict[str, int] = {str(team): 0 for team in TEAM_IDS}
    zone_counts: dict[str, int] = {zone: 0 for zone in ZONES}
    comparable = 0
    agreements = 0
    for event in events:
        team_key = str(event["team_id"])
        team_totals[team_key] += 1
        if event["mapping_status"] == "mapped":
            team_mapped[team_key] += 1
            zone_counts[str(event["zone"])] += 1
            if isinstance(event.get("shot_range"), str) and event.get("shot_range") != "free_throw":
                comparable += 1
                source_three = event["shot_range"] in {"three_pointer", "three", "3", "three_point"}
                zone_three = event["zone"] in {"corner_three", "above_break_three"}
                agreements += source_three == zone_three
    calculated_coverage = {
        team: team_mapped[team] / team_totals[team] if team_totals[team] else 0.0
        for team in sorted(team_totals)
    }
    metadata_coverage = metadata.get("mapped_coverage_by_team")
    if not isinstance(metadata_coverage, dict) or any(
        not math.isclose(float(metadata_coverage.get(team, -1)), value, rel_tol=0.0, abs_tol=1e-12)
        for team, value in calculated_coverage.items()
    ):
        raise ValueError("issue #23 mapped coverage diagnostics do not match event rows")
    metadata_agreement = metadata.get("two_three_point_agreement")
    calculated_agreement = agreements / comparable if comparable else 0.0
    if not isinstance(metadata_agreement, dict) or not math.isclose(
        float(metadata_agreement.get("agreement", -1)), calculated_agreement, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("issue #23 two-/three-point diagnostics do not match event rows")
    if metadata_agreement.get("comparable_mapped_events", comparable) != comparable or metadata_agreement.get("agreements", agreements) != agreements:
        raise ValueError("issue #23 two-/three-point diagnostic counts do not match event rows")
    metadata_zones = metadata.get("zone_counts")
    if not isinstance(metadata_zones, dict) or any(int(metadata_zones.get(zone, -1)) != count for zone, count in zone_counts.items()):
        raise ValueError("issue #23 zone diagnostics do not match event rows")
    counts = {
        "eligible_player_seasons": len(pairs),
        "player_shot_location_profiles": len(profiles),
        "profiles_with_five_zones": len(pairs),
        "event_locations": len(events),
        "event_location_rows": len(events),
        "player_shot_event_locations": len(events),
        "excluded_sub_rotation_players": len(metadata.get("excluded_sub_rotation_players", [])),
        **{f"status_{status}": statuses[status] for status in STATUSES},
    }
    metadata_row_counts = metadata.get("row_counts")
    if metadata_row_counts is None:
        metadata_row_counts = {key: metadata.get(key) for key in counts}
    if not isinstance(metadata_row_counts, dict) or any(key not in metadata_row_counts for key in counts):
        raise ValueError("issue #23 metadata is missing exact row/status counts")
    for key, value in counts.items():
        expected = _expected_count(metadata, key)
        if expected is not None and expected != value:
            raise ValueError(f"issue #23 metadata mismatch for {key}: expected {expected}, got {value}")
        expected = descriptor.get("expected", {}).get("row_counts", {}).get(key)
        if expected is not None and int(expected) != value:
            raise ValueError(f"issue #23 descriptor row-count mismatch for {key}")
    expected_events = 3857
    if counts["event_locations"] != expected_events:
        raise ValueError(f"issue #23 requires exactly {expected_events} event rows, got {counts['event_locations']}")
    coverage = metadata.get("mapped_coverage_by_team")
    if not isinstance(coverage, dict) or any(float(coverage.get(str(team), -1)) < 0.90 for team in TEAM_IDS):
        raise ValueError("issue #23 mapped coverage is below 90 percent for a scoped team")
    agreement = metadata.get("two_three_point_agreement", {})
    if not isinstance(agreement, dict) or float(agreement.get("agreement", -1)) < 0.99:
        raise ValueError("issue #23 two-/three-point agreement is below 99 percent")
    activation_gate = metadata.get("activation_gate")
    if activation_gate is not None and (
        not isinstance(activation_gate, dict)
        or activation_gate.get("valid_mapped_geometry") is not True
        or activation_gate.get("zone_reproduction") is not True
        or activation_gate.get("eligible") is not True
    ):
        raise ValueError("issue #23 activation gate is not satisfied")
    status_counts = metadata.get("status_counts")
    if status_counts is None:
        status_counts = metadata.get("event_status_counts")
    if status_counts is None:
        status_counts = metadata.get("mapping_status_counts")
    if status_counts is not None and status_counts != statuses:
        raise ValueError("issue #23 status counts do not match event rows")
    return {
        "metadata": metadata,
        "profiles": profiles,
        "events": events,
        "pairs": pairs,
        "player_ids": {player_id for player_id, _season in pairs},
        "counts": counts,
        "statuses": statuses,
    }


def _eligible_player_seasons(conn: Any) -> set[tuple[int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT membership.player_id, membership.season
            FROM team_roster_memberships membership
            JOIN player_seasons season_row
              ON season_row.player_id = membership.player_id
             AND season_row.team_id = membership.team_id
             AND season_row.season = membership.season
             AND season_row.source_active
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = membership.team_id
             AND eligibility.season = membership.season
            WHERE membership.season = %s
              AND membership.team_id = ANY(%s)
              AND membership.source_active
              AND season_row.minutes >= %s
            """,
            (SEASON, list(TEAM_IDS), MIN_ROTATION_MINUTES),
        )
        return {(int(player_id), int(season)) for player_id, season in cursor.fetchall()}


def _eligible_roster_memberships(conn: Any) -> set[tuple[int, int, int]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT membership.team_id, membership.player_id, membership.season
            FROM team_roster_memberships membership
            JOIN player_seasons season_row
              ON season_row.player_id = membership.player_id
             AND season_row.team_id = membership.team_id
             AND season_row.season = membership.season
             AND season_row.source_active
            JOIN team_season_eligibility eligibility
              ON eligibility.team_id = membership.team_id
             AND eligibility.season = membership.season
            WHERE membership.season = %s
              AND membership.team_id = ANY(%s)
              AND membership.source_active
              AND season_row.minutes >= %s
            """,
            (SEASON, list(TEAM_IDS), MIN_ROTATION_MINUTES),
        )
        return {(int(team_id), int(player_id), int(season)) for team_id, player_id, season in cursor.fetchall()}


def _verify_source_dependencies(conn: Any, descriptor: Mapping[str, Any]) -> None:
    for dependency in descriptor.get("source_dependencies", []):
        if not isinstance(dependency, Mapping):
            raise ValueError("issue #23 source dependency is malformed")
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM data_import_runs
                WHERE dataset = %s AND import_version = %s
                ORDER BY id DESC LIMIT 1
                """,
                (dependency.get("dataset"), dependency.get("import_version")),
            )
            row = cursor.fetchone()
        if row is None or row[0] != "published":
            raise ValueError(
                "issue #23 requires published source release "
                f"{dependency.get('dataset')}:{dependency.get('import_version')}"
            )


def _check_source_events(conn: Any, events: list[dict[str, Any]], player_ids: set[int]) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_play_id, player_id, season, game_id, team_id,
                   opponent_id, period, made, shot_range, location_x, location_y
            FROM player_shot_events
            WHERE season = %s AND player_id = ANY(%s)
              AND shot_range IS DISTINCT FROM 'free_throw'
            ORDER BY source_play_id
            """,
            (SEASON, sorted(player_ids)),
        )
        source = cursor.fetchall()
    source_ids = {int(row[0]) for row in source}
    artifact_ids = {int(row["source_play_id"]) for row in events}
    if source_ids != artifact_ids:
        raise ValueError(f"issue #23 source event completeness mismatch: missing={sorted(source_ids-artifact_ids)[:10]}, extra={sorted(artifact_ids-source_ids)[:10]}")
    by_id = {int(row[0]): row for row in source}
    for event in events:
        row = by_id[int(event["source_play_id"])]
        source_x = row[9]
        source_y = row[10]
        expected = (int(row[1]), int(row[2]), int(row[3]), row[4], row[5], row[6], None if row[7] is None else bool(row[7]), row[8], source_x, source_y)
        actual = (event["player_id"], event["season"], event["game_id"], event["team_id"], event["opponent_id"], event["period"], event["made"], event["shot_range"], event["location_x"], event["location_y"])
        if expected[:-2] != actual[:-2] or not _same_source_coordinate(expected[-2], actual[-2]) or not _same_source_coordinate(expected[-1], actual[-1]):
            raise ValueError(f"issue #23 source event changed for {event['source_play_id']}")


def _check_source_profiles(conn: Any, profiles: list[dict[str, Any]], player_ids: set[int]) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT player_id, season, zone, attempts, makes, attempt_share,
                   posterior_alpha, posterior_beta, adjusted_pps
            FROM player_shot_location_profiles
            WHERE model_version = %s AND season = %s AND player_id = ANY(%s)
            """,
            (SOURCE_PROFILE_VERSION, SEASON, sorted(player_ids)),
        )
        source = cursor.fetchall()
    expected = {(int(row["player_id"]), int(row["season"]), str(row["zone"])): row for row in profiles}
    actual = {(int(row[0]), int(row[1]), str(row[2])): row for row in source}
    if set(expected) != set(actual):
        raise ValueError("issue #23 audited source profile completeness mismatch")
    for key, row in actual.items():
        artifact = expected[key]
        values = (int(row[3]), int(row[4]), float(row[5]), float(row[6]), float(row[7]), float(row[8]))
        expected_values = (artifact["attempts"], artifact["makes"], artifact["attempt_share"], artifact["posterior_alpha"], artifact["posterior_beta"], artifact["adjusted_pps"])
        if values != expected_values:
            raise ValueError(f"issue #23 audited profile changed for {key}")


def _check_source_directions(conn: Any, events: list[dict[str, Any]]) -> None:
    """Ensure orientation was derived from the complete current game sample."""
    game_ids = sorted({int(event["game_id"]) for event in events})
    if not game_ids:
        return
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_play_id, game_id, team_id, opponent_id, period,
                   location_x
            FROM player_shot_events
            WHERE season = %s AND game_id = ANY(%s)
              AND shot_range IS DISTINCT FROM 'free_throw'
            """,
            (SEASON, game_ids),
        )
        rows = cursor.fetchall()
    observations = [
        ShotCoordinate(int(row[0]), int(row[1]), row[2], row[3], row[4], row[5])
        for row in rows
    ]
    directions = infer_attacking_baskets(observations)
    for event in events:
        basket = event.get("attacking_basket")
        if basket is not None and directions.baskets.get(int(event["source_play_id"])) != basket:
            raise ValueError(f"issue #23 source game direction changed for {event['source_play_id']}")


def _rows_equal(left: Mapping[str, Any], right: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    return all(left.get(field) == right.get(field) for field in fields)


def _same_source_coordinate(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)
    if not math.isfinite(left_number) or not math.isfinite(right_number):
        return not math.isfinite(left_number) and not math.isfinite(right_number)
    return left_number == right_number


def apply(conn: Any, root: Path, descriptor: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    """Apply immutable rows and independently activate the profile version."""

    if Json is None or execute_values is None:
        raise RuntimeError("psycopg2 is required to apply issue #23")
    metadata = prepared["metadata"]
    pairs = set(prepared["pairs"])
    _verify_source_dependencies(conn, descriptor)
    actual_pairs = _eligible_player_seasons(conn)
    if actual_pairs != pairs:
        raise ValueError(f"issue #23 eligibility mismatch: missing={sorted(actual_pairs-pairs)[:20]}, extra={sorted(pairs-actual_pairs)[:20]}")
    if metadata.get("eligible_roster_memberships") is not None:
        expected_memberships = {
            (int(item["team_id"]), int(item["player_id"]), int(item["season"]))
            for item in metadata["eligible_roster_memberships"]
        }
        actual_memberships = _eligible_roster_memberships(conn)
        if actual_memberships != expected_memberships:
            raise ValueError(f"issue #23 roster-membership mismatch: missing={sorted(actual_memberships-expected_memberships)[:20]}, extra={sorted(expected_memberships-actual_memberships)[:20]}")
    _check_source_events(conn, prepared["events"], set(prepared["player_ids"]))
    _check_source_directions(conn, prepared["events"])
    _check_source_profiles(conn, prepared["profiles"], set(prepared["player_ids"]))

    configuration = metadata.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("issue #23 model configuration is missing")
    expected_configuration = {
        "model_version": MODEL_VERSION,
        "base_model_version": SOURCE_PROFILE_VERSION,
        "zone_scheme": list(ZONES),
        "season": SEASON,
        "team_ids": list(TEAM_IDS),
        "rotation_minimum_minutes": MIN_ROTATION_MINUTES,
        "active_roster_membership": True,
        "direction_inference": "all_field_goals_in_relevant_game",
        "activation": "profile_only",
    }
    if not all(configuration.get(key) == value for key, value in expected_configuration.items()):
        raise ValueError("issue #23 model configuration is not the locked pilot configuration")
    model_version = str(metadata["model_version"])
    profiles = prepared["profiles"]
    events = prepared["events"]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT configuration, is_profile_active FROM shot_location_model_versions WHERE version = %s FOR UPDATE",
            (model_version,),
        )
        existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                "INSERT INTO shot_location_model_versions (version, is_active, is_profile_active, configuration) VALUES (%s, FALSE, FALSE, %s)",
                (model_version, Json(configuration)),
            )
        elif not _configuration_contains(existing[0], configuration):
            raise ValueError("issue #23 model configuration conflicts with the database")

        # Existing rows are immutable: a replay may see equal values, but any
        # conflicting value is rejected before INSERT can hide the problem.
        cursor.execute(
            "SELECT player_id, season, zone, attempts, makes, attempt_share, posterior_alpha, posterior_beta, adjusted_pps FROM player_shot_location_profiles WHERE model_version = %s",
            (model_version,),
        )
        existing_profiles = {
            (int(row[0]), int(row[1]), str(row[2])): {
                "player_id": int(row[0]), "season": int(row[1]), "zone": str(row[2]),
                "attempts": int(row[3]), "makes": int(row[4]), "attempt_share": float(row[5]),
                "posterior_alpha": float(row[6]), "posterior_beta": float(row[7]), "adjusted_pps": float(row[8]),
            }
            for row in cursor.fetchall()
        }
        for row in profiles:
            key = (row["player_id"], row["season"], row["zone"])
            if key in existing_profiles and not _rows_equal(existing_profiles[key], row, ("attempts", "makes", "attempt_share", "posterior_alpha", "posterior_beta", "adjusted_pps")):
                raise ValueError(f"issue #23 conflicting existing profile row: {key}")
        missing_profiles = [row for row in profiles if (row["player_id"], row["season"], row["zone"]) not in existing_profiles]
        if missing_profiles:
            execute_values(
                cursor,
                """INSERT INTO player_shot_location_profiles
                   (player_id, season, model_version, zone, attempts, makes,
                    attempt_share, posterior_alpha, posterior_beta, adjusted_pps)
                   VALUES %s""",
                [tuple(row[field] for field in PROFILE_FIELDS) for row in missing_profiles],
                page_size=1000,
            )

        cursor.execute(
            "SELECT source_play_id, mapping_status, normalized_x, normalized_y, zone FROM player_shot_event_locations WHERE model_version = %s",
            (model_version,),
        )
        existing_events = {
            int(row[0]): {"mapping_status": str(row[1]), "normalized_x": row[2], "normalized_y": row[3], "zone": row[4]}
            for row in cursor.fetchall()
        }
        for row in events:
            key = row["source_play_id"]
            if key in existing_events:
                current = existing_events[key]
                if any(current[field] != row[field] for field in ("mapping_status", "normalized_x", "normalized_y", "zone")):
                    raise ValueError(f"issue #23 conflicting existing event row: {key}")
        missing_events = [row for row in events if row["source_play_id"] not in existing_events]
        if missing_events:
            execute_values(
                cursor,
                """INSERT INTO player_shot_event_locations
                   (source_play_id, model_version, mapping_status,
                    normalized_x, normalized_y, zone)
                   VALUES %s""",
                [
                    (row["source_play_id"], model_version, row["mapping_status"], row["normalized_x"], row["normalized_y"], row["zone"])
                    for row in missing_events
                ],
                page_size=1000,
            )

        # Profile activation is independent of projection activation.  Never
        # update is_active/activated_at in this transaction.
        cursor.execute(
            "UPDATE shot_location_model_versions SET is_profile_active = FALSE WHERE is_profile_active AND version <> %s",
            (model_version,),
        )
        cursor.execute(
            "UPDATE shot_location_model_versions SET is_profile_active = TRUE, profile_activated_at = COALESCE(profile_activated_at, now()) WHERE version = %s",
            (model_version,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("issue #23 scoped model version was not activated")

    return {
        "row_counts": prepared["counts"],
        "validation": {
            "eligible_player_seasons": len(actual_pairs),
            "published_player_shot_location_profiles": len(profiles),
            "published_player_shot_event_locations": len(events),
            "model_version": model_version,
            "profile_activation": True,
        },
    }


__all__ = ["apply", "validate_artifact"]
