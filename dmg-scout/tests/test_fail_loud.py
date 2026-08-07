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
                        lambda **kw: fast_client())
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
    monkeypatch.setattr("app.pipeline.fetch.PoliteClient", lambda **kw: fast_client())
    runs = run_fetch(db_session, cfg, only_source="rss")
    assert runs["rss"].ok is True
    assert runs["rss"].records_fetched == 1


# ---- the fetch summary must survive its own session closing -----------------

def test_fetch_cli_prints_its_summary_after_the_session_closes(db_session, monkeypatch):
    """`scout fetch` read run.ok after session_scope() committed and expired it.

    Every source had already fetched and committed successfully; the command then
    raised DetachedInstanceError while printing the summary. The traceback is not
    the damage — `scout pipeline` catches per-step exceptions and counts them, so
    a completely successful fetch was reported as a failed pipeline step on every
    nightly cron run, which is precisely the signal Phase D relies on.
    """
    from typer.testing import CliRunner

    from app.cli import app as cli_app

    def _stub_run_fetch(session, cfg, only_source=None):
        ok = SourceRun(source="rss", ok=True, records_fetched=7, records_new=3)
        bad = SourceRun(source="goed", ok=False, records_fetched=0, records_new=0,
                        error="HTTPError: 503\nsecond line ignored")
        empty = SourceRun(source="ats", ok=False)   # ok False, error never set
        for r in (ok, bad, empty):
            session.add(r)
        session.commit()
        return {"rss": ok, "goed": bad, "ats": empty}

    monkeypatch.setattr("app.pipeline.fetch.run_fetch", _stub_run_fetch)
    result = CliRunner().invoke(cli_app, ["fetch"])
    assert result.exit_code == 0, result.output
    assert "rss: fetched=7 new=3 ok" in result.output
    assert "goed: fetched=0 new=0 FAILED: HTTPError: 503" in result.output
    # an ok=False run with no error recorded must not IndexError on splitlines()[0]
    assert "ats: fetched=0 new=0 FAILED: no error recorded" in result.output


# ---- an LLM stage that got nothing done must not exit 0 ---------------------

@pytest.mark.parametrize("stage,stats,expect_exit", [
    # the observed failure: 80 pending documents, 80 errors, exit 0
    ("triage", {"data_center": 0, "industrial": 0, "irrelevant": 0, "error": 80,
                "skipped": 0, "irrelevant_partial_read": 0}, 1),
    # some errors among real work is normal and stays quiet here
    ("triage", {"data_center": 3, "industrial": 5, "irrelevant": 40, "error": 2,
                "skipped": 0, "irrelevant_partial_read": 0}, 0),
    # a clean run over an empty queue is not a failure
    ("triage", {"data_center": 0, "industrial": 0, "irrelevant": 0, "error": 0,
                "skipped": 0, "irrelevant_partial_read": 0}, 0),
    # skipped documents are not "work done" — all-errors-plus-skips still fails
    ("triage", {"data_center": 0, "irrelevant": 0, "error": 12, "skipped": 5}, 1),
])
def test_stage_exits_nonzero_only_when_it_did_nothing(stage, stats, expect_exit):
    """`scout pipeline` catches per-step exceptions and counts them, so a stage
    that silently exits 0 having done nothing marches the whole cron onward and
    reports a green night on which no work happened."""
    import typer

    from app.cli import _fail_if_all_errored

    key = "error" if stage == "triage" else "errors"
    if expect_exit:
        with pytest.raises(typer.Exit) as exc:
            _fail_if_all_errored(stage, stats, key)
        assert exc.value.exit_code == 1
    else:
        _fail_if_all_errored(stage, stats, key)


def test_triage_cli_exits_nonzero_when_every_document_errors(db_session, monkeypatch):
    from typer.testing import CliRunner

    from app.cli import app as cli_app

    monkeypatch.setattr(
        "app.pipeline.triage.run_triage",
        lambda session, cfg, limit=200: {"data_center": 0, "industrial": 0,
                                         "irrelevant": 0, "error": 80, "skipped": 0,
                                         "irrelevant_partial_read": 0})
    result = CliRunner().invoke(cli_app, ["triage"])
    assert result.exit_code == 1
    assert "did no work" in result.output
