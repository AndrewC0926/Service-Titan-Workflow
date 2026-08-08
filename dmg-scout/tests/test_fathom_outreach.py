"""Fathom transcript -> Outreach row. See app/pipeline/fathom_outreach.py.

The LLM call itself is monkeypatched (same pattern as test_grounding.py) --
these tests are about the wiring: does the extracted next_action/date land
correctly on the Outreach row, does a missing date stay null, does the
Fathom URL get attached to the notes."""
from datetime import datetime

import app.pipeline.fathom_outreach as fo
from app.models import Category, Outreach, Project, Stage, Window


def _project(db_session):
    p = Project(name="Test Project", category=Category.data_center, county="Los Angeles",
               state="CA", status="active", in_territory=True, window=Window.PRE_BOD,
               stage=Stage.entitlement)
    db_session.add(p)
    db_session.commit()
    return p


def test_logs_outreach_with_next_action(db_session, monkeypatch):
    monkeypatch.setattr(fo, "outreach_from_transcript", lambda *a, **k: {
        "notes": "Discussed cooling tower replacement timeline for Building B.",
        "next_action": "Send budgetary quote", "next_action_date": "2026-08-15"})
    p = _project(db_session)

    outreach = fo.log_outreach_from_fathom(
        db_session, project_id=p.id, transcript="(transcript text)",
        fathom_url="https://fathom.video/calls/123")
    db_session.commit()

    row = db_session.get(Outreach, outreach.id)
    assert row.project_id == p.id
    assert row.channel == "call"
    assert "cooling tower" in row.notes
    assert "https://fathom.video/calls/123" in row.notes
    assert row.next_action == "Send budgetary quote"
    assert row.next_action_date == datetime(2026, 8, 15)


def test_no_stated_next_action_stays_null_not_guessed(db_session, monkeypatch):
    """Null over inference: a quiet call with no clear next step must not
    invent one."""
    monkeypatch.setattr(fo, "outreach_from_transcript", lambda *a, **k: {
        "notes": "Introductory call, no specific ask yet.",
        "next_action": None, "next_action_date": None})
    p = _project(db_session)

    outreach = fo.log_outreach_from_fathom(db_session, project_id=p.id, transcript="(transcript text)")
    db_session.commit()

    row = db_session.get(Outreach, outreach.id)
    assert row.next_action is None
    assert row.next_action_date is None


def test_non_iso_date_from_model_is_dropped_not_guessed(db_session, monkeypatch):
    """A model that returns something malformed (e.g. 'next week' instead of
    an ISO date) must not crash or silently coerce into a wrong date."""
    monkeypatch.setattr(fo, "outreach_from_transcript", lambda *a, **k: {
        "notes": "Call happened.", "next_action": "Follow up", "next_action_date": "next week"})
    p = _project(db_session)

    outreach = fo.log_outreach_from_fathom(db_session, project_id=p.id, transcript="(transcript text)")
    db_session.commit()

    row = db_session.get(Outreach, outreach.id)
    assert row.next_action == "Follow up"
    assert row.next_action_date is None


def test_meeting_date_used_instead_of_today(db_session, monkeypatch):
    monkeypatch.setattr(fo, "outreach_from_transcript", lambda *a, **k: {
        "notes": "x", "next_action": None, "next_action_date": None})
    p = _project(db_session)
    called_at = datetime(2026, 7, 1, 14, 30)

    outreach = fo.log_outreach_from_fathom(db_session, project_id=p.id,
                                           transcript="(transcript text)", meeting_date=called_at)
    db_session.commit()

    row = db_session.get(Outreach, outreach.id)
    assert row.date == called_at
