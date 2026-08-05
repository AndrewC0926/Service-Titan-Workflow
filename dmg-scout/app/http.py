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

# Retry budgets are per failure class, because "how long to keep trying" depends
# entirely on whether waiting can plausibly change the answer.
#
# THROTTLE — waiting is exactly the right response, so these get the full budget
# and honour Retry-After. 403 is here because CEQAnet answers 403 (not 429) when
# you burst it — verified live: a 12-request burst returns 403 around requests
# 9-10, then recovers.
THROTTLE_STATUS = {403, 429}
# SERVER_ERROR — worth a couple of attempts for a genuinely flaky backend, but
# a 5xx is very often deterministic. Legistar answers HTTP 500 for a client slug
# that does not exist; retrying that five times per slug across ten bad slugs is
# what turned one verify run into several minutes of guaranteed-useless waiting.
SERVER_ERROR_STATUS = {500, 502, 503, 504}
# NEVER_RETRY — the response is a fact about the request, not about the server's
# mood. Listed explicitly so this intent survives future edits; 404 in
# particular must never be retried. Note 403 is deliberately absent: it is
# treated as a throttle above.
NEVER_RETRY_STATUS = {400, 401, 404, 405, 406, 409, 410, 422}

RETRYABLE_STATUS = THROTTLE_STATUS | SERVER_ERROR_STATUS


class RobotsDisallowed(Exception):
    pass


class PoliteClient:
    def __init__(self, user_agent: str | None = None, interval: float | None = None,
                 max_retries: int | None = None, respect_robots: bool = True,
                 recorder=None, max_retries_5xx: int | None = None):
        cfg = load_config()
        self.user_agent = user_agent or cfg.get("user_agent")
        self.interval = interval if interval is not None else cfg.get("request_interval_seconds", 2.0)
        # Budget for throttles and transport errors, where waiting helps.
        self.max_retries = max_retries if max_retries is not None else cfg.get("request_max_retries", 4)
        # Separate, much smaller budget for 5xx — see SERVER_ERROR_STATUS.
        self.max_retries_5xx = (max_retries_5xx if max_retries_5xx is not None
                                else cfg.get("request_max_retries_5xx", 2))
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
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        """Needed for Workday's job-board API, which only answers POST."""
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        parts = urlsplit(url)
        host = parts.netloc
        if self.respect_robots:
            rp = self._robots_for(host, parts.scheme)
            if rp is not None and not rp.can_fetch(self.user_agent, url):
                raise RobotsDisallowed(f"robots.txt disallows {url}")

        delay = 2.0
        attempt = 0
        last_exc: Exception | None = None
        while True:
            self._throttle(host)
            started = time.monotonic()
            attempt += 1
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                # Transport-level failure: no status to reason about, so treat it
                # as transient and use the full budget.
                last_exc = exc
                budget = self.max_retries
                self._record(url, None, False, started, None, str(exc))
                log.warning("%s %s failed (%s), attempt %d/%d",
                            method, url, exc, attempt, budget + 1)
            else:
                self._record(url, resp.status_code, resp.is_success, started,
                             len(resp.content), None)
                status = resp.status_code
                if status in NEVER_RETRY_STATUS or status not in RETRYABLE_STATUS:
                    # Either a definitive answer or a success — either way, done.
                    resp.raise_for_status()
                    return resp
                # max_retries is the global ceiling; 5xx gets a tighter budget
                # on top of it, so passing max_retries=0 still means no retries.
                budget = (min(self.max_retries_5xx, self.max_retries)
                          if status in SERVER_ERROR_STATUS else self.max_retries)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {status}", request=resp.request, response=resp
                )
                log.warning("%s %s -> %d, attempt %d/%d",
                            method, url, status, attempt, budget + 1)
            if attempt > budget:
                raise last_exc
            time.sleep(delay)
            delay *= 2

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

    def post_json(self, url: str, **kwargs):
        return self.post(url, **kwargs).json()

    def get_text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text

    def get_bytes(self, url: str, **kwargs) -> bytes:
        return self.get(url, **kwargs).content
