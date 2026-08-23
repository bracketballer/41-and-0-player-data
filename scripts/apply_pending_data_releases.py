"""Apply checked-in ranked-roster delta descriptors to local development DBs.

The command intentionally reads only JSON descriptors committed under
``releases/``.  It never lists a Spaces prefix or selects a newest object.  A
descriptor is downloaded and verified completely before a database connection
used for writes is opened.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import psycopg2
except ImportError:  # Loaded lazily when a release has passed artifact checks.
    psycopg2 = None  # type: ignore[assignment]

from bracketballer_data.database import (
    connection_dsn,
    database_host,
    is_local_database,
    load_env_file,
)
from bracketballer_data.ranked_roster_delta import (
    DATASET,
    download_delta,
    safe_extract_delta,
    sha256_file,
    validate_release_version,
)

EXPECTED_FLYWAY_VERSION = "34"
DEFAULT_RELEASES_DIR = Path(__file__).resolve().parents[1] / "releases"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache" / "data-releases"
LOCK_PATH = Path(__file__).resolve().parents[1] / ".data-release.lock"


def read_descriptor(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read release descriptor {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"release descriptor must be a JSON object: {path}")
    if value.get("dataset") != DATASET:
        raise ValueError(f"release descriptor has unsupported dataset: {path}")
    if not isinstance(value.get("release_version"), str) or not isinstance(value.get("season"), int):
        raise ValueError(f"release descriptor is missing season or release_version: {path}")
    validate_release_version(value["release_version"])
    if value["season"] < 2024:
        raise ValueError(f"release descriptor season is unsupported: {path}")
    if value.get("pipeline_commit") is not None and (
        not isinstance(value["pipeline_commit"], str)
        or not re.fullmatch(r"[0-9a-f]{7,64}", value["pipeline_commit"].lower())
    ):
        raise ValueError(f"release descriptor pipeline commit is invalid: {path}")
    if isinstance(value.get("pipeline_commit"), str):
        value["pipeline_commit"] = value["pipeline_commit"].lower()
    objects = value.get("objects") or value.get("remote_keys")
    if not isinstance(objects, dict) or any(not isinstance(objects.get(key), str) for key in ("archive", "checksum", "manifest")):
        raise ValueError(f"release descriptor object keys are incomplete: {path}")
    if any(
        str(objects[key]).startswith("/")
        or ".." in Path(str(objects[key])).parts
        or "\x00" in str(objects[key])
        for key in ("archive", "checksum", "manifest")
    ):
        raise ValueError(f"release descriptor object keys are unsafe: {path}")
    expected = value.get("expected")
    if expected is None:
        expected = {
            "archive_sha256": value.get("archive_sha256"),
            "checksum_sha256": value.get("checksum_sha256"),
            "manifest_sha256": value.get("manifest_sha256"),
            "candidate_sha256": value.get("candidate_sha256"),
            "archive_bytes": value.get("archive_bytes"),
        }
    if not isinstance(expected, dict):
        raise ValueError(f"release descriptor expected checksums are missing: {path}")
    for key in ("archive_sha256", "candidate_sha256", "checksum_sha256", "manifest_sha256"):
        if not isinstance(expected.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", expected[key]):
            raise ValueError(f"release descriptor expected {key} is invalid: {path}")
    flyway = value.get("flyway_v34_checksums") or value.get("required_flyway_v34_checksums")
    if not isinstance(flyway, dict) or "34" not in flyway:
        raise ValueError(f"release descriptor Flyway V34 signature is missing: {path}")
    normalized_flyway = {str(key): value for key, value in flyway.items()}
    schema_dependency = value.get("schema_dependency")
    if schema_dependency is not None:
        if not isinstance(schema_dependency, dict):
            raise ValueError(f"release descriptor schema dependency is invalid: {path}")
        repository = schema_dependency.get("repository")
        ref = schema_dependency.get("ref")
        commit = schema_dependency.get("commit")
        flyway_version = schema_dependency.get("flyway_version")
        flyway_checksums = schema_dependency.get("flyway_checksums")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError(f"release descriptor schema repository is missing: {path}")
        if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
            raise ValueError(f"release descriptor schema ref is invalid: {path}")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit.lower()):
            raise ValueError(f"release descriptor schema commit is invalid: {path}")
        if not isinstance(flyway_version, str) or not flyway_version.isdigit():
            raise ValueError(f"release descriptor schema Flyway version is invalid: {path}")
        if not isinstance(flyway_checksums, dict) or "34" not in flyway_checksums:
            raise ValueError(f"release descriptor schema Flyway checksums are incomplete: {path}")
        normalized_schema_checksums = {
            str(key): value for key, value in flyway_checksums.items()
        }
        if flyway_version not in normalized_schema_checksums:
            raise ValueError(
                f"release descriptor schema Flyway version {flyway_version} checksum is missing: {path}"
            )
        for version, checksum in normalized_schema_checksums.items():
            if not version.isdigit() or not isinstance(checksum, (int, str)):
                raise ValueError(f"release descriptor schema Flyway checksum is invalid: {path}")
        value["schema_dependency"] = {
            "repository": repository.strip(),
            "ref": ref.strip(),
            "commit": commit.lower(),
            "flyway_version": flyway_version,
            "flyway_checksums": normalized_schema_checksums,
        }
        normalized_flyway = normalized_schema_checksums
    value["objects"] = objects
    value["expected"] = expected
    value["flyway_v34_checksums"] = {"34": normalized_flyway["34"]}
    value["required_flyway_checksums"] = normalized_flyway
    return value


def descriptor_paths(directory: Path = DEFAULT_RELEASES_DIR) -> list[Path]:
    """Return only checked-in descriptor files (never remote object listings)."""

    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def require_local_database(dsn: str, *, allow_remote: bool = False) -> None:
    if not allow_remote and not is_local_database(dsn):
        raise RuntimeError(
            f"refusing automatic data release against remote database host {database_host(dsn)!r}; "
            "run the manual command with --allow-remote"
        )


def flyway_signature(conn: Any, versions: set[str]) -> dict[str, int | str]:
    ordered_versions = sorted(versions, key=lambda version: int(version))
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT version, checksum, success
            FROM flyway_schema_history
            WHERE version = ANY(%s)
            ORDER BY installed_rank
            """,
            (ordered_versions,),
        )
        rows = cursor.fetchall()
    return {
        str(row[0]): row[1]
        for row in rows
        if len(row) >= 3 and row[2]
    }


