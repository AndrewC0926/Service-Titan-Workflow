"""Adapter tests against recorded payloads (respx mocks) — no live sites hit."""
import json

import httpx
import respx

from app.http import PoliteClient
from app.models import SignalType
from app.sources.ats import AtsAdapter
from app.sources.ceqanet import CeqanetAdapter
from app.sources.edgar import EdgarAdapter
from app.sources.legistar import LegistarAdapter
from app.sources.pucn import PucnAdapter


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


@respx.mock
def test_edgar_parses_hits(cfg, fixtures_dir):
    payload = json.loads((fixtures_dir / "edgar_fts.json").read_text())
    respx.get(url__startswith="https://efts.sec.gov/LATEST/search-index").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.get(url__startswith="https://www.sec.gov/Archives/").mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/html"},
                                    text="<html><body>" + ("48 MW campus. " * 500)
                                         + "</body></html>"))
    docs = list(EdgarAdapter().fetch(cfg, fast_client()))
    # 2 hits x N queries, but one row per accession -> deduped inside the adapter
    assert docs
    doc = docs[0]
    assert doc.source == "edgar"
    # source_uid is the accession, not accession:filename: one filing yields many
    # full-text hits (one per exhibit) and they are all the same filing.
    assert doc.source_uid == "0001193125-26-012345"
    assert "sec.gov/Archives/edgar/data/1710583" in doc.url
    # The filing body is fetched at fetch time, so triage sees the document
    # rather than ~200 chars of search-result metadata.
    assert doc.meta["needs_body_fetch"] is False
    assert doc.meta["body_chars"] > 1000
    assert "48 MW campus" in doc.raw_text
    assert doc.published_at.year == 2026
    abs_docs = [d for d in docs if d.default_signal_type == SignalType.abs_issuance]
    assert abs_docs, "424B2 should map to abs_issuance"


@respx.mock
def test_edgar_double_run_dedupes(db_session, cfg, fixtures_dir):
    """Gate 3c: same filing hit by many queries must dedupe. In the first demo,
    EDGAR reported 28 'new' for 2 unique filings because the matched query was
    embedded in raw_text and changed the content hash. Two full runs: run 1 new
    == unique filings, run 2 new == 0."""
    from app.pipeline.fetch import _store
    payload = json.loads((fixtures_dir / "edgar_fts.json").read_text())
    respx.get(url__startswith="https://efts.sec.gov/LATEST/search-index").mock(
        return_value=httpx.Response(200, json=payload)
    )
    docs1 = list(EdgarAdapter().fetch(cfg, fast_client()))
    new1 = sum(_store(db_session, d) for d in docs1)
    db_session.commit()
    assert new1 == 2, f"expected 2 unique filings, got {new1} 'new' from {len(docs1)} yields"

    docs2 = list(EdgarAdapter().fetch(cfg, fast_client()))
    new2 = sum(_store(db_session, d) for d in docs2)
    db_session.commit()
    assert new2 == 0, "second identical run must report zero new"


@respx.mock
def test_ceqanet_filters_and_parses(cfg, fixtures_dir):
    # Real endpoint is GET /Search?...&OutputFormat=CSV — /Search/DownloadCSV 404s.
    csv_text = (fixtures_dir / "ceqanet_search.csv").read_text()
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(
        return_value=httpx.Response(200, text=csv_text)
    )
    detail_html = "<html><body><h1>Meridian Data Center Campus</h1><p>APN 0110-111-22. 176 MW. Contact: Jane Doe (909) 555-0100</p></body></html>"
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/2026").mock(
        return_value=httpx.Response(200, text=detail_html)
    )
    docs = list(CeqanetAdapter().fetch(cfg, fast_client()))
    uids = {d.source_uid for d in docs}
    # The gate now passes BOTH boards' work. Data center rows:
    assert any(u.startswith("2026070456") for u in uids)
    # ...and industrial. "Serrano Logistics Center — warehouse distribution facility
    # on 45 acres" in Fontana is exactly the California industrial work that the
    # data-center-only gate was silently dropping at fetch, which is why the board
    # read NV 43 / CA 1.
    assert any(u.startswith("2026070123") for u in uids)
    # A housing element update is still neither, and must not get through.
    assert not any(u.startswith("2026070789") for u in uids)
    dc = next(d for d in docs if d.source_uid.startswith("2026070456"))
    assert dc.default_signal_type == SignalType.ceqa_nop
    assert dc.meta["sch_number"] == "2026070456"
    # Canonical URL comes from the CSV's own Document Portal URL column.
    assert dc.url == "https://ceqanet.lci.ca.gov/2026070456"
    # Rich CSV columns are carried into meta rather than re-scraped.
    assert dc.meta["acres"] == "88.5"
    assert dc.meta["parcel_number"] == "0110-111-22"
    assert dc.meta["contact_email"] == "jdoe@ontarioca.gov"
    # Contact appears in raw_text so extraction can place it on the ladder.
    assert "Jane Doe" in dc.raw_text and "(909) 555-0100" in dc.raw_text
    assert "APN 0110-111-22" in dc.raw_text  # detail page appended


