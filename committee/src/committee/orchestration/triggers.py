"""Re-review triggers for approved positions (DESIGN 8): a falsifier's check date
arrives, an 8-K item 4.01 / 4.02 / 5.02 is filed, price falls 20% from entry
(review, never auto-sell), or the quarterly scheduled review comes due. Kill
criteria are surfaced for the human to check. Each trigger is journaled once as a
``note`` (kind ``rereview_trigger``)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from committee.data.pit import PIT
from committee.journal.store import Journal

WATCH_8K_ITEMS = ("4.01", "4.02", "5.02")
DROP_FROM_ENTRY = 0.20
QUARTERLY_DAYS = 91


@dataclass(frozen=True)
class Trigger:
    review_id: str
    symbol: str
    key: str
    reason: str


def approved_briefings(journal: Journal) -> list[tuple[dt.datetime, dict[str, Any]]]:
    out = []
    for a in journal.entries("approval"):
        b = journal.find_by_hash(str(a.payload["briefing_hash"]))
        if b is not None and b.entry_type == "briefing":
            out.append((a.created_at, b.payload))
    return out


def find_triggers(
    journal: Journal,
    today: dt.date,
    last_close: Callable[[str], float],
    eight_k_items: Callable[[str, dt.date], list[str]],
) -> list[Trigger]:
    out: list[Trigger] = []
    for approved_at, b in approved_briefings(journal):
        rid, sym = str(b["review_id"]), str(b["symbol"])
        for f in b.get("falsifiers") or []:
            due = dt.date.fromisoformat(str(f["check_by"]))
            if due <= today:
                out.append(
                    Trigger(
                        rid,
                        sym,
                        f"falsifier:{f['observable']}:{due}",
                        f"falsifier due {due}: {f['observable']} {f['threshold']}",
                    )
                )
        for item in eight_k_items(sym, approved_at.date()):
            for watch in WATCH_8K_ITEMS:
                if watch in item.split(","):
                    out.append(Trigger(rid, sym, f"8k:{watch}:{item}", f"8-K item {watch} filed"))
        entry = float(b.get("entry_price") or 0)
        px = last_close(sym)
        if entry > 0 and px > 0 and px <= entry * (1 - DROP_FROM_ENTRY):
            out.append(
                Trigger(
                    rid,
                    sym,
                    f"drop20:{today:%Y-%m}",
                    f"price {px:.2f} is {1 - px / entry:.0%} below entry {entry:.2f} (review, never auto-sell)",
                )
            )
        age = (today - approved_at.date()).days
        if age >= QUARTERLY_DAYS:
            q = age // QUARTERLY_DAYS
            out.append(Trigger(rid, sym, f"quarterly:{q}", f"scheduled quarterly review #{q}"))
    return out


def journal_new(journal: Journal, triggers: list[Trigger]) -> list[Trigger]:
    seen = {
        (e.payload.get("review_id"), e.payload.get("key"))
        for e in journal.entries("note")
        if e.payload.get("kind") == "rereview_trigger"
    }
    new = [t for t in triggers if (t.review_id, t.key) not in seen]
    for t in new:
        journal.append(
            "note",
            {
                "kind": "rereview_trigger",
                "review_id": t.review_id,
                "symbol": t.symbol,
                "key": t.key,
                "reason": t.reason,
            },
        )
    return new


def pit_8k_items(p: PIT, asof: dt.date) -> Callable[[str, dt.date], list[str]]:
    from committee.data.security_master import resolve

    def items(symbol: str, since: dt.date) -> list[str]:
        sid = resolve(p, symbol, asof)
        if not sid or not p.has("filings"):
            return []
        df = p.latest("filings", asof, "security_id = $sid AND form LIKE '8-K%'", sid=sid)
        if df.empty:
            return []
        df = df[pd.to_datetime(df["accepted_at"], utc=True).dt.date >= since]
        return [str(x) for x in df["items"].dropna()]

    return items