def flyway_v34_signature(conn: Any) -> dict[str, int | str]:
    return flyway_signature(conn, {EXPECTED_FLYWAY_VERSION})


def require_flyway_checksums(conn: Any, expected: dict[str, Any]) -> None:
    normalized_expected = {str(key): value for key, value in expected.items()}
    actual = flyway_signature(conn, set(normalized_expected))
    if actual != normalized_expected:
        versions = ", ".join(sorted(normalized_expected, key=lambda version: int(version)))
        raise RuntimeError(
            f"Flyway migration checksum mismatch for V{versions}: "
            f"expected={normalized_expected}, actual={actual}"
        )


def require_flyway_v34(conn: Any, expected: dict[str, Any]) -> None:
    require_flyway_checksums(conn, {EXPECTED_FLYWAY_VERSION: expected[EXPECTED_FLYWAY_VERSION]})


def require_eligible_schools(conn: Any, candidate: dict[str, Any]) -> None:
    eligible = sorted({row.team_id for row in candidate["eligible"]})
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM schools WHERE id = ANY(%s)", (eligible,))
        known = {int(row[0]) for row in cursor.fetchall()}
    missing = sorted(set(eligible) - known)
    if missing:
        raise RuntimeError(f"eligible canonical schools are missing: {missing}")


