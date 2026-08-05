from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime  # noqa: F401 — used in signatures
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
    def fetch(self, cfg: Config, client: PoliteClient, since: "datetime | None" = None
              ) -> Iterator[FetchedDoc]:
        """Yield new documents. `since` overrides the configured lookback for
        backfills; adapters without date filtering may ignore it. Must be safe
        to re-run: yielding an already-seen source_uid is fine (the pipeline
        dedupes); crashing on one is not."""

    def backfill_chunks(self, cfg: Config, since: "datetime") -> list[dict]:
        """Split a historical pull into resumable chunks. Each chunk dict needs
        a stable 'key' plus whatever fetch_chunk() needs. Default: one chunk."""
        return [{"key": f"{since:%Y-%m-%d}:all"}]

    def fetch_chunk(self, cfg: Config, client: PoliteClient, since: "datetime",
                    chunk: dict) -> Iterator[FetchedDoc]:
        """Fetch one backfill chunk. Default: the whole range."""
        return self.fetch(cfg, client, since=since)

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """Live smoke-test: hit the source, return {status, detail}. Used by
        `scout verify-sources` after deploy to catch URL-structure drift.

        Three states, because "reachable" and "working" are not the same thing
        and conflating them is how a source sits broken for weeks:

          ok   — returned at least one document
          warn — every request succeeded but nothing survived filtering; the
                 endpoint is alive and the result may be legitimately empty
          fail — requests failed, or (see verify_targets) enough of a source's
                 configured sub-targets failed that the source is not usable

        Adapters that fan out over many sub-targets (Legistar clients, ATS
        boards) should override verify() via verify_targets() so a partial
        outage cannot be reported as success.
        """
        try:
            doc = next(iter(self.fetch(cfg, client)), None)
        except Exception as exc:  # noqa: BLE001 — report, don't crash the verify loop
            return {"status": "fail", "detail": f"{type(exc).__name__}: {exc}"}
        if doc is None:
            return {"status": "warn",
                    "detail": "reachable, but returned zero documents (check filters)"}
        return {"status": "ok", "detail": f"first doc: {doc.title[:80]!r} ({doc.url})"}


@dataclass
class TargetResult:
    """Outcome of probing one sub-target (a Legistar client, an ATS board)."""
    name: str
    ok: bool
    detail: str = ""
    records: int = 0   # raw records the target returned, pre-filter
    docs: int = 0      # records that survived keyword/geo filtering


def fanout_verify(kind: str, results: list[TargetResult],
                  min_ok_fraction: float = 0.5) -> dict:
    """Roll per-target probes into one source status.

    A source that fans out over N configured targets is only as good as those
    targets. If the majority fail it is FAIL, not "reachable" — that silent
    mode is what let ten broken Legistar slugs report OK. Any single failing
    target still downgrades to WARN, so a dead slug stays visible until it is
    fixed or removed.

    `records` (raw, pre-filter) is reported separately from `docs` (post-filter)
    so an empty result is attributable: 0 records means the endpoint gave us
    nothing, 0 docs from many records means the filters are simply selective.
    """
    total = len(results)
    if not total:
        return {"status": "fail", "detail": f"no {kind} configured"}

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    records = sum(r.records for r in results)
    docs = sum(r.docs for r in results)

    parts = [f"{len(good)}/{total} {kind} ok",
             f"{records} records scanned",
             f"{docs} matched"]
    if bad:
        parts.append("FAILING: " + ", ".join(f"{r.name} [{r.detail}]" for r in bad))
    detail = "; ".join(parts)

    if not good or (len(good) / total) < min_ok_fraction:
        return {"status": "fail", "detail": detail}
    if bad or docs == 0:
        return {"status": "warn", "detail": detail}
    return {"status": "ok", "detail": detail}


def keyword_match(text: str, cfg: Config) -> bool:
    """Cheap pre-LLM filter: does this text mention data centers at all?"""
    lower = text.lower()
    return any(kw in lower for kw in cfg.get("keywords.data_center", []))
