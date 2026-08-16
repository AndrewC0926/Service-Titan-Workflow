"""Regulatory trigger engine (Phase 6). See config.yaml's `regulatory_triggers`
block and docs/CHARTER.md invariant 13.

The reframe that unblocked this: most triggers don't need nameplate/asset
data. R-410A was the dominant commercial DX refrigerant from roughly 2010
(after the R-22 phaseout) to 2025 (the A2L transition) — so an equipment
INSTALL YEAR, sourced from permit history, is enough to infer refrigerant
type and check it against SB 1206's GWP-ban tiers. No site visit, no
nameplate, no asset database. The only trigger that genuinely needs
equipment specifics is CARB's refrigerant-charge threshold, and that's
narrowed to a candidate list via assessor use code (see
app/pipeline/assessor.py) rather than true per-facility knowledge — CARB's
R3 filer list itself is not public (confirmed: login-gated portal, no open
dataset).
"""
from __future__ import annotations

import re
from datetime import date

from app.config import Config

# Permit work-description text is free and often more specific than a
# nameplate would be — "REPLACE (2) 6-ton HEAT PUMP PACKAGE UNIT" names count
# AND tonnage without ever seeing the equipment. Patterns below matched
# against real LADBS mechanical-permit work_desc samples.
_SPELLED_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COUNT_PAREN = re.compile(r"\((\d+)\)")
_COUNT_LEADING = re.compile(
    r"\b(\d+)\s+(?:new\s+)?(?:rtus?|units?|split\s+systems?|heat\s+pumps?|compressors?)\b", re.I)
_COUNT_SPELLED = re.compile(
    r"\b(" + "|".join(_SPELLED_NUMBERS) + r")\s+(?:\d+(?:\.\d+)?[\s-]*ton\s+)?units?\b", re.I)
_TONS = re.compile(r"(\d+(?:\.\d+)?)[\s-]*ton\b", re.I)


def parse_equipment_from_work_desc(text: str) -> dict:
    """Best-effort {count, tons_each} from a permit work-description string.
    Null over inference: a field this can't confidently find stays None
    rather than defaulting to 1 or guessing from partial matches."""
    if not text:
        return {"count": None, "tons_each": None}

    count = None
    m = _COUNT_PAREN.search(text)
    if m:
        count = int(m.group(1))
    else:
        m = _COUNT_LEADING.search(text)
        if m:
            count = int(m.group(1))
        else:
            m = _COUNT_SPELLED.search(text)
            if m:
                count = _SPELLED_NUMBERS[m.group(1).lower()]

    tons_each = None
    m = _TONS.search(text)
    if m:
        tons_each = float(m.group(1))

    return {"count": count, "tons_each": tons_each}

# R-410A's practical service window. Before ~2010: R-22 (pre-phaseout stock)
# or early R-410A adoption — genuinely ambiguous, left null rather than
# guessed. After ~2024: the A2L transition (R-454B etc.) is underway, so an
# install that recent is NOT safely assumed to be R-410A either.
R410A_WINDOW_START = 2010
R410A_WINDOW_END = 2024
R410A_GWP = 2088


def customer_facing_triggers(cfg: Config) -> dict:
    """Every entry in config.yaml's regulatory_triggers with
    verification.status == "verified" — see docs/CHARTER.md invariant 13.
    Anything else (missing status, "unverified", "needs_review", or simply
    absent from the config) is excluded here, not caveated downstream. This
    is the ONLY sanctioned way to read regulatory_triggers for anything a
    customer might see."""
    triggers = cfg.get("regulatory_triggers", {}) or {}
    return {
        key: entry for key, entry in triggers.items()
        if (entry.get("verification") or {}).get("status") == "verified"
    }


def infer_refrigerant(install_year: int | None) -> dict:
    """Given an equipment install year (from permit issue_date), infer the
    likely refrigerant family. Returns {refrigerant, confidence, note}.
    Null over inference: outside the confident window, refrigerant is None
    rather than a guess — see R410A_WINDOW_START/END above."""
    if install_year is None:
        return {"refrigerant": None, "confidence": None,
                "note": "no install year available"}
    if R410A_WINDOW_START <= install_year <= R410A_WINDOW_END:
        return {"refrigerant": "R-410A", "confidence": "likely",
                "note": f"installed {install_year}, within R-410A's dominant "
                        f"service window ({R410A_WINDOW_START}-{R410A_WINDOW_END})"}
    if install_year < R410A_WINDOW_START:
        return {"refrigerant": None, "confidence": None,
                "note": f"installed {install_year}, before R-410A's dominant window — "
                        f"could be R-22 or early R-410A, not distinguishable from install year alone"}
    return {"refrigerant": None, "confidence": None,
            "note": f"installed {install_year}, at or after the A2L transition — "
                    f"not safely assumed to be R-410A"}


