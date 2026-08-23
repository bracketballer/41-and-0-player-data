"""Create a deterministic independent-audit manifest for 2025--26 shots.

The output is a review queue, not ground truth.  Annotators must fill
``expected_player_ids`` and independent evidence before marking a record
``annotation_status=complete``.  The sampler never uses CBBD's lineup
reference as the expected answer.

Example::

    python -m scripts.analysis.sample_onfloor_audit --season 2026 \
        --cache-root data/raw/lineup_validation \
        --database-url "host=localhost dbname=bracketballer_dev user=postgres" \
        --output docs/audits/onfloor-2026-v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.lineup_attribution import resolve_event_side
from bracketballer_data.lineup_validation import (
    GameMoment,
    SubstitutionStint,
    nearest_boundary_distance,
)
from scripts.analysis.fetch_cbbd_lineup_sources import DEFAULT_CACHE_ROOT
from scripts.analysis.validate_onfloor_lineups import (
    load_bundle,
    load_player_id_by_source_id,
)


AUDIT_SCHEMA_VERSION = 1
SELECTION_SEED = "issue-4-concern-1-v1"
MAX_PER_GAME = 10
QUOTAS = {
    "substitution_boundary": 60,
    "ordinary_regulation": 60,
    "overtime": 15,
    "late_sequence": 15,
}


def event_hash(source_play_id: int) -> str:
    return hashlib.sha256(f"{SELECTION_SEED}:{source_play_id}".encode()).hexdigest()


def classify_event(raw_payload: dict[str, Any], stints: list[SubstitutionStint]) -> str | None:
    moment = GameMoment.from_payload(raw_payload)
    if moment is None:
        return None
    if moment.period >= 3:
        return "overtime"
    distance = nearest_boundary_distance(stints, moment)
    if distance is not None and distance <= 5:
        return "substitution_boundary"
    play_text = str(raw_payload.get("playText", "")).lower()
    play_type = str(raw_payload.get("playType", "")).lower()
    if (
        "free throw" in play_text
        or "technical" in play_text
        or "foul" in play_type
        or moment.seconds_remaining <= 60
    ):
        return "late_sequence"
    return "ordinary_regulation"


def _ordered(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda row: event_hash(int(row["source_play_id"])))


def select_quota(
    candidates: list[dict[str, Any]], quota: int, used_games: set[int], per_game: dict[int, int]
) -> list[dict[str, Any]]:
    """Prefer new games, then fill deterministically without exceeding caps."""

    selected: list[dict[str, Any]] = []
    ordered = _ordered(candidates)
    for prefer_new_game in (True, False):
        for candidate in ordered:
            if len(selected) >= quota:
                break
            game_id = int(candidate["game_id"])
            if per_game.get(game_id, 0) >= MAX_PER_GAME:
                continue
            if prefer_new_game and game_id in used_games:
                continue
            if candidate in selected:
                continue
            selected.append(candidate)
            used_games.add(game_id)
            per_game[game_id] = per_game.get(game_id, 0) + 1
    return selected


def sample(
    season: int,
    database_url: str,
    cache_root: Path,
) -> dict[str, Any]:
    conn = psycopg2.connect(database_url)
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM players")
            known_player_ids = {row[0] for row in cursor.fetchall()}
        stints, _ = load_bundle(
            season, cache_root, load_player_id_by_source_id(conn)
        )
        with conn.cursor(name=f"onfloor_audit_candidates_{season}") as cursor:
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
            for source_play_id, game_id, team_id, opponent_id, raw_payload in cursor:
                resolution = resolve_event_side(
                    raw_payload, "defense", known_player_ids
                )
                if not resolution.is_valid_five:
                    continue
                moment = GameMoment.from_payload(raw_payload)
                team_stints = stints.get((game_id, opponent_id), [])
                stratum = classify_event(raw_payload, team_stints)
                if moment is None or stratum is None:
                    continue
                candidates[stratum].append(
                    {
                        "source_play_id": int(source_play_id),
                        "game_id": int(game_id),
                        "period": moment.period,
                        "seconds_remaining": moment.seconds_remaining,
                        "defensive_team_id": int(opponent_id),
                        "defensive_team": raw_payload.get("opponent"),
                        "observed_player_ids": sorted(resolution.resolved_player_ids),
                        "stratum": stratum,
                    }
                )
    finally:
        conn.close()

    selected: list[dict[str, Any]] = []
    used_games: set[int] = set()
    per_game: dict[int, int] = {}
    shortages: dict[str, int] = {}
    for stratum, quota in QUOTAS.items():
        chosen = select_quota(candidates[stratum], quota, used_games, per_game)
        selected.extend(chosen)
        if len(chosen) < quota:
            shortages[stratum] = quota - len(chosen)

    selected.sort(key=lambda row: int(row["source_play_id"]))
    events = []
    for row in selected:
        events.append(
            {
                **row,
                "annotation_status": "pending",
                "expected_player_ids": [],
                "evidence": {
                    "type": None,
                    "url": None,
                    "locator": None,
                },
                "review_status": "not_reviewed",
                "reviewer_notes": None,
            }
        )
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "season": season,
        "selection_seed": SELECTION_SEED,
        "target_events": sum(QUOTAS.values()),
        "selected_events": len(events),
        "minimum_games": 20,
        "selected_games": len(used_games),
        "quotas": QUOTAS,
        "shortages": shortages,
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--database-url")
    parser.add_argument(
        "--output", type=Path, default=Path("docs/audits/onfloor-2026-v1.json")
    )
    args = parser.parse_args()

    load_env_file()
    report = sample(
        season=args.season,
        database_url=args.database_url or connection_dsn(),
        cache_root=args.cache_root,
    )
    if report["shortages"] or report["selected_games"] < report["minimum_games"]:
        raise SystemExit(
            "audit sample does not meet its quotas: "
            f"shortages={report['shortages']}, "
            f"games={report['selected_games']}"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote {report['selected_events']} audit candidates across "
        f"{report['selected_games']} games -> {args.output}"
    )


if __name__ == "__main__":
    main()
