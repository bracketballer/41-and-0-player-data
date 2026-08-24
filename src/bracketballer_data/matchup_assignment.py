"""Deterministic heuristic assignments for offensive lineup projections.

The assignment is deliberately independent of projected scoring.  A defense
chooses who guards whom, so callers receive one matchup assignment rather
than a collection of candidate assignments to maximize over.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import median
from typing import Iterable


MODEL_VERSION = "heuristic-matchup-v1"
PERIMETER_SCORE_THRESHOLD = 70.0
SIZE_GRACE_INCHES = 3.0


@dataclass(frozen=True, slots=True)
class OffensiveMatchupPlayer:
    """Offensive player fields required by the v1 assignment heuristic."""

    player_id: int
    handler_score: float
    height: float | None = None


@dataclass(frozen=True, slots=True)
class DefensiveMatchupPlayer:
    """Defensive characteristics consumed by the v1 assignment heuristic.

    Scores are the same 0--100 values published in
    ``player_characteristic_scores``.  Missing characteristic scores are
    treated as neutral evidence (50) and surfaced as a limitation; they are
    never represented as zero.
    """

    player_id: int
    defensive_disruptor_score: float | None
    drop_compatible_big_score: float | None
    center_role_share: float | None
    height: float | None = None


@dataclass(frozen=True, slots=True)
class MatchupPair:
    """One offensive-to-defensive pairing in a canonical assignment."""

    offensive_player_id: int
    defensive_player_id: int
    is_primary_handler: bool
    is_best_perimeter_defender: bool
    height_difference_inches: float | None
    size_penalty: float | None
    limitations: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-compatible shape stored in V36."""

        return {
            "offensive_player_id": self.offensive_player_id,
            "defensive_player_id": self.defensive_player_id,
            "is_primary_handler": self.is_primary_handler,
            "is_best_perimeter_defender": self.is_best_perimeter_defender,
            "height_difference_inches": self.height_difference_inches,
            "size_penalty": self.size_penalty,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class MatchupAssignment:
    """One complete, deterministic assignment for two five-player lineups."""

    pairs: tuple[MatchupPair, ...]
    primary_handler_id: int
    perimeter_defender_id: int
    perimeter_score: float
    perimeter_threshold_met: bool
    total_size_penalty: float | None
    limitations: tuple[str, ...] = ()

    def as_json(self) -> list[dict[str, object]]:
        """Return the five-element array expected by ``matchup_assignment``."""

        return [pair.as_dict() for pair in self.pairs]


def _validate_player_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("player_id must be a positive integer")


def _validate_score(value: float, *, name: str) -> None:
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
        raise ValueError(f"{name} must be finite and in [0, 100]")


def _validate_height(value: float | None, *, name: str) -> None:
    if value is None:
        return
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{name} must be finite and positive when supplied")


def _validate_lineup(
    players: tuple[object, ...], *, expected_type: type, side: str
) -> None:
    if len(players) != 5:
        raise ValueError(f"{side} lineup must contain exactly five players")
    ids: list[int] = []
    for player in players:
        if not isinstance(player, expected_type):
            raise TypeError(f"{side} lineup contains an unexpected player type")
        _validate_player_id(player.player_id)
        ids.append(player.player_id)
    if len(set(ids)) != 5:
        raise ValueError(f"{side} lineup player IDs must be unique")


def _matching_height(
    height: float | None,
    *,
    all_known_heights: tuple[float, ...],
) -> float:
    """Use a transparent neutral imputation only for ordering/costing."""

    if height is not None:
        return float(height)
    return float(median(all_known_heights)) if all_known_heights else 0.0


def _height_details(
    offensive_height: float | None,
    defensive_height: float | None,
    *,
    all_known_heights: tuple[float, ...],
) -> tuple[float | None, float | None, float]:
    if offensive_height is None or defensive_height is None:
        return None, None, abs(
            _matching_height(
                offensive_height, all_known_heights=all_known_heights
            )
            - _matching_height(
                defensive_height, all_known_heights=all_known_heights
            )
        )
    difference = abs(float(offensive_height) - float(defensive_height))
    return difference, max(0.0, difference - SIZE_GRACE_INCHES), difference


def _perimeter_components(
    player: DefensiveMatchupPlayer,
) -> tuple[float, float | None, tuple[str, ...]]:
    limitations: list[str] = []
    disruptor = player.defensive_disruptor_score
    drop_big = player.drop_compatible_big_score
    if disruptor is None:
        disruptor = 50.0
        limitations.append("MISSING_DEFENSIVE_DISRUPTOR")
    if drop_big is None:
        drop_big = 50.0
        limitations.append("MISSING_DROP_COMPATIBLE_BIG")
    score = 0.75 * float(disruptor) + 0.25 * (100.0 - float(drop_big))
    center_role = player.center_role_share
    if center_role is None:
        limitations.append("MISSING_CENTER_ROLE_SHARE")
    return score, center_role, tuple(limitations)


def assign_matchups(
    offensive_players: Iterable[OffensiveMatchupPlayer],
    defensive_players: Iterable[DefensiveMatchupPlayer],
) -> MatchupAssignment:
    """Build one deterministic five-player matchup assignment.

    The highest perimeter score is fixed onto the highest handler score.  The
    other four pairs minimize soft excess height mismatch; no assignment is
    rejected solely because of size.
    """

    offense = tuple(offensive_players)
    defense = tuple(defensive_players)
    _validate_lineup(
        offense,
        expected_type=OffensiveMatchupPlayer,
        side="offensive",
    )
    _validate_lineup(
        defense,
        expected_type=DefensiveMatchupPlayer,
        side="defensive",
    )

    for player in offense:
        _validate_score(player.handler_score, name="handler_score")
        _validate_height(player.height, name="offensive height")
    for player in defense:
        if player.defensive_disruptor_score is not None:
            _validate_score(
                player.defensive_disruptor_score,
                name="defensive_disruptor_score",
            )
        if player.drop_compatible_big_score is not None:
            _validate_score(
                player.drop_compatible_big_score,
                name="drop_compatible_big_score",
            )
        if player.center_role_share is not None and (
            not math.isfinite(float(player.center_role_share))
            or not 0 <= float(player.center_role_share) <= 1
        ):
            raise ValueError("center_role_share must be finite and in [0, 1]")
        _validate_height(player.height, name="defensive height")

    handler = min(offense, key=lambda row: (-row.handler_score, row.player_id))
    known_heights = tuple(
        float(height)
        for height in (
            *[row.height for row in offense],
            *[row.height for row in defense],
        )
        if height is not None
    )

    perimeter_rows: list[
        tuple[DefensiveMatchupPlayer, float, float, tuple[str, ...]]
    ] = []
    for row in defense:
        score, center_role, limitations = _perimeter_components(row)
        _, _, matching_difference = _height_details(
            handler.height,
            row.height,
            all_known_heights=known_heights,
        )
        # Missing center-role evidence is sorted after known evidence while
        # retaining deterministic score and ID ordering.
        perimeter_rows.append((row, score, matching_difference, limitations))
    perimeter_rows.sort(
        key=lambda item: (
            -item[1],
            0 if item[0].center_role_share is not None else 1,
            item[0].center_role_share
            if item[0].center_role_share is not None
            else 2.0,
            item[2],
            item[0].player_id,
        )
    )
    perimeter, perimeter_score, _, perimeter_limitations = perimeter_rows[0]
    threshold_met = perimeter_score >= PERIMETER_SCORE_THRESHOLD
    outer_limitations = list(perimeter_limitations)
    if not threshold_met:
        outer_limitations.append("PERIMETER_THRESHOLD_FALLBACK")

    remaining_offense = tuple(
        sorted(
            (row for row in offense if row.player_id != handler.player_id),
            key=lambda row: row.player_id,
        )
    )
    remaining_defense = tuple(
        sorted(
            (row for row in defense if row.player_id != perimeter.player_id),
            key=lambda row: row.player_id,
        )
    )

    best_rest: tuple[
        float,
        tuple[int, ...],
        tuple[tuple[OffensiveMatchupPlayer, DefensiveMatchupPlayer], ...],
    ] | None = None
    for permutation in itertools.permutations(remaining_defense):
        total_cost = 0.0
        pairs: list[tuple[OffensiveMatchupPlayer, DefensiveMatchupPlayer]] = []
        for offensive, defensive in zip(remaining_offense, permutation):
            _, penalty, _ = _height_details(
                offensive.height,
                defensive.height,
                all_known_heights=known_heights,
            )
            if (
                penalty is not None
                and offensive.height is not None
                and defensive.height is not None
            ):
                total_cost += penalty
            pairs.append((offensive, defensive))
        tie_key = tuple(defensive.player_id for _, defensive in pairs)
        candidate = (total_cost, tie_key, tuple(pairs))
        if best_rest is None or candidate[:2] < best_rest[:2]:
            best_rest = candidate
    assert best_rest is not None

    raw_pairs: list[
        tuple[
            OffensiveMatchupPlayer,
            DefensiveMatchupPlayer,
            bool,
            bool,
            tuple[str, ...],
        ]
    ] = [
        (handler, perimeter, True, True, perimeter_limitations),
    ]
    raw_pairs.extend(
        (offensive, defensive, False, False, ())
        for offensive, defensive in best_rest[2]
    )

    pairs: list[MatchupPair] = []
    for offensive, defensive, is_handler, is_perimeter, limitations in raw_pairs:
        difference, penalty, _ = _height_details(
            offensive.height,
            defensive.height,
            all_known_heights=known_heights,
        )
        pair_limitations = list(limitations)
        if offensive.height is None or defensive.height is None:
            pair_limitations.append("MISSING_HEIGHT")
        if is_perimeter and not threshold_met:
            pair_limitations.append("PERIMETER_THRESHOLD_FALLBACK")
        pairs.append(
            MatchupPair(
                offensive_player_id=offensive.player_id,
                defensive_player_id=defensive.player_id,
                is_primary_handler=is_handler,
                is_best_perimeter_defender=is_perimeter,
                height_difference_inches=difference,
                size_penalty=penalty,
                limitations=tuple(dict.fromkeys(pair_limitations)),
            )
        )
    pairs.sort(key=lambda pair: pair.offensive_player_id)

    all_heights_known = all(
        offensive.height is not None and defensive.height is not None
        for offensive, defensive, _, _, _ in raw_pairs
    )
    total_size_penalty = (
        sum(pair.size_penalty or 0.0 for pair in pairs)
        if all_heights_known
        else None
    )
    if not all_heights_known:
        outer_limitations.append("MISSING_HEIGHT")

    return MatchupAssignment(
        pairs=tuple(pairs),
        primary_handler_id=handler.player_id,
        perimeter_defender_id=perimeter.player_id,
        perimeter_score=round(perimeter_score, 6),
        perimeter_threshold_met=threshold_met,
        total_size_penalty=(
            round(total_size_penalty, 6)
            if total_size_penalty is not None
            else None
        ),
        limitations=tuple(dict.fromkeys(outer_limitations)),
    )


__all__ = [
    "MODEL_VERSION",
    "PERIMETER_SCORE_THRESHOLD",
    "SIZE_GRACE_INCHES",
    "DefensiveMatchupPlayer",
    "MatchupAssignment",
    "MatchupPair",
    "OffensiveMatchupPlayer",
    "assign_matchups",
]
