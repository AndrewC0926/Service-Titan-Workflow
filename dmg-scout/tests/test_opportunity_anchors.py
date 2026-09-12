"""Block 4A Item 1 (Master Plan v3.6): Opportunity anchors -- an
Opportunity may anchor on an Account, a Building, or a Deadline facility,
with a real Signal row created for any building/facility the moment it is
promoted. Closes the gap Block 3 found: Promote structurally unreachable
because projects had signal rows but no account anchor, and buildings/
deadlines had anchors but no signal rows."""
from datetime import datetime

from sqlmodel import select

from app.models import (
    Ab869Plan, Category, HcaiProject, HospitalBuilding, Opportunity, OpscProject, PenState,
    Project, RetrofitBuilding, Signal, SignalType, Stage, TriggerType,
)
from app.pipeline.signals_feed import (
    FeedSignal, can_promote_signal, ensure_signal_for_promotion, four_part_filter,
    resolve_signal_id, unified_signals,
)


class TestPenStateComputation:
    def test_project_stage_maps_to_pen_state(self, db_session):
        cases = [
            (Stage.concept, PenState.not_moved),
            (Stage.entitlement, PenState.not_moved),
            (Stage.design, PenState.not_moved),
            (Stage.permitting, PenState.moving),
            (Stage.procurement, PenState.moved),
            (Stage.construction, PenState.moved),
            (Stage.operating, PenState.ABSTAIN),
            (Stage.unknown, PenState.ABSTAIN),
        ]
        for stage, expected in cases:
            db_session.add(Project(name=f"P-{stage.value}", category=Category.data_center, stage=stage))
        db_session.commit()
        for stage, expected in cases:
            match = next(s for s in unified_signals(db_session) if s.source == "project"
                        and s.evidence.startswith(f"P-{stage.value} "))
            assert match.pen_state == expected, f"stage={stage} expected {expected}, got {match.pen_state}"

    def test_retrofit_replacement_candidate_is_always_not_moved(self, db_session):
        db_session.add(RetrofitBuilding(apn="1-1-1", population="replacement_candidate"))
        db_session.commit()
        signals = [s for s in unified_signals(db_session) if s.source == "retrofit_building"]
        assert len(signals) == 1
        assert signals[0].pen_state == PenState.not_moved

    def test_ab869_not_approved_is_not_moved(self, db_session):
        db_session.add(HospitalBuilding(
            perm_id="P1", building_nbr="B1", facility_name="Test Hospital", county="Los Angeles",
            npc_rating="2", npc_deadline_year=2030, snapshot_date=datetime(2026, 1, 1), source_url="https://x",
        ))
        db_session.add(Ab869Plan(perm_id="P1", plan_status="Not Approved",
                                 source_pdf_path="/x.pdf", source_pdf_hash="h1"))
        db_session.commit()
        signals = [s for s in unified_signals(db_session) if s.source == "ab869_plan"]
        assert signals[0].pen_state == PenState.not_moved
        assert signals[0].facility_perm_id == "P1"

    def test_ab869_approved_abstains_no_contractor_field_to_confirm_moved(self, db_session):
        db_session.add(HospitalBuilding(
            perm_id="P2", building_nbr="B1", facility_name="Test Hospital 2", county="Los Angeles",
            npc_rating="2", npc_deadline_year=2030, snapshot_date=datetime(2026, 1, 1), source_url="https://x",
        ))
        db_session.add(Ab869Plan(perm_id="P2", plan_status="Approved",
                                 source_pdf_path="/x.pdf", source_pdf_hash="h2"))
        db_session.commit()
        signals = [s for s in unified_signals(db_session) if s.source == "ab869_plan"]
        assert signals[0].pen_state == PenState.ABSTAIN

    def test_hcai_stage_maps_to_pen_state(self, db_session):
        db_session.add(HcaiProject(
            record_no="R1", facility_id="F1", facility_name="Test", county="Los Angeles",
            scope_text="mechanical", status_raw="Active - Plan Review", stage="plan_review",
            is_mechanical=True, report_date=datetime(2026, 1, 1),
        ))
        db_session.add(HcaiProject(
            record_no="R2", facility_id="F1", facility_name="Test", county="Los Angeles",
            scope_text="mechanical", status_raw="Pending Construction Start", stage="pending_start",
            is_mechanical=True, report_date=datetime(2026, 1, 1),
        ))
        db_session.add(HcaiProject(
            record_no="R3", facility_id="F1", facility_name="Test", county="Los Angeles",
            scope_text="mechanical", status_raw="Field Operations in Progress", stage="in_construction",
            is_mechanical=True, report_date=datetime(2026, 1, 1),
        ))
        db_session.commit()
        by_id = {s.source_id: s for s in unified_signals(db_session) if s.source == "hcai_project"}
        assert by_id["R1"].pen_state == PenState.not_moved
        assert by_id["R2"].pen_state == PenState.moving
        assert by_id["R3"].pen_state == PenState.moved

    def test_opsc_pre_spec_is_not_moved(self, db_session):
        db_session.add(OpscProject(application_number="A1", district="Test USD", program="Modernization",
                                   status="Eligibility Determination", in_territory=True, source_url="https://x"))
        db_session.commit()
        signals = [s for s in unified_signals(db_session) if s.source == "opsc_project"]
        assert signals[0].pen_state == PenState.not_moved


