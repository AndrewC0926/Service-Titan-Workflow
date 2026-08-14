"""Access logging middleware: what gets logged, what doesn't, the one-time
new-IP notification, and /admin/access's admin-only gate.

username is read from the raw Authorization header, not from auth()'s return
value -- see app/access_log.py's module docstring for why (auth() 401s
before a non-admin username would ever reach a route, and in any case the
notification no longer keys on username at all -- see KNOWN_IPS and
log_access there for why it's keyed on IP instead)."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.access_log import KNOWN_IPS
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


def _client_from(db_session, monkeypatch, ip: str) -> TestClient:
    """Same setup as the `client` fixture, but with a controllable source
    IP -- TestClient's default ('testclient') isn't a real IP and isn't in
    KNOWN_IPS, which is exactly what the plain `client` fixture is for
    (anything not explicitly about IP behavior), but the new-IP
    notification tests need to pick specific IPs on purpose."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    app.dependency_overrides[get_session] = lambda: db_session
    return TestClient(app, client=(ip, 12345))


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


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


def test_known_ip_never_triggers_notification(db_session, monkeypatch, no_email):
    """68.4.250.117 and 127.0.0.1 are excluded outright -- the admin's own
    browser and local dev tunnel must never (re-)trigger this, no matter
    how many first-time-looking hits they generate."""
    known_ip = next(iter(KNOWN_IPS))
    c = _client_from(db_session, monkeypatch, known_ip)
    c.get("/board", headers=AUTH)
    c.get("/board", headers=AUTH)
    app.dependency_overrides.clear()
    assert no_email == []


def test_unauthenticated_hits_from_a_new_ip_never_trigger_notification(db_session, monkeypatch, no_email):
    """This is the exact shape of email-link-prescanner traffic (Office365
    Safe Links, Proofpoint, etc.): a burst of new IPs, no Authorization
    header at all. Confirmed directly in production access_log on
    2026-08-13. None of it should ever notify."""
    c = _client_from(db_session, monkeypatch, "203.0.113.9")
    c.get("/board")
    c.get("/watchlist")
    app.dependency_overrides.clear()
    assert no_email == []


def test_first_authenticated_hit_from_a_new_ip_fires_notification_once(db_session, monkeypatch, no_email):
    """A new IP that shows up unauthenticated first (the scanner-burst
    shape) and THEN authenticates -- the real signal this feature exists to
    catch -- notifies exactly once, on the first authenticated hit, not
    once per subsequent request from that same IP."""
    c = _client_from(db_session, monkeypatch, "203.0.113.9")
    c.get("/board")               # unauthenticated -- no notification
    c.get("/board", headers=AUTH)  # first authenticated hit from this IP
    c.get("/watchlist", headers=AUTH)
    app.dependency_overrides.clear()

    assert len(no_email) == 1  # exactly once, not once per request
    _args, kwargs = no_email[0]
    assert kwargs["json"]["to"] == ["acrane988@gmail.com"]
    assert "203.0.113.9" in kwargs["json"]["subject"]

    rows = db_session.exec(select(AccessLog).where(AccessLog.ip == "203.0.113.9")).all()
    assert len(rows) == 3  # every hit is still logged; only the EMAIL is once-per-IP


def test_different_new_ips_each_notify_once(db_session, monkeypatch, no_email):
    c1 = _client_from(db_session, monkeypatch, "203.0.113.9")
    c2 = _client_from(db_session, monkeypatch, "203.0.113.10")
    c1.get("/board", headers=AUTH)
    c2.get("/board", headers=AUTH)
    c1.get("/board", headers=AUTH)
    app.dependency_overrides.clear()
    assert len(no_email) == 2  # one per distinct new IP


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
