"""EDGAR narrowing + body fetch, CivicPlus agendas, NUL scrubbing, stub detection.

Every test here pins a defect found in the first real backfill:
  * EDGAR stored 3,016 search-result stubs averaging 202 chars and never fetched
    the filings, and its query matched any 10-K whose risk factors said
    "data center".
  * GOED's backfill stored nothing because pdfplumber returned NUL bytes and
    Postgres rejected the insert — while the adapter itself looked fine.
  * Storey County (Tahoe Reno Industrial Center) had no adapter at all.
  * `--source` was a single string, so passing it three times ran only the last.
"""
import httpx
import pytest
import respx
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import scrub
from app.sources.civicplus import CivicPlusAdapter
from app.sources.edgar import EdgarAdapter, filing_to_text


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def fts_hit(cik, accession, filename, form, names, date="2026-06-01"):
    return {
        "_id": f"{accession}:{filename}",
        "_source": {"ciks": [cik], "display_names": names,
                    "root_forms": [form], "file_date": date},
    }


# ---- EDGAR: the filer gate -------------------------------------------------

@respx.mock
def test_edgar_keeps_issuer_filings_and_drops_passing_mentions(cfg):
    """The precision gate is WHO FILED, not what the text says. A JPMorgan 424B2
    that mentions STACK is noise; a Vantage ABS-15G is signal."""
    keep = fts_hit("1727983", "0001-25-000001", "d1.htm", "ABS-15G",
                   ["Vantage Data Centers Holdings, LLC  (CIK 0001727983)"])
    drop = fts_hit("19617", "0002-25-000002", "d2.htm", "424B2",
                   ["JPMORGAN CHASE & CO  (JPM)  (CIK 0000019617)"])
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [keep, drop]}}))
    respx.get(url__startswith="https://www.sec.gov/Archives/").mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/html"},
            text="<html><body>" + ("Data center campus, 48 MW critical load. " * 200)
                 + "</body></html>"))

    docs = list(EdgarAdapter().fetch(cfg, fast_client()))
    filers = " ".join(" ".join(d.meta["filer_names"]) for d in docs)
    assert "Vantage" in filers
    assert "JPMORGAN" not in filers


@respx.mock
def test_edgar_drops_the_companies_that_polluted_the_first_backfill(cfg):
    """Navitas, Ambarella and Braze were the actual sample rows the user found."""
    hits = [
        fts_hit("1821769", "0003-25-1", "a.htm", "10-K",
                ["Navitas Semiconductor Corp  (NVTS)  (CIK 0001821769)"]),
        fts_hit("1280263", "0004-25-1", "b.htm", "10-K",
                ["Ambarella, Inc.  (AMBA)  (CIK 0001280263)"]),
        fts_hit("1676238", "0005-25-1", "c.htm", "10-K",
                ["Braze, Inc.  (BRZE)  (CIK 0001676238)"]),
        fts_hit("1144879", "0006-25-1", "d.htm", "8-K",
                ["Applied Digital Corp.  (APLD)  (CIK 0001144879)"]),
    ]
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": hits}}))
    respx.get(url__startswith="https://www.sec.gov/").mock(
        return_value=httpx.Response(200, text="body"))
    assert list(EdgarAdapter().fetch(cfg, fast_client())) == []


# ---- EDGAR: the body fetch ------------------------------------------------

@respx.mock
def test_edgar_fetches_the_filing_body_not_just_the_stub(cfg):
    body = "Portfolio\n" + ("The Reno campus delivers 32 MW to a single tenant. " * 400)
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [
            fts_hit("1727983", "0001-25-1", "deal.htm", "ABS-15G",
                    ["Vantage Data Centers Holdings, LLC"])]}}))
    route = respx.get(url__startswith="https://www.sec.gov/Archives/").mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/html"},
                                    text=f"<html><body>{body}</body></html>"))

    doc = next(iter(EdgarAdapter().fetch(cfg, fast_client())))
    assert route.called, "the stored document URL must actually be followed"
    assert "Reno campus delivers 32 MW" in doc.raw_text
    assert doc.meta["body_chars"] > 5000
    assert doc.meta["needs_body_fetch"] is False
    assert len(doc.raw_text) > 500, "202-char stub rows are the bug being fixed"
    assert doc.default_signal_type == SignalType.abs_issuance


