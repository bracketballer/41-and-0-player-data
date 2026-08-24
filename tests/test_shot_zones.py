import unittest

from bracketballer_data.shot_zones import (
    CORNER_THREE_LATERAL_FEET,
    CORNER_THREE_TRANSITION_X_FEET,
    HOOP_X_FEET,
    HOOP_Y_FEET,
    SHORT_MID_RADIUS_FEET,
    THREE_POINT_ARC_RADIUS_FEET,
    ShotCoordinate,
    classify_shot,
    infer_attacking_baskets,
    normalize_coordinates,
)
from scripts.analysis.audit_shot_zones import AuditEvent, summarize_events


def source_coordinate(x_feet: float, y_feet: float = HOOP_Y_FEET) -> tuple[float, float]:
    return x_feet * 10.0, y_feet * 10.0


class ShotZoneClassifierTests(unittest.TestCase):
    def test_all_five_zones_round_trip(self):
        self.assertEqual(classify_shot(*source_coordinate(HOOP_X_FEET), "left"), "rim")
        self.assertEqual(
            classify_shot(*source_coordinate(HOOP_X_FEET + 8), "left"), "short_mid"
        )
        self.assertEqual(
            classify_shot(*source_coordinate(HOOP_X_FEET + 18), "left"), "long_mid"
        )
        self.assertEqual(
            classify_shot(
                CORNER_THREE_TRANSITION_X_FEET * 10,
                (HOOP_Y_FEET + CORNER_THREE_LATERAL_FEET) * 10,
                "left",
            ),
            "corner_three",
        )
        self.assertEqual(
            classify_shot(
                (HOOP_X_FEET + THREE_POINT_ARC_RADIUS_FEET) * 10,
                HOOP_Y_FEET * 10,
                "left",
            ),
            "above_break_three",
        )

    def test_existing_fixture_coordinates_have_expected_zones(self):
        # onfloor_regular_2026.json: x=846, y=245, attacking the right basket.
        self.assertEqual(classify_shot(846, 245, "right"), "short_mid")
        # onfloor_pre2024.json: x=216.2, y=445, attacking the left basket.
        self.assertEqual(classify_shot(216.2, 445, "left"), "above_break_three")

    def test_rim_and_short_mid_boundaries_are_inclusive(self):
        rim_edge = source_coordinate(HOOP_X_FEET + 4)
        short_edge = source_coordinate(HOOP_X_FEET + SHORT_MID_RADIUS_FEET)
        self.assertEqual(classify_shot(*rim_edge, "left"), "rim")
        self.assertEqual(
            classify_shot(rim_edge[0] + 0.01, rim_edge[1], "left"), "short_mid"
        )
        self.assertEqual(classify_shot(*short_edge, "left"), "short_mid")
        self.assertEqual(
            classify_shot(short_edge[0] + 0.01, short_edge[1], "left"), "long_mid"
        )

    def test_corner_and_arc_boundaries_are_three_point_zones(self):
        self.assertEqual(
            classify_shot(
                CORNER_THREE_TRANSITION_X_FEET * 10,
                (HOOP_Y_FEET + CORNER_THREE_LATERAL_FEET) * 10,
                "left",
            ),
            "corner_three",
        )
        self.assertEqual(
            classify_shot(
                (HOOP_X_FEET + THREE_POINT_ARC_RADIUS_FEET) * 10,
                HOOP_Y_FEET * 10,
                "left",
            ),
            "above_break_three",
        )

    def test_reflection_and_lateral_symmetry(self):
        left = classify_shot(94, 245, "left")
        right = classify_shot(846, 245, "right")
        mirrored = classify_shot(94, 255, "left")
        self.assertEqual(left, right)
        self.assertEqual(left, mirrored)

    def test_bad_or_unresolved_coordinates_are_not_clamped(self):
        self.assertIsNone(normalize_coordinates(None, 250, "left"))
        self.assertIsNone(normalize_coordinates(100, 250, None))
        self.assertIsNone(normalize_coordinates(-1, 250, "left"))
        self.assertIsNone(normalize_coordinates(100, 501, "left"))
        self.assertIsNone(classify_shot(100, 250, None))
        with self.assertRaises(ValueError):
            classify_shot(100, 250, "baseline")  # type: ignore[arg-type]


class DirectionInferenceTests(unittest.TestCase):
    def test_game_orientation_switches_at_halftime_and_persists_in_overtime(self):
        rows = [
            ShotCoordinate(1, 10, 100, 200, 1, 100),
            ShotCoordinate(2, 10, 200, 100, 1, 800),
            ShotCoordinate(3, 10, 100, 200, 2, 800),
            ShotCoordinate(4, 10, 200, 100, 2, 100),
            ShotCoordinate(5, 10, 100, 200, 3, 800),
            ShotCoordinate(6, 10, 100, 200, 1, 800),  # backcourt heave
        ]
        result = infer_attacking_baskets(rows)
        self.assertEqual(result.game_orientations[10], "left")
        self.assertEqual(result.baskets[1], "left")
        self.assertEqual(result.baskets[3], "right")
        self.assertEqual(result.baskets[5], "right")
        self.assertEqual(result.baskets[6], "left")
        self.assertEqual(result.disagreements, 1)

    def test_tied_game_orientation_is_unresolved(self):
        result = infer_attacking_baskets(
            [
                ShotCoordinate(1, 10, 100, 200, 1, 100),
                ShotCoordinate(2, 10, 100, 200, 1, 800),
            ]
        )
        self.assertIsNone(result.game_orientations[10])
        self.assertIsNone(result.baskets[1])
        self.assertEqual(result.unresolved_games, 1)

    def test_missing_team_or_period_does_not_get_guessed(self):
        result = infer_attacking_baskets(
            [
                ShotCoordinate(1, 10, 100, 200, 1, 100),
                ShotCoordinate(2, 10, None, 200, 1, 100),
                ShotCoordinate(3, 10, 100, 200, None, 100),
            ]
        )
        self.assertEqual(result.baskets[1], "left")
        self.assertIsNone(result.baskets[2])
        self.assertIsNone(result.baskets[3])
        self.assertEqual(result.unresolved_events, 2)


class ShotZoneAuditTests(unittest.TestCase):
    def test_summary_excludes_free_throws_and_reports_missing_coordinates(self):
        events = [
            AuditEvent(1, 2026, 10, 100, 200, 1, "rim", "ACC", 52.5, 250),
            AuditEvent(2, 2026, 10, 200, 100, 1, "three_pointer", "ACC", 600, 250),
            AuditEvent(3, 2026, 10, 100, 200, 1, "jumper", "ACC", None, None),
            AuditEvent(4, 2026, 10, 100, 200, 1, "free_throw", "ACC", None, None),
        ]
        report = summarize_events(events)
        metrics = report["seasons"]["2026"]
        self.assertEqual(metrics["field_goal_attempts"], 3)
        self.assertEqual(metrics["located_attempts"], 2)
        self.assertEqual(metrics["missing_coordinates"], 1)
        self.assertEqual(metrics["classified_attempts"], 2)
        self.assertEqual(metrics["coordinate_coverage_pct"], 66.6667)
        self.assertEqual(report["geometry_confusion"]["three_point->three_point"], 1)


if __name__ == "__main__":
    unittest.main()
