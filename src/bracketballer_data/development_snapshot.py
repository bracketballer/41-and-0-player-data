"""Create, validate, and publish sanitized development database snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import urlopen

try:
    import psycopg2
except ImportError:  # Archive-only helpers/tests do not need the DB driver.
    class _MissingPsycopg:
        @staticmethod
        def connect(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("psycopg2 is required for snapshot database operations")

    psycopg2 = _MissingPsycopg()  # type: ignore[assignment]

from .database import connection_dsn
SNAPSHOT_FORMAT_VERSION = 1
EXPECTED_FLYWAY_VERSION = 34
DEFAULT_SPACES_REGION = "nyc3"
DEFAULT_URL_EXPIRY_SECONDS = 24 * 60 * 60

# This is deliberately an allowlist. New sports tables must be reviewed and
# added explicitly; no user or community table can enter a snapshot by drift.
SNAPSHOT_TABLES: tuple[str, ...] = (
    "schools",
    "players",
    "player_position_maps",
    "player_defensive_stats",
    "player_seasons",
    "player_shot_events",
    "player_shooting_ingestion_status",
    "shooting_model_versions",
    "player_shooting_profiles",
    "player_shooting_ability_profiles",
    "player_shooting_zone_profiles",
    "player_shooting_game_profiles",
    "player_contextual_shooting_profiles",
    "defensive_model_versions",
    "player_characteristic_scores",
    "team_seasons",
    "team_season_eligibility",
    "team_roster_memberships",
    "team_roster_position_maps",
    "college_games",
    "opponent_team_season_contexts",
    "team_game_lineups",
    "team_game_lineup_players",
)

SENSITIVE_TABLES: tuple[str, ...] = (
    "users",
    "games",
    "game_player_pool",
    "game_player_queue_spots",
    "game_draft_picks",
    "lineup_comments",
    "lineup_labels",
    "lineup_votes",
    "published_lineups",
    "saved_labels",
)

# Flyway V28 installs these seven system labels as part of schema creation.
# They are application data, not snapshot data, but a freshly migrated target
# must retain them for the community-labels feature to work.
MIGRATION_SEEDED_LABELS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Poor floor spacing", "spacing"),
        ("Five-out spacing", "spacing"),
        ("No reliable primary and secondary ball handlers", "offense"),
        ("Weak ball movement", "offense"),
        ("Downhill shot creation", "offense"),
        ("Switch-everything defense", "defense"),
        ("Reliable in the clutch", "clutch"),
    }
)
MIGRATION_SEEDED_TABLE_COUNTS: dict[str, int] = {
    "lineup_labels": len(MIGRATION_SEEDED_LABELS),
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TABLE_DATA_RE = re.compile(r"\bTABLE DATA public ([A-Za-z_][A-Za-z0-9_]*)\b")


def validate_release_version(version: str) -> str:
    """Return a safe immutable release identifier or reject it."""

    if not _VERSION_RE.fullmatch(version):
        raise ValueError(
            "release version must be 1-128 characters of letters, numbers, '.', '_', or '-'"
        )
    return version


def utc_timestamp(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    return timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path, digest: str | None = None) -> Path:
    checksum_path = path.with_name(f"{path.name}.sha256")
    digest = digest or sha256_file(path)
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)
    return checksum_path


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("snapshot manifest must be a JSON object")
    return value


def _run_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"{command} PostgreSQL client is required") from error
    return result.stdout.strip() or result.stderr.strip()


def postgres_client_major(command: str) -> int:
    match = re.search(r"PostgreSQL\)?\s*(\d+)", _run_version(command))
    if not match:
        raise RuntimeError(f"could not determine {command} major version")
    return int(match.group(1))


def archive_table_data_names(archive: Path) -> set[str]:
    """Return table-data entries from a pg_restore TOC."""

    try:
        result = subprocess.run(
            ["pg_restore", "--list", str(archive)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"invalid PostgreSQL snapshot archive {archive}") from error
    return set(_TABLE_DATA_RE.findall(result.stdout))


def validate_archive_allowlist(archive: Path) -> set[str]:
    """Reject archives containing table data outside the sports allowlist."""

    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError(f"snapshot archive is missing or empty: {archive}")
    names = archive_table_data_names(archive)
    unexpected = names - set(SNAPSHOT_TABLES)
    if unexpected:
        raise ValueError(f"snapshot contains non-allowlisted table data: {sorted(unexpected)}")
    return names


def _table_count_query(table: str) -> str:
    if table not in SNAPSHOT_TABLES:
        raise ValueError(f"table is not in snapshot allowlist: {table}")
    return f'SELECT COUNT(*)::bigint FROM public."{table}"'


def source_snapshot_metadata(conn: Any) -> tuple[dict[str, Any], str]:
    """Read exact counts and Flyway metadata under the current transaction."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT current_setting('server_version'),
                   COUNT(*)::int,
                   COALESCE(MAX(version::int), 0),
                   COALESCE(bool_and(success), TRUE)
            FROM flyway_schema_history
            """
        )
        server_version, flyway_count, flyway_max, flyway_success = cursor.fetchone()
        cursor.execute(
            """
            SELECT version, description, checksum, success
            FROM flyway_schema_history
            ORDER BY installed_rank
            """
        )
        flyway_entries = [
            {
                "version": row[0],
                "description": row[1],
                "checksum": row[2],
                "success": row[3],
            }
            for row in cursor.fetchall()
        ]
        counts: dict[str, int] = {}
        for table in SNAPSHOT_TABLES:
            cursor.execute(_table_count_query(table))
            counts[table] = int(cursor.fetchone()[0])
    return (
        {
            "server_version": server_version,
            "flyway": {
                "count": flyway_count,
                "max_version": flyway_max,
                "all_success": bool(flyway_success),
                "entries": flyway_entries,
            },
            "table_counts": counts,
        },
        server_version,
    )


def _spaces_settings() -> tuple[str, str, str, str]:
    region = os.environ.get("DO_SPACES_REGION", DEFAULT_SPACES_REGION)
    bucket = os.environ.get("DO_SPACES_BUCKET", "").strip()
    access_key = os.environ.get("DO_SPACES_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("DO_SPACES_SECRET_ACCESS_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("DO_SPACES_BUCKET", bucket),
            ("DO_SPACES_ACCESS_KEY_ID", access_key),
            ("DO_SPACES_SECRET_ACCESS_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing DigitalOcean Spaces configuration: {', '.join(missing)}")
    return region, bucket, access_key, secret_key


def spaces_client() -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:
        raise RuntimeError(
            "install boto3 to upload, download, or share Spaces snapshots"
        ) from error
    region, bucket, access_key, secret_key = _spaces_settings()
    endpoint = os.environ.get("DO_SPACES_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = f"https://{region}.digitaloceanspaces.com"
    else:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("DO_SPACES_ENDPOINT must be an HTTPS URL")
        # Accept either the documented region endpoint or a bucket hostname,
        # but never let boto3 prepend the bucket twice.
        bucket_prefix = f"{bucket.lower()}."
        host = parsed.hostname.lower()
        if host.startswith(bucket_prefix):
            endpoint = f"{parsed.scheme}://{host[len(bucket_prefix):]}"
        else:
            endpoint = endpoint.rstrip("/")
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(s3={"addressing_style": "virtual"}),
    )


def manifest_paths(manifest: dict[str, Any]) -> tuple[str, str, str]:
    objects = manifest.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("manifest objects are missing")
    keys: list[str] = []
    for name in ("archive", "checksum", "manifest"):
        value = objects.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"manifest object key is missing or invalid: {name}")
        keys.append(value)
    return keys[0], keys[1], keys[2]


def validate_checksum_sidecar(checksum: Path, archive: Path) -> str:
    """Validate a GNU-style checksum sidecar and return the archive digest."""

    expected_name = f"{archive.name}.sha256"
    if checksum.name != expected_name:
        raise ValueError(f"checksum sidecar must be named {expected_name}")
    try:
        fields = checksum.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read checksum sidecar {checksum}: {error}") from error
    if len(fields) != 2 or fields[1] != archive.name or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
        raise ValueError("checksum sidecar must contain '<sha256>  <archive filename>'")
    digest = sha256_file(archive)
    if fields[0].lower() != digest:
        raise ValueError("checksum sidecar does not match the archive")
    return digest


def _head_object(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.NoSuchKey:
        return None
    except Exception as error:
        response = getattr(error, "response", {})
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise RuntimeError(f"could not check existing Spaces object {key}") from error


def _object_matches(head: dict[str, Any], expected: dict[str, str | int]) -> bool:
    metadata = {
        str(key).lower(): str(value)
        for key, value in (head.get("Metadata") or {}).items()
    }
    for key, value in expected.items():
        if metadata.get(key.lower()) != str(value):
            return False
    content_length = head.get("ContentLength")
    if content_length is not None and "bytes" in expected:
        if int(content_length) != int(expected["bytes"]):
            return False
    return True


def upload_snapshot(
    archive: Path,
    checksum: Path,
    manifest_path: Path,
) -> dict[str, str]:
    manifest = read_json(manifest_path)
    validate_manifest_archive(manifest, archive)
    digest = validate_checksum_sidecar(checksum, archive)
    release_value = manifest.get("release_version")
    if not isinstance(release_value, str):
        raise ValueError("snapshot manifest release_version is missing or invalid")
    release_version = validate_release_version(release_value)
    archive_key, checksum_key, manifest_key = manifest_paths(manifest)
    _, bucket, _, _ = _spaces_settings()
    client = spaces_client()
    checksum_digest = sha256_file(checksum)
    manifest_digest = sha256_file(manifest_path)
    keys = (
        (
            archive_key,
            archive,
            "application/octet-stream",
            {
                "snapshot-sha256": digest,
                "snapshot-release": release_version,
                "snapshot-bytes": str(archive.stat().st_size),
            },
        ),
        (
            checksum_key,
            checksum,
            "text/plain; charset=utf-8",
            {
                "snapshot-sha256": checksum_digest,
                "snapshot-release": release_version,
                "snapshot-bytes": str(checksum.stat().st_size),
            },
        ),
        (
            manifest_key,
            manifest_path,
            "application/json",
            {
                "snapshot-sha256": manifest_digest,
                "snapshot-release": release_version,
                "snapshot-bytes": str(manifest_path.stat().st_size),
            },
        ),
    )
    pending: list[tuple[str, Path, str, dict[str, str | int]]] = []
    for key, path, content_type, metadata in keys:
        head = _head_object(client, bucket, key)
        if head is not None:
            if _object_matches(head, metadata):
                continue
            raise FileExistsError(
                f"refusing to overwrite existing mismatched Spaces object: {key}"
            )
        pending.append((key, path, content_type, metadata))
    for key, path, content_type, metadata in pending:
        client.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": metadata},
        )
    return {"bucket": bucket, "archive": archive_key, "checksum": checksum_key, "manifest": manifest_key}


def presigned_urls(manifest_path: Path, expires_in: int = DEFAULT_URL_EXPIRY_SECONDS) -> dict[str, str]:
    if not 60 <= expires_in <= 7 * 24 * 60 * 60:
        raise ValueError("presigned URL expiry must be between 60 seconds and 7 days")
    manifest = read_json(manifest_path)
    archive_key, checksum_key, manifest_key = manifest_paths(manifest)
    _, bucket, _, _ = _spaces_settings()
    client = spaces_client()
    return {
        "archive": client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": archive_key}, ExpiresIn=expires_in
        ),
        "checksum": client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": checksum_key}, ExpiresIn=expires_in
        ),
        "manifest": client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": manifest_key}, ExpiresIn=expires_in
        ),
    }


def download_url(url: str, destination: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("snapshot downloads require an HTTPS URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        with urlopen(url, timeout=120) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target, length=1024 * 1024)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    destination.chmod(0o600)
    return destination


def _snapshot_manifest_candidates(client: Any, bucket: str) -> list[str]:
    """Return published snapshot manifests, newest first.

    The publisher writes the manifest last, so a manifest is the publication
    marker for an otherwise immutable snapshot.  Listing is paginated because
    Spaces uses the S3 API and a bucket can eventually contain many releases.
    """

    candidates: list[tuple[datetime, str]] = []
    continuation_token: str | None = None
    while True:
        request: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": "development-snapshots/v34/",
            "MaxKeys": 1000,
        }
        if continuation_token:
            request["ContinuationToken"] = continuation_token
        page = client.list_objects_v2(**request)
        for item in page.get("Contents", []) or []:
            key = item.get("Key")
            if not isinstance(key, str) or not key.endswith(".dump.json"):
                continue
            modified = item.get("LastModified")
            if not isinstance(modified, datetime):
                modified = datetime.min.replace(tzinfo=timezone.utc)
            elif modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            candidates.append((modified, key))
        if not page.get("IsTruncated"):
            break
        next_token = page.get("NextContinuationToken")
        if not isinstance(next_token, str) or not next_token:
            raise RuntimeError("DigitalOcean Spaces returned an incomplete object listing")
        continuation_token = next_token
    return [key for _, key in sorted(candidates, reverse=True)]


def _download_spaces_object(client: Any, bucket: str, key: str, destination: Path) -> Path:
    """Download one Spaces object atomically into a private directory."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        client.download_file(bucket, key, str(temporary))
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    destination.chmod(0o600)
    return destination


