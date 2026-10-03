"""HTTP fetching with retries, backoff, timeouts and a rate limit.

Ingestion code depends on the ``Fetcher`` protocol; tests use
``FixtureFetcher`` with recorded responses (no live network in unit tests).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, msg: str) -> None:
        super().__init__(f"{url}: {status} {msg}")
        self.url = url
        self.status = status


def _redact(text: str, params: Mapping[str, str] | None) -> str:
    """Remove query-parameter values (API keys, tokens) an error body may echo back.

    Error messages end up in ingest summaries in the journal (REVIEW R-07).
    """
    for v in (params or {}).values():
        if len(v) >= 8:
            text = text.replace(v, "***")
    return text


class Fetcher(Protocol):
    def get(self, url: str, params: Mapping[str, str] | None = None) -> bytes: ...


@dataclass
class RateLimiter:
    per_second: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last: float = field(default=-1e9, init=False)

    def wait(self) -> None:
        gap = 1.0 / self.per_second
        now = self.clock()
        delay = self._last + gap - now
        if delay > 0:
            self.sleep(delay)
            now += delay
        self._last = now


@dataclass
class HttpFetcher:
    """Live fetcher. Exponential backoff on 429/5xx and transport errors."""

    headers: Mapping[str, str] = field(default_factory=dict)
    per_second: float = 5.0
    timeout_s: float = 30.0
    max_retries: int = 5
    backoff_base_s: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    transport: httpx.BaseTransport | None = None
    _limiter: RateLimiter = field(init=False)
    _client: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self._limiter = RateLimiter(self.per_second, sleep=self.sleep)
        self._client = httpx.Client(
            headers=dict(self.headers),
            timeout=self.timeout_s,
            transport=self.transport,
            follow_redirects=True,
        )

    def get(self, url: str, params: Mapping[str, str] | None = None) -> bytes:
        last: FetchError | None = None
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            try:
                r = self._client.get(url, params=params)
            except httpx.TransportError as e:
                last = FetchError(url, None, type(e).__name__)
            else:
                if r.status_code == 200:
                    return r.content
                if r.status_code not in RETRY_STATUSES:
                    raise FetchError(url, r.status_code, _redact(r.text, params)[:200])
                last = FetchError(url, r.status_code, "retryable")
            if attempt < self.max_retries:
                self.sleep(self.backoff_base_s * (2**attempt))
        assert last is not None
        raise last


@dataclass
class FixtureFetcher:
    """Serves recorded responses: ``routes`` maps a URL substring to a file or bytes."""

    routes: Mapping[str, Path | bytes]
    calls: list[str] = field(default_factory=list)

    def get(self, url: str, params: Mapping[str, str] | None = None) -> bytes:
        full = url + (
            "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
        )
        self.calls.append(full)
        best = max((k for k in self.routes if k in full), key=len, default=None)
        if best is None:
            raise FetchError(full, 404, "no fixture recorded")
        v = self.routes[best]
        return v if isinstance(v, bytes) else Path(v).read_bytes()
