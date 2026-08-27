import math
import tempfile
import unittest
from pathlib import Path

from bracketballer_data.shot_zones import (
    FIELD_GOAL_ZONES,
    ShotCoordinate,
    enrich_shot_event,
    enrich_shot_events,
)
from scripts.dev_sync.tickets.issue_0023_vt_clemson_shot_locations import validate_artifact
from scripts.publish.export_issue_0023_vt_clemson_shot_locations import export_issue_0023


class _StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _StubConnection:
    """Replays canned rows in query order, mirroring the exporter's four reads."""

    def __init__(self, *result_sets):
        self._queue = list(result_sets)

    def cursor(self):
        return _StubCursor(self._queue.pop(0))


def _build_fixture_connection() -> _StubConnection:
    """Build a minimal but fully scope-locked issue #23 dataset.

    Sized to satisfy every hard-coded lock in the exporter and validator
    (20 eligible player-seasons, 7 sub-100-minute exclusions including one
    with no ``player_seasons`` row at all, 100 profile rows, and exactly
    3857 field-goal events split evenly across both teams so coverage and
    2-/3-point agreement clear the activation gate).
    """

    season = 2026
    eligible_player_ids = list(range(1, 21))
    roster_rows = [
        (52 if player_id % 2 == 0 else 340, player_id, season, 150.0)
        for player_id in eligible_player_ids
    ]
    # Six genuinely sub-100-minute players plus one active roster membership
    # with no player_seasons row at all (minutes=None) -- the LEFT JOIN case.
    excluded_minutes = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, None]
    roster_rows += [
        (52 if index % 2 == 0 else 340, 100 + index, season, minutes)
        for index, minutes in enumerate(excluded_minutes)
    ]

    profile_rows = [
        (player_id, season, "shot-location-v1", zone, 10, 5, 0.2, 1.0, 1.0, 1.0)
        for player_id in eligible_player_ids
        for zone in FIELD_GOAL_ZONES
    ]

    total_events = 3857
    event_rows = []
    for index in range(total_events):
        team_id = 52 if index % 2 == 0 else 340
        opponent_id = 340 if team_id == 52 else 52
        game_id = 101 if team_id == 52 else 102
        player_id = eligible_player_ids[index % len(eligible_player_ids)]
        event_rows.append(
            (
                index + 1,
                player_id,
                season,
                game_id,
                team_id,
                opponent_id,
                1,
                index % 2 == 0,
                "jump_shot",
                100,
                250,
            )
        )
    # Each game is single-team here, so direction inference is unambiguous:
    # every observed location_x votes for the same side in period 1.
    game_event_rows = [(row[0], row[3], row[4], row[5], row[6], row[9], row[10]) for row in event_rows]

    return _StubConnection(roster_rows, profile_rows, event_rows, game_event_rows)


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


class Issue0023ExportMetadataTests(unittest.TestCase):
    def test_excluded_alias_survives_row_counts_merge_and_null_minutes_validates(self):
        connection = _build_fixture_connection()
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            metadata = export_issue_0023(connection, destination)

            # The historical bug: metadata.update(row_counts) clobbered the
            # 7-entry audit list with row_counts' integer count of 7.
            excluded = metadata["excluded_sub_rotation_players"]
            self.assertIsInstance(excluded, list)
            self.assertEqual(len(excluded), 7)
            self.assertTrue(all(isinstance(item, dict) for item in excluded))
            self.assertEqual(sum(item["minutes"] is None for item in excluded), 1)

            self.assertIsInstance(metadata["row_counts"], dict)
            self.assertEqual(metadata["row_counts"]["excluded_sub_rotation_players"], 7)
            self.assertEqual(metadata["eligible_player_seasons"], 20)

            # A minutes:null excluded entry (no player_seasons row) must
            # survive validate_artifact's nullable minutes handling.
            prepared = validate_artifact(destination, {})
            self.assertEqual(prepared["counts"]["excluded_sub_rotation_players"], 7)


if __name__ == "__main__":
    unittest.main()
