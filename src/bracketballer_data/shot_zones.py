"""Coordinate normalization and v1 NCAA shot-zone classification.

CBBD stores locations on a full-court grid whose observed dimensions are
``0..940`` by ``0..500``.  The values are tenths of a foot, so this module
converts them to feet and reflects every shot into a canonical half-court
frame before applying the zone polygons.

The direction helper intentionally works at the game/team/period level.  A
single shot can be a backcourt heave, and choosing the nearest basket for that
shot would put it in the wrong half.  Instead, all located shots vote on the
game orientation; the inferred attacking basket is then applied to every shot
in that team-period.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Hashable, Iterable, Literal, Mapping


AttackingBasket = Literal["left", "right"]
ShotZone = Literal[
    "rim",
    "short_mid",
    "long_mid",
    "corner_three",
    "above_break_three",
]

COURT_LENGTH_TENTHS = 940.0
COURT_WIDTH_TENTHS = 500.0
COURT_MIDPOINT_TENTHS = COURT_LENGTH_TENTHS / 2.0
COURT_LENGTH_FEET = COURT_LENGTH_TENTHS / 10.0
COURT_WIDTH_FEET = COURT_WIDTH_TENTHS / 10.0

# NCAA men's court dimensions, measured from the baseline and the sideline.
HOOP_X_FEET = 5.25  # 63 inches from the end line
HOOP_Y_FEET = 25.0
RIM_RADIUS_FEET = 4.0
SHORT_MID_RADIUS_FEET = 14.0
THREE_POINT_ARC_RADIUS_FEET = 22.0 + 1.75 / 12.0
CORNER_THREE_LATERAL_FEET = 21.0 + 7.875 / 12.0
CORNER_THREE_TRANSITION_X_FEET = HOOP_X_FEET + math.sqrt(
    THREE_POINT_ARC_RADIUS_FEET**2 - CORNER_THREE_LATERAL_FEET**2
)

FIELD_GOAL_ZONES: tuple[ShotZone, ...] = (
    "rim",
    "short_mid",
    "long_mid",
    "corner_three",
    "above_break_three",
)


@dataclass(frozen=True, slots=True)
class ShotCoordinate:
    """The fields needed to infer a game's attacking direction."""

    event_id: Hashable
    game_id: Hashable
    team_id: Hashable | None
    opponent_id: Hashable | None
    period: int | None
    location_x: float | None


@dataclass(frozen=True, slots=True)
class DirectionInference:
    """Direction assignments and diagnostics for a collection of shots."""

    baskets: Mapping[Hashable, AttackingBasket | None]
    votes: Mapping[Hashable, AttackingBasket | None]
    game_orientations: Mapping[Hashable, AttackingBasket | None]
    located_events: int
    disagreements: int
    unresolved_games: int

    @property
    def unresolved_events(self) -> int:
        return sum(
            1
            for event_id, vote in self.votes.items()
            if vote is not None and self.baskets.get(event_id) is None
        )


def _as_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_coordinates(
    location_x: float | None,
    location_y: float | None,
    attacking_basket: AttackingBasket | None,
) -> tuple[float, float] | None:
    """Return canonical feet coordinates, with the attacked basket on the left.

    ``None`` is returned for missing/non-finite/out-of-court coordinates or an
    unresolved attacking basket.  The function never clamps bad source data.
    """

    if attacking_basket not in ("left", "right", None):
        raise ValueError(f"unsupported attacking basket: {attacking_basket!r}")
    if attacking_basket is None:
        return None

    x = _as_finite_float(location_x)
    y = _as_finite_float(location_y)
    if x is None or y is None:
        return None
    if not (0.0 <= x <= COURT_LENGTH_TENTHS and 0.0 <= y <= COURT_WIDTH_TENTHS):
        return None

    x /= 10.0
    y /= 10.0
    if attacking_basket == "right":
        x = COURT_LENGTH_FEET - x
    return x, y


def _is_three_point(x: float, y: float) -> bool:
    """Classify the NCAA three-point polygon in canonical feet coordinates."""

    lateral = abs(y - HOOP_Y_FEET)
    if (
        x <= CORNER_THREE_TRANSITION_X_FEET
        and lateral >= CORNER_THREE_LATERAL_FEET
    ):
        return True
    return math.hypot(x - HOOP_X_FEET, lateral) >= THREE_POINT_ARC_RADIUS_FEET


