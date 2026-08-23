"""Create and upload the immutable ranked-roster database delta.

Examples::

    python -m scripts.publish.publish_ranked_roster_delta create \
      --candidate-dir data/raw/ranked_rosters/2026/ranked-rosters-2026-08-23.1 \
      --season 2026 --release-version ranked-rosters-2026-08-23.1 \
      --flyway-v34-checksum 34=-1973679461

    python -m scripts.publish.publish_ranked_roster_delta upload \
      --archive ...tar.gz --checksum ...tar.gz.sha256 \
      --manifest ...json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ImportError:  # Loaded lazily only when checksums come from a DB.
    psycopg2 = None  # type: ignore[assignment]

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.import_audit import pipeline_commit
from bracketballer_data.ranked_roster_delta import (
    DELTA_FORMAT_VERSION,
    create_delta_archive,
    sha256_file,
    upload_delta,
)
from bracketballer_data.paths import DATA_ROOT, REPO_ROOT


def _parse_flyway_checksum(values: list[str]) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    for value in values:
        if "=" not in value:
            # A single value is unambiguous for this release and is accepted
            # as a convenience shorthand for V34=<value>.
            version, checksum = "34", value
        else:
            version, checksum = value.split("=", 1)
        if not version or not checksum:
            raise ValueError("--flyway-v34-checksum must be VERSION=CHECKSUM")
        try:
            result[version] = int(checksum)
        except ValueError:
            result[version] = checksum
    return result


def flyway_v34_checksums(dsn: str | None = None) -> dict[str, int | str]:
    """Read the exact V34 signature from the database used to publish."""

    global psycopg2
    if psycopg2 is None:
        try:
            import psycopg2 as driver
        except ImportError as error:
            raise RuntimeError("psycopg2 is required to read Flyway V34 checksums") from error
        psycopg2 = driver
    conn = psycopg2.connect(dsn or connection_dsn())
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT version, checksum, success
                FROM flyway_schema_history
                WHERE version = '34'
                ORDER BY installed_rank
                """
            )
            rows = cursor.fetchall()
    finally:
        conn.close()
    if len(rows) != 1 or not rows[0][2]:
        raise RuntimeError("publishing requires one successful Flyway V34 migration")
    return {"34": rows[0][1]}


def _create(args: argparse.Namespace) -> dict[str, Any]:
    checksums = _parse_flyway_checksum(args.flyway_v34_checksum)
    if not checksums:
        checksums = flyway_v34_checksums(args.database_url)
    commit = args.pipeline_commit or pipeline_commit(REPO_ROOT)
    result = create_delta_archive(
        candidate_dir=args.candidate_dir,
        output_dir=args.output_dir,
        season=args.season,
        release_version=args.release_version,
        pipeline_commit=commit,
        flyway_v34_checksums=checksums,
        object_root=args.object_root,
    )
    archive, checksum, manifest, metadata = result
    return {
        "archive": str(archive),
        "checksum": str(checksum),
        "manifest": str(manifest),
        "archive_sha256": sha256_file(archive),
        "publication": metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a deterministic local delta")
    create.add_argument("--candidate-dir", "--source-dir", "--candidate", type=Path, required=True)
    create.add_argument("--season", type=int, required=True)
    create.add_argument("--release-version", required=True)
    create.add_argument(
        "--output-dir", type=Path,
        default=DATA_ROOT / "exports" / "data-releases",
    )
    create.add_argument("--pipeline-commit")
    create.add_argument("--database-url")
    create.add_argument(
        "--flyway-v34-checksum", "--flyway-checksum", action="append", default=[],
        help="Migration signature as VERSION=CHECKSUM; may be repeated",
    )
    create.add_argument("--object-root", default="data-releases/v1")

    upload = subparsers.add_parser("upload", help="upload a local delta to Spaces")
    upload.add_argument("--archive", type=Path, required=True)
    upload.add_argument("--checksum", type=Path)
    upload.add_argument("--manifest", type=Path, required=True)

    args = parser.parse_args()
    load_env_file()
    try:
        if args.command == "create":
            print(json.dumps(_create(args), indent=2, sort_keys=True))
            return
        checksum = args.checksum or args.archive.with_name(f"{args.archive.name}.sha256")
        if not checksum.is_file():
            parser.error(f"missing checksum sidecar: {checksum}")
        print(json.dumps(upload_delta(archive=args.archive, checksum=checksum, manifest_path=args.manifest), indent=2, sort_keys=True))
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
