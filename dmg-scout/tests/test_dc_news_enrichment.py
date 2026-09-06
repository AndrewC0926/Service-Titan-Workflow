"""DCF/DCD RSS enrichment (Phase 6f) — ENRICHMENT ONLY. See
app/pipeline/dc_news_enrichment.py. This module has no code path to
Project() at all; every test here defends that constraint directly.

The LLM extraction call (dc_news_facts) is monkeypatched, same pattern as
test_grounding.py and test_fathom_outreach.py — these tests are about the
matching/attachment wiring, not the model."""
import httpx
import respx

import app.pipeline.dc_news_enrichment as dcn
from app.http import PoliteClient
from app.models import (
    ACTIVE_STATUSES, Category, Project, ProjectSignal, RawDocument, Signal, Stage, Window,
)
from app.pipeline.dc_news_enrichment import run_dc_news_enrichment


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


RSS_ITEM = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item>
  <title>{title}</title>
  <link>{link}</link>
  <description>{description}</description>
  <pubDate>Fri, 07 Aug 2026 12:00:00 GMT</pubDate>
</item>
</channel></rss>"""


def _mock_all_feeds(xml: str = "") -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title></channel></rss>'
    for name, url in dcn.FEEDS.items():
        respx.get(url).mock(return_value=httpx.Response(200, text=xml or empty))


def _project(db_session, name, developer, county="Los Angeles", state="CA"):
    p = Project(name=name, category=Category.data_center, developer=developer,
               county=county, state=state, status="active", in_territory=True,
               window=Window.PRE_BOD, stage=Stage.entitlement)
    db_session.add(p)
    db_session.commit()
    return p


@respx.mock
def test_matches_existing_project_by_developer_and_attaches_signal(db_session, cfg, monkeypatch):
    p = _project(db_session, "Vantage NV11", "Vantage Data Centers")
    xml = RSS_ITEM.format(
        title="Vantage Data Centers breaks ground on new Nevada campus",
        link="https://example.com/article-1",
        description="Vantage announced 200MW in Storey County, Nevada.")
    for name, url in dcn.FEEDS.items():
        respx.get(url).mock(return_value=httpx.Response(
            200, text=xml if name == "dcd" else RSS_ITEM.format(title="x", link="https://x/empty", description="")))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": "Storey County, Nevada",
        "mw": 200.0, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["matched"] >= 1 and stats["attached"] >= 1

    from sqlmodel import select
    link = db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p.id)).first()
    assert link is not None
    sig = db_session.get(Signal, link.signal_id)
    assert sig.developer_or_owner == "Vantage Data Centers"
    assert sig.mw_total == 200.0


@respx.mock
def test_attach_rescopes_a_rescore_in_the_same_run(db_session, cfg, monkeypatch):
    """RATCHET BUG fix: attaching a signal must not leave Project.score/
    window computed from the pre-attach facts until the next `scout
    pipeline` run -- this module has no other stage to catch it up, since
    it's a standalone CLI command, not one of that pipeline's own stages."""
    p = _project(db_session, "Vantage NV11", "Vantage Data Centers")
    before_score = p.score
    assert before_score == 0.0, "fixture project should start with no signals and no score"

    xml = RSS_ITEM.format(
        title="Vantage Data Centers breaks ground on new Nevada campus",
        link="https://example.com/article-rescore",
        description="Vantage announced 200MW in Storey County, Nevada.")
    for name, url in dcn.FEEDS.items():
        respx.get(url).mock(return_value=httpx.Response(
            200, text=xml if name == "dcd" else RSS_ITEM.format(title="x", link="https://x/empty2", description="")))
    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": "Storey County, Nevada",
        "mw": 200.0, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["attached"] >= 1  # dc_news_facts is mocked identically for every entry

    db_session.refresh(p)
    assert p.score > before_score, "score was not recomputed in this same run"
    assert p.window == Window.PRE_BOD, "window was not recomputed in this same run"


@respx.mock
def test_never_creates_a_project_on_no_match(db_session, cfg, monkeypatch):
    """No project named 'Totally Unknown Developer' exists -- the article
    must be discarded, not queued or turned into a new project."""
    _mock_all_feeds()
    xml = RSS_ITEM.format(
        title="Totally Unknown Developer announces huge campus",
        link="https://example.com/article-2",
        description="500MW somewhere.")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Totally Unknown Developer", "location_text": None,
        "mw": 500.0, "is_data_center_project": True})

    before = len(db_session.exec(
        __import__("sqlmodel").select(Project)).all())
    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    after = len(db_session.exec(__import__("sqlmodel").select(Project)).all())

    assert stats["matched"] == 0 and stats["attached"] == 0
    assert before == after, "an unmatched article must never create a project"


@respx.mock
def test_ambiguous_developer_with_two_projects_skipped_without_location(db_session, cfg, monkeypatch):
    p1 = _project(db_session, "Amazon DC 1", "Amazon Data Services", county="Los Angeles")
    p2 = _project(db_session, "Amazon DC 2", "Amazon Data Services", county="Riverside")
    _mock_all_feeds()
    xml = RSS_ITEM.format(title="Amazon Data Services expands", link="https://example.com/article-3",
                          description="No location stated.")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Amazon Data Services", "location_text": None,
        "mw": None, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["attached"] == 0, "ambiguous match with no disambiguating location must be skipped"

    from sqlmodel import select
    assert db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p1.id)).first() is None
    assert db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p2.id)).first() is None


@respx.mock
def test_ambiguous_developer_disambiguated_by_location(db_session, cfg, monkeypatch):
    p1 = _project(db_session, "Amazon DC 1", "Amazon Data Services", county="Los Angeles")
    _project(db_session, "Amazon DC 2", "Amazon Data Services", county="Riverside")
    _mock_all_feeds()
    xml = RSS_ITEM.format(title="Amazon Data Services expands", link="https://example.com/article-4",
                          description="New campus in Los Angeles County.")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Amazon Data Services", "location_text": "Los Angeles County",
        "mw": None, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["attached"] == 1

    from sqlmodel import select
    link = db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p1.id)).first()
    assert link is not None


@respx.mock
def test_non_project_article_is_not_extracted_into_a_signal(db_session, cfg, monkeypatch):
    _project(db_session, "Vantage NV11", "Vantage Data Centers")
    _mock_all_feeds()
    xml = RSS_ITEM.format(title="Vantage Data Centers hires new CFO", link="https://example.com/article-5",
                          description="Personnel announcement, no project.")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": None,
        "mw": None, "is_data_center_project": False})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["matched"] == 0 and stats["attached"] == 0


@respx.mock
def test_rerun_does_not_duplicate_the_signal(db_session, cfg, monkeypatch):
    p = _project(db_session, "Vantage NV11", "Vantage Data Centers")
    _mock_all_feeds()
    xml = RSS_ITEM.format(title="Vantage Data Centers breaks ground", link="https://example.com/article-6",
                          description="200MW campus.")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": None,
        "mw": 200.0, "is_data_center_project": True})

    run_dc_news_enrichment(db_session, cfg, fast_client())
    stats2 = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats2["attached"] == 0, "already-seen article URL must be skipped on re-run"

    from sqlmodel import select
    links = db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p.id)).all()
    assert len(links) == 1


@respx.mock
def test_same_article_in_two_dcf_sections_attaches_once(db_session, cfg, monkeypatch):
    """DCF's hyperscale and site-selection section feeds can legitimately
    both list the same underlying article -- must not double-attach."""
    p = _project(db_session, "Vantage NV11", "Vantage Data Centers")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(
        200, text='<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title></channel></rss>'))
    xml = RSS_ITEM.format(title="Vantage Data Centers breaks ground",
                          link="https://example.com/shared-article",
                          description="200MW campus.")
    respx.get(dcn.FEEDS["dcf_hyperscale"]).mock(return_value=httpx.Response(200, text=xml))
    respx.get(dcn.FEEDS["dcf_site_selection"]).mock(return_value=httpx.Response(200, text=xml))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": None,
        "mw": 200.0, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["attached"] == 1

    from sqlmodel import select
    links = db_session.exec(select(ProjectSignal).where(ProjectSignal.project_id == p.id)).all()
    assert len(links) == 1


@respx.mock
def test_one_feed_failing_does_not_block_the_others(db_session, cfg, monkeypatch):
    p = _project(db_session, "Vantage NV11", "Vantage Data Centers")
    respx.get(dcn.FEEDS["dcd"]).mock(return_value=httpx.Response(503))
    xml = RSS_ITEM.format(title="Vantage Data Centers breaks ground", link="https://example.com/article-7",
                          description="200MW campus.")
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title></channel></rss>'
    respx.get(dcn.FEEDS["dcf_hyperscale"]).mock(return_value=httpx.Response(200, text=xml))
    respx.get(dcn.FEEDS["dcf_site_selection"]).mock(return_value=httpx.Response(200, text=empty))

    monkeypatch.setattr(dcn, "dc_news_facts", lambda title, summary: {
        "developer": "Vantage Data Centers", "location_text": None,
        "mw": 200.0, "is_data_center_project": True})

    stats = run_dc_news_enrichment(db_session, cfg, fast_client())
    assert stats["feeds"]["dcd"]["errors"] == 1
    assert stats["attached"] == 1
