"""AB 869 seismic compliance plan parser -- reads
docs/hcai/ab869/raw/{perm_id}/compliance_plan_expanded.pdf (the Playwright
second pass's "This View" export, sections pre-expanded) and extracts what
that specific PDF export can actually support.

THIS PDF'S TEXT LAYER HAS A REAL, DISCLOSED DEFECT, confirmed against the
fixture (docs/hcai/ab869/10049.pdf) by direct pdfplumber word-position
inspection, not assumed: wherever Tableau's PDF export renders a group of
SHORT, independently-positioned text objects (a bulleted list, a table
header, an adjacent-narrow-column table cell, two lines of a wrapped cell,
or -- confirmed on this exact document -- a sentence that happens to share
a Y-coordinate with an unrelated citation footer), those objects' glyphs
land at overlapping or near-identical Y positions instead of properly
incrementing, and pdfplumber's word-grouping merges characters from two
unrelated strings into single garbled tokens (e.g. "1 - Main Hospital"
interleaved with a stray "5" and "2" from the adjacent SPC/NPC columns
becomes "1H o- sMpaitianl5"; "Regulatory Deadlines" interleaved with a
citation becomes "RMNMDJcoa..."). LONG, single-flow paragraph text (the
plan-status paragraph, the delay-section prose) is generally NOT affected,
because it lives in one continuous text run rather than a set of
independently-positioned short objects -- confirmed clean on the fixture.

Given this, the module does NOT attempt to re-derive fields Scout already
has from a more reliable source:
  - building name, SPC rating, NPC rating: sourced from HospitalBuilding
    via the (perm_id, building_nbr) join -- confirmed 99.5% match rate
    against the crosstab's own Building No. values in the same
    investigation that produced this module. The PDF's own Bldg Name/SPC/
    NPC columns are the single most severely garbled region on the page
    (narrow columns immediately adjacent to a long, often-wrapping Bldg
    Name column) and are never read from the PDF at all.
  - compliance_type, plan-level status (Application Status), and five of
    the six ownership/financial lines: sourced from the crosstab CSV
    (docs/hcai/ab869/raw/{perm_id}/crosstab.csv, already fetched and
    validated) -- three of its own column headers are the LITERAL question
    text of three of the six PDF ownership lines ("Who manages the
    hospital? ", "What type of entity manages this hospital? ", "Who is
    financially responsible for the seismic upgrades?"), which is strong
    independent confirmation the crosstab is the right source for these,
    not a guess. The sixth line ("Other contact financially obligated for
    infrastructure improvements") has no crosstab equivalent and the PDF's
    own ownership-lines block is confirmed garbled the same way as the
    table headers -- it is NULLed with that reason, not guessed.
  - plan status fallback: 43 of 201 in-territory facilities carry no
    Application Status value in their crosstab at all (confirmed by
    direct count) -- for those, and ONLY those, this module falls back to
    the PDF's own "Status: {text}" header line, which reconstructs cleanly
    (single continuous text run, confirmed on the fixture).

What IS read from the PDF, because nothing else in Scout has it:
  - the plan-status paragraph following "Status: {text}"
  - the Compliance Method table's building_no (anchor, always clean),
    narrative, and HCAI comment (both extracted by X-range column slicing,
    each candidate word checked for garbling -- see _is_garbled_word)
  - the milestone table's building_no, milestone_type, completion_date,
    description, and HCAI comment (same column-slicing + per-word
    garbling check; met_by_HCAI is read from the "Has Milestone been met?"
    column the same way)
  - the delay-request section's free text (single-flow paragraph -- clean)
  - the AB 869 letter link, via pdfplumber's own hyperlink annotations
    (page.hyperlinks), which are completely unaffected by the text-layer
    defect above since they are a separate PDF object type

GARBLING DETECTION (_is_garbled_word): tuned empirically against the
fixture's own confirmed-clean and confirmed-garbled word lists (11/12
known-garbled tokens caught, 0/53 false positives on a realistic
vocabulary sample for this domain -- regulatory/medical-facility English,
codes, and dates). A word is garbled if it contains a lowercase letter
immediately followed by an uppercase letter (a case-transition pattern
that essentially never occurs in this domain's real vocabulary, dates, or
codes) or five or more consecutive consonants (a sequence real English
words essentially never produce). Known limitation, accepted: a genuine
PascalCase proper noun (e.g. "McDonald") would be wrongly flagged -- no
such name appears in this corpus. If ANY word in an extracted column
region is garbled, the WHOLE region is NULLed with a reason rather than
partially kept, per "every extracted value is checked back against the
source words or stays NULL with a reason."

COLUMN BOUNDARIES were measured directly off the fixture's own word
x-coordinates (see the module's test suite for the exact figures) and are
a per-template calibration, not a per-document one -- every facility's PDF
is generated from the same Tableau workbook layout, so the same
boundaries are used for every document. If a future template change moves
these columns, the fix is here, not per-document.

KNOWN, DISCLOSED RESIDUAL GAP: _is_garbled_word is a tuned lexical
heuristic (case-transition + consonant-run + short-fragment-density), not
a proof. Confirmed on the fixture: it catches the large majority of
garbled tokens but not all of them -- a garbled multi-syllable word with
neither a case transition nor a 5+ consonant run (e.g. one confirmed
example, a scrambled "Relocate services" reading as
"Rbueillodcinagte(s") can pass through undetected. There is no ground
truth to check field values against beyond the fixture itself, so this is
an accepted, disclosed risk rather than a solved problem -- report a
sample of extracted values for a human spot-check before trusting this
corpus for anything higher-stakes than a call list.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pdfplumber
from sqlmodel import Session, delete, select

from app.models import Ab869Building, Ab869Milestone, Ab869Plan, HospitalBuilding, SourceRun, utcnow

log = logging.getLogger(__name__)

AB869_SOURCE = "ab869_compliance_plans"

_VOWELS = set("aeiouAEIOU")
_BLD_RE = re.compile(r"^BLD-\d+$")
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

# Compliance Method table column boundaries (x0, in points), measured on
# the fixture's own data rows -- see module docstring.
COMPLIANCE_COLS = {
    "building_no": (30, 50),
    # name/spc/npc (50-76) deliberately not read -- see module docstring.
    "compliance_type": (76, 101),
    "narrative": (101, 190),
    "hcai_comment": (190, 262),
}

# Milestone table column boundaries, same calibration discipline.
MILESTONE_COLS = {
    "building_no": (30, 50),
    "milestone_type": (50, 77),
    "description": (77, 112),
    "completion_date": (112, 126),
    "hcai_comment": (126, 190),
    "met_by_hcai": (190, 262),
}


def _has_case_transition(word: str) -> bool:
    letters = [c for c in word if c.isalpha()]
    return any(a.islower() and b.isupper() for a, b in zip(letters, letters[1:]))


def _max_consonant_run(word: str) -> int:
    run = longest = 0
    for c in word:
        if c.isalpha() and c not in _VOWELS:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _is_garbled_word(word: str) -> bool:
    """See module docstring's GARBLING DETECTION section."""
    return _has_case_transition(word) or _max_consonant_run(word) >= 5


