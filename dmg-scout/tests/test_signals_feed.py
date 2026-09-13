"""Block 3 Item 2 (Master Plan v3.2 section 12): signal consolidation and
the four-part filter (app.pipeline.signals_feed)."""
from datetime import datetime

from sqlmodel import select

from app.models import (
    Ab869Plan, Category, Contact, Contractor, FieldIntel, HcaiProject, HospitalBuilding,
    Opportunity, OpscProject, PenState, ProductLine, Project, ProjectContact, ReasonBlock,
    ReasonStrength, RetrofitBuilding, Signal, SignalType, TriggerType, WhyKind,
)
from app.pipeline.signals_feed import (
    FeedSignal, four_part_filter, promote_to_opportunity, unified_signals,
)


def _project(name="Test DC", category=Category.data_center):
    return Project(name=name, category=category)


def test_unified_signals_folds_more_than_just_the_board(db_session):
    """The old path only ever looked at board Projects -- this must fail
    against that path by construction, since it asserts non-project
    sources are present in the unified feed."""
    db_session.add(RetrofitBuilding(
        apn="1234-005-006", address="1 Test Way", population="replacement_candidate",
        year_built=1990,
    ))
    db_session.commit()

    signals = unified_signals(db_session)
    sources = {s.source for s in signals}
    assert "retrofit_building" in sources
    assert any(s.trigger_type == TriggerType.permit_gap for s in signals)


def test_project_signal_is_entitlement_milestone(db_session):
    p = _project()
    db_session.add(p)
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "project"]
    assert len(signals) == 1
    assert signals[0].trigger_type == TriggerType.entitlement_milestone
    assert signals[0].category == Category.data_center


def test_retrofit_recently_active_buildings_are_a_permit_activity_signal(db_session):
    """Block 4B-prep-2 Item 3 (superseding the old assertion that
    recently_active buildings were excluded -- real behavior legitimately
    changed, not a regression): population=='recently_active' is the
    symmetric opposite of replacement_candidate's absence-is-the-signal
    permit_gap -- a real, dated, permit-verified install, folded in as its
    own trigger_type."""
    db_session.add(RetrofitBuilding(
        apn="9999-000-000", population="recently_active", equipment_type="packaged_rooftop",
        latest_install_year=2015, service_life_status="not_due",
    ))
    db_session.commit()

    signals = unified_signals(db_session)
    matches = [s for s in signals if s.source == "retrofit_building" and s.trigger_type == TriggerType.permit_activity]
    assert len(matches) == 1
    fs = matches[0]
    assert fs.trigger_date == datetime(2015, 1, 1)
    assert fs.pen_state == PenState.moved  # not_due -- a real permit's hold is still current


def test_retrofit_recently_active_pen_state_reopens_when_due_or_overdue(db_session):
    db_session.add(RetrofitBuilding(
        apn="9999-000-001", population="recently_active", latest_install_year=2000,
        service_life_status="overdue",
    ))
    db_session.commit()

    signals = unified_signals(db_session)
    fs = next(s for s in signals if s.source == "retrofit_building" and s.trigger_type == TriggerType.permit_activity)
    assert fs.pen_state == PenState.not_moved


def test_retrofit_recently_active_pen_state_abstains_on_unknown_service_life(db_session):
    db_session.add(RetrofitBuilding(
        apn="9999-000-002", population="recently_active", latest_install_year=2000,
        service_life_status=None,
    ))
    db_session.commit()

    signals = unified_signals(db_session)
    fs = next(s for s in signals if s.source == "retrofit_building" and s.trigger_type == TriggerType.permit_activity)
    assert fs.pen_state == PenState.ABSTAIN


def test_ab869_npc_outstanding_is_a_deadline_signal(db_session):
    db_session.add(HospitalBuilding(
        perm_id="PERM1", building_nbr="B1", facility_name="Test Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2030,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(
        perm_id="PERM1", plan_status="In Progress",
        source_pdf_path="/x.pdf", source_pdf_hash="abc",
    ))
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "ab869_plan"]
    assert len(signals) == 1
    assert signals[0].trigger_type == TriggerType.deadline
    assert signals[0].trigger_date == datetime(2030, 1, 1)


