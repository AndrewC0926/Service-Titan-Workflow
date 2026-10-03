"""Response cache so re-running a stage with the same inputs reuses its outputs
(DESIGN 8: idempotent). Keyed by SHA-256 of the full request (model, system
blocks, packet, follow-ups). A cache hit reports zero tokens and cost.

Integrity (review finding R-17): a cached reply is served only if its SHA-256
matches the ``raw_output_sha256`` of an ``agent_output`` already in the
hash-chained journal for the same agent, or a reply this process fetched live.
A tampered cache row is therefore a miss, never a forged agent output."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from committee.agents.llm import LLMClient, LLMRequest, LLMResponse, Usage
from committee.journal.store import Journal


def request_key(r: LLMRequest) -> str:
    body = json.dumps(
        {
            "model": r.model,
            "system": list(r.system_blocks),
            "packet": r.packet,
            "instruction": r.instruction,
            "followups": [list(f) for f in r.followups],
            "max_tokens": r.max_tokens,
        },
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


class CachingClient:
    def __init__(self, inner: LLMClient, path: Path | str, journal: Journal | None = None) -> None:
        self.inner = inner
        # Loaded once on the calling thread (the journal connection is not thread-safe).
        self._trusted: set[tuple[str, str]] = (
            {
                (str(e.payload.get("agent")), str(e.payload.get("raw_output_sha256")))
                for e in journal.entries("agent_output")
            }
            if journal is not None
            else set()
        )
        self.rejected = 0
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model_id TEXT, text TEXT, stop_reason TEXT)"
        )
        self.hits = 0
        self._lock = threading.Lock()

    def complete(self, request: LLMRequest) -> LLMResponse:
        k = request_key(request)
        with self._lock:
            row = self.con.execute(
                "SELECT model_id, text, stop_reason FROM llm_cache WHERE key=?", (k,)
            ).fetchone()
            digest = hashlib.sha256(row[1].encode()).hexdigest() if row else ""
            if row and (request.agent, digest) not in self._trusted:
                self.rejected += 1  # not backed by the journal: treat as a miss
                row = None
            if row:
                self.hits += 1
                return LLMResponse(
                    text=row[1], model_id=row[0], usage=Usage(), stop_reason=row[2], latency_s=0.0
                )
        resp = self.inner.complete(request)
        with self._lock:
            self._trusted.add((request.agent, hashlib.sha256(resp.text.encode()).hexdigest()))
            self.con.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?)",
                (k, resp.model_id, resp.text, resp.stop_reason),
            )
        return resp
