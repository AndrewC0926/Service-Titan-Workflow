"""SEC EDGAR ingestion: filing index, Forms 3/4/5, 10-K/10-Q sections, XBRL facts.

Fair access: the fetcher sends a User-Agent with name and email, stays under the
configured requests/second (<= 8) and backs off exponentially on 429/503
(``HttpFetcher``). Every raw response is stored in the raw zone.

known_time for a filing is its EDGAR acceptance datetime. The submissions API
reports ``acceptanceDateTime`` with a trailing ``Z`` but the wall clock is US
Eastern (as on EDGAR index pages); we interpret it as America/New_York. If that
were wrong it would only make known_time later (conservative), never earlier.

``filings`` rows carry one column beyond the contract: ``n_txns`` (number of
insider_txns rows parsed for a Form 4/5; 0 = holdings-only; null for other forms
or when the document went to the dead-letter table). The DQ suite uses it.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from committee.data.common import (
    NY,
    Batch,
    IngestError,
    RunStats,
    cik_of,
    drop_known,
    existing_keys,
    resolve_universe,
    to_date,
    utcnow,
)
from committee.data.form4 import Form4ParseError, parse_form4
from committee.data.http import Fetcher, FetchError, HttpFetcher
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.data.sections import extract_sections, sections_hash
from committee.data.xbrl import parse_companyfacts
from committee.journal.store import Journal

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

OWNERSHIP_FORMS = frozenset({"4", "4/A", "5", "5/A"})
SECTION_FORMS = frozenset({"10-K", "10-Q"})
FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "3", "3/A"}) | (
    OWNERSHIP_FORMS
)
SOURCE = "sec_edgar"


def sec_fetcher(user_agent: str, per_second: float = 8.0) -> HttpFetcher:
    """Live EDGAR fetcher: User-Agent required, rate limited, backoff on 429/503."""
    if "@" not in user_agent:
        raise IngestError("SEC_USER_AGENT must include a contact email, e.g. 'Name you@x.com'")
    return HttpFetcher(
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
        per_second=min(per_second, 8.0),
    )


def parse_acceptance(s: str) -> dt.datetime:
    naive = dt.datetime.fromisoformat(s.strip().removesuffix("Z").split(".")[0])
    return naive.replace(tzinfo=NY).astimezone(dt.UTC)


@dataclass(frozen=True)
class FilingRef:
    accession: str
    cik: int
    form: str
    filing_date: dt.date
    accepted_at: dt.datetime
    period: dt.date | None
    primary_document: str
    items: str | None

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik, acc=self.accession.replace("-", ""), doc=self.primary_document
        )

    @property
    def xml_url(self) -> str:
        """Ownership filings list the XSL-rendered doc; the raw XML drops the xsl dir."""
        doc = self.primary_document.rsplit("/", 1)[-1]
        return ARCHIVE_URL.format(cik=self.cik, acc=self.accession.replace("-", ""), doc=doc)


def parse_filing_arrays(arrays: Mapping[str, Any], cik: int) -> list[FilingRef]:
    """Column arrays (``filings.recent`` or an older page) -> FilingRefs."""
    out = []
    n = len(arrays.get("accessionNumber", []))
    for i in range(n):

        def col(name: str, i: int = i) -> str:
            vals = arrays.get(name) or []
            return str(vals[i]) if i < len(vals) and vals[i] is not None else ""

        acc, accepted = col("accessionNumber"), col("acceptanceDateTime")
        if not acc or not accepted:
            continue
        out.append(
            FilingRef(
                accession=acc,
                cik=cik,
                form=col("form"),
                filing_date=dt.date.fromisoformat(col("filingDate")),
                accepted_at=parse_acceptance(accepted),
                period=to_date(col("reportDate")),
                primary_document=col("primaryDocument"),
                items=col("items") or None,
            )
        )
    return out


def fetch_filing_index(
    fetcher: Fetcher, raw: RawZone, cik: int, since: dt.date, now: dt.datetime
) -> list[FilingRef]:
    """All filings since ``since`` (recent block plus any older pages needed)."""
    body = fetcher.get(SUBMISSIONS_URL.format(cik=cik))
    raw.put("sec_submissions", f"CIK{cik:010d}", now, body)
    data = json.loads(body)
    filings = data.get("filings", {})
    refs = parse_filing_arrays(filings.get("recent", {}), cik)
    oldest = min((r.filing_date for r in refs), default=None)
    if oldest is None or oldest > since:
        for page in filings.get("files", []):
            last = to_date(page.get("filingTo"))
            if last is not None and last < since:
                continue
            pbody = fetcher.get(SUBMISSIONS_PAGE_URL.format(name=page["name"]))
            raw.put("sec_submissions", str(page["name"]), now, pbody)
            refs += parse_filing_arrays(json.loads(pbody), cik)
    return refs


def _filing_row(
    ref: FilingRef, security_id: str, shash: str | None, n_txns: int | None
) -> dict[str, Any]:
    return {
        "accession": ref.accession,
        "cik": ref.cik,
        "security_id": security_id,
        "form": ref.form,
        "period": ref.period,
        "accepted_at": ref.accepted_at,
        "url": ref.url,
        "items": ref.items,
        "sections_hash": shash,
        "n_txns": n_txns,
        "event_time": ref.accepted_at,
        "known_time": ref.accepted_at,
    }


def _ownership(
    fetcher: Fetcher, raw: RawZone, ref: FilingRef, sid: str, batch: Batch, stats: RunStats
) -> int | None:
    try:
        body = fetcher.get(ref.xml_url)
        raw.put("sec_filing", ref.accession, ref.accepted_at, body, ext="xml")
        rows = parse_form4(
            body, accession=ref.accession, form=ref.form, accepted_at=ref.accepted_at,
            security_id=sid,
        )  # fmt: skip
    except (Form4ParseError, FetchError) as e:
        batch.dead_letter(f"form{ref.form}:{sid}", ref.accession, f"{type(e).__name__}: {e}")
        stats.counts["form4_dead_letter"] += 1
        return None
    batch.add("insider_txns", rows)
    stats.counts["insider_txns"] += len(rows)
    if not rows:
        stats.counts["form4_holdings_only"] += 1
    return len(rows)


def _sections(
    fetcher: Fetcher, raw: RawZone, ref: FilingRef, sid: str, batch: Batch, stats: RunStats
) -> str | None:
    try:
        body = fetcher.get(ref.url)
    except FetchError as e:
        batch.dead_letter(f"sections:{sid}", ref.accession, f"FetchError: {e}")
        return None
    raw.put("sec_filing", ref.accession, ref.accepted_at, body, ext="html")
    secs = extract_sections(body.decode("utf-8", errors="replace"), ref.form)
    if not secs:
        stats.error(f"{ref.accession}: no Item 1A / MD&A sections found")
    batch.add(
        "filing_sections",
        [
            {
                "accession": ref.accession,
                "security_id": sid,
                "form": ref.form,
                "period": ref.period,
                "item": s.item,
                "text": s.text,
                "embedding_ref": None,
                "event_time": ref.accepted_at,
                "known_time": ref.accepted_at,
            }
            for s in secs
        ],
    )
    stats.counts["sections"] += len(secs)
    return sections_hash(secs)


def _facts(
    fetcher: Fetcher,
    raw: RawZone,
    cik: int,
    sid: str,
    accepted: Mapping[str, dt.datetime],
    known: set[tuple[str, ...]],
    batch: Batch,
    stats: RunStats,
    now: dt.datetime,
) -> None:
    try:
        body = fetcher.get(COMPANYFACTS_URL.format(cik=cik))
    except FetchError as e:
        stats.error(f"companyfacts CIK{cik}: {e}")
        return
    raw.put("sec_companyfacts", f"CIK{cik:010d}", now, body)
    try:
        rows = parse_companyfacts(body, sid, accepted)
    except (ValueError, KeyError, TypeError) as e:
        batch.dead_letter(f"companyfacts:{sid}", COMPANYFACTS_URL.format(cik=cik), repr(e))
        return
    rows = drop_known(rows, known, FUND_KEY)
    batch.add("fundamentals", rows)
    stats.counts["fundamentals"] += len(rows)


FUND_KEY = ("security_id", "metric", "fiscal_period", "accession")


def ingest_edgar(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    tickers: Sequence[str],
    since: dt.date,
    *,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
    include_facts: bool = True,
) -> dict[str, Any]:
    """Ingest filings, ownership forms, sections and XBRL facts; returns the summary."""
    now = now or utcnow()
    stats = RunStats(SOURCE, new_ingest_id(), extra={"tickers": [t.upper() for t in tickers]})
    pit = PIT(lake)
    try:
        resolved = resolve_universe(pit, tickers, now, stats)
        seen = {k[0] for k in existing_keys(pit, "filings", ["accession"])}
        fund_known = existing_keys(pit, "fundamentals", FUND_KEY)
        prior = pit.latest("filings", now)
    finally:
        pit.close()
    accepted: dict[str, dt.datetime] = (
        {str(r["accession"]): r["accepted_at"].to_pydatetime() for _, r in prior.iterrows()}
        if not prior.empty
        else {}
    )
    batch = Batch(dtypes={"filings": {"n_txns": "Int64"}})
    ok = 0
    for ticker, sid in resolved.items():
        cik = cik_of(sid)
        try:
            refs = fetch_filing_index(fetcher, raw, cik, since, now)
        except (FetchError, ValueError, KeyError) as e:
            stats.error(f"{ticker} submissions: {e}")
            continue
        ok += 1
        accepted.update({r.accession: r.accepted_at for r in refs})
        for ref in refs:
            if ref.form not in FORMS or ref.filing_date < since:
                continue
            if ref.accession in seen:
                stats.counts["already_ingested"] += 1
                continue
            seen.add(ref.accession)
            n_txns = (
                _ownership(fetcher, raw, ref, sid, batch, stats)
                if ref.form in OWNERSHIP_FORMS
                else None
            )
            shash = (
                _sections(fetcher, raw, ref, sid, batch, stats)
                if ref.form in SECTION_FORMS
                else None
            )
            batch.add("filings", [_filing_row(ref, sid, shash, n_txns)])
            stats.counts[f"form:{ref.form}"] += 1
        if include_facts:
            _facts(fetcher, raw, cik, sid, accepted, fund_known, batch, stats, now)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(resolved), succeeded=ok)
