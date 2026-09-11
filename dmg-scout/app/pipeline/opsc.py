"""OPSC School Facility Program -- data.ca.gov's own "School Facility Program
Funding" bulk CSV (CKAN package dd1eabf1-0b66-49d6-857d-8cef6ed93d45,
resource 8080bb19-a63b-47e3-82d3-7451d119e27f) plus the SAB Modernization and
New Construction Workload List PDFs (dgs.ca.gov/OPSC). See
app.models.OpscProject/OpscWorkload for the table design.

COMPLIANCE, checked before this shipped (Phase A research 2026-09-06,
re-confirmed 2026-09-08): data.ca.gov/robots.txt disallows /api/ and
/datastore/* for User-agent: * -- the SAME CKAN-standard block CHHS's own
robots.txt uses (see app/pipeline/hcai.py's module docstring), which is why
this fetcher NEVER calls datastore_search on any schedule, only the plain
bulk-CSV resource-download URL (.../download/school-facility-program-
funding.csv), which is NOT in that disallow list -- /dataset/activity/*,
/dataset/groups/*, /dataset/showcases/*, /dataset/*/issues/* are the only
/dataset/ paths blocked, none of which match a resource download URL. The
same statewide ca.gov Conditions of Use CAEATFA's own module docstring
already quotes (public domain, no automated-access restriction) applies
here too -- data.ca.gov is a CDT property under the identical policy. This
fetcher is CAEATFA-shaped on the path it actually uses (a sanctioned bulk
download) and CHHS-shaped on the path it deliberately avoids (the
disallowed API) -- see app/assumptions.py's "OPSC School Facility Program"
entry.

FETCH CADENCE: monthly, per the dataset's own CKAN accrualPeriodicity
(R/P1M) -- confirmed against live metadata 2026-09-06. NOT called from
`scout pipeline` at any cadence -- same "run by hand" treatment as
app/pipeline/ab802.py: `scout fetch-opsc-projects` / `scout
fetch-opsc-workload` are run manually.

TABLE: full replace of the WHOLE table on every load -- this file is one
continuously-updated snapshot (14,506+ rows as of 2026-09-06, spanning
decades), not an annual series like AB 802's, so there is no per-year
partition to preserve.

SIGNAL GATING: a Signal is only ever created for an in-territory row whose
Last_SAB_Date falls within the last 18 months, OR whose Status differs from
what it was on the PRIOR load. On a true cold start (no OpscProject row has
ever been loaded before), "status changed" has no meaningful prior to
compare against, so only the 18-month recency test applies -- treating
every one of 14,506+ historical rows as "changed" on the very first run
would flood the signal pipeline with rows nobody asked to see. Every other
row is still stored in OpscProject (the full history stays queryable) but
stays inert: no RawDocument, no Signal, for that load.

Signals are built DIRECTLY from the CSV's own structured fields, never
through the LLM extraction pipeline (contrast app/sources/caeatfa.py, which
transcribes a row into raw_text and lets the standard triage/extract stage
re-parse it): every fact here is already an exact, unambiguous government
value, so an LLM round-trip would add cost and hallucination risk for zero
benefit. Same discipline app.manual.add_manual_signal already uses for
hand-entered signals -- just machine-driven instead of a human typing a
form. A RawDocument is still written (transcribed row, verbatim) for the
same audit-trail reason CAEATFA's own row-per-RawDocument choice exists.

NEVER auto-links to a Project and NEVER auto-creates one -- see
app.pipeline.resolve._resolve_loop's own school_facility_funding branch:
this is the one SignalType that always queues a MatchCandidate (or does
nothing, if no fuzzy candidate exists at all) instead of taking the normal
auto-link/auto-create path, per this source's own explicit design.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import datetime, timedelta

from sqlmodel import Session, delete, select

from app.config import Config
from app.http import PoliteClient
from app.models import (
    Category, OpscProject, OpscStatusClass, OpscWorkload, RawDocument, Signal, SignalType,
    Stage, SourceRun, TriageResult, classify_opsc_status, utcnow,
)
from app.pdftext import pdf_to_text
from app.pipeline.size_score import in_territory as _territory_check

log = logging.getLogger(__name__)

SOURCE = "opsc_school_facility"
CSV_URL = ("https://data.ca.gov/dataset/dd1eabf1-0b66-49d6-857d-8cef6ed93d45/resource/"
          "8080bb19-a63b-47e3-82d3-7451d119e27f/download/school-facility-program-funding.csv")
PORTAL_URL = "https://data.ca.gov/dataset/opsc-open-data"

SIGNAL_RECENCY_MONTHS = 18
# Distinct from any real status string, including None -- a row genuinely
# absent from the prior load must never be conflated with a row that WAS
# present with a blank/null status.
_NOT_PREVIOUSLY_PRESENT = object()

_NOT_AVAILABLE = {"", "-", "n/a", "na"}


def _num(val) -> float | None:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("$", "")
    if s.lower() in _NOT_AVAILABLE:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _str(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() not in _NOT_AVAILABLE else None


def _date(val):
    s = _str(val)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except ValueError:
        return None


def parse_rows(raw_bytes: bytes) -> list[dict]:
    """One dict per data row, field names matching OpscProject 1:1. Uses
    the CSV's own header (DictReader), not positional indexing -- contrast
    app/pipeline/ab802.py's positional COL_* choice, made there because
    that file's headers carry fragile unicode punctuation ("²", an en
    dash); this CSV's headers are plain ASCII identifiers, materially less
    likely to drift silently, so name-based lookup is both simpler and
    safer here."""
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for row in reader:
        app_no = _str(row.get("Application_Number"))
        if not app_no:
            continue  # unkeyable -- can't upsert without the row's own unique key
        out.append({
            "county": _str(row.get("County")),
            "district": _str(row.get("District")),
            "school_name": _str(row.get("School_Name")),
            "program": _str(row.get("Program")),
            "application_number": app_no,
            "applicant": _str(row.get("Applicant")),
            "preliminary_grant_application": _str(row.get("Preliminary_Grant_Application")),
            "full_grant_application": _num(row.get("Full_Grant_Application")),
            "site_and_design_application": _num(row.get("Site_and_Design_Application")),
            "site_only_application": _num(row.get("Site_Only_Application")),
            "design_only_application": _num(row.get("Design_Only_Application")),
            "environmental_hardship_application": _str(row.get("Environmental_Hardship_Application")),
            "reduced_to_costs_incurred": _str(row.get("Reduced_to_Costs_Incurred")),
            "number_of_elementary_school_pupil_grants_requested":
                _str(row.get("Number_of_Elementary_School_Pupil_Grants_Requested")),
            "number_of_middle_school_pupil_grants_requested":
                _num(row.get("Number_of_Middle_School_Pupil_Grants_Requested")),
            "number_of_high_school_pupil_grants_requested":
                _num(row.get("Number_of_High_School_Pupil_Grants_Requested")),
            "number_of_non_severe_school_pupil_grants_requested":
                _num(row.get("Number_of_Non_Severe_School_Pupil_Grants_Requested")),
            "number_of_severe_school_pupil_grants_requested":
                _num(row.get("Number_of_Severe_School_Pupil_Grants_Requested")),
            "grade_level_of_project": _str(row.get("Grade_Level_of_Project")),
            "state_share_of_funding": _num(row.get("State_Share_of_Funding")),
            "site_acquisition": _num(row.get("Site_Acquisition")),
            "financial_hardship": _num(row.get("Financial_Hardship")),
            "csfa_lease_amount": _str(row.get("CSFA_Lease_Amount")),
            "ctefp_loan_amount": _str(row.get("CTEFP_Loan_Amount")),
            "type_of_joint_use_facility": _str(row.get("Type_of_Joint_Use_Facility")),
            "type_of_joint_use_partner": _str(row.get("Type_of_Joint_Use_Partner")),
            "industry_sector": _str(row.get("Industry_Sector")),
            "portables_replaced": _str(row.get("Portables_Replaced")),
            "last_sab_date": _date(row.get("Last_SAB_Date")),
            "status": _str(row.get("Status")),
        })
    return out


def signal_stage(program: str | None, status: str | None) -> Stage:
    """Program 'Modernization' or 'New Construction' with Status 'Funds
    Released' -> procurement (Window.POST_BOD, via
    app.pipeline.scoring.classify_window). Status 'Closed' -> operating: the
    application is terminal, so the underlying school project is done, not
    still in entitlement (WS3.4 fix -- see classify_opsc_status). Every
    other combination -> entitlement (Window.PRE_BOD). Never guessed past
    what these two literal fields say."""
    status_class = classify_opsc_status(status)
    if status_class == OpscStatusClass.funds_released and program in ("Modernization", "New Construction"):
        return Stage.procurement
    if status_class == OpscStatusClass.closed:
        return Stage.operating
    return Stage.entitlement


def _is_recent(last_sab_date, months: int = SIGNAL_RECENCY_MONTHS) -> bool:
    if last_sab_date is None:
        return False
    cutoff = utcnow() - timedelta(days=30.44 * months)  # average month length
    return last_sab_date >= cutoff


def _emit_signal(session: Session, rec: OpscProject) -> Signal:
    text = (
        f"OPSC SCHOOL FACILITY PROGRAM ({rec.application_number})\n"
        f"District: {rec.district or '-'} | School: {rec.school_name or '-'} | "
        f"County: {rec.county or '-'}\n"
        f"Program: {rec.program or '-'} | Status: {rec.status or '-'} | "
        f"Last SAB Date: {rec.last_sab_date.date() if rec.last_sab_date else '-'}\n"
        f"State Share of Funding: {rec.state_share_of_funding}"
    )
    doc = RawDocument(
        source=SOURCE, source_uid=rec.application_number, url=rec.source_url,
        title=f"OPSC {rec.application_number}: {rec.school_name or rec.district or ''}",
        raw_text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(),
        triage_result=TriageResult.relevant, triage_reason="structured government funding record",
        processed_at=utcnow(),
        meta={"default_signal_type": SignalType.school_facility_funding.value,
             "triage_category": Category.industrial.value},
    )
    session.add(doc)
    session.flush()

    signal = Signal(
        raw_document_id=doc.id, signal_type=SignalType.school_facility_funding,
        category=Category.industrial, project_name=rec.school_name,
        developer_or_owner=rec.district, county=rec.county, state="CA",
        stage=signal_stage(rec.program, rec.status), event_date=rec.last_sab_date,
        summary_one_line=(f"{rec.program or 'OPSC'} funding, Status: {rec.status or 'unknown'}")[:300],
        confidence=1.0,
    )
    session.add(signal)
    session.flush()
    return signal


def fetch_opsc_projects(session: Session, cfg: Config, client: PoliteClient) -> dict:
    """Downloads the bulk CSV, full-replaces the WHOLE opsc_projects table,
    computes in_territory, then emits a Signal for exactly the rows this
    source's own gating allows -- see module docstring for the 18-month/
    status-changed rule and why a cold start never floods the pipeline."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    fetched = stored = signals_created = 0
    error = None
    try:
        raw = client.get_bytes(CSV_URL)
        rows = parse_rows(raw)
        fetched = len(rows)

        # Snapshot the PRIOR load's status per application_number BEFORE
        # replacing the table -- the only source of "status changed since
        # prior load", so it must be read before the delete below.
        prior_status: dict[str, object] = {
            r.application_number: r.status for r in session.exec(select(OpscProject)).all()
        }
        is_cold_start = len(prior_status) == 0

        session.exec(delete(OpscProject))

        territory_cfg = cfg.get("territory") or {}
        new_rows: list[OpscProject] = []
        for parsed in rows:
            territory = bool(territory_cfg) and _territory_check(cfg, "CA", parsed["county"])
            rec = OpscProject(source_url=CSV_URL, in_territory=territory, **parsed)
            session.add(rec)
            new_rows.append(rec)
        session.flush()
        stored = len(new_rows)

        for rec in new_rows:
            if not rec.in_territory:
                continue
            recent = _is_recent(rec.last_sab_date)
            changed = (not is_cold_start and prior_status.get(
                rec.application_number, _NOT_PREVIOUSLY_PRESENT) != rec.status)
            if not (recent or changed):
                continue
            _emit_signal(session, rec)
            signals_created += 1

        session.commit()
    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("OPSC project fetch failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = stored
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()
    return {"fetched": fetched, "stored": stored, "signals_created": signals_created, "error": error}


# ---- SAB Workload Lists (in house, not yet funded) -------------------------

CA_COUNTIES = [
    "Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa", "Del Norte",
    "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake",
    "Lassen", "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced", "Modoc",
    "Mono", "Monterey", "Napa", "Nevada", "Orange", "Placer", "Plumas", "Riverside",
    "Sacramento", "San Benito", "San Bernardino", "San Diego", "San Francisco", "San Joaquin",
    "San Luis Obispo", "San Mateo", "Santa Barbara", "Santa Clara", "Santa Cruz", "Shasta",
    "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter", "Tehama", "Trinity",
    "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba",
]
_COUNTY_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in sorted(CA_COUNTIES, key=len, reverse=True)) + r")\b"
)
_APP_NO_RE = re.compile(r"(\d{2}/\d{4,6}-\d{2}-\d{3})")

