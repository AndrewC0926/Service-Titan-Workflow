"""LLM spend tracking and the daily budget kill switch.

Every Anthropic call records tokens and dollars to token_spend. Before each
call, the day's total is checked against llm.daily_budget_usd — past it, calls
raise BudgetExceeded and the pipeline stops spending. At budget_warn_fraction
the digest and dashboard warn. SCOUT_LLM_DISABLED=1 is the manual kill switch.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlmodel import select

from app.config import Config, load_config
from app.db import session_scope
from app.models import TokenSpend

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def price(cfg: Config, model: str, input_tokens: int, output_tokens: int) -> float:
    p = cfg.get("llm.prices", {}).get(model)
    if p is None:
        log.warning("no price configured for model %s; assuming Sonnet pricing", model)
        p = {"in": 3.0, "out": 15.0}
    return input_tokens / 1e6 * p["in"] + output_tokens / 1e6 * p["out"]


def record(stage: str, model: str, input_tokens: int, output_tokens: int) -> float:
    cfg = load_config()
    cost = price(cfg, model, input_tokens, output_tokens)
    try:
        with session_scope() as session:
            session.add(TokenSpend(day=_today(), stage=stage, model=model,
                                   input_tokens=input_tokens, output_tokens=output_tokens,
                                   cost_usd=cost))
    except Exception as exc:  # noqa: BLE001 — a logging failure must not kill extraction
        log.error("failed to record token spend: %s", exc)
    return cost


def spent_today() -> float:
    with session_scope() as session:
        rows = session.exec(select(TokenSpend).where(TokenSpend.day == _today())).all()
        return sum(r.cost_usd for r in rows)


def spent_month() -> float:
    prefix = _today()[:7]
    with session_scope() as session:
        rows = session.exec(select(TokenSpend)).all()
        return sum(r.cost_usd for r in rows if r.day.startswith(prefix))


def check_budget() -> None:
    """Raise BudgetExceeded past the daily cap; log a warning at the threshold."""
    if os.environ.get("SCOUT_LLM_DISABLED") == "1":
        raise BudgetExceeded("SCOUT_LLM_DISABLED=1 — manual kill switch is on")
    cfg = load_config()
    budget = cfg.get("llm.daily_budget_usd")
    if not budget:
        return
    spent = spent_today()
    if spent >= budget:
        raise BudgetExceeded(
            f"daily LLM budget exhausted: ${spent:.2f} of ${budget:.2f} — "
            f"raise llm.daily_budget_usd in config.yaml to continue today"
        )
    if spent >= budget * cfg.get("llm.budget_warn_fraction", 0.8):
        log.warning("LLM spend at %.0f%% of daily budget ($%.2f of $%.2f)",
                    100 * spent / budget, spent, budget)


def budget_status() -> dict:
    cfg = load_config()
    budget = cfg.get("llm.daily_budget_usd") or 0.0
    today = spent_today()
    return {
        "today_usd": round(today, 2),
        "month_usd": round(spent_month(), 2),
        "daily_budget_usd": budget,
        "warn": bool(budget) and today >= budget * cfg.get("llm.budget_warn_fraction", 0.8),
        "exhausted": bool(budget) and today >= budget,
    }
