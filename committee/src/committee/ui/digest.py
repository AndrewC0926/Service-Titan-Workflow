"""Daily digest (07:30 local): new briefings, re-review triggers, wash-sale
windows, DQ issues, incidents. Mobile-readable plain text. Never a trading
instruction: it tells you what to read in the weekly session."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from committee.broker.approval import cooling_off_remaining, decided, load_approvable
from committee.broker.models import APPROVABLE_ENTRY_TYPES
from committee.journal.store import Journal


@dataclass
class Digest:
    date: dt.date
    awaiting: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    wash_sale: list[str] = field(default_factory=list)
    dq: list[str] = field(default_factory=list)
    incidents: list[str] = field(default_factory=list)
    orders_blocked: str | None = None

    @property
    def subject(self) -> str:
        bits = [f"{len(self.awaiting)} awaiting"]
        if self.triggers:
            bits.append(f"{len(self.triggers)} triggers")
        if self.dq or self.incidents:
            bits.append("issues")
        return f"Committee digest {self.date}: " + ", ".join(bits)

    def text(self) -> str:
        def sec(title: str, items: list[str], empty: str) -> list[str]:
            return [f"{title}:"] + ([f"  - {x}" for x in items] or [f"  {empty}"]) + [""]

        lines = [self.subject, ""]
        if self.orders_blocked:
            lines += [f"ORDERS BLOCKED: {self.orders_blocked}", ""]
        lines += sec("Briefings awaiting your weekly session", self.awaiting, "none")
        lines += sec("Re-review triggers", self.triggers, "none")
        lines += sec("Wash-sale windows (do not buy)", self.wash_sale, "none")
        lines += sec("Data quality", self.dq, "all checks passed")
        lines += sec("Incidents (24h)", self.incidents, "none")
        lines += ["No trading from the digest. Research, not advice. The human decides."]
        return "\n".join(lines)


def build_digest(
    journal: Journal,
    now: dt.datetime,
    wash_sale_blocks: dict[str, dt.date] | None = None,
    orders_blocked: str | None = None,
) -> Digest:
    d = Digest(now.date(), orders_blocked=orders_blocked)
    since = now - dt.timedelta(days=1)
    for e in journal.entries():
        if (
            e.entry_type in APPROVABLE_ENTRY_TYPES
            and "legs" in e.payload
            and not decided(journal, e.hash)
        ):
            if e.payload.get("recommendation") in ("PASS", "WATCH", "HOLD"):
                continue
            if now - e.created_at > dt.timedelta(days=7):
                continue
            _, ap = load_approvable(journal, e.hash)
            wait = cooling_off_remaining(e, ap, now)
            label = e.payload.get("symbol") or e.payload.get("kind", e.entry_type)
            status = (
                "ready"
                if wait == dt.timedelta(0)
                else f"cooling-off {int(wait.total_seconds() // 3600)}h left"
            )
            d.awaiting.append(f"{label}: {ap.recommendation} ({status}) [{e.hash[:10]}]")
        elif (
            e.entry_type == "state_transition"
            and e.created_at >= since
            and str(e.payload.get("reason", "")).startswith("trigger:")
        ):
            d.triggers.append(
                f"{e.payload.get('symbol', e.payload.get('review_id'))}: {e.payload['reason'][8:]}"
            )
        elif (
            e.entry_type == "note"
            and e.payload.get("kind") == "rereview_trigger"
            and e.created_at >= since
        ):
            d.triggers.append(f"{e.payload.get('symbol')}: {e.payload.get('reason')}")
        elif e.entry_type == "incident" and e.created_at >= since:
            d.incidents.append(
                f"{e.payload.get('level')} {e.payload.get('kind')}: {e.payload.get('job', '')}".strip()
            )
    dq = journal.latest("dq_report")
    if dq is not None:
        d.dq = [str(x) for x in dq.payload.get("failures", [])][:10]
    for sym, until in sorted((wash_sale_blocks or {}).items()):
        d.wash_sale.append(f"{sym} until {until}")
    return d