def test_ab869_facility_fully_compliant_is_not_a_signal(db_session):
    db_session.add(HospitalBuilding(
        perm_id="PERM2", building_nbr="B1", facility_name="Compliant Hospital",
        county="Los Angeles", npc_rating="5", npc_deadline_year=None,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(
        perm_id="PERM2", plan_status="Complete",
        source_pdf_path="/x.pdf", source_pdf_hash="abc2",
    ))
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "ab869_plan"]
    assert signals == []


def test_hcai_open_mechanical_project_is_public_work(db_session):
    db_session.add(HcaiProject(
        record_no="R1", facility_id="F1", facility_name="Test Facility",
        county="Los Angeles", scope_text="Replace chiller plant", status_raw="Under Review",
        stage="plan_review", is_mechanical=True, report_date=datetime(2026, 1, 1),
    ))
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "hcai_project"]
    assert len(signals) == 1
    assert signals[0].trigger_type == TriggerType.public_work


def test_hcai_closed_mechanical_project_is_excluded(db_session):
    db_session.add(HcaiProject(
        record_no="R2", facility_id="F1", facility_name="Test Facility",
        county="Los Angeles", scope_text="Replace chiller plant", status_raw="Complete",
        stage="closed", is_mechanical=True, report_date=datetime(2026, 1, 1),
    ))
    db_session.commit()

    assert not any(s.source == "hcai_project" for s in unified_signals(db_session))


def test_opsc_pre_spec_row_is_public_work(db_session):
    db_session.add(OpscProject(
        application_number="A1", district="Test USD", school_name="Test Elementary",
        program="Modernization", status="Eligibility Determination", in_territory=True,
        source_url="https://x",
    ))
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "opsc_project"]
    assert len(signals) == 1
    assert signals[0].trigger_type == TriggerType.public_work


def test_opsc_funds_released_row_is_excluded_spec_already_locked(db_session):
    db_session.add(OpscProject(
        application_number="A2", district="Test USD", school_name="Test Elementary",
        program="Modernization", status="Funds Released", in_territory=True,
        source_url="https://x",
    ))
    db_session.commit()

    assert not any(s.source == "opsc_project" for s in unified_signals(db_session))


def test_field_intel_is_relationship_intro(db_session):
    db_session.add(FieldIntel(
        reported_by="andrew", reported_at=datetime(2026, 6, 1), source_notes="Talked to the PE at lunch",
        stage="design",
    ))
    db_session.commit()

    signals = [s for s in unified_signals(db_session) if s.source == "field_intel"]
    assert len(signals) == 1
    assert signals[0].trigger_type == TriggerType.relationship_intro


