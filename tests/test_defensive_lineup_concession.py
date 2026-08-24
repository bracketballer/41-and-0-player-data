from __future__ import annotations

import math
import unittest

from bracketballer_data.defensive_lineup_concession import (
    FIELD_GOAL_ZONES,
    DefensiveLineupEvidence,
    LeagueSeasonBaseline,
    TeamSeasonBaseline,
    build_concession_profiles,
    canonical_lineup_hash,
    select_realistic_units,
)


def zones(value: int) -> dict[str, int]:
    return {zone: value for zone in FIELD_GOAL_ZONES}


def unit(
    *,
    player_ids: tuple[int, ...] = (1, 2, 3, 4, 5),
    total_seconds: float = 100.0,
    possessions: float = 50.0,
    opponent_fga: float = 50.0,
    attempts: dict[str, int] | None = None,
) -> DefensiveLineupEvidence:
    player_ids, lineup_hash = canonical_lineup_hash(player_ids)
    return DefensiveLineupEvidence(
        season=2026,
        team_id=10,
        lineup_hash=lineup_hash,
        player_ids=player_ids,
        total_seconds=total_seconds,
        possessions=possessions,
        opponent_fga=opponent_fga,
        zone_attempts=attempts or zones(0),
    )


class DefensiveLineupConcessionTests(unittest.TestCase):
    def test_canonical_identity_requires_sorted_unique_five(self):
        player_ids, lineup_hash = canonical_lineup_hash((5, 1, 4, 2, 3))
        self.assertEqual(player_ids, (1, 2, 3, 4, 5))
        self.assertEqual(lineup_hash, "1-2-3-4-5")
        with self.assertRaises(ValueError):
            canonical_lineup_hash((1, 2, 3, 4))
        with self.assertRaises(ValueError):
            canonical_lineup_hash((1, 2, 3, 4, 4))

    def test_minutes_threshold_is_inclusive_and_adapts_to_team_total(self):
        selected = select_realistic_units(
            [
                unit(total_seconds=100, player_ids=(1, 2, 3, 4, 5)),
                unit(total_seconds=4_901, player_ids=(6, 7, 8, 9, 10)),
            ]
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0][0].total_seconds, 4_901)
        self.assertAlmostEqual(selected[0][1], 4_901 / 5_001)

    def test_two_stage_shrinkage_and_tilt_are_deterministic(self):
        league = LeagueSeasonBaseline(2026, zones(100))
        team_counts = {zone: 0 for zone in FIELD_GOAL_ZONES}
        team_counts["rim"] = 100
        lineup_counts = {zone: 0 for zone in FIELD_GOAL_ZONES}
        lineup_counts["rim"] = 10
        rows = build_concession_profiles(
            [league],
            [TeamSeasonBaseline(2026, 10, team_counts)],
            [unit(attempts=lineup_counts, opponent_fga=10)],
        )
        rim = next(row for row in rows if row.zone == "rim")
        team_rim = (100 + 50 * 0.2) / 150
        unit_rim = (10 + 50 * team_rim) / 60
        self.assertAlmostEqual(rim.zone_concession_tilt or 0.0, math.log(unit_rim / 0.2))
        self.assertTrue(all(row.evidence_status == "provisional" for row in rows))

    def test_confidence_uses_attribution_coverage(self):
        attempts = zones(10)
        rows = build_concession_profiles(
            [LeagueSeasonBaseline(2026, zones(100))],
            [TeamSeasonBaseline(2026, 10, zones(100))],
            [unit(attempts=attempts, opponent_fga=100)],
        )
        expected = 100 * 50 / 100 * 0.5
        self.assertAlmostEqual(rows[0].confidence or 0.0, expected)
        self.assertTrue(all(row.evidence_status == "provisional" for row in rows))

    def test_unavailable_evidence_is_null_not_zero(self):
        rows = build_concession_profiles(
            [LeagueSeasonBaseline(2026, zones(100))],
            [TeamSeasonBaseline(2026, 10, zones(100))],
            [unit(attempts=zones(0))],
        )
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row.evidence_status == "unavailable" for row in rows))
        self.assertTrue(all(row.zone_concession_tilt is None for row in rows))
        self.assertTrue(all(row.confidence is None for row in rows))

    def test_available_requires_independent_audit_and_confidence_floor(self):
        rows = build_concession_profiles(
            [LeagueSeasonBaseline(2026, zones(100))],
            [TeamSeasonBaseline(2026, 10, zones(100))],
            [unit(attempts=zones(100), opponent_fga=100)],
            independent_audit_passed=True,
        )
        self.assertTrue(all(row.evidence_status == "available" for row in rows))

    def test_invalid_baseline_and_duplicate_unit_are_rejected(self):
        with self.assertRaises(ValueError):
            build_concession_profiles(
                [LeagueSeasonBaseline(2026, zones(0))],
                [TeamSeasonBaseline(2026, 10, zones(1))],
                [unit()],
            )
        duplicate = unit()
        with self.assertRaises(ValueError):
            build_concession_profiles(
                [LeagueSeasonBaseline(2026, zones(1))],
                [TeamSeasonBaseline(2026, 10, zones(1))],
                [duplicate, duplicate],
            )


if __name__ == "__main__":
    unittest.main()
