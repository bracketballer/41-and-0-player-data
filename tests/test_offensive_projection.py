from __future__ import annotations

import time
import unittest

import numpy as np

from bracketballer_data.offensive_projection import (
    DefensiveUnitEvidence,
    OffensivePlayerEvidence,
    project_offensive_matrix,
)
from bracketballer_data.shot_location_profiles import FIELD_GOAL_ZONES, ShotLocationProfile
from bracketballer_data.matchup_assignment import DefensiveMatchupPlayer


def player(player_id: int, *, attempts: int = 100, usage: float | None = None) -> OffensivePlayerEvidence:
    profiles = {}
    for index, zone in enumerate(FIELD_GOAL_ZONES):
        share = (0.20, 0.20, 0.20, 0.15, 0.25)[index]
        accuracy = (0.58, 0.48, 0.42, 0.36, 0.34)[index]
        zone_attempts = attempts // len(FIELD_GOAL_ZONES)
        profiles[zone] = ShotLocationProfile(
            player_id=player_id,
            season=2026,
            zone=zone,
            attempts=zone_attempts,
            makes=round(zone_attempts * accuracy),
            attempt_share=share,
            posterior_alpha=max(1.0, round(zone_attempts * accuracy)) + 50 * accuracy,
            posterior_beta=max(1.0, zone_attempts - round(zone_attempts * accuracy)) + 50 * (1 - accuracy),
            adjusted_pps=(2.0 if index < 3 else 3.0) * accuracy,
        )
    return OffensivePlayerEvidence(
        player_id=player_id,
        usage=usage if usage is not None else 10.0 + player_id,
        handler_score=80.0 - player_id,
        height=72.0 + (player_id % 5),
        profiles=profiles,
    )


def defense(team_id: int = 20, *, status: str = "available", confidence: float | None = 90.0, tilt: float = 0.0) -> DefensiveUnitEvidence:
    ids = (201, 202, 203, 204, 205)
    return DefensiveUnitEvidence(
        team_id=team_id,
        season=2026,
        player_ids=ids,
        zone_tilts={zone: tilt for zone in FIELD_GOAL_ZONES},
        confidence=confidence,
        evidence_status=status,
        matchup_players=tuple(
            DefensiveMatchupPlayer(
                player_id=player_id,
                defensive_disruptor_score=80.0 - index * 5,
                drop_compatible_big_score=30.0 + index * 5,
                center_role_share=0.2 + index * 0.1,
                height=72.0 + index,
            )
            for index, player_id in enumerate(ids)
        ),
    )


class OffensiveProjectionTests(unittest.TestCase):
    def test_matrix_shape_and_deterministic_intervals(self):
        players = [player(index) for index in range(1, 11)]
        first_lineups, first = project_offensive_matrix(players, [defense()], season=2026, offense_team_id=10)
        second_lineups, second = project_offensive_matrix(players, [defense()], season=2026, offense_team_id=10)

        self.assertEqual(len(first_lineups), 252)
        self.assertEqual(len(first), 252)
        self.assertEqual(first, second)
        self.assertEqual(first_lineups, second_lineups)
        self.assertTrue(all(row.projected_pps is not None for row in first))
        self.assertTrue(all(row.interval_low <= row.projected_pps <= row.interval_high for row in first))

    def test_tilt_changes_projection_and_preserves_one_assignment(self):
        players = [player(index) for index in range(1, 11)]
        neutral = project_offensive_matrix(players, [defense(tilt=0.0)], season=2026, offense_team_id=10)[1]
        tilted_unit = defense(team_id=21, tilt=0.0)
        tilted_unit = DefensiveUnitEvidence(
            team_id=tilted_unit.team_id,
            season=tilted_unit.season,
            player_ids=tilted_unit.player_ids,
            zone_tilts={zone: (1.0 if zone == "rim" else 0.0) for zone in FIELD_GOAL_ZONES},
            confidence=tilted_unit.confidence,
            evidence_status=tilted_unit.evidence_status,
            matchup_players=tilted_unit.matchup_players,
        )
        tilted = project_offensive_matrix(players, [tilted_unit], season=2026, offense_team_id=10)[1]
        self.assertNotEqual(
            [row.projected_pps for row in neutral],
            [row.projected_pps for row in tilted],
        )
        self.assertTrue(all(row.matchup_assignment is not None and len(row.matchup_assignment) == 5 for row in neutral))

    def test_conservative_confidence_marks_thin_players_provisional(self):
        players = [player(index, attempts=0 if index == 1 else 100) for index in range(1, 11)]
        _lineups, rows = project_offensive_matrix(players, [defense()], season=2026, offense_team_id=10)
        containing_thin = [row for row in rows if 1 in row.offensive_player_ids]
        self.assertTrue(containing_thin)
        self.assertTrue(all(row.evidence_status == "provisional" for row in containing_thin))
        self.assertTrue(all((row.confidence or 1) < 45 for row in containing_thin))

    def test_unavailable_defense_keeps_identity_and_nulls_results(self):
        players = [player(index) for index in range(1, 6)]
        _lineups, rows = project_offensive_matrix(
            players,
            [defense(status="unavailable", confidence=None)],
            season=2026,
            offense_team_id=10,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.evidence_status, "unavailable")
        self.assertIsNone(row.projected_pps)
        self.assertIsNone(row.interval_low)
        self.assertIsNone(row.matchup_assignment)

    def test_ten_player_matrix_is_vectorized(self):
        players = [player(index) for index in range(1, 11)]
        started = time.perf_counter()
        _lineups, rows = project_offensive_matrix(players, [defense(index) for index in range(10)], season=2026, offense_team_id=10)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(rows), 2520)
        self.assertTrue(np.isfinite([row.projected_pps for row in rows if row.projected_pps is not None]).all())
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
