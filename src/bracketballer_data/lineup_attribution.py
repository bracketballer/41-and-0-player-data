"""Pure logic for resolving on-floor lineups from stored shot-event payloads.

CBBD's ``onFloor`` array on a play object lists all ten players on the court
(both teams combined, tagged with each entry's ``team`` name) at the moment of
the shot. ``entry["id"]`` is CBBD's own athlete id, which this database uses
directly as ``players.id`` -- there is no separate mapping table to join
through. A player only has a ``players`` row if they were themselves ingested
as a shot-taker (see ``scripts/ingest/ingest_cbbd_shots.py``), so an on-floor
teammate who never attempted a shot may be unresolvable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import AbstractSet, Any, Iterable, Mapping

REQUIRED_LINEUP_SIZE = 5


def extract_on_floor_by_team(raw_payload: Mapping[str, Any]) -> dict[str, list[int]]:
    """Split a shot event's ``onFloor`` array into per-team athlete-id lists.

    Entries with a missing/non-integer id or missing team name are dropped
    rather than raising, since payload noise should degrade coverage stats
    instead of crashing the extraction job.
    """
    on_floor = raw_payload.get("onFloor")
    if not isinstance(on_floor, list):
        return {}

    by_team: dict[str, list[int]] = defaultdict(list)
    for entry in on_floor:
        if not isinstance(entry, Mapping):
            continue
        team = entry.get("team")
        athlete_id = entry.get("id")
        if not isinstance(team, str) or not team:
            continue
        if not isinstance(athlete_id, int):
            continue
        by_team[team].append(athlete_id)
    return dict(by_team)


@dataclass(frozen=True)
class ResolvedLineup:
    """The result of resolving one team's on-floor ids for a single event."""

    team: str
    on_floor_count: int
    resolved_player_ids: frozenset[int]

    @property
    def resolved_count(self) -> int:
        return len(self.resolved_player_ids)

    @property
    def is_valid_five(self) -> bool:
        return (
            self.on_floor_count == REQUIRED_LINEUP_SIZE
            and self.resolved_count == REQUIRED_LINEUP_SIZE
        )


def resolve_team_lineup(
    team: str, athlete_ids: Iterable[int], known_player_ids: AbstractSet[int]
) -> ResolvedLineup:
    """Resolve one team's on-floor athlete ids against known ``players`` ids."""
    ids = list(athlete_ids)
    resolved = frozenset(i for i in ids if i in known_player_ids)
    return ResolvedLineup(team=team, on_floor_count=len(ids), resolved_player_ids=resolved)


def resolve_event_lineups(
    raw_payload: Mapping[str, Any], known_player_ids: AbstractSet[int]
) -> dict[str, ResolvedLineup]:
    """Resolve both teams' on-floor lineups for a single shot event."""
    by_team = extract_on_floor_by_team(raw_payload)
    return {
        team: resolve_team_lineup(team, ids, known_player_ids)
        for team, ids in by_team.items()
    }


def resolve_event_side(
    raw_payload: Mapping[str, Any],
    side: str,
    known_player_ids: AbstractSet[int],
) -> ResolvedLineup:
    """Resolve one side ("offense" or "defense") of a single shot event.

    "offense" is ``raw_payload["team"]`` (the shooter's team); "defense" is
    ``raw_payload["opponent"]``. Always returns a ``ResolvedLineup`` -- an
    event with an empty/missing ``onFloor`` (all pre-2024 rows, and some
    2024+ rows) or a missing team name resolves to ``on_floor_count=0``
    rather than being silently dropped, so per-event coverage denominators
    (AC: "percentage of shot events...") include every event, not just ones
    where onFloor happened to be populated.
    """
    if side not in ("offense", "defense"):
        raise ValueError(f"side must be 'offense' or 'defense', got {side!r}")
    team_name = raw_payload.get("team" if side == "offense" else "opponent")
    if not isinstance(team_name, str) or not team_name:
        return ResolvedLineup(team=team_name or "", on_floor_count=0, resolved_player_ids=frozenset())
    by_team = extract_on_floor_by_team(raw_payload)
    ids = by_team.get(team_name, [])
    return resolve_team_lineup(team_name, ids, known_player_ids)


@dataclass
class CoverageStat:
    """Aggregate coverage for one season: share of team-sides with a valid five."""

    season: int
    team_sides_seen: int = 0
    valid_five_count: int = 0

    def record(self, resolution: ResolvedLineup) -> None:
        self.team_sides_seen += 1
        if resolution.is_valid_five:
            self.valid_five_count += 1

    @property
    def coverage_pct(self) -> float | None:
        if self.team_sides_seen == 0:
            return None
        return 100.0 * self.valid_five_count / self.team_sides_seen


def compute_coverage_stats(
    resolutions_by_season: Iterable[tuple[int, ResolvedLineup]],
) -> dict[int, CoverageStat]:
    """Build per-season coverage stats from a stream of (season, resolution) pairs.

    Each event contributes one entry per team side (offense and defense), so
    the denominator is team-sides, not events.
    """
    stats: dict[int, CoverageStat] = {}
    for season, resolution in resolutions_by_season:
        stat = stats.setdefault(season, CoverageStat(season=season))
        stat.record(resolution)
    return stats


@dataclass
class DisagreementStat:
    """Aggregate lineup-vs-``team_game_lineups`` agreement for one season."""

    season: int
    comparable_count: int = 0
    disagreement_count: int = 0
    no_reference_count: int = 0

    @property
    def disagreement_rate_pct(self) -> float | None:
        if self.comparable_count == 0:
            return None
        return 100.0 * self.disagreement_count / self.comparable_count


def lineup_matches_any(
    derived: frozenset[int], stored_lineups: Iterable[frozenset[int]]
) -> bool:
    """Whether a derived five-man set matches any stored lineup, order-independent."""
    return any(derived == stored for stored in stored_lineups)


def compute_disagreement_stats(
    comparisons: Iterable[tuple[int, frozenset[int], Iterable[frozenset[int]] | None]],
) -> dict[int, DisagreementStat]:
    """Build per-season disagreement stats.

    Each item is ``(season, derived_five, stored_lineups_for_that_game_team)``.
    ``stored_lineups_for_that_game_team`` is ``None`` (or empty) when
    ``team_game_lineups`` has no rows for that game/team, which is tracked
    separately from an actual disagreement.
    """
    stats: dict[int, DisagreementStat] = {}
    for season, derived, stored_lineups in comparisons:
        stat = stats.setdefault(season, DisagreementStat(season=season))
        stored_list = list(stored_lineups) if stored_lineups else []
        if not stored_list:
            stat.no_reference_count += 1
            continue
        stat.comparable_count += 1
        if not lineup_matches_any(derived, stored_list):
            stat.disagreement_count += 1
    return stats
