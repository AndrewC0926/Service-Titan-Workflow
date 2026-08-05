"""Backfill: chunking, checkpoint resume, crash-safety, cost estimation."""
import httpx
import pytest
import respx
from sqlmodel import select

from app.models import BackfillCheckpoint, RawDocument, utcnow
from app.pipeline.backfill import estimate_cost, run_backfill
from app.sources.ceqanet import CeqanetAdapter
from app.sources.edgar import EdgarAdapter
from datetime import datetime, timedelta


def test_ceqanet_chunks_are_county_months(cfg):
    """One CSV request per county per month. Not per county x document type:
    that shape cost 5x the requests and tripped CEQAnet's 403 throttle, and the
    CSV export returns every type in one call anyway."""
    chunks = CeqanetAdapter().backfill_chunks(cfg, datetime(2024, 8, 1))
    keys = [c["key"] for c in chunks]
    assert "Riverside:2024-08" in keys
    counties = cfg.source("ceqanet")["counties"]
    months = {k.split(":")[1] for k in keys}
    assert len(chunks) == len(counties) * len(months)
    assert "2024-08" in months and len(months) >= 24
    first = next(c for c in chunks if c["key"] == "Riverside:2024-08")
    assert first["start"].startswith("2024-08-01")   # partial first month honored
    assert first["end"].startswith("2024-08-31")


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
    csv_route = respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(
        return_value=httpx.Response(200, text=csv_text))
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/2026").mock(
        return_value=httpx.Response(200, text="<html><body>detail</body></html>"))

    # Short window keeps the chunk count small; the exact count is derived from
    # the adapter so this does not drift as the calendar moves.
    since = utcnow().replace(day=1) - timedelta(days=40)
    n_chunks = len(CeqanetAdapter().backfill_chunks(cfg, since))
    assert n_chunks > 0

    totals = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals["chunks_run"] == n_chunks
    assert totals["chunks_skipped"] == 0
    cps = db_session.exec(select(BackfillCheckpoint)).all()
    assert len(cps) == n_chunks
    first_calls = csv_route.call_count

    # Resume: every chunk checkpointed -> zero requests, zero chunks run
    totals2 = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals2["chunks_run"] == 0
    assert totals2["chunks_skipped"] == n_chunks
    assert csv_route.call_count == first_calls

    # Reset refetches
    totals3 = run_backfill(db_session, cfg, "ceqanet", since, reset=True)
    assert totals3["chunks_run"] == n_chunks
    assert totals3["new"] == 0  # all content already stored -> dedupe holds


@respx.mock
def test_failed_chunk_not_checkpointed(db_session, cfg, fast_client):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text="SCH Number,Title\n")

    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(side_effect=flaky)
    since = utcnow().replace(day=1) - timedelta(days=40)
    n_chunks = len(CeqanetAdapter().backfill_chunks(cfg, since))
    assert n_chunks > 3

    totals = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals["chunk_errors"] == 3
    cps = db_session.exec(select(BackfillCheckpoint)).all()
    assert len(cps) == n_chunks - 3  # failed chunks are NOT checkpointed

    # Next run retries exactly the 3 failed chunks
    totals2 = run_backfill(db_session, cfg, "ceqanet", since)
    assert totals2["chunks_run"] == 3 and totals2["chunks_skipped"] == n_chunks - 3
    assert len(db_session.exec(select(BackfillCheckpoint)).all()) == n_chunks


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
