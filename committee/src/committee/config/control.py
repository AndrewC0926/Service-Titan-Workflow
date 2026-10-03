"""Config change control (DESIGN 11): limit changes take effect 7 days after
they are journaled. Until then the previously journaled values stay active.

The journal is the source of truth for controlled files (risk_limits.yaml,
policy_portfolio.yaml). Editing the YAML only *requests* a change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from committee.config.loader import CONFIG_FILES, CONTROLLED_FILES, load_raw, read_yaml, validate
from committee.config.schema import AppConfig
from committee.journal.canonical import canonical_json
from committee.journal.store import Journal

CHANGE_DELAY = dt.timedelta(days=7)
_KEY_FOR_FILE = {v: k for k, v in CONFIG_FILES.items()}


def content_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


@dataclass(frozen=True)
class ConfigVersion:
    file: str
    content: dict[str, Any]
    content_hash: str
    effective_at: dt.datetime
    seq: int
    baseline: bool


@dataclass(frozen=True)
class SyncResult:
    file: str
    status: str  # "baseline" | "pending" | "unchanged"
    effective_at: dt.datetime | None = None


def _versions(journal: Journal, file: str) -> list[ConfigVersion]:
    out = []
    for e in journal.entries("config_change"):
        p = e.payload
        if p.get("file") != file:
            continue
        out.append(
            ConfigVersion(
                file=file,
                content=p["content"],
                content_hash=p["content_hash"],
                effective_at=dt.datetime.fromisoformat(p["effective_at_utc"]),
                seq=e.seq,
                baseline=bool(p.get("baseline")),
            )
        )
    return out


def sync(journal: Journal, config_dir: Path, now: dt.datetime) -> list[SyncResult]:
    """Journal any change to a controlled file as a pending change effective now + 7 days."""
    results = []
    raw = load_raw(config_dir)
    validate(raw)  # never journal a change that would not load
    for file in CONTROLLED_FILES:
        data = read_yaml(config_dir / file)
        h = content_hash(data)
        versions = _versions(journal, file)
        if not versions:
            journal.append(
                "config_change",
                {
                    "file": file,
                    "content": data,
                    "content_hash": h,
                    "previous_hash": None,
                    "effective_at_utc": now.isoformat(),
                    "baseline": True,
                },
            )
            results.append(SyncResult(file, "baseline", now))
            continue
        if versions[-1].content_hash == h:
            results.append(SyncResult(file, "unchanged"))
            continue
        eff = now + CHANGE_DELAY
        journal.append(
            "config_change",
            {
                "file": file,
                "content": data,
                "content_hash": h,
                "previous_hash": versions[-1].content_hash,
                "effective_at_utc": eff.isoformat(),
                "baseline": False,
            },
        )
        results.append(SyncResult(file, "pending", eff))
    return results


def active_version(journal: Journal, file: str, now: dt.datetime) -> ConfigVersion | None:
    """The latest journaled version whose effective time has passed."""
    active = None
    for v in _versions(journal, file):
        if v.effective_at <= now and (active is None or v.seq > active.seq):
            active = v
    return active


def pending_versions(journal: Journal, now: dt.datetime) -> list[ConfigVersion]:
    out: list[ConfigVersion] = []
    for file in CONTROLLED_FILES:
        out.extend(v for v in _versions(journal, file) if v.effective_at > now)
    return out


def load_active_config(journal: Journal, config_dir: Path, now: dt.datetime) -> AppConfig:
    """Config with controlled files replaced by their journaled, active versions."""
    sync(journal, config_dir, now)
    raw = load_raw(config_dir)
    for file in CONTROLLED_FILES:
        v = active_version(journal, file, now)
        if v is not None:
            raw[_KEY_FOR_FILE[file]] = v.content
    return validate(raw)
