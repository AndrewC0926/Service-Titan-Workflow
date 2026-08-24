"""The one Outreach writer. Every caller that wants to log outreach —
the dashboard's own form (app/web/main.py), the log_outreach MCP tool
(app/mcp_tools.py), and the voice-capture review card's confirm action
(app/pipeline/voice_capture.py) — goes through log_outreach() here, so
there is exactly one place an Outreach row is ever created. Previously the
dashboard form and the MCP tool each built their own Outreach(...) insert
independently; a third independent insert (this feature's confirm button)
was the reason to stop duplicating it a second time.

project_id and account_id are both optional, but at least one is required
-- the same "no unresolved-entity concept" discipline
app/web/main.py:capture_confirm already enforces for projects, extended
here to accounts (see Outreach's own docstring) rather than relaxed.
"""
from __future__ import annotations

from datetime import datetime

from app.models import Outreach


def log_outreach(session, *, project_id: int | None = None, account_id: int | None = None,
                 channel: str = "call", notes: str = "", contact_id: int | None = None,
                 next_action: str | None = None,
                 next_action_date: datetime | None = None) -> Outreach:
    if project_id is None and account_id is None:
        raise ValueError("log_outreach requires project_id or account_id -- no unresolved-entity outreach")
    o = Outreach(project_id=project_id, account_id=account_id, contact_id=contact_id,
                channel=channel, notes=notes, next_action=next_action or None,
                next_action_date=next_action_date)
    session.add(o)
    session.commit()
    session.refresh(o)
    return o
