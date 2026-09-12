"""Block 3 (Master Plan v3.2 section 13): the redesign skeleton. Nav
collapsed from the 2026-08-18 pass's 5-primary/19-overflow/24-total flat
list to a left rail of six always-visible destinations (Today, Pipeline,
Accounts, Signals, Deadlines, Reports) plus Settings for an operator only.
No overflow disclosure any more -- 6-7 items fit without one. Every route
that used to be a top-level nav destination still works, 307-redirected to
its new home under one of the six (or Settings) -- see
app.web.main._OLD_ROUTE_REDIRECTS. Per-user HTTP Basic Auth from
config.yaml's dashboard.users (app.access_log.configured_users) replaces
the single shared admin credential; Settings is gated on
dashboard.operator_usernames via app.web.main.operator()."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.manual import add_manual_signal
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}

RAIL_LABELS = {"Today", "Pipeline", "Accounts", "Signals", "Deadlines", "Reports"}

# Every old flat-nav path -> where it 307-redirects today. Mirrors
# app.web.main._OLD_ROUTE_REDIRECTS exactly -- kept as a separate literal
# table here (not imported from main) so a drift between the two is a
# visible test failure, not a tautology.
OLD_ROUTE_REDIRECTS = {
    "/board": "/signals/entitlement",
    "/retrofit": "/signals/permit-gap",
    "/replacement-leads": "/signals/replacement-leads",
    "/map": "/signals/map",
    "/add-signal": "/signals/add",
    "/searches": "/signals/saved-searches",
    "/contractors": "/accounts/contractors",
    "/firms": "/accounts/firms",
    "/contacts": "/accounts/contacts",
    "/watchlist": "/accounts/watchlist",
    "/hospitals": "/deadlines/hospitals",
    "/ab869": "/deadlines/ab869",
    "/review": "/pipeline/review",
    "/corrections-review": "/pipeline/corrections",
    "/outreach": "/pipeline/outreach",
    "/ask": "/reports/ask",
    "/reference": "/settings/reference",
    "/assumptions": "/settings/assumptions",
    "/lines": "/settings/lines",
    "/capture": "/settings/capture",
    "/captures": "/settings/captures",
    "/intel": "/settings/intel",
    "/health": "/settings/health",
}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed(db_session, cfg):
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="design")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


# --- the rail: six destinations, no overflow, Settings operator-only -----------

def test_nav_is_no_longer_the_old_flat_list(client, db_session, cfg):
    """Fails against the pre-Block-3 nav by construction: that nav had 24
    items (5 primary tabs + 19 overflow) and no operator gate at all."""
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    for label in RAIL_LABELS:
        assert f'class="rail-item"' in r.text
    assert r.text.count('class="rail-item"') + r.text.count('class="rail-item rail-item-settings"') == 7
    assert "navmore" not in r.text  # the old overflow disclosure is gone


def test_rail_has_exactly_the_six_destinations_plus_settings_for_an_operator(client, db_session, cfg):
    r = client.get("/", headers=AUTH)
    for href in ("/", "/pipeline", "/accounts", "/signals", "/deadlines", "/reports", "/settings"):
        assert f'href="{href}"' in r.text


def test_settings_is_hidden_from_a_non_operator(client, db_session, cfg, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_JASON", "jasonpw")
    monkeypatch.setattr(
        "app.web.main.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    monkeypatch.setattr(
        "app.access_log.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    jason_auth = {"Authorization": "Basic " + base64.b64encode(b"jason:jasonpw").decode()}
    r = client.get("/", headers=jason_auth)
    assert r.status_code == 200
    assert 'href="/settings"' not in r.text


def test_non_operator_gets_403_on_settings(client, db_session, cfg, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_JASON", "jasonpw")
    monkeypatch.setattr(
        "app.web.main.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    jason_auth = {"Authorization": "Basic " + base64.b64encode(b"jason:jasonpw").decode()}
    r = client.get("/settings", headers=jason_auth)
    assert r.status_code == 403


def test_operator_gets_200_on_settings(client, db_session, cfg):
    r = client.get("/settings", headers=AUTH)
    assert r.status_code == 200


def _cfg_with_users(cfg, users, operators):
    """A thin wrapper around the REAL Config that overrides just
    dashboard.users/operator_usernames -- app.access_log.configured_users
    and app.web.main.operator only ever call .get() for those two keys, but
    other code reached from the same request (today_brief -> _stale_sources
    touches cfg.data directly) needs the real object underneath, not a bare
    stand-in that only implements .get()."""
    class _Cfg:
        def __getattr__(self, name):
            return getattr(cfg, name)

        def get(self, key, default=None):
            if key == "dashboard.users":
                return [{"username": u, "password_env": e} for u, e in users]
            if key == "dashboard.operator_usernames":
                return operators
            return cfg.get(key, default)
    return _Cfg()


# --- per-user auth: two real users authenticate as themselves ------------------

def test_two_configured_users_authenticate_as_distinct_usernames(client, db_session, cfg, monkeypatch):
    """The actual point of Block 3's auth change: a second real person no
    longer shows up in AccessLog as 'andrew'."""
    from app.models import AccessLog
    from sqlmodel import select

    monkeypatch.setenv("DASHBOARD_PASSWORD_JASON", "jasonpw")
    monkeypatch.setattr(
        "app.web.main.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    monkeypatch.setattr(
        "app.access_log.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    jason_auth = {"Authorization": "Basic " + base64.b64encode(b"jason:jasonpw").decode()}
    assert client.get("/", headers=AUTH).status_code == 200
    assert client.get("/", headers=jason_auth).status_code == 200

    usernames = {row.username for row in db_session.exec(select(AccessLog)).all()}
    assert usernames == {"andrew", "jason"}


def test_wrong_password_still_401s_with_a_configured_user_list(client, db_session, cfg, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD_JASON", "jasonpw")
    monkeypatch.setattr(
        "app.web.main.load_config",
        lambda: _cfg_with_users(cfg, [("andrew", "DASHBOARD_PASSWORD"), ("jason", "DASHBOARD_PASSWORD_JASON")],
                                operators=["andrew"]),
    )
    bad_auth = {"Authorization": "Basic " + base64.b64encode(b"jason:wrongpw").decode()}
    assert client.get("/", headers=bad_auth).status_code == 401


# --- old routes keep working: every one 307-redirects to its new home ----------

@pytest.mark.parametrize("old_path,new_path", sorted(OLD_ROUTE_REDIRECTS.items()))
def test_old_route_redirects_to_its_new_home(client, old_path, new_path):
    r = client.get(old_path, follow_redirects=False)
    assert r.status_code == 307, f"{old_path} did not 307 (got {r.status_code})"
    assert r.headers["location"] == new_path, f"{old_path} redirected to {r.headers['location']!r}, expected {new_path!r}"


@pytest.mark.parametrize("old_path", ["/firms", "/contacts", "/searches", "/add-signal", "/intel"])
def test_old_post_paths_also_redirect(client, old_path):
    r = client.post(old_path, follow_redirects=False)
    assert r.status_code == 307


def test_every_relocated_page_loads_at_its_new_path(client, db_session, cfg):
    """The proof that nothing was retired -- every new path renders 200,
    not just redirects away from the old one."""
    for new_path in set(OLD_ROUTE_REDIRECTS.values()):
        r = client.get(new_path, headers=AUTH)
        assert r.status_code == 200, f"{new_path} returned {r.status_code}"


# --- server-rendered command-palette search (accounts + projects) --------------

def test_search_finds_a_project_by_name(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/search?q=Meridian", headers=AUTH)
    assert r.status_code == 200
    assert "Meridian DC" in r.text


def test_search_finds_an_account_by_name(client, db_session, cfg):
    from app.models import Account
    db_session.add(Account(name="Rady Children's Hospital", name_norm="rady childrens hospital"))
    db_session.commit()
    r = client.get("/search?q=Rady", headers=AUTH)
    assert r.status_code == 200
    assert "Rady Children" in r.text


def test_search_with_no_matches_shows_empty_state(client, db_session, cfg):
    r = client.get("/search?q=zzzznomatchzzzz", headers=AUTH)
    assert r.status_code == 200
    assert "No accounts or projects match" in r.text


def test_search_requires_auth(client):
    assert client.get("/search?q=x").status_code == 401
