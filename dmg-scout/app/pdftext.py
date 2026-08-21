"""PDF text extraction, shared by every adapter that pulls agenda/board PDFs.

Meeting packets are the worst case: a Storey County Board of Commissioners
packet is 268 pages and 16 MB, and the interesting part — the agenda item list —
is in the first few pages. So extraction is page-capped, and callers screen on
the agenda pages before deciding whether the whole packet is worth keeping.
"""
from __future__ import annotations

import io
import logging
import re

import pdfplumber

log = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 80

_TAG_TOKEN = re.compile(r"\b(?:RTU|AH|CU)[-\s]?(?:\d+(?:-[A-Z])?|[A-Z])\b", re.IGNORECASE)


def _normalize_tag(tag: str) -> str:
    return re.sub(r"[-\s]", "", tag).upper()


def count_equipment_tags_deterministic(data: bytes) -> int:
    """Count equipment schedule rows in a ruling-line equipment schedule PDF,
    without an LLM. This is a test/validation oracle, not part of the
    extraction pipeline: it only understands the specific "MARK row lists
    tags across a transposed table" and "spec-sheet block headed by its own
    tag" shapes it was built against, so it will miscount documents laid
    out differently.

    Tag strings are NOT globally unique -- two different buildings can both
    have a unit called "RTU-01" -- so MARK-row cells are counted per
    occurrence, not de-duplicated by name. Spec-sheet-only blocks (e.g. an
    air handler detailed on its own page with no corresponding MARK-row
    entry anywhere) are added on top, but only when that exact tag doesn't
    already appear in some MARK row -- otherwise the same physical unit
    (e.g. a condensing unit listed in the schedule AND detailed later on its
    own spec sheet) would be counted twice.
    """
    mark_row_tags: list[str] = []
    spec_sheet_tags: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                rows = table.extract()
                for row in rows:
                    if not row or len(row) < 2:
                        continue
                    label = (row[1] or "").strip().upper()
                    if label != "MARK":
                        continue
                    for cell in row[2:]:
                        if not cell:
                            continue
                        first_line = cell.split("\n")[0]
                        mark_row_tags.extend(_TAG_TOKEN.findall(first_line))
                for row in rows:
                    for cell in row:
                        if not cell:
                            continue
                        first_line = cell.split("\n")[0].strip()
                        if _TAG_TOKEN.fullmatch(first_line):
                            spec_sheet_tags.append(first_line)

    mark_row_normalized = {_normalize_tag(t) for t in mark_row_tags}
    new_from_spec_sheets = {
        _normalize_tag(t) for t in spec_sheet_tags if _normalize_tag(t) not in mark_row_normalized
    }
    return len(mark_row_tags) + len(new_from_spec_sheets)


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