def _cluster_lines(words: list[dict], tolerance: float = 1.0) -> list[tuple[float, list[dict]]]:
    """Group words sharing a close 'top' into one physical line, left to
    right. Returns [(top, [word_dicts])], in top order. This is the base
    primitive for any single-flow paragraph text on the page -- NOT used
    for table cells, which are extracted by column x-range instead (see
    _column_text) because two adjacent columns can share the same top."""
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[tuple[float, list[dict]]] = []
    current: list[dict] = []
    current_top = None
    for w in ordered:
        if current_top is None or abs(w["top"] - current_top) <= tolerance:
            current.append(w)
            current_top = w["top"] if current_top is None else current_top
        else:
            lines.append((current_top, sorted(current, key=lambda w: w["x0"])))
            current, current_top = [w], w["top"]
    if current:
        lines.append((current_top, sorted(current, key=lambda w: w["x0"])))
    return lines


def _line_clean_text(line_words: list[dict]) -> tuple[str | None, str | None]:
    """(text, reason) -- text is None if any word on the line is garbled,
    OR if the "line" is a single non-alphabetic fragment (a stray citation
    symbol like "§" has no letters, so it never trips the garbled-word
    check on its own -- confirmed on the fixture: a top-tolerance edge
    case split a garbled citation line so only the bare "§" landed in its
    own cluster, which then read as a clean one-word "paragraph line").
    A single real word (no spaces) is accepted -- multi-word lines need at
    least 2 words to guard against exactly this kind of fragment."""
    texts = [w["text"] for w in line_words]
    bad = [t for t in texts if _is_garbled_word(t)]
    if bad:
        return None, f"garbled word(s) on this line: {bad!r}"
    if not any(any(c.isalpha() for c in t) for t in texts):
        return None, "line contains no alphabetic content -- likely a stray fragment"
    # A run of many 1-3 char fragments ("hh ee s e cl mm ea nin s") is the
    # OTHER garbling signature the per-word check misses -- each fragment
    # is individually too short to trip a case-transition or consonant-run
    # test, but real prose does not produce many consecutive tiny tokens.
    # Confirmed on the fixture: this exact pattern survived the per-word
    # check untouched and leaked into the reconstructed paragraph.
    if len(texts) > 5 and (sum(len(t) for t in texts) / len(texts)) < 2.5:
        return None, "line is mostly 1-3 character fragments -- likely garbled"
    return " ".join(texts), None