class TestSellableAccountOrBuildingGate:
    def test_facility_perm_id_alone_without_pen_state_abstains(self, db_session):
        fs = FeedSignal(source="ab869_plan", source_id="P1", trigger_type=TriggerType.deadline,
                        trigger_date=None, evidence="x", confidence=None, facility_perm_id="P1")
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" in result.missing


class TestCanPromoteSignal:
    def test_project_retrofit_and_ab869_have_a_path(self):
        for source in ("project", "retrofit_building", "ab869_plan"):
            fs = FeedSignal(source=source, source_id="1", trigger_type=TriggerType.deadline,
                            trigger_date=None, evidence="x", confidence=None)
            assert can_promote_signal(fs) is True

    def test_hcai_opsc_field_intel_have_no_path_yet(self):
        for source in ("hcai_project", "opsc_project", "field_intel"):
            fs = FeedSignal(source=source, source_id="1", trigger_type=TriggerType.public_work,
                            trigger_date=None, evidence="x", confidence=None)
            assert can_promote_signal(fs) is False


class TestEnsureSignalForPromotion:
    def test_creates_a_real_signal_row_for_retrofit_building(self, db_session):
        fs = FeedSignal(source="retrofit_building", source_id="1", trigger_type=TriggerType.permit_gap,
                        trigger_date=None, evidence="1 Test Way: no permit on record", confidence=None,
                        building_id=1, pen_state=PenState.not_moved)
        signal_id = ensure_signal_for_promotion(db_session, fs)
        assert signal_id is not None
        row = db_session.exec(select(Signal).where(Signal.id == signal_id)).first()
        assert row.signal_type == SignalType.retrofit_permit_gap
        assert row.pen_state == PenState.not_moved
        assert row.summary_one_line == fs.evidence

    def test_creates_a_real_signal_row_for_ab869_plan(self, db_session):
        fs = FeedSignal(source="ab869_plan", source_id="P1", trigger_type=TriggerType.deadline,
                        trigger_date=datetime(2030, 1, 1), evidence="AB 869 plan status=Not Approved",
                        confidence=None, facility_perm_id="P1", pen_state=PenState.not_moved)
        signal_id = ensure_signal_for_promotion(db_session, fs)
        row = db_session.exec(select(Signal).where(Signal.id == signal_id)).first()
        assert row.signal_type == SignalType.ab869_npc_deadline
        assert row.event_date == datetime(2030, 1, 1)

    def test_reuses_an_existing_signal_for_project_without_creating_a_new_one(self, db_session):
        from app.manual import add_manual_signal
        from app.pipeline.resolve import run_resolve

        add_manual_signal(db_session, "prequal_invite", "test", project_name="Test DC",
                          developer="Test Dev", county="San Bernardino", state="CA", mw_it=10, stage="design")
        from app.config import load_config
        run_resolve(db_session, load_config(), use_llm=False)
        project = db_session.exec(select(Project)).first()
        before_count = len(db_session.exec(select(Signal)).all())

        fs = next(s for s in unified_signals(db_session) if s.source == "project" and s.project_id == project.id)
        signal_id = ensure_signal_for_promotion(db_session, fs)
        after_count = len(db_session.exec(select(Signal)).all())
        assert signal_id == resolve_signal_id(db_session, fs)
        assert after_count == before_count  # no new row created

    def test_returns_none_for_sources_with_no_path(self, db_session):
        fs = FeedSignal(source="hcai_project", source_id="1", trigger_type=TriggerType.public_work,
                        trigger_date=None, evidence="x", confidence=None)
        assert ensure_signal_for_promotion(db_session, fs) is None


class TestPromoteWithBuildingAnchorEndToEnd:
    def test_promotion_creates_opportunity_anchored_on_a_building_with_a_real_signal(self, db_session):
        """The actual fix: a building-anchored signal, once it also
        clears named_reachable_contact and eligible_fitting_line (still
        structurally rare, per docs/BUILD-PLAN.md's Item 1 report -- built
        by hand here to prove the wiring), promotes to a real Opportunity
        with a real, newly-created Signal row, not a structurally-blocked
        dead end."""
        from app.models import Contact, ProductLine, ProjectContact, ReasonBlock
        from app.pipeline.signals_feed import promote_to_opportunity

        b = RetrofitBuilding(apn="9-9-9", address="9 Test Way", population="replacement_candidate")
        db_session.add(b)
        db_session.add(ProductLine(name="Test AHU", name_norm="test ahu",
                                   category="air_handling_units", building_role="air_handling"))
        db_session.commit()

        fs = FeedSignal(source="retrofit_building", source_id=str(b.id), trigger_type=TriggerType.permit_gap,
                        trigger_date=datetime(2026, 1, 1), evidence="manufactured for this test", confidence=None,
                        building_id=b.id, pen_state=PenState.not_moved, category=Category.data_center)
        result = four_part_filter(db_session, fs)
        assert "sellable_account_or_building" not in result.missing

        signal_id = ensure_signal_for_promotion(db_session, fs)
        assert signal_id is not None
        opp = promote_to_opportunity(db_session, fs, signal_id=signal_id)
        db_session.commit()

        assert opp.building_id == b.id
        assert opp.pen_state == PenState.not_moved
        assert opp.signal_id == signal_id
        row = db_session.exec(select(Signal).where(Signal.id == signal_id)).first()
        assert row.signal_type == SignalType.retrofit_permit_gap
