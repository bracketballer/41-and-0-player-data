import math
import unittest

from bracketballer_data.shot_zones import (
    ShotCoordinate,
    enrich_shot_event,
    enrich_shot_events,
)


class Issue0023EnrichmentTests(unittest.TestCase):
    def test_status_precedence_and_no_fabricated_evidence(self):
        missing = enrich_shot_event({"source_play_id": 1, "location_x": None, "location_y": 501}, "left")
        self.assertEqual(missing.mapping_status, "missing_coordinates")
        self.assertIsNone(missing.normalized_coordinates)

        invalid = enrich_shot_event({"source_play_id": 2, "location_x": math.nan, "location_y": 250}, "left")
        self.assertEqual(invalid.mapping_status, "invalid_coordinates")
        unresolved = enrich_shot_event({"source_play_id": 3, "location_x": 100, "location_y": 250}, None)
        self.assertEqual(unresolved.mapping_status, "unresolved_direction")

    def test_reflection_and_complete_game_direction_sample(self):
        selected = [{"source_play_id": 10, "game_id": 1, "team_id": 52, "period": 1, "location_x": 100, "location_y": 250}]
        all_game = [
            ShotCoordinate(10, 1, 52, 340, 1, 100),
            ShotCoordinate(11, 1, 340, 52, 1, 800),
            # A selected-player heave votes against the normal orientation,
            # but cannot overturn the complete-game sample above.
            ShotCoordinate(12, 1, 52, 340, 1, 800),
        ]
        result = enrich_shot_events(selected, direction_observations=all_game)[0]
        self.assertEqual(result.mapping_status, "mapped")
        self.assertEqual(result.attacking_basket, "left")
        self.assertEqual(result.normalized_coordinates, (10.0, 25.0))


if __name__ == "__main__":
    unittest.main()
