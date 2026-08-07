"""Nevada PUCN (Public Utilities Commission of Nevada) — large-load electric
service agreement dockets.

Nevada has no standing interconnection-queue disclosure the way SCE's CEC
IEPR filing is (see app/pipeline/iepr.py). What it has instead: NV Energy and
Sierra Pacific Power must seek PUCN approval of each individual Large Load
Electric Service Agreement (LLESA) before serving a data center or other
large customer, and every one of those becomes its own public docket. This
adapter scrapes PUCN's bulk docket index and hands the keyword-matched rows
(named counterparty, filing date, utility) into the normal
fetch -> triage -> extract -> resolve pipeline, same shape as every other
source.

**Why this may be the ONLY public trace for a TRIC-area project, and why
Nevada data center coverage has been thin until now**: the Tahoe Reno
Industrial Center (Storey County) operates under a master development
agreement that permits a broad range of uses "over the counter" — no
discrete, hearing-triggered land-use filing at all for many projects. CEQAnet
has no Nevada equivalent, and Nevada's county planning agendas (already
covered via app/sources/legistar.py and app/sources/civicplus.py where
available) simply never generate a filing for a project sited this way. An
LLESA docket, filed when the power contract is signed, can be the first and
only public record a TRIC project produces. That gap — not a gap in this
system's coverage, a gap in what Storey County requires be filed at all — is
the reason CEQA-style board rows dry up for TRIC even on well-known projects,
and it is worth remembering the next time Nevada coverage looks thin: it may
not be a broken adapter, it may be that nothing else exists to fetch.

Mechanics: PUCN's docket search (puc.nv.gov) blocks all crawling in
robots.txt, but the actual docket DATA lives on a different subdomain,
pucweb1.state.nv.us, which has no robots.txt at all — PoliteClient's
per-host check allows it. That page is a single flat, unpaginated HTML table
(~8,900 rows and growing) with no date-range query parameter and no API; the
"View" links to the underlying filed documents use ASP.NET postbacks tied to
a session, which is not worth session-aware scraping for — the docket LISTING
description line (e.g. "Application ... for approval of a Large Load Electric
Service Agreement with Amazon Data Services, Inc.") already names the
counterparty and is the whole signal this adapter needs. That is also why
`default_min_doc_chars` is set low here: the description line is genuinely
the entire document, not a stub of a longer one.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)

# The docket index covers every filing PUCN handles — rate cases, complaints,
# tariffs, general orders — so without this gate the adapter would hand
# thousands of irrelevant rows to triage every run. "large load electric
# service agreement" is the docket-type language NV Energy/Sierra Pacific use
# specifically for these agreements; "data center" catches the rarer docket
# that names the use directly (e.g. a UEPA transmission permit for a named
# data center's substation) even when LLESA language is absent.
_RELEVANT = re.compile(r"large load electric service agreement|data center", re.I)
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _parse_date(s: str) -> datetime | None:
    m = _DATE.search(s)
    if not m:
        return None
    month, day, year = (int(x) for x in m.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


class PucnAdapter(SourceAdapter):
    name = "pucn"
    default_min_doc_chars = 60  # see module docstring: the listing line IS the document

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://pucweb1.state.nv.us").rstrip("/")
        index_path = src.get("index_path", "/PUC2/Dktinfo.aspx")
        lookback_days = src.get("lookback_days", 180)
        cutoff = since or (datetime.utcnow() - timedelta(days=lookback_days))

        try:
            html = client.get_text(f"{base}{index_path}?Util=All")
        except Exception as exc:  # noqa: BLE001
            raise SourceFailure(f"PUCN docket index unreachable: {exc}") from exc

        tree = HTMLParser(html)
        tables = tree.css("table")
        if not tables:
            raise SourceFailure("PUCN docket index returned no table — page structure changed")
        # The docket data isn't laid out as clean one-docket-per-<tr> markup —
        # this GridView renders as a handful of nested <table> wrappers around
        # a single flat run of <td> cells, so per-<tr> traversal cross-reads
        # into sibling sub-tables (one row can resolve tens of thousands of
        # <td> descendants). The reliable structure is the flat cell sequence
        # of the largest table: a header run, then a strict repeating group of
        # 4 cells per docket — (Dkt No., Date Filed, Description, "View").
        cells = [td.text(strip=True) for td in max(tables, key=lambda t: len(t.css("td"))).css("td")]

        dkt_re = re.compile(r"^\d{2}-\d{5}$")
        start = next((i for i, c in enumerate(cells) if dkt_re.match(c)), None)
        if start is None:
            raise SourceFailure("PUCN docket table had no data rows — page structure likely changed")

        seen_rows = 0
        i = start
        while i + 3 < len(cells):
            docket_no, date_str, description = cells[i], cells[i + 1], cells[i + 2]
            if not dkt_re.match(docket_no):
                break  # ran past the last docket group into trailing page chrome
            seen_rows += 1
            i += 4
            if not _RELEVANT.search(description):
                continue
            filed = _parse_date(date_str)
            if filed is not None and filed < cutoff:
                continue
            yield FetchedDoc(
                source=self.name,
                source_uid=docket_no,
                # No stable deep link exists — the underlying filed document is
                # behind an ASP.NET postback tied to a session (see module
                # docstring) — so this points at the public search page a human
                # would use to find the docket by number, which is honest about
                # what's actually reachable rather than a URL that would 404.
                url=f"{base}{index_path}?Util=All",
                title=f"PUCN {docket_no}: {description[:120]}",
                raw_text=f"Nevada PUCN docket {docket_no}, filed {date_str}.\n{description}",
                published_at=filed,
                meta={"docket_no": docket_no},
                default_signal_type=SignalType.utility_load_request,
            )
        if seen_rows == 0:
            raise SourceFailure("PUCN docket table had no data rows — page structure likely changed")
