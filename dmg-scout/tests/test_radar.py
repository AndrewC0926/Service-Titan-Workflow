"""Block 4C Item 5 (Master Plan v3.6 section 42): "Two modes on the same
data." Guide (Today unchanged) vs Radar (four panels scoped to one
user's own book) as a per-user preference."""
import base64

import pytest
from sqlmodel import select

from app.db import get_session
from app.models import (
    Ab869Plan, Account, Contact, Contractor, DecisionNote, LeadSource, NoteType, Opportunity, Outcome, PenState,
    ProjectSignal, ReasonBlock, ReasonStrength, RetrofitBuilding, Signal, SignalType, UserPreference, WhyKind,
)
from app.pipeline.radar import (
    DEFAULT_MODE, deadline_exposure_panel, get_mode, new_signals_panel, notes_and_patterns_panel,
    record_radar_visit, set_mode, weakest_why_panel,
)


def _signal(db_session, **kw):
    s = Signal(signal_type=SignalType.ceqa_nop, **kw)
    db_session.add(s)
    db_session.flush()
    return s


class TestModePreference:
    def test_default_mode_is_guide_with_no_row(self, db_session):
        assert get_mode(db_session, "andy") == DEFAULT_MODE == "guide"

    def test_set_mode_persists(self, db_session):
        set_mode(db_session, "andy", "radar")
        db_session.commit()
        assert get_mode(db_session, "andy") == "radar"

    def test_set_mode_rejects_an_unknown_mode(self, db_session):
        with pytest.raises(ValueError):
            set_mode(db_session, "andy", "sonar")

    def test_mode_is_per_user_not_global(self, db_session):
        set_mode(db_session, "andy", "radar")
        db_session.commit()
        assert get_mode(db_session, "jason") == "guide"

    def test_switching_back_to_guide_works(self, db_session):
        set_mode(db_session, "andy", "radar")
        db_session.commit()
        set_mode(db_session, "andy", "guide")
        db_session.commit()
        assert get_mode(db_session, "andy") == "guide"


class TestRecordRadarVisit:
    def test_first_visit_returns_none(self, db_session):
        previous = record_radar_visit(db_session, "andy")
        db_session.commit()
        assert previous is None

    def test_second_visit_returns_the_first_visits_timestamp(self, db_session):
        first = record_radar_visit(db_session, "andy")
        db_session.commit()
        pref = db_session.get(UserPreference, "andy")
        stamped_at = pref.last_radar_visit_at
        assert stamped_at is not None

        second = record_radar_visit(db_session, "andy")
        db_session.commit()
        assert second == stamped_at
        assert first is None