class TestFourPartFilter:
    def test_missing_everything_reports_all_four(self, db_session):
        fs = FeedSignal(source="retrofit_building", source_id="1",
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None)
        result = four_part_filter(db_session, fs)
        assert not result.passed
        assert set(result.missing) == {
            "named_reachable_contact", "sellable_account_or_building",
            "dated_reason", "eligible_fitting_line",
        }

    def test_building_id_satisfies_sellable_account_or_building_when_pen_not_moved(self, db_session):
        """Block 4A Item 1: a building/facility anchor alone is no longer
        enough -- replacement-clock work also needs pen_state not_moved
        or moving (Master Plan v3.6 section 12b)."""
        b = RetrofitBuilding(apn="1-1-1", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id, pen_state=PenState.not_moved)
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" not in result.missing

    def test_building_id_with_abstain_pen_state_does_not_satisfy_the_part(self, db_session):
        """The new gate, not a regression of the old one: a building
        anchor with no basis to say whether the pen has moved must NOT
        pass -- ABSTAIN is not not_moved."""
        b = RetrofitBuilding(apn="1-1-2", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id, pen_state=PenState.ABSTAIN)
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" in result.missing

    def test_building_id_with_pen_moved_does_not_satisfy_the_part(self, db_session):
        """"the window closes when a contractor with an incumbent brand
        relationship is on site" (Master Plan v3.6 section 12b) -- a
        building whose pen has already moved is not sellable just because
        Scout knows where it is."""
        b = RetrofitBuilding(apn="1-1-3", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id, pen_state=PenState.moved)
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" in result.missing

    def test_facility_perm_id_with_pen_moving_satisfies_the_part(self, db_session):
        """The third anchor (Block 4A Item 1): a Deadline facility, same
        pen_state gate as a building."""
        fs = FeedSignal(source="ab869_plan", source_id="PERM1",
                        trigger_type=TriggerType.deadline, trigger_date=None,
                        evidence="x", confidence=None, facility_perm_id="PERM1", pen_state=PenState.moving)
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" not in result.missing

    def test_dated_reason_requires_a_trigger_date(self, db_session):
        fs_no_date = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                                trigger_date=None, evidence="x", confidence=None)
        fs_dated = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                              trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None)
        assert "dated_reason" in four_part_filter(db_session, fs_no_date).missing
        assert "dated_reason" not in four_part_filter(db_session, fs_dated).missing

    def test_all_four_parts_pass_when_every_condition_is_met(self, db_session):
        p = _project()
        db_session.add(p)
        db_session.flush()
        contact = Contact(name="Jane PE", phone="555-1234", reach_status="confirmed")
        db_session.add(contact)
        db_session.flush()
        db_session.add(ProjectContact(project_id=p.id, contact_id=contact.id, role="engineer_of_record"))
        db_session.add(ProductLine(name="Test AHU Line", name_norm="test ahu line",
                                   category="air_handling_units", building_role="air_handling"))
        db_session.commit()

        fs = FeedSignal(source="project", source_id=str(p.id), trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None,
                        project_id=p.id, account_id=None, building_id=None, category=Category.data_center)
        # sellable_account_or_building needs an account or building -- give it one directly
        fs.account_id = 999
        result = four_part_filter(db_session, fs)
        assert result.passed, result.missing
        assert result.contact_id == contact.id
        assert result.line_id is not None

    def test_contact_must_be_reach_status_confirmed(self, db_session):
        p = _project()
        db_session.add(p)
        db_session.flush()
        pending = Contact(name="Unreachable Guy", reach_status="pending")
        db_session.add(pending)
        db_session.flush()
        db_session.add(ProjectContact(project_id=p.id, contact_id=pending.id, role="gc"))
        db_session.commit()

        fs = FeedSignal(source="project", source_id=str(p.id), trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=None, evidence="x", confidence=None, project_id=p.id)
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" in result.missing

    def test_named_reachable_contact_via_nearby_contractor_yard(self, db_session):
        """Block 4B-prep-2 Item 2: a building-anchored signal with no
        Project link at all can still satisfy named_reachable_contact via
        a reachable Contact anchored on a Contractor whose yard is within
        15mi."""
        b = RetrofitBuilding(apn="2-2-1", population="replacement_candidate",
                             latitude=34.00, longitude=-118.00)
        contractor = Contractor(license_no="C1", business_name="Nearby Mechanical",
                                latitude=34.05, longitude=-118.05)  # ~4mi away
        db_session.add(b)
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Joe", phone="555-1111", reach_status="confirmed", contractor_id=contractor.id)
        db_session.add(contact)
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" not in result.missing
        assert result.contact_id == contact.id

    def test_named_reachable_contact_ignores_a_contractor_outside_the_radius(self, db_session):
        b = RetrofitBuilding(apn="2-2-2", population="replacement_candidate",
                             latitude=34.00, longitude=-118.00)
        contractor = Contractor(license_no="C2", business_name="Far Mechanical",
                                latitude=36.00, longitude=-118.00)  # ~138mi away
        db_session.add(b)
        db_session.add(contractor)
        db_session.flush()
        db_session.add(Contact(name="Jane", phone="555-2222", reach_status="confirmed",
                               contractor_id=contractor.id))
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" in result.missing

    def test_named_reachable_contact_ignores_a_contractor_contact_not_confirmed(self, db_session):
        b = RetrofitBuilding(apn="2-2-3", population="replacement_candidate",
                             latitude=34.00, longitude=-118.00)
        contractor = Contractor(license_no="C3", business_name="Nearby But Unreachable",
                                latitude=34.02, longitude=-118.02)
        db_session.add(b)
        db_session.add(contractor)
        db_session.flush()
        db_session.add(Contact(name="Pending Person", reach_status="pending", contractor_id=contractor.id))
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" in result.missing

    def test_named_reachable_contact_with_no_building_coordinates_abstains(self, db_session):
        """No geocode at all on the anchor -- never guessed at a distance
        that can't be computed."""
        b = RetrofitBuilding(apn="2-2-4", population="replacement_candidate")
        contractor = Contractor(license_no="C4", business_name="Anywhere Mechanical",
                                latitude=34.00, longitude=-118.00)
        db_session.add(b)
        db_session.add(contractor)
        db_session.flush()
        db_session.add(Contact(name="Joe", phone="555-3333", reach_status="confirmed",
                               contractor_id=contractor.id))
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id),
                        trigger_type=TriggerType.permit_gap, trigger_date=None,
                        evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" in result.missing

    def test_named_reachable_contact_via_nearby_contractor_yard_on_a_facility_anchor(self, db_session):
        """The third anchor (facility_perm_id, Block 4A Item 1) gets the
        same proximity path, via its HospitalBuilding's own coordinates."""
        db_session.add(HospitalBuilding(
            perm_id="PERM9", building_nbr="B1", facility_name="Test Hospital",
            county="Los Angeles", latitude=34.00, longitude=-118.00,
            snapshot_date=datetime(2026, 1, 1), source_url="https://x",
        ))
        contractor = Contractor(license_no="C5", business_name="Hospital-Adjacent Mechanical",
                                latitude=34.03, longitude=-118.03)
        db_session.add(contractor)
        db_session.flush()
        db_session.add(Contact(name="Ana", phone="555-4444", reach_status="confirmed",
                               contractor_id=contractor.id))
        db_session.commit()

        fs = FeedSignal(source="ab869_plan", source_id="PERM9", trigger_type=TriggerType.deadline,
                        trigger_date=None, evidence="x", confidence=None, facility_perm_id="PERM9")
        result = four_part_filter(db_session, fs)
        assert "named_reachable_contact" not in result.missing

    def test_no_category_means_eligible_fitting_line_abstains(self, db_session):
        fs = FeedSignal(source="opsc_project", source_id="A1", trigger_type=TriggerType.public_work,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None)
        result = four_part_filter(db_session, fs)
        assert "eligible_fitting_line" in result.missing

    def test_retrofit_building_with_a_known_equipment_type_uses_the_equipment_class_path(self, db_session):
        """Block 4B-prep Item 3: a retrofit_building signal has no
        Category (opsc/ab869/hcai/retrofit never do), so it falls through
        to the new equipment-class path rather than ABSTAINing outright."""
        b = RetrofitBuilding(apn="9-9-9", population="recently_active", equipment_type="boiler")
        db_session.add(b)
        db_session.add(ProductLine(name="Test Boiler Line", name_norm="test boiler line",
                                   category="heaters", building_role="heating_specialty"))
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id), trigger_type=TriggerType.permit_gap,
                        trigger_date=None, evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "eligible_fitting_line" not in result.missing

    def test_retrofit_building_with_no_equipment_type_still_abstains(self, db_session):
        """The replacement_candidate population's own permanently-null
        equipment_type -- the honest, expected zero-coverage case."""
        b = RetrofitBuilding(apn="8-8-8", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id), trigger_type=TriggerType.permit_gap,
                        trigger_date=None, evidence="x", confidence=None, building_id=b.id)
        result = four_part_filter(db_session, fs)
        assert "eligible_fitting_line" in result.missing

    def test_ab869_plan_always_abstains_no_equipment_class_field_exists(self, db_session):
        """HospitalBuilding/Ab869Plan carry no equipment-class-bearing
        field at all -- this must stay honest ABSTAIN, never a guess."""
        db_session.add(ProductLine(name="Test OSP Chiller", name_norm="test osp chiller",
                                   category="chillers_cooling", building_role="cooling_generation",
                                   oshpd_osp=True))
        db_session.commit()
        fs = FeedSignal(source="ab869_plan", source_id="P1", trigger_type=TriggerType.deadline,
                        trigger_date=None, evidence="x", confidence=None, facility_perm_id="P1")
        result = four_part_filter(db_session, fs)
        assert "eligible_fitting_line" in result.missing