@respx.mock
def test_edgar_flags_the_row_when_the_body_cannot_be_fetched(cfg):
    """A failed body fetch must stay visible for the extract-stage retry rather
    than silently becoming a stub."""
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [
            fts_hit("1727983", "0001-25-1", "deal.htm", "ABS-15G",
                    ["Vantage Data Centers Holdings, LLC"])]}}))
    respx.get(url__startswith="https://www.sec.gov/").mock(
        return_value=httpx.Response(404))
    doc = next(iter(EdgarAdapter().fetch(cfg, fast_client())))
    assert doc.meta["needs_body_fetch"] is True
    assert doc.meta["body_chars"] == 0
    assert "body_error" in doc.meta


@respx.mock
def test_edgar_dedupes_documents_of_one_filing(cfg):
    """Full-text search returns a hit per document; one filing is one row."""
    same = [fts_hit("1727983", "0001-25-1", f"ex{i}.htm", "ABS-15G",
                    ["Vantage Data Centers Holdings, LLC"]) for i in range(6)]
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": same}}))
    respx.get(url__startswith="https://www.sec.gov/").mock(
        return_value=httpx.Response(200, text="x" * 6000))
    assert len(list(EdgarAdapter().fetch(cfg, fast_client()))) == 1


@respx.mock
def test_edgar_verify_fails_on_stub_length(cfg):
    """verify must not report OK for metadata-only rows — the original defect."""
    respx.get(url__startswith="https://efts.sec.gov/").mock(
        return_value=httpx.Response(200, json={"hits": {"hits": [
            fts_hit("1727983", "0001-25-1", "deal.htm", "ABS-15G",
                    ["Vantage Data Centers Holdings, LLC"])]}}))
    respx.get(url__startswith="https://www.sec.gov/").mock(
        return_value=httpx.Response(404))   # no body -> stub
    result = EdgarAdapter().verify(cfg, fast_client())
    assert result["status"] == "fail"
    assert "stub" in result["detail"].lower()


def test_filing_to_text_handles_html_and_plaintext():
    assert "Hello" in filing_to_text(b"<html><body><p>Hello</p></body></html>", "text/html")
    assert "PLAIN TEXT FILING" in filing_to_text(b"PLAIN TEXT FILING\n\n\n\nmore", "text/plain")
    # latin-1 bytes must not raise
    assert filing_to_text(b"caf\xe9 data center", "text/plain")


# ---- NUL scrubbing --------------------------------------------------------

def test_scrub_strips_nul_bytes():
    """psycopg2 rejects NUL in a text literal; pdfplumber emits them. This killed
    the whole GOED backfill chunk while the adapter reported success."""
    assert scrub("a\x00b") == "ab"
    assert scrub("") == ""


def test_fetched_doc_scrubs_nul_before_hashing():
    from app.sources.base import FetchedDoc
    clean = FetchedDoc(source="s", source_uid="u", url="x", title="t\x00", raw_text="a\x00b")
    assert "\x00" not in clean.raw_text and "\x00" not in clean.title
    # Hash is computed on the scrubbed text, so dedupe is stable either way.
    assert clean.content_hash == FetchedDoc(
        source="s", source_uid="u", url="x", title="t", raw_text="ab").content_hash


# ---- CivicPlus / Storey County --------------------------------------------

CATEGORY_HTML = """
<table id="table4"><tbody>
<tr id="row1392" class="catAgendaRow"><td><h3>
  <a id="_06182026-1392" name="_06182026-1392"></a>
  <strong aria-label="Agenda for June 18, 2026">Jun 18, 2026</strong></h3>
  <p><a href="/AgendaCenter/ViewFile/Agenda/_06182026-1392" target="_blank">
  Planning Commission Meeting</a></p></td>
  <td class="minutes"><a href="/AgendaCenter/ViewFile/Minutes/_06182026-1392">Minutes</a></td></tr>
<tr id="row1371" class="catAgendaRow"><td><h3>
  <strong aria-label="Agenda for February 5, 2026">Feb 5, 2026</strong></h3>
  <p><a href="/AgendaCenter/ViewFile/Agenda/_02052026-1371">Planning Commission Agenda</a></p>
  </td></tr>
</tbody></table>
"""


