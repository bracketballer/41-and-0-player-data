"""Immutable ranked-roster delta archives and DigitalOcean Spaces helpers.

The ranked-roster ingestion produces a JSON candidate.  This module turns that
candidate into a small, deterministic ``tar.gz`` containing only
``candidate.json`` and its source manifest.  A separate publication manifest is
written beside the archive and is uploaded last; its presence is the release
publication marker.

No database or object-store discovery happens here.  Consumers must provide an
explicit descriptor or object keys, which keeps a checked-out release
descriptor the only thing that can activate a developer release.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

DELTA_FORMAT_VERSION = 1
DATASET = "ap_top25_vt_rosters_lineups"
FLYWAY_VERSION = "34"
DEFAULT_OBJECT_ROOT = "data-releases/v1"
DEFAULT_SPACES_REGION = "nyc3"
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_release_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError(
            "release version must be 1-128 characters of letters, numbers, '.', '_', or '-'"
        )
    return version


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


sha256 = sha256_file


def checksum_sidecar_path(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}.sha256")


def write_checksum_sidecar(archive: Path) -> Path:
    checksum = checksum_sidecar_path(archive)
    digest = sha256_file(archive)
    if checksum.exists():
        existing = checksum.read_text(encoding="utf-8")
        if existing != f"{digest}  {archive.name}\n":
            raise FileExistsError(f"refusing to overwrite mismatched checksum sidecar: {checksum}")
        return checksum
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    return checksum


def read_checksum_sidecar(checksum: Path, archive: Path) -> str:
    if checksum.name != f"{archive.name}.sha256":
        raise ValueError(f"checksum sidecar must be named {archive.name}.sha256")
    try:
        fields = checksum.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read archive checksum sidecar {checksum}: {error}") from error
    if len(fields) != 2 or fields[1] != archive.name or not _SHA256_RE.fullmatch(fields[0].lower()):
        raise ValueError("checksum sidecar must contain '<sha256>  <archive filename>'")
    actual = sha256_file(archive)
    if fields[0].lower() != actual:
        raise ValueError("archive checksum sidecar does not match the archive")
    return actual


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _candidate_checksum(candidate: dict[str, Any]) -> str:
    """Calculate the ingestion checksum, including derived eligibility rows."""

    from scripts.ingest.ingest_ranked_rosters import candidate_checksum

    return candidate_checksum(candidate)


def _candidate_files(candidate_dir: Path) -> tuple[Path, Path]:
    candidate = candidate_dir / "candidate.json"
    source_manifest = candidate_dir / "manifest.json"
    if not candidate.is_file() or not source_manifest.is_file():
        raise FileNotFoundError(
            f"candidate bundle must contain candidate.json and manifest.json: {candidate_dir}"
        )
    return candidate, source_manifest


def _validate_candidate_bundle(
    candidate_dir: Path,
    *,
    season: int,
    release_version: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    candidate_path, source_manifest_path = _candidate_files(candidate_dir)
    source_manifest = _json_object(source_manifest_path, "candidate source manifest")
    format_version = source_manifest.get("format_version")
    if format_version not in {3, 4}:
        raise ValueError(
            f"unsupported candidate source format: {format_version!r}"
        )
    expected = {
        "format_version": format_version,
        "status": "complete",
        "season": season,
        "release_version": release_version,
        "candidate_file": "candidate.json",
    }
    for key, value in expected.items():
        if source_manifest.get(key) != value:
            raise ValueError(
                f"candidate source manifest {key} does not match: "
                f"expected={value!r}, actual={source_manifest.get(key)!r}"
            )
    candidate = _json_object(candidate_path, "candidate")
    # Reuse the ingestion loader's strict derived eligibility and checksum
    # checks.  This also prevents a hand-written candidate from bypassing the
    # AP Top 25 coverage gate.
    from scripts.ingest.ingest_ranked_rosters import load_candidate_bundle

    loaded = load_candidate_bundle(candidate_dir, season, release_version)
    digest = _candidate_checksum(loaded)
    if source_manifest.get("candidate_sha256") != digest:
        raise ValueError("candidate source manifest checksum does not match candidate.json")
    serialized_keys = set(loaded) - {"eligible"}
    if set(candidate) != serialized_keys or candidate != {
        key: loaded[key] for key in candidate
    }:
        # ``loaded`` adds only the derived ``eligible`` key; all serialized
        # candidate rows must remain byte-for-byte represented by candidate.
        raise ValueError("candidate bundle contains unexpected derived data")
    return loaded, source_manifest, digest


def _counts(candidate: dict[str, Any]) -> dict[str, int]:
    counts = {
        "eligible_teams": len(candidate["eligible"]),
        "roster_memberships": sum(
            len(roster.get("players", [])) for roster in candidate["rosters"]
        ),
        "player_seasons": len(candidate["player_seasons"]),
        "games": len(candidate["games"]),
        "lineups": len(candidate["lineups"]),
        "opponent_contexts": len(candidate["opponent_contexts"]),
    }
    if "all_rosters" in candidate:
        counts["all_roster_memberships"] = sum(
            len(roster.get("players", []))
            for roster in candidate["all_rosters"]
        )
    return counts


def _object_prefix(season: int, release_version: str, root: str = DEFAULT_OBJECT_ROOT) -> str:
    validate_release_version(release_version)
    if not isinstance(season, int) or season < 2024:
        raise ValueError("season must be 2024 or later")
    root = root.strip("/")
    if not root or any(part in {".", ".."} for part in PurePosixPath(root).parts):
        raise ValueError("object root must be a relative object-storage path")
    return f"{root}/{DATASET}/{season}/{release_version}"


def artifact_stem(release_version: str) -> str:
    """Use the public release name once, even when it already has its prefix."""

    validate_release_version(release_version)
    return release_version if release_version.startswith("ranked-rosters-") else f"ranked-rosters-{release_version}"


def _archive_members(archive: Path) -> list[str]:
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"invalid ranked-roster delta archive {archive}: {error}") from error
    names: list[str] = []
    for member in members:
        name = PurePosixPath(member.name)
        if (
            member.isdir()
            or not member.isfile()
            or name.is_absolute()
            or ".." in name.parts
            or name.name not in {"candidate.json", "manifest.json"}
            or str(name) != name.name
        ):
            raise ValueError(f"delta archive contains an unsafe or unexpected member: {member.name}")
        names.append(member.name)
    if sorted(names) != ["candidate.json", "manifest.json"]:
        raise ValueError("delta archive must contain exactly candidate.json and manifest.json")
    return names


def create_delta_archive(
    *,
    candidate_dir: Path,
    output_dir: Path,
    season: int,
    release_version: str,
    pipeline_commit: str,
    flyway_v34_checksums: dict[str, int | str],
    object_root: str = DEFAULT_OBJECT_ROOT,
    created_at: datetime | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Create an immutable archive, sidecar, and publication manifest."""

    validate_release_version(release_version)
    if not isinstance(pipeline_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", pipeline_commit):
        raise ValueError("pipeline_commit must be a 7-64 character hexadecimal commit")
    if not flyway_v34_checksums:
        raise ValueError("required Flyway V34 checksums are required")
    normalized_flyway = {str(key): value for key, value in flyway_v34_checksums.items()}
    if FLYWAY_VERSION not in normalized_flyway:
        raise ValueError("required Flyway V34 checksum is missing")
    loaded, source_manifest, candidate_digest = _validate_candidate_bundle(
        candidate_dir, season=season, release_version=release_version
    )
    candidate_path, source_manifest_path = _candidate_files(candidate_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{artifact_stem(release_version)}.tar.gz"
    checksum = checksum_sidecar_path(archive)
    publication = output_dir / f"{artifact_stem(release_version)}.json"
    if archive.exists() or checksum.exists() or publication.exists():
        raise FileExistsError(
            f"refusing to overwrite existing ranked-roster delta objects for {release_version}"
        )

    # gzip's mtime and tar metadata are fixed so identical candidate inputs
    # produce identical archives regardless of the machine running the job.
    temporary = archive.with_name(f".{archive.name}.part")
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as target:
                    for source in (candidate_path, source_manifest_path):
                        info = tarfile.TarInfo(source.name)
                        info.size = source.stat().st_size
                        info.mtime = 0
                        info.mode = 0o600
                        with source.open("rb") as content:
                            target.addfile(info, content)
        temporary.replace(archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    archive.chmod(0o600)
    archive_digest = sha256_file(archive)
    checksum.write_text(f"{archive_digest}  {archive.name}\n", encoding="utf-8")
    checksum.chmod(0o600)
    _archive_members(archive)
    if created_at is None:
        # The ingestion source manifest is immutable and already records when
        # the candidate completed. Reusing it keeps the publication manifest
        # deterministic when a release is recreated for a dry-run or resume.
        source_timestamp = source_manifest.get("completed_at")
        if isinstance(source_timestamp, str):
            try:
                created_at = datetime.fromisoformat(source_timestamp.replace("Z", "+00:00"))
            except ValueError:
                created_at = None
    timestamp = (created_at or datetime(1970, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc)
    manifest = {
        "format_version": DELTA_FORMAT_VERSION,
        "dataset": DATASET,
        "release_version": release_version,
        "season": season,
        "created_at": timestamp.isoformat().replace("+00:00", "Z"),
        "pipeline_commit": pipeline_commit.lower(),
        "archive": {
            "filename": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": archive_digest,
            "compression": "gzip",
            "members": ["candidate.json", "manifest.json"],
        },
        "candidate": {
            "filename": "candidate.json",
            "sha256": candidate_digest,
            "bytes": candidate_path.stat().st_size,
            "source_manifest_sha256": sha256_file(source_manifest_path),
        },
        "row_counts": _counts(loaded),
        "flyway_v34_checksums": normalized_flyway,
        "required_flyway_v34_checksums": normalized_flyway,
        "objects": {
            "archive": f"{_object_prefix(season, release_version, object_root)}/{archive.name}",
            "checksum": f"{_object_prefix(season, release_version, object_root)}/{checksum.name}",
            "manifest": f"{_object_prefix(season, release_version, object_root)}/{publication.name}",
        },
    }
    publication.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    publication.chmod(0o600)
    return archive, checksum, publication, manifest


def validate_publication_manifest(
    manifest: dict[str, Any],
    *,
    archive: Path,
    checksum: Path,
    expected_dataset: str = DATASET,
    expected_season: int | None = None,
    expected_release_version: str | None = None,
) -> str:
    if manifest.get("format_version") != DELTA_FORMAT_VERSION:
        raise ValueError("unsupported ranked-roster delta manifest format")
    if manifest.get("dataset") != expected_dataset:
        raise ValueError("ranked-roster delta dataset does not match")
    release_version = manifest.get("release_version")
    if not isinstance(release_version, str):
        raise ValueError("ranked-roster delta release_version is missing")
    validate_release_version(release_version)
    if expected_release_version is not None and release_version != expected_release_version:
        raise ValueError("ranked-roster delta release version does not match descriptor")
    season = manifest.get("season")
    if not isinstance(season, int) or (expected_season is not None and season != expected_season):
        raise ValueError("ranked-roster delta season does not match descriptor")
    archive_meta = manifest.get("archive")
    candidate_meta = manifest.get("candidate")
    if not isinstance(archive_meta, dict) or not isinstance(candidate_meta, dict):
        raise ValueError("ranked-roster delta archive metadata is missing")
    if archive_meta.get("filename") != archive.name or archive_meta.get("bytes") != archive.stat().st_size:
        raise ValueError("ranked-roster delta archive filename or size does not match")
    archive_digest = sha256_file(archive)
    if archive_meta.get("sha256") != archive_digest:
        raise ValueError("ranked-roster delta archive checksum does not match manifest")
    read_checksum_sidecar(checksum, archive)
    if candidate_meta.get("filename") != "candidate.json" or not _SHA256_RE.fullmatch(str(candidate_meta.get("sha256", ""))):
        raise ValueError("ranked-roster delta candidate metadata is invalid")
    if not isinstance(manifest.get("flyway_v34_checksums"), dict) or "34" not in manifest["flyway_v34_checksums"]:
        raise ValueError("ranked-roster delta Flyway V34 checksum is missing")
    _archive_members(archive)
    return archive_digest


def _spaces_settings() -> tuple[str, str]:
    bucket = os.environ.get("DO_SPACES_BUCKET", "").strip()
    region = os.environ.get("DO_SPACES_REGION", DEFAULT_SPACES_REGION).strip() or DEFAULT_SPACES_REGION
    if not bucket or not os.environ.get("DO_SPACES_ACCESS_KEY_ID", "").strip() or not os.environ.get("DO_SPACES_SECRET_ACCESS_KEY", "").strip():
        raise RuntimeError(
            "missing DigitalOcean Spaces configuration: DO_SPACES_BUCKET, "
            "DO_SPACES_ACCESS_KEY_ID, and DO_SPACES_SECRET_ACCESS_KEY are required"
        )
    return region, bucket


def spaces_client() -> Any:
    # Keep importing archive/checksum helpers independent of optional database
    # and boto3 dependencies.  Upload/download commands load the established
    # Spaces client only when they actually need the network.
    from .development_snapshot import spaces_client as snapshot_spaces_client

    return snapshot_spaces_client()


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


def _object_matches(head: dict[str, Any], metadata: dict[str, str], size: int) -> bool:
    existing = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
    return all(existing.get(key.lower()) == value for key, value in metadata.items()) and (
        head.get("ContentLength") is None or int(head["ContentLength"]) == size
    )


def upload_delta(
    archive: Path,
    checksum: Path,
    manifest_path: Path,
    client: Any | None = None,
) -> dict[str, str]:
    manifest = _json_object(manifest_path, "publication manifest")
    archive_digest = validate_publication_manifest(manifest, archive=archive, checksum=checksum)
    objects = manifest.get("objects")
    if not isinstance(objects, dict):
        raise ValueError("ranked-roster delta object keys are missing")
    keys = [objects.get(name) for name in ("archive", "checksum", "manifest")]
    if any(not isinstance(key, str) or not key or key.startswith("/") for key in keys):
        raise ValueError("ranked-roster delta object keys are invalid")
    _, bucket = _spaces_settings()
    client = client or spaces_client()
    manifest_digest = sha256_file(manifest_path)
    items = [
        (keys[0], archive, "application/gzip", {"delta-sha256": archive_digest, "delta-release": manifest["release_version"]}),
        (keys[1], checksum, "text/plain; charset=utf-8", {"delta-sha256": sha256_file(checksum), "delta-release": manifest["release_version"]}),
        (keys[2], manifest_path, "application/json", {"delta-sha256": manifest_digest, "delta-release": manifest["release_version"]}),
    ]
    pending: list[tuple[str, Path, str, dict[str, str]]] = []
    for key, path, content_type, metadata in items:
        head = _head_object(client, bucket, key)
        if head is not None:
            if _object_matches(head, metadata, path.stat().st_size):
                continue
            raise FileExistsError(f"refusing to overwrite existing mismatched Spaces object: {key}")
        pending.append((key, path, content_type, metadata))
    # Archive and checksum become available first.  The manifest is explicitly
    # last so a consumer never observes an apparently published partial delta.
    for key, path, content_type, metadata in pending:
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={"ContentType": content_type, "Metadata": metadata},
        )
    return {"bucket": bucket, "archive": keys[0], "checksum": keys[1], "manifest": keys[2]}


def _safe_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if not member.isfile() or name.is_absolute() or ".." in name.parts or str(name) != name.name:
        raise ValueError(f"unsafe delta archive member: {member.name}")


def safe_extract_delta(archive: Path, destination: Path) -> tuple[Path, Path]:
    """Extract exactly the two expected files without path traversal."""

    _archive_members(archive)
    if destination.exists() and destination.is_symlink():
        raise ValueError("delta extraction directory must not be a symlink")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("delta extraction directory must be empty")
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            _safe_member(member)
            target = (destination / member.name).resolve()
            if target.parent != destination:
                raise ValueError(f"delta archive member escapes extraction directory: {member.name}")
            with source.extractfile(member) as content, target.open("wb") as output:
                shutil.copyfileobj(content, output)
            target.chmod(0o600)
    candidate = destination / "candidate.json"
    source_manifest = destination / "manifest.json"
    return candidate, source_manifest


def download_object(client: Any, bucket: str, key: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise ValueError(f"refusing to download over symlink: {destination}")
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        client.download_file(bucket, key, str(temporary))
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    destination.chmod(0o600)
    return destination


def download_delta(
    descriptor: dict[str, Any],
    destination_dir: Path,
    *,
    client: Any | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Download and verify a descriptor-selected delta before DB writes."""

    if descriptor.get("dataset") != DATASET:
        raise ValueError("release descriptor dataset does not match")
    release_version = descriptor.get("release_version")
    season = descriptor.get("season")
    if not isinstance(release_version, str) or not isinstance(season, int):
        raise ValueError("release descriptor release_version or season is invalid")
    validate_release_version(release_version)
    if season < 2024:
        raise ValueError("release descriptor season is unsupported")
    objects = descriptor.get("objects") or descriptor.get("remote_keys")
    if not isinstance(objects, dict) or any(not isinstance(objects.get(k), str) for k in ("archive", "checksum", "manifest")):
        raise ValueError("release descriptor object keys are incomplete")
    if any(
        str(objects[key]).startswith("/")
        or ".." in PurePosixPath(str(objects[key])).parts
        or "\x00" in str(objects[key])
        for key in ("archive", "checksum", "manifest")
    ):
        raise ValueError("release descriptor object keys are unsafe")
    expected = descriptor.get("expected")
    if expected is None:
        expected = {
            "archive_sha256": descriptor.get("archive_sha256"),
            "checksum_sha256": descriptor.get("checksum_sha256"),
            "manifest_sha256": descriptor.get("manifest_sha256"),
            "candidate_sha256": descriptor.get("candidate_sha256"),
        }
    if expected is not None and not isinstance(expected, dict):
        raise ValueError("release descriptor expected checksums are invalid")
    _, bucket = _spaces_settings()
    client = client or spaces_client()
    destination_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = destination_dir / "publication-manifest.json"
    archive = destination_dir / f"{artifact_stem(release_version)}.tar.gz"
    checksum = checksum_sidecar_path(archive)
    download_object(client, bucket, objects["manifest"], manifest_path)
    if isinstance(expected, dict) and expected.get("manifest_sha256"):
        if sha256_file(manifest_path) != expected["manifest_sha256"]:
            raise ValueError("downloaded publication manifest does not match descriptor checksum")
    manifest = _json_object(manifest_path, "publication manifest")
    # Validate descriptor/manifest agreement before fetching the large archive.
    if manifest.get("objects") != objects:
        raise ValueError("published delta manifest object keys do not match descriptor")
    if descriptor.get("pipeline_commit") and manifest.get("pipeline_commit") != descriptor["pipeline_commit"]:
        raise ValueError("published delta pipeline commit does not match descriptor")
    archive_name = manifest.get("archive", {}).get("filename")
    if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
        raise ValueError("published delta archive filename is unsafe")
    archive = destination_dir / archive_name
    checksum = checksum_sidecar_path(archive)
    download_object(client, bucket, objects["archive"], archive)
    download_object(client, bucket, objects["checksum"], checksum)
    if isinstance(expected, dict):
        if expected.get("archive_sha256") and sha256_file(archive) != expected["archive_sha256"]:
            raise ValueError("downloaded delta archive does not match descriptor checksum")
        if expected.get("checksum_sha256") and sha256_file(checksum) != expected["checksum_sha256"]:
            raise ValueError("downloaded delta sidecar does not match descriptor checksum")
    validate_publication_manifest(
        manifest,
        archive=archive,
        checksum=checksum,
        expected_season=season,
        expected_release_version=release_version,
    )
    return archive, checksum, manifest


# Friendly public aliases matching the existing development-snapshot API and
# keeping callers independent of the implementation's archive-oriented name.
create_delta = create_delta_archive
create_archive = create_delta_archive
upload_ranked_roster_delta = upload_delta
download_ranked_roster_delta = download_delta
validate_delta_manifest = validate_publication_manifest
