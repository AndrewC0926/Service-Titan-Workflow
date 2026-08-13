"""Access logging middleware: what gets logged, what doesn't, the one-time
new-username notification, and /admin/access's admin-only gate.

username is read from the raw Authorization header, not from auth()'s return
value -- see app/access_log.py's module docstring for why (auth() 401s
before a non-admin username would ever reach a route; the raw header is the
only way "a username that isn't andrew" is a detectable event at all)."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import AccessLog
from app.web.main import app, auth


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
BOB_AUTH = {"Authorization": "Basic " + base64.b64encode(b"bob:wrongpw").decode()}


@pytest.fixture()
def no_email(monkeypatch):
    """Every test here either doesn't care about the notification or
    explicitly tests it -- default to a no-op so unrelated tests don't
    accidentally depend on network access or its absence."""
    calls = []
    monkeypatch.setattr("app.access_log.httpx.post", lambda *a, **k: calls.append((a, k)) or _FakeResp())
    return calls


class _FakeResp:
    def raise_for_status(self):
        pass


def test_static_asset_not_logged(client, db_session, no_email):
    client.get("/static/app.css")
    assert db_session.exec(select(AccessLog)).all() == []


def test_healthz_not_logged(client, db_session, no_email):
    client.get("/healthz")
    assert db_session.exec(select(AccessLog)).all() == []


def test_unauthenticated_page_hit_logs_null_username(client, db_session, no_email):
    resp = client.get("/")
    assert resp.status_code == 401  # auth() still gates the page itself
    rows = db_session.exec(select(AccessLog)).all()
    assert len(rows) == 1
    assert rows[0].username is None
    assert rows[0].path == "/"
    assert rows[0].method == "GET"


def test_authenticated_admin_hit_logs_username(client, db_session, no_email):
    client.get("/healthz")  # not logged, sanity check it's excluded regardless of auth
    client.get("/board", headers=AUTH)
    rows = db_session.exec(select(AccessLog)).all()
    assert len(rows) == 1
    assert rows[0].username == "andrew"


def test_admin_username_never_triggers_notification(client, db_session, no_email):
    client.get("/board", headers=AUTH)
    client.get("/board", headers=AUTH)
    assert no_email == []


def test_first_login_email_fires_exactly_once_per_new_username(client, db_session, no_email):
    """bob's password is wrong -- auth() 401s the page -- but the raw
    username still gets logged and the notification still fires, since it's
    a real signal (someone tried a different username) independent of
    whether the credentials checked out."""
    client.get("/board", headers=BOB_AUTH)
    client.get("/board", headers=BOB_AUTH)
    client.get("/watchlist", headers=BOB_AUTH)

    assert len(no_email) == 1  # exactly once, not once per request
    _args, kwargs = no_email[0]
    assert kwargs["json"]["to"] == ["acrane988@gmail.com"]
    assert "bob" in kwargs["json"]["subject"]

    rows = db_session.exec(select(AccessLog).where(AccessLog.username == "bob")).all()
    assert len(rows) == 3  # every hit is still logged; only the EMAIL is once-per-username


def test_different_new_usernames_each_notify_once(client, db_session, no_email):
    eve_auth = {"Authorization": "Basic " + base64.b64encode(b"eve:x").decode()}
    client.get("/board", headers=BOB_AUTH)
    client.get("/board", headers=eve_auth)
    client.get("/board", headers=BOB_AUTH)
    assert len(no_email) == 2  # one per distinct new username


def test_admin_access_rejects_non_admin_user(client, db_session, no_email):
    """No real second user can authenticate (auth() only ever accepts the
    configured admin username), so this exercises the route's own explicit
    check the way it will actually matter -- an authenticated non-admin
    request -- via a dependency override, the same pattern this app's own
    tests already use for get_session."""
    app.dependency_overrides[auth] = lambda: "bob"
    try:
        resp = client.get("/admin/access")
    finally:
        del app.dependency_overrides[auth]
    assert resp.status_code == 403


def test_admin_access_allows_admin_user(client, db_session, no_email):
    client.get("/board", headers=AUTH)
    resp = client.get("/admin/access", headers=AUTH)
    assert resp.status_code == 200
    assert "andrew" in resp.text


def test_admin_access_itself_requires_auth(client, db_session, no_email):
    assert client.get("/admin/access").status_code == 401
