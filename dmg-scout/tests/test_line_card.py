"""Line card view: role-in-a-building grouping, pull-through, the accounts/
projects cross-references that make it a tool instead of a catalog, and the
reverse view on a project's own page. Nothing here touches the score/rank
pipeline -- these are read-only cross-references over data that already
exists."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.accounts import (
    CATEGORY_TO_ROLE,
    ROLE_ORDER,
    ROLE_OVERRIDE_BY_LINE,
    ROLE_PROJECT_RELEVANCE,
    category_is_best_guess,
    create_account,
    facility_types_by_project,
    line_account_matrix,
    line_offering_by_role,
    matching_projects_for_line,
    project_facility_type,
    pull_through,
    resolve_building_role,
    seed_product_lines,
)
from app.db import get_session
from app.models import (
    AccountCoverage, Category, FacilityType, Project, ProductLine, Signal, SignalType,
    ProjectSignal, Stage, Window,
)
from app.web.main import app


def seed(db_session, cfg):
    return seed_product_lines(db_session, cfg)


def _project(session, name, category=Category.data_center, status="active", **kw):
    p = Project(name=name, category=category, county="Los Angeles", state="CA",
               stage=kw.pop("stage", Stage.entitlement), status=status,
               in_territory=True, **kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _link_signal(session, project, facility_type=FacilityType.unknown,
                 signal_type=SignalType.ceqa_nop):
    s = Signal(signal_type=signal_type, category=project.category,
              facility_type=facility_type, project_name=project.name)
    session.add(s)
    session.commit()
    session.refresh(s)
    session.add(ProjectSignal(project_id=project.id, signal_id=s.id))
    session.commit()
    return s


# ---- role resolution -------------------------------------------------------

def test_every_category_maps_to_a_role(cfg):
    lines = cfg.get("accounts.line_card", []) or []
    categories = {entry["category"] for entry in lines}
    for cat in categories:
        assert cat in CATEGORY_TO_ROLE, f"{cat} has no default role"


def test_role_override_wins_over_category_default():
    # Pottorff's category (air_distribution) defaults to air_distribution_terminal,
    # but its actual product (fire/smoke dampers) is life-safety, not a terminal.
    assert resolve_building_role("Pottorff", "air_distribution") == "dampers_life_safety"
    assert CATEGORY_TO_ROLE["air_distribution"] == "air_distribution_terminal"


def test_unoverridden_line_gets_category_default():
    assert resolve_building_role("Some New Line", "cooling_towers") == "heat_rejection"


def test_seed_resolves_building_role_and_markets(db_session, cfg):
    seed(db_session, cfg)
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    assert aaon.building_role == "air_handling"
    pottorff = db_session.exec(select(ProductLine).where(ProductLine.name == "Pottorff")).one()
    assert pottorff.building_role == "dampers_life_safety"
    basx = db_session.exec(select(ProductLine).where(ProductLine.name == "BASX")).one()
    assert basx.markets_served == ["data_center", "healthcare", "labs"]
    assert basx.markets_served_source == "researched"
    assert basx.markets_served_basis
    # Not every line has a market signal -- most should be an empty list, not
    # guessed. (Yaskawa used to be this example; it was researched into
    # config.yaml's markets key on 2026-08-13 and is no longer a good example
    # of an unmapped line.)
    generic = db_session.exec(select(ProductLine).where(ProductLine.name == "Ebtron")).one()
    assert generic.markets_served == []
    assert generic.markets_served_source is None


def test_seed_distinguishes_researched_from_legacy_guess_markets(db_session, cfg):
    """A guess and a sourced fact must never be indistinguishable in the DB --
    see app/models.py's ProductLine.markets_served_source docstring."""
    seed(db_session, cfg)
    # Recold has no `markets:` key in config.yaml, so it falls back to the
    # unsourced MARKETS_BY_LINE table -- must be flagged, not silently mixed
    # in with researched results. (Marley used to be this example; it was
    # researched into config.yaml's markets key on 2026-08-13 as part of the
    # ranked-line-card research pass and is now itself "researched".)
    recold = db_session.exec(select(ProductLine).where(ProductLine.name == "Recold")).one()
    assert recold.markets_served == ["data_center"]
    assert recold.markets_served_source == "legacy_guess"
    assert recold.markets_served_basis is None
    # VU Flow Environmental was researched and came back confirmed-empty --
    # MARKETS_BY_LINE has a non-empty entry for it, and a naive `x or
    # fallback` seed would wrongly resurrect that guess. It must not.
    vu_flow = db_session.exec(
        select(ProductLine).where(ProductLine.name == "VU Flow Environmental")).one()
    assert vu_flow.markets_served == []
    assert vu_flow.markets_served_source == "researched"


def test_every_role_has_at_least_one_line_after_seed(db_session, cfg):
    seed(db_session, cfg)
    lines = db_session.exec(select(ProductLine)).all()
    roles_present = {line.building_role for line in lines}
    assert roles_present == set(ROLE_ORDER)


