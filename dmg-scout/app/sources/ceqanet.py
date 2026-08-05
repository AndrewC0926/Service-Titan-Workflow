"""CEQAnet (ceqanet.lci.ca.gov) — California environmental filings.

Endpoint shape verified live against the site's own advanced-search form
(/Search/Advanced), which submits `GET /Search`:

    GET /Search?County=Riverside&DocumentType=NOP&StartRange=2026-01-01
               &EndRange=2026-08-04&OutputFormat=CSV

`OutputFormat=CSV` returns the full result set as text/csv (it is *not* capped
or paginated the way the HTML table is — the HTML view stops at 100 rows, the
CSV returned 5,490 for one county over five years). robots.txt only disallows
`/*/AttachmentZip`; the CSV form is allowed.

Two gotchas that cost us the first live run:

* The response is **cp1252**, not UTF-8 (en-dashes in descriptions decode as
  0x96). Decoding via `.text` mangles or raises, so we read bytes.
* Bursting the endpoint returns **403** once you exceed roughly 8 requests in
  quick succession — it is a throttle, not an authorization failure. We
  therefore issue one request per county rather than per county x document
  type (7 instead of 35), and PoliteClient treats 403 as retryable.

The CSV carries far more than the old HTML-scrape path did: lead agency, named
contact with email and phone, acreage, parcel number, coordinates, and the
canonical detail URL. Contact fields are written into raw_text so extraction
can put the lead-agency planner on the contact ladder (rung 7).

Document types are the site's real 3-letter codes (NOP, EIR, MND, NEG, NOD,
NOE, ...). NOP = earliest and most valuable. NOD = already too late (still
logged: it moves the project's stage/window so the score drops).
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import (
    FetchedDoc, SourceAdapter, SourceFailure, TargetResult, fanout_verify, keyword_match,
)

log = logging.getLogger(__name__)

# Real CEQAnet document-type codes -> our signal taxonomy. Codes not listed
# fall back to planning_agenda.
DOCTYPE_SIGNAL = {
    "NOP": SignalType.ceqa_nop,          # Notice of Preparation of a Draft EIR
    "EIR": SignalType.ceqa_deir,         # Draft EIR
    "SBE": SignalType.ceqa_deir,         # Subsequent EIR
    "SIR": SignalType.ceqa_deir,         # Supplemental EIR
    "SEA": SignalType.ceqa_deir,         # Supplemental EIR
    "RIR": SignalType.ceqa_deir,         # Revised EIR
    "EIS": SignalType.ceqa_deir,         # Draft EIS
    "SIS": SignalType.ceqa_deir,         # Revised/Supplemental EIS
    "MND": SignalType.planning_agenda,   # Mitigated Negative Declaration
    "NEG": SignalType.planning_agenda,   # Negative Declaration
    "EA": SignalType.planning_agenda,    # Environmental Assessment
    "SCA": SignalType.planning_agenda,   # Sustainable Communities Env. Assessment
    "NOE": SignalType.planning_agenda,   # Notice of Exemption
    "NOD": SignalType.planning_agenda,   # Notice of Determination (too late)
    "NDE": SignalType.planning_agenda,   # Notice of Decision
    "ROD": SignalType.planning_agenda,   # Record of Decision
    "NOI": SignalType.planning_agenda,   # Notice of Intent
    "CON": SignalType.planning_agenda,   # Early Consultation
    "FIN": SignalType.planning_agenda,   # Final Document
}

# Columns copied straight into meta when non-empty.
_META_COLUMNS = {
    "Lead Agency Name": "lead_agency",
    "Lead Agency Acronym": "lead_agency_acronym",
    "Location Total Acres": "acres",
    "Location Parcel Number": "parcel_number",
    "Location Coordinates": "coordinates",
    "Location Cross Streets": "cross_streets",
    "Location Zip Code": "location_zip",
    "Cities": "cities",
    "Counties": "counties",
    "NOC Development Type": "development_type",
    "NOC Local Action": "local_action",
    "NOC State Review Start Date": "state_review_start",
    "NOC State Review End Date": "state_review_end",
    "NOC Public Review Start Date": "public_review_start",
    "NOC Public Review End Date": "public_review_end",
    "Contact Full Name": "contact_name",
    "Contact Job Title": "contact_title",
    "Contact Email Address": "contact_email",
    "Contact Phone Number": "contact_phone",
    "Contact Authority": "contact_authority",
}


def _decode(raw: bytes) -> str:
    """CEQAnet serves cp1252 with no charset in the Content-Type header."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detail_text(html: str) -> str:
    """Flatten a CEQAnet detail page to labeled text for the LLM."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "header", "footer"):
        for node in tree.css(sel):
            node.decompose()
    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _month_windows(since: datetime, until: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Calendar-month [start, end] pairs covering since..until inclusive."""
    cur = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur <= until:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cur, since), min(nxt - timedelta(days=1), until)
        cur = nxt


