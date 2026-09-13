"""Block 4B Item 5 (Master Plan v3.6 section 31/35): "A manager can see
the team's Outcomes and Notes as patterns (counts by reason code,
competitor by line, pen holder by trigger type); a rep sees only their
own." These three functions compute the TEAM view (every row, no user
filter) -- the rep-scoped "own only" view is a plain
`.where(author/user == username)` at the call site (app/web/main.py),
not a second set of functions here.
"""
from __future__ import annotations

from collections import Counter

from sqlmodel import Session, select

from app.models import DecisionNote, Outcome, Signal


def reason_code_counts(session: Session) -> list[dict]:
    """Outcome.reason_code (set only on `lost`) and DecisionNote.
    reason_code (nullable, same LostReasonCode vocabulary -- see that
    model's own docstring) combined into one count per code. Rows with no
    reason_code at all are excluded, not bucketed as "none" -- a null
    reason_code is "not applicable to this row's disposition/note_type,"
    never itself a reason."""
    counts = Counter()
    for code in session.exec(select(Outcome.reason_code).where(Outcome.reason_code.is_not(None))):
        counts[code.value] += 1
    for code in session.exec(select(DecisionNote.reason_code).where(DecisionNote.reason_code.is_not(None))):
        counts[code.value] += 1
    return [{"reason_code": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def competitor_by_line(session: Session) -> list[dict]:
    """DecisionNote rows naming a competitor_line, grouped by (line,
    competitor_line) -- "which competitor keeps winning on which line"
    (section 31's own words). Rows with no competitor_line are excluded:
    silence about a competitor is not the same fact as "no competitor,"
    and this never guesses which is which."""
    rows = session.exec(
        select(DecisionNote.line, DecisionNote.competitor_line)
        .where(DecisionNote.competitor_line.is_not(None))
    ).all()
    counts = Counter((line or "unknown, not guessed", competitor) for line, competitor in rows)
    return [{"line": line, "competitor_line": competitor, "count": count}
           for (line, competitor), count in sorted(counts.items(), key=lambda kv: -kv[1])]


def pen_holder_by_signal_type(session: Session) -> list[dict]:
    """DecisionNote.pen_holder, grouped by the signal_type of the Signal
    it's linked to via signal_id -- the closest real concept to "trigger
    type" a Note can reach (Signal.signal_type is the extraction-pipeline
    vocabulary; TriggerType is unified_signals()' own read-time label for
    six read-time SOURCES, which a persisted DecisionNote has no path
    back to). A note with no signal_id (the large majority -- most notes
    anchor on an Opportunity/Project/Building/Account instead) is grouped
    under "no signal linked" rather than dropped, since pen_holder is a
    real fact about it either way."""
    rows = session.exec(
        select(DecisionNote.pen_holder, DecisionNote.signal_id)
    ).all()
    signal_ids = [sid for _, sid in rows if sid is not None]
    signal_types = {}
    if signal_ids:
        for sid, stype in session.exec(select(Signal.id, Signal.signal_type).where(Signal.id.in_(signal_ids))):
            signal_types[sid] = stype.value

    counts = Counter()
    for pen_holder, signal_id in rows:
        label = signal_types.get(signal_id, "no signal linked")
        counts[(pen_holder.value, label)] += 1
    return [{"pen_holder": ph, "signal_type": st, "count": count}
           for (ph, st), count in sorted(counts.items(), key=lambda kv: -kv[1])]


def team_patterns(session: Session) -> dict:
    """Everything the manager-scoped /notes view needs, one call."""
    return {
        "reason_code_counts": reason_code_counts(session),
        "competitor_by_line": competitor_by_line(session),
        "pen_holder_by_signal_type": pen_holder_by_signal_type(session),
    }
