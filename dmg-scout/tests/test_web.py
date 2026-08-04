"""Dashboard smoke tests: auth enforcement and each view renders with data."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.manual import add_manual_signal
from app.models import SourceRun
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import app


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"andrew:wrong").decode()}


def seed(db_session, cfg):
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="design")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


def test_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=BAD).status_code == 401


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_board_renders(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Meridian DC" in r.text
    assert "IN_BOD" in r.text  # design stage -> IN_BOD window


def test_project_detail_and_notes(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/project/1", headers=AUTH)
    assert r.status_code == 200
    assert "Signal timeline" in r.text and "prequal_invite" in r.text
    assert "57,200" in r.text or "stated IT load" in r.text  # tonnage basis shown

    r = client.post("/project/1/notes", headers=AUTH,
                    data={"notes": "call EOR", "next_action": "intro call"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "intro call" in client.get("/project/1", headers=AUTH).text


def test_outreach_log(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.post("/project/1/outreach", headers=AUTH,
                    data={"channel": "call", "notes": "left VM"}, follow_redirects=False)
    assert r.status_code == 303
    assert "left VM" in client.get("/project/1", headers=AUTH).text


def test_other_views_render(client, db_session, cfg):
    seed(db_session, cfg)
    db_session.add(SourceRun(source="ceqanet", ok=False, error="boom"))
    db_session.commit()
    for path in ("/review", "/contacts", "/map", "/health", "/add-signal"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200, path
    assert "boom" in client.get("/health", headers=AUTH).text


def test_add_signal_form_creates_project(client, db_session, cfg):
    r = client.post("/add-signal", headers=AUTH, data={
        "signal_type": "engineer_move", "summary": "Jane Doe moved to kW MCE",
        "person_name": "Jane Doe", "person_org": "kW Mission Critical Engineering",
        "county": "Orange", "state": "CA",
    }, follow_redirects=False)
    assert r.status_code == 303
    board = client.get("/", headers=AUTH).text
    assert "Unnamed" in board or "Jane" in board


def test_gate5_views_and_exports(client, db_session, cfg):
    seed(db_session, cfg)
    # brief page renders with traceable content
    r = client.get("/project/1/brief", headers=AUTH)
    assert r.status_code == 200 and "project brief" in r.text

    # outcome form closes the project off the board
    r = client.post("/project/1/outcome", headers=AUTH,
                    data={"status": "dead", "reason": "cancelled"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Meridian DC" not in client.get("/", headers=AUTH).text

    # watchlist view + CSV exports
    assert client.get("/watchlist", headers=AUTH).status_code == 200
    for path in ("/export/board.csv", "/export/contacts.csv", "/export/firms.csv",
                 "/export/signals.csv"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200, path
        assert "text/csv" in r.headers["content-type"]
    assert client.get("/export/board.csv", headers=AUTH).text.startswith("id,project,developer")

    # dashboard firm add + roster on contacts page
    r = client.post("/firms", headers=AUTH,
                    data={"name": "Test Firm Engineering", "firm_type": "mep",
                          "aliases": "TFE; Test Firm"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Test Firm Engineering" in client.get("/contacts", headers=AUTH).text


def test_no_password_fails_closed(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    assert client.get("/", headers=AUTH).status_code == 503
