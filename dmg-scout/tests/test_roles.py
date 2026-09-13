"""Block 4B Item 5 (Master Plan v3.6 section 35): roles from config,
per-role default landing, and manager-vs-rep Notes visibility."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.access_log import default_landing_for_role, user_role
from app.config import load_config
from app.db import get_session
from app.web.main import app


def _cfg_with_users(cfg, users, operators):
    """users: [(username, password_env, role_or_None), ...]. Same "thin
    wrapper around the REAL Config" shape as tests/test_nav_ia.py's own
    helper, extended with an optional role per user."""
    class _Cfg:
        def __getattr__(self, name):
            return getattr(cfg, name)

        def get(self, key, default=None):
            if key == "dashboard.users":
                entries = []
                for u, e, r in users:
                    entry = {"username": u, "password_env": e}
                    if r is not None:
                        entry["role"] = r
                    entries.append(entry)
                return entries
            if key == "dashboard.operator_usernames":
                return operators
            return cfg.get(key, default)
    return _Cfg()


class TestUserRole:
    def test_explicit_role_from_config(self):
        cfg = load_config()

        class _Cfg:
            def get(self, key, default=None):
                if key == "dashboard.users":
                    return [{"username": "larry", "password_env": "X", "role": "executive"}]
                if key == "dashboard.operator_usernames":
                    return ["andrew"]
                return default
        assert user_role(_Cfg(), "larry") == "executive"

    def test_invalid_role_value_falls_through_to_default(self):
        class _Cfg:
            def get(self, key, default=None):
                if key == "dashboard.users":
                    return [{"username": "larry", "password_env": "X", "role": "not_a_real_role"}]
                if key == "dashboard.operator_usernames":
                    return []
                return default
        assert user_role(_Cfg(), "larry") == "rep"

    def test_operator_usernames_membership_wins_when_no_explicit_role(self):
        class _Cfg:
            def get(self, key, default=None):
                if key == "dashboard.users":
                    return [{"username": "andrew", "password_env": "X"}]
                if key == "dashboard.operator_usernames":
                    return ["andrew"]
                return default
        assert user_role(_Cfg(), "andrew") == "operator"

    def test_unrecognized_username_defaults_to_rep(self):
        class _Cfg:
            def get(self, key, default=None):
                if key == "dashboard.users":
                    return []
                if key == "dashboard.operator_usernames":
                    return ["andrew"]
                return default
        assert user_role(_Cfg(), "whoever") == "rep"

    def test_none_username_defaults_to_rep(self):
        class _Cfg:
            def get(self, key, default=None):
                return [] if key == "dashboard.users" else default
        assert user_role(_Cfg(), None) == "rep"


class TestDefaultLandingForRole:
    @pytest.mark.parametrize("role,expected", [
        ("executive", "/reports"),
        ("rep", "/"),
        ("inside_sales", "/"),
        ("manager", "/"),
        ("operator", "/"),  # "operators SEE Settings" -- the existing operator_usernames
                            # capability, not a second landing-page redirect; see
                            # DEFAULT_LANDING_BY_ROLE's own comment for why.
    ])
    def test_landing(self, role, expected):
        assert default_landing_for_role(role) == expected


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


class TestPerRoleLandingRoute:
    def test_rep_stays_on_today_no_redirect(self, client, db_session, monkeypatch):
        """AUTH (andrew) is configured as operator in the real config.yaml
        -- a genuinely rep-shaped user needs its own mocked config, same
        as the executive/operator cases below."""
        cfg = load_config()
        monkeypatch.setenv("DASHBOARD_PASSWORD_REP", "reppw")
        monkeypatch.setattr(
            "app.web.main.load_config",
            lambda: _cfg_with_users(cfg, [("rep1", "DASHBOARD_PASSWORD_REP", "rep")], operators=[]),
        )
        rep_auth = {"Authorization": "Basic " + base64.b64encode(b"rep1:reppw").decode()}
        resp = client.get("/", headers=rep_auth, follow_redirects=False)
        assert resp.status_code == 200

    def test_executive_redirects_to_reports(self, client, db_session, monkeypatch):
        cfg = load_config()
        monkeypatch.setenv("DASHBOARD_PASSWORD_LARRY", "larrypw")
        monkeypatch.setattr(
            "app.web.main.load_config",
            lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD", "operator"),
                                          ("larry", "DASHBOARD_PASSWORD_LARRY", "executive")],
                                    operators=["andrew"]),
        )
        larry_auth = {"Authorization": "Basic " + base64.b64encode(b"larry:larrypw").decode()}
        resp = client.get("/", headers=larry_auth, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/reports"

    def test_operator_does_not_redirect_stays_on_today(self, client, db_session, monkeypatch):
        """"Operators SEE Settings" is the existing operator_usernames
        capability (Settings nav visible, /settings reachable) -- not a
        landing-page redirect. Andrew is Scout's one real operator today
        and still needs Today as his default view."""
        cfg = load_config()
        monkeypatch.setattr(
            "app.web.main.load_config",
            lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD", "operator")], operators=["andrew"]),
        )
        resp = client.get("/", headers=AUTH, follow_redirects=False)
        assert resp.status_code == 200


class TestNotesVisibility:
    def _note(self, db_session, author, **kw):
        from app.models import DecisionNote, LeadSource, NoteType, Opportunity, PenState, Signal, SignalType

        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        opp = Opportunity(account_id=1, signal_id=signal.id, pen_state=PenState.not_moved, owner_user="andrew")
        db_session.add(opp)
        db_session.flush()
        note = DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                            lead_source=LeadSource.scout_signal, author=author, free_text=f"note by {author}", **kw)
        db_session.add(note)
        db_session.commit()
        return note

    def test_rep_sees_only_their_own_notes(self, client, db_session, monkeypatch):
        cfg = load_config()
        monkeypatch.setenv("DASHBOARD_PASSWORD_REP", "reppw")
        monkeypatch.setattr(
            "app.web.main.load_config",
            lambda: _cfg_with_users(cfg, [("rep1", "DASHBOARD_PASSWORD_REP", "rep")], operators=[]),
        )
        self._note(db_session, "rep1")
        self._note(db_session, "someone_else")
        rep_auth = {"Authorization": "Basic " + base64.b64encode(b"rep1:reppw").decode()}
        resp = client.get("/notes", headers=rep_auth)
        assert resp.status_code == 200
        assert "note by rep1" in resp.text
        assert "note by someone_else" not in resp.text
        assert "Team patterns" not in resp.text

    def test_manager_sees_every_note_and_the_patterns_section(self, client, db_session, monkeypatch):
        cfg = load_config()
        monkeypatch.setenv("DASHBOARD_PASSWORD_MGR", "mgrpw")
        monkeypatch.setattr(
            "app.web.main.load_config",
            lambda: _cfg_with_users(cfg, [("mgr1", "DASHBOARD_PASSWORD_MGR", "manager")], operators=[]),
        )
        self._note(db_session, "rep1")
        self._note(db_session, "rep2")
        mgr_auth = {"Authorization": "Basic " + base64.b64encode(b"mgr1:mgrpw").decode()}
        resp = client.get("/notes", headers=mgr_auth)
        assert resp.status_code == 200
        assert "note by rep1" in resp.text
        assert "note by rep2" in resp.text
        assert "Team patterns" in resp.text

    def test_operator_also_sees_the_team_view(self, client, db_session):
        self._note(db_session, "someone")
        resp = client.get("/notes", headers=AUTH)  # andrew, configured operator
        assert resp.status_code == 200
        assert "note by someone" in resp.text
        assert "Team patterns" in resp.text


class TestAccessLogRole:
    def test_role_is_recorded_on_an_authenticated_hit(self, client, db_session):
        from sqlmodel import select

        from app.models import AccessLog

        client.get("/pipeline", headers=AUTH)
        row = db_session.exec(
            select(AccessLog).where(AccessLog.username == "andrew").order_by(AccessLog.id.desc())
        ).first()
        assert row is not None
        assert row.role == "operator"

    def test_role_is_null_for_an_unauthenticated_hit(self, client, db_session):
        from sqlmodel import select

        from app.models import AccessLog

        client.get("/pipeline")  # no auth header -- 401, still logged
        row = db_session.exec(select(AccessLog).order_by(AccessLog.id.desc())).first()
        assert row is not None
        assert row.username is None
        assert row.role is None
