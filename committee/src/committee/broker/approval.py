"""Human approval gate (DESIGN 8, 11).

No order is created without a signed approval record that references a
journaled briefing (or core/harvest proposal) by hash. Rules:
- cooling-off: briefing age >= its cooling_off_hours (24 default, 72 on a Behavioral "stop")
- approvals expire 7 days after the briefing; an expired briefing must be rerun
- every approval needs a one-sentence reason; a "stop" also needs a written justification
- the human may approve smaller, never larger, and never a leg the briefing did not recommend
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic import ValidationError

from committee.broker.models import APPROVABLE_ENTRY_TYPES, Approvable, ApprovalRecord, ApprovedLeg
from committee.journal.store import Journal, JournalEntry


class ApprovalError(Exception):
    """The approval violates a gate rule; nothing was journaled."""


@dataclass(frozen=True)
class GateSettings:
    expiry_days: int = 7
    min_reason_chars: int = 10


DEFAULT_GATE = GateSettings()


def load_approvable(journal: Journal, briefing_hash: str) -> tuple[JournalEntry, Approvable]:
    entry = journal.find_by_hash(briefing_hash)
    if entry is None:
        raise ApprovalError(f"no journal entry with hash {briefing_hash[:16]}")
    if entry.entry_type not in APPROVABLE_ENTRY_TYPES:
        raise ApprovalError(
            f"entry {entry.seq} is a {entry.entry_type}, not an approvable briefing"
        )
    try:
        approvable = Approvable.model_validate(
            {k: entry.payload[k] for k in Approvable.model_fields if k in entry.payload}
        )
    except ValidationError as e:
        raise ApprovalError(f"briefing {entry.seq} is missing approvable fields: {e}") from None
    return entry, approvable


def decided(journal: Journal, briefing_hash: str) -> str | None:
    """'approved', 'rejected' or None for a briefing."""
    for e in journal.entries("approval"):
        if e.payload.get("briefing_hash") == briefing_hash:
            return "approved"
    for e in journal.entries("decision"):
        if e.payload.get("briefing_hash") == briefing_hash:
            return str(e.payload.get("decision", "decided")).lower()
    return None


def cooling_off_remaining(
    entry: JournalEntry, approvable: Approvable, now: dt.datetime
) -> dt.timedelta:
    ready = entry.created_at + dt.timedelta(hours=approvable.cooling_off_hours)
    return max(ready - now, dt.timedelta(0))


def approve(
    journal: Journal,
    briefing_hash: str,
    legs: list[ApprovedLeg],
    reason: str,
    account_value_usd: float,
    now: dt.datetime,
    justification: str | None = None,
    settings: GateSettings = DEFAULT_GATE,
) -> tuple[ApprovalRecord, JournalEntry]:
    entry, ap = load_approvable(journal, briefing_hash)
    if decided(journal, briefing_hash):
        raise ApprovalError("briefing already decided")
    if now - entry.created_at > dt.timedelta(days=settings.expiry_days):
        raise ApprovalError(
            f"briefing expired (older than {settings.expiry_days} days); rerun it on fresh data"
        )
    wait = cooling_off_remaining(entry, ap, now)
    if wait > dt.timedelta(0):
        raise ApprovalError(f"cooling-off not satisfied: {wait} remaining")
    if len(reason.strip()) < settings.min_reason_chars:
        raise ApprovalError("a one-sentence reason is required")
    if (
        ap.behavioral_severity == "stop"
        and len((justification or "").strip()) < settings.min_reason_chars
    ):
        raise ApprovalError("Behavioral Auditor said STOP: a written justification is required")
    if not legs:
        raise ApprovalError("approve at least one leg (or reject the briefing)")
    if ap.recommendation in ("PASS", "WATCH", "HOLD"):
        raise ApprovalError(f"recommendation is {ap.recommendation}; there is nothing to approve")
    ceilings = {(leg.symbol, leg.side, leg.account): leg.max_pct_total for leg in ap.legs}
    for leg in legs:
        key = (leg.symbol, leg.side, leg.account)
        if key not in ceilings:
            raise ApprovalError(f"{leg.side} {leg.symbol} in {leg.account} was not recommended")
        if leg.pct_total > ceilings[key] + 1e-9:
            raise ApprovalError(
                f"{leg.symbol}: approved {leg.pct_total}% exceeds recommended {ceilings[key]}% (smaller only)"
            )
    rec = ApprovalRecord(
        briefing_hash=briefing_hash,
        briefing_seq=entry.seq,
        briefing_type=entry.entry_type,
        action=ap.recommendation,
        legs=legs,
        reason=reason.strip(),
        justification=justification,
        account_value_usd=account_value_usd,
        approved_at_utc=now,
    )
    return rec, journal.append("approval", rec.model_dump(mode="json"))


def reject(
    journal: Journal,
    briefing_hash: str,
    reason: str,
    now: dt.datetime,
    settings: GateSettings = DEFAULT_GATE,
) -> JournalEntry:
    entry, _ = load_approvable(journal, briefing_hash)
    if decided(journal, briefing_hash):
        raise ApprovalError("briefing already decided")
    if len(reason.strip()) < settings.min_reason_chars:
        raise ApprovalError("a one-sentence reason is required")
    return journal.append(
        "decision",
        {
            "briefing_hash": briefing_hash,
            "briefing_seq": entry.seq,
            "decision": "REJECTED",
            "human_reason": reason.strip(),
            "decided_at_utc": now.isoformat(),
        },
    )


def expire_stale(
    journal: Journal, now: dt.datetime, settings: GateSettings = DEFAULT_GATE
) -> list[int]:
    """Journal EXPIRED decisions for undecided briefings older than the expiry window."""
    out = []
    for e in journal.entries():
        if e.entry_type not in APPROVABLE_ENTRY_TYPES or "legs" not in e.payload:
            continue
        if now - e.created_at > dt.timedelta(days=settings.expiry_days) and not decided(
            journal, e.hash
        ):
            journal.append(
                "decision",
                {
                    "briefing_hash": e.hash,
                    "briefing_seq": e.seq,
                    "decision": "EXPIRED",
                    "human_reason": None,
                    "decided_at_utc": now.isoformat(),
                },
            )
            out.append(e.seq)
    return out
