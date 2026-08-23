from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.apply_pending_data_releases import (
    database_host,
    descriptor_paths,
    is_local_database,
    read_descriptor,
    require_local_database,
)


class DataReleaseRunnerTests(unittest.TestCase):
    def test_local_database_guard_handles_url_and_keyword_dsns(self):
        self.assertEqual(database_host("postgresql://u:p@localhost/db"), "localhost")
        self.assertEqual(database_host("host=127.0.0.1 dbname=db"), "127.0.0.1")
        self.assertTrue(is_local_database("postgresql://localhost/db"))
        self.assertFalse(is_local_database("postgresql://db.example/db"))
        with self.assertRaisesRegex(RuntimeError, "remote"):
            require_local_database("postgresql://db.example/db")

    def test_descriptor_is_explicit_and_validates_checksums(self):
        descriptor = read_descriptor(Path("releases/ranked-rosters-2026-08-23.1.json"))
        self.assertEqual(descriptor["objects"]["manifest"].split("/")[0], "data-releases")
        self.assertEqual(descriptor["expected"]["candidate_sha256"], "b55851d4ec42cf1b01be9134f260a50a1624dc0315783839ca0ef75e8d0456c7")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(descriptor_paths(Path(directory)), [])


if __name__ == "__main__":
    unittest.main()

