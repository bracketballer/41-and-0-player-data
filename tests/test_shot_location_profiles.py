import unittest

from bracketballer_data.shot_location_profiles import (
    FIELD_GOAL_ZONES,
    PlayerShotLocationInput,
    SeasonShotLocationBaseline,
    ZoneCount,
    build_shot_location_profiles,
)


def baseline(season=2026):
    return SeasonShotLocationBaseline(
        season=season,
        zones={zone: ZoneCount(100, 50) for zone in FIELD_GOAL_ZONES},
    )


def player(player_id=1, season=2026, zones=None):
    return PlayerShotLocationInput(
        player_id=player_id,
        season=season,
        zones=zones or {},
    )


class ShotLocationProfileTests(unittest.TestCase):
    def test_zero_attempt_player_gets_league_prior_for_all_five_zones(self):
        profiles = build_shot_location_profiles([player()], [baseline()])

        self.assertEqual(len(profiles), 5)
        self.assertEqual({row.zone for row in profiles}, set(FIELD_GOAL_ZONES))
        self.assertAlmostEqual(sum(row.attempt_share for row in profiles), 1.0)
        for row in profiles:
            self.assertEqual(row.attempts, 0)
            self.assertEqual(row.makes, 0)
            expected_pps = 1.0 if row.zone in {"rim", "short_mid", "long_mid"} else 1.5
            self.assertAlmostEqual(row.adjusted_pps, expected_pps)

    def test_share_smoothing_is_positive_and_normalized(self):
        profiles = build_shot_location_profiles(
            [
                player(
                    zones={
                        "rim": ZoneCount(10, 5),
                    }
                )
            ],
            [baseline()],
        )
        shares = {row.zone: row.attempt_share for row in profiles}
        self.assertAlmostEqual(sum(shares.values()), 1.0)
        self.assertTrue(all(value > 0 for value in shares.values()))
        self.assertAlmostEqual(shares["rim"], 10.1 / 10.5)

    def test_accuracy_uses_the_established_fifty_attempt_prior(self):
        profiles = build_shot_location_profiles(
            [
                player(
                    zones={
                        "rim": ZoneCount(10, 10),
                    }
                )
            ],
            [baseline()],
        )
        rim = next(row for row in profiles if row.zone == "rim")
        self.assertAlmostEqual(rim.posterior_alpha, 35.0)
        self.assertAlmostEqual(rim.posterior_beta, 25.0)
        self.assertAlmostEqual(rim.adjusted_pps, 2.0 * 35.0 / 60.0)

    def test_seasons_use_independent_baselines(self):
        second_baseline = SeasonShotLocationBaseline(
            season=2025,
            zones={zone: ZoneCount(100, 25) for zone in FIELD_GOAL_ZONES},
        )
        profiles = build_shot_location_profiles(
            [player(1, 2025), player(2, 2026)],
            [second_baseline, baseline()],
        )
        by_key = {
            (row.player_id, row.zone): row
            for row in profiles
        }
        self.assertAlmostEqual(by_key[(1, "rim")].adjusted_pps, 2.0 * 0.25)
        self.assertAlmostEqual(by_key[(2, "rim")].adjusted_pps, 2.0 * 0.5)

    def test_duplicate_players_and_invalid_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            build_shot_location_profiles([player(), player()], [baseline()])
        with self.assertRaises(ValueError):
            build_shot_location_profiles(
                [player(zones={"rim": ZoneCount(1, 2)})], [baseline()]
            )

    def test_missing_or_empty_baseline_is_rejected(self):
        with self.assertRaises(ValueError):
            build_shot_location_profiles([player()], [])
        with self.assertRaises(ValueError):
            build_shot_location_profiles(
                [player()],
                [
                    SeasonShotLocationBaseline(
                        season=2026,
                        zones={zone: ZoneCount() for zone in FIELD_GOAL_ZONES},
                    )
                ],
            )


if __name__ == "__main__":
    unittest.main()
