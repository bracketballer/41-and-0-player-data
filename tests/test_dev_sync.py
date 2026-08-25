from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.dev_sync import sync_after_git_update as sync
from scripts.dev_sync.ticket_runner import read_ticket_descriptor


class DevelopmentSyncTests(unittest.TestCase):
    def test_descriptor_pins_fastify_revision_and_flyway_checksums(self):
        dependencies = sync._schema_dependencies()
        dependency = next(
            item
            for item in dependencies
            if item["descriptor"] == "ranked-rosters-2026-08-23.2.json"
        )
        self.assertEqual(dependency["repository"], "bracketballer/fastify")
        self.assertEqual(dependency["ref"], "develop")
        self.assertEqual(len(dependency["commit"]), 40)
        self.assertEqual(dependency["flyway_checksums"]["35"], 1162551216)

    def test_issue_0010_descriptor_pins_v36_release_order_and_sources(self):
        dependencies = sync._schema_dependencies()
        dependency = next(
            item
            for item in dependencies
            if item["descriptor"] == "issue-0010-0002-issue-0010-defense-concession-2026.1.json"
        )
        self.assertEqual(dependency["flyway_version"], "36")
        ticket = read_ticket_descriptor(
            Path("releases/tickets/issue-0010-0002-issue-0010-defense-concession-2026.1.json")
        )
        self.assertEqual(ticket["release_sequence"], 2)
        self.assertEqual(
            dependency["commit"],
            "08d37935d76757338db86624095fef4421f12b12",
        )

    def test_flyway_environment_supports_url_and_keyword_dsn(self):
        url = sync._flyway_environment(
            "postgresql://postgres:test@localhost:5433/bracketballer_dev?sslmode=disable"
        )
        self.assertEqual(
            url["FLYWAY_URL"],
            "jdbc:postgresql://localhost:5433/bracketballer_dev?sslmode=disable",
        )
        keyword = sync._flyway_environment(
            "host=127.0.0.1 port=5432 dbname=bracketballer_dev user=postgres password=test"
        )
        self.assertEqual(keyword["FLYWAY_USER"], "postgres")
        self.assertEqual(keyword["FLYWAY_PASSWORD"], "test")

    def test_flyway_environment_rejects_unusable_socket_dsn(self):
        with self.assertRaisesRegex(RuntimeError, "TCP"):
            sync._flyway_environment(
                "host=/var/run/postgresql dbname=bracketballer_dev user=postgres"
            )

    def test_remote_database_is_skipped_before_schema_or_data_work(self):
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://postgres:test@db.example/bracketballer_dev",
                "DO_SPACES_BUCKET": "bucket",
                "DO_SPACES_ACCESS_KEY_ID": "key",
                "DO_SPACES_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ), patch.object(sync, "load_env_file"), patch.object(
            sync, "synchronize_schema"
        ) as schema, patch.object(sync, "apply_pending") as apply:
            result = sync.synchronize()
        self.assertIn("remote", result)
        schema.assert_not_called()
        apply.assert_not_called()

    def test_invalid_dependency_commit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid Fastify commit"):
            sync._validate_dependency(
                {
                    "descriptor": "fixture.json",
                    "ref": "develop",
                    "commit": "not-a-commit",
                }
            )

    def test_schema_and_data_are_ordered(self):
        calls: list[str] = []
        dependency = {
            "descriptor": "fixture.json",
            "repository": "bracketballer/fastify",
            "ref": "develop",
            "commit": "a" * 40,
            "flyway_version": "35",
            "flyway_checksums": {"34": -1, "35": 2},
        }
        with patch.object(sync, "_ensure_fastify_revision", return_value="a" * 40), patch.object(
            sync, "_fastify_worktree"
        ) as worktree, patch.object(sync, "_run_flyway") as migrate, patch.object(
            sync, "_verify_flyway"
        ) as verify:
            worktree.return_value.__enter__.return_value = Path(tempfile.gettempdir())
            worktree.return_value.__exit__.return_value = False
            migrate.side_effect = lambda *args, **kwargs: calls.append("migrate")
            verify.side_effect = lambda *args, **kwargs: calls.append("verify")
            with patch.object(sync, "_fastify_path", return_value=Path(tempfile.gettempdir())):
                sync.synchronize_schema("postgresql://postgres:test@localhost/db", [dependency])
        self.assertEqual(calls, ["migrate", "verify"])

    def test_sync_lock_blocks_concurrent_acquisition_and_releases_after_use(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "lock"
            with sync._sync_lock(lock_path) as first_acquired:
                self.assertTrue(first_acquired)
                with sync._sync_lock(lock_path) as second_acquired:
                    self.assertFalse(second_acquired)
            with sync._sync_lock(lock_path) as reacquired:
                self.assertTrue(reacquired)

    def test_descriptor_rejects_non_hex_schema_commit(self):
        descriptor_path = Path("releases/ranked-rosters-2026-08-23.2.json")
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["schema_dependency"]["commit"] = "z" * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / descriptor_path.name
            path.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(ValueError, "schema commit"):
                sync._schema_dependencies_from_paths([path])


if __name__ == "__main__":
    unittest.main()
