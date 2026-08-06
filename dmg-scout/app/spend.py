"""LLM spend tracking, the daily budget kill switch, and the per-run budget.

Every Anthropic call records tokens and dollars to token_spend. Before each
call, the day's total is checked against llm.daily_budget_usd — past it, calls
raise BudgetExceeded and the pipeline stops spending. At budget_warn_fraction
the digest and dashboard warn. SCOUT_LLM_DISABLED=1 is the manual kill switch.

**The daily cap alone is not a guard on a long run.** It is keyed on the UTC
calendar day, so a run that crosses midnight gets a fresh allowance mid-flight:
`spent_today()` drops back to nearly zero at 00:00 and the kill switch silently
re-arms at full budget. A run started at 23:50 could therefore spend the daily
cap twice without a single check firing. That is not hypothetical — the resolve
of 2026-08-05 ran into 08-06 and did exactly this.

So a run carries its OWN budget for its lifetime, via `run_budget()`. It counts
only the spend of calls made inside it, it never consults the clock, and its cap
is fixed the moment the run opens at:

    min(llm.run_budget_usd, daily budget REMAINING when the run started)

Freezing the daily remainder at run start is the half that closes the rollover
hole: the run may spend what the day had left when it began, and midnight cannot
top that up. The daily check still runs too, so short runs behave as before.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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


@dataclass
class RunBudget:
    """One pipeline run's own budget, immune to the calendar.

    `cap_usd` is fixed at open and never recomputed — that is the entire point.
    `spent_usd` accumulates only calls made inside this run.
    """
    name: str
    cap_usd: float
    started_day: str
    started_spent_today: float
    spent_usd: float = 0.0
    calls: int = 0
    warned: bool = field(default=False, repr=False)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.cap_usd


_active_run: ContextVar[RunBudget | None] = ContextVar("_active_run", default=None)


def active_run() -> RunBudget | None:
    return _active_run.get()


@contextmanager
def run_budget(name: str, cap_usd: float | None = None) -> Iterator[RunBudget]:
    """Give this run its own budget for its lifetime.

    The cap is the smaller of llm.run_budget_usd (or `cap_usd`) and whatever the
    daily budget had left at the instant the run opened. Both halves are read
    once, here, and then never again — so a midnight rollover during the run
    cannot raise either one.

    Nested calls reuse the outer run rather than opening a second budget, so a
    `scout pipeline` that calls triage, extract and resolve is ONE run against
    ONE cap, not three.
    """
    existing = _active_run.get()
    if existing is not None:
        yield existing
        return

    cfg = load_config()
    day = _today()
    daily = cfg.get("llm.daily_budget_usd") or float("inf")
    try:
        already = spent_today()
    except Exception as exc:  # noqa: BLE001
        # Same principle as record(): a DB problem must not be the thing that
        # decides how much we may spend. We cannot read the day's remainder, so
        # fall back to the configured run cap alone — still a bound, just a
        # blinder one — and say so.
        log.error("cannot read today's spend (%s); run %r capped by "
                  "llm.run_budget_usd alone", exc, name)
        already = 0.0
        daily = float("inf")
    configured = cap_usd if cap_usd is not None else cfg.get("llm.run_budget_usd")
    run_cap = float(configured) if configured else float("inf")
    cap = min(run_cap, max(0.0, daily - already))

    run = RunBudget(name=name, cap_usd=cap, started_day=day, started_spent_today=already)
    log.info("run %r budget: $%.2f (daily $%.2f had $%.2f left; run cap $%s)",
             name, cap, daily, max(0.0, daily - already),
             f"{run_cap:.2f}" if run_cap != float("inf") else "unset")
    token = _active_run.set(run)
    try:
        yield run
    finally:
        _active_run.reset(token)
        log.info("run %r spent $%.4f over %d LLM calls (cap $%.2f)",
                 name, run.spent_usd, run.calls, cap)


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
    # Charged to the run even if the DB write above failed. The run budget is a
    # safety guard, and a guard that stops counting when logging breaks is worse
    # than no guard: that is precisely when spend runs away unobserved.
    run = _active_run.get()
    if run is not None:
        run.spent_usd += cost
        run.calls += 1
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
    """Raise BudgetExceeded past the run cap or the daily cap.

    The run cap is checked FIRST and is the one that holds across midnight. The
    daily check below is kept for calls made outside any run, and is deliberately
    left clock-keyed — it is a per-day ceiling and that is what it should mean.
    """
    if os.environ.get("SCOUT_LLM_DISABLED") == "1":
        raise BudgetExceeded("SCOUT_LLM_DISABLED=1 — manual kill switch is on")

    run = _active_run.get()
    if run is not None:
        if run.exhausted:
            raise BudgetExceeded(
                f"run budget exhausted: {run.name!r} spent ${run.spent_usd:.2f} of "
                f"${run.cap_usd:.2f} over {run.calls} calls — this cap was fixed when "
                f"the run started and does not reset at midnight. Raise "
                f"llm.run_budget_usd (or llm.daily_budget_usd) and re-run; resolve, "
                f"triage and extract all resume from where they stopped."
            )
        cfg = load_config()
        warn_at = cfg.get("llm.budget_warn_fraction", 0.8)
        if not run.warned and run.cap_usd and run.spent_usd >= run.cap_usd * warn_at:
            run.warned = True
            log.warning("run %r at %.0f%% of its budget ($%.2f of $%.2f)",
                        run.name, 100 * run.spent_usd / run.cap_usd,
                        run.spent_usd, run.cap_usd)

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
    st = {
        "today_usd": round(today, 2),
        "month_usd": round(spent_month(), 2),
        "daily_budget_usd": budget,
        "run_budget_usd": cfg.get("llm.run_budget_usd"),
        "warn": bool(budget) and today >= budget * cfg.get("llm.budget_warn_fraction", 0.8),
        "exhausted": bool(budget) and today >= budget,
    }
    run = _active_run.get()
    if run is not None:
        st["run"] = {"name": run.name, "spent_usd": round(run.spent_usd, 4),
                     "cap_usd": round(run.cap_usd, 2), "calls": run.calls,
                     "exhausted": run.exhausted}
    return st