WORKLOAD_URLS = {
    "New Construction": ("https://www.dgs.ca.gov/-/media/Divisions/OPSC/Resources/"
                         "Workload-Lists/SAB-NC-Current-WorkLoad-Report-ADA.pdf"),
    "Modernization": ("https://www.dgs.ca.gov/-/media/Divisions/OPSC/Resources/"
                      "Workload-Lists/SAB-MOD-Current-WorkLoad-Report-ADA.pdf"),
}
WORKLOAD_LABEL = "application in house, not funded"
# Below this parse rate, that program's half is reported and skipped rather
# than loading a table more wrong than right -- see fetch_opsc_workload.
WORKLOAD_MIN_PARSE_RATE = 0.90


def parse_workload_text(text: str) -> dict:
    """{"rows": [...], "parsed": n, "failed": n, "fail_examples": [...]}.

    Each candidate line (one containing an Application_Number, via
    _APP_NO_RE) is split on the LAST whole-word California county name
    found before that number: District is everything before it, School
    Name everything after. Rightmost, not first: many CA district names
    contain their own county's name (e.g. "Riverside Unified ... Riverside
    ..."), which a first-match anchor mis-splits -- measured 2026-09-08,
    taking the rightmost match instead fixed 38 of 40 failures on the New
    Construction list alone (88.3% -> 99.4%; 99.8% on Modernization). The
    remaining ~0.2-0.6% are a PDF text-extraction artifact, not a
    parsing-logic gap: two adjacent words merge with no space at all in
    the extracted text (e.g. "...EducationTehama..."), so no word boundary
    exists to find -- correctly reported as a failure, never guessed at.

    Split from parse_workload_pdf so the parsing logic itself is testable
    against plain text, without needing to construct a real PDF."""
    lines = [line for line in text.splitlines() if _APP_NO_RE.search(line)]
    rows: list[dict] = []
    fail_examples: list[str] = []
    failed = 0
    for line in lines:
        m = _APP_NO_RE.search(line)
        app_no = m.group(1)
        before = line[:m.start()].strip()
        matches = list(_COUNTY_RE.finditer(before))
        if not matches:
            failed += 1
            if len(fail_examples) < 10:
                fail_examples.append(line.strip())
            continue
        last = matches[-1]
        district = before[:last.start()].strip()
        school = before[last.end():].strip()
        if not district or not school:
            failed += 1
            if len(fail_examples) < 10:
                fail_examples.append(line.strip())
            continue
        rows.append({"district": district, "school_name": school,
                     "application_number": app_no, "raw_row_text": line.strip()})
    return {"rows": rows, "parsed": len(rows), "failed": failed, "fail_examples": fail_examples}


