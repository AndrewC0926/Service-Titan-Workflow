"""/board and /retrofit default to the LA-office rep's own territory, with
everything else behind a filter rather than deleted -- see MY_TERRITORY_COUNTIES
in app/web/main.py. Reuses app.schedule_mapping.BRANCH_BY_COUNTY's literal
county association (the same one the branch-restriction feature uses), not a
new taxonomy: today that means exactly "los angeles"."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.manual import add_manual_signal
from app.models import RetrofitBuilding
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_la_and_nevada(db_session, cfg):
    add_manual_signal(db_session, "manual_tip", "la tip", project_name="LA Data Center",
                      county="Los Angeles", state="CA", mw_it=40, stage="design",
                      category="data_center")
    add_manual_signal(db_session, "manual_tip", "nv tip", project_name="Monarch Data Center",
                      county="Lyon", state="NV", mw_it=40, stage="design",
                      category="data_center")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


def test_board_defaults_to_my_territory(client, db_session, cfg):
    _seed_la_and_nevada(db_session, cfg)
    body = client.get("/board", headers=AUTH).text
    assert "LA Data Center" in body
    assert "Monarch Data Center" not in body


def test_board_territory_all_shows_everything(client, db_session, cfg):
    _seed_la_and_nevada(db_session, cfg)
    body = client.get("/board?territory=all", headers=AUTH).text
    assert "LA Data Center" in body
    assert "Monarch Data Center" in body


def test_board_territory_default_survives_a_category_switch(client, db_session, cfg):
    """The territory chip and the category chips are independent filters --
    switching category must not silently drop back to seeing everything."""
    add_manual_signal(db_session, "manual_tip", "la industrial tip",
                      project_name="LA Warehouse Plant", county="Los Angeles", state="CA",
                      stage="permitting", category="industrial")
    add_manual_signal(db_session, "manual_tip", "nv industrial tip",
                      project_name="Nevada Warehouse Plant", county="Washoe", state="NV",
                      stage="permitting", category="industrial")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    body = client.get("/board?category=industrial", headers=AUTH).text
    assert "LA Warehouse Plant" in body
    assert "Nevada Warehouse Plant" not in body


def test_retrofit_defaults_to_my_territory(client, db_session, cfg):
    la = RetrofitBuilding(apn="LA-1", population="replacement_candidate",
                          address="1 LA Way", county="Los Angeles", state="CA", rank_score=5.0)
    other = RetrofitBuilding(apn="OC-1", population="replacement_candidate",
                             address="1 Orange Way", county="Orange", state="CA", rank_score=5.0)
    db_session.add(la)
    db_session.add(other)
    db_session.commit()

    body = client.get("/retrofit", headers=AUTH).text
    assert "1 LA Way" in body
    assert "1 Orange Way" not in body


def test_retrofit_territory_all_shows_everything(client, db_session, cfg):
    la = RetrofitBuilding(apn="LA-2", population="replacement_candidate",
                          address="2 LA Way", county="Los Angeles", state="CA", rank_score=5.0)
    other = RetrofitBuilding(apn="OC-2", population="replacement_candidate",
                             address="2 Orange Way", county="Orange", state="CA", rank_score=5.0)
    db_session.add(la)
    db_session.add(other)
    db_session.commit()

    body = client.get("/retrofit?territory=all", headers=AUTH).text
    assert "2 LA Way" in body
    assert "2 Orange Way" in body


def test_retrofit_explicit_county_overrides_territory_default(client, db_session, cfg):
    """A rep explicitly picking a county from the dropdown means it -- the
    territory default must not override an explicit choice."""
    la = RetrofitBuilding(apn="LA-3", population="replacement_candidate",
                          address="3 LA Way", county="Los Angeles", state="CA", rank_score=5.0)
    other = RetrofitBuilding(apn="OC-3", population="replacement_candidate",
                             address="3 Orange Way", county="Orange", state="CA", rank_score=5.0)
    db_session.add(la)
    db_session.add(other)
    db_session.commit()

    body = client.get("/retrofit?county=Orange", headers=AUTH).text
    assert "3 Orange Way" in body
    assert "3 LA Way" not in body
