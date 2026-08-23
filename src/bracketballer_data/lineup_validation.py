"""Temporal validation helpers for CBBD on-floor lineups.

``team_game_lineups`` contains aggregate five-player units for a whole game,
so membership in that table cannot establish which five were on the floor for
one shot.  This module turns CBBD's starter/substitution stints into a
time-addressable reference and deliberately exposes ambiguous or malformed
states instead of guessing.

The functions are dependency-free and operate on JSON-compatible mappings so
downloaded source bundles can be validated offline and unit-tested without a
database or an API key.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .lineup_attribution import ResolvedLineup, resolve_event_side


REGULATION_PERIOD_SECONDS = 20 * 60
OVERTIME_PERIOD_SECONDS = 5 * 60
MATCHABLE = "matchable"
MATCH = "match"
MISMATCH = "mismatch"
ON_FLOOR_UNRESOLVED = "onfloor_unresolved"
MISSING_EVENT_TIME = "missing_event_time"
NO_REFERENCE = "no_reference"
UNRESOLVED_REFERENCE = "unresolved_reference"
INVALID_REFERENCE = "invalid_reference"
AMBIGUOUS_BOUNDARY = "ambiguous_boundary"


def _value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def period_length_seconds(period: int) -> int:
    """Return the regulation length for a CBBD period number."""

    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    return REGULATION_PERIOD_SECONDS if period <= 2 else OVERTIME_PERIOD_SECONDS


def elapsed_seconds(period: int, seconds_remaining: int) -> int:
    """Convert a period/descending clock pair to elapsed game seconds.

    Men's college basketball uses two 20-minute periods followed by five-
    minute overtime periods.  The result is suitable for ordering stints
    across halftime and overtime.
    """

    duration = period_length_seconds(period)
    if seconds_remaining < 0 or seconds_remaining > duration:
        raise ValueError(
            f"seconds_remaining {seconds_remaining} is outside period {period}"
        )
    if period <= 2:
        before_period = (period - 1) * REGULATION_PERIOD_SECONDS
    else:
        before_period = (2 * REGULATION_PERIOD_SECONDS) + (
            (period - 3) * OVERTIME_PERIOD_SECONDS
        )
    return before_period + duration - seconds_remaining


@dataclass(frozen=True)
class GameMoment:
    """A validated event clock and its monotonic elapsed representation."""

    period: int
    seconds_remaining: int

    @property
    def elapsed(self) -> int:
        return elapsed_seconds(self.period, self.seconds_remaining)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GameMoment | None:
        period = _strict_int(_value(payload, "period"))
        seconds = _strict_int(
            _value(payload, "secondsRemaining", "seconds_remaining")
        )
        if period is None or seconds is None:
            return None
        try:
            return cls(period=period, seconds_remaining=seconds)
        except ValueError:
            return None


@dataclass(frozen=True)
class SubstitutionStint:
    """One player's interval on the floor for one team/game."""

    game_id: int
    team_id: int
    player_id: int
    sub_in: GameMoment
    sub_out: GameMoment | None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubstitutionStint | None:
        game_id = _strict_int(_value(payload, "gameId", "game_id"))
        team_id = _strict_int(_value(payload, "teamId", "team_id"))
        player_id = _strict_int(_value(payload, "athleteId", "athlete_id"))
        sub_in_payload = _value(payload, "subIn", "sub_in")
        sub_out_payload = _value(payload, "subOut", "sub_out")
        if (
            game_id is None
            or team_id is None
            or player_id is None
            or not isinstance(sub_in_payload, Mapping)
        ):
            return None

        sub_in = GameMoment.from_payload(sub_in_payload)
        sub_out = (
            GameMoment.from_payload(sub_out_payload)
            if isinstance(sub_out_payload, Mapping)
            else None
        )
        if sub_in is None:
            return None
        if sub_out is not None and sub_out.elapsed <= sub_in.elapsed:
            return None
        return cls(
            game_id=game_id,
            team_id=team_id,
            player_id=player_id,
            sub_in=sub_in,
            sub_out=sub_out,
        )

    @property
    def start_elapsed(self) -> int:
        return self.sub_in.elapsed

    @property
    def end_elapsed(self) -> int | None:
        return self.sub_out.elapsed if self.sub_out else None

    def contains(self, moment: GameMoment) -> bool:
        """Whether the stint contains a non-boundary event moment."""

        at = moment.elapsed
        return self.start_elapsed < at and (
            self.end_elapsed is None or at < self.end_elapsed
        )


@dataclass(frozen=True)
class LineupReference:
    """Reference lineup state at one event moment."""

    status: str
    player_ids: frozenset[int] = frozenset()
    boundary: bool = False

    @property
    def is_valid_five(self) -> bool:
        return self.status == MATCHABLE and len(self.player_ids) == 5


