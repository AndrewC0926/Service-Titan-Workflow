"""Human-sourced project intelligence: creation, the Firm/Account name
join, confirmation-candidate surfacing, and confirmation itself. Nothing
here touches app.pipeline.resolve/size_score/grounding -- see
app.field_intel's own module docstring for why."""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.accounts import create_account
from app.field_intel import (
    confirm_field_intel,
    confirmation_candidates,
    create_field_intel,
    field_intel_activity,
    firm_active_projects,
    resolve_named_entity,
)
from app.models import FieldIntel, Firm, Project, ProjectFirm, utcnow
from app.normalize import normalize_company_name, normalize_name


def _firm(session, name, firm_type="mech_contractor"):
    f = Firm(name=name, name_norm=normalize_name(name), firm_type=firm_type)
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


def _project(session, name, developer=None, status="active", stage="unknown"):
    p = Project(name=name, developer=developer, status=status, stage=stage)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _link(session, project, firm, role="unknown"):
    session.add(ProjectFirm(project_id=project.id, firm_id=firm.id, role=role))
    session.commit()


# ---- create_field_intel: required fields, never-infer ---------------------

def test_requires_reported_by(db_session):
    with pytest.raises(ValueError, match="reported_by"):
        create_field_intel(db_session, reported_by="", reported_at=datetime(2026, 8, 25),
                           source_notes="Told me about a job.")


def test_requires_source_notes(db_session):
    with pytest.raises(ValueError, match="source_notes"):
        create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                           source_notes="")


def test_minimal_record_leaves_every_pipe_field_empty_not_guessed(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim (ACME GC)",
                               reported_at=datetime(2026, 8, 25),
                               source_notes="Mentioned a job but didn't give details.")
    assert intel.owner is None
    assert intel.location is None
    assert intel.size_scope is None
    assert intel.expected_timing is None
    assert intel.engineer_name is None
    assert intel.engineer_firm_id is None
    assert intel.engineer_account_id is None
    assert intel.mech_contractor_name is None
    assert intel.mech_contractor_firm_id is None
    assert intel.mech_contractor_account_id is None
    assert intel.stage.value == "unknown"
    assert intel.status == "active"
    assert intel.confirmed_project_id is None


def test_full_record_keeps_exactly_what_was_stated(db_session):
    intel = create_field_intel(
        db_session, reported_by="Dave Kim (ACME GC)", reported_at=datetime(2026, 8, 20),
        source_notes="Says they're pursuing a cold storage job in Fontana for a private owner.",
        owner="Fontana Cold Co", location="Fontana, San Bernardino Co",
        size_scope="~150,000 sqft cold storage", stage="design", expected_timing="bidding Q1 2027",
    )
    assert intel.owner == "Fontana Cold Co"
    assert intel.location == "Fontana, San Bernardino Co"
    assert intel.size_scope == "~150,000 sqft cold storage"
    assert intel.stage.value == "design"
    assert intel.expected_timing == "bidding Q1 2027"


# ---- resolve_named_entity / the Firm+Account join --------------------------

def test_resolve_named_entity_none_for_blank_name(db_session):
    assert resolve_named_entity(db_session, None) == {"firm": None, "account": None}
    assert resolve_named_entity(db_session, "   ") == {"firm": None, "account": None}


def test_resolve_named_entity_matches_an_existing_firm(db_session):
    firm = _firm(db_session, "Acme Mechanical Engineering", firm_type="mep")
    result = resolve_named_entity(db_session, "Acme Mechanical Engineering, Inc.")
    assert result["firm"].id == firm.id
    assert result["account"] is None


def test_resolve_named_entity_distinguishes_normalize_name_blast_radius_firms(db_session):
    """resolve_named_entity's Firm side goes through app.firms.match_firm,
    which now keys on normalize_company_name -- so 'MCM Engineering' and
    'P2S Engineering', which normalize_name collides to bare 'engineering',
    must resolve to their own distinct Firm rows here, the field-intel
    exact match named directly in the Phase B blast-radius fix."""
    mcm = Firm(name="MCM Engineering, Inc.",
               name_norm=normalize_company_name("MCM Engineering, Inc."), firm_type="mep")
    p2s = Firm(name="P2S Engineering, Inc.",
               name_norm=normalize_company_name("P2S Engineering, Inc."), firm_type="mep")
    db_session.add(mcm)
    db_session.add(p2s)
    db_session.commit()

    assert resolve_named_entity(db_session, "MCM Engineering, Inc.")["firm"].id == mcm.id
    assert resolve_named_entity(db_session, "P2S Engineering, Inc.")["firm"].id == p2s.id


