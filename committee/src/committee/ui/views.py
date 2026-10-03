"""Data for the dashboard pages, as plain functions over the journal and config
(the Streamlit layer in app.py only renders these)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from committee.broker.approval import cooling_off_remaining, decided, load_approvable
from committee.broker.models import APPROVABLE_ENTRY_TYPES
from committee.journal.store import Journal, JournalEntry


@dataclass(frozen=True)
class QueueItem:
    hash: str
    seq: int
    kind: str
    label: str
    recommendation: str
    created_at: dt.datetime
    cooling_left: dt.timedelta
    expires_at: dt.datetime
    severity: str

    @property
    def ready(self) -> bool:
        return self.cooling_left == dt.timedelta(0)


def approval_queue(journal: Journal, now: dt.datetime, expiry_days: int = 7) -> list[QueueItem]:
    out = []
    for e in journal.entries():
        if (
            e.entry_type not in APPROVABLE_ENTRY_TYPES
            or "legs" not in e.payload
            or decided(journal, e.hash)
        ):
            continue
        _, ap = load_approvable(journal, e.hash)
        if ap.recommendation in ("PASS", "WATCH", "HOLD"):
            continue
        exp = e.created_at + dt.timedelta(days=expiry_days)
        if now > exp:
            continue
        out.append(
            QueueItem(
                e.hash,
                e.seq,
                e.entry_type,
                str(e.payload.get("symbol") or e.payload.get("kind") or e.entry_type),
                ap.recommendation,
                e.created_at,
                cooling_off_remaining(e, ap, now),
                exp,
                ap.behavioral_severity,
            )
        )
    return sorted(out, key=lambda q: q.created_at)


def agent_outputs_for(journal: Journal, review_id: str) -> list[JournalEntry]:
    return [
        e
        for e in journal.entries("agent_output")
        if e.payload.get("run_id") == review_id or e.payload.get("review_id") == review_id
    ]


def costs(journal: Journal, now: dt.datetime) -> dict[str, Any]:
    month = now.strftime("%Y-%m")
    total = 0.0
    by_agent: dict[str, float] = {}
    reviews: set[str] = set()
    for e in journal.entries("agent_output"):
        c = float(e.payload.get("cost_usd") or 0.0)
        if e.created_at_utc[:7] != month:
            continue
        total += c
        a = str(e.payload.get("agent", "?"))
        by_agent[a] = by_agent.get(a, 0.0) + c
        rid = e.payload.get("run_id") or e.payload.get("review_id")
        if rid:
            reviews.add(str(rid))
    return {
        "month": month,
        "total_usd": round(total, 4),
        "by_agent": {k: round(v, 4) for k, v in sorted(by_agent.items())},
        "reviews": len(reviews),
        "per_review_usd": round(total / len(reviews), 4) if reviews else None,
    }


def search(
    journal: Journal, text: str = "", entry_type: str | None = None, limit: int = 200
) -> list[JournalEntry]:
    rows = [
        e for e in journal.entries(entry_type) if not text or text.lower() in str(e.payload).lower()
    ]
    return rows[-limit:]
