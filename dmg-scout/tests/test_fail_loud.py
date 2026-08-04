"""A total source outage must record ok=False, never 'ok, 0 records'."""
import httpx
import pytest
import respx
from sqlmodel import select

from app.http import PoliteClient
from app.models import SourceRun
from app.pipeline.fetch import run_fetch
from app.sources.base import SourceFailure
from app.sources.rss import RssAdapter


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


@respx.mock
def test_rss_total_outage_raises(cfg):
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(503))
    with pytest.raises(SourceFailure):
        list(RssAdapter().fetch(cfg, fast_client()))


@respx.mock
def test_total_outage_marks_run_failed(db_session, cfg, monkeypatch):
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(503))
    monkeypatch.setattr("app.pipeline.fetch.PoliteClient",
                        lambda: fast_client())
    runs = run_fetch(db_session, cfg, only_source="rss")
    assert runs["rss"].ok is False
    assert "RSS feeds failed" in runs["rss"].error
    stored = db_session.exec(select(SourceRun).where(SourceRun.source == "rss")).all()
    assert stored and stored[-1].ok is False


@respx.mock
def test_partial_outage_still_ok(db_session, cfg, fixtures_dir, monkeypatch):
    """One dead feed among live ones: run succeeds, warning logged."""
    rss_xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>New data center campus in Reno</title><link>https://x/a</link>
    <guid>a</guid><description>A 100 megawatt data center</description></item>
    </channel></rss>"""
    respx.get(url__startswith="https://www.datacenterdynamics.com/").mock(
        return_value=httpx.Response(200, text=rss_xml))
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(503))
    monkeypatch.setattr("app.pipeline.fetch.PoliteClient", lambda: fast_client())
    runs = run_fetch(db_session, cfg, only_source="rss")
    assert runs["rss"].ok is True
    assert runs["rss"].records_fetched == 1
