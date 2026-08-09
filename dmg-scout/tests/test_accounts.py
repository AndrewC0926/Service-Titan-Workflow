"""Accounts module: line-card seeding, adjacency gap ranking, replacement
windows, and the CSV importer. Nothing here touches the project pipeline."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.accounts import (
    account_replacement_windows,
    account_type_modifier,
    category_affinity,
    compute_gaps,
    create_account,
    ensure_coverage_rows,
    seed_product_lines,
    value_tier_band,
)
from app.db import get_session
from app.importers.accounts_csv import (
    commit_import,
    guess_mapping,
    parse_csv,
    preview_import,
)
from app.models import Account, AccountCoverage, ProductLine
from app.web.main import app


def seed(db_session, cfg):
    added = seed_product_lines(db_session, cfg)
    return added


def bought(db_session, account, line_name, install_year=None, dollar_value=None):
    line = db_session.exec(select(ProductLine).where(ProductLine.name == line_name)).one()
    cov = db_session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == account.id,
                                      AccountCoverage.product_line_id == line.id)).one()
    cov.status = "bought"
    cov.install_year = install_year
    cov.dollar_value = dollar_value
    db_session.add(cov)
    db_session.commit()


# ---- seeding -------------------------------------------------------------

def test_seed_product_lines_idempotent(db_session, cfg):
    first = seed(db_session, cfg)
    assert first == 70
    second = seed_product_lines(db_session, cfg)
    assert second == 0
    assert db_session.exec(select(ProductLine)).all().__len__() == 70


def test_seed_product_lines_no_duplicate_for_both_firm_lines(db_session, cfg):
    seed(db_session, cfg)
    lg_rows = db_session.exec(select(ProductLine).where(ProductLine.name == "LG")).all()
    assert len(lg_rows) == 1
    assert lg_rows[0].firm == "both"


def test_seed_product_lines_marley_recold_carry_unverified_heat_rejection_mode(db_session, cfg):
    """The two lines named as carrying adiabatic/hybrid capability -- stated
    by the rep, not inferred from the brand -- must land unverified until
    confirmed with the factory."""
    seed(db_session, cfg)
    for name in ("Marley", "Recold"):
        line = db_session.exec(select(ProductLine).where(ProductLine.name == name)).one()
        assert line.heat_rejection_mode == "adiabatic_hybrid"
        assert line.heat_rejection_mode_verified is False
        assert line.heat_rejection_mode_basis, "an unverified capability must still carry who stated it and when"


def test_seed_product_lines_other_lines_have_no_invented_heat_rejection_mode(db_session, cfg):
    """No brand-name inference for lines nobody has actually stated a
    heat-rejection capability for."""
    seed(db_session, cfg)
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()
    assert aaon.heat_rejection_mode is None
    assert aaon.heat_rejection_mode_verified is False


# ---- affinity / adjacency -------------------------------------------------

def test_category_affinity_self_is_zero(cfg):
    assert category_affinity(cfg, "rooftop_units", "rooftop_units") == 0.0


def test_category_affinity_unlisted_pair_is_zero(cfg):
    assert category_affinity(cfg, "rooftop_units", "humidification_dehumidification") == 0.0


def test_category_affinity_symmetric(cfg):
    a = category_affinity(cfg, "rooftop_units", "controls")
    b = category_affinity(cfg, "controls", "rooftop_units")
    assert a == b > 0


def test_account_type_modifier_engineer_reduced(cfg):
    assert account_type_modifier(cfg, "engineer", "rooftop_units") < 1.0
    assert account_type_modifier(cfg, "mechanical_contractor", "rooftop_units") == 1.0


def test_value_tier_band_ordering(cfg):
    t1 = value_tier_band(cfg, 1)
    t4 = value_tier_band(cfg, 4)
    assert t1[0] > t4[1]  # tier 1's floor is above tier 4's ceiling


# ---- gap ranking: the AAON example from the spec --------------------------

def test_aaon_account_has_no_use_for_seresco_or_monoxivent(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="AAON-Only Mechanical", account_type="mechanical_contractor")
    bought(db_session, account, "AAON")

    gaps = compute_gaps(db_session, cfg, account.id)
    names = {g.line.name for g in gaps}
    assert "Seresco" not in names
    assert "Monoxivent" not in names


def test_aaon_account_gap_ranking_favors_yaskawa_pottorff_titus(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="AAON-Only Mechanical 2", account_type="mechanical_contractor")
    bought(db_session, account, "AAON")

    gaps = compute_gaps(db_session, cfg, account.id)
    names = {g.line.name for g in gaps}
    for expected in ("Yaskawa", "Pottorff", "Titus", "Vibro-Acoustics"):
        assert expected in names, f"{expected} should be a visible gap for a rooftop account"


def test_gaps_ranked_by_dollar_value_not_count(db_session, cfg):
    """The spec's non-negotiable: a missing tier-1 line outranks a missing
    tier-4 line even at lower relevance."""
    seed(db_session, cfg)
    account = create_account(db_session, name="Rank Test Co", account_type="mechanical_contractor")
    bought(db_session, account, "AAON")

    gaps = compute_gaps(db_session, cfg, account.id)
    top = gaps[0]
    assert top.line.value_tier == 1, "highest-ranked gap should be tier 1, not a cheap high-relevance line"


def test_no_coverage_account_still_gets_ranked_gaps(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Blank Slate Co", account_type="mechanical_contractor")
    gaps = compute_gaps(db_session, cfg, account.id)
    assert len(gaps) > 0


def test_engineer_account_gaps_discounted_vs_mechanical_contractor(db_session, cfg):
    seed(db_session, cfg)
    mc = create_account(db_session, name="MC Co", account_type="mechanical_contractor")
    eng = create_account(db_session, name="Engineer Co", account_type="engineer")
    bought(db_session, mc, "AAON")
    bought(db_session, eng, "AAON")

    mc_gaps = {g.line.name: g.gap_score for g in compute_gaps(db_session, cfg, mc.id)}
    eng_gaps = {g.line.name: g.gap_score for g in compute_gaps(db_session, cfg, eng.id)}
    common = set(mc_gaps) & set(eng_gaps)
    assert common
    for name in common:
        assert eng_gaps[name] < mc_gaps[name]


# ---- replacement windows ---------------------------------------------------

def test_replacement_window_reuses_existing_service_life_table(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Replacement Test Co", ownership_type="private_commercial")
    bought(db_session, account, "AAON", install_year=2008)  # 18yr old in 2026

    windows = account_replacement_windows(db_session, cfg, account.id)
    assert len(windows) == 1
    w = windows[0]
    assert w.line.name == "AAON"
    assert w.status == "overdue"  # private_commercial packaged_rooftop high=17
    assert "packaged rooftop" in w.basis
    assert "UNVERIFIED" in w.basis


def test_replacement_window_branches_by_ownership(db_session, cfg):
    seed(db_session, cfg)
    private_acct = create_account(db_session, name="Private Owner Co", ownership_type="private_commercial")
    federal_acct = create_account(db_session, name="Federal Owner Co", ownership_type="federal")
    bought(db_session, private_acct, "AAON", install_year=2018)  # 8yr old
    bought(db_session, federal_acct, "AAON", install_year=2018)

    private_status = account_replacement_windows(db_session, cfg, private_acct.id)
    federal_status = account_replacement_windows(db_session, cfg, federal_acct.id, include_not_due=True)
    # 8yr is nowhere near due on the 15-17yr private band...
    assert not any(w.status in ("due", "overdue") for w in private_status)
    # ...but IS live on the federal 7-10yr planned-replacement band.
    assert any(w.status in ("due", "overdue") for w in federal_status)


def test_replacement_window_absent_for_lines_with_no_equipment_type(db_session, cfg):
    """Most of the line card (controls, filters, dampers...) has no ASHRAE-
    style service life on file — abstain, don't invent a window. Neptronic
    (controls) has equipment_type: null in config.yaml, unlike Titus/Nailor
    (vav_terminal) or AAON (packaged_rooftop), which DO have a band."""
    seed(db_session, cfg)
    line = db_session.exec(select(ProductLine).where(ProductLine.name == "Neptronic")).one()
    assert line.equipment_type is None
    account = create_account(db_session, name="Neptronic Only Co")
    bought(db_session, account, "Neptronic", install_year=2005)  # 21yr old, if it counted
    windows = account_replacement_windows(db_session, cfg, account.id, include_not_due=True)
    assert windows == []


def test_replacement_window_requires_install_year(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="No Install Year Co")
    bought(db_session, account, "AAON", install_year=None)
    assert account_replacement_windows(db_session, cfg, account.id) == []


# ---- coverage rows ----------------------------------------------------------

def test_ensure_coverage_rows_creates_one_per_line(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Coverage Rows Co")
    rows = db_session.exec(select(AccountCoverage).where(AccountCoverage.account_id == account.id)).all()
    assert len(rows) == 70
    assert all(r.status == "unknown" for r in rows)


def test_ensure_coverage_rows_picks_up_new_line(db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Late Line Co")
    db_session.add(ProductLine(name="Brand New Line", name_norm="brand new line",
                               firm="DMG", category="controls", value_tier=3))
    db_session.commit()
    added = ensure_coverage_rows(db_session, account)
    assert added == 1
    rows = db_session.exec(select(AccountCoverage).where(AccountCoverage.account_id == account.id)).all()
    assert len(rows) == 71


# ---- CSV importer -----------------------------------------------------------

SAMPLE_CSV = (
    "Company Name,Type,County,State,Rep,Last Order\n"
    "Acme Mechanical,Mechanical Contractor,San Bernardino,CA,Andrew,2023-04-12\n"
    "Acme Mechanicl Inc,GC,Riverside,CA,Andrew,2022-01-05\n"
    ",GC,Orange,CA,Andrew,\n"  # no name -> skipped
)


def test_guess_mapping_finds_obvious_columns():
    headers = ["Company Name", "Type", "County", "State", "Rep", "Last Order"]
    mapping = guess_mapping(headers)
    assert mapping["Company Name"] == "name"
    assert mapping["County"] == "county"
    assert mapping["State"] == "state"
    assert mapping["Last Order"] == "last_order_date"


def test_parse_csv_roundtrip():
    headers, rows = parse_csv(SAMPLE_CSV)
    assert headers == ["Company Name", "Type", "County", "State", "Rep", "Last Order"]
    assert len(rows) == 3
    assert rows[0]["Company Name"] == "Acme Mechanical"


def test_preview_flags_within_batch_near_duplicate(db_session, cfg):
    headers, rows = parse_csv(SAMPLE_CSV)
    mapping = guess_mapping(headers)
    preview = preview_import(db_session, headers, rows, mapping)
    assert preview[0].action == "create"
    assert preview[0].fuzzy_candidates == []
    assert preview[1].action == "create"
    assert preview[1].fuzzy_candidates  # flagged against row 1
    assert preview[2].error == "no account name"


def test_commit_import_creates_accounts_with_coverage(db_session, cfg):
    seed(db_session, cfg)
    headers, rows = parse_csv(SAMPLE_CSV)
    mapping = guess_mapping(headers)
    result = commit_import(db_session, headers, rows, mapping)
    assert result["created"] == 2
    assert result["skipped"] == 1
    assert len(result["flagged"]) == 1

    acme = db_session.exec(select(Account).where(Account.name == "Acme Mechanical")).one()
    assert acme.county == "San Bernardino"
    assert acme.account_type == "mechanical_contractor"
    rows_ = db_session.exec(select(AccountCoverage).where(AccountCoverage.account_id == acme.id)).all()
    assert len(rows_) == 70


def test_commit_import_updates_existing_without_blanking_fields(db_session, cfg):
    seed(db_session, cfg)
    existing = create_account(db_session, name="Acme Mechanical", county="Riverside",
                              notes="hand-entered note")
    headers, rows = parse_csv(SAMPLE_CSV)
    mapping = guess_mapping(headers)
    result = commit_import(db_session, headers, rows, mapping)
    assert result["updated"] >= 1
    db_session.refresh(existing)
    assert existing.county == "San Bernardino"  # overwritten by non-empty CSV cell
    assert existing.notes == "hand-entered note"  # CSV had no notes column mapped — untouched


def test_account_type_normalized_from_free_text(db_session, cfg):
    seed(db_session, cfg)
    headers, rows = parse_csv(SAMPLE_CSV)
    mapping = guess_mapping(headers)
    commit_import(db_session, headers, rows, mapping)
    gc_row = db_session.exec(select(Account).where(Account.name == "Acme Mechanicl Inc")).one()
    assert gc_row.account_type == "gc"


# ---- route smoke tests -------------------------------------------------------

@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def test_accounts_list_requires_auth(client):
    assert client.get("/accounts").status_code == 401


def test_accounts_list_renders_empty(client):
    r = client.get("/accounts", headers=AUTH)
    assert r.status_code == 200
    assert "No accounts yet" in r.text


def test_accounts_new_form_renders(client):
    r = client.get("/accounts/new", headers=AUTH)
    assert r.status_code == 200
    assert "New account" in r.text


def test_account_create_and_detail_flow(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.post("/accounts", headers=AUTH, data={
        "name": "Route Test Mechanical", "account_type": "mechanical_contractor",
        "county": "Riverside", "state": "CA", "assigned_rep": "Andrew",
    }, follow_redirects=False)
    assert r.status_code == 303
    account_id = int(r.headers["location"].rsplit("/", 1)[-1])

    detail = client.get(f"/accounts/{account_id}", headers=AUTH)
    assert detail.status_code == 200
    assert "Route Test Mechanical" in detail.text
    assert detail.text.count('id="cov-') == 70


def test_coverage_update_endpoint_saves_and_returns_row(client, db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Coverage Endpoint Co")
    line = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()

    r = client.post(f"/accounts/{account.id}/coverage/{line.id}", headers=AUTH, data={
        "status": "bought", "install_year": "2015", "dollar_value": "120000",
    })
    assert r.status_code == 200
    assert 'class="cov-bought"' in r.text
    assert "120000" in r.text

    db_session.expire_all()
    cov = db_session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == account.id,
                                      AccountCoverage.product_line_id == line.id)).one()
    assert cov.status == "bought"
    assert cov.install_year == 2015


def test_account_brief_renders(client, db_session, cfg):
    seed(db_session, cfg)
    account = create_account(db_session, name="Brief Test Co")
    bought(db_session, account, "AAON", install_year=2010, dollar_value=100000)
    r = client.get(f"/accounts/{account.id}/brief", headers=AUTH)
    assert r.status_code == 200
    assert "account brief" in r.text
    assert "AAON" in r.text


def test_import_upload_and_preview_flow(client, db_session, cfg):
    r = client.get("/accounts/import", headers=AUTH)
    assert r.status_code == 200

    r = client.post("/accounts/import/preview", headers=AUTH,
                    files={"file": ("test.csv", SAMPLE_CSV, "text/csv")})
    assert r.status_code == 200
    assert "Acme Mechanical" in r.text
    assert "similar to" in r.text