def _column_text(words: list[dict], top: float, x_range: tuple[float, float],
                 tolerance: float = 3.0) -> tuple[str | None, str | None]:
    """Words whose x0 falls in x_range, on a line close to `top` (allowing
    a couple points of wrap slack within the SAME row band, not a second
    physical line -- multi-line wrapped cells are exactly the case this
    module cannot reliably reconstruct, see module docstring). Returns
    (text, reason); text is None if the slice is empty or contains any
    garbled word."""
    lo, hi = x_range
    cell = sorted(
        (w for w in words if lo <= w["x0"] < hi and abs(w["top"] - top) <= tolerance),
        key=lambda w: (w["top"], w["x0"]),
    )
    if not cell:
        return None, "no words found in this column's position"
    texts = [w["text"] for w in cell]
    bad = [t for t in texts if _is_garbled_word(t)]
    if bad:
        return None, f"garbled word(s) in this column: {bad!r}"
    if len(texts) > 5 and (sum(len(t) for t in texts) / len(texts)) < 2.5:
        return None, "mostly 1-3 character fragments -- likely garbled"
    return " ".join(texts), None


@dataclass
class ComplianceRow:
    building_no: str
    compliance_type: str | None = None
    compliance_type_reason: str | None = None
    narrative: str | None = None
    narrative_reason: str | None = None
    hcai_comment: str | None = None
    hcai_comment_reason: str | None = None


@dataclass
class MilestoneRow:
    building_no: str
    milestone_type: str | None = None
    milestone_type_reason: str | None = None
    description: str | None = None
    description_reason: str | None = None
    completion_date: str | None = None
    completion_date_reason: str | None = None
    hcai_comment: str | None = None
    hcai_comment_reason: str | None = None
    met_by_hcai: str | None = None
    met_by_hcai_reason: str | None = None


@dataclass
class Ab869ParseResult:
    perm_id: str
    plan_status: str | None = None
    plan_status_source: str | None = None  # "pdf_header"
    plan_status_paragraph: str | None = None
    plan_status_paragraph_reason: str | None = None
    compliance_rows: list[ComplianceRow] = field(default_factory=list)
    milestone_rows: list[MilestoneRow] = field(default_factory=list)
    delay_text: str | None = None
    delay_text_reason: str | None = None
    ab869_letter_url: str | None = None
    parse_error: str | None = None


def _find_anchors(words: list[dict], pattern: re.Pattern,
                  x_range: tuple[float, float] | None = None) -> list[dict]:
    """BLD-xxxxx tokens matching `pattern`, restricted to `x_range` when
    given. Restriction matters: this exact tag also appears INSIDE
    narrative sentences ("Building BLD-02468 requires...") elsewhere on
    the same row -- without restricting to the Building No. column's own
    x-position, that self-reference creates a spurious duplicate row.
    Confirmed on the fixture: BLD-02468, BLD-02469, BLD-07280 and BLD-07388
    each appeared twice before this restriction was added."""
    hits = [w for w in words if pattern.match(w["text"])]
    if x_range is None:
        return hits
    lo, hi = x_range
    return [w for w in hits if lo <= w["x0"] < hi]


def _extract_status(words: list[dict]) -> tuple[str | None, str, str | None, str | None]:
    """Returns (status, paragraph, paragraph_reason, source). The status
    line ("Status: X") and its following paragraph are single-flow text --
    confirmed clean on the fixture -- so this uses line-clustering, not
    column-slicing."""
    lines = _cluster_lines(words, tolerance=1.0)
    status = None
    status_idx = None
    for i, (top, line_words) in enumerate(lines):
        text, reason = _line_clean_text(line_words)
        if text and text.startswith("Status:"):
            status = text[len("Status:"):].strip()
            status_idx = i
            break
    if status_idx is None:
        return None, None, "no 'Status:' line found or it was garbled", None

    paragraph_parts = []
    skipped = 0
    for top, line_words in lines[status_idx + 1:]:
        text, line_reason = _line_clean_text(line_words)
        if text is None:
            # Skip this one line, not the rest of the paragraph -- a single
            # line colliding with an unrelated citation must not silently
            # discard every clean sentence after it. Confirmed on the
            # fixture: the sentence right after "Status:" collides with a
            # Health & Safety Code citation, but the sentence after THAT
            # ("The facility shall complete...") is clean on its own.
            skipped += 1
            continue
        # Stop at the next major section boundary.
        if "Click to" in text or text.startswith("Summary Schedule"):
            break
        paragraph_parts.append(text)
    paragraph = "\n\n".join(paragraph_parts) if paragraph_parts else None
    reason = None
    if skipped:
        reason = f"{skipped} line(s) in this paragraph were garbled and skipped"
    elif paragraph is None:
        reason = "no paragraph text found after the status line"
    return status, paragraph, reason, "pdf_header"


def _extract_compliance_table(words: list[dict]) -> list[ComplianceRow]:
    anchors = _find_anchors(words, _BLD_RE, COMPLIANCE_COLS["building_no"])
    rows = []
    for anchor in anchors:
        top = anchor["top"]
        row = ComplianceRow(building_no=anchor["text"])
        row.compliance_type, row.compliance_type_reason = _column_text(
            words, top, COMPLIANCE_COLS["compliance_type"])
        row.narrative, row.narrative_reason = _column_text(
            words, top, COMPLIANCE_COLS["narrative"])
        row.hcai_comment, row.hcai_comment_reason = _column_text(
            words, top, COMPLIANCE_COLS["hcai_comment"])
        rows.append(row)
    return rows


