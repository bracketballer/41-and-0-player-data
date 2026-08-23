"""Download CBBD starter and substitution evidence for offline validation.

The bundle is intentionally separate from PostgreSQL.  It is a resumable,
read-only source cache consumed by ``validate_onfloor_lineups`` and can be
deleted and rebuilt without changing application data.

Example::

    python -m scripts.analysis.fetch_cbbd_lineup_sources --season 2026 \
        --database-url "host=localhost dbname=bracketballer_dev user=postgres"

Pass ``--team`` one or more times to avoid a database connection when building
a smaller fixture bundle.  A CBBD API key is required only for this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

try:
    import cbbd
    from cbbd.rest import ApiException
except ImportError:  # pragma: no cover - exercised only without requirements
    cbbd = None  # type: ignore[assignment]

    class ApiException(Exception):
        status = None

import psycopg2

from bracketballer_data.database import connection_dsn, load_env_file
from bracketballer_data.paths import DATA_ROOT


FORMAT_VERSION = 2
DEFAULT_CACHE_ROOT = DATA_ROOT / "raw" / "lineup_validation"
DEFAULT_RETRIES = 4


def json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def model_payload(value: Any) -> dict[str, Any]:
    """Convert a generated CBBD model into alias-keyed JSON-safe data."""

    if hasattr(value, "to_json"):
        try:
            return json.loads(value.to_json())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "json"):
        try:
            return json.loads(value.json(by_alias=True))
        except TypeError:
            return json.loads(value.json())
    raw = value.to_dict() if hasattr(value, "to_dict") else value
    return json.loads(json.dumps(raw, default=json_default))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read source bundle {path}: {error}") from error


def write_json(path: Path, value: Any) -> None:
    """Atomically write a source response or completion manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".part",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, default=json_default, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def call_with_retries(call: Callable[[], Any], retries: int) -> Any:
    for attempt in range(1, retries + 1):
        try:
            return call()
        except Exception as error:
            status = getattr(error, "status", None)
            retryable = not isinstance(error, ApiException) or (
                status is None or status == 429 or status >= 500
            )
            if attempt == retries or not retryable:
                raise
            time.sleep(min(30.0, 2 ** (attempt - 1)))
    raise AssertionError("retry loop exited")


def safe_team_filename(team: str) -> str:
    """Return a stable, collision-resistant filename for a team name."""

    slug = re.sub(r"[^a-z0-9]+", "-", team.lower()).strip("-") or "team"
    digest = hashlib.sha256(team.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:60]}-{digest}.json"


def source_directory(season: int, cache_root: Path) -> Path:
    return cache_root / str(season)


def discover_teams(database_url: str, season: int) -> list[str]:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT team FROM player_shot_events
                WHERE season = %s AND team IS NOT NULL
                UNION
                SELECT opponent FROM player_shot_events
                WHERE season = %s AND opponent IS NOT NULL
                ORDER BY 1
                """,
                (season, season),
            )
            return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def api_clients(access_token: str) -> tuple[Any, Any]:
    if cbbd is None:
        raise RuntimeError("cbbd is required for source downloads")
    configuration = cbbd.Configuration(access_token=access_token)
    client = cbbd.ApiClient(configuration)
    return cbbd.GamesApi(client), cbbd.PlaysApi(client)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_bundle(
    season: int,
    teams: list[str],
    cache_root: Path,
    access_token: str,
    retries: int = DEFAULT_RETRIES,
    refresh: bool = False,
) -> Path:
    """Download or resume one season's starter/substitution source bundle."""

    target = source_directory(season, cache_root)
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / "manifest.json"
    if manifest_path.exists() and not refresh:
        manifest = read_json(manifest_path)
        cached_teams = sorted(
            entry.get("team")
            for entry in manifest.get("teams", [])
            if isinstance(entry, dict) and isinstance(entry.get("team"), str)
        )
        if (
            manifest.get("format_version") == FORMAT_VERSION
            and cached_teams == sorted(set(teams))
        ):
            print(f"using complete cached bundle {target}")
            return target

    games_api, plays_api = api_clients(access_token)
    substitution_dir = target / "substitutions"
    substitution_dir.mkdir(parents=True, exist_ok=True)
    game_players_dir = target / "game_players"
    game_players_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for team in sorted(set(teams)):
        substitution_path = substitution_dir / safe_team_filename(team)
        game_players_path = game_players_dir / safe_team_filename(team)
        if substitution_path.exists() and not refresh:
            rows = read_json(substitution_path)
        else:
            response = call_with_retries(
                lambda team_name=team: plays_api.get_substitutions_by_team(
                    season=season,
                    team=team_name,
                    _request_timeout=(10, 120),
                ),
                retries,
            )
            rows = [model_payload(row) for row in response or []]
            write_json(substitution_path, rows)
        if not isinstance(rows, list):
            raise ValueError(f"substitution cache is not a list: {substitution_path}")

        if game_players_path.exists() and not refresh:
            game_rows = read_json(game_players_path)
        else:
            response = call_with_retries(
                lambda team_name=team: games_api.get_game_players(
                    season=season,
                    team=team_name,
                    _request_timeout=(10, 120),
                ),
                retries,
            )
            game_rows = [model_payload(row) for row in response or []]
            write_json(game_players_path, game_rows)
        if not isinstance(game_rows, list):
            raise ValueError(f"game-player cache is not a list: {game_players_path}")
        entries.append(
            {
                "team": team,
                "substitutions_file": str(substitution_path.relative_to(target)),
                "rows": len(rows),
                "sha256": sha256_file(substitution_path),
                "game_players_file": str(game_players_path.relative_to(target)),
                "game_players_rows": len(game_rows),
                "game_players_sha256": sha256_file(game_players_path),
            }
        )
        print(f"{team}: {len(game_rows)} game-player rows, {len(rows)} substitution rows")

    write_json(
        manifest_path,
        {
            "format_version": FORMAT_VERSION,
            "source": "cbbd",
            "season": season,
            "teams": entries,
        },
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--database-url")
    parser.add_argument("--team", action="append", dest="teams")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    args = parser.parse_args()

    load_env_file()
    teams = list(args.teams or [])
    if not teams:
        database_url = args.database_url or connection_dsn()
        teams = discover_teams(database_url, args.season)
    access_token = os.environ.get("CBBD_API_KEY")
    if not access_token:
        raise SystemExit("Set CBBD_API_KEY before downloading lineup sources")
    download_bundle(
        season=args.season,
        teams=teams,
        cache_root=args.cache_root,
        access_token=access_token,
        retries=max(1, args.retries),
        refresh=args.refresh,
    )


if __name__ == "__main__":
    main()
