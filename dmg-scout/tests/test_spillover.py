"""Phase 4: county-adjacency data center spillover. See app/pipeline/spillover.py.

Storey/Washoe is the case that motivated county adjacency over county-exact
matching (TRIC sits in Storey County, its workforce lives in Washoe) — that
pairing is asserted directly against config so a future edit to
scoring.spillover.county_adjacency can't silently drop it.
"""
from datetime import timedelta

from app.models import Category, IeprForwardLoad, Project, Window, utcnow
from app.pipeline.size_score import run_size_score
from app.pipeline.spillover import (
    county_spillover_mw, project_spillover, spillover_factor,
)


def _dc(name, county, state, mw, status="active", days_ago=0):
    return Project(
        name=name, category=Category.data_center, county=county, state=state,
        status=status, in_territory=True, window=Window.PRE_BOD,
        mw_total=mw, last_signal_at=utcnow() - timedelta(days=days_ago),
    )


def test_storey_washoe_adjacency_is_configured(cfg):
    table = cfg.get("scoring.spillover.county_adjacency", {})
    assert "Washoe" in table.get("NV", {}).get("Storey", []), (
        "Storey/TRIC's workforce commutes from Washoe (Reno/Sparks) — the case "
        "that motivated adjacency over exact-county match. See module docstring.")


def test_spillover_factor_saturates_at_configured_weight(cfg):
    saturation = cfg.get("scoring.spillover.saturation_mw", 500)
    weight = cfg.get("scoring.spillover.weight", 0.15)
    assert spillover_factor(cfg, 0) == 1.0
    assert spillover_factor(cfg, saturation) == 1.0 + weight
    assert spillover_factor(cfg, saturation * 100) == 1.0 + weight, "must cap, not grow unbounded"
    assert 1.0 < spillover_factor(cfg, saturation / 2) < 1.0 + weight


def test_project_excludes_its_own_mw_from_its_own_spillover(cfg, db_session):
    """A lone data center must not be able to boost its own score off its own
    presence — spillover has to be external activity, not self-reference."""
    solo = _dc("Solo DC", "Tulare", "CA", mw=200)
    db_session.add(solo)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw, basis = project_spillover(cfg, totals, solo)
    assert mw == 0.0 and basis is None


def test_project_is_credited_by_a_second_project_in_the_same_county(cfg, db_session):
    a = _dc("Project A", "Tulare", "CA", mw=100)
    b = _dc("Project B", "Tulare", "CA", mw=150)
    db_session.add(a)
    db_session.add(b)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw_a, basis_a = project_spillover(cfg, totals, a)
    assert mw_a == 150.0, "A's own 100 excluded; only B's 150 counts"
    assert basis_a and "Tulare" in basis_a


def test_project_is_credited_by_an_adjacent_county_not_just_exact_match(cfg, db_session):
    storey_project = _dc("TRIC Project", "Storey", "NV", mw=50)
    washoe_project = _dc("Reno Project", "Washoe", "NV", mw=300)
    db_session.add(storey_project)
    db_session.add(washoe_project)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw, basis = project_spillover(cfg, totals, storey_project)
    assert mw == 300.0, "Storey's own 50 excluded (self); Washoe's 300 is adjacent, not same-county"
    assert "adjacent" in basis


def test_far_away_county_contributes_nothing(cfg, db_session):
    """Los Angeles is not adjacent to Storey — no shared border, and it is not
    in the configured adjacency list, so it must not leak in."""
    storey_project = _dc("TRIC Project", "Storey", "NV", mw=50)
    la_project = _dc("Unrelated LA Project", "Los Angeles", "CA", mw=900)
    db_session.add(storey_project)
    db_session.add(la_project)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw, basis = project_spillover(cfg, totals, storey_project)
    assert mw == 0.0 and basis is None


def test_old_activity_decays_toward_zero(cfg, db_session):
    halflife = cfg.get("scoring.spillover.halflife_days", 365)
    recent = _dc("Recent", "Tulare", "CA", mw=100, days_ago=0)
    ancient = _dc("Ancient", "Tulare", "CA", mw=100, days_ago=halflife * 10)
    db_session.add(recent)
    db_session.add(ancient)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw_recent, _ = project_spillover(cfg, totals, recent)
    mw_ancient, _ = project_spillover(cfg, totals, ancient)
    # recent credited with (decayed) ancient contribution; ancient credited
    # with full-strength recent contribution — ancient's own decayed-near-zero
    # MW is excluded from its own total either way.
    assert mw_ancient > mw_recent, "an unaged neighbor should count for more than a decade-stale one"


def test_lost_and_dead_projects_do_not_count_as_recent_activity(cfg, db_session):
    dead = _dc("Dead Project", "Tulare", "CA", mw=500, status="dead")
    other = _dc("Live Project", "Tulare", "CA", mw=10)
    db_session.add(dead)
    db_session.add(other)
    db_session.commit()
    totals = county_spillover_mw(db_session, cfg)
    mw, _ = project_spillover(cfg, totals, other)
    assert mw == 0.0, "a dead project never built anything and must not count as workforce activity"


def test_iepr_layer_feeds_county_totals_discounted_by_churn(cfg, db_session):
    db_session.add(IeprForwardLoad(
        utility="SCE", docket_tn="1", source_url="https://efiling.energy.ca.gov/x",
        status="Active Inqury", city="Irvine", county="Orange", state="CA",
        requested_peak_mw=100.0))
    db_session.commit()
    discount = cfg.get("iepr.churn_discount", 0.0)
    totals = county_spillover_mw(db_session, cfg)
    assert totals[("CA", "Orange")] == 100.0 * (1.0 - discount)


def test_run_size_score_writes_spillover_fields_onto_the_project(cfg, db_session):
    a = _dc("Project A", "Tulare", "CA", mw=100, status="active")
    b = _dc("Project B", "Tulare", "CA", mw=400, status="active")
    db_session.add(a)
    db_session.add(b)
    db_session.commit()
    run_size_score(db_session, cfg)
    db_session.refresh(a)
    # Wiring check: run_size_score populates these from the spillover module.
    # The projects carry no linked Signal, so priority_score's certainty term
    # is legitimately 0 and score stays 0 regardless — see test_scoring.py
    # for the multiplier's own behavior.
    assert a.spillover_mw == 400.0
    assert a.spillover_basis and "Tulare" in a.spillover_basis


def test_run_size_score_leaves_isolated_project_spillover_null(cfg, db_session):
    solo = _dc("Solo", "Imperial", "CA", mw=20, status="active")
    db_session.add(solo)
    db_session.commit()
    run_size_score(db_session, cfg)
    db_session.refresh(solo)
    assert solo.spillover_mw is None
    assert solo.spillover_basis is None
