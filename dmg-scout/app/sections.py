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
    body_chars: int = 0  # source chars included, excluding inserted markers

    @property
    def selected_chars(self) -> int:
        return len(self.text)

    @property
    def coverage(self) -> float:
        """Fraction of the source document actually shown to the model.

        Recorded because a fixed budget cannot cover an arbitrarily long document:
        a 400,000-char prospectus gets ~15% however it is sliced. The limit is real
        and unavoidable, so it is reported per document rather than hidden — a
        verdict reached on 15% of a filing is a different kind of claim from one
        reached on all of it.
        """
        if not self.original_chars:
            return 1.0
        return min(1.0, (self.body_chars or len(self.text)) / self.original_chars)


def _capture(text: str, start: int, cap: int) -> str:
    """Capture from a heading until the next heading, bounded by cap chars."""
    window = text[start:start + cap]
    nxt = ANY_HEADING.search(window, pos=200)  # skip the matched heading itself
    return window[:nxt.start()] if nxt else window


def select_triage_text(text: str, cfg: Config) -> SectionSelection:
    """What triage reads. Coverage matters more here than contiguity.

    Triage answers "is a specific building project named ANYWHERE in this
    document", so a head-only read is not a partial answer to that question — it
    is a different question. Sending `text[:6000]` meant 144 of 204 documents in
    the corpus were judged on a fraction of themselves, and 86% of all stored text
    was never seen at triage time at all. An 82,724-char Storey County commission
    packet was ruled "no specific building project" on its first 7%, in the county
    that holds the Tahoe Reno Industrial Center.

    Deliberately NOT the section-aware selection extraction uses. That heuristic
    matches EIR and SEC headings; a county agenda packet has neither, so it falls
    through to a head+tail sample and leaves the middle — where agenda items live —
    just as invisible. Those six fallback documents are exactly the ones that were
    misjudged.

    So: whole document when it fits, and above that the head plus evenly spaced
    windows spanning everything to the last character. Sampled, but with no region
    structurally unreachable.
    """
    max_chars = cfg.get("llm.triage_max_chars", 60000)
    if len(text) <= max_chars:
        return SectionSelection(text=text, sections_found=["full_document"],
                                chunked=False, original_chars=len(text),
                                body_chars=len(text))

    head_chars = min(cfg.get("llm.triage_head_chars", 8000), max_chars // 2)
    # Many small windows beat few large ones for spotting a mention at a fixed
    # budget: a named project runs a few hundred chars, so halving the window
    # doubles the number of places we can catch one.
    window = cfg.get("llm.triage_sample_window_chars", 1200)
    head, rest = text[:head_chars], text[head_chars:]
    n_windows = max(1, (max_chars - head_chars) // window)
    stride = max(1, len(rest) // n_windows)

    parts = [head]
    body = head_chars
    covered_end = 0
    for i in range(n_windows):
        start = i * stride
        if start >= len(rest):
            break
        chunk = rest[start:start + window]
        parts.append(f"\n\n===== EXCERPT at {(head_chars + start) / len(text):.0%} "
                     f"of document =====\n{chunk}")
        body += len(chunk)
        covered_end = start + len(chunk)
    # The last stride lands short of the end whenever len(rest) is not a clean
    # multiple of it, and the tail is exactly where a late-added agenda item goes.
    if covered_end < len(rest):
        tail = rest[-window:]
        parts.append("\n\n===== DOCUMENT TAIL =====\n" + tail)
        body += len(tail)

    selection = SectionSelection(
        text="".join(parts), sections_found=["document_head", "sampled_spread"],
        chunked=True, original_chars=len(text), body_chars=body)
    floor = cfg.get("llm.triage_coverage_warn_below", 0.5)
    if selection.coverage < floor:
        log.warning("triage sees only %.0f%% of a %d-char document (%d-char gaps "
                    "between excerpts): a negative verdict here is weaker evidence "
                    "than on a document read whole",
                    selection.coverage * 100, len(text), max(0, stride - window))
    return selection


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

    # Spend the rest of the budget rather than stopping early. The section patterns
    # are built for EIR and SEC headings; a county agenda packet has neither, so
    # matching would end after the head plus maybe one spurious hit — 8,716 chars of
    # a 65,291-char Storey County packet, against a 60,000 budget. Triage was reading
    # 95% of that same document and correctly reporting a data center campus in it,
    # while extraction saw 13% and returned an unnamed project. Unused budget is not
    # a saving; it is a project dropped.
    used = sum(len(p) for p in parts)
    if used < max_chars * cfg.get("llm.section_topup_below_fraction", 0.8):
        spread = _spanning_sample(text, head_chars, max_chars - used,
                                  cfg.get("llm.section_topup_window_chars", 2500))
        if spread:
            parts.append(spread)
            found.append("spanning_topup")

    selection = "".join(parts)[:max_chars]
    return SectionSelection(text=selection, sections_found=found,
                            chunked=True, original_chars=len(text),
                            body_chars=min(len(selection), len(text)))


def _spanning_sample(text: str, skip_chars: int, budget: int, window: int) -> str:
    """Evenly spaced excerpts across everything past `skip_chars`, within `budget`."""
    rest = text[skip_chars:]
    n = max(0, budget // window)
    if n == 0 or not rest:
        return ""
    stride = max(1, len(rest) // n)
    out = []
    for i in range(n):
        start = i * stride
        if start >= len(rest):
            break
        out.append(f"\n\n===== EXCERPT at {(skip_chars + start) / len(text):.0%} "
                   f"of document =====\n{rest[start:start + window]}")
    return "".join(out)
