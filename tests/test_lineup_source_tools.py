import tempfile
import unittest
from pathlib import Path

from scripts.analysis.fetch_cbbd_lineup_sources import (
    read_json,
    safe_team_filename,
    sha256_file,
    write_json,
)
from scripts.analysis.sample_onfloor_audit import select_quota
from scripts.analysis.validate_onfloor_lineups import load_bundle


class LineupSourceToolTests(unittest.TestCase):
    def test_team_filename_is_stable_and_safe(self):
        first = safe_team_filename("St. John's")
        self.assertEqual(first, safe_team_filename("St. John's"))
        self.assertNotIn("/", first)
        self.assertTrue(first.endswith(".json"))

    def test_atomic_json_round_trip_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "source.json"
            write_json(path, {"rows": [1, 2, 3]})
            self.assertEqual(read_json(path), {"rows": [1, 2, 3]})
            self.assertEqual(len(sha256_file(path)), 64)
            self.assertFalse(path.with_suffix(".json.part").exists())

    def test_quota_prefers_new_games_and_respects_cap(self):
        candidates = [
            {
                "source_play_id": index,
                "game_id": index // 2,
            }
            for index in range(40)
        ]
        used_games = set()
        per_game = {}
        selected = select_quota(candidates, 5, used_games, per_game)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len(used_games), 5)
        self.assertTrue(all(value <= 10 for value in per_game.values()))

    def test_bundle_seeds_starters_missing_from_substitution_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            season_dir = root / "2026"
            substitution_path = season_dir / "substitutions" / "team.json"
            game_players_path = season_dir / "game_players.json"
            write_json(substitution_path, [
                {
                    "gameId": 10,
                    "teamId": 20,
                    "athleteId": 1,
                    "subIn": {"period": 1, "secondsRemaining": 1200},
                    "subOut": None,
                }
            ])
            write_json(
                game_players_path,
                [
                    {
                        "gameId": 10,
                        "teamId": 20,
                        "players": [
                            {
                                "athleteId": 4000 + player,
                                "athleteSourceId": str(9000 + player),
                                "starter": True,
                            }
                            for player in range(1, 6)
                        ],
                    }
                ],
            )
            write_json(
                season_dir / "manifest.json",
                {
                    "format_version": 2,
                    "season": 2026,
                    "teams": [
                        {
                            "team": "Example",
                            "substitutions_file": "substitutions/team.json",
                            "rows": 1,
                            "sha256": sha256_file(substitution_path),
                            "game_players_file": "game_players.json",
                            "game_players_rows": 1,
                            "game_players_sha256": sha256_file(game_players_path),
                        }
                    ],
                },
            )
            stints, _ = load_bundle(
                2026,
                root,
                {str(9000 + player): player for player in range(1, 6)},
            )
            self.assertEqual(
                {stint.player_id for stint in stints[(10, 20)]},
                {1, 2, 3, 4, 5},
            )


if __name__ == "__main__":
    unittest.main()
