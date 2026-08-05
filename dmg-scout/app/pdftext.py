"""PDF text extraction, shared by every adapter that pulls agenda/board PDFs.

Meeting packets are the worst case: a Storey County Board of Commissioners
packet is 268 pages and 16 MB, and the interesting part — the agenda item list —
is in the first few pages. So extraction is page-capped, and callers screen on
the agenda pages before deciding whether the whole packet is worth keeping.
"""
from __future__ import annotations

import io
import logging

import pdfplumber

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 80


def pdf_to_text(data: bytes, max_pages: int = DEFAULT_MAX_PAGES) -> str:
    """Extract text from the first `max_pages` pages. Returns "" for PDFs with
    no text layer (scanned images) — callers must treat empty as "no content",
    not as "no match"."""
    out: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            out.append(page.extract_text() or "")
    return "\n".join(out)


def pdf_page_count(data: bytes) -> int:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return len(pdf.pages)
