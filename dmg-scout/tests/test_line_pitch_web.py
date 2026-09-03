"""/line/{id}'s Pitch panel, the Confirm/Reject/Edit actions, /reference's
'pitches' tab, and the per-branch printable cheat sheet. No LLM calls here
-- rows are seeded directly, exercising only the web layer.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import LineCompetitor, LinePitch, ProductLine, ProductLineBranch, utcnow
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _line(session, name="AAON", building_role="air_handling"):
    line = ProductLine(name=name, name_norm=name.lower(), category="rooftop_units",
                       building_role=building_role)
    session.add(line)
    session.commit()
    session.refresh(line)
    return line


def _pitch(session, line, **kw):
    defaults = dict(product_line_id=line.id, what_it_is="A rooftop unit line.",
                    elevator_pitch="It's a solid line.", review_status="draft",
                    differentiators=["Factory built."], engineer_questions=["What's the tonnage?"])
    defaults.update(kw)
    pitch = LinePitch(**defaults)
    session.add(pitch)
    session.commit()
    session.refresh(pitch)
    return pitch


def _competitor_row(session, line, name="Trane", **kw):
    defaults = dict(product_line_id=line.id, competitor_name=name,
                    why_we_lose="Brand recognition.", why_we_win="Configurability.",
                    review_status="draft")
    defaults.update(kw)
    row = LineCompetitor(**defaults)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ---- /line/{id}: Pitch panel + UNVERIFIED marker ---------------------------

def test_line_detail_shows_unverified_marker_for_a_draft_pitch(client, db_session, cfg):
    line = _line(db_session)
    _pitch(db_session, line)
    r = client.get(f"/line/{line.id}", headers=AUTH)
    assert r.status_code == 200
    assert "UNVERIFIED" in r.text
    assert "A rooftop unit line." in r.text


def test_line_detail_shows_confirmed_styling_not_unverified(client, db_session, cfg):
    line = _line(db_session)
    _pitch(db_session, line, review_status="confirmed", reviewed_by="Andrew")
    r = client.get(f"/line/{line.id}", headers=AUTH)
    assert "confirmed by Andrew" in r.text
    # UNVERIFIED must not appear for a confirmed pitch
    assert "UNVERIFIED" not in r.text.split("Pitch")[1].split("Branch coverage")[0]


def test_line_detail_handles_no_pitch_generated_yet(client, db_session, cfg):
    line = _line(db_session)
    r = client.get(f"/line/{line.id}", headers=AUTH)
    assert r.status_code == 200
    assert "generate-line-pitches" in r.text


def test_line_detail_shows_competitor_rows(client, db_session, cfg):
    line = _line(db_session)
    _pitch(db_session, line)
    _competitor_row(db_session, line)
    r = client.get(f"/line/{line.id}", headers=AUTH)
    assert "Trane" in r.text
    assert "Brand recognition." in r.text


# ---- Confirm / Reject / Edit -----------------------------------------------

def test_confirm_pitch_action(client, db_session, cfg):
    line = _line(db_session)
    pitch = _pitch(db_session, line)
    r = client.post(f"/line/{line.id}/pitch/review", headers=AUTH,
                    data={"action": "confirm", "reviewed_by": "Andrew"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(pitch)
    assert pitch.review_status == "confirmed"
    assert pitch.reviewed_by == "Andrew"
    assert pitch.reviewed_at is not None


def test_reject_pitch_action(client, db_session, cfg):
    line = _line(db_session)
    pitch = _pitch(db_session, line)
    client.post(f"/line/{line.id}/pitch/review", headers=AUTH,
               data={"action": "reject", "reviewed_by": "Andrew"})
    db_session.refresh(pitch)
    assert pitch.review_status == "rejected"


def test_edit_pitch_action_applies_fields_and_confirms(client, db_session, cfg):
    line = _line(db_session)
    pitch = _pitch(db_session, line)
    client.post(f"/line/{line.id}/pitch/review", headers=AUTH, data={
        "action": "edit", "reviewed_by": "Andrew",
        "what_it_is": "A corrected description.",
        "elevator_pitch": "A corrected pitch.",
        "differentiators": "One\nTwo\n",
        "engineer_questions": "Q1\nQ2",
    })
    db_session.refresh(pitch)
    assert pitch.review_status == "confirmed"
    assert pitch.what_it_is == "A corrected description."
    assert pitch.differentiators == ["One", "Two"]
    assert pitch.engineer_questions == ["Q1", "Q2"]


def test_review_action_404s_for_unknown_line(client, db_session, cfg):
    r = client.post("/line/99999/pitch/review", headers=AUTH,
                    data={"action": "confirm", "reviewed_by": "Andrew"})
    assert r.status_code == 404


def test_review_action_400s_for_unknown_action(client, db_session, cfg):
    line = _line(db_session)
    _pitch(db_session, line)
    r = client.post(f"/line/{line.id}/pitch/review", headers=AUTH,
                    data={"action": "delete", "reviewed_by": "Andrew"})
    assert r.status_code == 400


def test_confirm_competitor_action(client, db_session, cfg):
    line = _line(db_session)
    row = _competitor_row(db_session, line)
    r = client.post(f"/line/{line.id}/competitor/{row.id}/review", headers=AUTH,
                    data={"action": "confirm", "reviewed_by": "Andrew"}, follow_redirects=False)
    assert r.status_code == 303
    db_session.refresh(row)
    assert row.review_status == "confirmed"


def test_edit_competitor_action(client, db_session, cfg):
    line = _line(db_session)
    row = _competitor_row(db_session, line)
    client.post(f"/line/{line.id}/competitor/{row.id}/review", headers=AUTH, data={
        "action": "edit", "reviewed_by": "Andrew",
        "why_we_lose": "New lose reason.", "why_we_win": "New win reason.",
    })
    db_session.refresh(row)
    assert row.review_status == "confirmed"
    assert row.why_we_lose == "New lose reason."


def test_competitor_review_404s_for_mismatched_line(client, db_session, cfg):
    line = _line(db_session, name="AAON")
    other_line = _line(db_session, name="Titus")
    row = _competitor_row(db_session, other_line)
    r = client.post(f"/line/{line.id}/competitor/{row.id}/review", headers=AUTH,
                    data={"action": "confirm", "reviewed_by": "Andrew"})
    assert r.status_code == 404


# ---- /reference?tab=pitches: confirmed only, plus draft count -------------

def test_reference_pitches_tab_shows_only_confirmed(client, db_session, cfg):
    confirmed_line = _line(db_session, name="Confirmed Co")
    draft_line = _line(db_session, name="Draft Co")
    _pitch(db_session, confirmed_line, review_status="confirmed", reviewed_by="Andrew",
          elevator_pitch="Confirmed pitch text.")
    _pitch(db_session, draft_line, elevator_pitch="Draft pitch text -- should not show.")

    r = client.get("/reference?tab=pitches", headers=AUTH)
    assert r.status_code == 200
    assert "Confirmed Co" in r.text
    assert "Confirmed pitch text." in r.text
    assert "Draft Co" not in r.text
    assert "Draft pitch text" not in r.text
    assert "1 draft" in r.text  # the draft count is surfaced, even though the pitch itself isn't


def test_reference_pitches_tab_empty_state(client, db_session, cfg):
    r = client.get("/reference?tab=pitches", headers=AUTH)
    assert r.status_code == 200
    assert "0 drafts" in r.text or "No confirmed pitches" in r.text


# ---- /lines/cheat-sheet/{branch}: confirmed only, for the truck -----------

def test_cheat_sheet_shows_only_confirmed_pitches_for_that_branch(client, db_session, cfg):
    line = _line(db_session, name="AAON")
    db_session.add(ProductLineBranch(product_line_id=line.id, branch="DMG Los Angeles",
                                     status="confirmed_covered", source_detail="test"))
    db_session.commit()
    _pitch(db_session, line, review_status="confirmed", reviewed_by="Andrew",
          elevator_pitch="Confirmed AAON pitch.")

    r = client.get("/lines/cheat-sheet/DMG Los Angeles", headers=AUTH)
    assert r.status_code == 200
    assert "Confirmed AAON pitch." in r.text
    assert "AAON" in r.text


def test_cheat_sheet_excludes_draft_pitches(client, db_session, cfg):
    line = _line(db_session, name="AAON")
    db_session.add(ProductLineBranch(product_line_id=line.id, branch="DMG Los Angeles",
                                     status="confirmed_covered", source_detail="test"))
    db_session.commit()
    _pitch(db_session, line, review_status="draft", elevator_pitch="Draft pitch, must not print.")

    r = client.get("/lines/cheat-sheet/DMG Los Angeles", headers=AUTH)
    assert r.status_code == 404  # nothing confirmed for this branch


def test_cheat_sheet_excludes_lines_not_confirmed_covered_at_that_branch(client, db_session, cfg):
    line = _line(db_session, name="AAON")
    db_session.add(ProductLineBranch(product_line_id=line.id, branch="DMG Los Angeles",
                                     status="confirmed_not_covered", source_detail="test"))
    db_session.commit()
    _pitch(db_session, line, review_status="confirmed", reviewed_by="Andrew")

    r = client.get("/lines/cheat-sheet/DMG Los Angeles", headers=AUTH)
    assert r.status_code == 404


def test_cheat_sheet_404s_for_a_branch_with_nothing_confirmed(client, db_session, cfg):
    r = client.get("/lines/cheat-sheet/DMG Nowhere", headers=AUTH)
    assert r.status_code == 404
