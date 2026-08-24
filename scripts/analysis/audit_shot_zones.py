"""Audit CBBD coordinate coverage and the v1 shot-zone geometry.

The preferred source is ``player_shot_events`` in PostgreSQL.  A JSONL export
can be supplied with ``--shots-export`` for an offline, read-only audit:

    python -m scripts.analysis.audit_shot_zones \
        --first 2024 --last 2026 \
        --shots-export data/exports/shots/shots_2020_2026.jsonl.gz \
        --report data/reports/shot-zones/shot-zone-audit.json

The report contains aggregate diagnostics only; it never republishes raw CBBD
coordinates or payloads.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.shot_zones import (
    FIELD_GOAL_ZONES,
    ShotCoordinate,
    classify_shot,
    infer_attacking_baskets,
)


FIRST_SEASON = 2020
LAST_SEASON = 2026
DEFAULT_REPORT = Path("data/reports/shot-zones/shot-zone-audit.json")
FREE_THROW_RANGE = "free_throw"
THREE_POINT_RANGE = "three_pointer"
TWO_POINT_RANGES = frozenset({"rim", "jumper"})


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """The non-sensitive fields needed by the aggregate audit."""

    event_id: int
    season: int
    game_id: int
    team_id: int | None
    opponent_id: int | None
    period: int | None
    shot_range: str | None
    conference: str
    location_x: Any
    location_y: Any


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


def _coordinate_state(event: AuditEvent) -> str:
    """Return ``valid``, ``missing``, or ``invalid`` for raw coordinates."""

    if event.location_x is None or event.location_y is None:
        return "missing"
    x = _finite(event.location_x)
    y = _finite(event.location_y)
    if x is None or y is None:
        return "invalid"
    if not (0.0 <= x <= 940.0 and 0.0 <= y <= 500.0):
        return "invalid"
    return "valid"


def _conference(raw_payload: Mapping[str, Any] | None) -> str:
    if not isinstance(raw_payload, Mapping):
        return "<missing>"
    value = raw_payload.get("conference")
    return str(value).strip() if value not in (None, "") else "<missing>"


def _event_from_mapping(raw: Mapping[str, Any]) -> AuditEvent:
    if not isinstance(raw, Mapping):
        raise ValueError("row must be a JSON object")
    payload = raw.get("raw_payload")
    return AuditEvent(
        event_id=int(raw["source_play_id"]),
        season=int(raw["season"]),
        game_id=int(raw["game_id"]),
        team_id=_integer(raw.get("team_id")),
        opponent_id=_integer(raw.get("opponent_id")),
        period=_integer(raw.get("period")),
        shot_range=(
            str(raw.get("shot_range"))
            if raw.get("shot_range") is not None
            else None
        ),
        conference=_conference(payload if isinstance(payload, Mapping) else None),
        location_x=raw.get("location_x"),
        location_y=raw.get("location_y"),
    )


def iter_export_events(path: Path, first: int, last: int) -> Iterator[AuditEvent]:
    """Stream a snake_case JSONL or JSONL.GZ shot export."""

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event = _event_from_mapping(raw)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid shot export row {line_number}: {error}") from error
            if first <= event.season <= last and event.shot_range != FREE_THROW_RANGE:
                yield event


def iter_database_events(
    database_url: str, first: int, last: int
) -> Iterator[AuditEvent]:
    """Stream field-goal events from PostgreSQL in a read-only session."""

    try:
        import psycopg2
    except ImportError as error:  # pragma: no cover - exercised only without extras
        raise RuntimeError("psycopg2 is required for the database audit") from error

    conn = psycopg2.connect(database_url)
    conn.set_session(readonly=True)
    try:
        with conn.cursor(name="shot_zone_audit_events") as cursor:
            cursor.itersize = 5000
            cursor.execute(
                """
                SELECT source_play_id, season, game_id, team_id, opponent_id,
                       period, shot_range, location_x, location_y, raw_payload
                FROM player_shot_events
                WHERE season BETWEEN %s AND %s
                  AND shot_range IS DISTINCT FROM %s
                ORDER BY source_play_id
                """,
                (first, last, FREE_THROW_RANGE),
            )
            for row in cursor:
                (
                    source_play_id,
                    season,
                    game_id,
                    team_id,
                    opponent_id,
                    period,
                    shot_range,
                    location_x,
                    location_y,
                    raw_payload,
                ) = row
                yield AuditEvent(
                    event_id=int(source_play_id),
                    season=int(season),
                    game_id=int(game_id),
                    team_id=_integer(team_id),
                    opponent_id=_integer(opponent_id),
                    period=_integer(period),
                    shot_range=str(shot_range) if shot_range is not None else None,
                    conference=_conference(raw_payload),
                    location_x=location_x,
                    location_y=location_y,
                )
    finally:
        conn.close()


def _new_metrics() -> dict[str, Any]:
    return {
        "field_goal_attempts": 0,
        "located_attempts": 0,
        "classified_attempts": 0,
        "missing_coordinates": 0,
        "invalid_coordinates": 0,
        "unresolved_direction": 0,
        "coordinate_coverage_pct": None,
        "classification_coverage_pct": None,
        "zone_counts": {zone: 0 for zone in FIELD_GOAL_ZONES},
    }


def _finish_metrics(metrics: dict[str, Any]) -> None:
    attempts = metrics["field_goal_attempts"]
    if attempts:
        metrics["coordinate_coverage_pct"] = round(
            100.0 * metrics["located_attempts"] / attempts, 4
        )
        metrics["classification_coverage_pct"] = round(
            100.0 * metrics["classified_attempts"] / attempts, 4
        )


def _increment_metrics(metrics: dict[str, Any], event: AuditEvent, zone: str | None) -> None:
    metrics["field_goal_attempts"] += 1
    state = _coordinate_state(event)
    if state == "valid":
        metrics["located_attempts"] += 1
    elif state == "missing":
        metrics["missing_coordinates"] += 1
    else:
        metrics["invalid_coordinates"] += 1
    if state == "valid" and zone is None:
        metrics["unresolved_direction"] += 1
    if zone is not None:
        metrics["classified_attempts"] += 1
        metrics["zone_counts"][zone] += 1


def summarize_events(events: Iterable[AuditEvent]) -> dict[str, Any]:
    """Build a deterministic aggregate report from field-goal events."""

    # Keep this helper safe for callers that pass a mixed event stream even
    # though both built-in iterators already filter free throws.
    rows = [event for event in events if event.shot_range != FREE_THROW_RANGE]
    observations = [
        ShotCoordinate(
            event_id=event.event_id,
            game_id=event.game_id,
            team_id=event.team_id,
            opponent_id=event.opponent_id,
            period=event.period,
            location_x=_finite(event.location_x),
        )
        for event in rows
    ]
    direction = infer_attacking_baskets(observations)

    seasons: dict[int, dict[str, Any]] = defaultdict(_new_metrics)
    conferences: dict[tuple[int, str], dict[str, Any]] = defaultdict(_new_metrics)
    geometry_confusion: Counter[tuple[str, str]] = Counter()
    direction_by_season: Counter[int] = Counter()
    direction_votes_by_season: Counter[int] = Counter()
    direction_by_conference: Counter[tuple[int, str]] = Counter()
    direction_votes_by_conference: Counter[tuple[int, str]] = Counter()

    for event in rows:
        basket = direction.baskets.get(event.event_id)
        zone = classify_shot(event.location_x, event.location_y, basket)
        _increment_metrics(seasons[event.season], event, zone)
        _increment_metrics(conferences[(event.season, event.conference)], event, zone)

        vote = direction.votes.get(event.event_id)
        if vote is not None:
            direction_votes_by_season[event.season] += 1
            direction_votes_by_conference[(event.season, event.conference)] += 1
            if basket is not None and vote != basket:
                direction_by_season[event.season] += 1
                direction_by_conference[(event.season, event.conference)] += 1

        if zone is not None and event.shot_range in (
            THREE_POINT_RANGE,
            *TWO_POINT_RANGES,
        ):
            observed = "three_point" if event.shot_range == THREE_POINT_RANGE else "two_point"
            derived = "three_point" if zone in {
                "corner_three",
                "above_break_three",
            } else "two_point"
            geometry_confusion[(observed, derived)] += 1

    for metrics in seasons.values():
        _finish_metrics(metrics)
    for metrics in conferences.values():
        _finish_metrics(metrics)

    season_direction = {
        str(season): {
            "direction_votes": direction_votes_by_season[season],
            "direction_disagreements": direction_by_season[season],
            "direction_disagreement_pct": round(
                100.0 * direction_by_season[season] / direction_votes_by_season[season],
                4,
            )
            if direction_votes_by_season[season]
            else None,
        }
        for season in sorted(seasons)
    }
    conference_direction = {
        f"{season}:{conference}": {
            "season": season,
            "conference": conference,
            "direction_votes": direction_votes_by_conference[(season, conference)],
            "direction_disagreements": direction_by_conference[(season, conference)],
            "direction_disagreement_pct": round(
                100.0
                * direction_by_conference[(season, conference)]
                / direction_votes_by_conference[(season, conference)],
                4,
            )
            if direction_votes_by_conference[(season, conference)]
            else None,
        }
        for season, conference in sorted(conferences)
    }

    return {
        "schema_version": 1,
        "coordinate_scale": {
            "source_x_min": 0,
            "source_x_max": 940,
            "source_y_min": 0,
            "source_y_max": 500,
            "feet_per_source_unit": 0.1,
        },
        "seasons": {str(season): seasons[season] for season in sorted(seasons)},
        "conferences": {
            key: conferences[(season, conference)]
            for season, conference in sorted(conferences)
            for key in [f"{season}:{conference}"]
        },
        "direction": {
            "located_events_with_x_vote": direction.located_events,
            "direction_disagreements": direction.disagreements,
            "direction_disagreement_pct": round(
                100.0 * direction.disagreements / direction.located_events, 4
            )
            if direction.located_events
            else None,
            "unresolved_games": direction.unresolved_games,
            "unresolved_events": direction.unresolved_events,
            "by_season": season_direction,
            "by_conference": conference_direction,
        },
        "geometry_confusion": {
            f"{observed}->{derived}": count
            for (observed, derived), count in sorted(geometry_confusion.items())
        },
    }


def run(
    first: int,
    last: int,
    report_path: Path,
    *,
    shots_export: Path | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    load_env_file()
    if shots_export is not None:
        events = iter_export_events(shots_export, first, last)
        source = str(shots_export)
    else:
        events = iter_database_events(database_url or connection_dsn(), first, last)
        source = "player_shot_events"

    report = summarize_events(events)
    report["source"] = source
    report["first_season"] = first
    report["last_season"] = last
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote shot-zone audit -> {report_path}")
    for season, metrics in report["seasons"].items():
        print(
            f"  season {season}: {metrics['coordinate_coverage_pct'] or 'n/a'}% "
            f"coordinates ({metrics['located_attempts']}/{metrics['field_goal_attempts']})"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--shots-export", type=Path)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    run(
        args.first,
        args.last,
        args.report,
        shots_export=args.shots_export,
        database_url=args.database_url,
    )


if __name__ == "__main__":
    main()
