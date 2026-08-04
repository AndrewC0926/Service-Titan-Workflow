"""CEQAnet (ceqanet.lci.ca.gov) — California environmental filings.

The /Search page supports querying by county and document type and offers a CSV
download of results. Detail pages live at /{SCH number} (e.g. /2025080816) and
carry parcel numbers, acreage, lead agency, and a named contact.

NOP = earliest, most valuable. NOD = already too late (but logged: it moves the
project's stage/window so the score drops).
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
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure, keyword_match

log = logging.getLogger(__name__)

DOCTYPE_SIGNAL = {
    "NOP": SignalType.ceqa_nop,
    "EIR": SignalType.ceqa_deir,
    "NEG": SignalType.planning_agenda,
    "MND": SignalType.planning_agenda,
    "NOD": SignalType.planning_agenda,
}


def _detail_text(html: str) -> str:
    """Flatten a CEQAnet detail page to labeled text for the LLM."""
    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "header", "footer"):
        for node in tree.css(sel):
            node.decompose()
    body = tree.body
    return body.text(separator="\n", strip=True) if body else ""


class CeqanetAdapter(SourceAdapter):
    name = "ceqanet"

    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://ceqanet.lci.ca.gov").rstrip("/")
        counties = src.get("counties", [])
        doc_types = src.get("document_types", ["NOP"])
        lookback = int(src.get("lookback_days", 30))
        start = (datetime.utcnow() - timedelta(days=lookback)).strftime("%m/%d/%Y")

        attempts = failures = 0
        for county in counties:
            for doc_type in doc_types:
                url = (
                    f"{base}/Search/DownloadCSV"
                    f"?County={county}&DocumentType={doc_type}&ReceivedStartDate={start}"
                )
                attempts += 1
                try:
                    text = client.get_text(url)
                except Exception as exc:  # noqa: BLE001 — one county failing must not kill the run
                    log.warning("ceqanet CSV fetch failed for %s/%s: %s", county, doc_type, exc)
                    failures += 1
                    continue
                yield from self._parse_csv(text, base, county, doc_type, cfg, client)
        if attempts and failures == attempts:
            raise SourceFailure(f"all {attempts} CEQAnet CSV queries failed")

    def _parse_csv(self, csv_text: str, base: str, county: str, doc_type: str,
                   cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            # Column names as exported by CEQAnet; fall back defensively.
            sch = (row.get("SCH Number") or row.get("SCHNumber") or "").strip()
            title = (row.get("Title") or row.get("Document Title") or "").strip()
            desc = (row.get("Description") or row.get("Project Description") or "").strip()
            received = (row.get("Received") or row.get("Received Date") or "").strip()
            if not sch:
                continue
            haystack = f"{title}\n{desc}"
            if not keyword_match(haystack, cfg):
                continue
            detail_url = f"{base}/{sch}"
            text = haystack
            try:
                text = _detail_text(client.get_text(detail_url)) or haystack
            except Exception as exc:  # noqa: BLE001
                log.warning("ceqanet detail fetch failed for %s: %s", sch, exc)
            published = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    published = datetime.strptime(received, fmt)
                    break
                except ValueError:
                    continue
            yield FetchedDoc(
                source=self.name,
                source_uid=f"{sch}:{doc_type}",
                url=detail_url,
                title=title or f"SCH {sch}",
                raw_text=text,
                published_at=published,
                meta={"county": county, "document_type": doc_type, "sch_number": sch},
                default_signal_type=DOCTYPE_SIGNAL.get(doc_type, SignalType.planning_agenda),
            )
