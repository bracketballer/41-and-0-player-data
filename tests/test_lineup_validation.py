import unittest
import json
import tempfile
from pathlib import Path

from bracketballer_data.lineup_validation import (
    AMBIGUOUS_BOUNDARY,
    INVALID_REFERENCE,
    MATCH,
    MISMATCH,
    ON_FLOOR_UNRESOLVED,
    GameMoment,
    SubstitutionStint,
    TemporalValidationStat,
    compare_on_floor_to_reference,
    elapsed_seconds,
    lineups_at_moment,
    nearest_boundary_distance,
    wilson_interval,
)
from scripts.analysis.validate_onfloor_lineups import evaluate_audit_manifest


def stint(player_id, sub_in=1200, sub_out=None, period=1, out_period=None):
    return SubstitutionStint.from_payload(
        {
            "gameId": 10,
            "teamId": 20,
            "athleteId": player_id,
            "subIn": {"period": period, "secondsRemaining": sub_in},
            "subOut": (
                {
                    "period": out_period or period,
                    "secondsRemaining": sub_out,
                }
                if sub_out is not None
                else None
            ),
        }
    )


class ClockTests(unittest.TestCase):
    def test_regulation_and_overtime_are_monotonic(self):
        self.assertEqual(elapsed_seconds(1, 1200), 0)
        self.assertEqual(elapsed_seconds(2, 1200), 1200)
        self.assertEqual(elapsed_seconds(2, 0), 2400)
        self.assertEqual(elapsed_seconds(3, 300), 2400)
        self.assertEqual(elapsed_seconds(3, 0), 2700)


class StintTests(unittest.TestCase):
    def test_starter_five_resolves(self):
        rows = [stint(player) for player in range(1, 6)]
        result = lineups_at_moment(rows, GameMoment(1, 600), set(range(1, 6)))
        self.assertEqual(result.status, "matchable")
        self.assertEqual(result.player_ids, frozenset(range(1, 6)))

    def test_substitution_changes_five_between_clocks(self):
        rows = [stint(player) for player in range(1, 5)]
        rows += [stint(5, sub_in=1200, sub_out=600)]
        rows += [stint(6, sub_in=600)]
        before = lineups_at_moment(rows, GameMoment(1, 601), set(range(1, 7)))
        after = lineups_at_moment(rows, GameMoment(1, 599), set(range(1, 7)))
        self.assertEqual(before.player_ids, frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(after.player_ids, frozenset({1, 2, 3, 4, 6}))

    def test_same_clock_is_ambiguous(self):
        rows = [stint(player) for player in range(1, 5)]
        rows += [stint(5, sub_in=1200, sub_out=600)]
        rows += [stint(6, sub_in=600)]
        result = lineups_at_moment(rows, GameMoment(1, 600), set(range(1, 7)))
        self.assertEqual(result.status, AMBIGUOUS_BOUNDARY)

    def test_invalid_active_count_is_not_a_match(self):
        rows = [stint(player) for player in range(1, 4)]
        result = lineups_at_moment(rows, GameMoment(1, 600), set(range(1, 4)))
        self.assertEqual(result.status, INVALID_REFERENCE)

    def test_malformed_reverse_interval_is_dropped(self):
        self.assertIsNone(stint(1, sub_in=600, sub_out=1200))

    def test_nearest_boundary_uses_elapsed_time(self):
        rows = [stint(1, sub_in=1200, sub_out=600)]
        self.assertEqual(nearest_boundary_distance(rows, GameMoment(1, 605)), 5)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.rows = [stint(player) for player in range(1, 6)]
        self.payload = {
            "team": "Offense",
            "opponent": "Defense",
            "period": 1,
            "secondsRemaining": 600,
            "onFloor": [
                {"id": player, "team": "Defense"} for player in range(1, 6)
            ]
            + [{"id": player + 10, "team": "Offense"} for player in range(1, 6)],
        }

    def test_exact_temporal_match(self):
        comparison = compare_on_floor_to_reference(
            self.payload, "defense", set(range(1, 16)), self.rows
        )
        self.assertEqual(comparison.status, MATCH)

    def test_temporal_mismatch_is_counted(self):
        self.payload["onFloor"][0]["id"] = 6
        comparison = compare_on_floor_to_reference(
            self.payload, "defense", set(range(1, 16)), self.rows
        )
        self.assertEqual(comparison.status, MISMATCH)

    def test_unresolved_on_floor_never_compares(self):
        self.payload["onFloor"][0]["id"] = 99
        comparison = compare_on_floor_to_reference(
            self.payload, "defense", set(range(1, 6)), self.rows
        )
        self.assertEqual(comparison.status, ON_FLOOR_UNRESOLVED)


class StatisticsTests(unittest.TestCase):
    def test_temporal_stats_separate_matchable_and_missing_time(self):
        rows = [stint(player) for player in range(1, 6)]
        payload = {
            "team": "Offense",
            "opponent": "Defense",
            "period": 1,
            "secondsRemaining": 600,
            "onFloor": [
                {"id": player, "team": "Defense"} for player in range(1, 6)
            ],
        }
        stat = TemporalValidationStat(2026, "defense")
        stat.record(
            compare_on_floor_to_reference(
                payload, "defense", set(range(1, 6)), rows
            )
        )
        stat.record(
            compare_on_floor_to_reference(
                {**payload, "secondsRemaining": None},
                "defense",
                set(range(1, 6)),
                rows,
            )
        )
        self.assertEqual(stat.events_seen, 2)
        self.assertEqual(stat.matchable, 1)
        self.assertEqual(stat.matches, 1)

    def test_wilson_zero_error_has_nonzero_lower_bound(self):
        lower, upper = wilson_interval(150, 150)
        self.assertGreater(lower, 0.97)
        self.assertEqual(upper, 1.0)

    def test_independent_audit_manifest_counts_exact_match(self):
        payload = {
            "team": "Offense",
            "opponent": "Defense",
            "onFloor": [
                {"id": player, "team": "Defense"} for player in range(1, 6)
            ],
        }
        manifest = {
            "schema_version": 1,
            "events": [
                {
                    "source_play_id": 99,
                    "defensive_team_id": 20,
                    "annotation_status": "complete",
                    "expected_player_ids": [1, 2, 3, 4, 5],
                    "evidence": {
                        "type": "official_record",
                        "url": "https://www.ncaa.com/game/99",
                        "locator": "period 1, 10:00",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            path.write_text(json.dumps(manifest))
            result = evaluate_audit_manifest(
                path,
                {99: (1, 1, 20, payload)},
                set(range(1, 6)),
            )
        self.assertEqual(result["exact_matches"], 1)
        self.assertEqual(result["mismatches"], 0)

    def test_independent_audit_rejects_cbbd_evidence(self):
        manifest = {
            "schema_version": 1,
            "events": [
                {
                    "source_play_id": 99,
                    "defensive_team_id": 20,
                    "annotation_status": "complete",
                    "expected_player_ids": [1, 2, 3, 4, 5],
                    "evidence": {
                        "type": "cbbd_ui",
                        "url": "https://api.collegebasketballdata.com/plays/99",
                        "locator": "period 1",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.json"
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                evaluate_audit_manifest(path, {}, set(range(1, 6)))


if __name__ == "__main__":
    unittest.main()
