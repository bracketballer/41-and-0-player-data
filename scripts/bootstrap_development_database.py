"""Restore the latest development snapshot into a pristine local V34 database.

This command is intentionally conservative: it never writes to a remote
database, never replaces an initialized database, and treats missing local
onboarding prerequisites as an actionable skip. Corrupt artifacts and unsafe
target contents are hard failures.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

try:
    import psycopg2
except ImportError:  # pragma: no cover - setup-local installs requirements first.
    psycopg2 = None  # type: ignore[assignment]

from bracketballer_data.database import (
    connection_dsn,
    database_host,
    is_local_database,
    load_env_file,
)
from bracketballer_data.development_snapshot import (
    EXPECTED_FLYWAY_VERSION,
    SNAPSHOT_TABLES,
    database_public_table_names,
    database_table_counts,
    download_latest_snapshot,
    restore_snapshot,
    unexpected_target_table_counts,
    validate_migration_seed_data,
)


def spaces_configured() -> bool:
    return all(
        os.environ.get(name, "").strip()
        for name in (
            "DO_SPACES_BUCKET",
            "DO_SPACES_ACCESS_KEY_ID",
            "DO_SPACES_SECRET_ACCESS_KEY",
        )
    )


def database_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip()) or bool(
        os.environ.get("PSQL_USER", "").strip()
        and os.environ.get("DEV_DB", "").strip()
    )


def database_bootstrap_state(conn: Any) -> str:
    """Return ``pristine`` or ``initialized`` for a compatible V34 target."""

    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.flyway_schema_history')")
        if cursor.fetchone()[0] is None:
            return "not-migrated"
        cursor.execute(
            """
            SELECT version, success
            FROM flyway_schema_history
            ORDER BY installed_rank
            """
        )
        flyway_rows = cursor.fetchall()
    if not flyway_rows or not all(bool(row[1]) for row in flyway_rows):
        return "not-migrated"
    numeric_versions = [
        int(str(row[0])) for row in flyway_rows if str(row[0]).isdigit()
    ]
    if not numeric_versions or max(numeric_versions) != EXPECTED_FLYWAY_VERSION:
        return "not-migrated"

    public_tables = database_public_table_names(conn)
    required_tables = set(SNAPSHOT_TABLES) | {"lineup_labels"}
    missing_tables = sorted(required_tables - set(public_tables))
    if missing_tables:
        return "not-migrated"

    snapshot_counts = database_table_counts(conn, list(SNAPSHOT_TABLES))
    if any(snapshot_counts.values()):
        return "initialized"

    all_counts = database_table_counts(conn, public_tables)
    unexpected = unexpected_target_table_counts(all_counts)
    if unexpected:
        raise RuntimeError(
            "local database has application data but no development snapshot; "
            f"refusing to replace it: {unexpected}"
        )
    validate_migration_seed_data(conn)
    return "pristine"


def bootstrap_development_database(*, dsn: str) -> tuple[bool, str]:
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required to inspect the development database")
    conn = psycopg2.connect(dsn)
    try:
        state = database_bootstrap_state(conn)
    finally:
        conn.rollback()
        conn.close()

    if state == "not-migrated":
        return False, (
            "skipped: local database is not migrated through Flyway V34; "
            "run the Fastify migrations and rerun scripts/setup-local.sh"
        )
    if state == "initialized":
        return True, "skipped: local database already contains development snapshot data"

    with TemporaryDirectory(prefix="bracketballer-development-snapshot-") as directory:
        archive, _, manifest_path, manifest, manifest_key = download_latest_snapshot(
            Path(directory)
        )
        counts = restore_snapshot(
            archive=archive,
            manifest_path=manifest_path,
            target_dsn=dsn,
        )
    return True, (
        f"restored release {manifest.get('release_version')} from {manifest_key} "
        f"({sum(counts.values())} allowlisted rows)"
    )


def main() -> int:
    load_env_file()
    if not database_configured():
        print(
            "database bootstrap: skipped (configure DATABASE_URL or PSQL_USER/DEV_DB "
            "and rerun scripts/setup-local.sh)"
        )
        return 10

    dsn = connection_dsn()
    if not is_local_database(dsn):
        print(
            "database bootstrap: skipped "
            f"(database host {database_host(dsn)!r} is remote; automatic snapshot "
            "restores are local-only)"
        )
        return 10
    if not spaces_configured():
        print(
            "database bootstrap: skipped (configure read-only DO_SPACES_* credentials "
            "and rerun scripts/setup-local.sh)"
        )
        return 10

    try:
        ready, result = bootstrap_development_database(dsn=dsn)
    except Exception as error:
        operational_error = getattr(psycopg2, "OperationalError", ())
        if operational_error and isinstance(error, operational_error):
            print(
                "database bootstrap: skipped (local PostgreSQL is unavailable; start it, "
                "run the Fastify migrations through V34, and rerun scripts/setup-local.sh)",
                file=sys.stderr,
            )
            return 10
        print(f"database bootstrap: FAILED: {error}", file=sys.stderr)
        return 1
    print(f"database bootstrap: {result}")
    return 0 if ready else 10


if __name__ == "__main__":
    raise SystemExit(main())
