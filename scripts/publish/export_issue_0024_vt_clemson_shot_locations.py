"""Export the issue #24 Virginia Tech/Clemson 2026 shot-location pilot.

The exporter is intentionally read-only.  It writes a deterministic artifact
which is later validated and applied by the ticket handler.  Direction is
inferred from every field goal in a relevant game, while only the selected
rotation players' events are emitted as enrichment rows.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import psycopg2
except ModuleNotFoundError:  # pragma: no cover - dependency is present in export environments
    psycopg2 = None  # type: ignore[assignment]

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.shot_zones import (
    FIELD_GOAL_ZONES,
    ShotCoordinate,
    ShotLocationEvent,
    enrich_shot_events,
    infer_attacking_baskets,
)
from bracketballer_data.shot_location_profiles import (
    ACCURACY_PRIOR_ATTEMPTS,
    POINT_VALUES,
    SHARE_PRIOR_ATTEMPTS,
)


FORMAT_VERSION = 1
SEASON = 2026
TEAM_IDS = (52, 340)  # Clemson, Virginia Tech
MIN_ROTATION_MINUTES = 100
SOURCE_PROFILE_VERSION = "shot-location-v1"
MODEL_VERSION = "shot-location-v1-2026-vt-clemson"
RELEASE_VERSION = "issue-0024-vt-clemson-shot-locations-2026.1"
SOURCE_PROFILE_RELEASE = "issue-0009-shot-location-2026.1"
SOURCE_SHOT_RELEASE = "issue-0006-shots-2026.1"
PROFILE_FILE = "profiles.jsonl.gz"
EVENT_FILE = "event_locations.jsonl.gz"
METADATA_FILE = "metadata.json"
STATUSES = ("mapped", "missing_coordinates", "invalid_coordinates", "unresolved_direction")
# Explicit aliases used by release manifests and external audit notebooks.
SCOPED_SEASON = SEASON
SCOPED_TEAM_IDS = TEAM_IDS
SCOPED_MODEL_VERSION = MODEL_VERSION
ISSUE_RELEASE_VERSION = RELEASE_VERSION


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_jsonl_gz(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    """Write canonical gzip bytes (including a zero timestamp)."""

    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            for row in rows:
                line = json.dumps(
                    dict(row),
                    default=_json_default,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
                compressed.write(line + b"\n")


def _row_dict(columns: tuple[str, ...], row: Iterable[Any]) -> dict[str, Any]:
    return {column: value for column, value in zip(columns, row)}


ROSTER_COLUMNS = ("team_id", "player_id", "season", "minutes")
EVENT_COLUMNS = (
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
)
PROFILE_COLUMNS = (
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


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coordinate_value(value: Any) -> Any:
    """Keep invalid source coordinates visible so they can be excluded."""
    if value is None:
        return None
    number = _finite(value)
    return number if number is not None else str(value)


def _source_digest(
    player_rows: Iterable[Mapping[str, Any]],
    profile_rows: Iterable[Mapping[str, Any]],
    event_rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the exact selected source values in canonical order."""

    digest = hashlib.sha256()
    for label, rows in (("players", player_rows), ("profiles", profile_rows), ("events", event_rows)):
        for row in rows:
            digest.update(label.encode("ascii") + b"\0")
            digest.update(
                json.dumps(dict(row), default=_json_default, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _model_configuration() -> dict[str, Any]:
    # Keep the audited issue #9 configuration visible while making the scope
    # immutable and explicit for downstream readers.
    return {
        "model_version": MODEL_VERSION,
        "base_model_version": SOURCE_PROFILE_VERSION,
        "zone_scheme": list(FIELD_GOAL_ZONES),
        "point_values": dict(POINT_VALUES),
        "rotation_eligibility": {
            "minimum_minutes": MIN_ROTATION_MINUTES,
            "minimum_classified_attempts": 0,
            "active_roster_membership": True,
        },
        "share_shrinkage": {
            "prior_attempts": SHARE_PRIOR_ATTEMPTS,
            "target": "league_season_share",
        },
        "pps_shrinkage": {
            "prior_attempts": ACCURACY_PRIOR_ATTEMPTS,
            "target": "league_season_accuracy",
            "accuracy_clamp": [0.01, 0.99],
        },
        "season_blend": "current_season_only",
        "recency_weighting": {"enabled": False, "decay": None},
        "season": SEASON,
        "team_ids": list(TEAM_IDS),
        "rotation_minimum_minutes": MIN_ROTATION_MINUTES,
        "active_roster_membership": True,
        "direction_inference": "all_field_goals_in_relevant_game",
        "activation": "profile_only",
    }


def _load_roster(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT membership.team_id, membership.player_id,
                            membership.season, season_row.minutes
            FROM team_roster_memberships membership
            LEFT JOIN player_seasons season_row
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
            ORDER BY membership.team_id, membership.player_id
            """,
            (SEASON, list(TEAM_IDS)),
        )
        rows = [_row_dict(ROSTER_COLUMNS, row) for row in cursor.fetchall()]
    for row in rows:
        row["team_id"] = int(row["team_id"])
        row["player_id"] = int(row["player_id"])
        row["season"] = int(row["season"])
        row["minutes"] = None if row["minutes"] is None else float(row["minutes"])
    return rows


def _load_profiles(conn: Any, player_ids: list[int]) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT player_id, season, model_version, zone, attempts, makes,
                   attempt_share, posterior_alpha, posterior_beta, adjusted_pps
            FROM player_shot_location_profiles
            WHERE model_version = %s AND season = %s
              AND player_id = ANY(%s)
            ORDER BY player_id, zone
            """,
            (SOURCE_PROFILE_VERSION, SEASON, player_ids),
        )
        rows = [_row_dict(PROFILE_COLUMNS, row) for row in cursor.fetchall()]
    normalized: list[dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                **row,
                "player_id": int(row["player_id"]),
                "season": int(row["season"]),
                # The scoped table row is copied exactly except for its model
                # identity, which is the immutable pilot version.
                "model_version": MODEL_VERSION,
                "attempts": int(row["attempts"]),
                "makes": int(row["makes"]),
                "attempt_share": float(row["attempt_share"]),
                "posterior_alpha": float(row["posterior_alpha"]),
                "posterior_beta": float(row["posterior_beta"]),
                "adjusted_pps": float(row["adjusted_pps"]),
            }
        )
    return normalized


def _load_events(conn: Any, player_ids: list[int]) -> list[dict[str, Any]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_play_id, player_id, season, game_id, team_id,
                   opponent_id, period, made, shot_range, location_x, location_y
            FROM player_shot_events
            WHERE season = %s
              AND player_id = ANY(%s)
              AND shot_range IS DISTINCT FROM 'free_throw'
            ORDER BY source_play_id
            """,
            (SEASON, player_ids),
        )
        rows = [_row_dict(EVENT_COLUMNS, row) for row in cursor.fetchall()]
    for row in rows:
        row["source_play_id"] = int(row["source_play_id"])
        row["player_id"] = int(row["player_id"])
        row["season"] = int(row["season"])
        row["game_id"] = int(row["game_id"])
        row["team_id"] = int(row["team_id"]) if row["team_id"] is not None else None
        row["opponent_id"] = int(row["opponent_id"]) if row["opponent_id"] is not None else None
        row["period"] = int(row["period"]) if row["period"] is not None else None
        row["made"] = None if row["made"] is None else bool(row["made"])
        row["location_x"] = _coordinate_value(row["location_x"])
        row["location_y"] = _coordinate_value(row["location_y"])
    return rows


def _load_game_events(conn: Any, game_ids: list[int]) -> list[dict[str, Any]]:
    if not game_ids:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_play_id, game_id, team_id, opponent_id, period,
                   location_x, location_y
            FROM player_shot_events
            WHERE season = %s AND game_id = ANY(%s)
              AND shot_range IS DISTINCT FROM 'free_throw'
            ORDER BY source_play_id
            """,
            (SEASON, game_ids),
        )
        rows = [
            _row_dict(
                ("source_play_id", "game_id", "team_id", "opponent_id", "period", "location_x", "location_y"),
                row,
            )
            for row in cursor.fetchall()
        ]
    return rows


def _profile_counts(profiles: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "eligible_player_seasons": len({(int(row["player_id"]), int(row["season"])) for row in profiles}),
        "player_shot_location_profiles": len(profiles),
        "profiles_with_five_zones": sum(
            len(rows) == len(FIELD_GOAL_ZONES)
            for rows in _group_by_pair(profiles).values()
        ),
    }


def _group_by_pair(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[int, int], list[Mapping[str, Any]]]:
    grouped: dict[tuple[int, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(int(row["player_id"]), int(row["season"]))].append(row)
    return grouped


def _event_artifact_row(source: Mapping[str, Any], enriched: ShotLocationEvent) -> dict[str, Any]:
    return {
        "source_play_id": int(source["source_play_id"]),
        "player_id": int(source["player_id"]),
        "season": int(source["season"]),
        "game_id": int(source["game_id"]),
        "team_id": source.get("team_id"),
        "opponent_id": source.get("opponent_id"),
        "period": source.get("period"),
        "made": source.get("made"),
        "shot_range": source.get("shot_range"),
        "location_x": source.get("location_x"),
        "location_y": source.get("location_y"),
        "model_version": MODEL_VERSION,
        "mapping_status": enriched.mapping_status,
        "normalized_x": enriched.normalized_x,
        "normalized_y": enriched.normalized_y,
        "zone": enriched.zone,
        "attacking_basket": enriched.attacking_basket,
    }


def export_issue_0024(conn: Any, destination: Path) -> dict[str, Any]:
    """Build all three issue #24 artifact files using a read-only connection."""

    destination.mkdir(parents=True, exist_ok=True)
    all_roster = _load_roster(conn)
    roster = [
        row for row in all_roster
        if row["minutes"] is not None and float(row["minutes"]) >= MIN_ROTATION_MINUTES
    ]
    # Active roster memberships without an active player-season row are also
    # explicitly unavailable.  They are retained in the artifact rather than
    # silently disappearing from the locked roster audit.
    excluded_roster = [
        row for row in all_roster
        if row["minutes"] is None or float(row["minutes"]) < MIN_ROTATION_MINUTES
    ]
    pairs = {(int(row["player_id"]), int(row["season"])) for row in roster}
    if len(roster) != 20 or len(pairs) != 20:
        raise ValueError(f"issue #24 requires exactly 20 eligible player-seasons, got memberships={len(roster)} pairs={len(pairs)}")
    if len(excluded_roster) != 7:
        raise ValueError(f"issue #24 requires exactly seven sub-100-minute roster players, got {len(excluded_roster)}")
    if {int(row["team_id"]) for row in roster} != set(TEAM_IDS):
        raise ValueError("issue #24 roster scope must contain Clemson and Virginia Tech only")
    player_ids = sorted({pair[0] for pair in pairs})
    profiles = _load_profiles(conn, player_ids)
    if {(int(row["player_id"]), int(row["season"])) for row in profiles} != pairs:
        raise ValueError("issue #24 profile rows do not cover the eligible player-seasons")
    profile_counts = _profile_counts(profiles)
    if profile_counts["player_shot_location_profiles"] != 100 or profile_counts["profiles_with_five_zones"] != 20:
        raise ValueError("issue #24 source profiles must contain exactly five audited zones per player-season")
    if any({str(row["zone"]) for row in rows} != set(FIELD_GOAL_ZONES) for rows in _group_by_pair(profiles).values()):
        raise ValueError("issue #24 source profiles contain an incomplete or duplicate zone set")

    events = _load_events(conn, player_ids)
    if len(events) != 3857:
        raise ValueError(f"issue #24 source must contain exactly 3857 field-goal events, got {len(events)}")
    if len({int(row["source_play_id"]) for row in events}) != len(events):
        raise ValueError("issue #24 source contains duplicate source_play_id values")
    if any(int(row["source_play_id"]) <= 0 for row in events):
        raise ValueError("issue #24 source contains invalid source_play_id values")
    if any(row.get("made") is None for row in events):
        raise ValueError("issue #24 source field-goal events must all have make/miss values")
    if any((int(row["player_id"]), int(row["season"])) not in pairs for row in events):
        raise ValueError("issue #24 source event is outside the active roster scope")
    if any(row.get("team_id") not in TEAM_IDS for row in events):
        raise ValueError("issue #24 source event belongs to an out-of-scope team")
    game_ids = sorted({int(row["game_id"]) for row in events})
    game_events = _load_game_events(conn, game_ids)
    observations = [
        ShotCoordinate(
            event_id=int(row["source_play_id"]),
            game_id=int(row["game_id"]),
            team_id=row.get("team_id"),
            opponent_id=row.get("opponent_id"),
            period=row.get("period"),
            location_x=row.get("location_x"),
            location_y=row.get("location_y"),
        )
        for row in game_events
    ]
    directions = infer_attacking_baskets(observations)
    enriched = enrich_shot_events(events, direction_observations=observations)
    event_rows = [_event_artifact_row(source, result) for source, result in zip(events, enriched)]
    statuses = Counter(row["mapping_status"] for row in event_rows)
    zone_counts = Counter(row["zone"] for row in event_rows if row["zone"] is not None)
    team_totals: Counter[str] = Counter()
    team_mapped: Counter[str] = Counter()
    for row in event_rows:
        key = str(row["team_id"])
        team_totals[key] += 1
        if row["mapping_status"] == "mapped":
            team_mapped[key] += 1

    comparable = [
        row
        for row in event_rows
        if row["mapping_status"] == "mapped"
        and isinstance(row.get("shot_range"), str)
        and row.get("shot_range") != "free_throw"
    ]
    agreements = 0
    for row in comparable:
        source_three = row["shot_range"] in {"three_pointer", "three", "3", "three_point"}
        zone_three = row["zone"] in {"corner_three", "above_break_three"}
        agreements += source_three == zone_three
    coverage = {
        team: (team_mapped[team] / total if total else 0.0)
        for team, total in sorted(team_totals.items())
    }
    profile_source_rows = [{**row, "model_version": SOURCE_PROFILE_VERSION} for row in profiles]
    source_sha256 = _source_digest(roster, profile_source_rows, events)
    row_counts = {
        **profile_counts,
        "event_locations": len(event_rows),
        "event_location_rows": len(event_rows),
        "player_shot_event_locations": len(event_rows),
        "excluded_sub_rotation_players": len(excluded_roster),
        **{f"status_{status}": statuses.get(status, 0) for status in STATUSES},
    }
    metadata: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "dataset": "player_shot_event_locations",
        "release_version": RELEASE_VERSION,
        "model_version": MODEL_VERSION,
        "first_season": SEASON,
        "last_season": SEASON,
        "team_ids": list(TEAM_IDS),
        "player_ids": player_ids,
        "eligible_roster_memberships": [
            {
                "team_id": int(row["team_id"]),
                "player_id": int(row["player_id"]),
                "season": int(row["season"]),
                "minutes": float(row["minutes"]),
            }
            for row in roster
        ],
        "excluded_sub_rotation_players": [
            {
                "team_id": int(row["team_id"]),
                "player_id": int(row["player_id"]),
                "season": int(row["season"]),
                "minutes": None if row["minutes"] is None else float(row["minutes"]),
                "availability": "unavailable_below_100_minutes",
            }
            for row in excluded_roster
        ],
        "eligible_player_seasons": len(pairs),
        "configuration": _model_configuration(),
        "source_releases": {
            "shot_corpus": SOURCE_SHOT_RELEASE,
            "audited_profiles": SOURCE_PROFILE_RELEASE,
            "audited_profile_model_version": SOURCE_PROFILE_VERSION,
        },
        "source_release_versions": {
            "shot_corpus": SOURCE_SHOT_RELEASE,
            "audited_profiles": SOURCE_PROFILE_RELEASE,
        },
        "source_release_checksums": {
            "shot_corpus_archive_sha256": "0754742140a5dc14e41972e03542a6ce77f63bdbd8be15233502d82625b4bff1",
            "audited_profiles_archive_sha256": "1113091d079a0366e61788d3ec6c69697a04338a9ffaf8dd73a22f355c166fe8",
        },
        "source_sha256": source_sha256,
        "row_counts": row_counts,
        "status_counts": {status: statuses.get(status, 0) for status in STATUSES},
        "event_status_counts": {status: statuses.get(status, 0) for status in STATUSES},
        "mapping_status_counts": {status: statuses.get(status, 0) for status in STATUSES},
        "mapped_coverage_by_team": coverage,
        "zone_counts": {zone: zone_counts.get(zone, 0) for zone in FIELD_GOAL_ZONES},
        "direction_diagnostics": {
            "games": len(directions.game_orientations),
            "located_events": directions.located_events,
            "disagreements": directions.disagreements,
            "unresolved_games": directions.unresolved_games,
            "unresolved_events": directions.unresolved_events,
            "relevant_game_field_goals": len(game_events),
            "game_orientations": {
                str(game_id): orientation
                for game_id, orientation in sorted(directions.game_orientations.items(), key=lambda item: str(item[0]))
            },
        },
        "two_three_point_agreement": {
            "comparable_mapped_events": len(comparable),
            "agreements": agreements,
            "agreement": agreements / len(comparable) if comparable else 0.0,
        },
        "activation_gate": {
            "valid_mapped_geometry": True,
            "zone_reproduction": True,
            "minimum_team_coverage": min(coverage.values()) if coverage else 0.0,
            "two_three_point_agreement": agreements / len(comparable) if comparable else 0.0,
            "eligible": min(coverage.values(), default=0.0) >= 0.90
            and (agreements / len(comparable) if comparable else 0.0) >= 0.99,
        },
    }
    # Top-level aliases preserve the convention used by the earlier ticket
    # artifacts while row_counts remains the canonical exact-count block.
    # ``excluded_sub_rotation_players`` is the audit list at the top level and a
    # count in row_counts, so aliases must never clobber an existing key.
    metadata.update({key: value for key, value in row_counts.items() if key not in metadata})
    _write_jsonl_gz(destination / PROFILE_FILE, profiles)
    _write_jsonl_gz(destination / EVENT_FILE, event_rows)
    (destination / METADATA_FILE).write_text(
        json.dumps(metadata, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return metadata


# Friendly aliases used by release tooling and tests.
export = export_issue_0024
export_artifact = export_issue_0024
export_scoped_release = export_issue_0024


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required to export issue #24")
    load_env_file()
    conn = psycopg2.connect(connection_dsn())
    conn.set_session(readonly=True)
    try:
        print(json.dumps(export_issue_0024(conn, args.output_dir), indent=2, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