def parse_workload_pdf(raw_bytes: bytes) -> dict:
    """parse_workload_text over this PDF's own extracted text -- see that
    function's docstring for the parsing method."""
    return parse_workload_text(pdf_to_text(raw_bytes))


def fetch_opsc_workload(session: Session, cfg: Config, client: PoliteClient) -> dict:
    """Fetches both SAB workload PDFs, parses each with parse_workload_pdf,
    full-replaces opsc_workload PER PROGRAM. If either PDF's parse rate
    falls below WORKLOAD_MIN_PARSE_RATE, that program's half is reported
    and SKIPPED (existing opsc_workload rows for that program are left
    untouched) rather than loading a table more wrong than right -- the
    funded dataset (fetch_opsc_projects) is entirely separate and is never
    affected by this."""
    results: dict[str, dict] = {}
    for program, url in WORKLOAD_URLS.items():
        run_name = f"{SOURCE}_workload_{program.lower().replace(' ', '_')}"
        run = SourceRun(source=run_name)
        session.add(run)
        session.commit()

        error = None
        result = {"parsed": 0, "failed": 0, "rate": 0.0, "loaded": False}
        try:
            raw = client.get_bytes(url)
            parsed = parse_workload_pdf(raw)
            total = parsed["parsed"] + parsed["failed"]
            rate = parsed["parsed"] / total if total else 0.0
            result.update(parsed=parsed["parsed"], failed=parsed["failed"], rate=rate)
            if rate < WORKLOAD_MIN_PARSE_RATE:
                error = (f"parse rate {100*rate:.1f}% below the {100*WORKLOAD_MIN_PARSE_RATE:.0f}% "
                        f"floor ({parsed['failed']} of {total} rows failed) -- not loaded")
                result["fail_examples"] = parsed["fail_examples"]
                log.error("OPSC workload (%s): %s", program, error)
            else:
                session.exec(delete(OpscWorkload).where(OpscWorkload.program == program))
                for row in parsed["rows"]:
                    session.add(OpscWorkload(program=program, label=WORKLOAD_LABEL,
                                             source_url=url, **row))
                session.commit()
                result["loaded"] = True
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            error = f"{type(exc).__name__}: {exc}"
            log.error("OPSC workload (%s) fetch failed: %s", program, error)

        result["error"] = error
        results[program] = result

        run.finished_at = utcnow()
        run.records_fetched = result["parsed"] + result["failed"]
        run.records_new = result["parsed"] if result["loaded"] else 0
        run.ok = error is None
        run.error = error
        session.add(run)
        session.commit()

    return results


