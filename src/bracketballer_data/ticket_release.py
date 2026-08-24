"""Immutable ticketed data artifacts for local development synchronization.

Ticket releases are deliberately descriptor-driven: a checked-in descriptor
names every object and checksum, while the descriptor's handler performs the
dataset-specific validation and database write.  No caller lists a Spaces
prefix or selects a newest object implicitly.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

TICKET_RELEASE_FORMAT_VERSION = 1
DEFAULT_OBJECT_ROOT = "data-releases/tickets"
DEFAULT_SPACES_REGION = "nyc3"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HANDLER_RE = re.compile(
    r"^scripts\.dev_sync\.tickets\.issue_(?P<ticket>[0-9]{4})_[a-z0-9_]+$"
)


def validate_release_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError("release version is not a safe immutable identifier")
    return version


def validate_handler(handler: str, ticket_number: int) -> str:
    if not isinstance(handler, str):
        raise ValueError("ticket handler must be a module path")
    match = _HANDLER_RE.fullmatch(handler)
    if match is None or int(match.group("ticket")) != int(ticket_number):
        raise ValueError("ticket handler must match its zero-padded ticket number")
    return handler


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path) -> PurePosixPath:
    relative = PurePosixPath(path.as_posix())
    if relative.is_absolute() or ".." in relative.parts or "\x00" in str(relative):
        raise ValueError(f"unsafe artifact path: {path}")
    return relative


def _artifact_files(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        raise FileNotFoundError(f"ticket artifact source directory is missing: {source_dir}")
    files: list[Path] = []
    for path in sorted(source_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in ticket artifacts: {path}")
        if path.is_file():
            _safe_relative(path.relative_to(source_dir))
            files.append(path)
    if not files:
        raise ValueError("ticket artifact source directory is empty")
    return files


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def create_ticket_archive(
    *,
    source_dir: Path,
    output_dir: Path,
    ticket_number: int,
    release_version: str,
    dataset: str,
    object_root: str = DEFAULT_OBJECT_ROOT,
    pipeline_commit: str | None = None,
    flyway_version: str | None = None,
    flyway_checksums: dict[str, int | str] | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Create a deterministic archive, checksum sidecar, and manifest."""

    if int(ticket_number) <= 0:
        raise ValueError("ticket number must be positive")
    validate_release_version(release_version)
    if not dataset or not isinstance(dataset, str):
        raise ValueError("dataset is required")
    files = _artifact_files(source_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"issue-{int(ticket_number):04d}-{release_version}.tar.gz"
    if archive.exists():
        raise FileExistsError(f"refusing to overwrite ticket artifact: {archive}")

    with archive.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                for path in files:
                    relative = _safe_relative(path.relative_to(source_dir))
                    info = tar.gettarinfo(str(path), arcname=str(relative))
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with path.open("rb") as source:
                        tar.addfile(info, source)

    digest = sha256_file(archive)
    checksum = archive.with_name(f"{archive.name}.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    object_prefix = f"{object_root.rstrip('/')}/{dataset}/{int(ticket_number):04d}/{release_version}"
    publication = {
        "format_version": TICKET_RELEASE_FORMAT_VERSION,
        "ticket_number": int(ticket_number),
        "dataset": dataset,
        "release_version": release_version,
        "objects": {
            "archive": f"{object_prefix}/{archive.name}",
            "checksum": f"{object_prefix}/{checksum.name}",
            "manifest": f"{object_prefix}/{archive.stem}.json",
        },
        "archive": {"sha256": digest, "bytes": archive.stat().st_size},
        "files": [str(_safe_relative(path.relative_to(source_dir))) for path in files],
    }
    if pipeline_commit is not None:
        publication["pipeline_commit"] = pipeline_commit
    if flyway_version is not None:
        publication["flyway_version"] = flyway_version
    if flyway_checksums is not None:
        publication["flyway_checksums"] = {str(k): v for k, v in flyway_checksums.items()}
    manifest = output_dir / f"{archive.stem}.json"
    manifest.write_bytes(_json_bytes(publication))
    return archive, checksum, manifest, publication


def _spaces_client() -> Any:
    from .development_snapshot import spaces_client

    return spaces_client()


def _object_exists(client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as error:
        exceptions = getattr(client, "exceptions", None)
        missing = getattr(exceptions, "NoSuchKey", ()) if exceptions else ()
        if missing and isinstance(error, missing):
            return None
        if error.__class__.__name__ in {"NoSuchKey", "NotFound", "ClientError"}:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
        raise


def _upload_one(client: Any, path: Path, bucket: str, key: str) -> None:
    existing = _object_exists(client, bucket, key)
    digest = sha256_file(path)
    if existing is not None:
        metadata = existing.get("Metadata", {})
        if metadata.get("sha256") == digest and int(existing.get("ContentLength", -1)) == path.stat().st_size:
            return
        raise FileExistsError(f"refusing to overwrite mismatched Spaces object: {key}")
    client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"Metadata": {"sha256": digest}},
    )


def upload_ticket_artifact(
    *,
    archive: Path,
    checksum: Path,
    manifest: Path,
    client: Any | None = None,
    bucket: str | None = None,
) -> dict[str, str]:
    """Upload archive/checksum first and publication manifest last."""

    client = client or _spaces_client()
    bucket = bucket or os.environ.get("DO_SPACES_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("DO_SPACES_BUCKET is required")
    publication = json.loads(manifest.read_text(encoding="utf-8"))
    objects = publication.get("objects")
    if not isinstance(objects, dict) or any(key not in objects for key in ("archive", "checksum", "manifest")):
        raise ValueError("ticket publication manifest has incomplete object keys")
    _upload_one(client, archive, bucket, str(objects["archive"]))
    _upload_one(client, checksum, bucket, str(objects["checksum"]))
    _upload_one(client, manifest, bucket, str(objects["manifest"]))
    return {key: str(objects[key]) for key in ("archive", "checksum", "manifest")}


def download_ticket_artifact(
    descriptor: dict[str, Any],
    cache_dir: Path,
    *,
    client: Any | None = None,
    bucket: str | None = None,
) -> tuple[Path, Path, Path]:
    client = client or _spaces_client()
    bucket = bucket or os.environ.get("DO_SPACES_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("DO_SPACES_BUCKET is required")
    objects = descriptor["objects"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        name: cache_dir / Path(str(objects[name])).name
        for name in ("archive", "checksum", "manifest")
    }
    for name, path in paths.items():
        client.download_file(bucket, str(objects[name]), str(path))
    return paths["archive"], paths["checksum"], paths["manifest"]


def safe_extract_ticket(archive: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk():
                raise ValueError(f"unsafe ticket archive member: {member.name}")
            target = (destination / Path(*relative.parts)).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"ticket archive escapes destination: {member.name}")
            tar.extract(member, destination, filter="data")
            if member.isfile():
                extracted.append(target)
    return extracted


def verify_ticket_artifact(
    descriptor: dict[str, Any],
    archive: Path,
    checksum: Path,
    manifest: Path,
) -> str:
    expected = descriptor["expected"]
    if sha256_file(archive) != expected["archive_sha256"]:
        raise ValueError("ticket archive checksum does not match descriptor")
    if expected.get("archive_bytes") is not None and archive.stat().st_size != int(expected["archive_bytes"]):
        raise ValueError("ticket archive size does not match descriptor")
    if sha256_file(checksum) != expected["checksum_sha256"]:
        raise ValueError("ticket checksum sidecar does not match descriptor")
    if sha256_file(manifest) != expected["manifest_sha256"]:
        raise ValueError("ticket publication manifest does not match descriptor")
    return expected["archive_sha256"]


__all__ = [
    "DEFAULT_OBJECT_ROOT",
    "TICKET_RELEASE_FORMAT_VERSION",
    "create_ticket_archive",
    "download_ticket_artifact",
    "safe_extract_ticket",
    "sha256_file",
    "upload_ticket_artifact",
    "validate_handler",
    "validate_release_version",
    "verify_ticket_artifact",
]
