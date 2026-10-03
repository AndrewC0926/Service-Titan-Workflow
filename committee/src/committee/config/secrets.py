"""Secrets: OS keychain first, then .env, then the process environment.

Values are never logged; use ``mask`` for display.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SECRET_NAMES: tuple[str, ...] = (
    "SEC_USER_AGENT",
    "FINNHUB_KEY",
    "MASSIVE_KEY",
    "FRED_KEY",
    "ANTHROPIC_API_KEY",
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
    "LIVE_TRADING_ENABLED",
    "SMTP_URL",
    "DIGEST_TO",
    "BACKUP_PASSPHRASE",
)
LIVE_ONLY: frozenset[str] = frozenset({"ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET"})
KEYCHAIN_SERVICE = "committee"


def mask(value: str | None) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def _keychain_lookup(name: str) -> str | None:
    try:
        import keyring

        value = keyring.get_password(KEYCHAIN_SERVICE, name)
    except Exception:
        return None
    return value if isinstance(value, str) else None


@dataclass
class Secrets:
    """Resolved secrets. Live broker keys resolve only when the live flag is on."""

    env_file: Path | None = None
    keychain: Callable[[str], str | None] = _keychain_lookup
    _env: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.env_file is not None:
            self._env = parse_env_file(self.env_file)

    def __repr__(self) -> str:  # never expose values
        return "Secrets(<masked>)"

    def _resolve(self, name: str) -> str | None:
        v = self.keychain(name)
        if v:
            return v
        if self._env.get(name):
            return self._env[name]
        return os.environ.get(name) or None

    def live_trading_enabled(self) -> bool:
        return (self._resolve("LIVE_TRADING_ENABLED") or "false").strip().lower() == "true"

    def get(self, name: str) -> str | None:
        if name not in SECRET_NAMES:
            raise KeyError(f"unknown secret {name}")
        if name in LIVE_ONLY and not self.live_trading_enabled():
            return None
        return self._resolve(name)

    def require(self, name: str) -> str:
        v = self.get(name)
        if not v:
            raise RuntimeError(f"secret {name} is not set (keychain service 'committee' or .env)")
        return v

    def summary(self) -> dict[str, str]:
        return {
            n: mask(self.get(n))
            if n != "LIVE_TRADING_ENABLED"
            else str(self.live_trading_enabled()).lower()
            for n in SECRET_NAMES
        }
