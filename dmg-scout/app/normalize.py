"""Entity-name normalization for matching. 'Vantage Data Centers NV11, LLC' -> 'vantage data centers'."""
from __future__ import annotations

import re

_LEGAL_SUFFIXES = [
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "lp", "llp", "ltd", "limited", "holdings", "partners", "trust",
]
_ROMAN = re.compile(r"\b[ivxlcdm]{1,6}\b")
_PHASE = re.compile(r"\b(phase|bldg|building|campus|site|parcel)\s*[\w-]{0,4}\b")
# Trailing project/SPE designators like "NV11", "PY A", "CA-2", "DC3"
_SPE_CODE = re.compile(r"\b[a-z]{1,3}[\s-]?\d{1,4}[a-z]?\b")


def normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = s.replace(".", "")                   # collapse dotted abbreviations: L.P. -> lp
    s = re.sub(r"[^\w\s-]", " ", s)          # punctuation
    for suf in _LEGAL_SUFFIXES:
        s = re.sub(rf"\b{re.escape(suf)}\b", " ", s)
    s = _PHASE.sub(" ", s)
    s = _SPE_CODE.sub(" ", s)
    s = _ROMAN.sub(" ", s)
    s = re.sub(r"[\s-]+", " ", s).strip()
    return s


def normalize_company_name(name: str) -> str:
    """Entity-name normalization for a company's own identity (Firm rows and
    anything matched against them) -- NOT for project/permit signals, which
    keep using normalize_name unchanged. See app.importers.netsuite_customers
    and the Phase B blast-radius report this was built from: normalize_name's
    _SPE_CODE and _ROMAN passes are tuned for a PROJECT name's own SPE/phase
    codes ("Vantage Data Centers NV11, LLC" -> "vantage data centers", the
    intended collapse), but applied to an ordinary COMPANY name they silently
    collide unrelated real companies -- "MCM ENGINEERING, INC." (MCM parses
    as the roman numeral 1900), "L&D Engineering Inc." (L and D are each
    single-letter roman numerals once "&" is stripped to a space), and "P2S
    Engineering, Inc." (matches the SPE-code shape) all normalized to the
    bare string "engineering"; separately, "LM Construction" (L and M are
    each roman numerals) normalized to bare "construction" and, per that
    report, was measured to be one normalize_name call away from silently
    absorbing 221 distinct, real CSLB-licensed contractors it has never done
    business with, the moment any of their names is ever extracted from a
    document (app.firms.resolve_signal_firms looks up-then-reuses on
    Firm.name_norm; a same-key hit never creates a second row or raises --
    it just silently attaches the wrong company to the existing Firm).

    Company identity has no legitimate use for either pass: a company's own
    name is never itself a per-project SPE code, and stripping a bare
    roman-numeral-shaped token is safe ONLY when it is doing real
    phase/building/parcel/unit work ("Data Center Phase II" should still
    collapse toward "Data Center") -- which _PHASE already captures on its
    own by consuming the numeral as part of one bounded match right after
    the keyword (phase|bldg|building|campus|site|parcel). A roman-shaped
    token anywhere else -- the leading token ("LM", "MCM") or any standalone
    token not adjacent to one of those keywords -- is never phase language,
    so this function never runs the standalone _SPE_CODE or _ROMAN passes at
    all. _PHASE itself is kept exactly as normalize_name runs it, so
    "Phase II"/"Building III"-shaped project language collapses the same way
    here as it does for signals; only the two passes that were never safe
    for a company's OWN name are dropped.
    """
    s = name.lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"[^\w\s-]", " ", s)
    for suf in _LEGAL_SUFFIXES:
        s = re.sub(rf"\b{re.escape(suf)}\b", " ", s)
    s = _PHASE.sub(" ", s)
    s = re.sub(r"[\s-]+", " ", s).strip()
    return s


"""Trailing generational and credential suffixes on a PERSON's name. Stripped only
as the final token, so "John Smith III" matches "John Smith" while "Di Wu" survives."""
_PERSON_TRAILING = {"jr", "sr", "ii", "iii", "iv", "v", "pe", "pmp", "aia", "leed", "phd"}


def normalize_person_name(name: str) -> str:
    """Normalize a HUMAN name for identity comparison.

    Deliberately not `normalize_name`, which exists for companies and strips legal
    suffixes, phase/parcel words, SPE codes and roman numerals. Those rules maul
    people: 'Di Wu' -> 'wu', 'Max Li' -> 'max', 'Clive Dix' -> 'clive', and
    'Livi Cid' -> '' — every token happens to be roman-numeral letters. The ladder
    deduped people with it, so two different people at one rung collapsed into one
    and a callable human silently vanished from the board.

    Here: case, punctuation and spacing only, plus trailing generational or
    credential suffixes. Hyphen and space are equivalent, so 'Heather
    Wessling-Grosz' and 'Heather Wessling Grosz' are one person.
    """
    s = name.lower().strip()
    s = s.replace(".", "")
    s = re.sub(r"[^\w\s-]", " ", s)      # keep hyphen for the collapse below
    s = re.sub(r"[\s-]+", " ", s).strip()
    parts = s.split()
    while len(parts) > 1 and parts[-1] in _PERSON_TRAILING:
        parts.pop()
    return " ".join(parts)


def normalize_county(county: str | None) -> str | None:
    if not county:
        return None
    c = county.strip()
    c = re.sub(r"\s+county$", "", c, flags=re.IGNORECASE)
    return c.title()


# Full name -> USPS code, for extraction that reports a state exactly as the
# source document phrased it ("State of Nevada...") rather than as a code —
# correct extraction behavior, not a bug, so this is where the normalizing
# belongs, not in the extraction prompt. Every state-keyed lookup in this
# codebase (territory in config.yaml, county adjacency, day-to-bid overrides)
# assumes a 2-letter USPS code; a value that slips through as "Nevada" matches
# none of them and silently drops out of every state-keyed decision.
_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
VALID_STATE_CODES = frozenset(_STATE_NAME_TO_CODE.values())


def normalize_state(state: str | None) -> str | None:
    """Best-effort full-name -> USPS-code normalization. An already-valid code
    passes through unchanged (case-folded); an unrecognized value passes
    through UNCHANGED rather than being dropped or guessed at — null over
    inference applies here too, and app.ops.doctor's state_values check is
    what catches whatever this function could not resolve, rather than this
    function silently hiding it."""
    if not state:
        return None
    s = state.strip()
    if len(s) == 2 and s.upper() in VALID_STATE_CODES:
        return s.upper()
    return _STATE_NAME_TO_CODE.get(s.lower(), s)
