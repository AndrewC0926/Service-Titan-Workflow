"""HCAI Facilities Development Division project reports -- "Projects by
County" (report.hcai.ca.gov, division OSHPD, report id 38). See
app.models.HcaiProject's own docstring for the table design and
app/assumptions.py's "HCAI Facilities Development Division project
reports" entry for the Phase A access findings this rests on
(2026-09-08/09): no robots.txt on report.hcai.ca.gov/hcai.ca.gov/
esp.hcai.ca.gov (all 404), hcai.ca.gov's own Conditions of Use states
public domain, no automated-access restriction.

NOT a live fetcher, deliberately: the report was pulled ONCE by hand with
Playwright (same "hand-pulled, statically stored" precedent as
app/pipeline/ab869.py's PDF corpus and app/pipeline/scaqmd.py's CARB CSV),
saved to docs/sources-pilot/hcai-reports/, and report.hcai.ca.gov is not
touched again by this module or by `scout load-hcai-projects` -- a future
refresh means pulling a new export by hand, the same way, and re-running
the load command against the new file.

FILE SHAPE: SSRS's own generic Textbox* column names, renamed here to
their real meaning:
  Textbox145 -> report title, carries the report's own "as of MM/DD/YYYY"
                date -- parsed once as report_date, same for every row in
                one load.
  Textbox139 -> "NN - County Name" (e.g. "19 - Los Angeles") -- county is
                the name after the dash.
  Textbox140 -> "FACILITY_ID - FACILITY NAME\\nADDRESS" -- confirmed 100%
                consistent across all 45,132 rows in the Phase A pull, no
                exceptions found.
  Textbox126 -> Compliance Officer (not stored -- not part of this
                table's scope).
  ProjNo -> record_no (globally unique, confirmed: 45,132 of 45,132
            distinct in the Phase A pull -- the primary key).
  ParentId -> parent_no.
  ScopeofWork -> scope_text.
  FileDate -> date_in.
  Costs -> cost_est ("-" or blank means not stated, never zero).
  PctComplete -> pct_complete.
  Status -> status_raw, verbatim, PLUS this application's own `stage`
            collapse (see STAGE_MAP below).
  Textbox28, Textbox170 -> facility-level running totals (cost, avg
                %complete) -- not stored, derivable from this table's own
                rows per facility_id if ever needed.

STAGE_MAP: HCAI's own 23 raw Status values collapse to five buckets --
plan_review, pending_start, in_construction, closed, other. This is THIS
APPLICATION'S classification, not HCAI's own field; status_raw is always
kept alongside it, verbatim, never replaced. A status this map has never
seen (a new HCAI status introduced after 2026-09) maps to "other" via
STAGE_MAP.get(..., "other") -- never dropped, never guessed into a more
specific bucket. See app/assumptions.py for the reasoning behind each of
the 23 individual choices (why "Approved" is pending_start and not
plan_review, why "Cancelled"/"Withdrawn"/"Void" are closed rather than
their own bucket, why bare "Pending" and bare "Active" -- too ambiguous
standing alone -- are "other" rather than guessed toward a specific
stage).

IS_MECHANICAL: a plain regex over scope_text, no LLM -- see MECHANICAL_RE.
Same discipline as app.pipeline.opsc's direct-Signal-construction: every
fact here is already an exact, government-typed value, so an LLM
round-trip adds cost and hallucination risk for zero benefit. False means
"the keyword set didn't match," never "confirmed non-mechanical." A
second pass, NEGATIVE_MECHANICAL_RE, clears is_mechanical back to False
when a scope line ALSO matches a non-mechanical keyword (fire alarm,
seismic bracing, etc.) commonly found alongside a positive keyword in a
scope line that isn't actually mechanical work -- see that constant's own
comment and app/assumptions.py for the measured effect.
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime

from sqlmodel import Session, select

from app.models import HcaiProject, HospitalBuilding, SourceRun, utcnow

log = logging.getLogger(__name__)

SOURCE = "hcai_projects"
DEFAULT_CSV_PATH = "docs/sources-pilot/hcai-reports/projects-by-county-7-counties-all-statuses.csv"

# HCAI's own 23 raw Status values -> this application's five-bucket stage.
# Every value seen in the 2026-09-08 Phase A pull (45,132 rows) is mapped
# explicitly below; nothing is left to the get(..., "other") default by
# omission -- that default exists only for a genuinely NEW status HCAI
# introduces after this mapping was written.
STAGE_MAP: dict[str, str] = {
    # closed -- no longer an active pipeline item, for any reason
    "Closed": "closed",
    "Closed Inactive": "closed",
    "Cancelled": "closed",
    "Closed Non-CAC Compliant": "closed",
    "Closed Non-CBSC Compliant": "closed",
    "Withdrawn": "closed",
    "Void": "closed",
    "Closed Administrative": "closed",
    "Expired": "closed",
    # plan_review -- design/application is still being reviewed
    "Open - Remarked": "plan_review",
    "Active - Plan Review": "plan_review",
    "Open - Returned": "plan_review",
    "Open": "plan_review",
    "Active - Triage": "plan_review",
    # pending_start -- design approved / permit issued, construction not
    # yet begun
    "Pending Construction Start": "pending_start",
    "Approved": "pending_start",
    "Pending Field Operations": "pending_start",
    "Issued": "pending_start",
    # in_construction -- field/construction activity underway (including
    # temporarily suspended -- the project IS under construction, just
    # paused, not back in plan review)
    "Field Operations in Progress": "in_construction",
    "Field Operations Suspended": "in_construction",
    "Field Operations Resumed": "in_construction",
    # other -- too ambiguous standing alone to place in a specific stage;
    # never guessed toward one
    "Pending": "other",
    "Active": "other",
}

OPEN_STAGES = ("plan_review", "pending_start", "in_construction")

# Same keyword set used in Phase A's own scope-of-work scan (2026-09-08),
# now registered as a real regex -- see app/assumptions.py for the
# measured precision sample. Word-boundary matched, case-insensitive.
MECHANICAL_KEYWORDS = [
    "HVAC", "mechanical", "chiller", "boiler", "cooling tower", "central plant",
    "air handler", "AHU", "RTU", "VRF", "heat pump", "exhaust",
]
MECHANICAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in MECHANICAL_KEYWORDS) + r")\b", re.IGNORECASE)

# Follow-up (2026-09-10, HCAI mechanical-scope false-positive check): none of
# these words are themselves mechanical scope, but each shares a positive
# keyword's word stem or commonly appears alongside one in a non-mechanical
# scope line ("replace fire alarm and mechanical smoke detector wiring",
# "seismic bracing for boiler anchorage" -- a real seismic/structural
# project that happens to mention the mechanical equipment being braced, not
# mechanical work on it). A scope line matching BOTH a positive keyword and
# one of these clears is_mechanical back to False -- see
# app/assumptions.py's "HCAI Facilities Development Division project
# reports" entry for the measured before/after counts and the 10 rows this
# flipped, read individually, not just counted.
NEGATIVE_MECHANICAL_KEYWORDS = [
    "anchorage", "receptacle", "detector", "fire alarm", "seismic",
    "bracing", "lighting", "roofing", "signage",
]
NEGATIVE_MECHANICAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in NEGATIVE_MECHANICAL_KEYWORDS) + r")\b", re.IGNORECASE)

_REPORT_DATE_RE = re.compile(r"as of (\d{2}/\d{2}/\d{4})")


def _num(val: str | None) -> float | None:
    if not val:
        return None
    s = val.strip().replace("$", "").replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(val: str | None) -> datetime | None:
    if not val or not val.strip():
        return None
    try:
        return datetime.strptime(val.strip(), "%m/%d/%Y")
    except ValueError:
        return None


def parse_report_date(title: str) -> datetime | None:
    m = _REPORT_DATE_RE.search(title or "")
    return datetime.strptime(m.group(1), "%m/%d/%Y") if m else None


def parse_rows(raw_bytes: bytes) -> list[dict]:
    """Parses the SSRS CSV export. Textbox140's "ID - NAME\\nADDRESS"
    shape was confirmed 100% consistent across all 45,132 rows in the
    Phase A pull; a row that doesn't match is skipped (facility_id would
    otherwise be a guess) rather than stored with a guessed split."""
    import io as _io

    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(_io.StringIO(text))
    out = []
    for row in reader:
        record_no = (row.get("ProjNo") or "").strip()
        if not record_no:
            continue

        facility_block = row.get("Textbox140") or ""
        if " - " not in facility_block:
            continue
        fac_id, _, rest = facility_block.partition(" - ")
        fac_id = fac_id.strip()
        if "\n" in rest:
            fac_name, fac_address = rest.split("\n", 1)
        else:
            fac_name, fac_address = rest, None
        fac_name = fac_name.strip()
        fac_address = fac_address.strip() if fac_address else None

        county_raw = (row.get("Textbox139") or "").strip()
        county = county_raw.split(" - ", 1)[1].strip() if " - " in county_raw else county_raw

        status_raw = (row.get("Status") or "").strip()
        stage = STAGE_MAP.get(status_raw, "other")
        scope_text = (row.get("ScopeofWork") or "").strip()

        out.append({
            "record_no": record_no,
            "parent_no": (row.get("ParentId") or "").strip() or None,
            "facility_id": fac_id,
            "facility_name": fac_name,
            "facility_address": fac_address,
            "county": county,
            "scope_text": scope_text,
            "date_in": _date(row.get("FileDate")),
            "cost_est": _num(row.get("Costs")),
            "pct_complete": _num(row.get("PctComplete")),
            "status_raw": status_raw,
            "stage": stage,
            "is_mechanical": bool(MECHANICAL_RE.search(scope_text))
                            and not bool(NEGATIVE_MECHANICAL_RE.search(scope_text)),
            "report_date": parse_report_date(row.get("Textbox145") or ""),
        })
    return out


def load_hcai_projects(session: Session, csv_path: str = DEFAULT_CSV_PATH) -> dict:
    """Idempotent upsert by record_no. Reads the static CSV already on
    disk (see this module's docstring for how it got there -- a one-off
    Playwright pull, never a live fetch). Own SourceRun for `scout
    doctor`/source_health visibility."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    fetched, new_count, updated_count, error = 0, 0, 0, None
    try:
        with open(csv_path, "rb") as fh:
            raw = fh.read()
        rows = parse_rows(raw)
        fetched = len(rows)

        existing = {p.record_no: p for p in session.exec(select(HcaiProject)).all()}
        for r in rows:
            p = existing.get(r["record_no"])
            if p is None:
                session.add(HcaiProject(**r))
                new_count += 1
            else:
                for k, v in r.items():
                    setattr(p, k, v)
                session.add(p)
                updated_count += 1
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("HCAI project load failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = new_count
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()

    return {"fetched": fetched, "new": new_count, "updated": updated_count, "error": error}


def hcai_report_date(session: Session) -> datetime | None:
    """The single report_date carried by every HcaiProject row (they all
    come from one file load) -- for the board's staleness display. None
    if the table is empty (never loaded yet)."""
    return session.exec(select(HcaiProject.report_date)).first()


def hcai_match_rate(session: Session) -> dict:
    """Facility-id join coverage against HospitalBuilding.perm_id --
    reported after every load, never hidden (Charter invariant 6: no
    join hides its miss rate)."""
    hcai_facility_ids = set(session.exec(select(HcaiProject.facility_id)).all())
    perm_ids = set(session.exec(select(HospitalBuilding.perm_id)).all())
    matched = hcai_facility_ids & perm_ids
    unmatched = hcai_facility_ids - perm_ids
    return {
        "hcai_facilities": len(hcai_facility_ids),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "unmatched_facility_ids": sorted(unmatched),
    }


def open_hcai_projects_by_facility_id(session: Session) -> dict[str, list[HcaiProject]]:
    """{facility_id: [HcaiProject, ...]} for every OPEN (plan_review,
    pending_start, in_construction) project, regardless of mechanical
    flag -- the board-surfacing query for /ab869 (facility_id is the same
    value as HospitalBuilding.perm_id where the two join). Mechanical
    rows sorted first within each facility, per spec."""
    rows = session.exec(
        select(HcaiProject).where(HcaiProject.stage.in_(OPEN_STAGES))
    ).all()
    out: dict[str, list[HcaiProject]] = {}
    for p in rows:
        out.setdefault(p.facility_id, []).append(p)
    for perm_id, projects in out.items():
        # Two stable sorts: most-recent first within each group, then
        # mechanical rows moved ahead of non-mechanical, preserving each
        # group's own date order.
        projects.sort(key=lambda p: p.date_in or datetime.min, reverse=True)
        projects.sort(key=lambda p: not p.is_mechanical)
    return out