def _extract_milestone_table(words: list[dict]) -> list[MilestoneRow]:
    # The milestone table's own BLD-xxxxx anchors sit further down the page
    # than the Compliance Method table's -- both tables reuse the exact same
    # tag format, so anchors are disambiguated by page region: everything
    # after the Compliance Method table's own last row. Callers pass the
    # already-scoped word list (see parse_pdf).
    anchors = _find_anchors(words, _BLD_RE, MILESTONE_COLS["building_no"])
    rows = []
    for anchor in anchors:
        top = anchor["top"]
        row = MilestoneRow(building_no=anchor["text"])
        row.milestone_type, row.milestone_type_reason = _column_text(
            words, top, MILESTONE_COLS["milestone_type"])
        row.description, row.description_reason = _column_text(
            words, top, MILESTONE_COLS["description"])
        raw_date, date_reason = _column_text(words, top, MILESTONE_COLS["completion_date"])
        if raw_date and not _DATE_RE.match(raw_date.strip()):
            raw_date, date_reason = None, f"did not match a date pattern: {raw_date!r}"
        row.completion_date, row.completion_date_reason = raw_date, date_reason
        row.hcai_comment, row.hcai_comment_reason = _column_text(
            words, top, MILESTONE_COLS["hcai_comment"])
        row.met_by_hcai, row.met_by_hcai_reason = _column_text(
            words, top, MILESTONE_COLS["met_by_hcai"])
        rows.append(row)
    return rows


MAX_DELAY_SECTION_LINES = 4  # see docstring below


def _extract_delay_section(words: list[dict]) -> tuple[str | None, str | None]:
    """The section that follows this one (the six ownership Q&A lines) is
    confirmed garbled the same way as the table headers (see module
    docstring) -- its own header text ("Who owns this facility?") almost
    never reconstructs cleanly enough to serve as a reliable stop-boundary.
    Rather than run past it into scrambled ownership text, this caps
    collection at MAX_DELAY_SECTION_LINES clean lines, sized generously
    against the two real shapes observed (a 2-sentence "no application on
    file" notice, or a short delay-request narrative) -- not a text-content
    boundary, a line-count one, precisely because the text-content
    boundary is one of the confirmed-unreliable regions."""
    lines = _cluster_lines(words, tolerance=1.0)
    delay_idx = None
    for i, (top, line_words) in enumerate(lines):
        text, _ = _line_clean_text(line_words)
        if text and text.strip() == "Facility's Request for Delay - Click to Hide":
            delay_idx = i
            break
    if delay_idx is None:
        return None, "'Facility's Request for Delay' section header not found or was garbled"

    parts = []
    skipped = 0
    for top, line_words in lines[delay_idx + 1:]:
        if len(parts) >= MAX_DELAY_SECTION_LINES:
            break
        text, line_reason = _line_clean_text(line_words)
        if text is None:
            skipped += 1
            continue
        if "Click to" in text:
            break
        parts.append(text)
    if not parts:
        return None, (f"{skipped} line(s) were garbled and skipped, none survived"
                      if skipped else "no delay-section text found")
    reason = f"{skipped} line(s) in this section were garbled and skipped" if skipped else None
    return "\n\n".join(parts), reason


def parse_pdf(path: str | Path) -> Ab869ParseResult:
    """Parse one compliance_plan_expanded.pdf. Never raises for a bad or
    unreadable file -- returns a result with parse_error set, same
    discipline as every other document-ingestion path in this app."""
    path = Path(path)
    perm_id = path.parent.name
    result = Ab869ParseResult(perm_id=perm_id)
    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                result.parse_error = "PDF has no pages"
                return result
            page = pdf.pages[0]
            words = page.extract_words()

            (result.plan_status, result.plan_status_paragraph,
             result.plan_status_paragraph_reason, result.plan_status_source) = _extract_status(words)

            # Split the page into "Compliance Method" vs "Milestone" table
            # regions using the section header lines as boundaries -- both
            # tables use identical BLD-xxxxx anchors, so anchors must be
            # scoped by region or milestone rows would double with
            # compliance rows.
            lines = _cluster_lines(words, tolerance=1.0)
            compliance_start = milestone_start = milestone_end = None
            for top, line_words in lines:
                text, _ = _line_clean_text(line_words)
                if not text:
                    continue
                if text.startswith("Compliance Type Tables") and compliance_start is None:
                    compliance_start = top
                if text.startswith("Building Milestone Description Table") and milestone_start is None:
                    milestone_start = top
                if text.startswith("Facility's Request for Delay") and milestone_end is None:
                    milestone_end = top

            if compliance_start is not None:
                hi = milestone_start if milestone_start is not None else 1e9
                compliance_words = [w for w in words if compliance_start <= w["top"] < hi]
                result.compliance_rows = _extract_compliance_table(compliance_words)
            if milestone_start is not None:
                hi = milestone_end if milestone_end is not None else 1e9
                milestone_words = [w for w in words if milestone_start <= w["top"] < hi]
                result.milestone_rows = _extract_milestone_table(milestone_words)

            result.delay_text, result.delay_text_reason = _extract_delay_section(words)

            try:
                links = page.hyperlinks
            except Exception:  # noqa: BLE001 -- some malformed annotation objects raise; a missing link is not fatal
                links = []
            for link in links:
                uri = link.get("uri") or ""
                if "ab" in uri.lower() and "869" in uri.lower() or "letter" in uri.lower():
                    result.ab869_letter_url = uri
                    break
            if result.ab869_letter_url is None and links:
                # Only one link is expected on this page (the letter, when
                # a delay was actually requested) -- take it if the
                # keyword match above missed an unexpected URL shape.
                result.ab869_letter_url = links[0].get("uri")
    except Exception as exc:  # noqa: BLE001 -- a bad PDF must not kill a batch run
        result.parse_error = f"{type(exc).__name__}: {exc}"
    return result


