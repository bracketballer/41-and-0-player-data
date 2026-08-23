"""Pure helpers for the AP Top 25 + Virginia Tech dataset boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping

VIRGINIA_TECH_TEAM_ID = 340
FIRST_SUPPORTED_SEASON = 2024
AP_POLL_TYPES = frozenset({"ap", "ap top 25"})
MINIMUM_AP_TOP_25_TEAMS = 25

POSITION_MAP = {
    "PG": ("PG",),
    "SG": ("SG",),
    "SF": ("SF",),
    "PF": ("PF",),
    "C": ("C",),
    "G": ("PG", "SG"),
    "F": ("SF", "PF"),
    "G-F": ("SG", "SF"),
    "F-G": ("SG", "SF"),
    "F-C": ("PF", "C"),
    "C-F": ("PF", "C"),
}


@dataclass(frozen=True)
class EligibleTeam:
    team_id: int
    season: int
    reasons: tuple[str, ...]
    first_poll_week: int | None
    first_poll_date: date | None
    peak_rank: int | None


def _value(row: Mapping[str, Any], camel: str, snake: str | None = None) -> Any:
    return row.get(camel, row.get(snake or camel))


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def build_eligible_teams(
    rankings: Iterable[Mapping[str, Any]],
    season: int,
) -> list[EligibleTeam]:
    """Return the permanent season union of AP ranks 1-25 plus Virginia Tech."""
    if season < FIRST_SUPPORTED_SEASON:
        raise ValueError(f"Season {season} predates supported season 2024")

    ap_rows: dict[int, list[Mapping[str, Any]]] = {}
    for row in rankings:
        if int(_value(row, "season") or season) != season:
            continue
        poll_type = str(
            _value(row, "pollType", "poll_type") or ""
        ).strip().casefold()
        if poll_type not in AP_POLL_TYPES:
            continue
        ranking = _value(row, "ranking")
        team_id = _value(row, "teamId", "team_id")
        if ranking is None or team_id is None or not 1 <= int(ranking) <= 25:
            continue
        ap_rows.setdefault(int(team_id), []).append(row)

    team_ids = set(ap_rows)
    team_ids.add(VIRGINIA_TECH_TEAM_ID)
    result: list[EligibleTeam] = []
    for team_id in sorted(team_ids):
        rows = ap_rows.get(team_id, [])
        reasons = ["ap_top_25"] if rows else []
        if team_id == VIRGINIA_TECH_TEAM_ID:
            reasons.append("virginia_tech")
        ordered = sorted(
            rows,
            key=lambda row: (
                int(_value(row, "week") or 0),
                _date(_value(row, "pollDate", "poll_date")) or date.min,
            ),
        )
        result.append(
            EligibleTeam(
                team_id=team_id,
                season=season,
                reasons=tuple(reasons),
                first_poll_week=(
                    int(_value(ordered[0], "week")) if ordered else None
                ),
                first_poll_date=(
                    _date(_value(ordered[0], "pollDate", "poll_date"))
                    if ordered
                    else None
                ),
                peak_rank=(
                    min(int(_value(row, "ranking")) for row in rows)
                    if rows
                    else None
                ),
            )
        )
    return result


def validate_ap_top25_coverage(
    eligible: Iterable[EligibleTeam],
    *,
    minimum_teams: int = MINIMUM_AP_TOP_25_TEAMS,
) -> dict[str, int]:
    """Reject a ranking response that silently collapses to the VT fallback."""
    rows = list(eligible)
    ap_top25_teams = sum("ap_top_25" in row.reasons for row in rows)
    if ap_top25_teams < minimum_teams:
        raise ValueError(
            "AP Top 25 eligibility failed: "
            f"expected at least {minimum_teams} ranked teams, "
            f"found {ap_top25_teams}"
        )
    return {
        "ap_top25_teams": ap_top25_teams,
        "eligible_teams": len(rows),
    }


def normalize_positions(raw_position: str | None) -> tuple[str, ...]:
    if raw_position is None:
        return ()
    normalized = raw_position.upper().strip().replace("/", "-")
    return POSITION_MAP.get(normalized, ())


def validate_roster_coverage(
    eligible_team_ids: set[int],
    roster_team_ids: Iterable[int],
    *,
    minimum_players: int = 5,
) -> dict[str, Any]:
    counts = {team_id: 0 for team_id in eligible_team_ids}
    for team_id in roster_team_ids:
        if team_id in counts:
            counts[team_id] += 1
    missing = sorted(team_id for team_id, count in counts.items() if count == 0)
    undersized = {
        team_id: count
        for team_id, count in counts.items()
        if 0 < count < minimum_players
    }
    if missing or undersized:
        raise ValueError(
            "Roster coverage failed: "
            f"missing={missing}, undersized={undersized}"
        )
    return {
        "eligible_teams": len(eligible_team_ids),
        "roster_players": sum(counts.values()),
        "minimum_team_roster": min(counts.values()) if counts else 0,
        "maximum_team_roster": max(counts.values()) if counts else 0,
    }
