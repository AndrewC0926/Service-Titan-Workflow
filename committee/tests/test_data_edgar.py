from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from committee.data.cli import app as data_cli
from committee.data.edgar import parse_acceptance, parse_filing_arrays
from committee.data.form4 import Form4ParseError, parse_form4
from committee.data.http import FixtureFetcher
from committee.data.ingest import lake_for, run_check, run_edgar, run_securities
from committee.data.pit import PIT
from committee.data.sections import extract_sections, html_to_text
from committee.data.xbrl import fiscal_label, parse_companyfacts

D = Path(__file__).parent / "fixtures" / "data" / "sec"
ACC = dt.datetime(2024, 10, 3, 22, 30, 10, tzinfo=dt.UTC)
SID = "CIK0000320193"


def _f4(name: str, form: str = "4") -> list[dict[str, object]]:
    return parse_form4(
        (D / name).read_bytes(), accession="A-1", form=form, accepted_at=ACC, security_id=SID
    )


def test_form4_10b5_1_footnote_roles_and_derivatives() -> None:
    rows = _f4("form4_10b51.xml")
    assert len(rows) == 3  # 2 non-derivative + 1 derivative; the holding is not a txn
    m, s, rsu = rows
    assert [r["line"] for r in rows] == [0, 1, 2]
    assert s["txn_code"] == "S" and s["acquired_disposed"] == "D"
    assert s["shares"] == 50000.0 and s["price"] == 226.25
    assert s["shares_owned_after"] == 3330000.0
    assert s["is_10b5_1"] is True  # footnote F1 mentions Rule 10b5-1
    assert m["is_10b5_1"] is False and rsu["is_10b5_1"] is False
    assert rsu["is_derivative"] is True and m["is_derivative"] is False
    assert s["role"] == "officer,director" and s["officer_title"] == "Chief Executive Officer"
    assert s["insider_id"] == "1214156" and s["cik"] == 320193
    assert s["txn_date"] == dt.date(2024, 10, 2) and s["is_amendment"] is False
    assert s["known_time"] == ACC and s["filed_at"] == ACC


def test_form4_amendment_with_checkbox() -> None:
    (row,) = _f4("form4a_checkbox.xml", form="4/A")
    assert row["is_amendment"] is True
    assert row["is_10b5_1"] is True  # aff10b5One checkbox
    assert row["txn_date"] == dt.date(2024, 10, 1)  # "2024-10-01-04:00"
    assert row["role"] == "officer" and row["is_director"] is False


def test_form4_multi_owner_one_row_per_owner() -> None:
    rows = _f4("form4_multi.xml")
    assert [(r["line"], r["insider_id"], r["role"]) for r in rows] == [
        (0, "1067983", "ten_pct_owner"),
        (1, "1067984", "ten_pct_owner,other"),
    ]
    assert {r["shares"] for r in rows} == {1500000.0}


def test_form4_holdings_only_and_malformed() -> None:
    assert _f4("form4_holdings.xml") == []
    with pytest.raises(Form4ParseError, match="XML parse error"):
        _f4("form4_bad.xml")
    with pytest.raises(Form4ParseError, match="DTD"):
        parse_form4(
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><ownershipDocument/>',
            accession="x", form="4", accepted_at=ACC, security_id=SID,
        )  # fmt: skip
    bad_code = (
        (D / "form4_multi.xml").read_bytes().replace(b"<value>A</value>", b"<value>Q</value>")
    )
    with pytest.raises(Form4ParseError, match="acquired/disposed"):
        parse_form4(bad_code, accession="x", form="4", accepted_at=ACC, security_id=SID)


def test_filing_index_8k_items_and_acceptance_time() -> None:
    data = json.loads((D / "submissions_aapl.json").read_bytes())
    refs = {r.accession: r for r in parse_filing_arrays(data["filings"]["recent"], 320193)}
    k8 = refs["0000320193-24-000069"]
    assert k8.form == "8-K" and k8.items == "2.02,9.01"
    assert refs["0000320193-24-000123"].items is None
    # EDGAR's acceptanceDateTime wall clock is Eastern: 06:01:36 EDT == 10:01:36 UTC.
    assert refs["0000320193-24-000123"].accepted_at == dt.datetime(
        2024, 11, 1, 10, 1, 36, tzinfo=dt.UTC
    )
    assert parse_acceptance("2024-01-05T17:00:00.000Z").hour == 22  # EST
    f4 = refs["0000320193-24-000100"]
    assert f4.xml_url.endswith("/320193/000032019324000100/wk-form4_1727994607.xml")
    assert refs["0000320193-24-000081"].period == dt.date(2024, 6, 29)


