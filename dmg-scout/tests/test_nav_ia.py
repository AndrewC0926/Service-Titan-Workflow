"""The 2026-08-18 information-architecture pass (see
/home/andrewcrane/.claude/plans/parallel-crunching-clover.md): nav collapsed
to 5 primary tabs + an overflow menu + a Cmd-K command palette fed by the
same list, watch/review counts moved from the nav tab bar onto Today,
methodology callouts collapsed behind "How this is built" disclosures
(closed by default), and a two-tier stats strip on the board. No color/font/
widget assertions here -- those are covered by tests/test_widgets.py and
were explicitly out of scope for this pass."""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.manual import add_manual_signal
from app.models import MatchCandidate, Project, Signal
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}

PRIMARY_TABS = {"Today", "Board", "Retrofit", "Contractors", "Accounts"}
OVERFLOW_LABELS = {
    "Watch list", "Review queue", "Ask", "Saved searches", "Outreach", "Captures",
    "Hospitals", "Line card", "Reference", "Firms", "Contacts", "Map", "Source health",
    "Assumptions", "Add signal",
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


# --- nav: 5 primary tabs, never a second row ------------------------------------

def test_primary_nav_has_exactly_five_tabs(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200
    # The 5 primary tabs render as <a class="tab"> outside the overflow
    # panel -- count them by their exact href set, not just label text
    # (labels also appear inside the overflow panel and the palette JSON).
    for href in ("/", "/board", "/retrofit", "/contractors", "/accounts"):
        assert f'href="{href}" class="tab"' in r.text
    # Nothing else gets class="tab" -- every other destination is inside
    # .navmore-panel or the palette JSON only.
    assert r.text.count('class="tab"') == 5


def test_overflow_menu_contains_every_non_primary_destination(client, db_session, cfg):
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200
    panel_start = r.text.index('class="navmore-panel"')
    panel_end = r.text.index("</details>", panel_start)
    panel = r.text[panel_start:panel_end]
    for label in OVERFLOW_LABELS:
        assert label in panel, f"{label!r} missing from the overflow menu"
    # And none of the 5 primary tabs leak into the overflow panel too.
    for label in PRIMARY_TABS:
        assert label not in panel, f"{label!r} should not be in the overflow menu"


def test_command_palette_json_has_all_twenty_destinations(client, db_session, cfg):
    import json
    r = client.get("/board", headers=AUTH)
    start = r.text.index('id="nav-items">') + len('id="nav-items">')
    end = r.text.index("</script>", start)
    items = json.loads(r.text[start:end])
    assert len(items) == 20
    labels = {it["label"] for it in items}
    assert labels == PRIMARY_TABS | OVERFLOW_LABELS
    assert sum(1 for it in items if it["primary"]) == 5


def test_theme_toggle_has_no_visible_text_label(client, db_session, cfg):
    """2026-08-18: the BOND/DARK text label is gone -- icon only, aria-label
    carries what a screen reader needs."""
    r = client.get("/board", headers=AUTH)
    assert "BOND" not in r.text
    assert 'aria-label="Toggle dark mode"' in r.text


# --- masthead: one line, Printed/Sheet gone, counts moved to the footer ---------

def test_masthead_drops_sheet_and_printed(client, db_session, cfg):
    r = client.get("/board", headers=AUTH)
    assert "Sheet tone" not in r.text
    assert ">Printed<" not in r.text


def test_project_signal_document_counts_moved_to_the_footer(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    assert 'class="pagefoot"' in r.text
    foot = r.text[r.text.index('class="pagefoot"'):]
    assert "projects" in foot and "signals" in foot and "documents" in foot


# --- Today: watch/review counts surfaced as badges ------------------------------

def test_today_shows_review_queue_badge_when_nonzero(client, db_session, cfg):
    seed(db_session, cfg)
    sig = db_session.exec(select(Signal)).first()
    db_session.add(MatchCandidate(signal_id=sig.id, project_id=1, similarity=0.5,
                                  llm_verdict="uncertain", status="pending"))
    db_session.commit()

    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    # class="chip" distinguishes the Today badge from the plain overflow-menu
    # link to the same page, which is present on every route regardless.
    assert 'class="chip" href="/review"' in r.text


def test_today_hides_review_queue_badge_when_zero(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert 'class="chip" href="/review"' not in r.text


def test_today_shows_watchlist_badge_when_nonzero(client, db_session, cfg):
    db_session.add(Project(name="Out of Territory DC", category="data_center",
                           in_territory=False, status="active"))
    db_session.commit()
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    assert 'class="chip" href="/watchlist"' in r.text


# --- disclosures: closed by default, contractors reads first --------------------

def test_contractors_content_is_not_hidden_behind_open_prose(client, db_session, cfg):
    """The instruction's own example: on /contractors the reader should see
    contractors first. Confirms the methodology callouts are collapsed
    <details> (closed by default -- no [open] attribute) rather than the old
    always-expanded <div>."""
    r = client.get("/contractors", headers=AUTH)
    assert r.status_code == 200
    assert r.text.count("<details") >= 2
    assert "How this is built" in r.text
    # Closed by default: no bare `<details ... open` anywhere in the response.
    assert " open>" not in r.text and " open " not in r.text


def test_contractors_disclosure_preserves_every_word_of_the_original_callout(client, db_session, cfg):
    r = client.get("/contractors", headers=AUTH)
    assert "CSLB's own public license data, verbatim — nothing inferred" in r.text
    assert "cslb.ca.gov/onlineservices/dataportal" in r.text


def test_board_trustworthiness_callouts_stay_uncollapsed(client, db_session, cfg):
    """Board's callouts are live "is this safe to act on" warnings, not
    methodology -- they must stay plain, always-visible <div>s, not
    collapsed behind a disclosure."""
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200
    if "Zero pre-BOD projects" in r.text:
        idx = r.text.index("Zero pre-BOD projects")
        assert "<details" not in r.text[max(0, idx - 300):idx]


# --- stats hierarchy: two hero numbers, the rest demoted -------------------------

def test_board_stats_strip_has_exactly_two_hero_numbers(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    assert r.status_code == 200
    hero_start = r.text.index('class="kv-hero"')
    hero_section = r.text[hero_start:r.text.index('class="kv-rest', hero_start)]
    assert "Callable today" in hero_section
    assert "In BOD" in hero_section
    assert hero_section.count('class="k"') == 2


def test_board_demoted_stats_still_present_just_not_hero(client, db_session, cfg):
    seed(db_session, cfg)
    r = client.get("/board", headers=AUTH)
    rest_start = r.text.index('class="kv-rest')
    rest_section = r.text[rest_start:rest_start + 2000]
    for label in ("Name only", "Nobody named", "Median score", "Sized tonnage", "Pre-BOD", "Post-BOD"):
        assert label in rest_section