def test_all_70_lines_get_a_role(db_session, cfg):
    seed(db_session, cfg)
    lines = db_session.exec(select(ProductLine)).all()
    assert len(lines) == 70
    assert all(line.building_role in ROLE_ORDER for line in lines)


# ---- best-guess category flag ----------------------------------------------

def test_exactly_four_lines_are_best_guess(db_session, cfg):
    """2026-08-11 review resolved 6 of the original 12 (VTS, PEP Filters,
    Recold, HCi, CRC corrected; Thermal Corp, DB, Hecoclima confirmed
    correct) and added Cambridge to the flagged set. 2026-08-13's ranked
    line-card research pass resolved 2 more of those six from data already
    in the system rather than new research -- Engineered Comfort (Nailor's
    own OSP-0772 filing already categorizes the product as 'Air
    Conditioning Units / Fan Coil Units', and Islandaire is already
    correctly bucketed under this same vrf_split category) and ChangeAir
    (its own already-completed 5-field research unambiguously describes a
    classroom ERV product, matching air_handling exactly) -- leaving four:
    see app.accounts.NEEDS_VERIFICATION for the remaining names."""
    seed(db_session, cfg)
    lines = db_session.exec(select(ProductLine)).all()
    flagged = [line for line in lines if category_is_best_guess(line)]
    assert len(flagged) == 4


def test_best_guess_marker_is_in_description_not_a_separate_lie(db_session, cfg):
    seed(db_session, cfg)
    db_line = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    assert not category_is_best_guess(db_line)


# ---- pull-through -----------------------------------------------------------

def test_pull_through_reuses_adjacency_and_excludes_own_category(db_session, cfg):
    seed(db_session, cfg)
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    results = pull_through(db_session, cfg, aaon)
    assert results, "AAON should pull through at least one other line"
    categories = {r["line"].category for r in results}
    assert "rooftop_units" not in categories  # never its own category
    names = {r["line"].name for r in results}
    # rooftop_units -> air_distribution (0.8) and controls (0.8) are the two
    # highest edges in config.yaml -- Titus and Yaskawa should both surface.
    assert "Titus" in names
    assert "Yaskawa" in names
    # sorted descending by weight
    weights = [r["weight"] for r in results]
    assert weights == sorted(weights, reverse=True)


def test_pull_through_respects_relevance_floor(db_session, cfg):
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    floor = float(cfg.get("accounts.adjacency.min_relevance_to_show", 0.15))
    results = pull_through(db_session, cfg, line)
    assert all(r["weight"] >= floor for r in results)


# ---- accounts matrix --------------------------------------------------------

def test_line_account_matrix_groups_by_status(db_session, cfg):
    seed(db_session, cfg)
    acc = create_account(db_session, name="Alpha Mechanical", account_type="mechanical_contractor")
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    cov = db_session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == acc.id,
                                      AccountCoverage.product_line_id == aaon.id)).one()
    cov.status = "bought"
    cov.dollar_value = 50000
    db_session.add(cov)
    db_session.commit()

    matrix = line_account_matrix(db_session, aaon.id)
    assert matrix["counts"]["bought"] == 1
    assert matrix["by_status"]["bought"][0]["account"].name == "Alpha Mechanical"
    assert matrix["by_status"]["bought"][0]["coverage"].dollar_value == 50000


# ---- project facility type resolution --------------------------------------

def test_project_facility_type_from_signal(db_session):
    p = _project(db_session, "Cleanroom Job")
    s = Signal(signal_type=SignalType.ceqa_nop, category=Category.data_center,
              facility_type=FacilityType.cleanroom)
    assert project_facility_type(p, [s]) == FacilityType.cleanroom


def test_project_facility_type_data_center_fallback(db_session):
    p = _project(db_session, "DC no facility signal", category=Category.data_center)
    s = Signal(signal_type=SignalType.ceqa_nop, category=Category.data_center,
              facility_type=FacilityType.unknown)
    assert project_facility_type(p, [s]) == FacilityType.data_center


def test_project_facility_type_industrial_stays_unknown(db_session):
    p = _project(db_session, "Industrial no facility signal", category=Category.industrial)
    s = Signal(signal_type=SignalType.ceqa_nop, category=Category.industrial,
              facility_type=FacilityType.unknown)
    assert project_facility_type(p, [s]) == FacilityType.unknown


def test_facility_types_by_project_batches_correctly(db_session):
    p1 = _project(db_session, "DC job", category=Category.data_center)
    _link_signal(db_session, p1, facility_type=FacilityType.cleanroom)
    p2 = _project(db_session, "Industrial job", category=Category.industrial)
    _link_signal(db_session, p2, facility_type=FacilityType.warehouse_conditioned)

    out = facility_types_by_project(db_session, [p1.id, p2.id])
    assert out[p1.id] == FacilityType.cleanroom
    assert out[p2.id] == FacilityType.warehouse_conditioned


