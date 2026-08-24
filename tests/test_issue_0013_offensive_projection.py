from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.compute.compute_lineup_offensive_projections import model_configuration
from scripts.dev_sync.tickets.issue_0013_lineup_offensive_projections import validate_artifact


def row(offensive_hash: str = "1-2-3-4-5", *, status: str = "provisional") -> dict:
    unavailable = status == "unavailable"
    return {
        "season": 2026,
        "model_version": "shot-location-v1",
        "offense_team_id": 340,
        "offensive_lineup_hash": offensive_hash,
        "offensive_player_ids": [1, 2, 3, 4, 5],
        "defense_team_id": 41,
        "defensive_lineup_hash": "11-12-13-14-15",
        "defensive_player_ids": [11, 12, 13, 14, 15],
        "projected_pps": None if unavailable else 1.2,
        "interval_low": None if unavailable else 1.0,
        "interval_high": None if unavailable else 1.4,
        "confidence": None if unavailable else 40.0,
        "evidence_status": status,
        "matchup_assignment": None if unavailable else [{"offensive_player_id": index, "defensive_player_id": index + 10} for index in range(1, 6)],
    }


def write_artifact(root: Path, rows: list[dict]) -> None:
    metadata = {
        "dataset": "lineup_offensive_projections",
        "model_version": "shot-location-v1",
        "season": 2026,
        "configuration": model_configuration(),
    }
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with gzip.open(root / "projections.jsonl.gz", "wt", encoding="utf-8") as target:
        for value in rows:
            target.write(json.dumps(value) + "\n")


def descriptor(expected_rows: int = 1) -> dict:
    return {
        "first_season": 2026,
        "last_season": 2026,
        "model_version": "shot-location-v1",
        "offense_team_id": 340,
        "defense_team_id": 41,
        "expected": {"row_counts": {"lineup_offensive_projections": expected_rows}},
    }


class Issue0013ArtifactTests(unittest.TestCase):
    def test_valid_scored_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifact(root, [row()])
            prepared = validate_artifact(root, descriptor())
        self.assertEqual(prepared["counts"]["lineup_offensive_projections"], 1)

    def test_unavailable_rows_must_have_null_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = row(status="unavailable")
            value["projected_pps"] = 0.0
            write_artifact(root, [value])
            with self.assertRaises(ValueError):
                validate_artifact(root, descriptor())

    def test_point_estimate_must_be_inside_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = row()
            value["projected_pps"] = 2.0
            write_artifact(root, [value])
            with self.assertRaises(ValueError):
                validate_artifact(root, descriptor())

    def test_duplicate_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifact(root, [row(), row()])
            with self.assertRaises(ValueError):
                validate_artifact(root, descriptor(expected_rows=2))


if __name__ == "__main__":
    unittest.main()