# ---- crosstab CSV: the reliable source for compliance_type, plan status, ---
# ---- and five of the six ownership lines -- see module docstring. --------

DELAY_NOT_RECEIVED_PHRASE = "hcai has not received an application for delay from this facility"


@dataclass
class CrosstabFacility:
    plan_status: str | None = None
    plan_status_ambiguous_values: set = field(default_factory=set)
    owner_name: str | None = None
    owner_type: str | None = None
    manager_name: str | None = None
    manager_type: str | None = None
    financially_responsible_party: str | None = None
    buildings: dict = field(default_factory=dict)  # building_nbr -> compliance_type
    missed_milestone_buildings: set = field(default_factory=set)  # building_nbr with Progress == "Missed Milestone(s)"


def read_crosstab(path: str | Path) -> CrosstabFacility:
    """Parse one facility's crosstab.csv. Rows repeat per progress bucket
    (confirmed: a building's own compliance_type/ownership fields are
    constant across its repeated rows) -- this collapses to one value per
    field, and to one compliance_type per DISTINCT building_nbr, matching
    the same dedupe discipline used to produce the corrected Phase C
    numbers this module's own investigation reported."""
    facility = CrosstabFacility()
    text = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    status_values = set()
    for row in reader:
        bno = (row.get("Building No.") or "").strip()
        ctype = (row.get("Compliance Type (Reported by Hosptial)") or "").strip()
        if bno and ctype and bno not in facility.buildings:
            facility.buildings[bno] = ctype
        if bno and (row.get("Progress") or "").strip() == "Missed Milestone(s)":
            facility.missed_milestone_buildings.add(bno)

        app_status = (row.get("Application Status") or "").strip()
        if app_status:
            status_values.add(app_status)

        for attr, col in (
            ("owner_name", "Owner Name"), ("owner_type", "Ownership"),
            ("manager_name", "Who manages the hospital? "),
            ("manager_type", "What type of entity manages this hospital? "),
            ("financially_responsible_party", "Who is financially responsible for the seismic upgrades?"),
        ):
            val = (row.get(col) or "").strip()
            if val and getattr(facility, attr) is None:
                setattr(facility, attr, val)

    if len(status_values) == 1:
        facility.plan_status = next(iter(status_values))
    elif len(status_values) > 1:
        # Confirmed on the real corpus: perm_id 11966 carries both "Out to
        # Applicant" and "Not Approved" across its own rows -- genuinely
        # ambiguous, not a parsing bug. Null rather than guess which one
        # is current.
        facility.plan_status_ambiguous_values = status_values
    return facility