def classify_shot(
    location_x: float | None,
    location_y: float | None,
    attacking_basket: AttackingBasket | None,
) -> ShotZone | None:
    """Classify one field-goal location into the five v1 zones.

    Coordinates use CBBD's full-court tenths-of-a-foot units.  Three-point
    geometry is evaluated before the radial two-point buckets so corner and
    above-break attempts always remain three-point attempts.
    """

    coordinates = normalize_coordinates(location_x, location_y, attacking_basket)
    if coordinates is None:
        return None
    x, y = coordinates

    if _is_three_point(x, y):
        if (
            x <= CORNER_THREE_TRANSITION_X_FEET
            and abs(y - HOOP_Y_FEET) >= CORNER_THREE_LATERAL_FEET
        ):
            return "corner_three"
        return "above_break_three"

    distance = math.hypot(x - HOOP_X_FEET, y - HOOP_Y_FEET)
    if distance <= RIM_RADIUS_FEET:
        return "rim"
    if distance <= SHORT_MID_RADIUS_FEET:
        return "short_mid"
    return "long_mid"


def _opposite(side: AttackingBasket) -> AttackingBasket:
    return "right" if side == "left" else "left"


def _vote_for_x(location_x: float | None) -> AttackingBasket | None:
    value = _as_finite_float(location_x)
    if value is None or not (0.0 <= value <= COURT_LENGTH_TENTHS):
        return None
    if value == COURT_MIDPOINT_TENTHS:
        return None
    return "left" if value < COURT_MIDPOINT_TENTHS else "right"


def _expected_side(
    base_sides: Mapping[Hashable, AttackingBasket],
    team_id: Hashable | None,
    period: int | None,
) -> AttackingBasket | None:
    if team_id not in base_sides or period is None or period < 1:
        return None
    side = base_sides[team_id]
    # NCAA teams switch ends at halftime; overtime uses the second-half ends.
    return side if period == 1 else _opposite(side)


def infer_attacking_baskets(
    observations: Iterable[ShotCoordinate],
) -> DirectionInference:
    """Infer attacked baskets from game orientation and period invariants.

    For each game, the first observed team is assigned one candidate basket
    in period one and the other team receives the opposite basket.  Both
    possible assignments are scored against the coordinate votes.  A tied
    score leaves the game's direction unresolved instead of guessing.
    """

    rows = list(observations)
    votes = {row.event_id: _vote_for_x(row.location_x) for row in rows}
    by_game: dict[Hashable, list[ShotCoordinate]] = {}
    for row in rows:
        by_game.setdefault(row.game_id, []).append(row)

    baskets: dict[Hashable, AttackingBasket | None] = {
        row.event_id: None for row in rows
    }
    orientations: dict[Hashable, AttackingBasket | None] = {}
    disagreements = 0
    located_events = sum(vote is not None for vote in votes.values())
    unresolved_games = 0

    for game_id, game_rows in by_game.items():
        team_ids = sorted(
            {row.team_id for row in game_rows if row.team_id is not None},
            key=str,
        )
        if not team_ids or len(team_ids) > 2:
            orientations[game_id] = None
            unresolved_games += 1
            continue

        candidate_scores: dict[AttackingBasket, int] = {}
        for first_team_side in ("left", "right"):
            base_sides: dict[Hashable, AttackingBasket] = {
                team_ids[0]: first_team_side,
            }
            if len(team_ids) == 2:
                base_sides[team_ids[1]] = _opposite(first_team_side)
            score = 0
            for row in game_rows:
                expected = _expected_side(base_sides, row.team_id, row.period)
                vote = votes[row.event_id]
                if expected is not None and vote is not None and expected == vote:
                    score += 1
            candidate_scores[first_team_side] = score

        if candidate_scores["left"] == candidate_scores["right"]:
            orientations[game_id] = None
            unresolved_games += 1
            continue

        orientation = max(candidate_scores, key=candidate_scores.get)
        orientations[game_id] = orientation
        base_sides = {team_ids[0]: orientation}
        if len(team_ids) == 2:
            base_sides[team_ids[1]] = _opposite(orientation)

        for row in game_rows:
            expected = _expected_side(base_sides, row.team_id, row.period)
            baskets[row.event_id] = expected
            vote = votes[row.event_id]
            if expected is not None and vote is not None and expected != vote:
                disagreements += 1

    return DirectionInference(
        baskets=baskets,
        votes=votes,
        game_orientations=orientations,
        located_events=located_events,
        disagreements=disagreements,
        unresolved_games=unresolved_games,
    )


__all__ = [
    "AttackingBasket",
    "DirectionInference",
    "FIELD_GOAL_ZONES",
    "ShotCoordinate",
    "ShotZone",
    "classify_shot",
    "infer_attacking_baskets",
    "normalize_coordinates",
]
