"""Shared PostgreSQL environment loading without ingestion dependencies."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from .paths import ENV_FILE


def load_env_file(path: str | Path = ENV_FILE) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connection_dsn() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    user = os.environ.get("PSQL_USER")
    password = os.environ.get("PSQL_PWD")
    dbname = os.environ.get("DEV_DB")
    if not (user and dbname):
        raise SystemExit(
            "Missing DB config. Set DATABASE_URL, or PSQL_USER/PSQL_PWD/DEV_DB"
        )
    host = os.environ.get("PSQL_HOST", "localhost")
    port = os.environ.get("PSQL_PORT", "5432")
    return (
        f"host={host} port={port} dbname={dbname} user={user} "
        f"password={password or ''}"
    )


def database_host(dsn: str) -> str:
    """Return the normalized host from a PostgreSQL URL or keyword DSN."""

    if "://" in dsn:
        parsed = urlparse(dsn)
        return (parsed.hostname or "").lower()
    match = re.search(r"(?:^|\s)host=([^\s]+)", dsn)
    return (match.group(1) if match else "localhost").strip("'\"").lower()


def is_local_database(dsn: str) -> bool:
    host = database_host(dsn)
    return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("/")
