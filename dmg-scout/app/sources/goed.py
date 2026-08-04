"""Nevada GOED (goed.nv.gov) — tax abatement board agendas and packets.

WordPress site. Notices/agendas listing at /notices-agendas/ (paginated);
meeting PDFs live under /wp-content/uploads/YYYY/MM/. Board packets name the
applicant entity, capital investment, and job counts 18-30 months before
construction. Strategy: scrape the listing pages for new PDF links, download,
parse with pdfplumber, keyword-filter, and hand relevant text to the LLM.
"""
from __future__ import annotations

import io
import logging
from typing import Iterator

import pdfplumber
from selectolax.parser import HTMLParser

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)

MAX_LISTING_PAGES = 3
MAX_PDF_PAGES = 80  # board packets run long; cap parse cost


def pdf_to_text(data: bytes, max_pages: int = MAX_PDF_PAGES) -> str:
    out: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            out.append(page.extract_text() or "")
    return "\n".join(out)


class GoedAdapter(SourceAdapter):
    name = "goed"

    def fetch(self, cfg: Config, client: PoliteClient) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        base = src.get("base_url", "https://goed.nv.gov").rstrip("/")
        notices_path = src.get("notices_path", "/notices-agendas/")

        pdf_links: dict[str, str] = {}
        for page_no in range(1, MAX_LISTING_PAGES + 1):
            url = f"{base}{notices_path}" if page_no == 1 else f"{base}{notices_path}page/{page_no}/"
            try:
                html = client.get_text(url)
            except Exception as exc:  # noqa: BLE001
                if page_no == 1:
                    raise SourceFailure(f"GOED listing page unreachable: {exc}") from exc
                log.warning("goed listing page %s failed: %s", url, exc)
                break
            tree = HTMLParser(html)
            for a in tree.css("a[href]"):
                href = a.attributes.get("href") or ""
                if "/wp-content/uploads/" in href and href.lower().endswith(".pdf"):
                    full = href if href.startswith("http") else f"{base}{href}"
                    label = (a.text(strip=True) or full.rsplit("/", 1)[-1])[:200]
                    pdf_links.setdefault(full, label)

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
