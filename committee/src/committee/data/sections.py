"""10-K / 10-Q section extraction: Item 1A (Risk Factors) and MD&A.

HTML is flattened to text with one line per block element; headings are found
with line-anchored regexes on short lines. A section runs from its heading to
the next "Item N" heading. Tables of contents produce short spans, so the
longest candidate span wins.

MD&A is stored under item "7" for both forms (it is Item 2 of Part I in a 10-Q).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser

_BLOCK = {
    "p",
    "div",
    "br",
    "tr",
    "li",
    "ul",
    "ol",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "hr",
    "title",
}
_SKIP = {"script", "style", "head", "ix:header"}
_CELL = {"td", "th"}
MAX_HEADING_LEN = 160
MIN_SECTION_CHARS = 40


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag in _CELL:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ").replace("​", "")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


_APOS = "['’`]?"
_ITEM_ANY = re.compile(r"^(?:part\s+i+\s*[,.\-–—]?\s*)?item\s*\d+[a-c]?\b", re.I)

# (item key, form) -> heading patterns in priority order (titled first, bare fallback).
_PATTERNS: dict[tuple[str, str], tuple[re.Pattern[str], ...]] = {
    ("1A", "10-K"): (
        re.compile(r"^item\s*1a\s*[.:\-–—]?\s*risk\s+factors", re.I),
        re.compile(r"^item\s*1a\b", re.I),
    ),
    ("7", "10-K"): (
        re.compile(rf"^item\s*7\s*[.:\-–—]?\s*management{_APOS}s?\s+discussion", re.I),
        re.compile(r"^item\s*7(?![0-9a-c])\b", re.I),
    ),
    ("1A", "10-Q"): (re.compile(r"^item\s*1a\s*[.:\-–—]?\s*risk\s+factors", re.I),),
    ("7", "10-Q"): (
        re.compile(rf"^item\s*2\s*[.:\-–—]?\s*management{_APOS}s?\s+discussion", re.I),
    ),
}


@dataclass(frozen=True)
class Section:
    item: str
    text: str


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for ln in text.split("\n"):
        out.append((pos, ln))
        pos += len(ln) + 1
    return out


def _span(text: str, lines: list[tuple[int, str]], pat: re.Pattern[str]) -> str | None:
    best: str | None = None
    for i, (start, ln) in enumerate(lines):
        if len(ln) > MAX_HEADING_LEN or not pat.search(ln):
            continue
        end = len(text)
        for nstart, nln in lines[i + 1 :]:
            if len(nln) <= MAX_HEADING_LEN and _ITEM_ANY.search(nln):
                end = nstart
                break
        body = text[start:end].strip()
        if best is None or len(body) > len(best):
            best = body
    return best


def extract_sections(html: str, form: str) -> list[Section]:
    """Item 1A and MD&A text for a 10-K or 10-Q primary document."""
    base = form.upper().removesuffix("/A")
    text = html_to_text(html)
    lines = _lines_with_offsets(text)
    out = []
    for item in ("1A", "7"):
        for pat in _PATTERNS.get((item, base), ()):
            body = _span(text, lines, pat)
            if body and len(body) >= MIN_SECTION_CHARS:
                out.append(Section(item, body))
                break
    return out


def sections_hash(sections: list[Section]) -> str | None:
    if not sections:
        return None
    h = hashlib.sha256()
    for s in sorted(sections, key=lambda s: s.item):
        h.update(s.item.encode() + b"\0" + s.text.encode() + b"\0")
    return h.hexdigest()
