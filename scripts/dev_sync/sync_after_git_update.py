"""Synchronize local schema revisions and audited data releases after Git updates.

The Git hooks call this module after merges, rebases, and checkouts.  Schema
dependencies are pinned by release descriptors.  The required Fastify commit
is fetched into a temporary detached worktree, so a developer's active
Fastify checkout is never switched, stashed, cleaned, or pulled.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from bracketballer_data.database import (
    connection_dsn,
    database_host,
    is_local_database,
    load_env_file,
)
from scripts.apply_pending_data_releases import (
    apply_pending,
    descriptor_paths,
    read_descriptor,
)
from scripts.dev_sync.ticket_runner import (
    apply_pending_ticket_releases,
    read_ticket_descriptor,
    ticket_descriptor_paths,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FASTIFY_REPO = REPO_ROOT.parent / "fastify"
LOCK_PATH = REPO_ROOT / ".development-sync.lock"
FASTIFY_REPOSITORY = "bracketballer/fastify"
REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
    check: bool = True,
    runner: Any = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def _git(
    repository: Path,
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    runner: Any = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", *arguments],
        cwd=repository,
        capture_output=capture_output,
        check=check,
        runner=runner,
    )


def _repository_key(value: str) -> str:
    normalized = value.strip().lower().removesuffix(".git")
    if normalized.startswith("git@github.com:"):
        normalized = normalized.removeprefix("git@github.com:")
    elif normalized.startswith("ssh://git@github.com/"):
        normalized = normalized.removeprefix("ssh://git@github.com/")
    elif normalized.startswith("https://github.com/"):
        normalized = normalized.removeprefix("https://github.com/")
    elif normalized.startswith("http://github.com/"):
        normalized = normalized.removeprefix("http://github.com/")
    return normalized.rstrip("/")


def _schema_dependencies_from_paths(paths: Sequence[Path]) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for path in paths:
        descriptor = read_descriptor(path)
        dependency = descriptor.get("schema_dependency")
        if dependency is not None:
            dependencies.append(
                {
                    "descriptor": path.name,
                    **dependency,
                }
            )
    return dependencies


def _schema_dependencies() -> list[dict[str, Any]]:
    dependencies = _schema_dependencies_from_paths(descriptor_paths())
    for path in ticket_descriptor_paths(REPO_ROOT / "releases" / "tickets"):
        descriptor = read_ticket_descriptor(path)
        dependency = descriptor.get("schema_dependency")
        if dependency is not None:
            dependencies.append(
                {
                    "descriptor": path.name,
                    **dependency,
                }
            )
    return dependencies


def _fastify_path() -> Path:
    configured = os.environ.get("FASTIFY_REPO_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_FASTIFY_REPO


def _validate_dependency(dependency: dict[str, Any]) -> None:
    ref = dependency.get("ref")
    if not isinstance(ref, str) or not REF_PATTERN.fullmatch(ref) or ".." in ref:
        raise ValueError(f"invalid Fastify ref in {dependency['descriptor']}")
    if not isinstance(dependency.get("commit"), str) or not re.fullmatch(
        r"[0-9a-f]{40}", dependency["commit"]
    ):
        raise ValueError(f"invalid Fastify commit in {dependency['descriptor']}")


def _ensure_fastify_revision(
    repository: Path,
    dependency: dict[str, Any],
    *,
    runner: Any = subprocess.run,
) -> str:
    _validate_dependency(dependency)
    if not repository.is_dir() or not (repository / ".git").exists():
        raise RuntimeError(f"Fastify repository is missing: {repository}")

    remote = _git(
        repository,
        ["remote", "get-url", "origin"],
        capture_output=True,
        runner=runner,
    ).stdout.strip()
    expected_repository = _repository_key(str(dependency["repository"]))
    if expected_repository and _repository_key(remote) != expected_repository:
        raise RuntimeError(
            f"Fastify origin mismatch: expected {expected_repository}, found {_repository_key(remote)}"
        )

    _git(repository, ["fetch", "--quiet", "origin", str(dependency["ref"])], runner=runner)
    fetched_commit = _git(
        repository,
        ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
        capture_output=True,
        runner=runner,
    ).stdout.strip().lower()
    requested_commit = str(dependency["commit"]).lower()
    if fetched_commit != requested_commit:
        is_ancestor = _git(
            repository,
            ["merge-base", "--is-ancestor", requested_commit, "FETCH_HEAD"],
            check=False,
            runner=runner,
        )
        if is_ancestor.returncode != 0:
            raise RuntimeError(
                f"Fastify ref {dependency['ref']} does not contain pinned commit {requested_commit}"
            )
    _git(repository, ["cat-file", "-e", f"{requested_commit}^{{commit}}"], runner=runner)
    return requested_commit


@contextmanager
def _fastify_worktree(
    repository: Path,
    commit: str,
    *,
    runner: Any = subprocess.run,
) -> Iterator[Path]:
    temporary_parent = Path(
        tempfile.mkdtemp(prefix="bracketballer-fastify-sync-")
    )
    worktree = temporary_parent / "checkout"
    created = False
    try:
        _git(repository, ["worktree", "add", "--detach", str(worktree), commit], runner=runner)
        created = True
        yield worktree
    finally:
        if created:
            _git(
                repository,
                ["worktree", "remove", "--force", str(worktree)],
                check=False,
                runner=runner,
            )
        shutil.rmtree(temporary_parent, ignore_errors=True)


def _flyway_environment(dsn: str) -> dict[str, str]:
    if "://" in dsn:
        parsed = urlsplit(dsn)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise RuntimeError("local DATABASE_URL must be a PostgreSQL TCP URL")
        host = parsed.hostname
        port = parsed.port or 5432
        database = unquote(parsed.path.lstrip("/"))
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        options = dict(parse_qsl(parsed.query, keep_blank_values=True))
    else:
        values = dict(
            token.split("=", 1)
            for token in shlex.split(dsn)
            if "=" in token
        )
        host = values.get("host", "localhost")
        port = int(values.get("port", "5432"))
        database = values.get("dbname", "")
        user = values.get("user", "")
        password = values.get("password", "")
        options = {key: values[key] for key in ("sslmode",) if key in values}
        if host.startswith("/"):
            raise RuntimeError("automatic Flyway sync requires a TCP local database host")

    if not database or not user:
        raise RuntimeError("database configuration must include a database name and user")
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host
    jdbc = f"jdbc:postgresql://{host_for_url}:{port}/{quote(database)}"
    if options:
        jdbc = f"{jdbc}?{urlencode(options)}"
    return {
        "FLYWAY_URL": jdbc,
        "FLYWAY_USER": user,
        "FLYWAY_PASSWORD": password,
    }


def _run_flyway(
    worktree: Path,
    dsn: str,
    commit: str,
    *,
    runner: Any = subprocess.run,
) -> None:
    image = f"bracketballer-fastify-flyway:dev-sync-{commit[:12]}"
    _run(
        [
            "docker",
            "build",
            "--file",
            str(worktree / "database" / "Dockerfile.flyway"),
            "--tag",
            image,
            str(worktree),
        ],
        runner=runner,
    )
    environment = os.environ.copy()
    environment.update(_flyway_environment(dsn))
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--env",
            "FLYWAY_URL",
            "--env",
            "FLYWAY_USER",
            "--env",
            "FLYWAY_PASSWORD",
            image,
            "-connectRetries=10",
            "migrate",
        ],
        runner=lambda command, **kwargs: runner(command, env=environment, **kwargs),
    )


def _verify_flyway(dsn: str, checksums: dict[str, Any]) -> None:
    try:
        import psycopg2
    except ImportError as error:  # pragma: no cover - requirements install this.
        raise RuntimeError("psycopg2 is required to verify Flyway migrations") from error
    from scripts.apply_pending_data_releases import require_flyway_checksums

    connection = psycopg2.connect(dsn)
    try:
        require_flyway_checksums(connection, checksums)
    finally:
        connection.rollback()
        connection.close()


def synchronize_schema(
    dsn: str,
    dependencies: list[dict[str, Any]],
    *,
    repository: Path | None = None,
    runner: Any = subprocess.run,
) -> None:
    if not dependencies:
        return
    fastify = repository or _fastify_path()
    seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
    for dependency in sorted(
        dependencies,
        key=lambda item: (int(item["flyway_version"]), item["commit"]),
    ):
        checksums = {
            str(key): value for key, value in dependency["flyway_checksums"].items()
        }
        commit = str(dependency["commit"]).lower()
        identity = (commit, tuple(sorted(checksums.items())))
        if identity in seen:
            continue
        seen.add(identity)
        pinned_commit = _ensure_fastify_revision(fastify, dependency, runner=runner)
        with _fastify_worktree(fastify, pinned_commit, runner=runner) as worktree:
            _run_flyway(worktree, dsn, pinned_commit, runner=runner)
        _verify_flyway(dsn, checksums)


@contextmanager
def _sync_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
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


def synchronize(*, runner: Any = subprocess.run) -> str:
    load_env_file()
    if not os.environ.get("DATABASE_URL", "").strip() and not (
        os.environ.get("PSQL_USER", "").strip() and os.environ.get("DEV_DB", "").strip()
    ):
        return "skipped: local database is not configured"
    dsn = connection_dsn()
    if not is_local_database(dsn):
        return (
            f"skipped: database host {database_host(dsn)!r} is remote; "
            "automatic development sync is local-only"
        )
    legacy_paths = descriptor_paths()
    ticket_paths = ticket_descriptor_paths(REPO_ROOT / "releases" / "tickets")
    if not legacy_paths and not ticket_paths:
        return "no checked-in data releases found"
    dependencies = _schema_dependencies()
    with _sync_lock() as acquired:
        if not acquired:
            return "skipped: another development sync is active"
        synchronize_schema(dsn, dependencies, runner=runner)
        results: list[str] = []
        if legacy_paths:
            if _spaces_configured():
                results.extend(apply_pending(dsn=dsn))
            else:
                results.append("skipped legacy data releases because Spaces credentials are not configured")
        if ticket_paths:
            results.extend(apply_pending_ticket_releases(
                releases_dir=REPO_ROOT / "releases" / "tickets",
                dsn=dsn,
            ))
    return "; ".join(results) if results else "no checked-in data releases found"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        print(f"development sync: {synchronize()}")
    except (FileNotFoundError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"development sync: FAILED: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
