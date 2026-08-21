"""Two boards, one pipeline: triage classifies, and the category is load-bearing.

The bug these guard against: triage used to answer a boolean, so a new industrial
building was indistinguishable from a moratorium — both "not a data center", both
dropped. 39 real Nevada prospects went in the bin. Category makes the drop a
routing decision instead.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.manual import add_manual_signal
from app.models import Category, Project, RawDocument, Signal, SignalType, Stage, TriageResult
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.pipeline.triage import run_triage
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def _doc(db_session, uid="d1", text="a new 200,000 sqft facility in Storey County") -> RawDocument:
    doc = RawDocument(source="goed", source_uid=uid, url=f"https://x/{uid}",
                      title="GOED: APPLICATION", raw_text=text,
                      content_hash=uid, triage_result=TriageResult.pending)
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


def _fake_triage(monkeypatch, **verdict):
    import app.pipeline.triage as tri
    monkeypatch.setattr(tri, "triage", lambda *a, **k: verdict)


# --- triage classifies ----------------------------------------------------


@pytest.mark.parametrize("category,expect_result,expect_cat", [
    ("data_center", TriageResult.relevant, Category.data_center),
    ("industrial", TriageResult.relevant, Category.industrial),
    ("other", TriageResult.irrelevant, Category.other),
])
def test_triage_routes_each_category(db_session, cfg, monkeypatch,
                                     category, expect_result, expect_cat):
    doc = _doc(db_session)
    _fake_triage(monkeypatch, category=category, names_location=True, reason="r")
    run_triage(db_session, cfg, limit=10)
    db_session.refresh(doc)
    assert doc.triage_result is expect_result
    assert doc.meta["triage_category"] == expect_cat.value


def test_industrial_is_kept_not_dropped(db_session, cfg, monkeypatch):
    """The whole point: an industrial building is relevant, and a boolean triage
    would have thrown it away."""
    doc = _doc(db_session, text="new 624,000 sqft manufacturing plant, Reno NV")
    _fake_triage(monkeypatch, category="industrial", names_location=True,
                 reason="new manufacturing plant")
    stats = run_triage(db_session, cfg, limit=10)
    db_session.refresh(doc)
    assert doc.triage_result is TriageResult.relevant
    assert stats["industrial"] == 1 and stats["irrelevant"] == 0


def test_no_named_location_is_never_relevant(db_session, cfg, monkeypatch):
    """A project with no location cannot be resolved, sized or sold to."""
    doc = _doc(db_session)
    _fake_triage(monkeypatch, category="data_center", names_location=False, reason="r")
    run_triage(db_session, cfg, limit=10)
    db_session.refresh(doc)
    assert doc.triage_result is TriageResult.irrelevant
    assert doc.meta["triage_category"] == Category.other.value


def test_unknown_category_is_dropped_not_guessed(db_session, cfg, monkeypatch):
    doc = _doc(db_session)
    _fake_triage(monkeypatch, category="warehouse-ish", names_location=True, reason="r")
    run_triage(db_session, cfg, limit=10)
    db_session.refresh(doc)
    assert doc.triage_result is TriageResult.irrelevant
    assert doc.meta["triage_category"] == Category.other.value


# --- category is a hard blocking key -------------------------------------


def _signal(db_session, **kw) -> Signal:
    defaults = dict(signal_type=SignalType.abatement_application, stage=Stage.permitting,
                    confidence=0.9, summary_one_line="s", category=Category.data_center)
    defaults.update(kw)
    s = Signal(**defaults)
    db_session.add(s)
    db_session.commit()
    return s


def test_same_developer_across_categories_never_merges(db_session, cfg):
    """One company can build a data center AND a factory in the same county. Shared
    developer name alone would have merged them into one project."""
    _signal(db_session, project_name="Hyve Data Center", county="Washoe", state="NV",
            developer_or_owner="Hyve Solutions", category=Category.data_center)
    run_resolve(db_session, cfg, use_llm=False)
    _signal(db_session, project_name="Hyve Manufacturing Plant", county="Washoe", state="NV",
            developer_or_owner="Hyve Solutions", category=Category.industrial)
    run_resolve(db_session, cfg, use_llm=False)

    projects = db_session.exec(select(Project)).all()
    assert len(projects) == 2
    assert {p.category for p in projects} == {Category.data_center, Category.industrial}


def test_project_inherits_category_from_signal(db_session, cfg):
    _signal(db_session, project_name="Big Warehouse", county="Clark", state="NV",
            category=Category.industrial)
    run_resolve(db_session, cfg, use_llm=False)
    assert db_session.exec(select(Project)).one().category is Category.industrial


# --- the board defaults to data centers ----------------------------------


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_both(db_session, cfg):
    add_manual_signal(db_session, "manual_tip", "dc tip", project_name="Alpha Data Center",
                      county="Storey", state="NV", mw_it=40, stage="design",
                      category="data_center")
    add_manual_signal(db_session, "manual_tip", "industrial tip",
                      project_name="Beta Manufacturing Plant", county="Washoe", state="NV",
                      stage="permitting", category="industrial")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


def test_board_defaults_to_data_center(client, db_session, cfg):
    _seed_both(db_session, cfg)
    body = client.get("/board?territory=all", headers=AUTH).text
    assert "Alpha Data Center" in body
    assert "Beta Manufacturing Plant" not in body


def test_board_industrial_tab(client, db_session, cfg):
    _seed_both(db_session, cfg)
    body = client.get("/board?category=industrial&territory=all", headers=AUTH).text
    assert "Beta Manufacturing Plant" in body
    assert "Alpha Data Center" not in body


def test_board_all_shows_both(client, db_session, cfg):
    _seed_both(db_session, cfg)
    body = client.get("/board?category=all&territory=all", headers=AUTH).text
    assert "Alpha Data Center" in body and "Beta Manufacturing Plant" in body


def test_unknown_category_shows_everything_not_nothing(client, db_session, cfg):
    """A typo in the query string must not render a convincingly empty board."""
    _seed_both(db_session, cfg)
    body = client.get("/board?category=nonsense&territory=all", headers=AUTH).text
    assert "Alpha Data Center" in body and "Beta Manufacturing Plant" in body


def test_manual_signal_defaults_onto_the_data_center_board(client, db_session, cfg):
    """Manual entries skip triage, so nothing else would set their category. If it
    defaulted to `other` a hand-entered tip would be invisible on every board."""
    add_manual_signal(db_session, "prequal_invite", "ACCO prequal",
                      project_name="Gamma Data Center", county="Clark", state="NV",
                      mw_it=20, stage="design")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    assert db_session.exec(select(Project)).one().category is Category.data_center
    assert "Gamma Data Center" in client.get("/board?territory=all", headers=AUTH).text


def test_csv_export_respects_category(client, db_session, cfg):
    _seed_both(db_session, cfg)
    dc = client.get("/export/board.csv?category=data_center", headers=AUTH).text
    ind = client.get("/export/board.csv?category=industrial", headers=AUTH).text
    assert "Alpha Data Center" in dc and "Beta Manufacturing Plant" not in dc
    assert "Beta Manufacturing Plant" in ind and "Alpha Data Center" not in ind
    assert "category" in dc.splitlines()[0]
