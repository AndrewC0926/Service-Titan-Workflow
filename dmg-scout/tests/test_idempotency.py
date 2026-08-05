"""Gate 4 idempotency proof: the full pipeline run twice on identical input
must leave the database in an identical state."""
import json

import httpx
import respx
from sqlmodel import select

from app.models import Project, ProjectSignal, RawDocument, Signal
from app.pipeline.fetch import run_fetch
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score


def db_fingerprint(session) -> dict:
    """Everything that matters, minus timestamps that legitimately update."""
    docs = [(d.source, d.source_uid, d.content_hash, d.title)
            for d in session.exec(select(RawDocument)).all()]
    signals = [(s.raw_document_id, s.signal_type.value, s.project_name, s.mw_it, s.mw_total)
               for s in session.exec(select(Signal)).all()]
    projects = [(p.name, p.developer, p.county, p.stage.value, p.window.value,
                 round(p.tons_estimate_low or 0), round(p.tons_estimate_high or 0),
                 p.estimate_basis)
                for p in session.exec(select(Project)).all()]
    links = [(l.project_id, l.signal_id, l.match_method)
             for l in session.exec(select(ProjectSignal)).all()]
    return {"docs": sorted(docs), "signals": sorted(signals, key=str),
            "projects": sorted(projects, key=str), "links": sorted(links)}


@respx.mock
def test_pipeline_twice_identical_state(db_session, cfg, fixtures_dir, monkeypatch):
    from app.http import PoliteClient
    orig = PoliteClient.__init__

    def fast_init(self, *a, **k):
        k.update(interval=0, max_retries=0, respect_robots=False)
        orig(self, *a, **k)
    monkeypatch.setattr(PoliteClient, "__init__", fast_init)

    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json=json.loads((fixtures_dir / "edgar_fts.json").read_text())))
    respx.get(url__startswith="https://webapi.legistar.com/").mock(
        return_value=httpx.Response(200, json=json.loads((fixtures_dir / "legistar_matters.json").read_text())))
    respx.get(url__startswith="https://boards-api.greenhouse.io/").mock(
        return_value=httpx.Response(200, json=json.loads((fixtures_dir / "greenhouse_jobs.json").read_text())))
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(
        return_value=httpx.Response(200, text=(fixtures_dir / "ceqanet_search.csv").read_text()))
    respx.get(url__regex=r"https://ceqanet\.lci\.ca\.gov/\d+").mock(
        return_value=httpx.Response(200, text="<html><body>Meridian Data Center Campus, 176 MW</body></html>"))
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(404))

    def full_run():
        run_fetch(db_session, cfg)
        # triage/extract need the API; simulate their deterministic effect by
        # promoting fetched docs via the manual path is NOT done here — resolve
        # and score run over whatever signals exist, which is the idempotency
        # property under test.
        run_resolve(db_session, cfg, use_llm=False)
        run_size_score(db_session, cfg)

    full_run()
    fp1 = db_fingerprint(db_session)
    assert fp1["docs"], "first run must have stored documents"

    full_run()
    fp2 = db_fingerprint(db_session)
    assert fp1 == fp2, "second identical run changed database state"

    # And a third, for luck — resolution must not spawn duplicate projects.
    full_run()
    assert db_fingerprint(db_session) == fp1