# ---- matching projects for a line ------------------------------------------

def test_matching_projects_for_line_filters_by_category(db_session, cfg):
    seed(db_session, cfg)
    marley = db_session.exec(select(ProductLine).where(ProductLine.name == "Marley")).one()
    assert marley.building_role == "heat_rejection"
    assert ROLE_PROJECT_RELEVANCE["heat_rejection"]["categories"] == ["data_center", "industrial"]

    dc = _project(db_session, "DC project")
    _link_signal(db_session, dc)
    esco = _project(db_session, "ESCO project", category=Category.esco)
    _link_signal(db_session, esco)

    matched = matching_projects_for_line(db_session, marley)
    ids = {p.id for p in matched}
    assert dc.id in ids
    assert esco.id not in ids  # heat_rejection excludes esco -- see ROLE_PROJECT_RELEVANCE


def test_matching_projects_for_line_excludes_inactive(db_session, cfg):
    seed(db_session, cfg)
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    dead = _project(db_session, "Dead project", status="dead")
    _link_signal(db_session, dead)
    matched = matching_projects_for_line(db_session, aaon)
    assert dead.id not in {p.id for p in matched}


def test_matching_projects_for_line_narrows_by_facility_type(db_session, cfg):
    seed(db_session, cfg)
    seresco = db_session.exec(select(ProductLine).where(ProductLine.name == "Seresco")).one()
    assert seresco.building_role == "humidification"
    cleanroom_dc = _project(db_session, "Cleanroom DC")
    _link_signal(db_session, cleanroom_dc, facility_type=FacilityType.cleanroom)
    warehouse = _project(db_session, "Warehouse job", category=Category.industrial)
    _link_signal(db_session, warehouse, facility_type=FacilityType.warehouse_conditioned)

    matched = matching_projects_for_line(db_session, seresco)
    ids = {p.id for p in matched}
    assert cleanroom_dc.id in ids
    assert warehouse.id not in ids  # humidification's facility_types list excludes warehouse


# ---- reverse view: line_offering_by_role -----------------------------------

def test_line_offering_by_role_flags_relevant_and_gap(db_session, cfg):
    seed(db_session, cfg)
    offerings = line_offering_by_role(db_session, Category.data_center, FacilityType.cleanroom)
    by_role = {o.role: o for o in offerings}
    assert set(by_role) == set(ROLE_ORDER)
    # air_handling is relevant to a DC cleanroom and the card has lines for it
    assert by_role["air_handling"].relevant is True
    assert by_role["air_handling"].lines
    assert by_role["air_handling"].gap is False
    # every role currently has at least one line, so nothing should gap today
    assert all(not o.gap for o in offerings)


def test_line_offering_by_role_marks_irrelevant_roles(db_session, cfg):
    seed(db_session, cfg)
    # heating_specialty is scoped to industrial/esco only -- not a data_center role
    offerings = line_offering_by_role(db_session, Category.data_center, FacilityType.data_center)
    by_role = {o.role: o for o in offerings}
    assert by_role["heating_specialty"].relevant is False


# ---- web routes --------------------------------------------------------

@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def test_lines_index_renders(client, db_session, cfg):
    seed(db_session, cfg)
    resp = client.get("/lines", headers=AUTH)
    assert resp.status_code == 200
    assert "Line card" in resp.text
    assert "4 of 70" in resp.text


def test_lines_index_role_filter(client, db_session, cfg):
    seed(db_session, cfg)
    resp = client.get("/lines?role=heat_rejection", headers=AUTH)
    assert resp.status_code == 200
    assert "Marley" in resp.text
    assert "AAON" not in resp.text  # AAON is air_handling, not shown on this tab


def test_lines_index_market_filter(client, db_session, cfg):
    seed(db_session, cfg)
    resp = client.get("/lines?role=air_handling&market=data_center", headers=AUTH)
    assert resp.status_code == 200
    assert "BASX" in resp.text
    assert "ClimateCraft" not in resp.text  # no market mapped for ClimateCraft


def test_line_detail_renders(client, db_session, cfg):
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.name == "Energy Labs")).one()
    resp = client.get(f"/line/{line.id}", headers=AUTH)
    assert resp.status_code == 200
    assert "Energy Labs" in resp.text
    assert "96" in resp.text  # lead_time_weeks_low from config.yaml
    assert "roughly two-year lead time" in resp.text.lower()


def test_line_detail_404(client, db_session, cfg):
    resp = client.get("/line/999999", headers=AUTH)
    assert resp.status_code == 404


def test_project_detail_shows_line_card_fit(client, db_session, cfg):
    seed(db_session, cfg)
    p = _project(db_session, "Reverse view project")
    _link_signal(db_session, p, facility_type=FacilityType.cleanroom)
    resp = client.get(f"/project/{p.id}", headers=AUTH)
    assert resp.status_code == 200
    assert "Line card fit" in resp.text
    assert "Air handling" in resp.text
