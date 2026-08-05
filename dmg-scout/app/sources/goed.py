"""Nevada GOED (goed.nv.gov) — tax abatement board agendas and packets.

WordPress site. Notices/agendas listing at /notices-agendas/ (paginated);
meeting PDFs live under /wp-content/uploads/YYYY/MM/. Board packets name the
applicant entity, capital investment, and job counts 18-30 months before
construction. Strategy: scrape the listing pages for new PDF links, download,
parse with pdfplumber, keyword-filter, and hand relevant text to the LLM.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterator

from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.pdftext import DEFAULT_MAX_PAGES, pdf_to_text
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)

MAX_LISTING_PAGES = 3
MAX_LISTING_PAGES_BACKFILL = 40
MAX_PDF_PAGES = DEFAULT_MAX_PAGES  # board packets run long; cap parse cost

_UPLOAD_DATE = re.compile(r"/wp-content/uploads/(\d{4})/(\d{2})/")


def upload_month(url: str) -> datetime | None:
    m = _UPLOAD_DATE.search(url)
    if not m:
        return None
    return datetime(int(m.group(1)), int(m.group(2)), 1)


class GoedAdapter(SourceAdapter):
    name = "goed"

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://goed.nv.gov").rstrip("/")
        notices_path = src.get("notices_path", "/notices-agendas/")
        max_pages = MAX_LISTING_PAGES if since is None else MAX_LISTING_PAGES_BACKFILL

        pdf_links: dict[str, str] = {}
        for page_no in range(1, max_pages + 1):
            url = f"{base}{notices_path}" if page_no == 1 else f"{base}{notices_path}page/{page_no}/"
            try:
                html = client.get_text(url)
            except Exception as exc:  # noqa: BLE001
                if page_no == 1:
                    raise SourceFailure(f"GOED listing page unreachable: {exc}") from exc
                log.warning("goed listing page %s failed: %s", url, exc)
                break
            tree = HTMLParser(html)
            page_months: list[datetime] = []
            for a in tree.css("a[href]"):
                href = a.attributes.get("href") or ""
                if "/wp-content/uploads/" in href and href.lower().endswith(".pdf"):
                    full = href if href.startswith("http") else f"{base}{href}"
                    month = upload_month(full)
                    if month:
                        page_months.append(month)
                    if since is not None and month is not None and month < since.replace(day=1):
                        continue  # older than the backfill window
                    label = (a.text(strip=True) or full.rsplit("/", 1)[-1])[:200]
                    pdf_links.setdefault(full, label)
            # Stop paging once an entire listing page predates the window.
            if since is not None and page_months and all(
                    m < since.replace(day=1) for m in page_months):
                break

        for pdf_url, label in pdf_links.items():
            # Board packets and agendas only; skip unrelated uploads by name.
            lower = pdf_url.lower()
            if not any(k in lower for k in ("agenda", "packet", "minutes", "board", "abatement", "data-center")):
                continue
            try:
                text = pdf_to_text(client.get_bytes(pdf_url))
            except Exception as exc:  # noqa: BLE001
                log.warning("goed PDF %s failed: %s", pdf_url, exc)
                continue
            if not text.strip():
                continue
            yield FetchedDoc(
                source=self.name,
                source_uid=pdf_url.rsplit("/wp-content/", 1)[-1],
                url=pdf_url,
                title=f"GOED: {label}",
                raw_text=text,
                meta={"kind": "board_pdf"},
                default_signal_type=SignalType.abatement_application,
            )
