"""Block 3 Item 5 (Master Plan v3.2 section 13): the minimal Signals and
Pipeline pages -- filter chips, cards with a Promote button disabled and
naming its missing parts, and an Opportunity table sorted weakest-why
first with a Reason Block detail expansion. No dispositions (Block 4)."""
import base64
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.manual import add_manual_signal
from app.models import (
    Category, Contact, Opportunity, OpportunityStage, ProductLine, Project, ProjectContact,
    ReasonBlock, ReasonStrength, RetrofitBuilding, Signal, SignalType, TriggerType, WhyKind,
)
from app.pipeline.reason_block import weakest_of
from app.pipeline.resolve import run_resolve
from app.pipeline.signals_feed import FeedSignal, resolve_signal_id
from app.pipeline.size_score import run_size_score
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed_project_signal(db_session, cfg):
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="design")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    return db_session.exec(select(Project)).first()


class TestWeakestOf:
    def test_returns_the_worst_of_three_strengths(self):
        assert weakest_of([ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Weak]) == ReasonStrength.Weak
        assert weakest_of([ReasonStrength.Strong, ReasonStrength.ABSTAIN, ReasonStrength.Strong]) == ReasonStrength.ABSTAIN
        assert weakest_of([ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong]) == ReasonStrength.Strong


class TestResolveSignalId:
    def test_project_sourced_signal_resolves_to_its_linked_signal(self, db_session, cfg):
        project = seed_project_signal(db_session, cfg)
        fs = FeedSignal(source="project", source_id=str(project.id), trigger_type=TriggerType.entitlement_milestone,
                        trigger_date=None, evidence="x", confidence=None, project_id=project.id)
        signal_id = resolve_signal_id(db_session, fs)
        assert signal_id is not None
        assert db_session.exec(select(Signal).where(Signal.id == signal_id)).first() is not None

    def test_non_project_source_has_no_resolvable_signal_today(self, db_session):
        """A real, disclosed schema gap (see app.pipeline.signals_feed.
        resolve_signal_id's own docstring), not a bug: only project-sourced
        signals have a real `signals` row behind them today."""
        b = RetrofitBuilding(apn="1-1-1", population="replacement_candidate")
        db_session.add(b)
        db_session.commit()
        fs = FeedSignal(source="retrofit_building", source_id=str(b.id), trigger_type=TriggerType.permit_gap,
                        trigger_date=None, evidence="x", confidence=None, building_id=b.id)
        assert resolve_signal_id(db_session, fs) is None


class TestSignalsPage:
    def test_page_loads_and_shows_filter_chips(self, client, db_session, cfg):
        seed_project_signal(db_session, cfg)
        r = client.get("/signals", headers=AUTH)
        assert r.status_code == 200
        assert 'href="/signals?trigger=entitlement_milestone"' in r.text

    def test_card_promote_button_is_disabled_and_names_missing_parts(self, client, db_session, cfg):
        seed_project_signal(db_session, cfg)
        r = client.get("/signals?trigger=entitlement_milestone", headers=AUTH)
        assert r.status_code == 200
        assert "disabled" in r.text
        assert "missing:" in r.text
        assert "named_reachable_contact" in r.text

    def test_filter_by_trigger_type_narrows_the_cards(self, client, db_session, cfg):
        seed_project_signal(db_session, cfg)
        r = client.get("/signals?trigger=deadline", headers=AUTH)
        assert r.status_code == 200
        assert "entitlement milestone --" not in r.text


class TestSignalsPromote:
    def test_promote_fails_closed_when_the_filter_still_fails(self, client, db_session, cfg):
        project = seed_project_signal(db_session, cfg)
        r = client.post("/signals/promote", data={"source": "project", "source_id": str(project.id)}, headers=AUTH)
        assert r.status_code == 409

    def test_promote_unknown_source_is_a_400(self, client, db_session, cfg):
        r = client.post("/signals/promote", data={"source": "nonsense", "source_id": "1"}, headers=AUTH)
        assert r.status_code == 400

    def test_promote_missing_signal_returns_404(self, client, db_session, cfg):
        r = client.post("/signals/promote", data={"source": "project", "source_id": "999999"}, headers=AUTH)
        assert r.status_code == 404

    def test_promote_succeeds_end_to_end_when_every_part_is_manufactured_to_pass(
            self, client, db_session, cfg, monkeypatch):
        """The realistic path (a card built from unified_signals()) can
        never pass today -- see resolve_signal_id/four_part_filter's own
        findings (docs/BUILD-PLAN.md section 9's Item 5 report: Promote is
        structurally unreachable given current data, not just empirically
        at 0). This test proves the WIRING (dispatch -> re-check ->
        promote -> redirect) works correctly for the day a real signal
        can pass, by manufacturing one -- project-sourced (a real
        resolvable signal_id) with account_id set by hand."""
        project = seed_project_signal(db_session, cfg)
        contact = Contact(name="Jane PE", phone="555-1234", reach_status="confirmed")
        db_session.add(contact)
        db_session.flush()
        db_session.add(ProjectContact(project_id=project.id, contact_id=contact.id, role="engineer_of_record"))
        db_session.add(ProductLine(name="Test AHU", name_norm="test ahu",
                                   category="air_handling_units", building_role="air_handling"))
        db_session.commit()

        fake_fs = FeedSignal(
            source="project", source_id=str(project.id), trigger_type=TriggerType.entitlement_milestone,
            trigger_date=datetime(2026, 1, 1), evidence="manufactured for this test", confidence=None,
            project_id=project.id, account_id=999, category=Category.data_center,
        )
        import app.pipeline.signals_feed as sf
        monkeypatch.setattr(sf, "_project_signals", lambda session: [fake_fs])

        r = client.post("/signals/promote", data={"source": "project", "source_id": str(project.id)},
                        headers=AUTH, follow_redirects=False)
        assert r.status_code == 303
        opp = db_session.exec(select(Opportunity)).first()
        assert opp is not None
        assert opp.account_id == 999


