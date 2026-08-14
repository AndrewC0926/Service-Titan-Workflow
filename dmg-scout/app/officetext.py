"""Text extraction for Word/Excel attachments -- federal UFGS spec sections
are frequently published as .docx rather than PDF (confirmed directly:
2026-08-13's first real SAM.gov run had 7/42 attachments fail to parse for
exactly this reason). Same "empty string means no text, not an error" contract
as app.pdftext.pdf_to_text.
"""
from __future__ import annotations

import io
import logging

import docx
import openpyxl

log = logging.getLogger(__name__)


def docx_to_text(data: bytes) -> str:
    """Paragraph and table-cell text, in document order. Returns "" for a
    docx with no extractable text (e.g. image-only), never raises for that
    case -- callers must treat empty as "no content"."""
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text]
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    return "\n".join(parts)


def xlsx_to_text(data: bytes) -> str:
    """Every non-empty cell, sheet by sheet, in reading order -- spec
    "sections" are sometimes tabulated (e.g. a manufacturer-comparison
    matrix) rather than prose, so this deliberately does not try to be
    smarter than "read every cell"."""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        parts = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows():
                cells = [str(c.value) for c in row if c.value is not None]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    finally:
        wb.close()
