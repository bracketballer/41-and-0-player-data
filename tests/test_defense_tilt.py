from __future__ import annotations

import math
import unittest

from bracketballer_data.defense_tilt import (
    CoefficientFit,
    TeamGameZoneCounts,
    build_leave_one_game_out_features,
    fit_global_lambda,
    fit_per_zone_lambda,
    select_model,
)


def observations() -> list[TeamGameZoneCounts]:
    rows: list[TeamGameZoneCounts] = []
    for season in (2024, 2025, 2026):
        for game in range(1, 13):
            game_id = season * 100 + game
            rows.append(TeamGameZoneCounts(season, game_id, 1, 2, (50 + game % 3, 20, 15, 10, 5)))
            rows.append(TeamGameZoneCounts(season, game_id, 2, 1, (20, 35 + game % 2, 20, 15, 10)))
    return rows


class DefenseTiltTests(unittest.TestCase):
    def test_leave_one_game_out_features_have_valid_compositions(self):
        features = build_leave_one_game_out_features(observations())
        self.assertEqual(features.counts.shape[1], 5)
        self.assertEqual(features.baseline.shape, features.tilt.shape)
        self.assertTrue((features.baseline > 0).all())
        self.assertTrue((features.baseline.sum(axis=1) > 0.999999).all())

    def test_global_and_per_zone_fits_are_finite(self):
        features = build_leave_one_game_out_features(observations())
        global_fit = fit_global_lambda(features)
        per_zone_fit = fit_per_zone_lambda(features)
        self.assertEqual(len(global_fit.coefficients), 1)
        self.assertEqual(len(per_zone_fit.coefficients), 5)
        self.assertTrue(all(math.isfinite(value) for value in global_fit.coefficients))
        self.assertTrue(all(math.isfinite(value) for value in per_zone_fit.standard_errors))
        self.assertGreater(global_fit.attempts, 0)

    def test_model_selection_requires_material_consistent_gain(self):
        global_fit = CoefficientFit((0.4,), (0.1,), -10, 3, 100)
        per_zone_fit = CoefficientFit((0.4,) * 5, (0.1,) * 5, -9, 3, 100)
        self.assertEqual(
            select_model(global_fit, per_zone_fit, [1.0, 1.0], [0.98, 0.98]),
            "per_zone",
        )
        self.assertEqual(
            select_model(global_fit, per_zone_fit, [1.0, 1.0], [0.995, 1.01]),
            "global",
        )

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            TeamGameZoneCounts(2024, 1, 1, 2, (1, 2))
        with self.assertRaises(ValueError):
            build_leave_one_game_out_features(observations(), smoothing=0)


if __name__ == "__main__":
    unittest.main()
