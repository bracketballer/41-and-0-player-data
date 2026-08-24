"""Create and upload a ticket-numbered development data release."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from bracketballer_data.database import load_env_file
from bracketballer_data.ticket_release import (
    create_ticket_archive,
    sha256_file,
    upload_ticket_artifact,
    validate_handler,
    validate_release_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _checksums(values: list[str]) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--flyway-checksum must be VERSION=CHECKSUM")
        version, checksum = value.split("=", 1)
        if not version.isdigit() or not checksum:
            raise ValueError("--flyway-checksum must be VERSION=CHECKSUM")
        result[version] = int(checksum) if re.fullmatch(r"-?[0-9]+", checksum) else checksum
    if not result:
        raise ValueError("at least one --flyway-checksum is required")
    return result


def _row_counts(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--expected-row-count must be NAME=COUNT")
        name, count = value.split("=", 1)
        if not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            raise ValueError("--expected-row-count name is invalid")
        if not re.fullmatch(r"[0-9]+", count):
            raise ValueError("--expected-row-count count must be non-negative")
        result[name] = int(count)
    if not result:
        raise ValueError("at least one --expected-row-count is required")
    return result


def create(args: argparse.Namespace) -> dict:
    validate_handler(args.handler, args.ticket)
    validate_release_version(args.release_version)
    flyway_checksums = _checksums(args.flyway_checksum)
    row_counts = _row_counts(args.expected_row_count)
    if args.flyway_version not in flyway_checksums:
        raise ValueError("--flyway-version must have a matching --flyway-checksum")
    archive, checksum, manifest, publication = create_ticket_archive(
        source_dir=args.source_dir,
        output_dir=args.output_dir,
        ticket_number=args.ticket,
        release_version=args.release_version,
        dataset=args.dataset,
        object_root=args.object_root,
        pipeline_commit=args.pipeline_commit,
        flyway_version=args.flyway_version,
        flyway_checksums=flyway_checksums,
    )
    descriptor = {
        "format_version": 1,
        "ticket_number": args.ticket,
        "release_sequence": args.sequence,
        "handler": args.handler,
        "dataset": args.dataset,
        "release_version": args.release_version,
        "first_season": args.first_season,
        "last_season": args.last_season,
        "pipeline_commit": args.pipeline_commit,
        "objects": publication["objects"],
        "expected": {
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": sha256_file(archive),
            "checksum_sha256": sha256_file(checksum),
            "manifest_sha256": sha256_file(manifest),
            "row_counts": row_counts,
        },
        "required_flyway_checksums": flyway_checksums,
        "schema_dependency": {
            "repository": args.schema_repository,
            "ref": args.schema_ref,
            "commit": args.schema_commit,
            "flyway_version": args.flyway_version,
            "flyway_checksums": flyway_checksums,
        },
    }
    descriptor_path = args.descriptor or (
        REPO_ROOT / "releases" / "tickets" /
        f"issue-{args.ticket:04d}-{args.sequence:04d}-{args.release_version}.json"
    )
    descriptor_path.parent.mkdir(parents=True, exist_ok=True)
    if descriptor_path.exists():
        raise FileExistsError(f"refusing to overwrite descriptor: {descriptor_path}")
    descriptor_path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "archive": str(archive),
        "checksum": str(checksum),
        "manifest": str(manifest),
        "descriptor": str(descriptor_path),
        "descriptor_sha256": sha256_file(descriptor_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--source-dir", type=Path, required=True)
    create_parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "data" / "exports" / "ticket-releases")
    create_parser.add_argument("--descriptor", type=Path)
    create_parser.add_argument("--ticket", type=int, required=True)
    create_parser.add_argument("--sequence", type=int, required=True)
    create_parser.add_argument("--handler", required=True)
    create_parser.add_argument("--dataset", required=True)
    create_parser.add_argument("--release-version", required=True)
    create_parser.add_argument("--first-season", type=int, required=True)
    create_parser.add_argument("--last-season", type=int, required=True)
    create_parser.add_argument("--pipeline-commit", required=True)
    create_parser.add_argument("--schema-repository", required=True)
    create_parser.add_argument("--schema-ref", required=True)
    create_parser.add_argument("--schema-commit", required=True)
    create_parser.add_argument("--flyway-version", required=True)
    create_parser.add_argument("--flyway-checksum", action="append", default=[])
    create_parser.add_argument("--expected-row-count", action="append", default=[])
    create_parser.add_argument("--object-root", default="data-releases/tickets")

    upload_parser = subparsers.add_parser("upload")
    upload_parser.add_argument("--archive", type=Path, required=True)
    upload_parser.add_argument("--checksum", type=Path, required=True)
    upload_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    load_env_file()
    if args.command == "create":
        print(json.dumps(create(args), indent=2, sort_keys=True))
    else:
        print(json.dumps(upload_ticket_artifact(archive=args.archive, checksum=args.checksum, manifest=args.manifest), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
