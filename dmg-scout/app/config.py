"""Configuration loading. config.yaml drives everything tunable; env vars carry secrets."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH_ENV = "SCOUT_CONFIG"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path lookup: cfg.get('sizing.tons_per_mw_it')."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def source(self, name: str) -> dict[str, Any]:
        return self._data.get("sources", {}).get(name, {})

    def source_enabled(self, name: str) -> bool:
        return bool(self.source(name).get("enabled", False))


@lru_cache(maxsize=1)
def load_config() -> Config:
    path = Path(os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG))
    with open(path) as f:
        return Config(yaml.safe_load(f))


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///scout.db")
    # Render supplies postgres://; SQLAlchemy 2.x requires postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")


def openai_api_key() -> str | None:
    """Whisper transcription only (app/pipeline/voice_capture.py) -- every
    other LLM call in this codebase goes through Anthropic."""
    return os.environ.get("OPENAI_API_KEY")


def capture_api_key() -> str | None:
    """Bearer token for POST /capture/voice (the iOS Shortcut endpoint) --
    deliberately separate from DASHBOARD_PASSWORD (app/web/main.py:auth):
    a phone automation credential and an interactive human login are
    different secrets with different exposure surfaces, and a leaked one
    should not hand over the other."""
    return os.environ.get("CAPTURE_API_KEY")


def backup_api_key() -> str | None:
    """Bearer token for POST /internal/backup (Block 4C Item 1) -- the
    nightly cron's own call into the web service, which is the only
    service Render allows a persistent Disk on (cron jobs can't have
    one, confirmed against Render's own docs before this was built).
    Separate secret from DASHBOARD_PASSWORD/CAPTURE_API_KEY for the same
    reason those are separate from each other."""
    return os.environ.get("BACKUP_API_KEY")


def weekly_brief_api_key() -> str | None:
    """Bearer token for POST /internal/weekly-briefs (Block 4C Item 6) --
    the Friday cron's own call into the web service, same separate-secret
    pattern as backup_api_key/capture_api_key above."""
    return os.environ.get("WEEKLY_BRIEF_API_KEY")


def user_email(cfg: Config, username: str) -> str | None:
    """The email address to send this username's own Weekly Brief to --
    dashboard.users[].email, optional per entry (Block 4C Item 6). A
    configured user with no email field, or a username not in
    dashboard.users at all, returns None -- app.pipeline.weekly_brief.
    run_weekly_briefs treats that exactly like no verified sending domain:
    archived, not emailed, with a reason on /reports, never a guessed
    address."""
    for entry in cfg.get("dashboard.users", []) or []:
        if entry.get("username") == username:
            return entry.get("email")
    return None


def backup_dir() -> str:
    """Where nightly pg_dump files live -- BACKUP_DIR env var, defaulting
    to the mount path of the Render Disk attached to dmg-scout-web in
    render.yaml. Overridable so local runs (`scout backup-now`) and tests
    don't need root to write under /var."""
    return os.environ.get("BACKUP_DIR", "/var/backups/scout")
