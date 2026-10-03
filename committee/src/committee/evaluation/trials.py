"""Trial registry: every prompt version, signal-weight change, model change and
strategy variant counts as a trial; the deflated Sharpe uses the total count."""

from __future__ import annotations

from committee.journal.store import Journal

TRIAL_KINDS = frozenset({"prompt", "signal_weights", "model", "strategy", "agent_weights"})


def register_trial(
    journal: Journal, kind: str, ref: str, content_hash: str, note: str = ""
) -> bool:
    """Journal a trial once per (kind, content_hash). Returns True if new."""
    if kind not in TRIAL_KINDS:
        raise ValueError(f"unknown trial kind {kind}")
    for e in journal.entries("trial"):
        if e.payload.get("kind") == kind and e.payload.get("content_hash") == content_hash:
            return False
    journal.append("trial", {"kind": kind, "ref": ref, "content_hash": content_hash, "note": note})
    return True


def trial_count(journal: Journal) -> int:
    return max(1, sum(1 for _ in journal.entries("trial")))