def test_resolve_named_entity_matches_an_existing_account(db_session):
    account = create_account(db_session, name="Southland Air Systems", account_type="mechanical_contractor")
    result = resolve_named_entity(db_session, "Southland Air Systems LLC")
    assert result["account"].id == account.id
    assert result["firm"] is None


def test_resolve_named_entity_checks_both_rosters_at_once(db_session):
    firm = _firm(db_session, "Both Rosters Co", firm_type="mep")
    account = create_account(db_session, name="Both Rosters Co", account_type="engineer")
    result = resolve_named_entity(db_session, "Both Rosters Co")
    assert result["firm"].id == firm.id
    assert result["account"].id == account.id


def test_resolve_named_entity_no_guess_on_no_match(db_session):
    assert resolve_named_entity(db_session, "Totally Unknown Firm LLC") == {
        "firm": None, "account": None}


def test_create_field_intel_resolves_engineer_and_mech_contractor_independently(db_session):
    eng_firm = _firm(db_session, "Critchfield Mechanical Engineering", firm_type="mep")
    mech_account = create_account(db_session, name="Pacific Coast Mechanical",
                                  account_type="mechanical_contractor")

    intel = create_field_intel(
        db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
        source_notes="Named the engineer and the mech sub.",
        engineer_name="Critchfield Mechanical Engineering Inc.",
        mech_contractor_name="Pacific Coast Mechanical Inc.",
    )
    assert intel.engineer_firm_id == eng_firm.id
    assert intel.engineer_account_id is None
    assert intel.mech_contractor_account_id == mech_account.id
    assert intel.mech_contractor_firm_id is None
    # The stated names survive verbatim regardless of what resolved.
    assert intel.engineer_name == "Critchfield Mechanical Engineering Inc."
    assert intel.mech_contractor_name == "Pacific Coast Mechanical Inc."


def test_create_field_intel_unresolved_names_still_save_with_null_joins(db_session):
    intel = create_field_intel(
        db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
        source_notes="Named an engineer nobody's heard of.",
        engineer_name="Totally New Engineering Firm",
    )
    assert intel.engineer_name == "Totally New Engineering Firm"
    assert intel.engineer_firm_id is None
    assert intel.engineer_account_id is None


# ---- firm_active_projects ---------------------------------------------------

def test_firm_active_projects_lists_only_active_status(db_session):
    firm = _firm(db_session, "Multi Project Engineering", firm_type="mep")
    live = _project(db_session, "Live DC", status="active")
    dead = _project(db_session, "Dead DC", status="dead")
    _link(db_session, live, firm, role="engineer_of_record")
    _link(db_session, dead, firm, role="engineer_of_record")

    rows = firm_active_projects(db_session, firm.id)
    assert [r["project"].id for r in rows] == [live.id]
    assert rows[0]["role"] == "engineer_of_record"


def test_firm_active_projects_empty_for_a_firm_on_nothing(db_session):
    firm = _firm(db_session, "Untouched Firm")
    assert firm_active_projects(db_session, firm.id) == []


# ---- confirmation_candidates: surfaced, never auto-linked ------------------

