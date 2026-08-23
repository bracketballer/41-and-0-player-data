from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bracketballer_data.ranked_roster_delta import (
    create_delta_archive,
    safe_extract_delta,
    upload_delta,
    validate_publication_manifest,
)
from bracketballer_data.ranked_rosters import build_eligible_teams
from scripts.ingest.ingest_ranked_rosters import write_candidate_bundle


def fixture() -> dict:
    rankings = [
        {"season": 2026, "pollType": "AP Top 25", "teamId": i, "week": 1, "ranking": i}
        for i in range(1, 26)
    ]
    eligible = build_eligible_teams(rankings, 2026)
    return {
        "rankings": rankings,
        "eligible": eligible,
        "rosters": [
            {"teamId": row.team_id, "players": [{"id": row.team_id * 10 + i} for i in range(5)]}
            for row in eligible
        ],
        "player_seasons": [], "games": [], "lineups": [],
        "opponent_contexts": [], "skipped_lineup_game_ids": [],
        "unavailable_lineup_game_teams": [],
    }


class RankedRosterDeltaTests(unittest.TestCase):
    def test_archive_is_exact_two_files_and_safe_to_extract(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            output = root / "output"
            write_candidate_bundle(source, fixture(), 2026, "ranked-rosters-test.1")
            archive, checksum, publication, manifest = create_delta_archive(
                candidate_dir=source,
                output_dir=output,
                season=2026,
                release_version="ranked-rosters-test.1",
                pipeline_commit="a" * 40,
                flyway_v34_checksums={"34": -1},
            )
            self.assertEqual(
                validate_publication_manifest(manifest, archive=archive, checksum=checksum),
                manifest["archive"]["sha256"],
            )
            with tarfile.open(archive, "r:gz") as tar:
                self.assertEqual(sorted(tar.getnames()), ["candidate.json", "manifest.json"])
            extracted = root / "extracted"
            candidate, source_manifest = safe_extract_delta(archive, extracted)
            self.assertTrue(candidate.is_file())
            self.assertTrue(source_manifest.is_file())

    def test_upload_publishes_manifest_last_and_resumes(self):
        class NoSuchKey(Exception):
            pass

        Exceptions = type("Exceptions", (), {"NoSuchKey": NoSuchKey})

        class Client:
            exceptions = Exceptions

            def __init__(self):
                self.objects = {}
                self.uploads = []

            def head_object(self, *, Bucket, Key):
                if Key not in self.objects:
                    raise NoSuchKey()
                return self.objects[Key]

            def upload_file(self, path, bucket, key, ExtraArgs):
                self.uploads.append(key)
                self.objects[key] = {
                    "Metadata": ExtraArgs["Metadata"],
                    "ContentLength": Path(path).stat().st_size,
                }

        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            output = root / "output"
            write_candidate_bundle(source, fixture(), 2026, "ranked-rosters-test.1")
            archive, checksum, publication, _ = create_delta_archive(
                candidate_dir=source, output_dir=output, season=2026,
                release_version="ranked-rosters-test.1", pipeline_commit="a" * 40,
                flyway_v34_checksums={"34": -1},
            )
            client = Client()
            with patch.dict(os.environ, {
                "DO_SPACES_BUCKET": "bucket",
                "DO_SPACES_ACCESS_KEY_ID": "access",
                "DO_SPACES_SECRET_ACCESS_KEY": "secret",
            }, clear=False):
                upload_delta(archive=archive, checksum=checksum, manifest_path=publication, client=client)
                upload_delta(archive=archive, checksum=checksum, manifest_path=publication, client=client)
            self.assertTrue(client.uploads[-1].endswith(".json"))
            self.assertEqual(len(client.uploads), 3)


if __name__ == "__main__":
    unittest.main()
