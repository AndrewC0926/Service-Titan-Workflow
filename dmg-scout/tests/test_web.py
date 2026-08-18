"""Dashboard smoke tests: auth enforcement and each view renders with data."""
import base64
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.manual import add_manual_signal
from app.models import Contractor, RawDocument, RetrofitBuilding, Signal, SourceRun
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


def seed_callable(db_session, cfg):
    """Like seed(), but with a named person a rep could actually call — the
    plain seed() fixture's project has a developer name but nobody with a
    phone or email, so it never reaches THREE TO CALL on the Today page."""
    s = add_manual_signal(db_session, "prequal_invite", "ACCO invited prequal for Meridian",
                          project_name="Meridian DC", developer="Vantage Data Centers",
                          county="San Bernardino", state="CA", mw_it=176, stage="design")
    sig = db_session.get(Signal, s.id)
    sig.named_people = [{"name": "Jane Doe", "title": "Mechanical Engineer",
                        "org": "Vantage Data Centers", "phone": "555-0100"}]
    db_session.add(sig)
    db_session.commit()
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)


def test_requires_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=BAD).status_code == 401


def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_today_is_the_landing_page(client, db_session, cfg):
    """/ is Today now, not the board — the board moved to /board and stays
    exactly as it is (see test_board_renders)."""
    seed_callable(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert "Jane Doe" in r.text and "Meridian DC" in r.text  # THREE TO CALL
    assert "NEW: Meridian DC" in r.text  # a fresh project is itself a change

    # A second load must show the SAME thing (changes_preview is read-only) —
    # not "nothing changed", which would mean the first page view had
    # silently consumed tomorrow's digest item.
    r2 = client.get("/", headers=AUTH)
    assert "NEW: Meridian DC" in r2.text


def test_today_called_them_logs_outreach_without_navigating(client, db_session, cfg):
    seed_callable(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert "hx-post" in r.text and "/project/1/outreach" in r.text

    r = client.post("/project/1/outreach", headers={**AUTH, "HX-Request": "true"},
                    data={"channel": "call", "notes": "Called Jane Doe"})
    assert r.status_code == 200
    assert "Logged" in r.text
    assert "left VM" not in r.text  # a partial, not the full project page

    assert "Called Jane Doe" in client.get("/project/1", headers=AUTH).text


def test_board_renders(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200
    assert "Meridian DC" in r.text
    # The window stamp reads IN-BOD; the enum value is IN_BOD. Asserting on the
    # rendered string is the point — the window has to be legible on the row, and
    # colour alone never carries it.
    assert "IN-BOD" in r.text  # design stage -> IN_BOD window


def test_contractors_list_renders(client, db_session, cfg):
    db_session.add(Contractor(license_no="1", business_name="Test Mechanical", county="Los Angeles",
                              classifications="C20", primary_status="CLEAR",
                              nearby_replacement_candidates=3, nearby_radius_miles=15))
    db_session.commit()
    r = client.get("/contractors", headers=AUTH)
    assert r.status_code == 200
    assert "Test Mechanical" in r.text
    assert "Los Angeles" in r.text


def test_contractors_list_county_filter(client, db_session, cfg):
    # classifications=C20 on both: /contractors now defaults to mechanical-only
    # (see test_contractors_list_defaults_to_mechanical_only below), and this
    # test is about the county filter specifically, not that default.
    db_session.add(Contractor(license_no="1", business_name="LA Co", county="Los Angeles", classifications="C20"))
    db_session.add(Contractor(license_no="2", business_name="OC Co", county="Orange", classifications="C20"))
    db_session.commit()
    r = client.get("/contractors?county=Orange", headers=AUTH)
    assert r.status_code == 200
    assert "OC Co" in r.text
    assert "LA Co" not in r.text


def test_contractors_list_defaults_to_mechanical_only(client, db_session, cfg):
    """The bug this guards against: unfiltered, 43 of the top 50 by any
    ranking were plain "B" (general building) licenses -- not a call list
    for a mechanical rep. classification must default to C-20/C-38 without
    the caller asking for it, with "all" as the explicit widen-out."""
    db_session.add(Contractor(license_no="1", business_name="General Builder Co",
                              classifications="B", primary_status="CLEAR"))
    db_session.add(Contractor(license_no="2", business_name="AC Mechanical Co",
                              classifications="C20", primary_status="CLEAR"))
    db_session.commit()

    default = client.get("/contractors", headers=AUTH)
    assert "AC Mechanical Co" in default.text
    assert "General Builder Co" not in default.text

    widened = client.get("/contractors?classification=all", headers=AUTH)
    assert "AC Mechanical Co" in widened.text
    assert "General Builder Co" in widened.text


def test_contractors_list_requires_auth(client, db_session, cfg):
    assert client.get("/contractors").status_code == 401


def test_retrofit_building_detail_renders(client, db_session, cfg):
    b = RetrofitBuilding(apn="123-456-789", population="replacement_candidate",
                         address="1 Test Way", county="Los Angeles", state="CA",
                         latitude=34.05, longitude=-118.25, rank_score=5.0)
    db_session.add(b)
    db_session.add(Contractor(license_no="1", business_name="Nearby HVAC", classifications="C20",
                              primary_status="CLEAR", latitude=34.06, longitude=-118.26))
    db_session.commit()
    db_session.refresh(b)

    r = client.get(f"/retrofit/building/{b.id}", headers=AUTH)
    assert r.status_code == 200
    assert "1 Test Way" in r.text
    assert "Nearby HVAC" in r.text


def test_retrofit_building_detail_404_for_unknown_id(client, db_session, cfg):
    assert client.get("/retrofit/building/999999", headers=AUTH).status_code == 404


def test_retrofit_building_detail_handles_ungeocoded_building(client, db_session, cfg):
    b = RetrofitBuilding(apn="no-geo", population="replacement_candidate", address="Unknown Rd")
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    r = client.get(f"/retrofit/building/{b.id}", headers=AUTH)
    assert r.status_code == 200
    assert "not yet geocoded" in r.text


def test_retrofit_board_links_to_building_detail(client, db_session, cfg):
    b = RetrofitBuilding(apn="123", population="replacement_candidate", address="1 Test Way",
                         county="Los Angeles", state="CA", rank_score=5.0)
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    r = client.get("/retrofit?population=replacement_candidate", headers=AUTH)
    assert r.status_code == 200
    assert f"/retrofit/building/{b.id}" in r.text


def test_retrofit_board_shows_ebewe_coverage_and_has_ebewe_filter(client, db_session, cfg):
    """Coverage is disclosed on the page itself (same discipline as the
    replacement-candidate population's false-positive-direction callout),
    and has_ebewe=true must narrow to only matched rows -- see
    app/assumptions.py's "Benchmark-to-building join method and coverage"
    entry for why this can never be a board-wide rank_buildings() term."""
    matched = RetrofitBuilding(apn="M1", population="replacement_candidate", address="1 Matched Way",
                               county="Los Angeles", state="CA", rank_score=5.0,
                               ebewe_matched=True, ebewe_building_id="B1", ebewe_program_year=2024,
                               ebewe_energy_star_score=22, ebewe_weather_normalized_site_eui=88.0,
                               ebewe_arcx_due_this_year=True)
    unmatched = RetrofitBuilding(apn="U1", population="replacement_candidate", address="2 Unmatched Rd",
                                 county="Los Angeles", state="CA", rank_score=4.0)
    db_session.add(matched)
    db_session.add(unmatched)
    db_session.commit()

    r = client.get("/retrofit?population=replacement_candidate", headers=AUTH)
    assert r.status_code == 200
    assert "1 of" in r.text or "1 of&#160;" in r.text or "covers" in r.text.lower()
    assert "1 Matched Way" in r.text and "2 Unmatched Rd" in r.text
    assert "A/RCx due this year" in r.text

    filtered = client.get("/retrofit?population=replacement_candidate&has_ebewe=true", headers=AUTH)
    assert filtered.status_code == 200
    assert "1 Matched Way" in filtered.text
    assert "2 Unmatched Rd" not in filtered.text


def test_retrofit_board_excludes_no_address_rows_and_discloses_the_count(client, db_session, cfg):
    """A call list should not contain a row that can't be called -- see the
    commit that added this. No-address rows are excluded from the ranked
    view and its total entirely (not sorted last), and the exclusion is
    disclosed via a callout, not silently dropped from view."""
    has_address = RetrofitBuilding(apn="HA1", population="replacement_candidate",
                                   address="1 Callable St", county="Los Angeles", state="CA",
                                   rank_score=5.0)
    no_address_null = RetrofitBuilding(apn="NA1", population="replacement_candidate",
                                       address=None, county="Los Angeles", state="CA", rank_score=9.0)
    no_address_blank = RetrofitBuilding(apn="NA2", population="replacement_candidate",
                                        address="", county="Los Angeles", state="CA", rank_score=8.0)
    db_session.add(has_address)
    db_session.add(no_address_null)
    db_session.add(no_address_blank)
    db_session.commit()

    r = client.get("/retrofit?population=replacement_candidate", headers=AUTH)
    assert r.status_code == 200
    assert "1 Callable St" in r.text
    assert "NA1" not in r.text and "NA2" not in r.text
    assert "2 buildings excluded" in r.text or "2&nbsp;buildings excluded" in r.text


def test_retrofit_report_excludes_no_address_rows(client, db_session, cfg):
    has_address = RetrofitBuilding(apn="HA2", population="replacement_candidate",
                                   address="1 Reportable Ave", county="Los Angeles", state="CA",
                                   rank_score=5.0, service_life_status="due")
    no_address = RetrofitBuilding(apn="NA3", population="replacement_candidate", address=None,
                                  county="Los Angeles", state="CA", rank_score=9.0,
                                  service_life_status="overdue")
    db_session.add(has_address)
    db_session.add(no_address)
    db_session.commit()

    r = client.get("/retrofit/report?population=replacement_candidate&min_status=due", headers=AUTH)
    assert r.status_code == 200
    assert "1 Reportable Ave" in r.text
    assert "NA3" not in r.text


def test_retrofit_board_sold_last_24mo_filter_and_badge(client, db_session, cfg):
    """Change-of-ownership recency is a filter + badge, not a board-wide
    ranking term (demoted 2026-08-16 -- see app/assumptions.py's "Retrofit
    ranking: ownership-change recency" entry for why: ~2% coverage, and the
    Assessor's RecordingDate fires on trust/family transfers and
    reassessment-triggering refinances, not only arms-length sales). The
    filter must narrow to recently-sold rows; the badge must show on any
    row with a sale on record regardless of the filter."""
    from datetime import timedelta

    from app.models import utcnow

    recent = RetrofitBuilding(apn="R1", population="replacement_candidate",
                              address="1 Recent Sale Way", county="Los Angeles", state="CA",
                              rank_score=5.0, last_sale_date=utcnow() - timedelta(days=30),
                              last_sale_source="la_county_assessor_recording_date")
    stale = RetrofitBuilding(apn="S1", population="replacement_candidate",
                             address="2 Stale Sale Rd", county="Los Angeles", state="CA",
                             rank_score=4.0, last_sale_date=utcnow() - timedelta(days=3000),
                             last_sale_source="la_county_assessor_recording_date")
    never = RetrofitBuilding(apn="N1", population="replacement_candidate",
                             address="3 Never Sold Ln", county="Los Angeles", state="CA",
                             rank_score=3.0)
    db_session.add(recent)
    db_session.add(stale)
    db_session.add(never)
    db_session.commit()

    unfiltered = client.get("/retrofit?population=replacement_candidate", headers=AUTH)
    assert unfiltered.status_code == 200
    assert "1 Recent Sale Way" in unfiltered.text
    assert "2 Stale Sale Rd" in unfiltered.text
    assert "3 Never Sold Ln" in unfiltered.text
    # Every row with a sale on record gets a badge, recent or not.
    assert unfiltered.text.count("Sold ") >= 2

    filtered = client.get("/retrofit?population=replacement_candidate&sold_last_24mo=true", headers=AUTH)
    assert filtered.status_code == 200
    assert "1 Recent Sale Way" in filtered.text
    assert "2 Stale Sale Rd" not in filtered.text
    assert "3 Never Sold Ln" not in filtered.text
    assert "Sold in last 24 months" in filtered.text


def test_masthead_data_as_of_is_utc(client, db_session, cfg):
    """The masthead's one remaining timestamp is UTC (tb.data_as_of =
    max(RawDocument.fetched_at)) -- carries a trailing Z. Supersedes the old
    two-field "Data as of vs. Printed carry the same convention" regression
    test: the 2026-08-18 IA pass cut Printed entirely (it duplicated Data as
    of and existed only to serve the bordered-sheet masthead this pass also
    removed), so there is no longer a second field to compare against."""
    db_session.add(RawDocument(source="rss", source_uid="x1", url="https://x", title="t",
                               content_hash="h1", raw_text="body"))
    db_session.commit()
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200

    data_as_of = re.search(r'Data as of</span>\s*<span class="mono">([^<]+)</span>', r.text)
    assert data_as_of, "masthead markup changed shape"
    assert data_as_of.group(1).endswith("Z")


def test_board_shows_whether_there_is_anyone_to_call(client, db_session, cfg):
    """The charter's criterion is a project PLUS a human, so the board shows both.

    Without this column the board cannot be read as a call list — a rep has to
    open every row to find out whether there is anyone on the other end. The three
    states are distinct on purpose: a name with no phone and no email is a
    research task, and labelling it as coverage is how a board of 58 rows once
    looked reachable when it was not.
    """
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    assert "Who to call" in r.text
    assert ("No one" in r.text or "Research" in r.text
            or "tel" in r.text or "@" in r.text)


def test_board_score_bar_is_scaled_to_the_board_maximum(client, db_session, cfg):
    """Scores cluster in a narrow band, so the bar scales to the range that exists.

    A fixed 0-1 scale spends most of its length on range that never occurs. The
    top-scoring row must therefore render a full-width bar whatever its raw score.
    """
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
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
    board = client.get("/board", headers=AUTH).text
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
    assert "Meridian DC" not in client.get("/board", headers=AUTH).text

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
    assert ".masthead" in css.text  # the real built stylesheet, not a 404 page


def test_retrofit_counties_correct_and_does_not_load_every_full_row(client, db_session):
    """Regression test for the 236MB /retrofit memory bug (2026-08-13):
    computing the county filter list used to run select(RetrofitBuilding)
    with no filter and no limit, instantiating every full ORM row (every
    column, including long basis-text fields) in the population just to
    read .county off each one -- measured via tracemalloc against
    production data: 236.4MB for 53,252 rows vs 0.02MB for the column-only
    equivalent with an identical result.

    A memory-threshold assertion turned out not to be a reliable way to
    pin this down here (SQLite's row materialization in the test DB doesn't
    reproduce psycopg2's allocation shape against real Postgres closely
    enough -- it passed against the unfixed query too). What's actually
    deterministic, driver-independent, and directly tests the thing that
    matters is the SQL itself: no unlimited query against retrofit_buildings
    should select more than the county column."""
    from sqlalchemy import event

    from app.db import get_engine
    from app.models import RetrofitBuilding

    for i in range(50):
        db_session.add(RetrofitBuilding(
            apn=f"TESTAPN{i:07d}", population="replacement_candidate",
            county="Los Angeles" if i % 2 == 0 else "Riverside",
            rank_score=float(i), service_life_basis="x" * 2000,
        ))
    db_session.commit()

    statements = []
    engine = get_engine()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        resp = client.get("/retrofit?population=replacement_candidate&limit=10", headers=AUTH)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    assert "Los Angeles" in resp.text and "Riverside" in resp.text

    for sql in statements:
        if "retrofit_buildings" not in sql or "LIMIT" in sql.upper():
            continue  # the properly-limited `buildings` query is fine either way
        if sql.strip().upper().startswith("SELECT COUNT("):
            # The legitimate `total` count (line ~372): count(*) over a
            # subquery that happens to spell out RetrofitBuilding's columns
            # in its SQL text because select(RetrofitBuilding) wraps the
            # whole entity, but only an integer ever crosses into Python --
            # not the bug this test guards against.
            continue
        assert "service_life_basis" not in sql, (
            f"an unlimited query against retrofit_buildings selects service_life_basis (and, by the "
            f"same regression, every other column) instead of just what it needs:\n{sql}"
        )


def test_hospitals_board_renders_and_is_territory_scoped_by_default(client, db_session, cfg):
    from app.models import HospitalBuilding, utcnow
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="LA Test Hospital",
                                    county="Los Angeles", spc_rating="1", npc_rating="2",
                                    spc_deadline_year=2020, npc_deadline_year=2030,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    db_session.add(HospitalBuilding(perm_id="2", building_nbr="B1", facility_name="Out Of Territory Hospital",
                                    county="Alameda", spc_rating="5", npc_rating="5",
                                    meets_2030_standard=True,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    db_session.commit()

    r = client.get("/hospitals", headers=AUTH)
    assert r.status_code == 200
    assert "LA Test Hospital" in r.text
    assert "Out Of Territory Hospital" not in r.text  # territory-scoped by default

    r_all = client.get("/hospitals?all_ca=1", headers=AUTH)
    assert "Out Of Territory Hospital" in r_all.text  # reachable with all_ca=1


def test_hospitals_board_shows_capability_gap_warning(client, db_session, cfg):
    r = client.get("/hospitals", headers=AUTH)
    assert "cannot currently field a full mechanical package" in r.text.lower()


def test_hospitals_board_deadline_filter(client, db_session, cfg):
    from app.models import HospitalBuilding, utcnow
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="Overdue Hospital",
                                    county="Los Angeles", spc_rating="1", spc_deadline_year=2020,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    db_session.add(HospitalBuilding(perm_id="2", building_nbr="B1", facility_name="2030 Hospital",
                                    county="Los Angeles", spc_rating="2", spc_deadline_year=2030,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    db_session.commit()

    r = client.get("/hospitals?deadline=2020", headers=AUTH)
    assert "Overdue Hospital" in r.text
    assert "2030 Hospital" not in r.text


def test_hospital_building_detail_renders(client, db_session, cfg):
    from app.models import HospitalBuilding, utcnow
    b = HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="Detail Test Hospital",
                         building_name="Main Tower", county="Los Angeles", spc_rating="3",
                         npc_rating="5", snapshot_date=utcnow(), source_url="https://example.com")
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)

    r = client.get(f"/hospitals/building/{b.id}", headers=AUTH)
    assert r.status_code == 200
    assert "Detail Test Hospital" in r.text
    assert "Main Tower" in r.text


def test_hospital_building_detail_404_for_unknown_id(client, db_session, cfg):
    assert client.get("/hospitals/building/999999", headers=AUTH).status_code == 404


def test_hospitals_board_not_mixed_into_project_board(client, db_session, cfg):
    """A different sale, a different buyer -- hospital rows must never
    appear on /board."""
    from app.models import HospitalBuilding, utcnow
    seed(db_session, cfg)
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="Should Not Leak Hospital",
                                    county="Los Angeles", snapshot_date=utcnow(),
                                    source_url="https://example.com"))
    db_session.commit()
    assert "Should Not Leak Hospital" not in client.get("/board", headers=AUTH).text


def test_hospitals_brief_renders_no_dollar_estimate(client, db_session, cfg):
    from app.accounts import seed_product_lines
    seed_product_lines(db_session, cfg)
    r = client.get("/hospitals/brief", headers=AUTH)
    assert r.status_code == 200
    # The template line-wraps this sentence for source readability; the
    # rendered HTML preserves those newlines literally (browsers collapse
    # them, r.text does not), so check the pieces rather than one long
    # exact substring.
    assert "NPC 5 assigned to" in r.text
    assert "72 hours" in r.text
    assert "$" not in r.text.split('<h2 style="font-size:18px">')[1]  # no dollar figure in the brief body


def test_hospitals_brief_shows_climacool_and_twin_city_fan_expired(client, db_session, cfg):
    from app.accounts import seed_product_lines
    seed_product_lines(db_session, cfg)
    r = client.get("/hospitals/brief", headers=AUTH)
    assert "ClimaCool" in r.text
    assert "TCF/Twin City Fan" in r.text
    assert "Expired" in r.text and "renewal ask" in r.text  # split status section header, not a single gap column
    assert "Not listed" in r.text and "new-application ask" in r.text
    # all nine previously-unresearched fan lines are now individually reported
    for name in ("Berner", "Canarm", "FanAm", "MacroAir", "Panasonic",
                "Delta Breez", "Broan NuTone", "Systemair", "Monoxivent"):
        assert name in r.text


def test_esco_board_is_reachable_and_separate(client, db_session, cfg):
    """esco rows are kept and counted, but do not join a ranking of new
    construction they are not competing in."""
    from app.models import Category, Project, Stage, Window
    seed(db_session, cfg)
    db_session.add(Project(name="City Hall ESPC", category=Category.esco,
                           stage=Stage.procurement, status="active", in_territory=True,
                           score=0.4, window=Window.PRE_BOD, county="Clark", state="NV"))
    db_session.commit()
    assert "City Hall ESPC" in client.get("/board?category=esco", headers=AUTH).text
    assert "City Hall ESPC" not in client.get("/board?category=all", headers=AUTH).text
