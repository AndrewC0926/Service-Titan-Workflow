from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Type

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType


@dataclass
class FetchedDoc:
    """One document pulled from a source, before dedupe/triage."""
    source: str
    source_uid: str          # stable source-native ID
    url: str
    title: str
    raw_text: str
    published_at: datetime | None = None
    meta: dict = field(default_factory=dict)
    # Adapter's best guess at the signal type this document represents.
    default_signal_type: SignalType = SignalType.news_report
    # Set True when the adapter already knows this is on-topic (e.g. keyword-matched
    # agenda item); triage still runs unless skip_triage is set.
    skip_triage: bool = False

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.raw_text.encode("utf-8", errors="replace")).hexdigest()


class SourceFailure(Exception):
    """Raised when a source produced nothing AND every request failed — a total
    outage must surface as a failed run, never as 'ok, 0 records'."""


registry: dict[str, Type["SourceAdapter"]] = {}


class SourceAdapter(ABC):
    """One public data source. fetch() yields documents; the pipeline handles
    dedupe, storage, and everything downstream."""

    name: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            registry[cls.name] = cls

    @abstractmethod
    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        """Yield new documents. Must be safe to re-run: yielding an already-seen
        source_uid is fine (the pipeline dedupes); crashing on one is not."""

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """Live smoke-test: hit the source, return {ok, detail}. Used by
        `scout verify-sources` after deploy to catch URL-structure drift."""
        try:
            doc = next(iter(self.fetch(cfg, client)), None)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the verify loop
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        if doc is None:
            return {"ok": True, "detail": "reachable, but returned zero documents (check filters)"}
        return {"ok": True, "detail": f"first doc: {doc.title[:80]!r} ({doc.url})"}


def keyword_match(text: str, cfg: Config) -> bool:
    """Cheap pre-LLM filter: does this text mention data centers at all?"""
    lower = text.lower()
    return any(kw in lower for kw in cfg.get("keywords.data_center", []))
