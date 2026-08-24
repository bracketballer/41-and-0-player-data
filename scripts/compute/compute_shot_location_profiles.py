"""Compute and publish coordinate-derived player shot-location profiles.

The command reads only ingested PostgreSQL events.  It is a dry run unless
``--apply`` is supplied; an applied candidate is written to the V36
``player_shot_location_profiles`` table and audited as an inactive model
version for the later projection tickets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from typing import Any

import psycopg2
from psycopg2.extras import Json, execute_values

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.import_audit import (
    begin_import_run,
    mark_failed,
    mark_validated,
    pipeline_commit,
)
from bracketballer_data.paths import REPO_ROOT
from bracketballer_data.shot_location_profiles import (
    ACCURACY_PRIOR_ATTEMPTS,
    MIN_ROTATION_MINUTES,
    MODEL_VERSION,
    POINT_VALUES,
    SHARE_PRIOR_ATTEMPTS,
    PlayerShotLocationInput,
    SeasonShotLocationBaseline,
    ShotLocationProfile,
    ZoneCount,
    build_shot_location_profiles,
)
from bracketballer_data.shot_zones import (
    FIELD_GOAL_ZONES,
    ShotCoordinate,
    classify_shot,
    infer_attacking_baskets,
)


FIRST_SEASON = 2024
LAST_SEASON = 2026
DATASET = "shot_location_profiles"
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

MODEL_CONFIGURATION: dict[str, Any] = {
    "model_version": MODEL_VERSION,
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
    "recompute_cadence": "nightly_during_active_season",
    "activation": "inactive_candidate_until_downstream_projection_complete",
}


def model_configuration(version: str) -> dict[str, Any]:
    """Return the immutable configuration with the requested version name."""

    return {**MODEL_CONFIGURATION, "model_version": version}


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


def load_eligible_player_seasons(
    conn: Any, first: int, last: int
) -> set[tuple[int, int]]:
    """Return the distinct T3 rotation population for the requested seasons."""

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
              AND ps.minutes >= %s
            ORDER BY ps.season, ps.player_id
            """,
            (first, last, MIN_ROTATION_MINUTES),
        )
        rows = cursor.fetchall()
    return {(int(player_id), int(season)) for player_id, season in rows}


