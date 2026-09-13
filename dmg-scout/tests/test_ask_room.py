"""Block 4C Item 4 (Master Plan v3.6 section 45): "Ask the room" --
routing (app.pipeline.ask_room) and the /room + Today web surfaces."""
import base64

import pytest
from sqlmodel import select

from app.config import load_config
from app.db import get_session
from app.models import (
    Account, Contact, Contractor, DecisionNote, LeadSource, NoteType, Opportunity, Outcome, PenState,
    Project, ProjectSignal, RoomQuestion, Signal, SignalType,
)
from app.pipeline.ask_room import answer_question, open_questions_for_user, post_question, users_for_question


def _cfg_with_manager(real_cfg, users):
    class _Cfg:
        def __getattr__(self, name):
            return getattr(real_cfg, name)

        def get(self, key, default=None):
            if key == "dashboard.users":
                return users
            if key == "dashboard.operator_usernames":
                return []
            return real_cfg.get(key, default)
    return _Cfg()


def _signal(db_session):
    s = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(s)
    db_session.flush()
    return s


class TestUsersForQuestionAccount:
    def test_routes_to_the_opportunity_owner(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        signal = _signal(db_session)
        opp = Opportunity(account_id=account.id, signal_id=signal.id, pen_state=PenState.not_moved,
                          owner_user="jason")
        db_session.add(opp)
        db_session.commit()

        users = users_for_question(db_session, load_config(), "account", account.id)
        assert "jason" in users

    def test_routes_to_an_outcome_author_on_that_account(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        signal = _signal(db_session)
        opp = Opportunity(account_id=account.id, signal_id=signal.id, pen_state=PenState.not_moved,
                          owner_user="jason")
        db_session.add(opp)
        db_session.flush()
        db_session.add(Outcome(opportunity_id=opp.id, user="andy", disposition="connected"))
        db_session.commit()

        users = users_for_question(db_session, load_config(), "account", account.id)
        assert "andy" in users

    def test_routes_to_a_note_author_on_that_account(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        db_session.add(DecisionNote(account_id=account.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.relationship, author="chris"))
        db_session.commit()

        users = users_for_question(db_session, load_config(), "account", account.id)
        assert "chris" in users

    def test_does_not_route_to_an_unrelated_accounts_people(self, db_session):
        a1 = Account(name="Acme", name_norm="acme")
        a2 = Account(name="Other Co", name_norm="other co")
        db_session.add(a1)
        db_session.add(a2)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(Opportunity(account_id=a2.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="stranger"))
        db_session.commit()

        users = users_for_question(db_session, load_config(), "account", a1.id)
        assert "stranger" not in users


class TestUsersForQuestionContractor:
    def test_routes_via_the_opportunitys_contact_contractor_link(self, db_session):
        contractor = Contractor(license_no="X1", business_name="Cool Air Inc")
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Someone", contractor_id=contractor.id)
        db_session.add(contact)
        db_session.flush()
        signal = _signal(db_session)
        opp = Opportunity(contact_id=contact.id, signal_id=signal.id, pen_state=PenState.not_moved,
                          owner_user="larry")
        db_session.add(opp)
        db_session.commit()

        users = users_for_question(db_session, load_config(), "contractor", contractor.id)
        assert "larry" in users

    def test_routes_to_a_note_anchored_directly_on_the_contractor(self, db_session):
        contractor = Contractor(license_no="X2", business_name="Cool Air Inc")
        db_session.add(contractor)
        db_session.commit()
        db_session.add(DecisionNote(contractor_id=contractor.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.relationship, author="andy"))
        db_session.commit()

        users = users_for_question(db_session, load_config(), "contractor", contractor.id)
        assert "andy" in users


class TestUsersForQuestionProject:
    def test_routes_via_the_opportunitys_project_signal_link(self, db_session):
        project = Project(name="Big Job")
        db_session.add(project)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(ProjectSignal(project_id=project.id, signal_id=signal.id))
        db_session.flush()
        opp = Opportunity(signal_id=signal.id, pen_state=PenState.not_moved, owner_user="jason")
        db_session.add(opp)
        db_session.commit()

        users = users_for_question(db_session, load_config(), "project", project.id)
        assert "jason" in users


class TestUsersForQuestionBuilding:
    def test_routes_via_the_opportunitys_building_id(self, db_session):
        from app.models import RetrofitBuilding

        building = RetrofitBuilding(apn="B1", population="replacement_candidate")
        db_session.add(building)
        db_session.flush()
        signal = _signal(db_session)
        opp = Opportunity(building_id=building.id, signal_id=signal.id, pen_state=PenState.not_moved,
                          owner_user="chris")
        db_session.add(opp)
        db_session.commit()

        users = users_for_question(db_session, load_config(), "building", building.id)
        assert "chris" in users


class TestAlwaysNotifyRoles:
    def test_managers_see_every_question_regardless_of_anchor(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        real_cfg = load_config()
        cfg = _cfg_with_manager(real_cfg, [
            {"username": "mgr1", "password_env": "X", "role": "manager"},
            {"username": "rep1", "password_env": "X", "role": "rep"},
        ])

        users = users_for_question(db_session, cfg, "account", account.id)
        assert "mgr1" in users
        assert "rep1" not in users

    def test_config_driven_role_list_is_respected(self, db_session):
        """Item 4's own words: "Config-driven routing" -- changing
        always_notify_roles must change who's always included, with no
        code change."""
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        real_cfg = load_config()

        class _Cfg:
            def __getattr__(self, name):
                return getattr(real_cfg, name)

            def get(self, key, default=None):
                if key == "dashboard.users":
                    return [{"username": "exec1", "password_env": "X", "role": "executive"}]
                if key == "ask_the_room.always_notify_roles":
                    return ["executive"]
                if key == "dashboard.operator_usernames":
                    return []
                return real_cfg.get(key, default)

        users = users_for_question(db_session, _Cfg(), "account", account.id)
        assert "exec1" in users


class TestInvalidAnchorType:
    def test_raises_on_an_unknown_anchor_type(self, db_session):
        with pytest.raises(ValueError):
            users_for_question(db_session, load_config(), "signal", 1)


class TestPostAndAnswerQuestion:
    def test_post_question_requires_real_text(self, db_session):
        with pytest.raises(ValueError):
            post_question(db_session, anchor_type="account", anchor_id=1, text="   ", author="andy")

    def test_post_question_rejects_an_unknown_anchor_type(self, db_session):
        with pytest.raises(ValueError):
            post_question(db_session, anchor_type="signal", anchor_id=1, text="who knows this?", author="andy")

    def test_answer_becomes_a_decision_note_under_the_answerers_name(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        q = post_question(db_session, anchor_type="account", anchor_id=account.id,
                          text="Who knows the FM here?", author="andy")
        db_session.commit()

        note = answer_question(db_session, question_id=q.id, answer_text="Talk to Bob in facilities.",
                               answerer="chris")
        db_session.commit()

        assert note.note_type == NoteType.intel
        assert note.author == "chris"
        assert note.account_id == account.id
        assert "Talk to Bob in facilities." in note.free_text
        assert "Who knows the FM here?" in note.free_text

        db_session.refresh(q)
        assert q.answered_note_id == note.id

    def test_answer_anchors_a_contractor_question_on_the_contractor(self, db_session):
        contractor = Contractor(license_no="X3", business_name="Cool Air Inc")
        db_session.add(contractor)
        db_session.commit()
        q = post_question(db_session, anchor_type="contractor", anchor_id=contractor.id,
                          text="Do we have a rep contact here?", author="andy")
        db_session.commit()

        note = answer_question(db_session, question_id=q.id, answer_text="Yes, Jeff.", answerer="chris")
        assert note.contractor_id == contractor.id
        assert note.account_id is None

    def test_cannot_answer_an_already_answered_question(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        q = post_question(db_session, anchor_type="account", anchor_id=account.id, text="?", author="andy")
        db_session.commit()
        answer_question(db_session, question_id=q.id, answer_text="first", answerer="chris")
        db_session.commit()

        with pytest.raises(ValueError):
            answer_question(db_session, question_id=q.id, answer_text="second", answerer="jason")

    def test_answer_requires_real_text(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        q = post_question(db_session, anchor_type="account", anchor_id=account.id, text="?", author="andy")
        db_session.commit()
        with pytest.raises(ValueError):
            answer_question(db_session, question_id=q.id, answer_text="  ", answerer="chris")

    def test_unknown_question_id_raises(self, db_session):
        with pytest.raises(ValueError):
            answer_question(db_session, question_id=999999, answer_text="x", answerer="chris")


class TestOpenQuestionsForUser:
    def test_a_user_sees_their_own_open_question(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        post_question(db_session, anchor_type="account", anchor_id=account.id, text="?", author="andy")
        db_session.commit()

        open_qs = open_questions_for_user(db_session, load_config(), "andy")
        assert len(open_qs) == 1

    def test_answered_questions_are_excluded(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        q = post_question(db_session, anchor_type="account", anchor_id=account.id, text="?", author="andy")
        db_session.commit()
        answer_question(db_session, question_id=q.id, answer_text="a", answerer="chris")
        db_session.commit()

        assert open_questions_for_user(db_session, load_config(), "andy") == []

    def test_an_unrelated_user_does_not_see_it(self, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        post_question(db_session, anchor_type="account", anchor_id=account.id, text="?", author="andy")
        db_session.commit()

        assert open_questions_for_user(db_session, load_config(), "stranger") == []


class TestRoomWebRoutes:
    @pytest.fixture()
    def client(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_post_a_question_then_see_it_on_room_page(self, client, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}

        resp = client.post("/room", headers=auth, data={
            "anchor_type": "account", "anchor_id": str(account.id), "text": "Who knows this account?",
        }, follow_redirects=False)
        assert resp.status_code == 303

        page = client.get("/room", headers=auth)
        assert "Who knows this account?" in page.text

    def test_answering_creates_a_visible_decision_note(self, client, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        client.post("/room", headers=auth, data={
            "anchor_type": "account", "anchor_id": str(account.id), "text": "Who knows this account?",
        })
        q = db_session.exec(select(RoomQuestion)).first()

        resp = client.post(f"/room/{q.id}/answer", headers=auth,
                           data={"answer_text": "Talk to Bob."}, follow_redirects=False)
        assert resp.status_code == 303

        note = db_session.exec(select(DecisionNote)).first()
        assert note.author == "andrew"
        assert "Talk to Bob." in note.free_text

    def test_room_requires_auth(self, client):
        assert client.get("/room").status_code == 401

    def test_bad_anchor_type_is_a_400_not_a_500(self, client):
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.post("/room", headers=auth, data={
            "anchor_type": "signal", "anchor_id": "1", "text": "?",
        })
        assert resp.status_code == 400


class TestTodayShowsRoomQuestions:
    @pytest.fixture()
    def client(self, db_session, monkeypatch):
        from fastapi.testclient import TestClient

        from app.web.main import app
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_a_question_routed_to_this_user_appears_on_today(self, client, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.flush()
        signal = _signal(db_session)
        db_session.add(Opportunity(account_id=account.id, signal_id=signal.id, pen_state=PenState.not_moved,
                                   owner_user="andrew"))
        db_session.commit()
        post_question(db_session, anchor_type="account", anchor_id=account.id,
                      text="Does anyone know the FM here?", author="jason")
        db_session.commit()

        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/", headers=auth)
        assert "Does anyone know the FM here?" in resp.text

    def test_an_unrelated_question_does_not_appear(self, client, db_session):
        account = Account(name="Acme", name_norm="acme")
        db_session.add(account)
        db_session.commit()
        post_question(db_session, anchor_type="account", anchor_id=account.id,
                      text="Totally unrelated to andrew", author="jason")
        db_session.commit()

        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/", headers=auth)
        assert "Totally unrelated to andrew" not in resp.text
