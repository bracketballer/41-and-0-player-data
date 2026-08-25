from __future__ import annotations

import unittest

from bracketballer_data.offensive_projection_backtest import (
    BacktestCell,
    BacktestProtocol,
    evaluate_gate,
    interval_diagnostics,
    paired_game_bootstrap,
    weighted_mae,
)


def cells(*, matchup_error: float = 0.1, baseline_error: float = 0.3, games: int = 30):
    rows = []
    for index in range(games):
        season = 2024 + index // 10
        game_id = index + 1
        rows.append(
            BacktestCell(
                season=season,
                game_id=game_id,
                offense_team_id=100,
                defense_team_id=200,
                offensive_lineup_hash="1-2-3-4-5",
                defensive_lineup_hash="6-7-8-9-10",
                fga=20,
                observed_pps=1.5,
                matchup_pps=1.5 - matchup_error,
                baseline_pps=1.5 + baseline_error,
                interval_low=1.0,
                interval_high=1.8,
                evidence_status="provisional",
            )
        )
    return rows


class OffensiveProjectionBacktestTests(unittest.TestCase):
    def test_weighted_mae_uses_fga_weights(self):
        rows = cells(games=30)
        self.assertAlmostEqual(weighted_mae(rows, matchup=True), 0.1)
        self.assertAlmostEqual(weighted_mae(rows, matchup=False), 0.3)

    def test_bootstrap_is_deterministic_and_paired(self):
        rows = cells()
        first = paired_game_bootstrap(rows, replicates=200, seed=123)
        second = paired_game_bootstrap(rows, replicates=200, seed=123)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first.relative_improvement, 2 / 3)
        self.assertGreater(first.lower_95, 0)

    def test_gate_passes_only_after_margin_and_confidence_checks(self):
        decision = evaluate_gate(cells())
        self.assertEqual(decision.status, "PASS")
        self.assertGreaterEqual(decision.bootstrap.relative_improvement, 0.05)

    def test_gate_fails_when_improvement_is_below_preregistered_margin(self):
        decision = evaluate_gate(cells(matchup_error=0.29, baseline_error=0.3))
        self.assertEqual(decision.status, "FAIL")
        self.assertIn("does not clear", decision.reason)

    def test_gate_fails_on_insufficient_holdout_before_bootstrap(self):
        protocol = BacktestProtocol(minimum_holdout_games=3, minimum_holdout_fga=100)
        decision = evaluate_gate(cells(games=2), protocol=protocol)
        self.assertEqual(decision.status, "FAIL")
        self.assertIsNone(decision.bootstrap)

    def test_interval_diagnostics_are_weighted_and_non_gating(self):
        rows = cells()
        count_coverage, fga_coverage, width = interval_diagnostics(rows)
        self.assertEqual(count_coverage, 1.0)
        self.assertEqual(fga_coverage, 1.0)
        self.assertAlmostEqual(width, 0.8)

    def test_invalid_interval_and_duplicate_cells_are_rejected(self):
        with self.assertRaises(ValueError):
            BacktestCell(
                season=2024,
                game_id=1,
                offense_team_id=1,
                defense_team_id=2,
                offensive_lineup_hash="1-2-3-4-5",
                defensive_lineup_hash="6-7-8-9-10",
                fga=5,
                observed_pps=1.0,
                matchup_pps=1.0,
                baseline_pps=1.0,
                interval_low=1.1,
                interval_high=1.2,
                evidence_status="provisional",
            )
        duplicate = cells(games=30)
        duplicate.append(duplicate[0])
        with self.assertRaises(ValueError):
            weighted_mae(duplicate, matchup=True)


if __name__ == "__main__":
    unittest.main()