def validate_ingestion_status(
    conn: Any, eligible: set[tuple[int, int]]
) -> None:
    """Require every eligible player-season to have a completed shot fetch."""

    if not eligible:
        raise ValueError("no eligible rotation player-seasons were found")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT player_id, season, status
            FROM player_shooting_ingestion_status
            WHERE (player_id, season) IN (
                SELECT * FROM unnest(%s::integer[], %s::smallint[])
            )
            """,
            (
                [player_id for player_id, _season in sorted(eligible)],
                [season for _player_id, season in sorted(eligible)],
            ),
        )
        statuses = {
            (int(player_id), int(season)): str(status)
            for player_id, season, status in cursor.fetchall()
        }
    incomplete = sorted(
        key for key in eligible if statuses.get(key) not in {"success", "no_data"}
    )
    if incomplete:
        raise ValueError(
            "eligible player-seasons without completed shot ingestion: "
            f"{incomplete[:20]}" + (" ..." if len(incomplete) > 20 else "")
        )


def _direction_inputs(conn: Any, season: int) -> list[ShotCoordinate]:
    with conn.cursor(name=f"shot_location_direction_{season}") as cursor:
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


def _source_digest_row(values: tuple[Any, ...]) -> bytes:
    return (
        json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        + b"\n"
    )


def load_classified_inputs(
    conn: Any,
    first: int,
    last: int,
    eligible: set[tuple[int, int]],
) -> tuple[
    list[PlayerShotLocationInput],
    list[SeasonShotLocationBaseline],
    dict[str, Any],
    str,
]:
    """Classify stored events and aggregate player and league-season counts."""

    player_counts: dict[tuple[int, int], dict[str, ZoneCount]] = {
        key: {zone: ZoneCount() for zone in FIELD_GOAL_ZONES} for key in eligible
    }
    league_counts: dict[int, dict[str, list[int]]] = {
        season: {zone: [0, 0] for zone in FIELD_GOAL_ZONES}
        for season in range(first, last + 1)
    }
    diagnostics: dict[str, Any] = {
        "eligible_player_seasons": len(eligible),
        "seasons": {},
    }
    digest = hashlib.sha256()
    for player_id, season in sorted(eligible):
        digest.update(_source_digest_row(("eligible", season, player_id)))

    for season in range(first, last + 1):
        directions = infer_attacking_baskets(_direction_inputs(conn, season))
        season_total = 0
        classified = 0
        unclassified = 0
        with conn.cursor(name=f"shot_location_events_{season}") as cursor:
            cursor.itersize = 10_000
            cursor.execute(
                """
                SELECT source_play_id, game_id, team_id, opponent_id, player_id,
                       period, made, location_x, location_y
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
            ) in cursor:
                if made is None:
                    raise ValueError(f"source event {source_play_id} has no make/miss value")
                season_total += 1
                digest.update(
                    _source_digest_row(
                        (
                            int(source_play_id),
                            season,
                            int(game_id),
                            _integer(team_id),
                            _integer(opponent_id),
                            int(player_id),
                            _integer(period),
                            bool(made),
                            _finite(location_x),
                            _finite(location_y),
                        )
                    )
                )
                attacking_basket = directions.baskets.get(int(source_play_id))
                zone = classify_shot(
                    _finite(location_x), _finite(location_y), attacking_basket
                )
                if zone is None:
                    unclassified += 1
                    continue
                classified += 1
                league_counts[season][zone][0] += 1
                league_counts[season][zone][1] += int(bool(made))
                key = (int(player_id), season)
                if key in player_counts:
                    previous = player_counts[key][zone]
                    player_counts[key][zone] = ZoneCount(
                        attempts=previous.attempts + 1,
                        makes=previous.makes + int(bool(made)),
                    )

        diagnostics["seasons"][str(season)] = {
            "field_goal_events": season_total,
            "classified_events": classified,
            "unclassified_events": unclassified,
            "coordinate_coverage": classified / season_total if season_total else 0.0,
        }

    baselines = [
        SeasonShotLocationBaseline(
            season=season,
            zones={
                zone: ZoneCount(attempts=values[0], makes=values[1])
                for zone, values in sorted(league_counts[season].items())
            },
        )
        for season in range(first, last + 1)
    ]
    inputs = [
        PlayerShotLocationInput(player_id=player_id, season=season, zones=zones)
        for (player_id, season), zones in sorted(player_counts.items())
    ]
    diagnostics["zero_classified_attempt_profiles"] = sum(
        sum(count.attempts for count in zones.values()) == 0
        for zones in player_counts.values()
    )
    diagnostics["thin_profiles_below_10_attempts"] = sum(
        sum(count.attempts for count in zones.values()) < 10
        for zones in player_counts.values()
    )
    return inputs, baselines, diagnostics, digest.hexdigest()


def _validate_profile_output(
    inputs: list[PlayerShotLocationInput], profiles: list[ShotLocationProfile]
) -> dict[str, Any]:
    expected_keys = {(item.season, item.player_id) for item in inputs}
    grouped: dict[tuple[int, int], list[ShotLocationProfile]] = defaultdict(list)
    for profile in profiles:
        grouped[(profile.season, profile.player_id)].append(profile)
        if not (0.0 <= profile.attempt_share <= 1.0):
            raise ValueError(f"invalid attempt share: {profile}")
        if profile.posterior_alpha <= 0 or profile.posterior_beta <= 0:
            raise ValueError(f"invalid posterior: {profile}")
        if not 0.0 <= profile.adjusted_pps <= 3.0:
            raise ValueError(f"invalid adjusted PPS: {profile}")

    if set(grouped) != expected_keys:
        raise ValueError("profile keys do not match the eligible player-season population")
    bad_zone_counts = [
        key for key, rows in grouped.items() if len(rows) != len(FIELD_GOAL_ZONES)
    ]
    if bad_zone_counts:
        raise ValueError(f"profiles without exactly five zones: {bad_zone_counts[:10]}")
    bad_shares = [
        key
        for key, rows in grouped.items()
        if not math.isclose(sum(row.attempt_share for row in rows), 1.0, abs_tol=1e-12)
    ]
    if bad_shares:
        raise ValueError(f"profile shares do not sum to one: {bad_shares[:10]}")
    return {
        "eligible_player_seasons": len(expected_keys),
        "profile_rows": len(profiles),
        "profiles_with_five_zones": len(grouped),
        "shares_sum_checked": len(grouped),
    }


