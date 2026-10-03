"""Review states and journaled transitions (DESIGN 8).

SCREENED -> BASE_RATE -> ANALYSTS -> BEAR -> RISK -> TAX -> BEHAVIORAL -> CHAIR ->
BRIEFED -> AWAITING_APPROVAL -> APPROVED | REJECTED | EXPIRED -> ORDERED -> FILLED ->
MONITORING -> EXIT_REVIEW.  A Risk VETO ends at VETOED (still scored); any failure
parks the review in NEEDS_ATTENTION instead of guessing.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from committee.journal.store import Journal, JournalEntry

PIPELINE: tuple[str, ...] = (
    "SCREENED",
    "BASE_RATE",
    "ANALYSTS",
    "BEAR",
    "RISK",
    "TAX",
    "BEHAVIORAL",
    "CHAIR",
    "BRIEFED",
    "AWAITING_APPROVAL",
)
AFTER_GATE: tuple[str, ...] = (
    "APPROVED",
    "REJECTED",
    "EXPIRED",
    "ORDERED",
    "FILLED",
    "MONITORING",
    "EXIT_REVIEW",
)
TERMINAL_REVIEW = frozenset({"AWAITING_APPROVAL", "VETOED", "NEEDS_ATTENTION"})
ALL_STATES = frozenset(PIPELINE) | frozenset(AFTER_GATE) | {"VETOED", "NEEDS_ATTENTION"}

ALLOWED: dict[str, frozenset[str]] = {
    "": frozenset({"SCREENED"}),
    **{a: frozenset({b, "NEEDS_ATTENTION"}) for a, b in pairwise(PIPELINE)},
    "AWAITING_APPROVAL": frozenset({"APPROVED", "REJECTED", "EXPIRED"}),
    "APPROVED": frozenset({"ORDERED", "NEEDS_ATTENTION"}),
    "ORDERED": frozenset({"FILLED", "NEEDS_ATTENTION"}),
    "FILLED": frozenset({"MONITORING"}),
    "MONITORING": frozenset({"EXIT_REVIEW", "MONITORING"}),
    "EXIT_REVIEW": frozenset({"MONITORING", "SCREENED"}),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset({"SCREENED"}),
    "VETOED": frozenset({"SCREENED"}),
    "NEEDS_ATTENTION": frozenset({"SCREENED"}),
}
ALLOWED["RISK"] = ALLOWED["RISK"] | {"VETOED"}
ALLOWED["TAX"] = ALLOWED["TAX"] | {"VETOED"}


class TransitionError(Exception):
    pass


def current_state(journal: Journal, review_id: str) -> str:
    state = ""
    for e in journal.entries("state_transition"):
        if e.payload.get("review_id") == review_id:
            state = str(e.payload["to"])
    return state


def transition(
    journal: Journal, review_id: str, to: str, reason: str = "", **extra: Any
) -> JournalEntry:
    frm = current_state(journal, review_id)
    if to not in ALLOWED.get(frm, frozenset()):
        raise TransitionError(f"{review_id}: {frm or '(new)'} -> {to} is not allowed")
    return journal.append(
        "state_transition",
        {"review_id": review_id, "from": frm, "to": to, "reason": reason, **extra},
    )
