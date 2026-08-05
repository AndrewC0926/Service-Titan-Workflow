"""Section-aware document chunking for long filings.

A 500-page Draft EIR cannot go to Sonnet whole (cost) or as its first N tokens
(the mechanical/utility/air-quality sections — where generator counts and MW
live — sit in the middle). For documents over the short-doc threshold we locate
target sections by heading match and send those, always including the document
head (title, project description summary, contacts).

Every selection records which sections were found so a heuristic miss is
visible per-document (`sections_found` in the signal's extraction record and
the log), never silent.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.config import Config

log = logging.getLogger(__name__)

# Target sections, in priority order, with heading patterns matched at line
# starts (optionally prefixed by numbering like "3.3", "IV.", "Section 5").
SECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("project_description", re.compile(
        r"^\s*(?:(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+)?project\s+description\b", re.I | re.M)),
    ("utilities_energy", re.compile(
        r"^\s*(?:(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:utilities(?:\s+and\s+(?:service\s+systems|energy))?|energy(?:\s+(?:demand|resources|conservation))?|"
        r"electrical?\s+(?:supply|service|load)|public\s+utilities)\b", re.I | re.M)),
    ("air_quality", re.compile(
        r"^\s*(?:(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:air\s+quality|greenhouse\s+gas(?:\s+emissions)?)\b", re.I | re.M)),
    ("water_supply", re.compile(
        r"^\s*(?:(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:water\s+(?:supply|resources|demand)|hydrology(?:\s+and\s+water\s+quality)?|"
        r"water\s+supply\s+assessment)\b", re.I | re.M)),
    ("noise_generators", re.compile(  # generator specs often hide in noise analyses
        r"^\s*(?:(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+)?noise(?:\s+and\s+vibration)?\b", re.I | re.M)),
    # --- SEC filings (ABS/CMBS prospectuses, 8-Ks) ---------------------------
    # An ABS prospectus is as long as an EIR and just as front-loaded with
    # boilerplate: the campus list, tenant names, MW and delivery dates live in
    # the portfolio/property and tenancy sections, not in the head.
    ("filing_portfolio", re.compile(
        r"^\s*(?:(?:item|section)\s+[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:the\s+)?(?:portfolio|propert(?:y|ies)|data\s+cent(?:er|re)s?|"
        r"the\s+facilities|collateral(?:\s+pool)?)\b", re.I | re.M)),
    ("filing_tenancy", re.compile(
        r"^\s*(?:(?:item|section)\s+[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:tenan(?:t|cy|ts)(?:\s+(?:overview|concentration|summary))?|"
        r"lease(?:s|\s+(?:summary|overview|expirations?))?|"
        r"customer\s+concentration)\b", re.I | re.M)),
    ("filing_transaction", re.compile(
        r"^\s*(?:(?:item|section)\s+[\dIVXivx]+(?:\.\d+)*\.?\s+)?"
        r"(?:transaction\s+(?:overview|summary|structure)|"
        r"summary\s+of\s+terms|the\s+offering|use\s+of\s+proceeds)\b", re.I | re.M)),
]

# Any plausible top-level heading — used to find where a captured section ends.
ANY_HEADING = re.compile(
    r"^\s*(?:section\s+)?[\dIVXivx]+(?:\.\d+)*\.?\s+[A-Z][A-Za-z &,/-]{3,60}\s*$"
    r"|^\s*[A-Z][A-Z &,/-]{6,60}\s*$", re.M)


@dataclass
class SectionSelection:
    text: str
    sections_found: list[str] = field(default_factory=list)
    chunked: bool = False
    original_chars: int = 0

    @property
    def selected_chars(self) -> int:
        return len(self.text)


def _capture(text: str, start: int, cap: int) -> str:
    """Capture from a heading until the next heading, bounded by cap chars."""
    window = text[start:start + cap]
    nxt = ANY_HEADING.search(window, pos=200)  # skip the matched heading itself
    return window[:nxt.start()] if nxt else window


def select_relevant_text(text: str, cfg: Config) -> SectionSelection:
    max_chars = cfg.get("llm.extract_max_chars", 60000)
    if len(text) <= max_chars:
        return SectionSelection(text=text, sections_found=["full_document"],
                                chunked=False, original_chars=len(text))

    head_chars = cfg.get("llm.section_head_chars", 8000)
    per_section = cfg.get("llm.section_max_chars", 15000)
    parts: list[str] = [text[:head_chars]]
    found: list[str] = ["document_head"]
    budget = max_chars - head_chars

    for name, pattern in SECTION_PATTERNS:
        if budget <= 2000:
            break
        matches = list(pattern.finditer(text))
        # Skip table-of-contents hits: prefer the LAST match (the body section);
        # TOC entries cluster in the first ~5% of the document.
        body_matches = [m for m in matches if m.start() > len(text) * 0.05] or matches
        if not body_matches:
            continue
        m = body_matches[-1]
        chunk = _capture(text, m.start(), min(per_section, budget))
        if len(chunk.strip()) < 300:
            continue
        parts.append(f"\n\n===== SECTION: {name.upper()} (from source document) =====\n{chunk}")
        found.append(name)
        budget -= len(chunk)

    if len(found) == 1:  # nothing matched — heuristic missed; fall back honestly
        log.warning("section chunking found NO target sections in %d-char document; "
                    "sending head+tail sample", len(text))
        tail = text[-(max_chars - head_chars) // 2:]
        parts.append("\n\n===== DOCUMENT TAIL (no sections matched) =====\n" + tail)
        found.append("tail_fallback")

    selection = "".join(parts)[:max_chars]
    return SectionSelection(text=selection, sections_found=found,
                            chunked=True, original_chars=len(text))
