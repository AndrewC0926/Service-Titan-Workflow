"""Block 4A Item 3 (Master Plan v3.6 section 31): the /notes page."""
import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import Account, DecisionNote, Opportunity, Signal, SignalType, utcnow
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _opportunity(db_session, created_at=None):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1)
    if created_at is not None:
        opp.created_at = created_at
    db_session.add(opp)
    db_session.commit()
    return opp


def test_notes_page_loads(client, db_session):
    r = client.get("/notes", headers=AUTH)
    assert r.status_code == 200
    assert "Decision Notes" in r.text


def test_notes_page_shows_three_deals_to_explain(client, db_session):
    opp = _opportunity(db_session, created_at=utcnow() - timedelta(days=20))
    r = client.get("/notes", headers=AUTH)
    assert f"Opportunity #{opp.id}" in r.text


def test_add_note_via_form_creates_a_row_with_the_authenticated_user(client, db_session):
    opp = _opportunity(db_session)
    r = client.post("/notes", headers=AUTH, data={
        "note_type": "decision", "lead_source": "rep_originated", "opportunity_id": str(opp.id),
        "free_text": "called the PE about the next package",
    }, follow_redirects=False)
    assert r.status_code == 303
    note = db_session.exec(select(DecisionNote).where(DecisionNote.opportunity_id == opp.id)).one()
    assert note.author == "andrew"
    assert note.free_text == "called the PE about the next package"


def test_add_note_without_any_anchor_400s(client, db_session):
    r = client.post("/notes", headers=AUTH, data={"note_type": "intel", "lead_source": "inbound"})
    assert r.status_code == 400


def test_notes_page_pre_fills_the_anchor_from_query_params(client, db_session):
    r = client.get("/notes?anchor_type=opportunity&anchor_id=42", headers=AUTH)
    assert r.status_code == 200
    assert 'name="opportunity_id" value="42"' in r.text


class TestAddNoteLinksOnObjectPages:
    """"An add-note action on every object page" -- confirms the link is
    actually there, not just that the backend can accept the anchor."""

    def test_project_detail_page_has_an_add_note_link(self, client, db_session, cfg):
        from app.manual import add_manual_signal
        from app.pipeline.resolve import run_resolve

        add_manual_signal(db_session, "prequal_invite", "test", project_name="Note Link DC",
                          developer="Test Dev", county="San Bernardino", state="CA", mw_it=10, stage="design")
        run_resolve(db_session, cfg, use_llm=False)
        from app.models import Project
        p = db_session.exec(select(Project)).first()
        r = client.get(f"/project/{p.id}", headers=AUTH)
        assert r.status_code == 200
        assert f"/notes?anchor_type=project&anchor_id={p.id}" in r.text

    def test_account_detail_page_has_an_add_note_link(self, client, db_session, cfg):
        from app.accounts import create_account
        account = create_account(db_session, name="Note Link Co")
        r = client.get(f"/accounts/{account.id}", headers=AUTH)
        assert r.status_code == 200
        assert f"/notes?anchor_type=account&anchor_id={account.id}" in r.text

    def test_retrofit_building_detail_page_has_an_add_note_link(self, client, db_session, cfg):
        from app.models import RetrofitBuilding
        b = RetrofitBuilding(apn="1-1-1", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        r = client.get(f"/retrofit/building/{b.id}", headers=AUTH)
        assert r.status_code == 200
        assert f"/notes?anchor_type=building&anchor_id={b.id}" in r.text

    def test_pipeline_row_has_an_add_note_link(self, client, db_session, cfg):
        opp = _opportunity(db_session)
        r = client.get("/pipeline", headers=AUTH)
        assert r.status_code == 200
        assert f"/notes?anchor_type=opportunity&anchor_id={opp.id}" in r.text


def test_recent_notes_list_shows_the_anchor_label(client, db_session):
    account = Account(name="Test Anchor Co", name_norm="test anchor co")
    db_session.add(account)
    db_session.commit()
    client.post("/notes", headers=AUTH, data={
        "note_type": "intel", "lead_source": "inbound", "account_id": str(account.id),
        "free_text": "heard they're looking at a new AHU",
    })
    r = client.get("/notes", headers=AUTH)
    assert "Account: Test Anchor Co" in r.text
