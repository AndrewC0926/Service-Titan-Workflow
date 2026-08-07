"""Dashboard smoke tests: auth enforcement and each view renders with data."""
import base64
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.manual import add_manual_signal
from app.models import SourceRun
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import app


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
BAD = {"Authorization": "Basic " + base64.b64encode(b"andrew:wrong").decode()}


def seed(db_session, cfg):
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="design")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


def test_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=BAD).status_code == 401


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_board_renders(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Meridian DC" in r.text
    # The window stamp reads IN-BOD; the enum value is IN_BOD. Asserting on the
    # rendered string is the point — the window has to be legible on the row, and
    # colour alone never carries it.
    assert "IN-BOD" in r.text  # design stage -> IN_BOD window


def test_board_shows_whether_there_is_anyone_to_call(client, db_session, cfg):
    """The charter's criterion is a project PLUS a human, so the board shows both.

    Without this column the board cannot be read as a call list — a rep has to
    open every row to find out whether there is anyone on the other end. The three
    states are distinct on purpose: a name with no phone and no email is a
    research task, and labelling it as coverage is how a board of 58 rows once
    looked reachable when it was not.
    """
    seed(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert "Who to call" in r.text
    assert ("No one" in r.text or "Research" in r.text
            or "tel" in r.text or "@" in r.text)


def test_board_score_bar_is_scaled_to_the_board_maximum(client, db_session, cfg):
    """Scores cluster in a narrow band, so the bar scales to the range that exists.

    A fixed 0-1 scale spends most of its length on range that never occurs. The
    top-scoring row must therefore render a full-width bar whatever its raw score.
    """
    seed(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert 'class="bar"' in r.text
    assert "width:100.0%" in r.text


def test_project_detail_and_notes(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/project/1", headers=AUTH)
    assert r.status_code == 200
    assert "Signal timeline" in r.text and "prequal_invite" in r.text
    assert "57,200" in r.text or "stated IT load" in r.text  # tonnage basis shown
    assert "Stage progression" in r.text and "design" in r.text

    r = client.post("/project/1/notes", headers=AUTH,
                    data={"notes": "call EOR", "next_action": "intro call"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "intro call" in client.get("/project/1", headers=AUTH).text


def test_project_page_shows_stage_progression(client, db_session, cfg):
    """A project touched by two signals at two different stages shows both,
    not just the current one — the gate for the stage-tracking work."""
    add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="entitlement")
    run_resolve(db_session, cfg, use_llm=False)
    add_manual_signal(db_session, "prequal_invite", "Meridian moved to permitting",
                      project_name="Meridian DC", developer="Vantage Data Centers",
                      county="San Bernardino", state="CA", mw_it=176, stage="permitting")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)

    r = client.get("/project/1", headers=AUTH)
    assert r.status_code == 200
    assert r.text.count("← current") == 1  # only the current stage's row(s) marked
    assert "entitlement" in r.text and "permitting" in r.text


def test_outreach_log(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.post("/project/1/outreach", headers=AUTH,
                    data={"channel": "call", "notes": "left VM"}, follow_redirects=False)
    assert r.status_code == 303
    assert "left VM" in client.get("/project/1", headers=AUTH).text


def test_other_views_render(client, db_session, cfg):
    seed(db_session, cfg)
    db_session.add(SourceRun(source="ceqanet", ok=False, error="boom"))
    db_session.commit()
    for path in ("/review", "/contacts", "/map", "/health", "/add-signal"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200, path
    assert "boom" in client.get("/health", headers=AUTH).text


def test_add_signal_form_creates_project(client, db_session, cfg):
    r = client.post("/add-signal", headers=AUTH, data={
        "signal_type": "engineer_move", "summary": "Jane Doe moved to kW MCE",
        "person_name": "Jane Doe", "person_org": "kW Mission Critical Engineering",
        "county": "Orange", "state": "CA",
    }, follow_redirects=False)
    assert r.status_code == 303
    board = client.get("/", headers=AUTH).text
    assert "Unnamed" in board or "Jane" in board


def test_gate5_views_and_exports(client, db_session, cfg):
    seed(db_session, cfg)
    # brief page renders with traceable content
    r = client.get("/project/1/brief", headers=AUTH)
    assert r.status_code == 200 and "project brief" in r.text

    # outcome form closes the project off the board
    r = client.post("/project/1/outcome", headers=AUTH,
                    data={"status": "dead", "reason": "cancelled"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Meridian DC" not in client.get("/", headers=AUTH).text

    # watchlist view + CSV exports
    assert client.get("/watchlist", headers=AUTH).status_code == 200
    for path in ("/export/board.csv", "/export/contacts.csv", "/export/firms.csv",
                 "/export/signals.csv"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200, path
        assert "text/csv" in r.headers["content-type"]
    assert client.get("/export/board.csv", headers=AUTH).text.startswith(
        "id,project,category,developer")

    # dashboard firm add + roster on contacts page
    r = client.post("/firms", headers=AUTH,
                    data={"name": "Test Firm Engineering", "firm_type": "mep",
                          "aliases": "TFE; Test Firm"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Test Firm Engineering" in client.get("/contacts", headers=AUTH).text


def test_no_password_fails_closed(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    assert client.get("/", headers=AUTH).status_code == 503


# ---- Phase C sheets ---------------------------------------------------------

def test_phase_c_sheets_render(client, db_session, cfg):
    seed(db_session, cfg)
    for path in ("/searches", "/firms", "/outreach", "/map", "/ask"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200, f"{path}: {r.status_code}"


def test_saved_search_rejects_an_unknown_filter(client, db_session, cfg):
    """The API boundary must refuse the same things the evaluator does — a filter
    that widens silently is worse than one that errors."""
    seed(db_session, cfg)
    r = client.post("/searches", headers=AUTH,
                    data={"name": "typo", "criteria_json": '{"mw_over": 10}'})
    assert r.status_code == 400
    r = client.post("/searches", headers=AUTH,
                    data={"name": "bad json", "criteria_json": "not json"})
    assert r.status_code == 400


def test_saved_search_round_trips(client, db_session, cfg):
    from sqlmodel import select

    from app.models import SavedSearch
    seed(db_session, cfg)
    r = client.post("/searches", headers=AUTH, follow_redirects=False,
                    data={"name": "Storey over 10",
                          "criteria_json": '{"county": "Storey", "min_mw": 10}',
                          "alert": "on"})
    assert r.status_code == 303
    saved = db_session.exec(select(SavedSearch)).all()
    assert len(saved) == 1 and saved[0].criteria == {"county": "Storey", "min_mw": 10}
    assert "Storey over 10" in client.get("/searches", headers=AUTH).text


# ---- deployed stylesheet delivery -------------------------------------------

def _dockerfile_forwarded_allow_ips() -> str:
    """The proxy trust boundary uvicorn actually runs with in the deployed
    image, read straight from the Dockerfile CMD so this test breaks if
    --forwarded-allow-ips is ever dropped, instead of hardcoding a value that
    could silently drift from what's really deployed."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    match = re.search(r"--forwarded-allow-ips[= ]'?([^'\s\"]+)'?", dockerfile)
    return match.group(1) if match else "127.0.0.1"  # uvicorn's own default


def test_stylesheet_link_resolves_behind_the_render_proxy(client, db_session, cfg):
    """Render terminates TLS at its edge and forwards to this container over
    plain HTTP, setting X-Forwarded-Proto: https. request.url_for() — what
    base.html's stylesheet <link> is built from — only honours that header for
    proxies uvicorn is told to trust; otherwise it stamps the link with
    scheme=http on a page the browser loaded over https, and the browser
    silently drops that as mixed content. The stylesheet itself still builds
    and serves fine on its own URL either way, and `app.css exists on disk`
    never catches this — only following the exact link the page rendered does.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seed(db_session, cfg)
    proxied_app = ProxyHeadersMiddleware(app, trusted_hosts=_dockerfile_forwarded_allow_ips())
    proxied = TestClient(proxied_app, base_url="http://dmg-scout-web-84bz.onrender.com")

    r = proxied.get("/", headers={**AUTH, "X-Forwarded-Proto": "https"})
    assert r.status_code == 200

    match = re.search(r'<link rel="stylesheet" href="([^"]+)">', r.text)
    assert match, "board page has no stylesheet <link>"
    href = match.group(1)
    assert href.startswith("https://"), (
        f"stylesheet link resolved to {href!r} on a page served over https — "
        "the browser blocks that as mixed content and the board renders unstyled"
    )

    css = proxied.get(href)
    assert css.status_code == 200
    assert ".titleblock" in css.text  # the real built stylesheet, not a 404 page


def test_esco_board_is_reachable_and_separate(client, db_session, cfg):
    """esco rows are kept and counted, but do not join a ranking of new
    construction they are not competing in."""
    from app.models import Category, Project, Stage, Window
    seed(db_session, cfg)
    db_session.add(Project(name="City Hall ESPC", category=Category.esco,
                           stage=Stage.procurement, status="active", in_territory=True,
                           score=0.4, window=Window.PRE_BOD, county="Clark", state="NV"))
    db_session.commit()
    assert "City Hall ESPC" in client.get("/?category=esco", headers=AUTH).text
    assert "City Hall ESPC" not in client.get("/?category=all", headers=AUTH).text
