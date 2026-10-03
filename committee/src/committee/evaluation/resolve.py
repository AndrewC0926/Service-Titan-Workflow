"""Resolve pre-registered forecasts from prices once their horizon has passed
(DESIGN 10). Resolution is code; nothing is resolved early."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence

from committee.evaluation.forecasts import resolve_from_path
from committee.journal.store import Journal

PricePath = Callable[[str, dt.date, dt.date], Sequence[float]]  # symbol, start, end -> closes


def add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(year, month, day)
        except ValueError:
            continue
    raise ValueError(d)


def thesis_symbols(journal: Journal) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in journal.entries("state_transition"):
        if e.payload.get("symbol"):
            out[str(e.payload["review_id"])] = str(e.payload["symbol"])
    return out


def resolve_due(
    journal: Journal,
    today: dt.date,
    path: PricePath,
    benchmark: str = "SPY",
    cost_bps: float = 100.0,
) -> int:
    done = {int(e.payload["forecast_seq"]) for e in journal.entries("forecast_resolution")}
    syms = thesis_symbols(journal)
    n = 0
    for f in list(journal.entries("forecast")):
        if f.seq in done:
            continue
        p = f.payload
        start = f.created_at.date()
        end = add_months(start, int(p["horizon_months"]))
        sym = syms.get(str(p["thesis_id"]))
        if end > today or sym is None:
            continue
        prices = list(path(sym, start, end))
        bench = list(path(benchmark, start, end))
        if len(prices) < 2 or (p["event"] == "beats_benchmark" and len(bench) < 2):
            continue  # data gap: retry next run rather than guess
        bret = bench[-1] / bench[0] - 1 if len(bench) >= 2 else 0.0
        outcome = resolve_from_path(prices, str(p["event"]), bench_ret=bret, cost_bps=cost_bps)
        journal.append(
            "forecast_resolution",
            {
                "forecast_seq": f.seq,
                "thesis_id": p["thesis_id"],
                "agent": p["agent"],
                "event": p["event"],
                "horizon_months": p["horizon_months"],
                "probability": p["probability"],
                "cohort": p["cohort"],
                "outcome": outcome,
                "symbol": sym,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "return": prices[-1] / prices[0] - 1,
                "benchmark_return": bret,
            },
        )
        n += 1
    return n