def _safe_archive_filename(manifest: dict[str, Any]) -> str:
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise ValueError("snapshot archive metadata is missing")
    filename = archive.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
        or "\x00" in filename
    ):
        raise ValueError("snapshot manifest contains an invalid archive filename")
    return filename


def download_latest_snapshot(destination_dir: Path) -> tuple[Path, Path, Path, dict[str, Any], str]:
    """Download and verify the newest complete snapshot from Spaces.

    The manifest is downloaded first because it contains the immutable object
    keys and archive filename.  Incomplete candidates are skipped when either
    data object is absent; malformed manifests are rejected rather than
    silently falling back to older data.
    """

    _, bucket, _, _ = _spaces_settings()
    client = spaces_client()
    candidates = _snapshot_manifest_candidates(client, bucket)
    if not candidates:
        raise RuntimeError("no development database snapshots were found in DigitalOcean Spaces")

    for index, manifest_key in enumerate(candidates):
        candidate_path = destination_dir / f"candidate-{index}.dump.json"
        _download_spaces_object(client, bucket, manifest_key, candidate_path)
        manifest = read_json(candidate_path)
        release_version = manifest.get("release_version")
        if not isinstance(release_version, str):
            raise ValueError("snapshot manifest release_version is missing or invalid")
        validate_release_version(release_version)
        archive_key, checksum_key, declared_manifest_key = manifest_paths(manifest)
        if declared_manifest_key != manifest_key:
            raise ValueError(
                "snapshot manifest object key does not match the published Spaces object"
            )
        archive_filename = _safe_archive_filename(manifest)
        if Path(archive_key).name != archive_filename:
            raise ValueError("snapshot archive object key does not match its manifest filename")
        if Path(checksum_key).name != f"{archive_filename}.sha256":
            raise ValueError("snapshot checksum object key does not match its archive filename")
        archive_path = destination_dir / archive_filename
        checksum_path = destination_dir / f"{archive_filename}.sha256"
        if _head_object(client, bucket, archive_key) is None or _head_object(
            client, bucket, checksum_key
        ) is None:
            candidate_path.unlink(missing_ok=True)
            continue
        _download_spaces_object(client, bucket, archive_key, archive_path)
        _download_spaces_object(client, bucket, checksum_key, checksum_path)
        validate_checksum_sidecar(checksum_path, archive_path)
        validate_manifest_archive(manifest, archive_path)
        return archive_path, checksum_path, candidate_path, manifest, manifest_key

    raise RuntimeError("no complete development database snapshot was found in DigitalOcean Spaces")


