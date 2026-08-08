"""Fathom transcript -> Outreach log entry (Phase 5c).

Fathom transcribes recorded calls; the outreach log fills itself instead of
depending on someone remembering to type it up. Scout has no Fathom API key
of its own (same shape as Apollo/Lusha in app/enrichment.py) -- Fathom's MCP
tools (list_meetings, get_meeting_transcript, get_meeting_summary) are only
reachable from a live Claude session, not from this backend's cron. So the
bridge is: fetch the transcript there, hand it to `scout
log-outreach-from-fathom` here, which project/contact it belongs to still has
to be named -- Fathom has no idea what a Scout project ID is. That is a real
cost, but "the project must be named manually" still beats typing the whole
call up by hand.

Untested against a real call as of this writing: the account that authorized
this had exactly one recording, Fathom's own onboarding demo. The extraction
prompt (see app/llm.py:outreach_from_transcript) is built to fail toward null
next_action/next_action_date rather than invent one, so a low-signal transcript
degrades to "notes only" rather than a fabricated next step -- but that
degradation itself is unverified against a real sales call.
"""
from __future__ import annotations

from app.llm import outreach_from_transcript
from app.models import Outreach, utcnow


def log_outreach_from_fathom(session, *, project_id: int, transcript: str,
                             summary: str | None = None, contact_id: int | None = None,
                             meeting_date=None, fathom_url: str | None = None) -> Outreach:
    """Extract notes/next_action from a Fathom transcript and write one
    Outreach row. `meeting_date` should be the call's actual date (from
    Fathom's list_meetings), not today's date, when known."""
    extracted = outreach_from_transcript(transcript, summary=summary)
    notes = extracted["notes"]
    if fathom_url:
        notes = f"{notes}\n\n[Fathom recording: {fathom_url}]"

    next_action_date = None
    if extracted.get("next_action_date"):
        from datetime import datetime
        try:
            next_action_date = datetime.fromisoformat(extracted["next_action_date"])
        except ValueError:
            pass  # model returned something non-ISO; drop rather than guess

    outreach = Outreach(
        project_id=project_id, contact_id=contact_id,
        date=meeting_date or utcnow(), channel="call",
        notes=notes, next_action=extracted.get("next_action"),
        next_action_date=next_action_date,
    )
    session.add(outreach)
    return outreach