def test_sections_10k_longest_span_beats_toc() -> None:
    secs = {s.item: s.text for s in extract_sections((D / "aapl_10k.htm").read_text(), "10-K")}
    assert set(secs) == {"1A", "7"}
    assert secs["1A"].startswith("Item 1A. Risk Factors")
    assert "Macroeconomic and Industry Risks" in secs["1A"]
    assert "Unresolved Staff Comments" not in secs["1A"]
    assert "hidden xbrl" not in secs["1A"]
    assert "Fiscal 2024 Highlights" in secs["7"] and "391,035" in secs["7"]
    assert "Quantitative and Qualitative" not in secs["7"]


def test_sections_10q_mdna_is_item_2() -> None:
    secs = {s.item: s.text for s in extract_sections((D / "aapl_10q.htm").read_text(), "10-Q")}
    assert "forward-looking statements" in secs["7"]
    assert "Unregistered" not in secs["7"]
    assert "no material changes to the risk factors" in secs["1A"]
    assert "Share repurchases" not in secs["1A"]


def test_html_to_text_entities_and_cells() -> None:
    text = html_to_text(
        "<p>A&amp;B&#8217;s</p><table><tr><td>Item 7.</td><td>MD&amp;A</td></tr></table>"
    )
    assert text.split("\n") == ["A&B’s", "Item 7. MD&A"]


def test_fiscal_labels() -> None:
    assert fiscal_label(dt.date(2023, 10, 1), dt.date(2024, 9, 28)) == "FY2024"
    assert fiscal_label(dt.date(2024, 3, 31), dt.date(2024, 6, 29)) == "2024Q2"
    assert fiscal_label(None, dt.date(2025, 1, 2)) == "2024Q4"  # 52/53-week year end
    assert fiscal_label(dt.date(2023, 10, 1), dt.date(2024, 6, 29)) is None  # YTD


def test_xbrl_concept_fallback_restatement_and_derived() -> None:
    accepted = {"0000320193-24-000123": dt.datetime(2024, 11, 1, 10, 1, 36, tzinfo=dt.UTC)}
    rows = parse_companyfacts((D / "companyfacts_aapl.json").read_bytes(), SID, accepted)
    by = {}
    for r in rows:
        by.setdefault((r["metric"], r["fiscal_period"]), []).append(r)
    # Revenues absent -> RevenueFromContractWithCustomerExcludingAssessedTax; older -> SalesRevenueNet
    assert [r["value"] for r in by[("revenue", "FY2024")]] == [391035000000.0]
    assert [r["value"] for r in by[("revenue", "FY2017")]] == [229234000000.0]
    # Same value repeated as a comparative in a later 10-K is not a new version.
    assert len(by[("revenue", "FY2023")]) == 1
    assert ("revenue", "2024Q2") in by  # quarter from the 10-Q; 8-K copy ignored
    # Restatement: a second version known at the later filing's acceptance time.
    ni = by[("net_income", "FY2023")]
    assert [r["value"] for r in ni] == [96995000000.0, 97000000000.0]
    assert ni[0]["known_time"] == dt.datetime(2023, 11, 3, 23, 59, 59, tzinfo=dt.UTC)
    assert ni[1]["known_time"] == accepted["0000320193-24-000123"]
    assert ni[1]["accession"] == "0000320193-24-000123"
    assert by[("ebitda", "FY2024")][0]["value"] == 123216000000.0 + 11445000000.0
    assert by[("ebit", "FY2024")][0]["value"] == 123216000000.0
    assert by[("book_value", "2024Q3")][0]["value"] == 56950000000.0
    assert by[("total_assets", "2024Q2")][0]["period_end"] == dt.date(2024, 6, 29)
    assert by[("shares_outstanding", "2024Q4")][0]["value"] == 15115823000.0
    assert not any(m.startswith("_") for m, _ in by)


