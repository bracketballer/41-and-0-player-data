"""Fit the v1 team-level defense-tilt coefficient.

The command reads stored shot events or the existing JSONL.GZ export, derives
the five coordinate zones, and writes aggregate diagnostics only.  It never
publishes raw CBBD payloads.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.defense_tilt import (
    TeamGameZoneCounts,
    build_leave_one_game_out_features,
    fit_global_lambda,
    fit_per_zone_lambda,
    predict_log_loss,
    select_model,
)
from bracketballer_data.shot_zones import FIELD_GOAL_ZONES, ShotCoordinate, classify_shot, infer_attacking_baskets
from scripts.analysis.audit_shot_zones import AuditEvent, iter_database_events, iter_export_events

FIRST_SEASON = 2024
LAST_SEASON = 2026
DEFAULT_REPORT = Path("data/reports/defense-tilt/defense-tilt-fit.json")


def build_observations(events: Iterable[AuditEvent]) -> list[TeamGameZoneCounts]:
    rows = [event for event in events if event.shot_range != "free_throw"]
    direction = infer_attacking_baskets(
        ShotCoordinate(
            event_id=event.event_id,
            game_id=event.game_id,
            team_id=event.team_id,
            opponent_id=event.opponent_id,
            period=event.period,
            location_x=event.location_x,
        )
        for event in rows
    )
    zone_index = {zone: index for index, zone in enumerate(FIELD_GOAL_ZONES)}
    grouped: dict[tuple[int, int, int, int], list[int]] = defaultdict(
        lambda: [0] * len(FIELD_GOAL_ZONES)
    )
    for event in rows:
        if event.team_id is None or event.opponent_id is None:
            continue
        zone = classify_shot(event.location_x, event.location_y, direction.baskets.get(event.event_id))
        if zone is None:
            continue
        key = (event.season, event.game_id, event.team_id, event.opponent_id)
        grouped[key][zone_index[zone]] += 1
    return [
        TeamGameZoneCounts(
            season=season,
            game_id=game_id,
            offense_team_id=offense,
            defense_team_id=defense,
            counts=tuple(counts),
        )
        for (season, game_id, offense, defense), counts in sorted(grouped.items())
        if sum(counts) > 0
    ]


def _fit_dict(fit, *, zones: tuple[str, ...] = FIELD_GOAL_ZONES) -> dict:
    if len(fit.coefficients) == 1:
        coefficients = {zone: fit.coefficients[0] for zone in zones}
        standard_errors = {zone: fit.standard_errors[0] for zone in zones}
    else:
        coefficients = dict(zip(zones, fit.coefficients))
        standard_errors = dict(zip(zones, fit.standard_errors))
    return {
        "coefficients": coefficients,
        "standard_errors": standard_errors,
        "log_likelihood": fit.log_likelihood,
        "observations": fit.observations,
        "attempts": fit.attempts,
    }


def fit_report(observations: list[TeamGameZoneCounts]) -> dict:
    features = build_leave_one_game_out_features(observations)
    global_fit = fit_global_lambda(features)
    per_zone_fit = fit_per_zone_lambda(features)
    held_out_global: list[float] = []
    held_out_per_zone: list[float] = []
    held_out_seasons: list[int] = []
    seasons = sorted({row.season for row in observations})
    for season in seasons:
        train = [row for row in observations if row.season != season]
        test = [row for row in observations if row.season == season]
        if not train or not test:
            continue
        try:
            train_features = build_leave_one_game_out_features(train)
            test_features = build_leave_one_game_out_features(test)
            train_global = fit_global_lambda(train_features)
            train_per_zone = fit_per_zone_lambda(train_features)
        except ValueError:
            continue
        held_out_global.append(predict_log_loss(test_features, train_global))
        held_out_per_zone.append(predict_log_loss(test_features, train_per_zone))
        held_out_seasons.append(season)
    if held_out_global:
        selected_model = select_model(global_fit, per_zone_fit, held_out_global, held_out_per_zone)
    else:
        selected_model = "global"
    selected_fit = per_zone_fit if selected_model == "per_zone" else global_fit
    selected_coefficients = (
        dict(zip(FIELD_GOAL_ZONES, selected_fit.coefficients))
        if len(selected_fit.coefficients) > 1
        else {zone: selected_fit.coefficients[0] for zone in FIELD_GOAL_ZONES}
    )
    return {
        "schema_version": 1,
        "seasons": seasons,
        "zones": list(FIELD_GOAL_ZONES),
        "profile_smoothing": 0.5,
        "observations": len(observations),
        "games": len({row.game_id for row in observations}),
        "attempts": sum(sum(row.counts) for row in observations),
        "global_fit": _fit_dict(global_fit),
        "per_zone_fit": _fit_dict(per_zone_fit),
        "held_out_log_loss": {
            "seasons": held_out_seasons,
            "global": held_out_global,
            "per_zone": held_out_per_zone,
        },
        "selected_model": selected_model,
        "selected_coefficients": selected_coefficients,
    }


def run(
    *,
    first: int,
    last: int,
    report_path: Path,
    shots_export: Path | None = None,
    database_url: str | None = None,
) -> dict:
    load_env_file()
    if shots_export is not None:
        events = iter_export_events(shots_export, first, last)
        source = str(shots_export)
    else:
        events = iter_database_events(database_url or connection_dsn(), first, last)
        source = "player_shot_events"
    observations = build_observations(events)
    if not observations:
        raise ValueError("no usable coordinate-derived team-game observations were found")
    report = fit_report(observations)
    report["source"] = source
    report["first_season"] = first
    report["last_season"] = last
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote defense-tilt fit -> {report_path}")
    print(f"observations={report['observations']} games={report['games']} attempts={report['attempts']}")
    print(f"selected_model={report['selected_model']}")
    for zone, value in report["selected_coefficients"].items():
        print(f"  {zone}: {value:.8f}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--shots-export", type=Path)
    parser.add_argument("--database-url")
    args = parser.parse_args()
    if args.first > args.last:
        parser.error("--first cannot exceed --last")
    run(
        first=args.first,
        last=args.last,
        report_path=args.report,
        shots_export=args.shots_export,
        database_url=args.database_url,
    )


if __name__ == "__main__":
    main()
