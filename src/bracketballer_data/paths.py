"""Repository-local paths shared by data preparation and ingestion jobs."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
RAW_PLAYER_SEASONS = DATA_ROOT / "raw" / "player_seasons"
RAW_RANKED_ROSTERS = DATA_ROOT / "raw" / "ranked_rosters"
PROCESSED_CORE = DATA_ROOT / "processed" / "core"
TORVIK_CACHE = DATA_ROOT / "cache" / "torvik"
SHOT_EXPORTS = DATA_ROOT / "exports" / "shots"
TORVIK_REPORTS = DATA_ROOT / "reports" / "torvik"
ENV_FILE = REPO_ROOT / ".env"


def ensure_data_directories() -> None:
    """Create writable output directories used by local data jobs."""

    for path in (
        RAW_PLAYER_SEASONS,
        RAW_RANKED_ROSTERS,
        PROCESSED_CORE,
        TORVIK_CACHE,
        SHOT_EXPORTS,
        TORVIK_REPORTS,
    ):
        path.mkdir(parents=True, exist_ok=True)