def _civicplus_cfg(**overrides) -> Config:
    data = {
        "keywords": {"data_center": ["data center", "hyperscale"]},
        "sources": {"civicplus": {
            "enabled": True, "lookback_days": 3650, "max_pdf_pages": 5,
            "document_kinds": ["Agenda"],
            "jurisdictions": [{
                "name": "Storey County, NV", "base_url": "https://storeycounty.org",
                "county": "Storey", "state": "NV",
                "categories": [{"id": 4, "label": "Planning Commission"}],
            }],
            **overrides}},
    }
    return Config(data)


def test_civicplus_parses_rows_and_respects_document_kinds():
    adapter = CivicPlusAdapter()
    rows = adapter._rows(CATEGORY_HTML, {"agenda"})
    assert [r["docid"] for r in rows] == ["1392", "1371"]
    assert rows[0]["date"].strftime("%Y-%m-%d") == "2026-06-18"
    assert rows[0]["title"] == "Planning Commission Meeting"
    assert all(r["kind"].lower() == "agenda" for r in rows), "Minutes excluded by config"
    # Each row lists its link twice in the real markup; must not double-count.
    assert len({r["uid"] for r in rows}) == len(rows)

    both = adapter._rows(CATEGORY_HTML, {"agenda", "minutes"})
    assert {r["kind"].lower() for r in both} == {"agenda", "minutes"}


@respx.mock
def test_civicplus_fetches_and_keyword_filters_packets(monkeypatch):
    cfg = _civicplus_cfg()
    respx.post(url__startswith="https://storeycounty.org/AgendaCenter/UpdateCategoryList").mock(
        return_value=httpx.Response(200, text=CATEGORY_HTML))
    respx.get(url__startswith="https://storeycounty.org/AgendaCenter/ViewFile/").mock(
        return_value=httpx.Response(200, content=b"%PDF-fake"))

    pages = {
        "/AgendaCenter/ViewFile/Agenda/_06182026-1392":
            "Vantage Data Centers Management Company, LLC - 1121 USA Parkway, McCarran NV",
        "/AgendaCenter/ViewFile/Agenda/_02052026-1371":
            "Consideration of a parcel map and road vacation. No technology uses.",
    }
    calls = []

    def fake_pdf(data, max_pages=40):
        return pages[calls.pop(0)]

    real_get_bytes = PoliteClient.get_bytes

    def tracking_get_bytes(self, url, **kw):
        calls.append(url.replace("https://storeycounty.org", ""))
        return real_get_bytes(self, url, **kw)

    monkeypatch.setattr(PoliteClient, "get_bytes", tracking_get_bytes)
    monkeypatch.setattr("app.sources.civicplus.pdf_to_text", fake_pdf)

    docs = list(CivicPlusAdapter().fetch(cfg, fast_client()))
    assert len(docs) == 1, "only the data-center agenda should be kept"
    doc = docs[0]
    assert "Vantage Data Centers" in doc.raw_text
    assert doc.source_uid == "storeycounty.org:Agenda/_06182026-1392"
    assert doc.meta["county"] == "Storey" and doc.meta["state"] == "NV"
    assert doc.meta["body"] == "Planning Commission"
    assert doc.default_signal_type == SignalType.planning_agenda
    assert doc.published_at.strftime("%Y-%m-%d") == "2026-06-18"


