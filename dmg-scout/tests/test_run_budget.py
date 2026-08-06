"""The per-run budget, and the midnight rollover it exists to close.

The daily cap is keyed on the UTC calendar day. A run that crosses midnight sees
`spent_today()` fall back to zero and the kill switch silently re-arm at full
budget — so a long job can spend the daily cap twice without a check firing. That
happened: the resolve of 2026-08-05 ran into 08-06 and did exactly this.

test_run_budget_survives_midnight_rollover is the regression test. It asserts
the old failure directly — `spent_today() == 0.0` after the rollover, which is
exactly what the clock-keyed guard would have seen and waved through — and then
asserts that the run stays stopped anyway.
"""
import pytest

import app.spend as spend_mod
from app.spend import (
    BudgetExceeded,
    active_run,
    budget_status,
    check_budget,
    record,
    run_budget,
)


def _tokens_for(cfg, dollars: float, model: str = "claude-sonnet-4-6") -> int:
    per_mtok = cfg.get("llm.prices", {}).get(model, {}).get("in", 3.0)
    return int(dollars / per_mtok * 1_000_000)


def _spend(cfg, dollars: float) -> None:
    record("extract", "claude-sonnet-4-6", _tokens_for(cfg, dollars), 0)


def test_run_budget_accumulates_and_stops(db_session, cfg):
    with run_budget("test", cap_usd=1.0) as run:
        _spend(cfg, 0.4)
        assert run.calls == 1
        assert 0.39 < run.spent_usd < 0.41
        check_budget()  # still under
        _spend(cfg, 0.7)
        assert run.exhausted
        with pytest.raises(BudgetExceeded, match="run budget exhausted"):
            check_budget()


def test_run_budget_survives_midnight_rollover(db_session, cfg, monkeypatch):
    """The regression. A run that crosses midnight keeps its own cap.

    The daily total resets when the clock rolls over — that is correct for a daily
    ceiling — but the RUN has already spent its allowance and must stay stopped.
    """
    day = {"v": "2026-08-05"}
    monkeypatch.setattr(spend_mod, "_today", lambda: day["v"])

    with run_budget("crosses-midnight", cap_usd=1.0):
        _spend(cfg, 1.2)
        with pytest.raises(BudgetExceeded, match="run budget exhausted"):
            check_budget()

        # 00:00 UTC. A fresh calendar day, so the daily total drops to zero...
        day["v"] = "2026-08-06"
        assert spend_mod.spent_today() == 0.0

        # ...and the run is still stopped. This is the whole point.
        with pytest.raises(BudgetExceeded, match="does not reset at midnight"):
            check_budget()


def test_daily_remainder_is_frozen_at_run_start(db_session, cfg, monkeypatch):
    """The run may spend what the day had left when it opened — and no more.

    Without freezing, a run opened at 23:50 with $1 of daily budget left would be
    handed the full daily cap ten minutes later.
    """
    monkeypatch.setattr(spend_mod, "_today", lambda: "2026-08-05")
    daily = cfg.get("llm.daily_budget_usd", 15.0)
    _spend(cfg, daily - 1.0)  # $1 of the day remains

    with run_budget("late-night", cap_usd=100.0) as run:
        assert 0.9 < run.cap_usd < 1.1  # capped by the daily remainder, not by 100
        _spend(cfg, 1.5)
        with pytest.raises(BudgetExceeded):
            check_budget()


def test_run_cap_binds_when_lower_than_daily_remainder(db_session, cfg):
    with run_budget("small", cap_usd=0.5) as run:
        assert run.cap_usd == 0.5


def test_nested_run_budget_reuses_the_outer_run(db_session, cfg):
    """`scout pipeline` is one run against one cap, not seven."""
    with run_budget("pipeline", cap_usd=2.0) as outer:
        with run_budget("triage", cap_usd=99.0) as inner:
            assert inner is outer
            _spend(cfg, 0.3)
        with run_budget("extract", cap_usd=99.0) as inner2:
            assert inner2 is outer
            _spend(cfg, 0.3)
        assert outer.calls == 2
        assert 0.55 < outer.spent_usd < 0.65


def test_run_is_charged_even_if_the_spend_write_fails(db_session, cfg, monkeypatch):
    """A guard that stops counting when logging breaks is worse than no guard."""
    def boom(*a, **k):
        raise RuntimeError("db is down")

    with run_budget("logging-broken", cap_usd=1.0) as run:
        # The DB dies after the run has opened, mid-flight.
        monkeypatch.setattr(spend_mod, "session_scope", boom)
        _spend(cfg, 1.5)
        assert run.spent_usd > 1.0
        with pytest.raises(BudgetExceeded, match="run budget exhausted"):
            check_budget()


def test_run_opens_with_a_cap_even_if_the_db_is_unreadable(db_session, cfg, monkeypatch):
    """A DB blip must not decide how much we may spend — but a cap must still exist."""
    def boom(*a, **k):
        raise RuntimeError("db is down")
    monkeypatch.setattr(spend_mod, "session_scope", boom)

    with run_budget("db-down", cap_usd=0.75) as run:
        assert run.cap_usd == 0.75


def test_no_active_run_outside_the_context(db_session, cfg):
    assert active_run() is None
    with run_budget("brief", cap_usd=1.0):
        assert active_run() is not None
    assert active_run() is None


def test_budget_status_reports_the_active_run(db_session, cfg):
    with run_budget("visible", cap_usd=2.0):
        _spend(cfg, 0.5)
        st = budget_status()
        assert st["run"]["name"] == "visible"
        assert st["run"]["calls"] == 1
        assert not st["run"]["exhausted"]
    assert "run" not in budget_status()