class TestPipelinePage:
    def test_empty_pipeline_shows_the_empty_state(self, client, db_session, cfg):
        r = client.get("/pipeline", headers=AUTH)
        assert r.status_code == 200
        assert "No Opportunities yet" in r.text

    def test_pipeline_shows_an_opportunity_with_its_weakest_why_and_reason_block(self, client, db_session, cfg):
        signal = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(signal)
        db_session.flush()
        opp = Opportunity(signal_id=signal.id, account_id=1, stage=OpportunityStage.identified)
        db_session.add(opp)
        db_session.flush()
        for kind, strength in ((WhyKind.them, ReasonStrength.Strong), (WhyKind.now, ReasonStrength.Strong),
                               (WhyKind.win, ReasonStrength.ABSTAIN)):
            db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=kind, strength=strength,
                                       evidence=f"{kind.value} evidence"))
        db_session.commit()

        r = client.get("/pipeline", headers=AUTH)
        assert r.status_code == 200
        assert "ABSTAIN" in r.text  # the weakest why shown in its own column
        assert "not yet in NetSuite" in r.text
        assert "now evidence" in r.text  # inside the expanded Reason Block detail

    def test_pipeline_sorts_weakest_why_first(self, client, db_session, cfg):
        sig1 = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        sig2 = Signal(signal_type=SignalType.ceqa_nop, event_date=datetime(2026, 1, 1))
        db_session.add(sig1); db_session.add(sig2); db_session.flush()
        strong_opp = Opportunity(signal_id=sig1.id, account_id=1, next_action="call strong")
        abstain_opp = Opportunity(signal_id=sig2.id, account_id=2, next_action="call abstain")
        db_session.add(strong_opp); db_session.add(abstain_opp); db_session.flush()
        for kind in WhyKind:
            db_session.add(ReasonBlock(opportunity_id=strong_opp.id, why_kind=kind, strength=ReasonStrength.Strong, evidence="x"))
        for kind in WhyKind:
            db_session.add(ReasonBlock(opportunity_id=abstain_opp.id, why_kind=kind, strength=ReasonStrength.ABSTAIN, evidence="x"))
        db_session.commit()

        r = client.get("/pipeline", headers=AUTH)
        assert r.text.index("call strong") < r.text.index("call abstain")


class TestPipelineOutcomeLogging:
    """Block 4A Item 2: one-tap disposition logging from Pipeline."""

    def _opportunity(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        opp = Opportunity(signal_id=signal.id, account_id=1)
        db_session.add(opp)
        db_session.commit()
        return opp

    def test_pipeline_row_shows_disposition_buttons_and_lost_form(self, client, db_session, cfg):
        self._opportunity(db_session)
        r = client.get("/pipeline", headers=AUTH)
        assert 'action="/pipeline/1/outcome"' in r.text
        assert "Connected" in r.text
        assert "Confirm lost" in r.text

    def test_logging_connected_updates_last_touch_not_stage(self, client, db_session, cfg):
        opp = self._opportunity(db_session)
        r = client.post(f"/pipeline/{opp.id}/outcome", headers=AUTH,
                        data={"disposition": "connected"}, follow_redirects=False)
        assert r.status_code == 303
        from app.models import Outcome
        outcome = db_session.exec(select(Outcome).where(Outcome.opportunity_id == opp.id)).one()
        assert outcome.user == "andrew"
        db_session.refresh(opp)
        assert opp.last_touch is not None
        assert opp.stage.value == "identified"

    def test_logging_won_moves_stage_to_won(self, client, db_session, cfg):
        opp = self._opportunity(db_session)
        client.post(f"/pipeline/{opp.id}/outcome", headers=AUTH, data={"disposition": "won"})
        db_session.refresh(opp)
        assert opp.stage.value == "won"

    def test_logging_lost_without_reason_code_400s(self, client, db_session, cfg):
        opp = self._opportunity(db_session)
        r = client.post(f"/pipeline/{opp.id}/outcome", headers=AUTH, data={"disposition": "lost"})
        assert r.status_code == 400

    def test_logging_lost_with_reason_code_moves_stage_to_lost(self, client, db_session, cfg):
        opp = self._opportunity(db_session)
        r = client.post(f"/pipeline/{opp.id}/outcome", headers=AUTH,
                        data={"disposition": "lost", "reason_code": "timing_deferred"},
                        follow_redirects=False)
        assert r.status_code == 303
        db_session.refresh(opp)
        assert opp.stage.value == "lost"