class CeqanetAdapter(SourceAdapter):
    name = "ceqanet"

    # ---- request building -------------------------------------------------

    def _counties(self, cfg: Config) -> list[str]:
        return cfg.source(self.name).get("counties", [])

    def _wanted_types(self, cfg: Config) -> set[str] | None:
        """Configured document-type allowlist, or None for 'accept everything'.

        Filtering happens client-side: one CSV request per county returns all
        types, and narrowing server-side would cost 5x the requests (which is
        what tripped the 403 throttle)."""
        types = cfg.source(self.name).get("document_types") or []
        codes = {t.strip().upper().split(" ")[0].split("-")[0] for t in types if t.strip()}
        return codes or None

    def _csv_rows(self, client: PoliteClient, base: str, county: str,
                  start: datetime, end: datetime | None) -> list[dict]:
        params = {
            "County": county,
            "StartRange": start.strftime("%Y-%m-%d"),
            "OutputFormat": "CSV",
        }
        if end is not None:
            params["EndRange"] = end.strftime("%Y-%m-%d")
        raw = client.get_bytes(f"{base}/Search", params=params)
        return list(csv.DictReader(io.StringIO(_decode(raw))))

    # ---- backfill --------------------------------------------------------

    def backfill_chunks(self, cfg: Config, since: datetime) -> list[dict]:
        """One chunk per county per calendar month — bounded responses and a
        stable resume key."""
        until = datetime.utcnow()
        return [
            {
                "key": f"{county}:{start:%Y-%m}",
                "county": county,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
            for county in self._counties(cfg)
            for start, end in _month_windows(since, until)
        ]

    def fetch_chunk(self, cfg: Config, client: PoliteClient, since: datetime,
                    chunk: dict) -> Iterator[FetchedDoc]:
        start = datetime.fromisoformat(chunk["start"])
        end = datetime.fromisoformat(chunk["end"])
        yield from self._fetch_counties(cfg, client, start, end, [chunk["county"]])

    # ---- fetch -----------------------------------------------------------

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        if since is None:
            since = datetime.utcnow() - timedelta(days=int(src.get("lookback_days", 30)))
        yield from self._fetch_counties(cfg, client, since, None, self._counties(cfg))

    def _fetch_counties(self, cfg: Config, client: PoliteClient, since: datetime,
                        end: datetime | None, counties: list[str]) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://ceqanet.lci.ca.gov").rstrip("/")
        wanted = self._wanted_types(cfg)
        fetch_detail = bool(src.get("fetch_detail", True))

        attempts = failures = 0
        for county in counties:
            attempts += 1
            try:
                rows = self._csv_rows(client, base, county, since, end)
            except Exception as exc:  # noqa: BLE001 — one county must not kill the run
                log.warning("ceqanet CSV fetch failed for %s: %s", county, exc)
                failures += 1
                continue
            log.info("ceqanet %s: %d rows", county, len(rows))
            for row in rows:
                doc = self._row_to_doc(row, base, county, wanted, cfg, client, fetch_detail)
                if doc is not None:
                    yield doc
        if attempts and failures == attempts:
            raise SourceFailure(f"all {attempts} CEQAnet CSV queries failed")

    def verify(self, cfg: Config, client: PoliteClient) -> dict:
        """Probe every configured county. Reports rows scanned separately from
        keyword matches: data-center filings are rare, so a 30-day window can be
        legitimately empty — but 0 rows scanned means the endpoint moved again."""
        src = cfg.source(self.name)
        base = src.get("base_url", "https://ceqanet.lci.ca.gov").rstrip("/")
        since = datetime.utcnow() - timedelta(days=int(src.get("lookback_days", 30)))
        wanted = self._wanted_types(cfg)

        results = []
        for county in self._counties(cfg):
            try:
                rows = self._csv_rows(client, base, county, since, None)
            except Exception as exc:  # noqa: BLE001
                results.append(TargetResult(county, False, f"{type(exc).__name__}: {exc}"))
                continue
            matched = sum(
                1 for r in rows
                if (wanted is None
                    or (r.get("Document Type") or "").strip().upper() in wanted)
                and keyword_match(
                    f"{r.get('Document Title') or ''}\n{r.get('Project Title') or ''}\n"
                    f"{r.get('Document Description') or ''}", cfg)
            )
            results.append(TargetResult(county, True, records=len(rows), docs=matched))
        return fanout_verify("counties", results)

    def _row_to_doc(self, row: dict, base: str, county: str, wanted: set[str] | None,
                    cfg: Config, client: PoliteClient,
                    fetch_detail: bool) -> FetchedDoc | None:
        get = lambda col: (row.get(col) or "").strip()  # noqa: E731

        sch = get("SCH Number")
        if not sch:
            return None
        doc_type = get("Document Type").upper()   # codes sometimes carry a trailing space
        if wanted is not None and doc_type not in wanted:
            return None

        doc_title = get("Document Title")
        project_title = get("Project Title")
        description = get("Document Description")
        title = project_title or doc_title or f"SCH {sch}"

        haystack = "\n".join(filter(None, (doc_title, project_title, description)))
        if not keyword_match(haystack, cfg):
            return None

        url = get("Document Portal URL") or f"{base}/{sch}"

        meta = {"county": county, "document_type": doc_type, "sch_number": sch}
        for col, key in _META_COLUMNS.items():
            val = get(col)
            if val:
                meta[key] = val

        # Labeled text so extraction can pull size, location, and the named
        # agency contact (ladder rung 7) without a second fetch.
        lines = [
            f"SCH Number: {sch}",
            f"Document Type: {doc_type}",
            f"Project Title: {project_title or doc_title}",
            f"Lead Agency: {get('Lead Agency Name')}",
            # The row's own Counties column, not the county we queried under: a
            # statewide filing is returned by several county queries, and keying
            # the text off the query would give one filing several content
            # hashes and defeat dedupe.
            f"Counties: {get('Counties') or county}",
        ]
        for col in ("Cities", "Location Total Acres", "Location Parcel Number",
                    "Location Coordinates", "Location Cross Streets",
                    "NOC Development Type", "NOC Local Action",
                    "NOC Public Review End Date", "NOE Exempt Status"):
            val = get(col)
            if val:
                lines.append(f"{col}: {val}")
        contact = " / ".join(filter(None, (
            get("Contact Full Name"), get("Contact Job Title"),
            get("Contact Authority"), get("Contact Email Address"),
            get("Contact Phone Number"))))
        if contact:
            lines.append(f"Contact: {contact}")
        if description:
            lines.append(f"\nDescription:\n{description}")

        text = "\n".join(lines)
        if fetch_detail:
            try:
                detail = _detail_text(client.get_text(url))
            except Exception as exc:  # noqa: BLE001 — CSV row already stands alone
                log.warning("ceqanet detail fetch failed for %s: %s", sch, exc)
            else:
                if detail:
                    text = f"{text}\n\n--- detail page ---\n{detail}"

        return FetchedDoc(
            source=self.name,
            source_uid=f"{sch}:{doc_type}",
            url=url,
            title=title,
            raw_text=text,
            published_at=_parse_date(get("Received")) or _parse_date(get("Posted")),
            meta=meta,
            default_signal_type=DOCTYPE_SIGNAL.get(doc_type, SignalType.planning_agenda),
        )