def _assert_version_available(conn: Any, version: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM shot_location_model_versions WHERE version = %s",
            (version,),
        )
        if cursor.fetchone() is not None:
            raise ValueError(f"model version already exists and is immutable: {version}")
        cursor.execute(
            """
            SELECT 1 FROM data_import_runs
            WHERE dataset = %s AND import_version = %s
            """,
            (DATASET, version),
        )
        if cursor.fetchone() is not None:
            raise ValueError(f"audit release already exists and is immutable: {version}")


def publish_profiles(
    conn: Any,
    profiles: list[ShotLocationProfile],
    version: str,
    run_id: int,
    source_summary: dict[str, Any],
    configuration: dict[str, Any],
) -> None:
    records = [
        (
            profile.player_id,
            profile.season,
            version,
            profile.zone,
            profile.attempts,
            profile.makes,
            profile.attempt_share,
            profile.posterior_alpha,
            profile.posterior_beta,
            profile.adjusted_pps,
        )
        for profile in profiles
    ]
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO shot_location_model_versions (version, is_active, configuration)
            VALUES (%s, FALSE, %s)
            """,
            (version, Json(configuration)),
        )
        execute_values(
            cursor,
            f"""
            INSERT INTO player_shot_location_profiles ({', '.join(PROFILE_COLUMNS)})
            VALUES %s
            """,
            records,
            page_size=1_000,
        )
        cursor.execute(
            """
            UPDATE data_import_runs
            SET status = 'published',
                published_row_counts = %s::jsonb,
                published_at = now(),
                finished_at = now()
            WHERE id = %s AND status = 'validated'
            """,
            (
                json.dumps(
                    {
                        "player_shot_location_profiles": len(records),
                        "eligible_player_seasons": source_summary[
                            "eligible_player_seasons"
                        ],
                    },
                    sort_keys=True,
                ),
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"import run {run_id} is not validated")
    conn.commit()


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.first < FIRST_SEASON or args.first > args.last:
        raise ValueError(f"season range must begin in {FIRST_SEASON} and be ordered")
    load_env_file()
    dsn = connection_dsn()
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=not args.apply)
    conn.autocommit = False
    run_id: int | None = None
    configuration = model_configuration(args.version)
    try:
        if args.apply:
            _assert_version_available(conn, args.version)
            run_id = begin_import_run(
                conn,
                dataset=DATASET,
                import_version=args.version,
                commit=pipeline_commit(REPO_ROOT),
                source_uri=(
                    "postgres://player_shot_events+eligible_rotation"
                    f"?first_season={args.first}&last_season={args.last}"
                ),
                first_season=args.first,
                last_season=args.last,
                model_version=args.version,
                metadata=configuration,
            )
        eligible = load_eligible_player_seasons(conn, args.first, args.last)
        validate_ingestion_status(conn, eligible)
        inputs, baselines, diagnostics, source_sha256 = load_classified_inputs(
            conn, args.first, args.last, eligible
        )
        profiles = build_shot_location_profiles(inputs, baselines)
        output_summary = _validate_profile_output(inputs, profiles)
        summary = {
            **diagnostics,
            **output_summary,
            "source_sha256": source_sha256,
            "model_version": args.version,
            "configuration": configuration,
        }
        if not args.apply:
            conn.rollback()
            return {"status": "dry_run", **summary}

        mark_validated(
            conn,
            run_id,
            source_sha256=source_sha256,
            staged_row_counts={"player_shot_location_profiles": len(profiles)},
            validation_results=summary,
        )
        publish_profiles(
            conn,
            profiles,
            args.version,
            run_id,
            output_summary,
            configuration,
        )
        return {"status": "published", **summary}
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
    parser.add_argument("--first", type=int, default=FIRST_SEASON)
    parser.add_argument("--last", type=int, default=LAST_SEASON)
    parser.add_argument("--version", default=MODEL_VERSION)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(_run(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
