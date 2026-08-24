from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bracketballer_data.ticket_release import (
    create_ticket_archive,
    safe_extract_ticket,
    sha256_file,
    upload_ticket_artifact,
    validate_handler,
)
from scripts.dev_sync.ticket_runner import (
    read_ticket_descriptor,
    ticket_descriptor_order,
    ticket_descriptor_paths,
)


def descriptor(path: Path, *, ticket: int = 7, sequence: int = 1, flyway: str = "35") -> None:
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "ticket_number": ticket,
                "release_sequence": sequence,
                "handler": f"scripts.dev_sync.tickets.issue_{ticket:04d}_example",
                "dataset": "fixture",
                "release_version": f"fixture-{ticket}-{sequence}",
                "first_season": 2024,
                "last_season": 2026,
                "pipeline_commit": "a" * 40,
                "objects": {
                    "archive": "data-releases/tickets/archive.tar.gz",
                    "checksum": "data-releases/tickets/archive.tar.gz.sha256",
                    "manifest": "data-releases/tickets/archive.json",
                },
                "expected": {
                    "archive_sha256": "a" * 64,
                    "checksum_sha256": "b" * 64,
                    "manifest_sha256": "c" * 64,
                },
                "required_flyway_checksums": {flyway: 1},
                "schema_dependency": {
                    "repository": "bracketballer/fastify",
                    "ref": "develop",
                    "commit": "b" * 40,
                    "flyway_version": flyway,
                    "flyway_checksums": {flyway: 1},
                },
            },
            sort_keys=True,
        )
    )


class TicketReleaseTests(unittest.TestCase):
    def test_handler_name_must_match_ticket(self):
        self.assertEqual(
            validate_handler("scripts.dev_sync.tickets.issue_0007_example", 7),
            "scripts.dev_sync.tickets.issue_0007_example",
        )
        with self.assertRaises(ValueError):
            validate_handler("scripts.dev_sync.tickets.issue_0008_example", 7)

    def test_archive_is_deterministic_and_safe_to_extract(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "source"
            source.mkdir()
            (source / "data.json").write_text('{"value": 1}\n')
            first = create_ticket_archive(
                source_dir=source,
                output_dir=root / "one",
                ticket_number=7,
                release_version="fixture.1",
                dataset="fixture",
            )
            second = create_ticket_archive(
                source_dir=source,
                output_dir=root / "two",
                ticket_number=7,
                release_version="fixture.1",
                dataset="fixture",
            )
            self.assertEqual(sha256_file(first[0]), sha256_file(second[0]))
            extracted = root / "extracted"
            files = safe_extract_ticket(first[0], extracted)
            self.assertEqual([path.name for path in files], ["data.json"])

    def test_upload_orders_manifest_last_and_resumes(self):
        class NoSuchKey(Exception):
            pass

        class Exceptions:
            pass

        Exceptions.NoSuchKey = NoSuchKey

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
            source.mkdir()
            (source / "data.json").write_text("{}\n")
            archive, checksum, manifest, _ = create_ticket_archive(
                source_dir=source,
                output_dir=root / "out",
                ticket_number=7,
                release_version="fixture.1",
                dataset="fixture",
            )
            client = Client()
            upload_ticket_artifact(
                archive=archive, checksum=checksum, manifest=manifest,
                client=client, bucket="bucket",
            )
            self.assertEqual(client.uploads[-1], json.loads(manifest.read_text())["objects"]["manifest"])

    def test_descriptors_are_sorted_by_schema_then_sequence(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            first = directory / "first.json"
            second = directory / "second.json"
            descriptor(first, ticket=7, sequence=2, flyway="36")
            descriptor(second, ticket=8, sequence=1, flyway="35")
            paths = ticket_descriptor_paths(directory)
            self.assertEqual(paths, [second, first])
            self.assertEqual(ticket_descriptor_order(read_ticket_descriptor(first)), (36, 2, 7))

    def test_duplicate_order_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            descriptor(directory / "first.json", ticket=7, sequence=1)
            descriptor(directory / "second.json", ticket=8, sequence=1)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                ticket_descriptor_paths(directory)


if __name__ == "__main__":
    unittest.main()