def build_manifest(
    *,
    archive: Path,
    metadata: dict[str, Any],
    release_version: str,
    created_at: str,
    object_prefix: str,
    pipeline_commit: str,
) -> dict[str, Any]:
    validate_release_version(release_version)
    validate_archive_allowlist(archive)
    digest = sha256_file(archive)
    archive_filename = archive.name
    checksum_filename = f"{archive_filename}.sha256"
    manifest_filename = f"{archive_filename}.json"
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "dataset": "development_player_school_sports",
        "release_version": release_version,
        "created_at": created_at,
        "pipeline_commit": pipeline_commit,
        "archive": {
            "filename": archive_filename,
            "bytes": archive.stat().st_size,
            "sha256": digest,
            "format": "postgres-custom-data-only",
            "compression": "zstd",
            "table_allowlist": list(SNAPSHOT_TABLES),
        },
        "source": metadata,
        "objects": {
            "archive": f"{object_prefix}/{archive_filename}",
            "checksum": f"{object_prefix}/{checksum_filename}",
            "manifest": f"{object_prefix}/{manifest_filename}",
        },
    }


def validate_manifest_archive(manifest: dict[str, Any], archive: Path) -> None:
    if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot manifest format")
    archive_metadata = manifest.get("archive")
    if not isinstance(archive_metadata, dict):
        raise ValueError("snapshot archive metadata is missing")
    if archive_metadata.get("filename") != archive.name:
        raise ValueError("manifest archive filename does not match the archive")
    if archive_metadata.get("bytes") != archive.stat().st_size:
        raise ValueError("manifest archive size does not match the archive")
    actual_digest = sha256_file(archive)
    if archive_metadata.get("sha256") != actual_digest:
        raise ValueError("snapshot archive checksum does not match the manifest")
    allowlist = archive_metadata.get("table_allowlist")
    if allowlist != list(SNAPSHOT_TABLES):
        raise ValueError("snapshot table allowlist does not match this application revision")
    validate_archive_allowlist(archive)


