from __future__ import annotations

import unittest

import numpy as np

from bracketballer_data.usage_efficiency import (
    UsageObservation,
    fit_usage_slope,
    lineups_for_rotation,
    renormalize_usage,
    score_usage_adjusted_lineups,
)


def panel_observations() -> list[UsageObservation]:
    player_effects = {1: 0.00, 2: 0.03, 3: -0.04, 4: 0.02, 5: -0.02}
    season_effects = {2024: 0.00, 2025: 0.02, 2026: -0.015}
    usage_bases = {1: 12.0, 2: 16.0, 3: 20.0, 4: 24.0, 5: 28.0}
    rows: list[UsageObservation] = []
    for player_id in sorted(player_effects):
        for season in sorted(season_effects):
            usage = (
                usage_bases[player_id]
                + (season - 2024) * 1.5
                + 0.7 * ((player_id * season) % 4)
            )
            target = (
                1.75
                - 0.02 * usage
                + player_effects[player_id]
                + season_effects[season]
                + 0.001 * ((player_id + season) % 3 - 1)
            )
            rows.append(
                UsageObservation(
                    player_id=player_id,
                    season=season,
                    usage=usage,
                    true_shooting_pct=target / 2.0,
                    field_goals_attempted=100 + player_id * 10,
                    free_throws_attempted=20 + season - 2024,
                )
            )
    return rows


class UsageEfficiencyTests(unittest.TestCase):
    def test_fixed_effect_fit_recovers_negative_usage_slope(self):
        fit = fit_usage_slope(panel_observations())
        self.assertAlmostEqual(fit.coefficient, -0.02, delta=0.002)
        self.assertGreaterEqual(fit.standard_error, 0.0)
        self.assertEqual(fit.observations, 15)
        self.assertEqual(fit.players, 5)
        self.assertEqual(fit.seasons, (2024, 2025, 2026))

    def test_fit_rejects_duplicate_or_non_repeated_population(self):
        rows = panel_observations()
        with self.assertRaises(ValueError):
            fit_usage_slope(rows + [rows[0]])
        with self.assertRaises(ValueError):
            fit_usage_slope(rows[:3])

    def test_usage_is_normalized_and_delta_is_capped(self):
        usage = np.asarray([30, 25, 20, 15, 10, 8, 7, 6, 5, 4], dtype=float)
        lineups = np.asarray([[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]])
        weights, normalized, deltas = renormalize_usage(usage, lineups)
        np.testing.assert_allclose(normalized[0], usage[:5])
        np.testing.assert_allclose(deltas[0], np.zeros(5))
        np.testing.assert_allclose(weights.sum(axis=1), np.ones(2))
        self.assertTrue((np.abs(deltas) <= 5.0).all())
        self.assertAlmostEqual(float(deltas[1].max()), 5.0)

    def test_invalid_usage_is_rejected(self):
        with self.assertRaises(ValueError):
            renormalize_usage([10, 0, 20, 20, 30], [[0, 1, 2, 3, 4]])
        with self.assertRaises(ValueError):
            renormalize_usage([10, 20, 30, 40, 50], [[0, 1, 2, 3, 3]])

    def test_two_opponents_produce_different_winning_lineups(self):
        lineups = np.asarray(lineups_for_rotation(range(10)), dtype=int)
        usage = np.asarray([30, 28, 25, 22, 19, 12, 10, 8, 6, 4], dtype=float)
        efficiencies = np.asarray(
            [
                [2.8] * 5 + [1.0] * 5,
                [1.0] * 5 + [2.8] * 5,
            ],
            dtype=float,
        )
        projections = score_usage_adjusted_lineups(
            efficiencies,
            usage,
            lineups,
            coefficient=-0.02,
        )
        winners = np.argmax(projections, axis=1)
        self.assertNotEqual(tuple(lineups[winners[0]]), tuple(lineups[winners[1]]))
        for row in projections:
            ordered = np.sort(row)
            self.assertGreaterEqual(float(ordered[-1] - ordered[-2]), 0.01)
        self.assertEqual(projections.shape, (2, 252))
        self.assertTrue(np.isfinite(projections).all())


if __name__ == "__main__":
    unittest.main()
