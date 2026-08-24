from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from bracketballer_data.shooting_data import EVENT_DB_COLUMNS
from scripts.dev_sync.tickets.issue_0006_expand_shot_corpus import validate_artifact
from scripts.ingest.ingest_cbbd_shots_bulk import (
    BUNDLE_FORMAT_VERSION,
    ELIGIBLE_PLAYER_SEASONS_SQL,
    _write_bundle_date,
    load_bundle_dates,
)
from scripts.publish.publish_ticket_release import _row_counts


def _event(player_id: int = 9001, season: int = 2024, source_play_id: int = 1) -> dict:
    value = {column: None for column in EVENT_DB_COLUMNS}
    value.update(
        {
            "source_play_id": source_play_id,
            "source_id": f"play-{source_play_id}",
            "player_id": player_id,
            "season": season,
            "game_id": 1001,
            "raw_payload": {"id": source_play_id, "season": season},
        }
    )
    return value


def _descriptor() -> dict:
    return {
        "first_season": 2024,
        "last_season": 2026,
        "expected": {
            "row_counts": {
                "expanded_player_seasons": 1,
                "event_rows": 1,
                "success_player_seasons": 1,
                "no_data_player_seasons": 0,
            }
        },
    }


class Issue0006ShotCorpusTests(unittest.TestCase):
    def test_eligibility_sql_preserves_legacy_union_and_minutes_gate(self):
        self.assertIn("SELECT p.id AS player_id", ELIGIBLE_PLAYER_SEASONS_SQL)
        self.assertIn("UNION", ELIGIBLE_PLAYER_SEASONS_SQL)
        self.assertIn("season_row.minutes >= 100", ELIGIBLE_PLAYER_SEASONS_SQL)
        self.assertIn("team_season_eligibility", ELIGIBLE_PLAYER_SEASONS_SQL)

    def test_bundle_date_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative, checksum = _write_bundle_date(
                root, 2024, date(2024, 1, 2), [{"id": 1, "season": 2024}]
            )
            manifest = {
                "format_version": BUNDLE_FORMAT_VERSION,
                "release_version": "fixture.1",
                "first_season": 2024,
                "last_season": 2024,
                "seasons": {
                    "2024": {
                        "game_dates": ["2024-01-02"],
                        "dates": {
                            "2024-01-02": {
                                "file": relative,
                                "sha256": checksum,
                                "plays": 1,
                            }
                        },
                    }
                },
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            loaded = load_bundle_dates(root, 2024, 2024)
            self.assertEqual(loaded[2024][0][0], date(2024, 1, 2))
            self.assertEqual(loaded[2024][0][1][0]["id"], 1)

    def test_ticket_artifact_validates_event_and_status_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = _event()
            (root / "events.jsonl.gz").write_bytes(b"")
            import gzip

            with gzip.open(root / "events.jsonl.gz", "wt", encoding="utf-8") as output:
                output.write(json.dumps(event) + "\n")
            (root / "statuses.jsonl").write_text(
                json.dumps(
                    {
                        "player_id": 9001,
                        "season": 2024,
                        "status": "success",
                        "event_count": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "metadata.json").write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "first_season": 2024,
                        "last_season": 2026,
                        "expanded_player_seasons": 1,
                        "event_rows": 1,
                        "success_player_seasons": 1,
                        "no_data_player_seasons": 0,
                    }
                ),
                encoding="utf-8",
            )
            prepared = validate_artifact(root, _descriptor())
            self.assertEqual(prepared["counts"]["event_rows"], 1)
            self.assertEqual(prepared["statuses"][0]["status"], "success")

    def test_row_count_parser_rejects_missing_or_negative_values(self):
        self.assertEqual(_row_counts(["events=4"]), {"events": 4})
        with self.assertRaises(ValueError):
            _row_counts([])
        with self.assertRaises(ValueError):
            _row_counts(["events=-1"])


if __name__ == "__main__":
    unittest.main()
