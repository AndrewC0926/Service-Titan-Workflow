"""Polite HTTP client: per-domain rate limiting, robots.txt respect, exponential backoff.

All adapters go through PoliteClient. It refuses paths disallowed by robots.txt
and raises RobotsDisallowed so the source run records the reason loudly.
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

from app.config import load_config

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    pass


class PoliteClient:
    def __init__(self, user_agent: str | None = None, interval: float | None = None,
                 max_retries: int | None = None, respect_robots: bool = True,
                 recorder=None):
        cfg = load_config()
        self.user_agent = user_agent or cfg.get("user_agent")
        self.interval = interval if interval is not None else cfg.get("request_interval_seconds", 2.0)
        self.max_retries = max_retries if max_retries is not None else cfg.get("request_max_retries", 4)
        self.respect_robots = respect_robots
        # recorder(url, status, ok, elapsed_ms, response_bytes, error) — request archive hook
        self.recorder = recorder
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _robots_for(self, host: str, scheme: str) -> urllib.robotparser.RobotFileParser | None:
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self._client.get(f"{scheme}://{host}/robots.txt")
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # no robots.txt -> everything allowed
        except httpx.HTTPError:
            rp = None
        self._robots[host] = rp
        return rp

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        parts = urlsplit(url)
        host = parts.netloc
        if self.respect_robots:
            rp = self._robots_for(host, parts.scheme)
            if rp is not None and not rp.can_fetch(self.user_agent, url):
                raise RobotsDisallowed(f"robots.txt disallows {url}")

        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(host)
            started = time.monotonic()
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_exc = exc
                self._record(url, None, False, started, None, str(exc))
                log.warning("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
            else:
                self._record(url, resp.status_code, resp.is_success, started,
                             len(resp.content), None)
                if resp.status_code not in RETRYABLE_STATUS:
                    resp.raise_for_status()
                    return resp
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
                log.warning("GET %s -> %d, attempt %d", url, resp.status_code, attempt + 1)
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        raise last_exc  # type: ignore[misc]

    def _record(self, url: str, status, ok: bool, started: float, nbytes, error) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder(url=url, status=status, ok=ok,
                          elapsed_ms=int((time.monotonic() - started) * 1000),
                          response_bytes=nbytes, error=error[:300] if error else None)
        except Exception as exc:  # noqa: BLE001 — archiving must never break a fetch
            log.debug("http recorder failed: %s", exc)

    def get_json(self, url: str, **kwargs):
        return self.get(url, **kwargs).json()

    def get_text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text

    def get_bytes(self, url: str, **kwargs) -> bytes:
        return self.get(url, **kwargs).content
