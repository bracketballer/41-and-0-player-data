import unittest

from bracketballer_data.ranked_rosters import (
    VIRGINIA_TECH_TEAM_ID,
    build_eligible_teams,
    normalize_positions,
    validate_ap_top25_coverage,
    validate_full_roster_coverage,
    validate_roster_coverage,
)


class RankedRosterTests(unittest.TestCase):
    def test_eligibility_is_ap_union_plus_virginia_tech(self):
        rows = [
            {
                "season": 2026,
                "pollType": "AP Top 25",
                "teamId": 10,
                "week": 1,
                "ranking": 25,
                "pollDate": "2025-11-01",
            },
            {
                "season": 2026,
                "pollType": "AP Top 25",
                "teamId": 10,
                "week": 4,
                "ranking": 8,
                "pollDate": "2025-11-22",
            },
            {"season": 2026, "pollType": "ap", "teamId": 20, "week": 2, "ranking": 3, "pollDate": "2025-11-08"},
            {"season": 2026, "pollType": "coaches", "teamId": 30, "week": 2, "ranking": 1, "pollDate": "2025-11-08"},
            {"season": 2026, "pollType": "ap", "teamId": 40, "week": 2, "ranking": 26, "pollDate": "2025-11-08"},
        ]
        result = {row.team_id: row for row in build_eligible_teams(rows, 2026)}
        self.assertEqual(set(result), {10, 20, VIRGINIA_TECH_TEAM_ID})
        self.assertEqual(result[10].first_poll_week, 1)
        self.assertEqual(result[10].peak_rank, 8)
        self.assertEqual(result[10].reasons, ("ap_top_25",))
        self.assertEqual(
            result[VIRGINIA_TECH_TEAM_ID].reasons,
            ("virginia_tech",),
        )

    def test_virginia_tech_keeps_both_reasons_when_ranked(self):
        result = build_eligible_teams(
            [{"season": 2026, "pollType": "ap", "teamId": 340, "week": 5, "ranking": 20}],
            2026,
        )
        self.assertEqual(result[0].reasons, ("ap_top_25", "virginia_tech"))

    def test_ap_top_25_coverage_rejects_vt_only_fallback(self):
        eligible = build_eligible_teams([], 2026)
        with self.assertRaisesRegex(ValueError, "at least 25"):
            validate_ap_top25_coverage(eligible)

    def test_ap_top_25_coverage_accepts_full_poll(self):
        rankings = [
            {
                "season": 2026,
                "pollType": "AP Top 25",
                "teamId": team_id,
                "week": 1,
                "ranking": team_id,
            }
            for team_id in range(1, 26)
        ]
        summary = validate_ap_top25_coverage(
            build_eligible_teams(rankings, 2026)
        )
        self.assertEqual(summary["ap_top25_teams"], 25)

    def test_position_normalization(self):
        self.assertEqual(normalize_positions("F-C"), ("PF", "C"))
        self.assertEqual(normalize_positions("g/f"), ("SG", "SF"))
        self.assertEqual(normalize_positions("Forward"), ("SF", "PF"))
        self.assertEqual(normalize_positions("Center"), ("C",))
        self.assertEqual(normalize_positions(None), ())

    def test_full_roster_validation_allows_transfers(self):
        summary = validate_full_roster_coverage(
            [
                {
                    "season": 2026,
                    "teamId": 1,
                    "team": "One",
                    "players": [
                        {"id": player_id, "name": f"Player {player_id}"}
                        for player_id in range(1, 6)
                    ],
                },
                {
                    "season": 2026,
                    "teamId": 2,
                    "team": "Two",
                    "players": [
                        {"id": player_id, "name": f"Player {player_id}"}
                        for player_id in range(1, 5)
                    ]
                    + [{"id": 6, "name": "Player 6"}],
                },
            ],
            2026,
        )
        self.assertEqual(summary["full_roster_teams"], 2)
        self.assertEqual(summary["full_roster_players"], 10)
        self.assertEqual(summary["full_roster_unique_players"], 6)

    def test_full_roster_validation_rejects_duplicate_player_and_bad_season(self):
        with self.assertRaisesRegex(ValueError, "duplicate player"):
            validate_full_roster_coverage(
                [
                    {
                        "season": 2026,
                        "teamId": 1,
                        "team": "One",
                        "players": [
                            {"id": 1, "name": "Player 1"},
                            {"id": 1, "name": "Player 1"},
                            {"id": 2, "name": "Player 2"},
                            {"id": 3, "name": "Player 3"},
                            {"id": 4, "name": "Player 4"},
                        ],
                    }
                ],
                2026,
            )
        with self.assertRaisesRegex(ValueError, "duplicate roster"):
            validate_full_roster_coverage(
                [
                    {
                        "season": 2026,
                        "teamId": 1,
                        "team": "One",
                        "players": [
                            {"id": player_id, "name": f"Player {player_id}"}
                            for player_id in range(1, 6)
                        ],
                    },
                    {
                        "season": 2026,
                        "teamId": 1,
                        "team": "One",
                        "players": [
                            {"id": player_id, "name": f"Other {player_id}"}
                            for player_id in range(6, 11)
                        ],
                    },
                ],
                2026,
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_full_roster_coverage(
                [
                    {
                        "season": 2025,
                        "teamId": 1,
                        "team": "One",
                        "players": [
                            {"id": player_id, "name": f"Player {player_id}"}
                            for player_id in range(1, 6)
                        ],
                    }
                ],
                2026,
            )

    def test_roster_validation_rejects_missing_and_undersized_teams(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_roster_coverage({1, 2}, [1, 1, 1, 1, 1])
        with self.assertRaisesRegex(ValueError, "undersized"):
            validate_roster_coverage({1}, [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
