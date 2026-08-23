"""Create, upload, or share an immutable development database snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bracketballer_data.database import load_env_file
from bracketballer_data.development_snapshot import (
    DEFAULT_URL_EXPIRY_SECONDS,
    create_snapshot,
    presigned_urls,
    upload_snapshot,
)
from bracketballer_data.import_audit import pipeline_commit
from bracketballer_data.paths import DATA_ROOT, REPO_ROOT


def _archive_for(output_dir: Path, release_version: str) -> Path:
    return output_dir / f"bracketballer-development-data-v34-{release_version}.dump"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="create a local snapshot")
    create_parser.add_argument("--release-version", required=True)
    create_parser.add_argument(
        "--output-dir", type=Path, default=DATA_ROOT / "exports" / "development"
    )

    upload_parser = subparsers.add_parser("upload", help="upload a local snapshot to Spaces")
    upload_parser.add_argument("--archive", type=Path, required=True)
    upload_parser.add_argument("--manifest", type=Path, required=True)

    share_parser = subparsers.add_parser("share", help="create short-lived download URLs")
    share_parser.add_argument("--manifest", type=Path, required=True)
    share_parser.add_argument("--expires-in", type=int, default=DEFAULT_URL_EXPIRY_SECONDS)

    args = parser.parse_args()
    load_env_file()

    if args.command == "create":
        archive = _archive_for(args.output_dir, args.release_version)
        result = create_snapshot(
            archive=archive,
            release_version=args.release_version,
            pipeline_commit=pipeline_commit(REPO_ROOT),
        )
        print(
            json.dumps(
                {
                    "archive": str(result[0]),
                    "checksum": str(result[1]),
                    "manifest": str(result[2]),
                },
                indent=2,
            )
        )
    elif args.command == "upload":
        checksum = args.archive.with_name(f"{args.archive.name}.sha256")
        if not checksum.is_file():
            parser.error(f"missing checksum sidecar: {checksum}")
        print(json.dumps(upload_snapshot(args.archive, checksum, args.manifest), indent=2))
    else:
        print(json.dumps(presigned_urls(args.manifest, args.expires_in), indent=2))


if __name__ == "__main__":
    main()