@respx.mock
def test_civicplus_skips_scanned_pdfs_without_pretending_they_are_empty(monkeypatch, caplog):
    """Storey County's quorum-notice category is entirely image-only scans. Those
    must be logged, not silently treated as 'no data centers mentioned'."""
    cfg = _civicplus_cfg()
    respx.post(url__startswith="https://storeycounty.org/AgendaCenter/UpdateCategoryList").mock(
        return_value=httpx.Response(200, text=CATEGORY_HTML))
    respx.get(url__startswith="https://storeycounty.org/AgendaCenter/ViewFile/").mock(
        return_value=httpx.Response(200, content=b"%PDF-scan"))
    monkeypatch.setattr("app.sources.civicplus.pdf_to_text", lambda d, max_pages=40: "")

    with caplog.at_level("WARNING"):
        docs = list(CivicPlusAdapter().fetch(cfg, fast_client()))
    assert docs == []
    assert any("no extractable text layer" in r.message for r in caplog.records)


def test_civicplus_backfill_chunks_are_jurisdiction_category_year():
    from datetime import datetime
    chunks = CivicPlusAdapter().backfill_chunks(_civicplus_cfg(), datetime(2024, 1, 1))
    keys = [c["key"] for c in chunks]
    assert "Storey County, NV:4:2024" in keys
    assert len(chunks) == len(range(2024, datetime.utcnow().year + 1))


# ---- CLI: repeatable --source ---------------------------------------------

def test_backfill_source_option_is_repeatable(monkeypatch):
    """`--source a --source b --source c` used to silently run only c."""
    ran = []
    monkeypatch.setattr("app.pipeline.backfill.run_backfill",
                        lambda session, cfg, name, since, reset=False:
                            (ran.append(name) or {"chunks_run": 0}))
    result = CliRunner().invoke(cli_app, [
        "backfill", "--source", "ceqanet", "--source", "goed", "--source", "edgar",
        "--since", "2024-08-01"])
    assert result.exit_code == 0, result.output
    assert ran == ["ceqanet", "goed", "edgar"]


def test_backfill_source_accepts_comma_separated_and_dedupes(monkeypatch):
    ran = []
    monkeypatch.setattr("app.pipeline.backfill.run_backfill",
                        lambda session, cfg, name, since, reset=False:
                            (ran.append(name) or {"chunks_run": 0}))
    result = CliRunner().invoke(cli_app, [
        "backfill", "--source", "ceqanet,goed", "--source", "ceqanet",
        "--since", "2024-08-01"])
    assert result.exit_code == 0, result.output
    assert ran == ["ceqanet", "goed"]


def test_backfill_rejects_unknown_source_before_fetching(monkeypatch):
    ran = []
    monkeypatch.setattr("app.pipeline.backfill.run_backfill",
                        lambda *a, **k: ran.append(a[2]))
    result = CliRunner().invoke(cli_app, [
        "backfill", "--source", "ceqanet", "--source", "bogus", "--since", "2024-08-01"])
    assert result.exit_code != 0
    assert ran == [], "nothing may run if any name is invalid"


# ---- stub detection in the base verify ------------------------------------

def test_base_verify_fails_on_short_documents(cfg, monkeypatch):
    from app.sources.base import FetchedDoc, SourceAdapter

    class StubbyAdapter(SourceAdapter):
        name = "stubby"

        def fetch(self, cfg, client, since=None):
            yield FetchedDoc(source="stubby", source_uid="1", url="u",
                             title="t", raw_text="Form: 8-K\nFiled: 2026-01-01")

    monkeypatch.setattr(StubbyAdapter, "min_doc_chars", lambda self, cfg: 500)
    out = StubbyAdapter().verify(cfg, fast_client())
    assert out["status"] == "fail" and "stub" in out["detail"].lower()


def test_base_verify_ok_on_real_documents(cfg, monkeypatch):
    from app.sources.base import FetchedDoc, SourceAdapter

    class RichAdapter(SourceAdapter):
        name = "richy"

        def fetch(self, cfg, client, since=None):
            yield FetchedDoc(source="richy", source_uid="1", url="u",
                             title="t", raw_text="x" * 5000)

    monkeypatch.setattr(RichAdapter, "min_doc_chars", lambda self, cfg: 500)
    assert RichAdapter().verify(cfg, fast_client())["status"] == "ok"
