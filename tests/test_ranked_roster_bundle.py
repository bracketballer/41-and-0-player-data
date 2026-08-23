import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bracketballer_data.ranked_rosters import build_eligible_teams
from scripts.ingest.ingest_ranked_rosters import (
    cached_rows,
    candidate_reference_school_names,
    load_candidate_bundle,
    main,
    raw_lineup_rows,
    select_reconciled_lineups,
    read_json,
    source_directory,
    supplement_rosters_from_lineups,
    write_candidate_bundle,
    write_json,
)


SEASON = 2026
RELEASE_VERSION = "ranked-rosters-test.1"


def candidate_fixture() -> dict:
    rankings = [
        {
            "season": SEASON,
            "pollType": "AP Top 25",
            "teamId": team_id,
            "week": 1,
            "ranking": team_id,
        }
        for team_id in range(1, 26)
    ]
    eligible = build_eligible_teams(rankings, SEASON)
    return {
        "rankings": rankings,
        "eligible": eligible,
        "rosters": [
            {
                "teamId": team.team_id,
                "players": [
                    {"id": team.team_id * 100 + ordinal}
                    for ordinal in range(1, 6)
                ],
            }
            for team in eligible
        ],
        "player_seasons": [],
        "games": [],
        "lineups": [],
        "opponent_contexts": [],
        "skipped_lineup_game_ids": [],
        "unavailable_lineup_game_teams": [],
    }


class RankedRosterBundleTests(unittest.TestCase):
    def test_historical_reference_schools_come_from_player_seasons(self):
        candidate = candidate_fixture()
        candidate["player_seasons"] = [
            {"teamId": 529, "team": "Lynchburg"},
            {"teamId": 529, "team": "Lynchburg"},
            {"teamId": 978, "team": "Emory & Henry"},
        ]

        self.assertEqual(
            candidate_reference_school_names(candidate),
            {529: "Lynchburg", 978: "Emory & Henry"},
        )

    def test_unreconciled_lineups_are_audited_and_excluded(self):
        game = {
            "id": 1,
            "status": "final",
            "homeTeamId": 10,
            "awayTeamId": 20,
            "homePoints": 70,
            "awayPoints": 60,
        }
        lineups = [
            {
                "_gameId": 1,
                "teamId": 10,
                "totalSeconds": 2400,
                "teamStats": {"points": 69},
            }
        ]

        selected, unavailable = select_reconciled_lineups(
            [game],
            lineups,
            {10, 20},
        )

        self.assertEqual(selected, [])
        self.assertEqual(
            {(row["team_id"], row["reason"]) for row in unavailable},
            {(10, "failed_reconciliation"), (20, "missing_source_rows")},
        )

    def test_raw_lineups_bypass_sdk_model_validation(self):
        class Response:
            raw_data = b'[{"teamId": 1, "defenseRating": null}]'

        class LineupsApi:
            def get_lineup_stats_by_game_with_http_info(self, **kwargs):
                self.arguments = kwargs
                return Response()

        api = LineupsApi()
        rows = raw_lineup_rows(api, game_id=123, retries=1)

        self.assertEqual(rows, [{"teamId": 1, "defenseRating": None}])
        self.assertFalse(api.arguments["_preload_content"])

    def test_lineups_supplement_missing_roster_memberships(self):
        rosters = [{"teamId": 1, "players": [{"id": 10, "name": "Existing"}]}]
        lineups = [
            {
                "teamId": 1,
                "athletes": [
                    {"id": 10, "name": "Existing"},
                    {"id": 11, "name": "Lineup Evidence"},
                ],
            }
        ]

        inferred = supplement_rosters_from_lineups(rosters, lineups)

        self.assertEqual(inferred, 1)
        self.assertEqual(rosters[0]["players"][-1]["id"], 11)
        self.assertEqual(
            rosters[0]["players"][-1]["_membershipEvidence"],
            "lineup",
        )

    def test_default_source_directory_rejects_unsafe_release_version(self):
        with self.assertRaisesRegex(ValueError, "release-version"):
            source_directory(SEASON, "../escape")

    def test_cached_rows_resumes_without_refetching(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "response.json"
            calls = []

            def fetch():
                calls.append(True)
                return [{"id": 1}]

            self.assertEqual(
                cached_rows(cache_path, fetch, retries=1, label="test"),
                [{"id": 1}],
            )
            self.assertEqual(
                cached_rows(cache_path, fetch, retries=1, label="test"),
                [{"id": 1}],
            )
            self.assertEqual(len(calls), 1)

    def test_bundle_round_trip_verifies_manifest_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary)
            write_candidate_bundle(
                source_dir,
                candidate_fixture(),
                SEASON,
                RELEASE_VERSION,
            )

            loaded = load_candidate_bundle(
                source_dir,
                SEASON,
                RELEASE_VERSION,
            )

            self.assertEqual(len(loaded["rankings"]), 25)
            self.assertEqual(len(loaded["eligible"]), 26)
            manifest = read_json(source_dir / "manifest.json")
            self.assertEqual(manifest["status"], "complete")

    def test_bundle_rejects_tampered_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary)
            write_candidate_bundle(
                source_dir,
                candidate_fixture(),
                SEASON,
                RELEASE_VERSION,
            )
            candidate = read_json(source_dir / "candidate.json")
            candidate["rankings"].append(
                {
                    "season": SEASON,
                    "pollType": "coaches",
                    "teamId": 999,
                    "week": 1,
                    "ranking": 1,
                }
            )
            write_json(source_dir / "candidate.json", candidate)

            with self.assertRaisesRegex(ValueError, "checksum"):
                load_candidate_bundle(source_dir, SEASON, RELEASE_VERSION)

    def test_dry_run_uses_bundle_without_api_or_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary)
            write_candidate_bundle(
                source_dir,
                candidate_fixture(),
                SEASON,
                RELEASE_VERSION,
            )
            arguments = [
                "ingest_ranked_rosters",
                "--season",
                str(SEASON),
                "--release-version",
                RELEASE_VERSION,
                "--source-dir",
                str(source_dir),
            ]

            with (
                patch("sys.argv", arguments),
                patch(
                    "scripts.ingest.ingest_ranked_rosters.cbbd.ApiClient",
                    side_effect=AssertionError("dry-run called CBBD"),
                ),
                patch(
                    "scripts.ingest.ingest_ranked_rosters.psycopg2.connect",
                    side_effect=AssertionError("dry-run connected to PostgreSQL"),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                main()

            self.assertIn("DRY RUN: no database rows changed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
