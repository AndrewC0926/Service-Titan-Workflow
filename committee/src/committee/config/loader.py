"""Load and validate every YAML file under config/."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from committee.config.schema import AppConfig

CONFIG_FILES: dict[str, str] = {
    "models": "models.yaml",
    "risk_limits": "risk_limits.yaml",
    "policy_portfolio": "policy_portfolio.yaml",
    "universe": "universe.yaml",
    "signals": "signals.yaml",
    "scenarios": "scenarios.yaml",
    "tax": "tax_config.yaml",
    "app": "app.yaml",
}

# Files whose changes go through the 7-day change-control delay (DESIGN 11).
# app.yaml holds the broker order caps and the approval-gate settings, which are
# limits too (REVIEW R-03).
CONTROLLED_FILES: tuple[str, ...] = ("risk_limits.yaml", "policy_portfolio.yaml", "app.yaml")


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def default_root() -> Path:
    """Project root: the directory holding config/ (walks up from cwd)."""
    here = Path.cwd().resolve()
    for p in (here, *here.parents):
        if (p / "config" / "models.yaml").exists() and (p / "pyproject.toml").exists():
            return p
    return here


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"missing config file: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def load_raw(config_dir: Path) -> dict[str, dict[str, Any]]:
    return {key: read_yaml(config_dir / name) for key, name in CONFIG_FILES.items()}


def validate(raw: dict[str, dict[str, Any]]) -> AppConfig:
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"])
            fname = CONFIG_FILES.get(str(err["loc"][0]), "?") if err["loc"] else "?"
            lines.append(f"  {fname}: {loc}: {err['msg']}")
        raise ConfigError("invalid configuration:\n" + "\n".join(lines)) from None


def load_config(config_dir: Path | None = None) -> AppConfig:
    config_dir = config_dir or default_root() / "config"
    return validate(load_raw(config_dir))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