@respx.mock
def test_ceqanet_accepts_document_types_beyond_the_old_allowlist(cfg, fixtures_dir):
    """The old NOP/EIR/NEG/MND/NOD allowlist discarded NOEs, which is where most
    real data center filings actually showed up in a 12-month live sample."""
    csv_text = (fixtures_dir / "ceqanet_search.csv").read_text()
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(
        return_value=httpx.Response(200, text=csv_text)
    )
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/2026").mock(
        return_value=httpx.Response(200, text="<html><body>detail</body></html>")
    )
    docs = list(CeqanetAdapter().fetch(cfg, fast_client()))
    noe = [d for d in docs if d.meta["document_type"] == "NOE"]
    assert noe, "NOE colocation filing must not be filtered out"


@respx.mock
def test_ceqanet_decodes_cp1252(cfg):
    """CEQAnet serves cp1252 with no charset header; en-dashes must not explode."""
    # U+2013 EN DASH encodes to the single byte 0x96 in cp1252, which is exactly
    # the byte that makes a naive UTF-8 decode of a live response blow up.
    header = "SCH Number,Document Title,Project Title,Document Type,Received,Document Description,Document Portal URL\n"
    body = ("2026070001,Data Center – Phase II,Data Center – Phase II,NOP,"
            "7/1/2026,A 90 MW data center,https://ceqanet.lci.ca.gov/2026070001\n")
    assert (header + body).encode("cp1252").find(b"\x96") > 0
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/Search").mock(
        return_value=httpx.Response(200, content=(header + body).encode("cp1252"))
    )
    respx.get(url__startswith="https://ceqanet.lci.ca.gov/2026").mock(
        return_value=httpx.Response(200, text="<html><body>detail</body></html>")
    )
    docs = list(CeqanetAdapter().fetch(cfg, fast_client()))
    assert docs, "cp1252 row must decode and pass the keyword filter"
    assert "–" in docs[0].title, "en-dash should round-trip, not become U+FFFD"


@respx.mock
def test_ats_greenhouse_geo_filter(cfg, fixtures_dir):
    payload = json.loads((fixtures_dir / "greenhouse_jobs.json").read_text())
    respx.get(url__startswith="https://boards-api.greenhouse.io/").mock(
        return_value=httpx.Response(200, json=payload)
    )
    respx.get(url__startswith="https://api.lever.co/").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://api.ashbyhq.com/").mock(return_value=httpx.Response(404))
    # Workday boards are POST-only; leaving them unmocked would fail a majority
    # of boards and (correctly) raise SourceFailure.
    respx.post(url__regex=r"https://.*\.myworkdayjobs\.com/.*").mock(
        return_value=httpx.Response(200, json={"total": 0, "jobPostings": []})
    )
    docs = list(AtsAdapter().fetch(cfg, fast_client()))
    assert docs, "Reno posting should pass geo filter"
    titles = " ".join(d.title for d in docs)
    assert "Construction Manager" in titles
    assert "Accountant" not in titles, "Denver posting must be filtered out"
    assert all(d.default_signal_type == SignalType.job_posting for d in docs)


