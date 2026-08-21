"""Numeric grounding audit: does the source document actually contain the number?

The dangerous extraction failure is not a null, it is a confident wrong value. A
missed MW costs a sizing estimate; an invented one puts a number in front of a
customer that the filing does not support. Amperesand's 500 MW became 357 MW IT and
136,964 tons, and nothing in the pipeline objected, because "stated" outranks every
low-confidence flag.

This checks the one thing that needs no human: if the model asserts 99 MW, does the
string 99 appear in the document at all? A number absent from the source cannot have
been read from it.

Deliberately NOT a correctness check. A grounded number can still be the wrong number
— read off the wrong row, or the product rating rather than the building load — and
only hand verification catches that. This finds fabrication, which is the subset that
can be found mechanically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlmodel import Session, or_, select

from app.models import RawDocument, Signal

# Bump this whenever unit_grounded()/name_grounded()/reject_ungrounded_*()'s
# own matching logic changes in a way that could change a verdict on
# already-persisted data (a new variant pattern, a tightened/loosened
# proximity window, a new field added to NUMERIC_FIELDS/UNIT_TOKENS -- NOT
# for changes that only affect newly-extracted documents the same way
# regardless of when they run, like a comment or a refactor with no
# behavioral change). extract() stamps every new signal with this value at
# write time; fix_corpus() only re-examines signals stamped BELOW it, so a
# version bump is what makes "re-check the corpus" mean something, and
# what makes fix_corpus's own SQL query cheap on every day nothing changed
# instead of a full-text scan of the growing corpus every single day.
#
# History: 1 (2026-08-19) -- named_people/named_firms grounding added
# (previously no check existed at all); audit_signal() unified with
# unit_grounded()'s exact logic (previously a separate, looser check);
# single-occurrence numeric grounding flagged weak.
GROUNDING_VERSION = 1

# Numeric fields worth auditing. Coordinates are excluded: they are transcribed from
# DMS to decimal, so the digits legitimately differ from the source text.
NUMERIC_FIELDS = [
    "mw_it", "mw_total", "generator_count", "generator_hp_each", "generator_kw_each",
    "generator_critical_count", "generator_critical_mw_each",
    "generator_house_count", "generator_house_mw_each",
    "building_sqft", "acres", "water_acre_feet_per_year", "building_count",
]
# Small integers appear everywhere by chance (page numbers, dates, item numbers), so
# a bare match proves nothing. Report them, but do not call them grounded evidence.
LOW_SIGNAL_BELOW = 10

# Fields where the number is meaningless without its unit, and where a wrong value
# is expensive. A dollar amount and a megawatt figure are both just digits; only the
# neighbouring token tells them apart, which is how Blue Owl's $163,355,xxx became
# 163.355 MW and 44,748 tons.
#
# Guarded by (?<![a-z]) rather than a leading \b. Bare "sf" and "mw" are real
# abbreviations but also live inside ordinary words ("tran-sf-er"), so the unit must
# not follow a LETTER. It may follow a digit, because filings glue the two together
# constantly -- "16MW", "8-12MW", "6MW" -- and a leading \b fails on every one of
# those, since there is no boundary between "6" and "M". That bug rejected Colovore's
# stated 16 MW, the second row on the data center board.
UNIT_TOKENS: dict[str, str] = {
    "mw_total": r"(?<![a-z])(?:mw|megawatts?)\b",
    "mw_it": r"(?<![a-z])(?:mw|megawatts?)\b",
    "generator_kw_each": r"(?<![a-z])(?:kw|kilowatts?)\b",
    # Stated directly in MW by the filing (CEC power filings routinely do this
    # for genset ratings), same token as mw_total/mw_it — not a conversion.
    "generator_critical_mw_each": r"(?<![a-z])(?:mw|megawatts?)\b",
    "generator_house_mw_each": r"(?<![a-z])(?:mw|megawatts?)\b",
    "building_sqft": r"(?<![a-z])(?:sq\.?\s?ft\.?|sqft|sf|square[\s-]f(?:ee|oo)t)\b",
    "acres": r"(?<![a-z])acres?\b",
}
# How far from the matched digits the unit may sit. Wide enough for a flattened
# table row, where the column header carries the unit and the value sits several
# cells later: GOED board packets render as
#   "Year Land Cost Building SqFt | Cost Purchase Amount | Year-1 | ... 91,000"
# and a 40-char window rejected three real square-footage values on that layout
# alone. Widening cannot resurrect the cases this guard exists for — Blue Owl's
# filing contains no MW token at any distance, and FAAC's 84,800 appears nowhere at
# all — so the window trades no precision for real recall.
UNIT_PROXIMITY_CHARS = 120

_UNIT_RE: dict[str, re.Pattern] = {
    f: re.compile(p, re.I) for f, p in UNIT_TOKENS.items()
}


def unit_grounded(field: str, value: float, text: str) -> bool:
    """True if the value appears in the text with its unit close by.

    Grounding alone is not enough for these fields: a filing full of dollar figures
    in the 160-million range makes "163.355" findable in spirit and meaningless in
    fact. Requiring the unit token nearby is what separates 99 in "99 MW" from 99 in
    "$99,000,000".
    """
    return count_unit_grounded_occurrences(field, value, text) > 0


def count_unit_grounded_occurrences(field: str, value: float, text: str) -> int:
    """How many INDEPENDENT places in the text ground this value+unit --
    unit_grounded() only needs to know "more than zero"; this is also used
    to flag single-occurrence grounding as weaker evidence (see WEAK_
    SINGLE_OCCURRENCE below): one mention is real but unconfirmed, several
    independent mentions corroborate each other the way a repeated MW
    figure in Vernon's own filing does.

    Deliberately NOT a fix for subject misattribution (a number that is
    genuinely present, with its unit, but describes something OTHER than
    this filing's own subject -- confirmed in production, Colovore Reno 1's
    mw_total=16.0 grounds against "(16MW in RNO01)", a company-history
    aside about an existing DIFFERENT site, not this filing's own stated
    load). That is explicitly out of scope for mechanical grounding per
    this module's own docstring ("a number can be present and still be the
    wrong one... only hand verification catches that") -- occurrence
    counting cannot tell a real corroborating repeat from a real but
    unrelated single mention, it can only tell "one" from "several", which
    is why this is surfaced as `weak`, not auto-rejected.
    """
    pattern = _UNIT_RE.get(field)
    if pattern is None:
        return 1                      # not a unit-bearing field; nothing to check
    lower = text.lower()
    n = 0
    for variant in _variants(value):
        for m in _value_pattern(variant.lower()).finditer(lower):
            lo = max(0, m.start() - UNIT_PROXIMITY_CHARS)
            hi = m.end() + UNIT_PROXIMITY_CHARS
            if pattern.search(lower[lo:hi]):
                n += 1
    return n


def _name_variants(name: str) -> set[str]:
    """How a real, correctly-identified name might legitimately fail a naive
    exact-substring check against the raw extracted text, WITHOUT being
    invented -- confirmed against the real production corpus (2026-08-19,
    all 474 signals): a parenthetical abbreviation the model expanded or
    contracted ("EDAWN" <-> "Economic Development Authority of Western
    Nevada"), a credential suffix ("Beth Chow, AICP"), a middle initial
    added or dropped ("Peter Irby" <-> "Peter P. Irby"), or a PDF
    text-extraction artifact -- a mid-word line wrap ("Berry-\\nJones") or a
    field cut off at a column width ("Kimley-Horn and Associates, In"). None
    of those are fabrication; a name with NO trace of any of its
    significant words anywhere in the text is."""
    variants: set[str] = {name}
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", name)
    if m:
        variants.add(m.group(1).strip())
        variants.add(m.group(2).strip())
    variants.add(re.sub(r",\s*[A-Z]{2,5}$", "", name).strip())  # trailing credential
    tokens = [t for t in re.split(r"[\s,]+", name) if t]
    if len(tokens) > 1:
        variants.add(tokens[-1])                    # surname / last word
        variants.add(f"{tokens[0]} {tokens[-1]}")    # first + last, drop middles
    # Hyphenated compounds (surnames, firm names) split further: the real
    # production case this exists for is a two-column PDF layout that
    # interleaved unrelated text between "Berry-" and "Jones" -- nowhere
    # near adjacent even after whitespace collapse, so "Berry-Jones" as one
    # token never matches, but "Jones" alone, checked independently, does.
    for t in list(tokens):
        for part in t.split("-"):
            if len(part) > 2:
                variants.add(part)
    return {v for v in variants if len(v) > 2}


def name_grounded(name: str, text: str) -> bool:
    """True if `name` (or a real-world-plausible variant of it, see
    _name_variants) appears literally in `text`. Whitespace is collapsed
    first so a PDF line-wrap that splits one name across a newline (a
    real, confirmed production case: "Amanda Berry-\\nJones") does not read
    as absent.

    Also accepts a long, unambiguous PREFIX of the name/firm as grounding
    evidence -- confirmed production case (Heritage Valley): a form field
    literally cut off at its own column width, "Kimley-Horn and
    Associates, In", for the model's own correct "Kimley-Horn and
    Associates, Inc." A short name has no long-enough prefix to be
    unambiguous, so this only engages past a length floor.
    """
    collapsed = re.sub(r"\s+", " ", text).lower()
    for variant in _name_variants(name):
        if re.sub(r"\s+", " ", variant).lower() in collapsed:
            return True
    full = re.sub(r"\s+", " ", name).strip().lower()
    if len(full) >= 12:
        prefix = full[:max(12, int(len(full) * 0.8))]
        if prefix in collapsed:
            return True
    return False


def reject_ungrounded_names(data: dict, text: str) -> tuple[dict, list[dict]]:
    """Same contract as reject_ungrounded_numbers, for named_people/
    named_firms: entries with no textual basis at all are dropped, not
    downgraded, and the drop is reported so it stays auditable.

    2026-08-19: this field pair had NO grounding check of any kind before
    this -- confirmed by reading app/pipeline/extract.py, where
    signal.named_people/named_firms were set directly from the raw model
    output. A retroactive scan of all 474 production signals found zero
    confirmed pure inventions (every flagged name traced to a real
    variant per _name_variants above), but the absence of ANY check was
    real and structural, not merely unexercised so far."""
    rejections: list[dict] = []
    for field_name in ("named_people", "named_firms"):
        items = data.get(field_name)
        if not items:
            continue
        kept, dropped = [], []
        for item in items:
            name = (item or {}).get("name") or ""
            if name.strip() and name_grounded(name, text):
                kept.append(item)
            else:
                dropped.append(item)
        if dropped:
            data[field_name] = kept
            for item in dropped:
                rejections.append({
                    "field": field_name, "value": item.get("name"),
                    "reason": f"{item.get('name')!r} does not appear in the source "
                              f"document, under any of its real-world-plausible "
                              f"variants (parenthetical/credential/initial stripped, "
                              f"line-wrap collapsed)",
                })
    return data, rejections


def reject_ungrounded_numbers(data: dict, text: str) -> tuple[dict, list[dict]]:
    """Null out unit-bearing numbers the document does not support, and say why.

    Rejects rather than downgrades. The whole pattern behind Amperesand and Blue Owl
    is that a bad number outranks a null everywhere downstream: it is "stated", so it
    beats the low-confidence flag, drives sizing, and lands on the board looking like
    the best-evidenced row there. A null costs an estimate; a confident wrong megawatt
    figure costs the credibility of every number beside it.
    """
    rejections: list[dict] = []
    for fname in UNIT_TOKENS:
        value = data.get(fname)
        if value is None:
            continue
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue
        if unit_grounded(fname, fval, text):
            continue
        data[fname] = None
        rejections.append({
            "field": fname, "value": fval,
            "reason": f"{fval:g} does not appear in the source document with a "
                      f"{fname.split('_')[-1]} unit within "
                      f"{UNIT_PROXIMITY_CHARS} characters",
            "nearest": _numbers_near(text, len(_variants(fval)[0])),
        })
    return data, rejections


# Below this, a ScheduleEntry is flagged for review regardless of whether
# every field grounded cleanly -- a model that is honestly unsure about a
# rotated table or a split row should not get to skip review just because
# the fields it did read happen to be real numbers. See
# app.assumptions's "Equipment schedule extraction" group.
SCHEDULE_LOW_CONFIDENCE_BELOW = 0.7


_QUOTE_ELLIPSIS = re.compile(r"\s*(?:\.{3}|…)\s*")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NON_ALNUM_NO_NL = re.compile(r"[^a-z0-9\n]+")


def _normalize_for_match(s: str) -> str:
    """Lowercase, collapse every run of non-alphanumeric characters
    (INCLUDING newlines) to one space. Deliberately more aggressive than
    name_grounded's plain whitespace collapse: a flattened schedule table
    uses "--", "|", ":" as pure column-separator punctuation
    ("MANUFACTURER -- TRANE"), which a model correctly treats as
    formatting, not content, when it copies a fragment as "MANUFACTURER
    TRANE" -- confirmed against a real public rooftop-unit schedule,
    2026-08-20. Only used for schedule-entry fragment matching, not for
    name_grounded/unit_grounded's own checks, which stay conservative on
    purpose."""
    return _NON_ALNUM.sub(" ", s.lower()).strip()


def _normalize_lines_for_match(s: str) -> str:
    """Same as _normalize_for_match, but keeps line breaks as row
    boundaries instead of collapsing them away. A flattened TRANSPOSED
    schedule table's physical PDF line is its table ROW -- confirmed
    against a real public rooftop-unit schedule, 2026-08-20, where a single
    line like 'MANUFACTURER -- TRANE TRANE CARRIER YORK...' carries every
    tag's value for that one field. Used by _same_line_token_sequence,
    which needs row boundaries to stay real: without them, a token search
    could match across two completely unrelated fields anywhere in the
    document."""
    return "\n".join(_NON_ALNUM_NO_NL.sub(" ", ln.lower()).strip() for ln in s.split("\n"))


def _same_line_token_sequence(fragment: str, lined_text: str) -> bool:
    """True if `fragment`'s words all appear, in order, on ONE physical
    line of `lined_text` (see _normalize_lines_for_match) -- with anything
    at all allowed between them, since a transposed table's row genuinely
    interleaves other tags' real values between a field's label and any one
    tag's own value ('MODEL -- 4TCC3048A1 TSC090A360A2800 ...' -- RTU-02's
    model number, TSC090A360A2800, is real and on the MODEL row, just not
    adjacent to the word "MODEL" because RTU-01's model number sits between
    them). This is still "every fragment literally appears in the
    document": every word is required, in the stated order, on a single
    real row of the table -- it only stops requiring them to be touching.

    Deliberately scoped to one line (not one paragraph, not the whole
    document): the arbitrary-gap tolerance this needs to bridge a crowded
    row would be dangerously loose applied document-wide -- it would prove
    a manufacturer name is 'grounded' just because it appears anywhere near
    an unrelated number anywhere in a long filing. Confining the gap to a
    single physical table row is what keeps this a real check instead of a
    rubber stamp.
    """
    # Tokenize with the SAME normalization already applied to lined_text --
    # a raw fragment token like "RTU-01" must become "rtu 01" (two tokens,
    # hyphen gone) to have any chance of matching, since lined_text's own
    # hyphens were already collapsed to spaces the same way.
    tokens = _normalize_for_match(fragment).split()
    if not tokens:
        return False
    pattern = re.compile(r".*?".join(re.escape(t) for t in tokens))
    return any(pattern.search(line) for line in lined_text.split("\n"))


@dataclass
class QuoteCheck:
    problem: str | None    # None means grounded -- nothing for the caller to flag
    assembled: bool        # True if grounded from >1 separately-verified fragment,
                           # not one contiguous span -- informational, not a failure


def quote_grounded(quote: str, text: str) -> QuoteCheck:
    """Checks `quote` against `text`, allowing for one real layout: a
    TRANSPOSED schedule table (one row per FIELD, one column per unit --
    confirmed against a real public rooftop-unit schedule, 2026-08-20) puts
    a tag and its capacity/manufacturer many lines apart, so a model asked
    for one verbatim excerpt legitimately cannot quote them as one
    contiguous span and instead joins several real fragments with "...".

    A tag proven by its own column-header row, plus a capacity proven by its
    own row label, plus a manufacturer proven by its own row label, IS
    genuinely grounded -- just assembled from several places instead of one.
    Treating that the same as a quote that is not in the document AT ALL
    (fabrication) would flag the single most common real-world schedule
    layout as if every row were invented, which is exactly what blocked
    every one of 62 rows on the first real document this ran against
    (2026-08-20) -- see app/pipeline/schedule.py's module docstring.

    So: QuoteCheck.problem is None whenever every fragment is independently
    grounded (whether the quote was one contiguous span or several joined
    with "..."). It is a description of what's missing ONLY when a fragment
    genuinely cannot be found anywhere in the document -- that is still
    unconditionally a failure; assembling evidence from real, separately-
    verified locations is not the same claim as inventing one, and this
    function must never blur that line the other direction either.
    QuoteCheck.assembled records which case it was, for transparency (shown
    on the schedule row, never used to flag it).

    A floor of 8 characters per fragment for the plain-substring tier, same
    reasoning as name_grounded's own length floor: text too short to be
    distinctive proves nothing there. The same-line token-sequence fallback
    has no separate floor -- multi-word requirement plus one-line scoping is
    already its own distinctiveness guard.
    """
    if not quote or not quote.strip():
        return QuoteCheck("source_quote is empty", assembled=False)
    normalized_text = _normalize_for_match(text)
    if _normalize_for_match(quote) in normalized_text:
        return QuoteCheck(None, assembled=False)
    fragments = [f.strip() for f in _QUOTE_ELLIPSIS.split(quote) if f.strip()]
    assembled = len(fragments) > 1
    lined_text = _normalize_lines_for_match(text)
    ungrounded = []
    for f in fragments:
        if len(f) >= 8 and _normalize_for_match(f) in normalized_text:
            continue
        if _same_line_token_sequence(f, lined_text):
            assembled = True  # a same-line reassembly is never the fully-contiguous case
            continue
        ungrounded.append(f)
    if ungrounded:
        return QuoteCheck(f"source_quote fragment(s) not found in the document: {ungrounded!r}",
                          assembled=assembled)
    return QuoteCheck(None, assembled=assembled)


_TAG_NOISE_PREFIX = re.compile(
    r"^(?:NEW\s+UNIT[-\s]*|REPLACEMENT(?:\s+FOR)?[-\s]*|EXISTING[-\s]*)", re.IGNORECASE)
_TAG_FOOTNOTE_SUFFIX = re.compile(r"\s*\*\d+\s*$")
_TAG_SHAPE = re.compile(r"^([A-Z]+)[-\s]*(\d+)([-\s]?[A-Z])?$")


def canonicalize_tag(raw: str) -> str:
    """Collapse cosmetic variation in a schedule tag string -- "CU 1" /
    "CU-3" / "CU3" / "NEW UNIT-CU-3" all becoming the same canonical form --
    WITHOUT deciding whether two different tag strings refer to the same
    physical unit. That is a much bigger claim (this session's real
    extraction runs also produced "CU1" and "CU 1" as genuinely two
    separate schedule rows for the same unit -- one read off the schedule
    table, one off its own spec sheet -- which this function does NOT
    merge; that is the strong/weak substitution problem, deliberately left
    unbuilt this session). Scope is bounded to exactly this shape: an
    alphabetic equipment-type prefix, optional separator, a number, and an
    optional trailing letter (RTU-01, RTU-01-A, CU3, AH1). Anything that
    doesn't match that shape is returned upper-cased and stripped, unchanged
    otherwise -- this is not a general tag parser.
    """
    s = (raw or "").strip().upper()
    s = _TAG_NOISE_PREFIX.sub("", s).strip()
    s = _TAG_FOOTNOTE_SUFFIX.sub("", s).strip()
    m = _TAG_SHAPE.match(s)
    if not m:
        return s
    prefix, number, letter = m.groups()
    number = str(int(number))  # "01" -> "1", so RTU-01 and RTU-1 canonicalize the same
    canon = f"{prefix}-{number}"
    if letter:
        canon += f"-{letter.strip('- ').upper()}"
    return canon


def ground_schedule_entry(entry: dict, text: str) -> tuple[dict, list[str]]:
    """Check one ScheduleEntryExtraction-shaped dict against the document
    text it was extracted from. Returns (entry, reasons) -- reasons is
    empty only when every check passes and confidence clears the floor.

    Manufacturer fields (basis_of_design_manufacturer, approved_equals) are
    REJECTED (nulled/filtered) on failure, same reject-not-downgrade
    discipline as reject_ungrounded_numbers/reject_ungrounded_names above --
    a manufacturer name Scout cannot find in the document must never reach a
    rep as if it were read off the page. tag/capacity_value/source_quote
    failures are reported but NOT nulled: a schedule row with no tag is not
    useful to keep at all, so the row is flagged for a human to look at
    rather than silently hollowed out.
    """
    reasons: list[str] = []
    entry = dict(entry)

    quote_check = quote_grounded(entry.get("source_quote") or "", text)
    if quote_check.problem:
        reasons.append(quote_check.problem)
    # Informational only -- shown on the row so a rep can see this was
    # assembled from several table locations rather than read off one line,
    # but never itself a reason to flag the row. See QuoteCheck's docstring.
    entry["quote_assembled"] = quote_check.assembled

    tag = entry.get("tag") or ""
    if not tag or not name_grounded(tag, text):
        reasons.append(f"tag {tag!r} does not appear in the document")

    collapsed = re.sub(r"\s+", " ", text)

    cap = entry.get("capacity_value")
    fcap = None
    if cap is not None:
        try:
            fcap = float(cap)
        except (TypeError, ValueError):
            fcap = None
        if fcap is not None and not any(v in collapsed for v in _variants(fcap)):
            reasons.append(f"capacity_value {fcap:g} does not appear in the document")
            fcap = None  # ungrounded -- don't use it for corroboration below

    btuh = entry.get("capacity_btuh")
    fbtuh = None
    if btuh is not None:
        try:
            fbtuh = float(btuh)
        except (TypeError, ValueError):
            fbtuh = None
        if fbtuh is not None and not any(v in collapsed for v in _variants(fbtuh)):
            reasons.append(f"capacity_btuh {fbtuh:g} does not appear in the document")
            fbtuh = None

    # Two grounded, independent readings of the SAME capacity (tons vs BTU/H)
    # agreeing is corroborating evidence, not conflicting evidence -- see
    # schemas.py's capacity_btuh docstring. Only checked when capacity_unit
    # is actually tons, since capacity_btuh is defined as "the same headline
    # number restated in BTU/H" specifically for a tons reading.
    entry["capacity_corroborated"] = None
    if fcap is not None and fbtuh is not None:
        unit = (entry.get("capacity_unit") or "").strip().lower()
        if unit in ("ton", "tons"):
            implied_tons = fbtuh / 12000
            # nominal tonnage is a rounded nameplate label, not an exact
            # figure -- allow the larger of a half ton or 10% of headroom.
            tolerance = max(0.5, 0.10 * fcap)
            corroborated = abs(implied_tons - fcap) <= tolerance
            entry["capacity_corroborated"] = corroborated
            if not corroborated:
                reasons.append(
                    f"capacity_value {fcap:g} tons and capacity_btuh {fbtuh:g} BTU/H "
                    f"({implied_tons:.2f} tons) disagree beyond a nameplate-rounding tolerance"
                )

    bod = entry.get("basis_of_design_manufacturer")
    if bod and not name_grounded(bod, text):
        reasons.append(f"basis_of_design_manufacturer {bod!r} does not appear in the document -- nulled")
        entry["basis_of_design_manufacturer"] = None

    kept_equals, dropped_equals = [], []
    for mfr in entry.get("approved_equals") or []:
        (kept_equals if mfr and name_grounded(mfr, text) else dropped_equals).append(mfr)
    if dropped_equals:
        reasons.append(f"approved_equals {dropped_equals!r} do not appear in the document -- dropped")
    entry["approved_equals"] = kept_equals

    confidence = entry.get("confidence") or 0.0
    if confidence < SCHEDULE_LOW_CONFIDENCE_BELOW:
        reasons.append(f"low extraction confidence ({confidence:.2f} < {SCHEDULE_LOW_CONFIDENCE_BELOW})")

    return entry, reasons


@dataclass
class Finding:
    doc_id: int
    signal_id: int
    source: str
    title: str
    field: str
    value: float
    grounded: bool
    weak: bool                      # matched, but the value is too small to be evidence
    nearest: list = field(default_factory=list)   # nearby numbers actually in the text


def _variants(value: float) -> list[str]:
    """How a number might legitimately be written in a filing."""
    out: set[str] = set()
    as_int = int(value) if float(value).is_integer() else None
    if as_int is not None:
        out.add(str(as_int))
        out.add(f"{as_int:,}")
        # 1,100,000 sqft is often written "1.1 million"
        for unit, div in (("million", 1_000_000), ("thousand", 1_000)):
            if as_int >= div:
                q = as_int / div
                out.add(f"{q:g} {unit}")
                out.add(f"{q:g}-{unit}")
    else:
        out.add(f"{value:g}")
        out.add(f"{value:,.1f}")
        out.add(f"{value:,.2f}")
    return [v for v in out if v]


def _value_pattern(variant: str) -> re.Pattern:
    """Match a number as a number, not as a run of digits inside a bigger one.

    Plain substring search grounds "3" against the 3 in "$30,183,000" and "16"
    against "163" — so every small value grounds trivially and the guard silently
    stops guarding. Digit boundaries fix that while still allowing a unit to be glued
    on the right ("16MW"), since a letter is not a digit.
    """
    return re.compile(rf"(?<![\d.]){re.escape(variant)}(?![\d])")


def _numbers_near(text: str, needle_len: int) -> list[str]:
    """A sample of numbers that ARE in the document, for the report."""
    found = re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", text)
    return sorted({f for f in found if len(f) >= max(2, needle_len - 1)},
                  key=lambda s: -len(s))[:12]


def audit_signal(signal: Signal, doc: RawDocument) -> list[Finding]:
    """Read-only audit of what's currently persisted. Uses the EXACT same
    unit_grounded() logic reject_ungrounded_numbers() checks at write time
    for every unit-bearing field (2026-08-19 fix -- this used to be a
    separate, looser inline check with no unit-proximity requirement at
    all, so `scout grounding` could report a value "grounded" that the
    write-time guard would have rejected, or vice versa. Confirmed in
    production: Blue Owl's mw_total=163.355 and FAAC's building_sqft=84800
    both predate this guard's own deployment and are STILL grounded=False
    under this shared logic -- they were never re-checked after the guard
    was added, only newly-extracted documents were. See `scout grounding
    --fix` for the retroactive pass that corrects already-persisted rows.

    Fields with no unit token (generator_count, building_count, etc.) keep
    the original bare digit-presence check -- reject_ungrounded_numbers()
    never covered these either (see UNIT_TOKENS), so there is nothing to
    unify there.
    """
    text = doc.raw_text or ""
    lower = text.lower()
    out: list[Finding] = []
    for fname in NUMERIC_FIELDS:
        value = getattr(signal, fname, None)
        if value is None:
            continue
        fval = float(value)
        variants = _variants(fval)
        if fname in UNIT_TOKENS:
            n = count_unit_grounded_occurrences(fname, fval, text)
            grounded = n > 0
            # A single independent mention is real evidence, but not
            # corroborated evidence -- see count_unit_grounded_occurrences'
            # own docstring for why this can flag but not reject.
            weak = grounded and n == 1
        else:
            grounded = any(_value_pattern(v.lower()).search(lower) for v in variants)
            weak = grounded and abs(fval) < LOW_SIGNAL_BELOW
        out.append(Finding(
            doc_id=doc.id, signal_id=signal.id, source=doc.source,
            title=doc.title or "", field=fname, value=fval,
            grounded=grounded, weak=weak,
            nearest=[] if grounded else _numbers_near(text, len(variants[0])),
        ))
    return out


def audit_corpus(session: Session) -> dict:
    signals = session.exec(select(Signal).where(Signal.raw_document_id.is_not(None))).all()
    findings: list[Finding] = []
    for s in signals:
        doc = session.get(RawDocument, s.raw_document_id)
        if doc is not None:
            findings.extend(audit_signal(s, doc))
    ungrounded = [f for f in findings if not f.grounded]
    by_field: dict[str, dict] = {}
    for f in findings:
        b = by_field.setdefault(f.field, {"asserted": 0, "grounded": 0, "ungrounded": 0,
                                          "weak": 0})
        b["asserted"] += 1
        b["grounded" if f.grounded else "ungrounded"] += 1
        if f.weak:
            b["weak"] += 1
    return {"findings": findings, "ungrounded": ungrounded, "by_field": by_field,
            "n_signals": len(signals), "n_asserted": len(findings)}


def audit_text(result: dict) -> str:
    lines = [f"Numeric grounding audit — {result['n_asserted']} asserted values "
             f"across {result['n_signals']} signals", "",
             f"{'field':24s}{'asserted':>9}{'grounded':>10}{'UNGROUNDED':>12}{'weak':>7}"]
    for fname, b in sorted(result["by_field"].items(),
                           key=lambda kv: -kv[1]["ungrounded"]):
        lines.append(f"{fname:24s}{b['asserted']:>9}{b['grounded']:>10}"
                     f"{b['ungrounded']:>12}{b['weak']:>7}")
    ung = result["ungrounded"]
    if not ung:
        lines += ["", "Every asserted number appears in its source document.",
                  "That rules out invention, NOT misreading — a number can be present "
                  "and still be the wrong one. Hand verification is what catches that."]
        return "\n".join(lines)
    lines += ["", f"UNGROUNDED ASSERTIONS ({len(ung)}) — the model stated a number that "
                  f"does not appear in the document:"]
    for f in sorted(ung, key=lambda f: -abs(f.value)):
        lines.append(f"  doc {f.doc_id} [{f.source}] {f.title[:52]}")
        lines.append(f"    {f.field} = {f.value:g}   not found in source text")
        if f.nearest:
            lines.append(f"    numbers actually present: {', '.join(f.nearest[:8])}")
    return "\n".join(lines)


def fix_corpus(session: Session) -> dict:
    """Retroactively re-apply the CURRENT grounding guard (numeric AND
    name) to every already-persisted signal, and correct what fails.

    2026-08-19: the reason this exists. reject_ungrounded_numbers() runs at
    write time, inside app.pipeline.extract._extract_docs, correctly,
    BEFORE a signal is created -- but a RawDocument is only ever processed
    ONCE (processed_at gates the extract query), so a row extracted before
    this guard existed, or before a guard IMPROVEMENT shipped, never gets
    re-checked against the newer logic. Confirmed in production: Blue Owl's
    mw_total=163.355 and FAAC's building_sqft=84800 are both the guard's
    OWN motivating examples (see this module's docstring) and were STILL
    sitting on live, board-visible signals, because both predate the guard
    and nothing had ever gone back to apply it to already-written rows.

    This is that retroactive pass -- the one-time and repeatable fix for
    "the guard runs, but only forward from when it was added." Run it
    again after any future change to unit_grounded()/name_grounded()'s own
    logic, for the same reason -- which is also why it is now version-
    gated (see GROUNDING_VERSION) rather than something that has to be
    remembered and run by hand: only signals stamped BELOW the current
    version are examined at all, so on any day nothing in this module
    changed, the query below returns nothing and this is a no-op, not a
    full-text scan of the whole corpus. Every examined signal is stamped
    to the current version whether or not anything was rejected -- a
    signal that already passes needs the stamp exactly as much as one that
    gets corrected, so it is not re-examined again for no reason tomorrow.
    """
    n_total = session.exec(
        select(Signal).where(Signal.raw_document_id.is_not(None))
    ).all()
    n_total = len(n_total)
    signals = session.exec(
        select(Signal).where(
            Signal.raw_document_id.is_not(None),
            or_(Signal.grounding_version.is_(None),
               Signal.grounding_version < GROUNDING_VERSION),
        )
    ).all()
    changes: list[dict] = []
    for sig in signals:
        doc = session.get(RawDocument, sig.raw_document_id)
        if doc is None or not doc.raw_text:
            continue
        text = doc.raw_text
        touched = False
        row_rejected_numeric: list[dict] = []
        for fname in UNIT_TOKENS:
            value = getattr(sig, fname, None)
            if value is None:
                continue
            fval = float(value)
            if unit_grounded(fname, fval, text):
                continue
            setattr(sig, fname, None)
            row_rejected_numeric.append({
                "field": fname, "value": fval,
                "reason": f"{fval:g} does not appear in the source document with a "
                          f"{fname.split('_')[-1]} unit within {UNIT_PROXIMITY_CHARS} "
                          f"characters (retroactive fix, 2026-08-19)",
            })
            touched = True
        row_rejected_names: list[dict] = []
        for fname in ("named_people", "named_firms"):
            items = getattr(sig, fname, None) or []
            kept, dropped = [], []
            for item in items:
                name = (item or {}).get("name") or ""
                if name.strip() and name_grounded(name, text):
                    kept.append(item)
                else:
                    dropped.append(item)
            if dropped:
                setattr(sig, fname, kept)
                for item in dropped:
                    row_rejected_names.append({
                        "field": fname, "value": item.get("name"),
                        "reason": f"{item.get('name')!r} does not appear in the source "
                                  f"document (retroactive fix, 2026-08-19)",
                    })
                touched = True
        if touched:
            ej = dict(sig.extraction_json or {})
            ej["rejected_numeric"] = (ej.get("rejected_numeric") or []) + row_rejected_numeric
            ej["rejected_names"] = (ej.get("rejected_names") or []) + row_rejected_names
            sig.extraction_json = ej
            changes.append({"signal_id": sig.id, "project_name": sig.project_name,
                            "rejected_numeric": row_rejected_numeric,
                            "rejected_names": row_rejected_names})
        # Stamped whether or not anything was rejected -- a clean signal is
        # just as "checked against the current guard" as a corrected one,
        # and needs the stamp for the SAME reason: so tomorrow's run (or
        # today's pipeline stage) doesn't re-examine it for nothing.
        sig.grounding_version = GROUNDING_VERSION
        session.add(sig)
    if signals:
        session.commit()
    return {"n_signals_total": n_total, "n_signals_examined": len(signals),
            "n_signals_corrected": len(changes), "changes": changes}


def fix_text(result: dict) -> str:
    lines = [f"Retroactive grounding fix (guard version {GROUNDING_VERSION}) — "
             f"{result['n_signals_examined']} of {result['n_signals_total']} signals were "
             f"stamped below the current version and re-checked, "
             f"{result['n_signals_corrected']} corrected"]
    if result["n_signals_examined"] == 0:
        return lines[0] + "\nEvery signal already carries the current guard version — nothing to re-check."
    if not result["changes"]:
        return lines[0] + "\nNothing to fix — every re-checked value already passes the current guard."
    for c in result["changes"]:
        lines.append(f"\nsignal #{c['signal_id']} {c['project_name']!r}")
        for r in c["rejected_numeric"]:
            lines.append(f"  nulled {r['field']}={r['value']:g} — {r['reason']}")
        for r in c["rejected_names"]:
            lines.append(f"  removed {r['field']}={r['value']!r} — {r['reason']}")
    return "\n".join(lines)
