"""School-district Division 23 spec parser -- a PILOT, not a Scout source.

Answers one question: do California school district mechanical specs name
manufacturers, and is AAON (or any other DMG line-card name) among them.
See CHARTER-adjacent conversation for the full brief; the short version:

    "A clean 'no' is a good outcome and saves me a quarter."

ACCESS: this reads documents a human placed by hand in `directory`
(default docs/specs-pilot/). It does NOT fetch anything from PlanetBids or
any other network source -- PlanetBids' own Terms and Conditions restrict
use of information obtained through it to completing a bid transaction,
which competitive-intelligence harvesting is outside of. There is
deliberately no fetcher here, this module is not registered under
config.yaml's sources: block, and it has no entry in app.ops.doctor's
source-health checks -- it is not a "source" in Scout's sense at all, it
is a one-shot manual analysis over files a human already has permission to
read.

PERSISTENCE: none. This does not write to the database. It is disposable
pilot analysis, not a Scout pipeline stage -- if the pilot says yes, a real
pipeline (with its own table, matching SpecMention's row-per-mention shape)
is a follow-up decision, not this one. Re-running costs LLM budget again
each time; for a small hand-fed corpus that is the right trade against
building persistence for something that might get thrown away entirely.

REUSE, per the Phase A inventory this was built from:
  - find_ufgs_23_series_text (app.pipeline.sam_gov) is reused UNCHANGED --
    it already handles a combined spec book (a run of consecutive 23-series
    section headings), which is exactly a school bid package's shape.
  - app.officetext.docx_to_text/xlsx_to_text and app.pdftext.pdf_to_text are
    reused UNCHANGED.
  - app.llm.extract_division_23_mentions is reused UNCHANGED for the
    Division 23 section/manufacturer/performance-only extraction itself --
    a Division 23 section is a Division 23 section regardless of whether it
    arrived via SAM.gov or a hand-supplied school bid package.
  - app.llm.extract_school_bid_metadata is NEW: district/project/bid-number/
    bid-date live in a bid package's front matter, which
    extract_division_23_mentions never sees (it is only ever handed the
    already-trimmed 23-series excerpt).
  - app.grounding.name_grounded is wired in explicitly (see below) -- Phase
    A found that app.pipeline.sam_gov's OWN use of extract_division_23_mentions
    had never actually called this, a real gap fixed in the same pass that
    built this module (see app.pipeline.sam_gov's module docstring).

GROUNDING: every manufacturer_name extract_division_23_mentions returns is
checked via name_grounded() against ufgs_text -- the SAME trimmed text the
model was given, never the full document (a model that saw more text than
its own extraction was grounded against would let a real mention from
elsewhere in the document masquerade as evidence for a fabricated one).
An ungrounded name is dropped, not stored, and recorded in
DocumentResult.rejected_ungrounded with its section and mention type.

OUTCOME CLASSES: the four requested -- NAMES_MANUFACTURERS,
PERFORMANCE_SPEC_ONLY, NO_DIVISION_23, PARSE_FAILED -- plus one addition,
UNGROUNDED_MENTIONS_ONLY, disclosed here rather than silently folded into
one of the four: a document where the model asserted manufacturer names but
NONE of them survived grounding is not the same finding as "no Division 23
content" (NO_DIVISION_23 would be false) or "the document itself specifies
by performance only" (performance_spec_only would be a claim the model
never made). Collapsing it into either would misstate what happened on the
one field this whole pilot exists to get right. See OutcomeClass below.

UNTESTED AGAINST REAL DOCUMENTS as of the commit that adds this module.
docs/specs-pilot/ is empty -- no synthetic/fixture documents were created
to test this against (an invented fixture proves nothing about real school
spec language and produced a false-confidence report here before). The
tests in tests/test_specs_pilot.py exercise the CODE PATHS ONLY -- control
flow, grounding rejection, outcome classification, line-card matching --
using minimal hand-written snippets exactly the way app.pipeline.sam_gov's
own test suite mocks its LLM calls. They say nothing about what real
California school district specs actually contain, and no number from
them should ever be reported as if it did.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.grounding import name_grounded
from app.normalize import normalize_name
from app.officetext import docx_to_text, xlsx_to_text
from app.pdftext import pdf_to_text
from app.pipeline.sam_gov import find_ufgs_23_series_text

log = logging.getLogger(__name__)

# Spec books run long -- same override app.pipeline.sam_gov uses over
# app.pdftext's 80-page default (sized for meeting packets, not
# multi-hundred-page construction specifications).
MAX_PDF_PAGES = 600

# How much of the document's OWN head text to hand extract_school_bid_metadata
# -- cover sheets/title blocks/bid forms are near the front, but a bid
# package often has a table of contents and general conditions ahead of the
# project-specific front matter. This is a guess sized generously rather
# than measured against anything real; if real documents put this
# information further in, this constant is the first thing to revisit.
HEAD_CHARS = 6000

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".xlsm")

# The minimum document count below which NO percentage should be reported at
# all -- only raw counts and individual outcomes. Not a measured threshold,
# a standard rule-of-thumb floor for treating a proportion as anything more
# than an anecdote.
MIN_FOR_ANY_PERCENTAGE = 30
# Rough size for a percentage with a realistic shot at +/-10 points of margin
# at typical (95%) confidence on a proportion that could be anywhere near
# 50/50 -- the standard n~96 sample-size formula for that margin, rounded.
# Below this, report the raw counts and say plainly that the percentage is
# not yet defensible, rather than presenting one report_text() computes.
RECOMMENDED_FOR_A_DEFENSIBLE_PERCENTAGE = 100


class OutcomeClass:
    NAMES_MANUFACTURERS = "NAMES_MANUFACTURERS"
    PERFORMANCE_SPEC_ONLY = "PERFORMANCE_SPEC_ONLY"
    NO_DIVISION_23 = "NO_DIVISION_23"
    PARSE_FAILED = "PARSE_FAILED"
    # Not one of the four requested -- see module docstring's OUTCOME CLASSES.
    UNGROUNDED_MENTIONS_ONLY = "UNGROUNDED_MENTIONS_ONLY"


REQUESTED_OUTCOMES = (
    OutcomeClass.NAMES_MANUFACTURERS, OutcomeClass.PERFORMANCE_SPEC_ONLY,
    OutcomeClass.NO_DIVISION_23, OutcomeClass.PARSE_FAILED,
)
ALL_OUTCOMES = REQUESTED_OUTCOMES + (OutcomeClass.UNGROUNDED_MENTIONS_ONLY,)


@dataclass
class MentionResult:
    spec_section: str | None
    spec_section_title: str | None
    manufacturer_name: str
    mention_type: str  # basis_of_design | acceptable_manufacturer
    on_dmg_line_card: bool
    dmg_line_card_name: str | None


@dataclass
class DocumentResult:
    filename: str
    outcome: str
    district: str | None = None
    project_name: str | None = None
    bid_number: str | None = None
    bid_date: str | None = None  # verbatim as written -- never parsed/inferred
    specifying_firm: str | None = None
    sections_present: list[str] = field(default_factory=list)
    mentions: list[MentionResult] = field(default_factory=list)
    rejected_ungrounded: list[str] = field(default_factory=list)  # audit trail, see module docstring
    parse_error: str | None = None
    notes: str | None = None


def _read_document_text(path: Path) -> str:
    """Dispatch on file extension. Raises on a genuinely unreadable file --
    caller records PARSE_FAILED with the reason, never skips silently."""
    ext = path.suffix.lower()
    data = path.read_bytes()
    if ext == ".pdf":
        return pdf_to_text(data, max_pages=MAX_PDF_PAGES)
    if ext == ".docx":
        return docx_to_text(data)
    if ext in (".xlsx", ".xlsm"):
        return xlsx_to_text(data)
    raise ValueError(f"unsupported file type: {path.suffix!r}")


def _line_card_names_norm(cfg) -> dict[str, str]:
    lines = cfg.get("accounts.line_card", []) or []
    return {normalize_name(e["name"]): e["name"] for e in lines}


def process_document(path: Path, cfg) -> DocumentResult:
    """One document, start to finish. A bad file, a bad LLM call, or an
    empty text layer never raises past this function -- every failure mode
    becomes a DocumentResult with outcome=PARSE_FAILED and a reason, per the
    "record PARSE_FAILED with the reason rather than skipping it silently"
    instruction."""
    filename = path.name
    try:
        text = _read_document_text(path)
    except Exception as exc:  # noqa: BLE001 -- a bad file must not kill the whole run
        return DocumentResult(filename=filename, outcome=OutcomeClass.PARSE_FAILED,
                              parse_error=f"{type(exc).__name__}: {exc}")
    if not text or not text.strip():
        return DocumentResult(
            filename=filename, outcome=OutcomeClass.PARSE_FAILED,
            parse_error="no text layer found -- this file may be a scanned image with no OCR")

    from app.llm import extract_division_23_mentions, extract_school_bid_metadata

    try:
        meta = extract_school_bid_metadata(text[:HEAD_CHARS], filename=filename)
    except Exception as exc:  # noqa: BLE001 -- one bad document must not kill the whole run
        return DocumentResult(filename=filename, outcome=OutcomeClass.PARSE_FAILED,
                              parse_error=f"metadata extraction failed: {type(exc).__name__}: {exc}")

    result = DocumentResult(
        filename=filename, outcome=OutcomeClass.NO_DIVISION_23,  # provisional, set for real below
        district=meta.get("district"), project_name=meta.get("project_name"),
        bid_number=meta.get("bid_number"), bid_date=meta.get("bid_date"),
    )

    ufgs_text = find_ufgs_23_series_text(text)
    if not ufgs_text:
        return result  # outcome stays NO_DIVISION_23

    try:
        division23 = extract_division_23_mentions(ufgs_text, title=filename)
    except Exception as exc:  # noqa: BLE001
        result.outcome = OutcomeClass.PARSE_FAILED
        result.parse_error = f"Division 23 extraction failed: {type(exc).__name__}: {exc}"
        return result

    result.specifying_firm = division23.get("specifying_firm")
    line_card_names = _line_card_names_norm(cfg)

    for section in division23.get("sections", []):
        spec_section = section.get("spec_section")
        if spec_section:
            result.sections_present.append(spec_section)
        mentions = []
        if section.get("basis_of_design_manufacturer"):
            mentions.append((section["basis_of_design_manufacturer"], "basis_of_design"))
        for m in section.get("or_equal_manufacturers", []):
            mentions.append((m, "acceptable_manufacturer"))
        for manufacturer_name, mention_type in mentions:
            # The field the whole conclusion rests on -- grounded against
            # ufgs_text, the SAME text the model was given, never the full
            # document. See module docstring's GROUNDING section.
            if not name_grounded(manufacturer_name, ufgs_text):
                result.rejected_ungrounded.append(
                    f"{manufacturer_name!r} ({mention_type}, section {spec_section})")
                continue
            matched = line_card_names.get(normalize_name(manufacturer_name))
            result.mentions.append(MentionResult(
                spec_section=spec_section, spec_section_title=section.get("spec_section_title"),
                manufacturer_name=manufacturer_name, mention_type=mention_type,
                on_dmg_line_card=matched is not None, dmg_line_card_name=matched,
            ))

    if result.mentions:
        result.outcome = OutcomeClass.NAMES_MANUFACTURERS
    elif division23.get("performance_spec_only"):
        result.outcome = OutcomeClass.PERFORMANCE_SPEC_ONLY
    elif result.rejected_ungrounded:
        result.outcome = OutcomeClass.UNGROUNDED_MENTIONS_ONLY
    else:
        # 23-series heading matched (find_ufgs_23_series_text found
        # something) but the model asserted neither a manufacturer nor
        # performance_spec_only -- the same ambiguous edge case
        # app.pipeline.sam_gov's own no_ufgs_23_series_found catch-all
        # covers for its analogous situation. Noted explicitly rather than
        # left indistinguishable from a genuine NO_DIVISION_23 in the
        # underlying data, even though it tallies into that bucket.
        result.notes = ("23-series section heading matched, but the model found neither a "
                        "manufacturer mention nor performance-spec content -- collapsed into "
                        "NO_DIVISION_23 for the four-class tally; see this note for the real reason.")
    return result


def run_specs_pilot(directory: str, cfg) -> dict:
    """Process every supported file in `directory`. No DB writes -- see
    module docstring. Returns {directory, directory_exists, documents,
    by_outcome}."""
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return {"directory": str(directory), "directory_exists": False, "documents": [], "by_outcome": {}}

    files = sorted(
        p for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    documents = [process_document(p, cfg) for p in files]
    by_outcome: dict[str, int] = {o: 0 for o in ALL_OUTCOMES}
    for d in documents:
        by_outcome[d.outcome] = by_outcome.get(d.outcome, 0) + 1
    return {"directory": str(directory), "directory_exists": True, "documents": documents,
           "by_outcome": by_outcome}


def report_text(result: dict) -> str:
    """Phase C's answer, as plain text. Every number reported traces to
    `result["documents"]` -- nothing here is asserted independent of what
    was actually measured. Gates any percentage behind
    MIN_FOR_ANY_PERCENTAGE, per the explicit "do not report a percentage on
    n=8 as though it were a rate" instruction."""
    if not result["directory_exists"]:
        return f"docs/specs-pilot directory does not exist: {result['directory']!r} -- nothing to process."

    docs: list[DocumentResult] = result["documents"]
    n = len(docs)
    lines = [f"Division 23 spec parser -- school district pilot: {n} document(s) processed "
            f"from {result['directory']}"]

    if n == 0:
        lines.append("")
        lines.append("No documents found. This module is untested against real spec language -- "
                     "place hand-supplied documents in this directory and re-run.")
        return "\n".join(lines)

    lines.append("")
    lines.append("Outcome counts (the four requested, plus one disclosed addition -- see "
                 "app.pipeline.specs_pilot's module docstring):")
    for outcome in ALL_OUTCOMES:
        count = result["by_outcome"].get(outcome, 0)
        marker = "" if outcome in REQUESTED_OUTCOMES else "  [not one of the four requested]"
        lines.append(f"  {outcome:26s}{count:>4}{marker}")

    if n < MIN_FOR_ANY_PERCENTAGE:
        lines.append("")
        lines.append(f"n={n} is below {MIN_FOR_ANY_PERCENTAGE} -- no percentage is reported. Raw counts "
                     f"above are the only defensible statement at this corpus size; treat each "
                     f"document's outcome as an individual data point, not a rate. Roughly "
                     f"{RECOMMENDED_FOR_A_DEFENSIBLE_PERCENTAGE} documents would be needed for a "
                     f"percentage with a realistic shot at +/-10 points of margin.")
    elif n < RECOMMENDED_FOR_A_DEFENSIBLE_PERCENTAGE:
        names_pct = 100 * result["by_outcome"].get(OutcomeClass.NAMES_MANUFACTURERS, 0) / n
        lines.append("")
        lines.append(f"{names_pct:.0f}% named a manufacturer (n={n}) -- above the n=30 floor for a "
                     f"directional signal, but below the ~{RECOMMENDED_FOR_A_DEFENSIBLE_PERCENTAGE} "
                     f"needed for a defensible +/-10-point margin. Treat this as a lean, not a rate.")
    else:
        names_pct = 100 * result["by_outcome"].get(OutcomeClass.NAMES_MANUFACTURERS, 0) / n
        lines.append("")
        lines.append(f"{names_pct:.0f}% named a manufacturer (n={n}).")

    all_mentions = [m for d in docs for m in d.mentions]
    lines.append("")
    lines.append(f"Distinct manufacturer names found: {len({m.manufacturer_name for m in all_mentions})} "
                 f"across {len(all_mentions)} grounded mention(s).")
    if all_mentions:
        freq: dict[str, int] = {}
        for m in all_mentions:
            freq[m.manufacturer_name] = freq.get(m.manufacturer_name, 0) + 1
        lines.append("")
        lines.append("Manufacturer frequency (grounded mentions only):")
        for name, count in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {name:40s}{count:>4}")

        on_card = [m for m in all_mentions if m.on_dmg_line_card]
        off_card = [m for m in all_mentions if not m.on_dmg_line_card]
        lines.append("")
        lines.append(f"On DMG's line card (exact normalize_name match): {len(on_card)} mention(s) "
                     f"across {len({m.dmg_line_card_name for m in on_card})} line(s)")
        lines.append(f"Not on the line card: {len(off_card)} mention(s) across "
                     f"{len({m.manufacturer_name for m in off_card})} distinct name(s)")

        aaon = [m for m in all_mentions if (m.dmg_line_card_name or "").upper() == "AAON"
               or m.manufacturer_name.upper() == "AAON"]
        bod = sum(1 for m in aaon if m.mention_type == "basis_of_design")
        alt = sum(1 for m in aaon if m.mention_type == "acceptable_manufacturer")
        lines.append("")
        lines.append(f"AAON named: {len(aaon)} document(s)/mention(s) total -- "
                     f"{bod} as basis of design, {alt} as an acceptable manufacturer.")

    rejected = [(d.filename, r) for d in docs for r in d.rejected_ungrounded]
    if rejected:
        lines.append("")
        lines.append(f"Rejected as ungrounded (asserted by the model, not found in the document's "
                     f"own 23-series text -- never counted above): {len(rejected)}")
        for filename, reason in rejected:
            lines.append(f"  {filename}: {reason}")

    failed = [d for d in docs if d.outcome == OutcomeClass.PARSE_FAILED]
    if failed:
        lines.append("")
        lines.append("Parse failures:")
        for d in failed:
            lines.append(f"  {d.filename}: {d.parse_error}")

    return "\n".join(lines)
