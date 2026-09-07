"""CALL TARGET: R1-R6, in priority order, plus WHO resolution for each
type. See app/call_target.py's module docstring for the full rule table.
"""
from sqlmodel import select

from app.call_target import (
    CallTarget,
    ab869_call_target,
    determine_call_target,
    engineer_of_record_by_project,
    gc_by_project,
    project_call_target,
)
from app.models import (
    Ab869Plan, Category, Firm, Project, ProjectFirm, Stage, Window,
)

STANDARDS_OWNERS = ["Kaiser", "Providence", "Irvine Company", "UC ", "LAUSD", "CommonSpirit",
                    "Scripps", "Sutter"]


# --- determine_call_target(): each rule in isolation -------------------------


def test_r1_owner_standards_fires_on_substring_case_insensitive_match():
    target, rule, reason = determine_call_target(
        developer_or_owner="kaiser foundation hospitals", delivery_method=None,
        is_existing_building_record=False, has_engineer_of_record=False,
        window=None, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.owner_standards
    assert rule == "R1"
    assert "Kaiser" in reason


def test_r2_db_contractor_fires_on_design_build_delivery_method():
    target, rule, _ = determine_call_target(
        developer_or_owner="Some Developer LLC", delivery_method="design_build",
        is_existing_building_record=False, has_engineer_of_record=False,
        window=None, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.db_contractor and rule == "R2"


def test_r2_does_not_fire_on_other_delivery_methods():
    for dm in ("design_bid_build", "design_assist", "cm_at_risk", "progressive_design_build"):
        target, rule, _ = determine_call_target(
            developer_or_owner=None, delivery_method=dm,
            is_existing_building_record=False, has_engineer_of_record=False,
            window=None, standards_owners=STANDARDS_OWNERS)
        assert target != CallTarget.db_contractor, f"{dm} incorrectly matched R2"


def test_r3_owner_fires_for_existing_building_with_no_engineer():
    target, rule, reason = determine_call_target(
        developer_or_owner="Some Hospital District", delivery_method=None,
        is_existing_building_record=True, has_engineer_of_record=False,
        window=None, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.owner and rule == "R3"
    assert "no engineer" in reason


def test_r3_does_not_fire_when_an_engineer_is_already_attached():
    target, rule, _ = determine_call_target(
        developer_or_owner=None, delivery_method=None,
        is_existing_building_record=True, has_engineer_of_record=True,
        window=None, standards_owners=STANDARDS_OWNERS)
    assert target != CallTarget.owner


def test_r4_engineer_fires_for_pre_bod_and_in_bod():
    for w in (Window.PRE_BOD, Window.IN_BOD):
        target, rule, reason = determine_call_target(
            developer_or_owner=None, delivery_method=None,
            is_existing_building_record=False, has_engineer_of_record=False,
            window=w, standards_owners=STANDARDS_OWNERS)
        assert target == CallTarget.engineer and rule == "R4"
        assert w.value in reason


def test_r5_bidding_contractors_fires_for_post_bod():
    target, rule, _ = determine_call_target(
        developer_or_owner=None, delivery_method=None,
        is_existing_building_record=False, has_engineer_of_record=False,
        window=Window.POST_BOD, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.bidding_contractors and rule == "R5"


def test_r6_unknown_names_the_missing_inputs():
    target, rule, reason = determine_call_target(
        developer_or_owner=None, delivery_method=None,
        is_existing_building_record=False, has_engineer_of_record=False,
        window=Window.OPERATING, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.unknown and rule == "R6"
    missing = reason.split("missing:")[1]
    assert "developer/owner name" in missing and "delivery_method" in missing
    # window WAS supplied (OPERATING, just not PRE/IN/POST_BOD) -- not "missing"
    assert "window (for R4/R5)" not in missing


def test_r6_names_window_missing_when_window_is_none():
    target, rule, reason = determine_call_target(
        developer_or_owner="Some Developer", delivery_method="cm_at_risk",
        is_existing_building_record=False, has_engineer_of_record=False,
        window=None, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.unknown and rule == "R6"
    assert "window" in reason


# --- priority order: every higher rule must beat every lower one ------------


def test_r1_outranks_r2_r3_r4_r5():
    target, rule, _ = determine_call_target(
        developer_or_owner="Providence St. Joseph", delivery_method="design_build",
        is_existing_building_record=True, has_engineer_of_record=False,
        window=Window.POST_BOD, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.owner_standards and rule == "R1"


def test_r2_outranks_r3_r4_r5():
    target, rule, _ = determine_call_target(
        developer_or_owner="Some Developer", delivery_method="design_build",
        is_existing_building_record=True, has_engineer_of_record=False,
        window=Window.POST_BOD, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.db_contractor and rule == "R2"


def test_r3_outranks_r4_r5():
    target, rule, _ = determine_call_target(
        developer_or_owner="Some Developer", delivery_method=None,
        is_existing_building_record=True, has_engineer_of_record=False,
        window=Window.POST_BOD, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.owner and rule == "R3"


def test_r4_outranks_r5():
    # window can only be one value at a time in reality, but the priority
    # chain itself must still put R4's check ahead of R5's in the function
    # body -- this locks that ordering in regardless of how the two
    # conditions happen to be mutually exclusive today.
    target, rule, _ = determine_call_target(
        developer_or_owner=None, delivery_method=None,
        is_existing_building_record=False, has_engineer_of_record=False,
        window=Window.PRE_BOD, standards_owners=STANDARDS_OWNERS)
    assert target == CallTarget.engineer and rule == "R4"


# --- project_call_target(): WHO resolution ----------------------------------


def test_project_who_owner_standards_shows_the_developer(cfg):
    p = Project(name="P", category=Category.data_center, developer="Kaiser Permanente",
               window=Window.PRE_BOD, stage=Stage.entitlement, status="active")
    r = project_call_target(cfg, p, engineer_of_record=None, gc=None, nearby_contractor=None)
    assert r.target == CallTarget.owner_standards
    assert r.who_label == "Kaiser Permanente"


def test_project_who_engineer_known(cfg):
    p = Project(name="P", category=Category.data_center, developer="Some Developer",
               window=Window.PRE_BOD, stage=Stage.entitlement, status="active")
    r = project_call_target(cfg, p, engineer_of_record="Jacobs Engineering", gc=None, nearby_contractor=None)
    assert r.target == CallTarget.engineer
    assert r.who_label == "Jacobs Engineering"


def test_project_who_engineer_unknown_says_so_explicitly(cfg):
    p = Project(name="P", category=Category.data_center, developer="Some Developer",
               window=Window.IN_BOD, stage=Stage.design, status="active")
    r = project_call_target(cfg, p, engineer_of_record=None, gc=None, nearby_contractor=None)
    assert r.target == CallTarget.engineer
    assert r.who_label == "engineer of record unknown"


def test_project_who_db_contractor_known_and_unknown(cfg):
    p = Project(name="P", category=Category.data_center, developer="Some Developer",
               delivery_method="design_build", window=Window.PRE_BOD, stage=Stage.entitlement,
               status="active")
    known = project_call_target(cfg, p, engineer_of_record=None, gc="Acme GC", nearby_contractor=None)
    assert known.who_label == "Acme GC"
    unknown = project_call_target(cfg, p, engineer_of_record=None, gc=None, nearby_contractor=None)
    assert unknown.who_label == "design-build contractor unknown"


def test_project_who_owner_uses_nearby_contractor_or_says_none_found(cfg):
    p = Project(name="P", category=Category.esco, developer="Some Public Agency",
               window=Window.OPERATING, stage=Stage.operating, status="active")
    found = project_call_target(cfg, p, engineer_of_record=None, gc=None,
                                nearby_contractor={"contractor_name": "ABC Mechanical",
                                                   "contractor_phone": "555-1234", "distance_miles": 2.3})
    assert found.target == CallTarget.owner
    assert found.who_label == "ABC Mechanical"
    assert "2.3mi" in found.who_detail

    none_found = project_call_target(cfg, p, engineer_of_record=None, gc=None, nearby_contractor=None)
    assert none_found.target == CallTarget.owner
    assert "no CSLB" in none_found.who_label


def test_project_who_unknown_has_no_who(cfg):
    # OPERATING now satisfies R3's is_existing_building_record on its own
    # (see test_r3_fires_for_operating_window_regardless_of_category below),
    # so the one remaining way to reach R6 on a real Project is OPERATING
    # WITH an engineer_of_record already attached -- R3 requires NO
    # engineer, so that combination still falls through to unknown.
    p = Project(name="P", category=Category.data_center, developer=None,
               window=Window.OPERATING, stage=Stage.operating, status="active")
    r = project_call_target(cfg, p, engineer_of_record="Some Firm", gc=None, nearby_contractor=None)
    assert r.target == CallTarget.unknown
    assert r.who_label is None


def test_r3_fires_for_operating_window_regardless_of_category(cfg):
    """2026-09-06: the pre-deploy distribution showed 37 real OPERATING
    projects (all data_center/industrial, none esco) falling to R6 unknown
    -- confirmed wrong and fixed: OPERATING is the same 'existing building,
    no BOD decision left to influence' fact esco already represents."""
    p = Project(name="P", category=Category.data_center, developer="Some Developer",
               window=Window.OPERATING, stage=Stage.operating, status="active")
    r = project_call_target(cfg, p, engineer_of_record=None, gc=None,
                            nearby_contractor={"contractor_name": "ABC Mechanical",
                                               "contractor_phone": "555", "distance_miles": 3.0})
    assert r.target == CallTarget.owner
    assert r.rule == "R3"
    assert r.who_label == "ABC Mechanical"


# --- batched lookups ---------------------------------------------------------


def test_engineer_of_record_by_project_batches_across_many_projects(db_session):
    p1 = Project(name="P1", category=Category.data_center, status="active")
    p2 = Project(name="P2", category=Category.data_center, status="active")
    db_session.add_all([p1, p2])
    db_session.commit()
    f = Firm(name="Jacobs Engineering", name_norm="jacobs engineering", firm_type="mep")
    db_session.add(f)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p1.id, firm_id=f.id, role="engineer_of_record"))
    db_session.commit()

    result = engineer_of_record_by_project(db_session, [p1.id, p2.id])
    assert result.get(p1.id) == "Jacobs Engineering"
    assert p2.id not in result


def test_gc_by_project_only_matches_gc_role(db_session):
    p = Project(name="P", category=Category.data_center, status="active")
    db_session.add(p)
    db_session.commit()
    f = Firm(name="Acme GC", name_norm="acme gc", firm_type="gc")
    db_session.add(f)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p.id, firm_id=f.id, role="gc"))
    db_session.commit()

    result = gc_by_project(db_session, [p.id])
    assert result.get(p.id) == "Acme GC"


# --- AB 869 facility ----------------------------------------------------------


def test_ab869_owner_standards_via_owner_name(cfg):
    plan = Ab869Plan(perm_id="10001", owner_name="Kaiser Foundation Hospitals",
                     source_pdf_path="x", source_pdf_hash="h")
    r = ab869_call_target(cfg, plan, "Some Kaiser Facility", [])
    assert r.target == CallTarget.owner_standards
    assert r.who_label == "Kaiser Foundation Hospitals"


def test_ab869_owner_uses_financially_responsible_party(cfg):
    plan = Ab869Plan(perm_id="10002", owner_name="Independent Hospital District",
                     financially_responsible_party="County of Riverside",
                     source_pdf_path="x", source_pdf_hash="h")
    r = ab869_call_target(cfg, plan, "Independent Hospital", [])
    assert r.target == CallTarget.owner
    assert r.who_label == "County of Riverside"


def test_ab869_owner_falls_back_to_nearby_contractor_when_no_frp(cfg):
    from types import SimpleNamespace
    plan = Ab869Plan(perm_id="10003", owner_name="Independent Hospital District",
                     financially_responsible_party=None, source_pdf_path="x", source_pdf_hash="h")
    contractor = SimpleNamespace(business_name="ABC Mechanical")
    r = ab869_call_target(cfg, plan, "Independent Hospital",
                          [{"contractor": contractor, "distance_miles": 4.1}])
    assert r.target == CallTarget.owner
    assert r.who_label == "ABC Mechanical"


def test_ab869_no_plan_falls_back_to_facility_name_for_r1(cfg):
    r = ab869_call_target(cfg, None, "Kaiser Foundation Hospital - Fontana", [])
    assert r.target == CallTarget.owner_standards


def test_ab869_no_plan_no_frp_no_contractor_says_both_unknown(cfg):
    r = ab869_call_target(cfg, None, "Some Independent Hospital", [])
    assert r.target == CallTarget.owner
    assert "unknown" in r.who_label


# --- opsc_call_target: OPSC Schools-tab rows, not Projects ------------------


def test_opsc_call_target_standards_district_wins_regardless_of_status(cfg):
    from app.call_target import opsc_call_target
    r = opsc_call_target(cfg, "Los Angeles Unified", "Funds Released")
    assert r.target == CallTarget.owner_standards
    assert r.rule == "R1"


def test_opsc_call_target_funds_released_is_bidding_contractors(cfg):
    from app.call_target import opsc_call_target
    r = opsc_call_target(cfg, "Some Non-Standards District", "Funds Released")
    assert r.target == CallTarget.bidding_contractors
    assert r.rule == "R5"


def test_opsc_call_target_earlier_status_is_engineer(cfg):
    from app.call_target import opsc_call_target
    for status in ("Closed", "100.00% Completed", None):
        r = opsc_call_target(cfg, "Some Non-Standards District", status)
        assert r.target == CallTarget.engineer
        assert r.rule == "R4"