# ---- board: the Schools tab -------------------------------------------


def schools_board(session: Session, cfg: Config, *, county: str | None = None,
                  district: str | None = None, program: str | None = None,
                  status: str | None = None, grade_level: str | None = None) -> list[dict]:
    """In-territory OpscProject rows, most recent Last_SAB_Date first, each
    paired with its call target -- see app.call_target.opsc_call_target for
    the rule (owner_standards for a district on the standards list,
    otherwise bidding_contractors once Funds Released, engineer before
    that). Rows with no Last_SAB_Date at all (never yet reached the SAB)
    sort last, not first -- an unstated date is not "most recent".

    Closed applications (classify_opsc_status == closed) are excluded from
    the DEFAULT board -- WS3.4 fix: a closed application is a terminal, dead
    lead, not an active one. Only excluded when `status` isn't explicitly
    requested -- `schools_board(..., status="Closed")` still returns them,
    same as any other explicit status filter; the exclusion is a default-view
    behavior, not a hard delete or a claim the rows don't exist."""
    from app.call_target import opsc_call_target

    q = select(OpscProject).where(OpscProject.in_territory == True)  # noqa: E712
    if county:
        q = q.where(OpscProject.county == county)
    if district:
        q = q.where(OpscProject.district == district)
    if program:
        q = q.where(OpscProject.program == program)
    if status:
        q = q.where(OpscProject.status == status)
    if grade_level:
        q = q.where(OpscProject.grade_level_of_project == grade_level)
    rows = session.exec(q).all()
    if status is None:
        rows = [r for r in rows if classify_opsc_status(r.status) != OpscStatusClass.closed]
    rows.sort(key=lambda r: r.last_sab_date or datetime.min, reverse=True)

    return [{"row": r, "call_target": opsc_call_target(cfg, r.district, r.status)} for r in rows]
