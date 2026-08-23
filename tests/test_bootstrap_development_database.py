from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts import bootstrap_development_database as bootstrap
from bracketballer_data.development_snapshot import (
    MIGRATION_SEEDED_LABELS,
    SNAPSHOT_TABLES,
)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query):
        self.query = " ".join(query.split())

    def fetchone(self):
        if "to_regclass" in self.query:
            return ("flyway_schema_history",)
        if 'FROM public."' in self.query:
            table = self.query.split('FROM public."', 1)[1].split('"', 1)[0]
            return (self.connection.counts.get(table, 0),)
        raise AssertionError(f"unexpected fetchone query: {self.query}")

    def fetchall(self):
        if "FROM lineup_labels" in self.query:
            if not self.connection.valid_system_labels:
                return [("Developer label", "offense", False, "user-id")] * 7
            return [
                (title, category, True, None)
                for title, category in sorted(MIGRATION_SEEDED_LABELS)
            ]
        if "FROM flyway_schema_history" in self.query:
            return [
                (str(version), True)
                for version in range(1, self.connection.schema_version + 1)
            ]
        if "FROM pg_tables" in self.query:
            return [(table,) for table in sorted(self.connection.tables)]
        raise AssertionError(f"unexpected fetchall query: {self.query}")


class FakeConnection:
    def __init__(self, counts=None, *, valid_system_labels=True, schema_version=34):
        self.tables = set(SNAPSHOT_TABLES) | {
            "data_import_runs",
            "lineup_labels",
            "users",
        }
        self.counts = {table: 0 for table in self.tables}
        self.counts["lineup_labels"] = 7
        self.counts.update(counts or {})
        self.valid_system_labels = valid_system_labels
        self.schema_version = schema_version

    def cursor(self):
        return FakeCursor(self)


class BootstrapDevelopmentDatabaseTests(unittest.TestCase):
    def test_pristine_v34_database_allows_flyway_seed_labels(self):
        self.assertEqual(
            bootstrap.database_bootstrap_state(FakeConnection()),
            "pristine",
        )

    def test_existing_snapshot_data_is_not_replaced(self):
        self.assertEqual(
            bootstrap.database_bootstrap_state(FakeConnection({"schools": 44})),
            "initialized",
        )

    def test_existing_snapshot_at_v35_is_initialized(self):
        self.assertEqual(
            bootstrap.database_bootstrap_state(
                FakeConnection({"schools": 44}, schema_version=35)
            ),
            "initialized",
        )

    def test_empty_database_above_snapshot_baseline_is_not_restored(self):
        self.assertEqual(
            bootstrap.database_bootstrap_state(FakeConnection(schema_version=35)),
            "not-migrated",
        )

    def test_application_data_without_snapshot_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
            bootstrap.database_bootstrap_state(FakeConnection({"users": 1}))

    def test_non_system_label_in_pristine_target_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Flyway-seeded lineup labels"):
            bootstrap.database_bootstrap_state(
                FakeConnection(valid_system_labels=False)
            )

    def test_missing_credentials_is_an_actionable_skip(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            bootstrap, "load_env_file"
        ), patch("builtins.print") as output:
            self.assertEqual(bootstrap.main(), 10)
        self.assertIn("configure DATABASE_URL", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