def audit_state(conn: Any, descriptor: dict[str, Any], candidate_sha256: str) -> str:
    """Check the immutable audit row without deleting or repairing history."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, source_sha256
            FROM data_import_runs
            WHERE dataset = %s AND import_version = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (descriptor["dataset"], descriptor["release_version"]),
        )
        row = cursor.fetchone()
    if row is None:
        return "absent"
    status, source_sha256 = row
    if status == "published" and source_sha256 == candidate_sha256:
        return "published"
    if status == "published":
        raise RuntimeError(
            "release audit checksum conflict: published row has a different source checksum"
        )
    raise RuntimeError(
        f"release audit row is {status}; refusing to delete or overwrite its history"
    )


@contextmanager
def release_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _spaces_configured() -> bool:
    return all(
        os.environ.get(name, "").strip()
        for name in (
            "DO_SPACES_BUCKET",
            "DO_SPACES_ACCESS_KEY_ID",
            "DO_SPACES_SECRET_ACCESS_KEY",
        )
    )


def apply_descriptor(
    descriptor_path: Path,
    *,
    dsn: str | None = None,
    allow_remote: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    spaces_client: Any | None = None,
    ingestion_runner: Any | None = None,
) -> str:
    """Download, verify, and apply one checked-in descriptor."""

    descriptor = read_descriptor(descriptor_path)
    database_dsn = dsn or connection_dsn()
    require_local_database(database_dsn, allow_remote=allow_remote)
    if not _spaces_configured() and spaces_client is None:
        raise RuntimeError(
            "missing Spaces credentials; manually run the delta publisher/apply command "
            "after configuring DO_SPACES_BUCKET, DO_SPACES_ACCESS_KEY_ID, and DO_SPACES_SECRET_ACCESS_KEY"
        )
    release_cache = cache_dir / str(descriptor["season"]) / descriptor["release_version"]
    release_cache.mkdir(parents=True, exist_ok=True)
    # Downloading and validating all bytes happens before opening a database
    # connection for either reads or writes.
    archive, checksum, publication = download_delta(
        descriptor, release_cache, client=spaces_client
    )
    if sha256_file(archive) != descriptor["expected"]["archive_sha256"]:
        raise ValueError("downloaded delta archive does not match release descriptor checksum")
    if descriptor["expected"].get("archive_bytes") is not None and archive.stat().st_size != int(descriptor["expected"]["archive_bytes"]):
        raise ValueError("downloaded delta archive size does not match release descriptor")
    if sha256_file(checksum) != descriptor["expected"]["checksum_sha256"]:
        raise ValueError("downloaded delta checksum sidecar does not match release descriptor checksum")
    publication_path = release_cache / "publication-manifest.json"
    if sha256_file(publication_path) != descriptor["expected"]["manifest_sha256"]:
        raise ValueError("downloaded delta publication manifest does not match descriptor checksum")

    with tempfile.TemporaryDirectory(prefix="ranked-roster-delta-", dir=release_cache) as temporary:
        candidate_path, source_manifest_path = safe_extract_delta(archive, Path(temporary))
        from scripts.ingest.ingest_ranked_rosters import (
            candidate_checksum,
            load_candidate_bundle,
            validate_candidate,
        )
        # The source manifest is checked against the candidate before any DB
        # connection.  load_candidate_bundle also verifies the candidate's
        # derived eligibility checksum and AP coverage.
        candidate = load_candidate_bundle(Path(temporary), descriptor["season"], descriptor["release_version"])
        candidate_sha256 = candidate_checksum(candidate)
        if candidate_sha256 != descriptor["expected"]["candidate_sha256"]:
            raise ValueError("downloaded candidate checksum does not match release descriptor")
        validation = validate_candidate(candidate)
        declared_counts = descriptor.get("row_counts")
        if isinstance(declared_counts, dict):
            actual_counts = {
                "eligible_teams": validation["eligible_teams"],
                "roster_memberships": validation["roster_players"],
                "player_seasons": validation["player_seasons"],
                "games": validation["college_games"],
                "lineups": validation["team_game_lineups"],
                "opponent_contexts": validation["opponent_contexts"],
            }
            if "all_roster_memberships" in declared_counts:
                actual_counts["all_roster_memberships"] = validation[
                    "full_roster_players"
                ]
            if {key: int(value) for key, value in declared_counts.items()} != actual_counts:
                raise ValueError(
                    f"candidate row counts do not match descriptor: expected={declared_counts}, actual={actual_counts}"
                )

        global psycopg2
        if psycopg2 is None:
            try:
                import psycopg2 as driver
            except ImportError as error:
                raise RuntimeError("psycopg2 is required to apply a data release") from error
            psycopg2 = driver
        conn = psycopg2.connect(database_dsn)
        conn.autocommit = False
        try:
            require_flyway_checksums(conn, descriptor["required_flyway_checksums"])
            require_eligible_schools(conn, candidate)
            state = audit_state(conn, descriptor, candidate_sha256)
            conn.rollback()
        finally:
            conn.close()
        if state == "published":
            return "skipped: already published with expected checksum"

        # Keep the established audited publisher as the single write path.  It
        # runs in a subprocess so its transaction owns both the audit lifecycle
        # and source-owned season replacement.
        runner = ingestion_runner or subprocess.run
        command = [
            sys.executable,
            "-m",
            "scripts.ingest.ingest_ranked_rosters",
            "--season",
            str(descriptor["season"]),
            "--release-version",
            descriptor["release_version"],
            "--source-dir",
            str(temporary),
            "--apply",
        ]
        child_env = os.environ.copy()
        if dsn:
            # The ingestion CLI reads its normal environment-based DSN. Keep a
            # manually supplied target scoped to this child process rather
            # than mutating the caller's environment.
            child_env["DATABASE_URL"] = database_dsn
        result = runner(command, check=True, text=True, env=child_env)
        return "published"


def apply_pending(
    *,
    releases_dir: Path = DEFAULT_RELEASES_DIR,
    selected_release: str | None = None,
    dsn: str | None = None,
    allow_remote: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[str]:
    paths = descriptor_paths(releases_dir)
    if selected_release:
        paths = [
            path for path in paths
            if path.stem == selected_release or path.name == selected_release
        ]
        if not paths:
            raise FileNotFoundError(f"no checked-in release descriptor named {selected_release}")
    results: list[str] = []
    for path in paths:
        results.append(f"{path.name}: {apply_descriptor(path, dsn=dsn, allow_remote=allow_remote, cache_dir=cache_dir)}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", help="one checked-in descriptor release version")
    parser.add_argument("--releases-dir", type=Path, default=DEFAULT_RELEASES_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--database-url")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="explicitly allow a manually requested remote database target",
    )
    args = parser.parse_args()
    load_env_file()
    with release_lock() as acquired:
        if not acquired:
            print("data release runner: another invocation is active; skipping")
            return 0
        for result in apply_pending(
            releases_dir=args.releases_dir,
            selected_release=args.release,
            dsn=args.database_url,
            allow_remote=args.allow_remote,
            cache_dir=args.cache_dir,
        ):
            print(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"data release runner: {error}", file=sys.stderr)
        raise SystemExit(1) from error