def sb1206_status(cfg: Config, install_year: int | None, as_of: date | None = None) -> dict | None:
    """If the equipment is inferred R-410A, return which SB 1206 GWP tier
    already applies (or will next apply) to it, from the verified trigger
    config. Returns None when refrigerant can't be inferred, or the trigger
    entry itself isn't verified (see customer_facing_triggers) — a permit
    row with no confident finding produces no trigger, not a caveated one."""
    trigger = customer_facing_triggers(cfg).get("sb_1206_hfc_phasedown")
    if not trigger:
        return None
    inferred = infer_refrigerant(install_year)
    if inferred["refrigerant"] != "R-410A":
        return None

    as_of = as_of or date.today()
    tiers = sorted(trigger["tiers"], key=lambda t: date.fromisoformat(t["effective_date"]))
    active_tier = None
    next_tier = None
    for tier in tiers:
        eff = date.fromisoformat(tier["effective_date"])
        if eff <= as_of:
            active_tier = tier
        elif next_tier is None:
            next_tier = tier

    if active_tier and R410A_GWP > active_tier["gwp_max"]:
        return {
            "trigger": "sb_1206_hfc_phasedown",
            "status": "in_effect",
            "gwp_max": active_tier["gwp_max"],
            "effective_date": active_tier["effective_date"],
            "detail": f"Virgin R-410A (GWP {R410A_GWP}) sale is already banned "
                     f"under the {active_tier['gwp_max']} GWP tier, effective "
                     f"{active_tier['effective_date']}.",
            "source_url": trigger["source_url"],
        }
    if next_tier and R410A_GWP > next_tier["gwp_max"]:
        return {
            "trigger": "sb_1206_hfc_phasedown",
            "status": "upcoming",
            "gwp_max": next_tier["gwp_max"],
            "effective_date": next_tier["effective_date"],
            "detail": f"Virgin R-410A (GWP {R410A_GWP}) sale becomes banned "
                     f"under the {next_tier['gwp_max']} GWP tier on "
                     f"{next_tier['effective_date']}.",
            "source_url": trigger["source_url"],
        }
    return None


# LAMC Section 91.9708, Table 9708.2 -- A/RCx (Audit and Retro-Commissioning)
# compliance is due once every 5 years, keyed to the LAST DIGIT of the
# LADBS Building ID (a 12-digit id LADBS assigns per building, distinct
# from the assessor AIN). Transcribed verbatim from LADBS's own "Audits and
# Retro-Commissioning FAQs" PDF (dbs.lacity.gov/sites/default/files/efs/
# forms/pc17/EBEWE-ARCx-FAQs-FINAL.pdf, "Last Updated 04/30/2026"), FAQ #2,
# which reproduces Table 9708.2 as an image and independently states (FAQ
# #1) "The next A/RCx compliance due date for Building IDs ending in 0 or 1
# is still December 1, 2026" and "...ending in 2 or 3 is still December 1,
# 2027" -- both consistent with the table below (2021+5=2026, 2022+5=2027),
# corroborating the OCR read against the document's own prose. This is a
# LEGAL COMPLIANCE SCHEDULE, not a judgment call or an estimate -- see
# app/assumptions.py for why it's listed there anyway (every new constant
# this task added, verified fact or not).
ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT = {
    0: 2021, 1: 2021,
    2: 2022, 3: 2022,
    4: 2023, 5: 2023,
    6: 2024, 7: 2024,
    8: 2025, 9: 2025,
}
ARCX_CYCLE_YEARS = 5
ARCX_SOURCE_URL = "https://dbs.lacity.gov/sites/default/files/efs/forms/pc17/EBEWE-ARCx-FAQs-FINAL.pdf"


def arcx_compliance_status(building_id: str | None, as_of: date | None = None) -> dict | None:
    """Whether `building_id` (a LADBS Building ID, not an AIN) owes an
    A/RCx filing in as_of's calendar year (default: today), and its next
    compliance date regardless of whether that's this year. Returns None if
    building_id is missing or doesn't end in a digit -- never guessed.

    Compares CALENDAR YEARS, not the exact December 1 due date -- exact
    because every cycle year for a given last digit is precisely 5 years
    apart starting from Table 9708.2's initial year (see
    ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT), so "as_of.year is one of
    those years" is not an approximation of the real rule, it IS the real
    rule at year granularity. next_compliance_date is still the exact
    December 1 date for a caller that needs it (e.g. to know a due-this-
    year building's filing deadline has or hasn't passed yet within the
    year)."""
    if not building_id or not building_id[-1].isdigit():
        return None
    last_digit = int(building_id[-1])
    initial_year = ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT[last_digit]
    as_of = as_of or date.today()

    if as_of.year < initial_year:
        next_due_year = initial_year
    else:
        offset = as_of.year - initial_year
        remainder = offset % ARCX_CYCLE_YEARS
        next_due_year = as_of.year if remainder == 0 else as_of.year + (ARCX_CYCLE_YEARS - remainder)

    return {
        "last_digit": last_digit,
        "initial_compliance_year": initial_year,
        "due_this_year": next_due_year == as_of.year,
        "next_compliance_date": date(next_due_year, 12, 1),
        "source_url": ARCX_SOURCE_URL,
    }
