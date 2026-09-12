"""Web-layer smoke tests for the AHJ A2L register's /reference tab and the
project-detail badge -- see app/pipeline/ahj_a2l.py and tests/test_ahj_a2l.py
for the data-layer coverage this builds on."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.models import Category, Project, Stage, Window
from app.pipeline.ahj_a2l import load_ahj_a2l_guidance
from app.web.main import app


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def test_reference_ahj_a2l_tab_renders_with_counts_and_not_reached_visible(db_session, client):
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    resp = client.get("/settings/reference?tab=ahj_a2l", headers=AUTH)
    assert resp.status_code == 200
    body = resp.text
    assert "AHJ A2L guidance" in body
    assert "6 HIT" in body
    assert "156 NONE_FOUND" in body
    assert "3 BLOCKED" in body
    assert "47 NOT_REACHED" in body
    # A real HIT row and a real NOT_REACHED row both actually appear in the table.
    assert "Los Angeles County" in body
    assert "Diamond Bar" in body
    assert 'id="ahj-los-angeles"' in body


def test_reference_ahj_a2l_tab_is_chip_navigable(client):
    resp = client.get("/settings/reference", headers=AUTH)
    assert resp.status_code == 200
    assert '/settings/reference?tab=ahj_a2l' in resp.text


def test_project_in_la_county_shows_ahj_a2l_badge(db_session, client):
    load_ahj_a2l_guidance(db_session)
    p = Project(name="Fixture Data Center", category=Category.data_center, county="Los Angeles",
               stage=Stage.design, window=Window.PRE_BOD)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    resp = client.get(f"/project/{p.id}", headers=AUTH)
    assert resp.status_code == 200
    assert "AHJ has written A2L guidance" in resp.text
    assert "ahj-los-angeles-county" in resp.text


def test_project_in_orange_county_shows_no_ahj_a2l_badge(db_session, client):
    """Orange County is NONE_FOUND, not HIT -- the badge must not fire."""
    load_ahj_a2l_guidance(db_session)
    p = Project(name="Fixture Orange Project", category=Category.data_center, county="Orange",
               stage=Stage.design, window=Window.PRE_BOD)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)

    resp = client.get(f"/project/{p.id}", headers=AUTH)
    assert resp.status_code == 200
    assert "AHJ has written A2L guidance" not in resp.text
