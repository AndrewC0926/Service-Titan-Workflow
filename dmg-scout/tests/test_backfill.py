"""Backfill: chunking, checkpoint resume, crash-safety, cost estimation."""
import httpx
import pytest
import respx
from sqlmodel import select

from app.models import BackfillCheckpoint, RawDocument
from app.pipeline.backfill import estimate_cost, run_backfill
from app.sources.ceqanet import CeqanetAdapter
from app.sources.edgar import EdgarAdapter
from datetime import datetime


def test_ceqanet_chunks_are_county_doctype_pairs(cfg):
    chunks = CeqanetAdapter().backfill_chunks(cfg, datetime(2024, 8, 1))
    keys = [c["key"] for c in chunks]
    assert "2024-08-01:Riverside:NOP" in keys
    assert len(chunks) == 7 * 5  # 7 counties x 5 document types


def test_edgar_chunks_are_months(cfg):
    chunks = EdgarAdapter().backfill_chunks(cfg, datetime(2024, 8, 15))
    keys = [c["key"] for c in chunks]
    assert keys[0] == "2024-08-15:month:2024-08"
    assert chunks[0]["startdt"] == "2024-08-15"  # partial first month honored
    assert len(chunks) >= 24


@pytest.fixture()
def fast_client(monkeypatch):
    from app.http import PoliteClient
    orig = PoliteClient.__init__

    def fast_init(self, *a, **k):
        k.update(interval=0, max_retries=0, respect_robots=False)
        orig(self, *a, **k)
    monkeypatch.setattr(PoliteClient, "__init__", fast_init)


@respx.mock
def test_backfill_checkpoints_and_resumes(db_session, cfg, fixtures_dir, fast_client):
    csv_text = (fixtures_dir / "ceqanet_search.csv").read_text()
    csv_route = respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search/DownloadCSV").mock(
        return_value=httpx.Response(200, text=csv_text))
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/2026").mock(
        return_value=httpx.Response(200, text="<html><body>detail</body></html>"))

    since = datetime(2024, 8, 1)
    totals = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals["chunks_run"] == 35
    assert totals["chunks_skipped"] == 0
    cps = db_session.exec(select(BackfillCheckpoint)).all()
    assert len(cps) == 35
    first_calls = csv_route.call_count

    # Resume: every chunk checkpointed -> zero requests, zero chunks run
    totals2 = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals2["chunks_run"] == 0
    assert totals2["chunks_skipped"] == 35
    assert csv_route.call_count == first_calls

    # Reset refetches
    totals3 = run_backfill(db_session, cfg, "ceqanet", since, reset=True)
    assert totals3["chunks_run"] == 35
    assert totals3["new"] == 0  # all content already stored -> dedupe holds


@respx.mock
def test_failed_chunk_not_checkpointed(db_session, cfg, fast_client):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text="SCH Number,Title\n")

    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search/DownloadCSV").mock(side_effect=flaky)
    since = datetime(2024, 8, 1)
    totals = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals["chunk_errors"] == 3
    cps = db_session.exec(select(BackfillCheckpoint)).all()
    assert len(cps) == 32  # failed chunks are NOT checkpointed

    # Next run retries exactly the 3 failed chunks
    totals2 = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals2["chunks_run"] == 3 and totals2["chunks_skipped"] == 32
    assert len(db_session.exec(select(BackfillCheckpoint)).all()) == 35


def test_estimate_paper_mode(db_session, cfg):
    est = estimate_cost(db_session, cfg, assumed_docs={"ceqanet": 600, "goed": 200, "edgar": 400})
    assert est["docs_to_triage"] == 1200
    assert est["total_cost_usd"] > 0
    assert "paper estimate" in est["basis"]


def test_estimate_measured_mode(db_session, cfg):
    db_session.add(RawDocument(source="t", source_uid="1", url="u", title="t",
                               content_hash="h", raw_text="x" * 8000))
    db_session.commit()
    est = estimate_cost(db_session, cfg)
    assert "measured from 1 pending" in est["basis"]
    assert est["docs_to_triage"] == 1