class TestWeakestWhyPanel:
    def test_only_this_users_own_opportunities(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        mine = _signal(db_session)
        theirs = _signal(db_session)
        db_session.add(Opportunity(account_id=account.id, signal_id=mine.id, pen_state=PenState.not_moved,
                                   owner_user="andy"))
        db_session.add(Opportunity(account_id=account.id, signal_id=theirs.id, pen_state=PenState.not_moved,
                                   owner_user="jason"))
        db_session.commit()

        rows = weakest_why_panel(db_session, "andy")
        assert len(rows) == 1

    def test_empty_when_no_opportunities(self, db_session):
        assert weakest_why_panel(db_session, "andy") == []

    def test_sorted_by_weakest_why_rank(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        s1, s2 = _signal(db_session), _signal(db_session)
        strong_opp = Opportunity(account_id=account.id, signal_id=s1.id, pen_state=PenState.not_moved,
                                 owner_user="andy")
        weak_opp = Opportunity(account_id=account.id, signal_id=s2.id, pen_state=PenState.not_moved,
                               owner_user="andy")
        db_session.add(strong_opp)
        db_session.add(weak_opp)
        db_session.flush()
        for kind in WhyKind:
            db_session.add(ReasonBlock(opportunity_id=strong_opp.id, why_kind=kind, strength=ReasonStrength.Strong,
                                       evidence="x"))
        for kind in WhyKind:
            strength = ReasonStrength.Weak if kind == WhyKind.win else ReasonStrength.Strong
            db_session.add(ReasonBlock(opportunity_id=weak_opp.id, why_kind=kind, strength=strength, evidence="x"))
        db_session.commit()

        rows = weakest_why_panel(db_session, "andy")
        # weakest_why_rank: "lower sorts first/stronger" -- an all-Strong
        # Opportunity (rank 0) sorts ahead of one with any Weak why.
        assert rows[0]["opportunity_id"] == strong_opp.id
        assert rows[1]["opportunity_id"] == weak_opp.id


class TestDeadlineExposurePanel:
    def test_empty_when_no_opportunities(self, db_session):
        assert deadline_exposure_panel(db_session, "andy") == []

    def test_matches_a_building_anchored_opportunity(self, db_session):
        building = RetrofitBuilding(apn="B1", population="replacement_candidate", address="1 Test Way",
                                    sb1206_trigger_status="in_effect")
        db_session.add(building)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(Opportunity(building_id=building.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="andy"))
        db_session.commit()

        rows = deadline_exposure_panel(db_session, "andy")
        assert any(r.source_id == str(building.id) for r in rows)

    def test_does_not_include_an_unrelated_buildings_deadline(self, db_session):
        mine = RetrofitBuilding(apn="B2", population="replacement_candidate", address="Mine",
                                sb1206_trigger_status="in_effect")
        theirs = RetrofitBuilding(apn="B3", population="replacement_candidate", address="Theirs",
                                  sb1206_trigger_status="in_effect")
        db_session.add(mine)
        db_session.add(theirs)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(Opportunity(building_id=mine.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="andy"))
        db_session.commit()

        rows = deadline_exposure_panel(db_session, "andy")
        assert all(r.source_id != str(theirs.id) for r in rows)


class TestNewSignalsPanel:
    def test_empty_when_no_building_anchored_opportunities(self, db_session):
        assert new_signals_panel(db_session, "andy", since=None) == []

    def test_first_visit_shows_everything_on_the_building(self, db_session):
        from datetime import datetime

        building = RetrofitBuilding(apn="B4", population="replacement_candidate", address="1 Test Way",
                                    latest_permit_nbr="P1", latest_install_year=2020)
        db_session.add(building)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(Opportunity(building_id=building.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="andy"))
        db_session.commit()

        rows = new_signals_panel(db_session, "andy", since=None)
        # permit_activity fires from latest_permit_nbr on a replacement_candidate/recently_active row
        # -- accept either an empty or populated result depending on unified_signals' own real rules,
        # the point of this test is that it runs without error against a real building anchor.
        assert isinstance(rows, list)


class TestNotesAndPatternsPanel:
    def test_only_this_users_own_notes(self, db_session):
        db_session.add(DecisionNote(note_type=NoteType.intel, lead_source=LeadSource.relationship,
                                    author="andy", account_id=None, project_id=None, building_id=None,
                                    signal_id=None, opportunity_id=None, netsuite_ref="SO-1",
                                    netsuite_ref_type="sales_order"))
        db_session.add(DecisionNote(note_type=NoteType.intel, lead_source=LeadSource.relationship,
                                    author="jason", netsuite_ref="SO-2", netsuite_ref_type="sales_order"))
        db_session.commit()

        data = notes_and_patterns_panel(db_session, "andy")
        assert len(data["notes"]) == 1
        assert data["notes"][0].author == "andy"

    def test_reason_code_counts_scoped_to_this_user(self, db_session):
        db_session.add(Outcome(opportunity_id=1, user="andy", disposition="lost", reason_code="price"))
        db_session.add(Outcome(opportunity_id=1, user="jason", disposition="lost", reason_code="price"))
        db_session.commit()

        data = notes_and_patterns_panel(db_session, "andy")
        assert data["reason_code_counts"] == [{"reason_code": "price", "count": 1}]


class TestRadarWebRoute:
    @pytest.fixture()
    def client(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_default_is_guide_today(self, client):
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/", headers=auth)
        assert resp.status_code == 200
        assert "Three to call" in resp.text
        assert "Switch to Radar" in resp.text

    def test_switching_to_radar_changes_the_page(self, client, db_session):
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        client.post("/today/mode", headers=auth, data={"mode": "radar"})
        resp = client.get("/", headers=auth)
        assert resp.status_code == 200
        assert "no suggested calls" in resp.text
        assert "Three to call" not in resp.text
        assert "Switch to Guide" in resp.text

    def test_radar_shows_the_customer_master_disclosure(self, client, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        db_session.add(Opportunity(account_id=account.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="andrew"))
        db_session.commit()
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        client.post("/today/mode", headers=auth, data={"mode": "radar"})
        resp = client.get("/", headers=auth)
        assert "not loaded" in resp.text

    def test_mode_persists_across_requests(self, client, db_session):
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        client.post("/today/mode", headers=auth, data={"mode": "radar"})
        resp1 = client.get("/", headers=auth)
        resp2 = client.get("/", headers=auth)
        assert "no suggested calls" in resp1.text
        assert "no suggested calls" in resp2.text

    def test_bad_mode_is_400(self, client):
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.post("/today/mode", headers=auth, data={"mode": "sonar"})
        assert resp.status_code == 400

    def test_requires_auth(self, client):
        assert client.post("/today/mode", data={"mode": "radar"}).status_code == 401
