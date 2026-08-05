"""Retry budgets and honest source status.

Both behaviours here are regressions from the first live verify-sources run:

* Five retries with exponential backoff were spent on deterministic HTTP 500s
  (Legistar answers 500 for a client slug that does not exist), turning one
  command into minutes of guaranteed-useless waiting.
* `verify-sources` printed [OK] for Legistar while ten of twelve configured
  clients were returning 500 and the source produced zero documents.
"""
import httpx
import pytest
import respx

from app.http import (
    NEVER_RETRY_STATUS, SERVER_ERROR_STATUS, THROTTLE_STATUS, PoliteClient,
)
from app.sources.base import TargetResult, fanout_verify


def client(**kw) -> PoliteClient:
    kw.setdefault("interval", 0)
    kw.setdefault("respect_robots", False)
    return PoliteClient(**kw)


# ---- retry budgets ----------------------------------------------------------

@respx.mock
def test_404_is_never_retried(monkeypatch):
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)
    route = respx.get("https://example.test/gone").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        client().get("https://example.test/gone")
    assert route.call_count == 1, "a 404 is a fact about the request; retrying cannot help"


@respx.mock
def test_5xx_is_retried_exactly_twice(monkeypatch):
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)
    route = respx.get("https://example.test/boom").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client(max_retries=4, max_retries_5xx=2).get("https://example.test/boom")
    assert route.call_count == 3, "2 retries = 3 attempts total, not 5"


@respx.mock
def test_throttle_keeps_the_full_budget(monkeypatch):
    """403/429 are the one class where waiting genuinely changes the answer."""
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)
    route = respx.get("https://example.test/slow").mock(return_value=httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        client(max_retries=4, max_retries_5xx=2).get("https://example.test/slow")
    assert route.call_count == 5


@respx.mock
def test_403_recovers_after_throttle(monkeypatch):
    """CEQAnet answers 403 (not 429) when bursted, then recovers."""
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)
    responses = [httpx.Response(403), httpx.Response(403), httpx.Response(200, text="ok")]
    route = respx.get("https://example.test/csv").mock(side_effect=responses)
    assert client(max_retries=4).get_text("https://example.test/csv") == "ok"
    assert route.call_count == 3


@respx.mock
def test_global_ceiling_clamps_5xx_budget(monkeypatch):
    monkeypatch.setattr("app.http.time.sleep", lambda s: None)
    route = respx.get("https://example.test/boom").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client(max_retries=0, max_retries_5xx=2).get("https://example.test/boom")
    assert route.call_count == 1, "max_retries is the global ceiling"


def test_status_classes_are_disjoint_and_404_is_terminal():
    assert 404 in NEVER_RETRY_STATUS
    assert not (NEVER_RETRY_STATUS & SERVER_ERROR_STATUS)
    assert not (NEVER_RETRY_STATUS & THROTTLE_STATUS)
    assert 403 in THROTTLE_STATUS and 403 not in NEVER_RETRY_STATUS


# ---- honest status ---------------------------------------------------------

def test_majority_failure_is_fail_not_ok():
    """The exact reported shape: 10 of 12 Legistar clients dead."""
    results = ([TargetResult(f"dead{i}", False, "HTTP 500") for i in range(10)]
               + [TargetResult("fontana", True, records=96, docs=0),
                  TargetResult("sanbernardino", True, records=96, docs=0)])
    out = fanout_verify("clients", results)
    assert out["status"] == "fail"
    assert "2/12 clients ok" in out["detail"]
    assert "dead0" in out["detail"], "failing targets must be named, not just counted"


def test_all_targets_ok_but_zero_documents_is_warn():
    """Reachable is not the same as working — this must not print OK."""
    out = fanout_verify("clients", [TargetResult("a", True, records=50, docs=0),
                                    TargetResult("b", True, records=50, docs=0)])
    assert out["status"] == "warn"
    assert "100 records scanned" in out["detail"] and "0 matched" in out["detail"]


def test_one_dead_target_downgrades_to_warn():
    """A single dead slug stays visible until it is fixed or removed."""
    out = fanout_verify("clients", [TargetResult("a", True, records=5, docs=1),
                                    TargetResult("b", True, records=5, docs=1),
                                    TargetResult("dead", False, "HTTP 500")])
    assert out["status"] == "warn"
    assert "dead" in out["detail"]


def test_ok_requires_documents_and_no_failures():
    out = fanout_verify("clients", [TargetResult("a", True, records=5, docs=2),
                                    TargetResult("b", True, records=5, docs=1)])
    assert out["status"] == "ok"


def test_no_targets_configured_is_fail():
    assert fanout_verify("clients", [])["status"] == "fail"


def test_exactly_half_failing_is_not_a_majority():
    """Threshold check: 'majority failing' means strictly more than half."""
    results = [TargetResult("a", True, records=1, docs=1),
               TargetResult("b", True, records=1, docs=1),
               TargetResult("c", False, "HTTP 500"),
               TargetResult("d", False, "HTTP 500")]
    assert fanout_verify("clients", results)["status"] == "warn"
