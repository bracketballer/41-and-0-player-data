"""Apply checked-in ticketed schema-plus-data releases.

This runner is intentionally separate from the legacy ranked-roster release
path.  Ticket handlers are imported only after a descriptor and its immutable
artifact have passed validation, and all writes occur inside the handler's
single database transaction.
"""

from __future__ import annotations

import importlib
import json
import re
import argparse
from pathlib import Path
from typing import Any, Callable

from bracketballer_data.database import database_host, is_local_database
from bracketballer_data.import_audit import begin_import_run, mark_failed
from bracketballer_data.ticket_release import (
    download_ticket_artifact,
    safe_extract_ticket,
    validate_handler,
    validate_release_version,
    verify_ticket_artifact,
)
from scripts.apply_pending_data_releases import (
    DEFAULT_CACHE_DIR,
    _spaces_configured,
    require_flyway_checksums,
)

TICKET_DESCRIPTOR_FORMAT_VERSION = 1
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def read_ticket_descriptor(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read ticket descriptor {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"ticket descriptor must be an object: {path}")
    if value.get("format_version") != TICKET_DESCRIPTOR_FORMAT_VERSION:
        raise ValueError(f"unsupported ticket descriptor format: {path}")
    ticket = value.get("ticket_number")
    sequence = value.get("release_sequence")
    if not isinstance(ticket, int) or isinstance(ticket, bool) or ticket <= 0:
        raise ValueError(f"ticket number is invalid: {path}")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise ValueError(f"release sequence is invalid: {path}")
    validate_handler(value.get("handler"), ticket)
    if not isinstance(value.get("dataset"), str) or not value["dataset"].strip():
        raise ValueError(f"ticket dataset is missing: {path}")
    validate_release_version(value.get("release_version"))
    commit = value.get("pipeline_commit")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit.lower()):
        raise ValueError(f"ticket pipeline commit is invalid: {path}")
    first = value.get("first_season")
    last = value.get("last_season")
    if not isinstance(first, int) or not isinstance(last, int) or not 1900 <= first <= last <= 2100:
        raise ValueError(f"ticket season range is invalid: {path}")
    objects = value.get("objects")
    if not isinstance(objects, dict) or any(not isinstance(objects.get(name), str) for name in ("archive", "checksum", "manifest")):
        raise ValueError(f"ticket object keys are incomplete: {path}")
    for name in ("archive", "checksum", "manifest"):
        parts = Path(str(objects[name])).parts
        if Path(str(objects[name])).is_absolute() or ".." in parts or "\x00" in str(objects[name]):
            raise ValueError(f"ticket object key is unsafe: {path}")
    expected = value.get("expected")
    if not isinstance(expected, dict):
        raise ValueError(f"ticket expected checksums are missing: {path}")
    for key in ("archive_sha256", "checksum_sha256", "manifest_sha256"):
        if not isinstance(expected.get(key), str) or not _SHA_RE.fullmatch(expected[key].lower()):
            raise ValueError(f"ticket expected {key} is invalid: {path}")
    required = value.get("required_flyway_checksums")
    dependency = value.get("schema_dependency")
    if not isinstance(required, dict) or not required:
        raise ValueError(f"ticket Flyway checksums are missing: {path}")
    if not isinstance(dependency, dict):
        raise ValueError(f"ticket schema dependency is missing: {path}")
    version = dependency.get("flyway_version")
    if not isinstance(version, str) or not version.isdigit() or version not in {str(k) for k in required}:
        raise ValueError(f"ticket schema dependency Flyway version is invalid: {path}")
    dependency_commit = dependency.get("commit")
    if not isinstance(dependency_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", dependency_commit.lower()):
        raise ValueError(f"ticket schema dependency commit is invalid: {path}")
    value["ticket_number"] = ticket
    value["release_sequence"] = sequence
    value["handler"] = validate_handler(value["handler"], ticket)
    value["pipeline_commit"] = commit.lower()
    value["required_flyway_checksums"] = {str(k): v for k, v in required.items()}
    value["schema_dependency"] = {**dependency, "commit": dependency_commit.lower(), "flyway_version": version}
    value["_path"] = str(path)
    return value


def ticket_descriptor_order(descriptor: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(descriptor["schema_dependency"]["flyway_version"]),
        int(descriptor["release_sequence"]),
        int(descriptor["ticket_number"]),
    )


def ticket_descriptor_paths(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = sorted(path for path in directory.glob("*.json") if path.is_file())
    descriptors = [read_ticket_descriptor(path) for path in paths]
    seen: set[tuple[int, int]] = set()
    for descriptor in descriptors:
        key = (descriptor["schema_dependency"]["flyway_version"], descriptor["release_sequence"])
        if key in seen:
            raise ValueError(f"duplicate ticket release order: {key}")
        seen.add(key)
    return [path for _order, path in sorted(zip(map(ticket_descriptor_order, descriptors), paths), key=lambda item: item[0])]


def _load_handler(descriptor: dict[str, Any]) -> Any:
    module = importlib.import_module(descriptor["handler"])
    if not callable(getattr(module, "validate_artifact", None)) or not callable(getattr(module, "apply", None)):
        raise TypeError(f"ticket handler {descriptor['handler']} must expose validate_artifact and apply")
    return module


def _audit_state(conn: Any, descriptor: dict[str, Any], source_sha256: str) -> str:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, source_sha256
            FROM data_import_runs
            WHERE dataset = %s AND import_version = %s
            ORDER BY id DESC LIMIT 1
            """,
            (descriptor["dataset"], descriptor["release_version"]),
        )
        row = cursor.fetchone()
    if row is None:
        return "absent"
    status, digest = row
    if status == "published" and digest == source_sha256:
        return "published"
    if status == "published":
        raise RuntimeError("ticket release audit checksum conflict")
    raise RuntimeError(f"ticket release audit row is {status}; use a new release version")


def _publish_audit(conn: Any, run_id: int, source_sha256: str, row_counts: dict[str, Any], validation: dict[str, Any]) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE data_import_runs
            SET status = 'published', source_sha256 = %s,
                staged_row_counts = %s::jsonb,
                published_row_counts = %s::jsonb,
                validation_results = %s::jsonb,
                validated_at = now(), published_at = now(), finished_at = now()
            WHERE id = %s AND status = 'running'
            """,
            (source_sha256, json.dumps(row_counts, sort_keys=True), json.dumps(row_counts, sort_keys=True), json.dumps(validation, sort_keys=True), run_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"ticket import run {run_id} is not running")


def apply_ticket_descriptor(
    descriptor_path: Path,
    *,
    dsn: str,
    cache_dir: Path = DEFAULT_CACHE_DIR / "tickets",
    spaces_client: Any | None = None,
    allow_remote: bool = False,
) -> str:
    descriptor = read_ticket_descriptor(descriptor_path)
    if not allow_remote and not is_local_database(dsn):
        raise RuntimeError(f"automatic ticket releases refuse remote database host {database_host(dsn)!r}")
    import psycopg2
    preflight = psycopg2.connect(dsn)
    try:
        require_flyway_checksums(preflight, descriptor["required_flyway_checksums"])
        state = _audit_state(preflight, descriptor, descriptor["expected"]["archive_sha256"])
        preflight.rollback()
    finally:
        preflight.close()
    if state == "published":
        return "skipped: already published with expected checksum"
    if spaces_client is None and not _spaces_configured():
        raise RuntimeError("DigitalOcean Spaces credentials are required for a pending ticket release")
    release_cache = cache_dir / str(descriptor["ticket_number"]) / descriptor["release_version"]
    archive, checksum, manifest = download_ticket_artifact(descriptor, release_cache, client=spaces_client)
    source_sha256 = verify_ticket_artifact(descriptor, archive, checksum, manifest)
    with __import__("tempfile").TemporaryDirectory(prefix="ticket-release-", dir=release_cache) as temporary:
        extracted = Path(temporary) / "artifact"
        safe_extract_ticket(archive, extracted)
        handler = _load_handler(descriptor)
        prepared = handler.validate_artifact(extracted, descriptor)
        if prepared is None:
            prepared = {}
        if not isinstance(prepared, dict):
            raise TypeError("ticket validate_artifact must return a dictionary")
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        run_id: int | None = None
        try:
            require_flyway_checksums(conn, descriptor["required_flyway_checksums"])
            state = _audit_state(conn, descriptor, source_sha256)
            conn.rollback()
            if state == "published":
                return "skipped: already published with expected checksum"
            run_id = begin_import_run(
                conn,
                dataset=descriptor["dataset"],
                import_version=descriptor["release_version"],
                commit=descriptor["pipeline_commit"],
                source_uri=str(descriptor["objects"]["archive"]),
                first_season=descriptor["first_season"],
                last_season=descriptor["last_season"],
                metadata={"ticket_number": descriptor["ticket_number"], "release_sequence": descriptor["release_sequence"]},
            )
            result = handler.apply(conn, extracted, descriptor, prepared)
            if result is None:
                result = {}
            if not isinstance(result, dict):
                raise TypeError("ticket apply must return a dictionary")
            row_counts = result.get("row_counts", {})
            validation = {"prepared": prepared, **result.get("validation", {})}
            _publish_audit(conn, run_id, source_sha256, row_counts, validation)
            conn.commit()
            return "published"
        except Exception as error:
            if run_id is not None:
                mark_failed(conn, run_id, error)
            else:
                conn.rollback()
            raise
        finally:
            conn.close()


def apply_pending_ticket_releases(
    *,
    releases_dir: Path,
    dsn: str,
    cache_dir: Path = DEFAULT_CACHE_DIR / "tickets",
    spaces_client: Any | None = None,
    allow_remote: bool = False,
) -> list[str]:
    results: list[str] = []
    for path in ticket_descriptor_paths(releases_dir):
        result = apply_ticket_descriptor(
            path,
            dsn=dsn,
            cache_dir=cache_dir,
            spaces_client=spaces_client,
            allow_remote=allow_remote,
        )
        results.append(f"{path.name}: {result}")
    return results


__all__ = [
    "apply_pending_ticket_releases",
    "apply_ticket_descriptor",
    "read_ticket_descriptor",
    "ticket_descriptor_order",
    "ticket_descriptor_paths",
]


def main() -> int:
    from bracketballer_data.database import connection_dsn, load_env_file

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--releases-dir", type=Path, default=Path("releases/tickets"))
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR / "tickets")
    parser.add_argument("--database-url")
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()
    load_env_file()
    results = apply_pending_ticket_releases(
        releases_dir=args.releases_dir,
        dsn=args.database_url or connection_dsn(),
        cache_dir=args.cache_dir,
        allow_remote=args.allow_remote,
    )
    for result in results:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
