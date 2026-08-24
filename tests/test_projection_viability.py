from __future__ import annotations

import unittest

import numpy as np

from bracketballer_data.projection_viability import (
    FIELD_GOAL_ZONES,
    bootstrap_gate,
    classify_gate,
    lineups_for_rotation,
    median_zone_attempts,
    projected_lineups,
    smoothed_share,
)


class ProjectionViabilityTests(unittest.TestCase):
    def test_rotation_produces_deterministic_252_lineups(self):
        lineups = lineups_for_rotation(range(10))
        self.assertEqual(len(lineups), 252)
        self.assertEqual(lineups, lineups_for_rotation(reversed(range(10))))
        self.assertEqual(len(set(lineups)), 252)

    def test_medians_retain_zero_attempt_zones(self):
        medians = median_zone_attempts(
            [
                (2026, 1, (10, 0, 4, 2, 1)),
                (2026, 2, (2, 3, 0, 0, 0)),
            ]
        )
        self.assertEqual(medians[2026], (6.0, 1.5, 2.0, 1.0, 0.5))

    def test_duplicate_player_season_is_rejected(self):
        with self.assertRaises(ValueError):
            median_zone_attempts(
                [(2026, 1, (1,) * len(FIELD_GOAL_ZONES))] * 2
            )

    def test_smoothing_prevents_zero_composition(self):
        share = smoothed_share((0, 0, 0, 0, 0), (1, 1, 1, 1, 1))
        np.testing.assert_allclose(share, np.full(5, 0.2))

    def test_projection_is_finite_and_has_a_spread(self):
        attempts = np.asarray(
            [
                (40, 20, 10, 8, 2),
                (20, 30, 10, 5, 5),
                (10, 10, 30, 4, 6),
                (5, 5, 5, 30, 20),
                (5, 5, 5, 20, 30),
            ],
            dtype=float,
        )
        makes = attempts * 0.5
        defense = np.asarray([(20, 20, 20, 20, 20)], dtype=float)
        lineups = np.asarray([[0, 1, 2, 3, 4]], dtype=int)
        projections, spreads = projected_lineups(
            attempts,
            makes,
            defense,
            lineups,
            np.ones(5),
            league_share=np.ones(5),
            league_accuracy=np.full(5, 0.5),
        )
        self.assertEqual(projections.shape, (1, 1))
        self.assertEqual(spreads.shape, (1,))
        self.assertTrue(np.isfinite(projections).all())
        self.assertGreaterEqual(spreads[0], 0)

    def test_gate_boundaries_are_explicit(self):
        self.assertEqual(classify_gate(0.11, 0.20, 0.10), "GO")
        self.assertEqual(classify_gate(0.01, 0.10, 0.10), "NO-GO")
        self.assertEqual(
            classify_gate(0.05, 0.15, 0.10), "SHIP AS EXPLORATION TOOL"
        )

    def test_bootstrap_is_deterministic_for_fixed_seed(self):
        game_attempts = np.asarray((10, 8, 6, 4, 2), dtype=float)
        game_makes = np.asarray((5, 4, 3, 2, 1), dtype=float)
        player_rows = [
            [(game_attempts, game_makes), (game_attempts, game_makes)]
            for _ in range(5)
        ]
        defense_rows = [[(game_attempts, np.zeros(5))] for _ in range(2)]
        kwargs = dict(
            lineup_indices=np.asarray([[0, 1, 2, 3, 4]], dtype=int),
            usage_rates=np.ones(5),
            league_share=np.ones(5),
            league_accuracy=np.full(5, 0.5),
            replicates=100,
            seed=42,
        )
        first = bootstrap_gate(player_rows, defense_rows, **kwargs)
        second = bootstrap_gate(player_rows, defense_rows, **kwargs)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