def _boundary_at(stints: Sequence[SubstitutionStint], at: int) -> bool:
    return any(
        stint.start_elapsed == at
        or (stint.end_elapsed is not None and stint.end_elapsed == at)
        for stint in stints
    )


def lineups_at_moment(
    stints: Iterable[SubstitutionStint],
    moment: GameMoment,
    known_player_ids: set[int] | None = None,
) -> LineupReference:
    """Resolve a team's five at ``moment`` without guessing boundaries.

    The CBBD substitution model has no sequence number for two events sharing
    a clock.  Those moments are therefore explicitly ambiguous.  A consumer
    may report them, but must not count them as a match or mismatch.
    """

    rows = list(stints)
    if not rows:
        return LineupReference(NO_REFERENCE)
    at = moment.elapsed
    if _boundary_at(rows, at):
        return LineupReference(AMBIGUOUS_BOUNDARY, boundary=True)

    active = [stint.player_id for stint in rows if stint.contains(moment)]
    if len(active) != len(set(active)) or len(active) != 5:
        return LineupReference(INVALID_REFERENCE)
    player_ids = frozenset(active)
    if known_player_ids is not None and not player_ids.issubset(known_player_ids):
        return LineupReference(UNRESOLVED_REFERENCE, player_ids)
    return LineupReference(MATCHABLE, player_ids)


@dataclass(frozen=True)
class TemporalComparison:
    """Comparison of one event's derived five against the temporal reference."""

    status: str
    derived: ResolvedLineup
    reference: LineupReference

    @property
    def is_match(self) -> bool:
        return self.status == MATCH

    @property
    def is_mismatch(self) -> bool:
        return self.status == MISMATCH


def compare_on_floor_to_reference(
    raw_payload: Mapping[str, Any],
    side: str,
    known_player_ids: set[int],
    stints: Iterable[SubstitutionStint],
) -> TemporalComparison:
    """Compare one event side to a time-aligned substitution reference."""

    derived = resolve_event_side(raw_payload, side, known_player_ids)
    empty_reference = LineupReference(NO_REFERENCE)
    if not derived.is_valid_five:
        return TemporalComparison(ON_FLOOR_UNRESOLVED, derived, empty_reference)

    moment = GameMoment.from_payload(raw_payload)
    if moment is None:
        return TemporalComparison(MISSING_EVENT_TIME, derived, empty_reference)

    reference = lineups_at_moment(stints, moment, known_player_ids)
    if not reference.is_valid_five:
        return TemporalComparison(reference.status, derived, reference)
    status = MATCH if derived.resolved_player_ids == reference.player_ids else MISMATCH
    return TemporalComparison(status, derived, reference)


@dataclass
class TemporalValidationStat:
    """Counts temporal validation outcomes for one season and side."""

    season: int
    side: str
    events_seen: int = 0
    on_floor_valid: int = 0
    matchable: int = 0
    matches: int = 0
    mismatches: int = 0
    statuses: Counter[str] = field(default_factory=Counter)

    def record(self, comparison: TemporalComparison) -> None:
        self.events_seen += 1
        self.statuses[comparison.status] += 1
        if comparison.derived.is_valid_five:
            self.on_floor_valid += 1
        if comparison.status in {MATCH, MISMATCH}:
            self.matchable += 1
        if comparison.is_match:
            self.matches += 1
        elif comparison.is_mismatch:
            self.mismatches += 1

    @property
    def temporal_match_rate_pct(self) -> float | None:
        return (
            100.0 * self.matches / self.matchable if self.matchable else None
        )

    @property
    def temporal_mismatch_rate_pct(self) -> float | None:
        return (
            100.0 * self.mismatches / self.matchable if self.matchable else None
        )


def wilson_interval(
    successes: int, trials: int, z: float = 1.959963984540054
) -> tuple[float, float] | None:
    """Return a Wilson confidence interval for a proportion in [0, 1]."""

    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("successes/trials must satisfy 0 <= successes <= trials")
    if trials == 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    radius = (
        z
        * math.sqrt((p * (1 - p) / trials) + (z * z / (4 * trials * trials)))
        / denominator
    )
    return max(0.0, centre - radius), min(1.0, centre + radius)


def nearest_boundary_distance(
    stints: Iterable[SubstitutionStint], moment: GameMoment
) -> int | None:
    """Return seconds to the nearest substitution boundary, if any."""

    at = moment.elapsed
    boundaries = [stint.start_elapsed for stint in stints]
    boundaries.extend(
        stint.end_elapsed for stint in stints if stint.end_elapsed is not None
    )
    return min((abs(boundary - at) for boundary in boundaries), default=None)