@respx.mock
def test_legistar_keyword_match(cfg, fixtures_dir):
    payload = json.loads((fixtures_dir / "legistar_matters.json").read_text())
    respx.get(url__startswith="https://webapi.legistar.com/v1/").mock(
        return_value=httpx.Response(200, json=payload)
    )
    docs = list(LegistarAdapter().fetch(cfg, fast_client()))
    assert docs
    assert all("data center" in d.raw_text.lower() for d in docs)
    assert not any("Sidewalk" in d.title for d in docs)
    assert docs[0].default_signal_type == SignalType.planning_agenda
    assert docs[0].source_uid.endswith(":55501")


@respx.mock
def test_ceqanet_keyword_lists_stay_separate(cfg, fixtures_dir):
    """The two lists must remain distinguishable, or the boards blur. The fetch gate
    only asks "worth storing"; which board a document lands on is triage's call, but
    a probe has to be able to tell why a row was kept."""
    from app.sources.base import keyword_category, keyword_match

    assert keyword_category("Meridian Data Center Campus", cfg) == "data_center"
    assert keyword_category("Serrano Logistics Center: warehouse", cfg) == "industrial"
    assert keyword_category("Rialto Housing Element Update", cfg) is None
    # A filing naming both is a data center that mentions its warehouse.
    assert keyword_category("data center with warehouse space", cfg) == "data_center"
    # And the gate itself accepts either.
    assert keyword_match("new distribution center", cfg)
    assert keyword_match("hyperscale campus", cfg)
    assert not keyword_match("sidewalk repair project", cfg)


@respx.mock
def test_pucn_flat_cell_parsing(cfg, fixtures_dir):
    """PUCN's GridView doesn't put one docket per <tr> — the real page nests
    several layout <table>s around a single flat run of <td> cells, four per
    docket (Dkt No., Date Filed, Description, "View"). Naive <tr>-based
    traversal cross-reads into the nested tables (one row resolved 35,000+ <td>
    descendants live); the adapter has to flat-chunk the biggest table's cells
    instead. This fixture reproduces that nesting."""
    from datetime import datetime
    html = (fixtures_dir / "pucn_dktinfo.html").read_text()
    respx.get(url__startswith="https://pucweb1.state.nv.us/PUC2/Dktinfo.aspx").mock(
        return_value=httpx.Response(200, text=html)
    )
    docs = list(PucnAdapter().fetch(cfg, fast_client(), since=datetime(2026, 1, 1)))
    uids = {d.source_uid for d in docs}
    # Two LLESA dockets inside the lookback window, named counterparties intact.
    assert uids == {"26-05028", "26-05026"}
    vantage = next(d for d in docs if d.source_uid == "26-05028")
    assert "Vantage Data Centers NV11, LLC" in vantage.raw_text
    assert vantage.default_signal_type == SignalType.utility_load_request
    amazon = next(d for d in docs if d.source_uid == "26-05026")
    assert "Amazon Data Services, Inc." in amazon.raw_text
    # A non-LLESA docket (Copia's UEPA notice, the rate case) must not pass the
    # keyword gate even though it's inside the window.
    assert "26-01035" not in uids and "25-11002" not in uids
    # An LLESA docket outside the lookback window (2024) must be excluded by date.
    assert "24-03017" not in uids


def test_edgar_lookback_covers_the_deal_cadence(cfg):
    """EDGAR is episodic, and a window shorter than its cadence looks like a fault.

    Measured live on 2026-08-07 over the same 28-query plan: a 14-day window
    returned 16 page-0 hits and 2 filings past the issuer gate; 90 days returned
    105 and 10. The daily fetch on 14 days stored ONE document, which is
    indistinguishable from a broken adapter — and the fix is the window, never a
    looser query. Precision here comes from issuer_patterns; loosening that is
    what produced the first backfill's 3,016 rows of chip-company boilerplate.
    """
    src = cfg.source("edgar")
    assert src["lookback_days"] >= 90, (
        "EDGAR yields ~3-4 relevant filings a month across the whole issuer list; "
        "a shorter window cannot tell a quiet quarter from a dead adapter")
    # The precision controls that must NOT be relaxed to compensate.
    assert src["issuer_patterns"], "issuer_patterns is the precision control"
    assert src["min_doc_chars"] >= 2000, "stub floor keeps search-result stubs out"