def test_confirmation_candidates_by_owner_name(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...", owner="Vantage Data Centers")
    match = _project(db_session, "Some DC", developer="Vantage Data Centers, LLC", status="active")
    no_match = _project(db_session, "Other DC", developer="Someone Else", status="active")

    candidates = confirmation_candidates(db_session, intel)
    ids = {p.id for p in candidates}
    assert match.id in ids
    assert no_match.id not in ids


def test_confirmation_candidates_by_shared_resolved_firm(db_session):
    eng_firm = _firm(db_session, "Shared Engineer Co", firm_type="mep")
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...", engineer_name="Shared Engineer Co")
    project = _project(db_session, "Some DC", status="active")
    _link(db_session, project, eng_firm, role="engineer_of_record")

    candidates = confirmation_candidates(db_session, intel)
    assert [p.id for p in candidates] == [project.id]


def test_confirmation_candidates_empty_when_nothing_matches(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...", owner="Nobody Matches This Owner")
    _project(db_session, "Some DC", developer="Someone Else", status="active")
    assert confirmation_candidates(db_session, intel) == []


def test_confirmation_candidates_empty_once_already_confirmed(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...", owner="Vantage Data Centers")
    project = _project(db_session, "Some DC", developer="Vantage Data Centers", status="active")
    confirm_field_intel(db_session, intel.id, project.id, confirmed_by="Andrew")

    assert confirmation_candidates(db_session, intel) == []


# ---- confirm_field_intel: explicit human action only -----------------------

def test_confirm_field_intel_sets_pointer_status_and_who(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...")
    project = _project(db_session, "Now Public DC", status="active")

    confirmed = confirm_field_intel(db_session, intel.id, project.id, confirmed_by="Andrew Crane")
    assert confirmed.confirmed_project_id == project.id
    assert confirmed.confirmed_by == "Andrew Crane"
    assert confirmed.confirmed_at is not None
    assert confirmed.status == "confirmed"

    db_session.refresh(project)
    assert project.status == "active"  # the public project is untouched, not merged/altered


def test_confirm_field_intel_unknown_intel_raises(db_session):
    project = _project(db_session, "Some DC")
    with pytest.raises(ValueError, match="no field intel"):
        confirm_field_intel(db_session, 999999, project.id, confirmed_by="Andrew")


def test_confirm_field_intel_unknown_project_raises(db_session):
    intel = create_field_intel(db_session, reported_by="Dave Kim", reported_at=datetime(2026, 8, 25),
                               source_notes="...")
    with pytest.raises(ValueError, match="no project"):
        confirm_field_intel(db_session, intel.id, 999999, confirmed_by="Andrew")


# ---- active_field_intel -----------------------------------------------------

def test_active_field_intel_excludes_confirmed_orders_by_reported_at_desc(db_session):
    from app.field_intel import active_field_intel

    old = create_field_intel(db_session, reported_by="A", reported_at=datetime(2026, 8, 1),
                             source_notes="older")
    new = create_field_intel(db_session, reported_by="B", reported_at=datetime(2026, 8, 20),
                             source_notes="newer")
    confirmed = create_field_intel(db_session, reported_by="C", reported_at=datetime(2026, 8, 25),
                                   source_notes="will be confirmed")
    project = _project(db_session, "Public Now")
    confirm_field_intel(db_session, confirmed.id, project.id, confirmed_by="Andrew")

    rows = active_field_intel(db_session)
    assert [r.id for r in rows] == [new.id, old.id]


# ---- field_intel_activity: a plain counter, not a health check -------------

def test_field_intel_activity_on_empty_table(db_session):
    activity = field_intel_activity(db_session)
    assert activity == {"total": 0, "most_recent": None, "last_30_days": 0}


def test_field_intel_activity_counts_total_and_recency(db_session):
    create_field_intel(db_session, reported_by="A", reported_at=utcnow() - timedelta(days=45),
                       source_notes="old one, outside the 30-day window")
    create_field_intel(db_session, reported_by="B", reported_at=utcnow() - timedelta(days=5),
                       source_notes="recent one, inside the window")
    most_recent_time = utcnow() - timedelta(days=1)
    create_field_intel(db_session, reported_by="C", reported_at=most_recent_time,
                       source_notes="the newest one")

    activity = field_intel_activity(db_session)
    assert activity["total"] == 3
    assert activity["last_30_days"] == 2
    assert abs((activity["most_recent"] - most_recent_time).total_seconds()) < 1


def test_field_intel_activity_ignores_confirmed_status(db_session):
    """Confirmed records are still field intel that was logged -- this is
    an activity count, not active_field_intel's board-listing filter."""
    intel = create_field_intel(db_session, reported_by="A", reported_at=utcnow() - timedelta(days=2),
                               source_notes="will be confirmed")
    project = _project(db_session, "Now Public DC")
    confirm_field_intel(db_session, intel.id, project.id, confirmed_by="Andrew")

    assert field_intel_activity(db_session)["total"] == 1
    assert field_intel_activity(db_session)["last_30_days"] == 1