def import_ab869(session: Session, raw_dir: str = "docs/hcai/ab869/raw") -> dict:
    """Import every facility under `raw_dir` that has both a crosstab.csv
    and a compliance_plan_expanded.pdf. Idempotent on (perm_id,
    source_pdf_hash): a facility whose PDF bytes are unchanged since the
    last import is skipped entirely (not re-parsed, not re-written); a
    changed PDF (Andrew re-pulled it) replaces that facility's plan +
    buildings + milestones, same replace-not-append discipline as
    Signal/ScheduleEntry's own one-per-source idiom.

    Records its own SourceRun (source=AB869_SOURCE) so `scout doctor` /
    source_health can see it -- see config.yaml's sources.ab869_compliance_plans
    entry for why this is NOT scheduled and carries an episodic stale_hours.
    """
    run = SourceRun(source=AB869_SOURCE)
    session.add(run)
    session.commit()

    stats = {"facilities_seen": 0, "facilities_imported": 0, "facilities_skipped_unchanged": 0,
             "facilities_no_crosstab_file": 0, "facilities_no_plan_on_file": 0, "buildings_written": 0, "milestones_written": 0,
             "errors": []}
    error = None
    try:
        base = Path(raw_dir)
        if not base.is_dir():
            raise ValueError(f"raw directory does not exist: {raw_dir!r}")

        territory_counties = None
        try:
            from app.config import load_config
            cfg = load_config()
            territory_counties = set(cfg.get("territories.california.counties", []))
        except Exception:  # noqa: BLE001 -- county denormalization is best-effort context, not required
            pass

        for perm_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            perm_id = perm_dir.name
            pdf_path = perm_dir / "compliance_plan_expanded.pdf"
            crosstab_path = perm_dir / "crosstab.csv"
            if not crosstab_path.exists():
                stats["facilities_no_crosstab_file"] += 1
                continue

            try:
                crosstab = read_crosstab(crosstab_path)
            except Exception as exc:  # noqa: BLE001 -- one bad facility must not kill the whole import
                stats["errors"].append({"perm_id": perm_id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            has_any_content = (crosstab.buildings or crosstab.plan_status
                              or crosstab.plan_status_ambiguous_values or crosstab.owner_name)
            if not has_any_content:
                # An empty crosstab (2-byte "\r\n", no header, no rows) --
                # HCAI's own filter matched nothing for this facility name,
                # meaning no compliance plan on file. Nothing to import;
                # this is the "never filed" case, not a technical gap.
                #
                # Deliberately NOT gated on crosstab.buildings alone: a
                # facility can carry a real plan_status with ZERO
                # buildings -- confirmed real, perm_id 10028's own
                # Application Status is "Already 2030 compliant, no need
                # to submit a compliance plan" per PIN 80's own exemption
                # rule for 2030-compliant facilities. That is a genuine,
                # informative status, not "nothing to import" -- checking
                # buildings alone would have silently discarded it.
                stats["facilities_no_plan_on_file"] += 1
                continue
            stats["facilities_seen"] += 1

            # A facility can have real crosstab rows (building_nbr,
            # compliance_type -- confirmed real for perm_ids 10685 and
            # 17905) with NO pdf on disk (the Playwright pass failed for
            # exactly those two, see the investigation report). Losing
            # their otherwise-real crosstab data because of an unrelated
            # PDF gap would silently shrink the join numbers for no good
            # reason -- import what the crosstab has, and record every
            # PDF-only field (narrative, milestones, delay text, the
            # status paragraph) as null with an honest reason instead.
            pdf_exists = pdf_path.exists()
            if pdf_exists:
                pdf_bytes = pdf_path.read_bytes()
                pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
            else:
                pdf_hash = "no_pdf_on_disk"

            existing = session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == perm_id)).first()
            if existing is not None and existing.source_pdf_hash == pdf_hash:
                stats["facilities_skipped_unchanged"] += 1
                continue

            if pdf_exists:
                try:
                    parsed = parse_pdf(pdf_path)
                except Exception as exc:  # noqa: BLE001 -- one bad facility must not kill the whole import
                    stats["errors"].append({"perm_id": perm_id, "error": f"{type(exc).__name__}: {exc}"})
                    continue
            else:
                parsed = Ab869ParseResult(
                    perm_id=perm_id,
                    parse_error=None,  # not a parse failure -- there is simply no PDF to parse
                    plan_status_paragraph_reason="no PDF on disk for this facility (Playwright pull failed)",
                    delay_text_reason="no PDF on disk for this facility (Playwright pull failed)",
                )

            county = None
            if territory_counties is not None:
                hb = session.exec(
                    select(HospitalBuilding.county).where(HospitalBuilding.perm_id == perm_id)
                ).first()
                county = hb

            plan_status = crosstab.plan_status
            plan_status_source = "crosstab" if plan_status else None
            plan_status_reason = None
            if crosstab.plan_status_ambiguous_values:
                plan_status_reason = (f"ambiguous: crosstab carries multiple distinct Application "
                                      f"Status values {sorted(crosstab.plan_status_ambiguous_values)!r}")
            if not plan_status and parsed.plan_status:
                plan_status = parsed.plan_status
                plan_status_source = "pdf_header"

            delay_requested = None
            if parsed.delay_text:
                delay_requested = DELAY_NOT_RECEIVED_PHRASE not in parsed.delay_text.lower()

            if existing is not None:
                session.exec(delete(Ab869Building).where(Ab869Building.perm_id == perm_id))
                session.exec(delete(Ab869Milestone).where(Ab869Milestone.perm_id == perm_id))
                plan = existing
            else:
                plan = Ab869Plan(perm_id=perm_id, source_pdf_path=str(pdf_path), source_pdf_hash=pdf_hash)

            plan.county = county
            plan.plan_status = plan_status
            plan.plan_status_source = plan_status_source
            plan.plan_status_paragraph = parsed.plan_status_paragraph
            plan.plan_status_paragraph_reason = plan_status_reason or parsed.plan_status_paragraph_reason
            plan.delay_text = parsed.delay_text
            plan.delay_text_reason = parsed.delay_text_reason
            plan.delay_requested = delay_requested
            plan.ab869_letter_url = parsed.ab869_letter_url
            plan.owner_name = crosstab.owner_name
            plan.owner_type = crosstab.owner_type
            plan.manager_name = crosstab.manager_name
            plan.manager_type = crosstab.manager_type
            plan.financially_responsible_party = crosstab.financially_responsible_party
            plan.other_financial_contact = None
            plan.other_financial_contact_reason = ("no reliable source: the PDF's own ownership-lines "
                                                    "block is confirmed garbled the same way as the "
                                                    "table headers, and the crosstab carries no "
                                                    "equivalent field for this specific line")
            plan.source_pdf_path = str(pdf_path)
            plan.source_pdf_hash = pdf_hash
            plan.crosstab_path = str(crosstab_path)
            plan.imported_at = utcnow()
            session.add(plan)
            session.commit()
            session.refresh(plan)

            # Buildings: crosstab's own building/compliance_type list is
            # the union of everything seen -- PDF-only narrative/comment
            # merged in by building_nbr where the PDF found a matching row.
            pdf_rows_by_building = {r.building_no: r for r in parsed.compliance_rows}
            for building_nbr, compliance_type in crosstab.buildings.items():
                pdf_row = pdf_rows_by_building.get(building_nbr)
                session.add(Ab869Building(
                    perm_id=perm_id, building_nbr=building_nbr, compliance_type=compliance_type,
                    narrative=pdf_row.narrative if pdf_row else None,
                    narrative_reason=(pdf_row.narrative_reason if pdf_row
                                     else "no matching row in the PDF's Compliance Method table"),
                    hcai_comment=pdf_row.hcai_comment if pdf_row else None,
                    hcai_comment_reason=(pdf_row.hcai_comment_reason if pdf_row
                                         else "no matching row in the PDF's Compliance Method table"),
                    has_missed_milestone=building_nbr in crosstab.missed_milestone_buildings,
                ))
                stats["buildings_written"] += 1

            for m in parsed.milestone_rows:
                completion_date = None
                if m.completion_date:
                    try:
                        completion_date = datetime.strptime(m.completion_date.strip(), "%m/%d/%Y")
                    except ValueError:
                        pass
                session.add(Ab869Milestone(
                    perm_id=perm_id, building_nbr=m.building_no,
                    milestone_type=m.milestone_type, milestone_type_reason=m.milestone_type_reason,
                    description=m.description, description_reason=m.description_reason,
                    completion_date_text=m.completion_date, completion_date=completion_date,
                    completion_date_reason=m.completion_date_reason,
                    hcai_comment=m.hcai_comment, hcai_comment_reason=m.hcai_comment_reason,
                    met_by_hcai=m.met_by_hcai, met_by_hcai_reason=m.met_by_hcai_reason,
                ))
                stats["milestones_written"] += 1

            session.commit()
            stats["facilities_imported"] += 1

    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("AB 869 import failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = stats["facilities_seen"]
    run.records_new = stats["facilities_imported"]
    run.ok = error is None and not stats["errors"]
    run.error = error or (f"{len(stats['errors'])} facilit(y/ies) failed to parse" if stats["errors"] else None)
    session.add(run)
    session.commit()

    if error:
        raise RuntimeError(error)
    return stats


# ---- /ab869 board + facility drill-down --------------------------------

NO_PLAN_ON_FILE = "No plan on file"

# milestone_type substrings this board treats as "the next thing that
# matters" -- both carry a real regulatory deadline (see PIN 80's own NPC
# schedule: permits by 3/1/2028, construction commencement implied by the
# same schedule), unlike a generic "Construction Milestone" progress note.
UPCOMING_MILESTONE_KEYWORDS = ("permit issuance", "construction commencement")


def ab869_board_rows(session: Session, cfg) -> list[dict]:
    """One row per in-territory facility -- small enough (~200 facilities,
    ~1,500 buildings, ~550 milestones) that this is plain Python over three
    cheap queries, not pushed into SQL; filtering happens in the web layer
    against this same list. Includes a facility with NO Ab869Plan row at
    all (plan_status becomes the literal NO_PLAN_ON_FILE, not hidden) --
    the 11 facilities HCAI's own crosstab filter matched nothing for."""
    from app.pipeline.scaqmd import scaqmd_matches_for_ab869

    territory_counties = set(cfg.get("territories.california.counties", []))

    facilities: dict[str, dict] = {}
    city_by_perm: dict[str, str] = {}
    for perm_id, facility_name, county, city in session.exec(
        select(HospitalBuilding.perm_id, HospitalBuilding.facility_name, HospitalBuilding.county,
              HospitalBuilding.city)
    ).all():
        if county not in territory_counties:
            continue
        facilities.setdefault(perm_id, {"perm_id": perm_id, "facility_name": facility_name, "county": county})
        if perm_id not in city_by_perm and city:
            city_by_perm[perm_id] = city

    # Normalized-facility-NAME match against ScaqmdFacility, restricted to
    # the same city -- not an address match, AB 869/HospitalBuilding carry
    # no street address at all. See app.pipeline.scaqmd's module docstring.
    air_permit_by_perm = scaqmd_matches_for_ab869(
        session, [(pid, f["facility_name"], city_by_perm.get(pid)) for pid, f in facilities.items()])

    plans = {p.perm_id: p for p in session.exec(select(Ab869Plan)).all() if p.perm_id in facilities}

    buildings_by_perm: dict[str, list] = {}
    for b in session.exec(select(Ab869Building)).all():
        if b.perm_id in facilities:
            buildings_by_perm.setdefault(b.perm_id, []).append(b)

    milestones_by_perm: dict[str, list] = {}
    for m in session.exec(select(Ab869Milestone).where(Ab869Milestone.completion_date.is_not(None))).all():
        if m.perm_id in facilities:
            milestones_by_perm.setdefault(m.perm_id, []).append(m)

    now = utcnow()
    rows = []
    for perm_id, base in facilities.items():
        plan = plans.get(perm_id)
        buildings = buildings_by_perm.get(perm_id, [])
        npc_count = sum(1 for b in buildings if b.compliance_type in ("NPC Retrofit", "SPC and NPC Retrofit"))
        missed_count = sum(1 for b in buildings if b.has_missed_milestone)

        next_date = None
        for m in milestones_by_perm.get(perm_id, []):
            mtype = (m.milestone_type or "").lower()
            if (any(k in mtype for k in UPCOMING_MILESTONE_KEYWORDS)
                    and m.completion_date and m.completion_date > now):
                if next_date is None or m.completion_date < next_date:
                    next_date = m.completion_date

        rows.append({
            "perm_id": perm_id,
            "facility_name": base["facility_name"],
            "county": base["county"],
            "npc_building_count": npc_count,
            "plan_status": plan.plan_status if plan and plan.plan_status else (NO_PLAN_ON_FILE if plan is None else None),
            "has_plan": plan is not None,
            "no_pdf_on_disk": bool(plan and plan.plan_status_paragraph_reason
                                   and "no PDF on disk" in plan.plan_status_paragraph_reason),
            "missed_milestone_count": missed_count,
            "delay_requested": plan.delay_requested if plan else None,
            "next_upcoming_date": next_date,
            "financially_responsible_party": plan.financially_responsible_party if plan else None,
            "air_permit_facility_id": air_permit_by_perm.get(perm_id),
        })
    rows.sort(key=lambda r: r["npc_building_count"], reverse=True)
    return rows


def ab869_facility_detail(session: Session, perm_id: str) -> dict:
    """Everything the facility drill-down page needs: the plan row (if
    any), every Ab869Building joined to its own HospitalBuilding row (for
    SPC/NPC/building name -- see module docstring on why those come from
    HospitalBuilding, not the PDF), and every milestone grouped by
    building, sorted by date (undated last, never dropped)."""
    plan = session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == perm_id)).first()
    buildings = session.exec(
        select(Ab869Building).where(Ab869Building.perm_id == perm_id)
        .order_by(Ab869Building.building_nbr)
    ).all()
    milestones = session.exec(
        select(Ab869Milestone).where(Ab869Milestone.perm_id == perm_id)
        .order_by(Ab869Milestone.completion_date.asc().nulls_last())
    ).all()
    milestones_by_building: dict[str, list] = {}
    for m in milestones:
        milestones_by_building.setdefault(m.building_nbr, []).append(m)
    hospital_buildings = {
        hb.building_nbr: hb for hb in session.exec(
            select(HospitalBuilding).where(HospitalBuilding.perm_id == perm_id)).all()
    }
    facility_name = next(iter(hospital_buildings.values())).facility_name if hospital_buildings else None
    return {
        "perm_id": perm_id, "plan": plan, "buildings": buildings,
        "milestones_by_building": milestones_by_building, "hospital_buildings": hospital_buildings,
        "facility_name": facility_name,
    }


def hcai_tableau_url(perm_id: str, facility_name: str | None) -> str:
    """The exact filtered-view URL confirmed live during this feature's own
    access investigation -- 'Facility Number and Name' is the real
    Tableau parameter name (found from the compliance-plan page's own
    instruction text: 'Use the "Facility Number and Name" filter in the
    upper right corner'), value is '{perm_id} {facility_name}', matching
    the exact format confirmed against real facility 10049."""
    import urllib.parse
    value = f"{perm_id} {facility_name}" if facility_name else perm_id
    params = urllib.parse.urlencode({"Facility Number and Name": value})
    return f"https://tab.hcai.ca.gov/t/OSHPD_PUBLIC/views/CompliancePlanWebsite/CompliancePlan?{params}"
