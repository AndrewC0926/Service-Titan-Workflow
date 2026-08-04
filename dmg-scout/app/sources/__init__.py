"""Source adapter registry. Adapters register by name; config enables/disables them."""
from __future__ import annotations

from app.sources.base import SourceAdapter, registry  # noqa: F401

# Importing modules registers their adapters.
from app.sources import ats, ceqanet, edgar, goed, legistar, rss  # noqa: E402,F401


def get_adapter(name: str) -> SourceAdapter:
    if name not in registry:
        raise KeyError(f"unknown source adapter: {name!r}; known: {sorted(registry)}")
    return registry[name]()


def enabled_adapters(cfg) -> list[str]:
    return [name for name in registry if cfg.source_enabled(name)]
