from __future__ import annotations

import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.compute.compute_defense_lineup_concession import model_configuration
from scripts.compute.compute_shot_location_profiles import MODEL_CONFIGURATION as PLAYER_MODEL_CONFIGURATION
from scripts.dev_sync.tickets.issue_0010_defense_lineup_concession import validate_artifact
from scripts.dev_sync.tickets.issue_0009_shot_location_profiles import _configuration_contains


def descriptor(row_count: int = 5) -> dict:
    return {
        "first_season": 2026,
        "last_season": 2026,
        "expected": {
            "row_counts": {
                "teams": 1,
                "realistic_units": 1,
                "defense_lineup_zone_concession": row_count,
                "profiles_with_five_zones": 1 if row_count == 5 else 0,
            }
        },
    }


def row(zone: str) -> dict:
    return {
        "model_version": "shot-location-v1",
        "season": 2026,
        "team_id": 10,
        "lineup_hash": "1-2-3-4-5",
        "lineup_player_ids": [1, 2, 3, 4, 5],
        "zone": zone,
        "zone_concession_tilt": 0.1,
        "possessions": 100.0,
        "confidence": 90.9090909090909,
        "evidence_status": "provisional",
        "minutes_share": 0.05,
        "classified_attempts": 500,
        "opponent_fga": 500.0,
        "attribution_coverage": 1.0,
    }


def write_artifact(root: Path, rows: list[dict]) -> None:
    configuration = model_configuration()
    metadata = {
        "format_version": 1,
        "dataset": "defense_lineup_zone_concession",
        "model_version": "shot-location-v1",
        "first_season": 2026,
        "last_season": 2026,
        "configuration": configuration,
        "teams": 1,
        "realistic_units": 1,
        "defense_lineup_zone_concession": len(rows),
        "profiles_with_five_zones": 1 if len(rows) == 5 else 0,
        "status_counts": {
            "available": 0,
            "provisional": len(rows),
            "unavailable": 0,
        },
    }
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with gzip.open(root / "concessions.jsonl.gz", "wt", encoding="utf-8") as target:
        for value in rows:
            target.write(json.dumps(value) + "\n")


class Issue0010ArtifactTests(unittest.TestCase):
    def test_valid_five_zone_artifact_is_prepared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifact(root, [row(zone) for zone in ("rim", "short_mid", "long_mid", "corner_three", "above_break_three")])
            prepared = validate_artifact(root, descriptor())
        self.assertEqual(prepared["counts"]["realistic_units"], 1)
        self.assertEqual(len(prepared["rows"]), 5)

    def test_duplicate_zone_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [row(zone) for zone in ("rim", "short_mid", "long_mid", "corner_three", "above_break_three")]
            rows[-1]["zone"] = "rim"
            write_artifact(root, rows)
            with self.assertRaises(ValueError):
                validate_artifact(root, descriptor())

    def test_unavailable_evidence_must_have_null_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [row(zone) for zone in ("rim", "short_mid", "long_mid", "corner_three", "above_break_three")]
            rows[0]["classified_attempts"] = 0
            rows[0]["zone_concession_tilt"] = 0.0
            rows[0]["confidence"] = None
            write_artifact(root, rows)
            with self.assertRaises(ValueError):
                validate_artifact(root, descriptor())

    def test_issue_9_configuration_accepts_downstream_extension(self):
        base = copy.deepcopy(PLAYER_MODEL_CONFIGURATION)
        base["model_version"] = "shot-location-v1"
        extension = model_configuration()
        self.assertTrue(_configuration_contains(extension, base))
        self.assertTrue(_configuration_contains(extension, {"defense_concession": {"confidence_floor": 45.0}}))
        extension["zone_scheme"] = ["wrong"]
        self.assertFalse(_configuration_contains(extension, base))


if __name__ == "__main__":
    unittest.main()
