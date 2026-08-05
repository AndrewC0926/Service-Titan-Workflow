"""Gate 4: budget kill switch, spend math, http archive, doctor, dead man's switch."""
import httpx
import pytest
import respx
from sqlmodel import select

import app.spend as spend_mod
from app.models import HttpLog, TokenSpend
from app.spend import BudgetExceeded, budget_status, check_budget, price, record


def test_price_math(cfg):
    # Haiku: 1M in @ $1 + 100k out @ $5 = $1.50
    assert abs(price(cfg, "claude-haiku-4-5-20251001", 1_000_000, 100_000) - 1.5) < 1e-9
    # Sonnet: 100k in @ $3 + 10k out @ $15 = $0.45
    assert abs(price(cfg, "claude-sonnet-4-6", 100_000, 10_000) - 0.45) < 1e-9


def test_record_and_budget_flow(db_session, cfg):
    record("triage", "claude-haiku-4-5-20251001", 500_000, 20_000)
    rows = db_session.exec(select(TokenSpend)).all()
    assert len(rows) == 1 and rows[0].stage == "triage"
    st = budget_status()
    assert st["today_usd"] > 0 and not st["exhausted"]
    check_budget()  # under budget: no raise


def _input_tokens_for(cfg, dollars: float, model: str = "claude-sonnet-4-6") -> int:
    """Input tokens that cost roughly `dollars` on `model`.

    Derived from config rather than hardcoded: these tests used fixed amounts tied
    to a $15 cap and broke the moment the cap moved to $40, which tested the
    constant instead of the kill switch.
    """
    per_mtok = cfg.get("llm.prices", {}).get(model, {}).get("in", 3.0)
    return int(dollars / per_mtok * 1_000_000)


def test_budget_kill_switch(db_session, cfg):
    budget = cfg.get("llm.daily_budget_usd", 15.0)
    record("extract", "claude-sonnet-4-6", _input_tokens_for(cfg, budget * 1.2), 0)
    with pytest.raises(BudgetExceeded):
        check_budget()
    assert budget_status()["exhausted"]


def test_manual_kill_switch(db_session, monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_DISABLED", "1")
    with pytest.raises(BudgetExceeded, match="manual kill switch"):
        check_budget()


def test_warn_threshold(db_session, cfg, caplog):
    budget = cfg.get("llm.daily_budget_usd", 15.0)
    warn_at = cfg.get("llm.budget_warn_fraction", 0.8)
    # Between the warn threshold and the cap, wherever those currently sit.
    spend = budget * (warn_at + (1.0 - warn_at) / 2)
    record("extract", "claude-sonnet-4-6", _input_tokens_for(cfg, spend), 0)
    with caplog.at_level("WARNING"):
        check_budget()
    assert any("daily budget" in r.message for r in caplog.records)
    assert budget_status()["warn"] and not budget_status()["exhausted"]


@respx.mock
def test_http_archive_records_requests(db_session, cfg, monkeypatch):
    from app.http import PoliteClient
    from app.pipeline.fetch import run_fetch
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(503))
    orig = PoliteClient.__init__

    def fast_init(self, *a, **k):
        k.update(interval=0, max_retries=1, respect_robots=False)
        orig(self, *a, **k)
    monkeypatch.setattr(PoliteClient, "__init__", fast_init)

    run_fetch(db_session, cfg, only_source="rss")
    logs = db_session.exec(select(HttpLog)).all()
    assert logs, "every fetch request must be archived"
    assert all(l.status == 503 and not l.ok for l in logs)
    assert logs[0].source_run_id is not None


def test_dead_mans_switch_disarmed_without_env(monkeypatch, caplog):
    from app.ops import ping_healthcheck
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    with caplog.at_level("WARNING"):
        assert ping_healthcheck() is False
    assert any("DISARMED" in r.message for r in caplog.records)


@respx.mock
def test_dead_mans_switch_pings(monkeypatch):
    from app.ops import ping_healthcheck
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc123")
    ok_route = respx.get("https://hc-ping.com/abc123").mock(return_value=httpx.Response(200))
    fail_route = respx.get("https://hc-ping.com/abc123/fail").mock(return_value=httpx.Response(200))
    assert ping_healthcheck(success=True)
    assert ok_route.called
    assert ping_healthcheck(success=False)
    assert fail_route.called


def test_doctor_reports_missing_pieces(db_session, monkeypatch):
    from app.ops import doctor
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HEALTHCHECK_URL", raising=False)
    checks = dict((name, (ok, detail)) for name, ok, detail in doctor())
    assert checks["database"][0] is True
    assert checks["anthropic_api_key"][0] is False
    assert checks["dead_mans_switch"][0] is False
    assert checks["source:ceqanet"][0] is False  # no successful run yet
    assert checks["llm_budget"][0] is True
