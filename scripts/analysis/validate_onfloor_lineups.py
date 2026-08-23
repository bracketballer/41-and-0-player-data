"""Validate CBBD ``onFloor`` lineups against time-aligned substitution stints.

This is deliberately separate from ``extract_onfloor_lineups``.  The older
script reports game-level set consistency against aggregate lineup rows; this
command reports only comparisons that have a valid event clock and a valid
five-player temporal reference.

The source bundle is produced by ``fetch_cbbd_lineup_sources``.  Once cached,
this command does not contact CBBD.

Example::

    python -m scripts.analysis.validate_onfloor_lineups --season 2026 \
        --database-url "host=localhost dbname=bracketballer_dev user=postgres"
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.lineup_validation import (
    GameMoment,
    SubstitutionStint,
    TemporalValidationStat,
    compare_on_floor_to_reference,
    wilson_interval,
)
from scripts.analysis.fetch_cbbd_lineup_sources import (
    DEFAULT_CACHE_ROOT,
    read_json,
    sha256_file,
)


DEFAULT_REPORT_ROOT = Path("data/reports/lineups")


def load_bundle(
    season: int,
    cache_root: Path,
    player_id_by_source_id: Mapping[str, int] | None = None,
) -> tuple[dict[tuple[int, int], list[SubstitutionStint]], dict[str, Any]]:
    """Load and checksum-verify cached substitution stints."""

    source_dir = cache_root / str(season)
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"missing source bundle {manifest_path}; run "
            "fetch_cbbd_lineup_sources first"
        )
    manifest = read_json(manifest_path)
    if manifest.get("format_version") != 2 or manifest.get("season") != season:
        raise ValueError(f"unsupported or mismatched source manifest: {manifest_path}")

    stints: dict[tuple[int, int], list[SubstitutionStint]] = defaultdict(list)
    invalid_rows = 0
    for entry in manifest.get("teams", []):
        substitution_path = source_dir / entry["substitutions_file"]
        if sha256_file(substitution_path) != entry.get("sha256"):
            raise ValueError(f"source checksum mismatch: {substitution_path}")
        rows = read_json(substitution_path)
        if not isinstance(rows, list):
            raise ValueError(
                f"source substitutions are not a list: {substitution_path}"
            )
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            stint = SubstitutionStint.from_payload(row)
            if stint is None:
                invalid_rows += 1
                continue
            stints[(stint.game_id, stint.team_id)].append(stint)

    # Some source responses omit a starter's opening stint because it is not a
    # substitution. Seed it from the box score, while avoiding duplicates when
    # the substitution endpoint already returned the 20:00 entry.
    for entry in manifest.get("teams", []):
        game_players_path = source_dir / entry["game_players_file"]
        if sha256_file(game_players_path) != entry.get("game_players_sha256"):
            raise ValueError(f"source checksum mismatch: {game_players_path}")
        game_players = read_json(game_players_path)
        if not isinstance(game_players, list):
            raise ValueError(f"game-player cache is not a list: {game_players_path}")
        for game in game_players:
            if not isinstance(game, dict):
                invalid_rows += 1
                continue
            game_id = game.get("gameId", game.get("game_id"))
            team_id = game.get("teamId", game.get("team_id"))
            if not isinstance(game_id, int) or not isinstance(team_id, int):
                invalid_rows += 1
                continue
            key = (game_id, team_id)
            existing = stints[key]
            existing_starters = {
                row.player_id
                for row in existing
                if row.sub_in.period == 1 and row.sub_in.seconds_remaining == 1200
            }
            players = game.get("players")
            if not isinstance(players, list):
                continue
            for player in players:
                if not isinstance(player, dict) or player.get("starter") is not True:
                    continue
                source_id = player.get("athleteSourceId", player.get("athlete_source_id"))
                player_id = (
                    player_id_by_source_id.get(str(source_id))
                    if player_id_by_source_id is not None and source_id is not None
                    else None
                )
                if not isinstance(player_id, int) or player_id in existing_starters:
                    if player.get("starter") is True and player_id is None:
                        invalid_rows += 1
                    continue
                existing.append(
                    SubstitutionStint(
                        game_id=game_id,
                        team_id=team_id,
                        player_id=player_id,
                        sub_in=GameMoment(period=1, seconds_remaining=1200),
                        sub_out=None,
                    )
                )
    manifest["invalid_substitution_rows"] = invalid_rows
    return dict(stints), manifest


def load_known_player_ids(conn: Any) -> set[int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM players")
        return {row[0] for row in cursor.fetchall()}


def load_player_id_by_source_id(conn: Any) -> dict[str, int]:
    """Load only unambiguous source-ID mappings for box-score starters."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_id::text, MIN(id)
            FROM players
            WHERE source_id IS NOT NULL
            GROUP BY source_id
            HAVING COUNT(*) = 1
            """
        )
        return {source_id: player_id for source_id, player_id in cursor.fetchall()}


def iter_shot_events(conn: Any, season: int):
    with conn.cursor(name=f"onfloor_temporal_{season}") as cursor:
        cursor.itersize = 5000
        cursor.execute(
            """
            SELECT source_play_id, game_id, team_id, opponent_id, raw_payload
            FROM player_shot_events
            WHERE season = %s
            ORDER BY source_play_id
            """,
            (season,),
        )
        yield from cursor


def _stat_dict(stat: TemporalValidationStat) -> dict[str, Any]:
    return {
        "season": stat.season,
        "side": stat.side,
        "events_seen": stat.events_seen,
        "on_floor_valid": stat.on_floor_valid,
        "temporal_matchable": stat.matchable,
        "temporal_matches": stat.matches,
        "temporal_mismatches": stat.mismatches,
        "temporal_match_rate_pct": stat.temporal_match_rate_pct,
        "temporal_mismatch_rate_pct": stat.temporal_mismatch_rate_pct,
        "statuses": dict(sorted(stat.statuses.items())),
    }


def evaluate(
    season: int,
    database_url: str,
    cache_root: Path,
    audit_manifest: Path | None = None,
) -> dict[str, Any]:
    conn = psycopg2.connect(database_url)
    try:
        known_player_ids = load_known_player_ids(conn)
        player_id_by_source_id = load_player_id_by_source_id(conn)
        stints, source_manifest = load_bundle(
            season, cache_root, player_id_by_source_id
        )
        stats = {
            "defense": TemporalValidationStat(season, "defense"),
            "offense": TemporalValidationStat(season, "offense"),
        }
        event_rows: dict[int, tuple[Any, ...]] = {}
        for source_play_id, game_id, team_id, opponent_id, raw_payload in iter_shot_events(
            conn, season
        ):
            event_rows[int(source_play_id)] = (
                game_id,
                team_id,
                opponent_id,
                raw_payload,
            )
            defense_stints = stints.get((game_id, opponent_id), [])
            offense_stints = stints.get((game_id, team_id), [])
            stats["defense"].record(
                compare_on_floor_to_reference(
                    raw_payload, "defense", known_player_ids, defense_stints
                )
            )
            stats["offense"].record(
                compare_on_floor_to_reference(
                    raw_payload, "offense", known_player_ids, offense_stints
                )
            )
    finally:
        conn.close()

    report: dict[str, Any] = {
        "schema_version": 1,
        "season": season,
        "source": "cbbd_substitution_stints",
        "source_manifest": source_manifest,
        "sides": {side: _stat_dict(stat) for side, stat in stats.items()},
    }
    if audit_manifest is not None:
        report["independent_audit"] = evaluate_audit_manifest(
            audit_manifest, event_rows, known_player_ids
        )
    return report


def evaluate_audit_manifest(
    path: Path,
    event_rows: dict[int, tuple[Any, ...]],
    known_player_ids: set[int],
) -> dict[str, Any]:
    """Evaluate completed independent labels without trusting CBBD references."""

    manifest = read_json(path)
    if manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported audit manifest: {path}")
    counts = defaultdict(int)
    for record in manifest.get("events", []):
        status = record.get("annotation_status", "pending")
        if status != "complete":
            counts["pending_or_unusable"] += 1
            continue
        expected = record.get("expected_player_ids")
        if (
            not isinstance(expected, list)
            or len(expected) != 5
            or len(set(expected)) != 5
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in expected)
        ):
            raise ValueError(
                f"completed audit record has invalid expected_player_ids: "
                f"{record.get('source_play_id')}"
            )
        evidence = record.get("evidence")
        if (
            not isinstance(evidence, dict)
            or not evidence.get("type")
            or not evidence.get("url")
            or not evidence.get("locator")
        ):
            raise ValueError(
                f"completed audit record is missing evidence: "
                f"{record.get('source_play_id')}"
            )
        evidence_text = " ".join(
            str(evidence.get(key, "")).lower() for key in ("type", "url", "locator")
        )
        if "collegebasketballdata.com" in evidence_text or "cbbd" in evidence_text:
            raise ValueError(
                f"audit evidence must be independent of CBBD: "
                f"{record.get('source_play_id')}"
            )
        source_play_id = int(record["source_play_id"])
        event = event_rows.get(source_play_id)
        if event is None:
            counts["event_missing"] += 1
            continue
        _, _, opponent_id, raw_payload = event
        if int(record.get("defensive_team_id", -1)) != int(opponent_id):
            counts["team_mismatch"] += 1
            continue
        observed = raw_payload.get("onFloor")
        observed_entries = [
            entry
            for entry in observed
            if isinstance(entry, dict)
            and entry.get("team") == raw_payload.get("opponent")
        ] if isinstance(observed, list) else []
        observed_ids = {
            entry.get("id")
            for entry in observed_entries
            if isinstance(entry.get("id"), int)
        }
        resolved = (
            len(observed_entries) == 5
            and len(observed_ids) == 5
            and observed_ids.issubset(known_player_ids)
        )
        if not resolved:
            counts["onfloor_unresolved"] += 1
        elif observed_ids == set(expected):
            counts["exact_matches"] += 1
        else:
            counts["mismatches"] += 1

    trials = counts["exact_matches"] + counts["mismatches"]
    interval = wilson_interval(counts["exact_matches"], trials)
    return {
        "manifest": str(path),
        "annotated": trials + counts["onfloor_unresolved"],
        "exact_matches": counts["exact_matches"],
        "mismatches": counts["mismatches"],
        "onfloor_unresolved": counts["onfloor_unresolved"],
        "event_missing": counts["event_missing"],
        "pending_or_unusable": counts["pending_or_unusable"],
        "exact_match_rate_pct": (100.0 * counts["exact_matches"] / trials) if trials else None,
        "wilson_95_lower_pct": 100.0 * interval[0] if interval else None,
        "wilson_95_upper_pct": 100.0 * interval[1] if interval else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--database-url")
    parser.add_argument("--audit-manifest", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    load_env_file()
    database_url = args.database_url or connection_dsn()
    report = evaluate(
        season=args.season,
        database_url=database_url,
        cache_root=args.cache_root,
        audit_manifest=args.audit_manifest,
    )
    report_path = args.report or (
        DEFAULT_REPORT_ROOT / f"onfloor_temporal_{args.season}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote temporal validation report -> {report_path}")
    for side, stats in report["sides"].items():
        print(
            f"{side}: {stats['temporal_matchable']} matchable, "
            f"{stats['temporal_mismatches']} mismatches, "
            f"{stats['temporal_match_rate_pct']!r}% match rate"
        )
    if report.get("independent_audit"):
        print(json.dumps(report["independent_audit"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