def sec_routes() -> dict[str, Path | bytes]:
    return {
        "company_tickers_exchange.json": D / "company_tickers_exchange.json",
        "submissions/CIK0000320193.json": D / "submissions_aapl.json",
        "submissions/CIK0000789019.json": D / "submissions_msft.json",
        "submissions/CIK0000789019-submissions-001.json": D / "submissions_msft_page1.json",
        "companyfacts/CIK0000320193.json": D / "companyfacts_aapl.json",
        "000032019324000123/aapl-20240928.htm": D / "aapl_10k.htm",
        "000032019324000081/aapl-20240629.htm": D / "aapl_10q.htm",
        "000032019324000100/wk-form4_1727994607.xml": D / "form4_10b51.xml",
        "000032019324000101/wk-form4a_1728066660.xml": D / "form4a_checkbox.xml",
        "000114036124040000/form4.xml": D / "form4_multi.xml",
        "000032019324000090/wk-form4_holdings.xml": D / "form4_holdings.xml",
        "000032019324000091/wk-form4_bad.xml": D / "form4_bad.xml",
    }


def test_ingest_edgar_end_to_end_then_data_check(ctx, project: Path) -> None:  # type: ignore[no-untyped-def]
    ff = FixtureFetcher(sec_routes())
    run_securities(ctx, fetcher=ff)
    s = run_edgar(ctx, ["AAPL", "MSFT", "JPM"], dt.date(2023, 1, 1), fetcher=ff)

    assert s["status"] == "partial"  # JPM submissions and MSFT companyfacts not recorded
    assert any(e.startswith("JPM submissions") for e in s["errors"])
    assert s["counts"]["form:4"] == 4 and s["counts"]["form:4/A"] == 1
    assert s["counts"]["form:8-K"] == 3  # AAPL + MSFT recent + MSFT older page
    assert "form:S-8" not in s["counts"] and s["counts"]["form:10-K"] == 1  # 2022 10-K < since
    assert s["counts"]["form4_holdings_only"] == 1 and s["counts"]["form4_dead_letter"] == 1
    assert s["rows_written"]["insider_txns"] == 6
    assert s["rows_written"]["filing_sections"] == 4
    assert s["dead_letters"] == 1
    assert not any("page-000" in c or "submissions-000" in c for c in ff.calls)

    lake = lake_for(ctx)
    pit = PIT(lake)
    tenk_known = dt.datetime(2024, 11, 1, 10, 1, 36, tzinfo=dt.UTC)
    before = pit.latest("filings", tenk_known - dt.timedelta(seconds=1))
    after = pit.latest("filings", tenk_known)
    assert "0000320193-24-000123" not in set(before["accession"])
    assert "0000320193-24-000123" in set(after["accession"])
    f = after.set_index("accession")
    assert f.loc["0000320193-24-000069", "items"] == "2.02,9.01"
    assert f.loc["0000320193-24-000090", "n_txns"] == 0
    assert str(f.loc["0000320193-24-000123", "sections_hash"])
    ni = pit.latest("fundamentals", "2030-01-01", where="metric = 'net_income'")
    fy23 = ni[ni["fiscal_period"] == "FY2023"].iloc[0]
    assert fy23["value"] == 97000000000.0
    assert fy23["known_time"].to_pydatetime() == tenk_known  # from the filings table
    pit.close()

    with ctx.journal() as j:
        summaries = [e.payload for e in j.entries("ingest_summary")]
    assert [p["source"] for p in summaries] == ["sec_tickers", "sec_edgar"]

    again = run_edgar(ctx, ["AAPL"], dt.date(2023, 1, 1), fetcher=ff)
    assert again["counts"]["already_ingested"] == 9
    assert again["rows_written"] == {}

    rep = run_check(ctx)
    assert rep.ok, rep.payload()
    out = CliRunner().invoke(data_cli, ["data", "check", "--root", str(project)])
    assert out.exit_code == 0, out.output
    assert "filings" in out.output and "form4_completeness" in out.output