def _flyway_signature(entries: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (entry.get("version"), entry.get("checksum"), entry.get("success"))
        for entry in entries
    ]


def database_public_table_names(conn: Any) -> list[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public' AND tablename <> 'flyway_schema_history'
            ORDER BY tablename
            """
        )
        return [row[0] for row in cursor.fetchall()]


def database_table_counts(conn: Any, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cursor:
        for table in tables:
            cursor.execute(
                f'SELECT COUNT(*)::bigint FROM public."{table}"'
            )
            counts[table] = int(cursor.fetchone()[0])
    return counts


def validate_migration_seed_data(conn: Any) -> None:
    """Require the exact V34 system-label shape in a restore target."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT title, category, is_seed, author_user_id
            FROM lineup_labels
            """
        )
        rows = cursor.fetchall()
    identities = {
        (str(title), str(category))
        for title, category, is_seed, author_user_id in rows
        if is_seed is True and author_user_id is None
    }
    if len(rows) != len(MIGRATION_SEEDED_LABELS) or identities != MIGRATION_SEEDED_LABELS:
        raise RuntimeError(
            "target database does not contain only the expected Flyway-seeded "
            "lineup labels"
        )


def unexpected_target_table_counts(counts: dict[str, int]) -> dict[str, int]:
    """Return target rows that make an initial snapshot restore unsafe."""

    unexpected: dict[str, int] = {}
    for table, count in counts.items():
        expected = MIGRATION_SEEDED_TABLE_COUNTS.get(table, 0)
        if count != expected:
            unexpected[table] = count
    return unexpected


def validate_target_schema_and_empty(conn: Any, manifest: dict[str, Any]) -> None:
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ValueError("snapshot source metadata is missing")
    source_flyway = source.get("flyway")
    if not isinstance(source_flyway, dict):
        raise ValueError("snapshot Flyway metadata is missing")
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version')")
        server_version = str(cursor.fetchone()[0])
        if not server_version.startswith("16."):
            raise RuntimeError("target PostgreSQL major version 16 is required")
        cursor.execute(
            """
            SELECT version, checksum, success
            FROM flyway_schema_history
            ORDER BY installed_rank
            """
        )
        target_entries = [
            {"version": row[0], "checksum": row[1], "success": row[2]}
            for row in cursor.fetchall()
        ]
    if _flyway_signature(target_entries) != _flyway_signature(source_flyway["entries"]):
        raise RuntimeError("target Flyway history does not match the snapshot schema")
    if not source_flyway.get("all_success"):
        raise RuntimeError("snapshot source has an unsuccessful Flyway migration")
    if source_flyway.get("max_version") != EXPECTED_FLYWAY_VERSION:
        raise RuntimeError(
            f"snapshot must be created at Flyway V{EXPECTED_FLYWAY_VERSION}"
        )
    all_tables = database_public_table_names(conn)
    counts = database_table_counts(conn, all_tables)
    unexpected = unexpected_target_table_counts(counts)
    if unexpected:
        raise RuntimeError(
            "target database must contain only Flyway seed data before restore: "
            f"{unexpected}"
        )
    validate_migration_seed_data(conn)


def restore_snapshot(
    *,
    archive: Path,
    manifest_path: Path,
    target_dsn: str,
    pg_restore_command: str = "pg_restore",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, int]:
    manifest = read_json(manifest_path)
    validate_manifest_archive(manifest, archive)
    if postgres_client_major(pg_restore_command) != 16:
        raise RuntimeError("pg_restore PostgreSQL major version 16 is required")
    conn = psycopg2.connect(target_dsn)
    conn.autocommit = False
    try:
        validate_target_schema_and_empty(conn, manifest)
    finally:
        conn.rollback()
        conn.close()
    runner(
        [
            pg_restore_command,
            "--dbname=" + target_dsn,
            "--data-only",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--single-transaction",
            str(archive),
        ],
        check=True,
        text=True,
    )
    conn = psycopg2.connect(target_dsn)
    try:
        with conn.cursor() as cursor:
            cursor.execute("ANALYZE")
        conn.commit()
        expected = manifest["source"]["table_counts"]
        actual = database_table_counts(conn, list(SNAPSHOT_TABLES))
        if actual != {table: int(expected[table]) for table in SNAPSHOT_TABLES}:
            raise RuntimeError(f"restored table counts differ: {actual}")
        sensitive_counts = database_table_counts(conn, list(SENSITIVE_TABLES))
        unexpected_sensitive = unexpected_target_table_counts(sensitive_counts)
        if unexpected_sensitive:
            raise RuntimeError(
                "sensitive tables differ from migration seed data: "
                f"{unexpected_sensitive}"
            )
        validate_migration_seed_data(conn)
        return actual
    finally:
        conn.close()


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)
    return path


def create_snapshot(
    *,
    archive: Path,
    release_version: str,
    pipeline_commit: str,
    pg_dump_command: str = "pg_dump",
    dsn: str | None = None,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[Path, Path, dict[str, Any]]:
    """Create a consistent allowlisted archive plus checksum and manifest."""

    validate_release_version(release_version)
    if postgres_client_major(pg_dump_command) != 16:
        raise RuntimeError("pg_dump PostgreSQL major version 16 is required")
    if archive.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot archive: {archive}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    database_dsn = dsn or connection_dsn()
    conn = psycopg2.connect(database_dsn)
    conn.autocommit = False
    try:
        with conn.cursor() as cursor:
            cursor.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SELECT pg_export_snapshot()")
            snapshot_id = cursor.fetchone()[0]
        metadata, _ = source_snapshot_metadata(conn)
        if not metadata["server_version"].startswith("16."):
            raise RuntimeError("source PostgreSQL major version 16 is required")
        if not metadata["flyway"]["all_success"]:
            raise RuntimeError("source has an unsuccessful Flyway migration")
        if metadata["flyway"]["max_version"] != EXPECTED_FLYWAY_VERSION:
            raise RuntimeError(
                f"source must be at Flyway V{EXPECTED_FLYWAY_VERSION} to create this snapshot"
            )
        command = [
            pg_dump_command,
            database_dsn,
            "--format=custom",
            "--compress=zstd:9",
            "--data-only",
            "--no-owner",
            "--no-acl",
            f"--snapshot={snapshot_id}",
            f"--file={archive}",
        ]
        command.extend(f"--table=public.{table}" for table in SNAPSHOT_TABLES)
        runner(command, check=True, text=True)
    finally:
        conn.rollback()
        conn.close()

    archive.chmod(0o600)
    validate_archive_allowlist(archive)
    checksum = write_checksum(archive)
    timestamp = utc_timestamp(now)
    object_prefix = f"development-snapshots/v34/{timestamp.replace(':', '').replace('+00:00', 'Z')}/{release_version}"
    manifest = build_manifest(
        archive=archive,
        metadata=metadata,
        release_version=release_version,
        created_at=timestamp,
        object_prefix=object_prefix,
        pipeline_commit=pipeline_commit,
    )
    manifest_path = archive.with_name(f"{archive.name}.json")
    write_manifest(manifest_path, manifest)
    return archive, checksum, manifest_path
