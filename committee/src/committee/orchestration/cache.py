"""Response cache so re-running a stage with the same inputs reuses its outputs
(DESIGN 8: idempotent). Keyed by SHA-256 of the full request (model, system
blocks, packet, follow-ups). A cache hit reports zero tokens and cost."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from committee.agents.llm import LLMClient, LLMRequest, LLMResponse, Usage


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
    def __init__(self, inner: LLMClient, path: Path | str) -> None:
        self.inner = inner
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
            if row:
                self.hits += 1
                return LLMResponse(
                    text=row[1], model_id=row[0], usage=Usage(), stop_reason=row[2], latency_s=0.0
                )
        resp = self.inner.complete(request)
        with self._lock:
            self.con.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?)",
                (k, resp.model_id, resp.text, resp.stop_reason),
            )
        return resp