class TestPromoteToOpportunity:
    def test_creates_opportunity_and_three_reason_blocks(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.commit()

        fs = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=datetime(2026, 1, 1), evidence="Board project fired", confidence=None,
                        account_id=42)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")

        assert opp.id is not None
        assert opp.signal_id == signal.id
        assert opp.account_id == 42

        blocks = db_session.exec(select(ReasonBlock).where(ReasonBlock.opportunity_id == opp.id)).all()
        assert len(blocks) == 3
        by_kind = {b.why_kind: b for b in blocks}
        assert set(by_kind) == {WhyKind.them, WhyKind.now, WhyKind.win}

    def test_win_is_always_abstain_in_block_3_public_data_only(self, db_session):
        """No DMG data exists yet to support a why-we-win claim -- Block 3's
        own scope (public data only) makes this an honest ABSTAIN, not a
        bug, on every single promotion."""
        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.commit()
        fs = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None, account_id=1)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")
        win_block = db_session.exec(
            select(ReasonBlock).where(ReasonBlock.opportunity_id == opp.id, ReasonBlock.why_kind == WhyKind.win)
        ).first()
        assert win_block.strength == ReasonStrength.ABSTAIN

    def test_them_is_strong_when_account_is_known(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.commit()
        fs = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None, account_id=1)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")
        them_block = db_session.exec(
            select(ReasonBlock).where(ReasonBlock.opportunity_id == opp.id, ReasonBlock.why_kind == WhyKind.them)
        ).first()
        assert them_block.strength == ReasonStrength.Strong

    def test_them_is_weak_when_only_a_building_is_known_owner_unknown(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        b = RetrofitBuilding(apn="2-2-2", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        fs = FeedSignal(source="retrofit_building", source_id=str(b.id), trigger_type=TriggerType.permit_gap,
                        trigger_date=None, evidence="x", confidence=None, building_id=b.id)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")
        them_block = db_session.exec(
            select(ReasonBlock).where(ReasonBlock.opportunity_id == opp.id, ReasonBlock.why_kind == WhyKind.them)
        ).first()
        assert them_block.strength == ReasonStrength.Weak

    def test_origin_is_scout_signal_for_a_non_relationship_trigger(self, db_session):
        from app.models import Origin

        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.commit()
        fs = FeedSignal(source="project", source_id="1", trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None, account_id=1)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")
        assert opp.origin == Origin.scout_signal

    def test_origin_is_relationship_intro_for_a_field_intel_trigger(self, db_session):
        from app.models import Origin

        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.commit()
        fs = FeedSignal(source="field_intel", source_id="1", trigger_type=TriggerType.relationship_intro,
                        trigger_date=datetime(2026, 1, 1), evidence="x", confidence=None, account_id=1)
        opp = promote_to_opportunity(db_session, fs, signal_id=signal.id, owner_user="andrew")
        assert opp.origin == Origin.relationship_intro
