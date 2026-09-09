"""BPELSG (Board for Professional Engineers, Land Surveyors, and Geologists)
Mechanical Engineer roster -- DCA's own free monthly licensee bulk file.

ACCESS, checked 2026-09-08: DCA's Public Information page
(www.dca.ca.gov/consumers/public_info/index.shtml) is robots.txt-clean --
its own robots.txt disallows only /images, /js, /styles, /ssi, /css,
/javascript, /webapplications/ (with /webapplications/apps/*/ carved back
open), and /maintenance/*, nothing touching /consumers/public_info. That
page embeds a Box shared folder (dca.box.com/s/oss6hf8jys2bmgxqd2gdz7w4oepm2il9)
holding one subfolder per DCA board. box.com's own robots.txt disallows
everything for User-agent: * EXCEPT an explicit carve-out for shared-link
paths -- 'Allow: /s/', 'Allow: /shared/', with the comment "the 'noindex'
robots directive is only respected if these pages are crawlable" -- i.e.
Box deliberately permits fetching a shared-link path, gating search-engine
INDEXING via a noindex meta tag instead of via robots.txt. This shared
folder's own URL is exactly that /s/ pattern, so it is not a blocked host.

Per the explicit instruction this was built under: ONE Playwright visit to
the Box folder (navigate in, select the two files, trigger Box's own
bulk-download), the resulting files saved under docs/bpelsg/, never
polled again -- same "hand-pulled, statically stored" precedent as
app/pipeline/ab869.py's PDF corpus and app/pipeline/scaqmd.py's CARB CSV.
Box's own "download" button on a shared folder is a BULK (zip) download
regardless of how many rows are selected -- both files arrived inside one
zip archive, unzipped once by hand into docs/bpelsg/.

FILE SHAPE: despite the .xls extension, this is a plain tab-delimited
text file (confirmed by inspection, not assumed -- `file` reports
"ASCII text"). Columns, verbatim from the header row: Agency Name,
License Type, Speciality Code, License Number, Indiv/Org, Org/Last Name,
First Name, Middle Name, Suffix, Address Line 1, Address Line 2, City,
County, State, Zip, Country, Original Issue Date, Expiration Date,
School, Year Graduated, Degree, License Status.

NO FIRM FIELD: Indiv/Org is 'I' for all 119,766 rows in the 2026-09-01
file, every license type, statewide -- there are zero organizational/
firm-held licenses anywhere in this file. Org/Last Name holds a PERSON'S
last name for every row, never a firm name. This table therefore never
attempts a firm-name join -- only a normalized-person-name join (see
match_bpelsg_for_project) -- and app.models.BpelsgEngineer's own
docstring says so directly.

CADENCE: monthly. DCA's own Public Information page states, verbatim,
"Data is refreshed automatically at the beginning of each month." The
2026-09-08 pull's file was dated 2026-09-01 in both the Box listing and
the file's own mtime.

SCOPE: License Type == "Mechanical Engineer" only, County in Scout's own
7-county California territory only (config.yaml's
territories.california.counties) -- every other row is read but dropped,
not stored. This is a narrow, purpose-built table, not a general BPELSG
mirror.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.models import BpelsgEngineer, Project, ProjectSignal, Signal, SourceRun, utcnow
from app.normalize import normalize_person_name

log = logging.getLogger(__name__)

SOURCE = "bpelsg_mechanical_roster"
DEFAULT_FILE_PATH = "docs/bpelsg/ProfEngrsLandSurvyrsGeologist_Data00.xls"
PORTAL_URL = "https://www.dca.ca.gov/consumers/public_info/index.shtml"
LICENSE_TYPE = "Mechanical Engineer"

# Titles in Signal.named_people that mark a raw-extracted PERSON as filling
# the mep_engineer role -- plain substring, case-insensitive, same
# discipline as app.call_target._matches_standards_owner. This is
# deliberately separate from ProjectFirm.role == "mep_engineer", which is
# always a FIRM (see app/firms.py's ROLE_TO_TYPE) -- named_people carries
# no structured role field at all, only free-text title.
MEP_ENGINEER_TITLE_MARKERS = ("mechanical engineer", "mep engineer")


def _parse_date(s: str | None) -> datetime | None:
    if not s or not s.strip():
        return None
    try:
        return datetime.strptime(s.strip(), "%m-%d-%Y")
    except ValueError:
        return None


def parse_rows(raw_bytes: bytes, territory_counties: set[str]) -> list[dict]:
    """Parses the tab-delimited file, keeping only Mechanical Engineer
    licenses with a California address in an in-territory county. Every
    other row is read (so a header/format change is still caught) but
    dropped before returning -- never stored."""
    import io as _io

    text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.DictReader(_io.StringIO(text), delimiter="\t")
    out = []
    for row in reader:
        if (row.get("License Type") or "").strip() != LICENSE_TYPE:
            continue
        state = (row.get("State") or "").strip()
        county = (row.get("County") or "").strip()
        if state != "CA" or county not in territory_counties:
            continue
        last = (row.get("Org/Last Name") or "").strip()
        first = (row.get("First Name") or "").strip()
        middle = (row.get("Middle Name") or "").strip()
        name = " ".join(p for p in (first, middle, last) if p)
        out.append({
            "license_no": (row.get("License Number") or "").strip(),
            "name": name,
            "license_type": LICENSE_TYPE,
            "city": (row.get("City") or "").strip() or None,
            "county": county,
            "status": (row.get("License Status") or "").strip(),
            "expiry": _parse_date(row.get("Expiration Date")),
        })
    return [r for r in out if r["license_no"] and r["name"]]


def load_bpelsg_engineers(session: Session, cfg: Config, file_path: str = DEFAULT_FILE_PATH,
                          file_date: datetime | None = None) -> dict:
    """Idempotent upsert by license_no, scoped to in-territory Mechanical
    Engineer rows only. Reads the static file already on disk (see this
    module's docstring for how it got there) -- never a live fetch. Own
    SourceRun for `scout doctor`/source_health visibility, same pattern as
    every other hand-run source in this codebase."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    fetched, new_count, updated_count, error = 0, 0, 0, None
    try:
        territory_counties = set(cfg.get("territories.california.counties", []))
        with open(file_path, "rb") as fh:
            raw = fh.read()
        rows = parse_rows(raw, territory_counties)
        fetched = len(rows)

        fd = file_date or utcnow()
        existing = {e.license_no: e for e in session.exec(select(BpelsgEngineer)).all()}
        for r in rows:
            e = existing.get(r["license_no"])
            if e is None:
                session.add(BpelsgEngineer(
                    license_no=r["license_no"], name=r["name"], license_type=r["license_type"],
                    city=r["city"], county=r["county"], status=r["status"], expiry=r["expiry"],
                    file_date=fd,
                ))
                new_count += 1
            else:
                e.name = r["name"]
                e.city = r["city"]
                e.county = r["county"]
                e.status = r["status"]
                e.expiry = r["expiry"]
                e.file_date = fd
                session.add(e)
                updated_count += 1
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("BPELSG engineer load failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = new_count
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()

    return {"fetched": fetched, "new": new_count, "updated": updated_count, "error": error}


def mep_engineer_person_names_by_project(session: Session, project_ids: list[int]) -> dict[int, list[str]]:
    """{project_id: [person names]} for every raw named_people entry, across
    every signal linked to that project, whose title marks them as the
    mechanical/MEP engineer (see MEP_ENGINEER_TITLE_MARKERS). This is the
    PERSON side of "the mep_engineer extract role" -- distinct from
    ProjectFirm.role == 'mep_engineer', which is always a FIRM (see
    app/firms.py's ROLE_TO_TYPE) and is never joined against this roster
    at all, since BPELSG's file carries no firm data (see this module's
    docstring)."""
    if not project_ids:
        return {}
    rows = session.exec(
        select(ProjectSignal.project_id, Signal.named_people)
        .where(ProjectSignal.project_id.in_(project_ids), Signal.id == ProjectSignal.signal_id)
    ).all()
    out: dict[int, list[str]] = {}
    for project_id, named_people in rows:
        for person in (named_people or []):
            title = (person.get("title") or "").lower()
            name = person.get("name")
            if name and any(marker in title for marker in MEP_ENGINEER_TITLE_MARKERS):
                out.setdefault(project_id, []).append(name)
    return out


def bpelsg_roster_by_normalized_name(session: Session) -> dict[str, list[BpelsgEngineer]]:
    """The whole in-territory roster (~5,300 rows, small enough for plain
    Python -- same discipline as app.pipeline.ab869.ab869_board_rows),
    grouped by normalized person name. Fetch ONCE per caller (a board
    page, a batch of projects), never per project -- see
    match_bpelsg_for_project, which takes this as an argument rather than
    querying it itself."""
    by_name: dict[str, list[BpelsgEngineer]] = {}
    for e in session.exec(select(BpelsgEngineer)).all():
        by_name.setdefault(normalize_person_name(e.name), []).append(e)
    return by_name


def match_bpelsg_for_project(roster_by_name: dict[str, list[BpelsgEngineer]], project: Project,
                             person_names: list[str]) -> dict | None:
    """Matches the first of `person_names` (already extracted as the
    project's mep_engineer person) against `roster_by_name` (see
    bpelsg_roster_by_normalized_name) by normalized name. Abstains --
    returns None, never guesses -- unless exactly one licensee survives:

      - No candidate at all for that normalized name: no match.
      - Exactly one candidate: match, regardless of county (the roster is
        already narrowed to Scout's own 7 counties at load time, so any
        single survivor is already in-territory).
      - More than one candidate sharing that normalized name (e.g. two
        licensees named "John Smith" in different counties): narrowed by
        the PROJECT's own county if it names exactly one of them;
        otherwise abstains -- two same-named licensees and no way to
        tell which one is meant is exactly the case this must not guess.

    Returns {"license_no", "county", "status"} or None."""
    for name in person_names:
        normalized = normalize_person_name(name)
        if not normalized:
            continue
        candidates = roster_by_name.get(normalized, [])
        if not candidates:
            continue
        if len(candidates) == 1:
            e = candidates[0]
            return {"license_no": e.license_no, "county": e.county, "status": e.status}
        if project.county:
            narrowed = [c for c in candidates if c.county == project.county]
            if len(narrowed) == 1:
                e = narrowed[0]
                return {"license_no": e.license_no, "county": e.county, "status": e.status}
        # ambiguous -- try the next name rather than guessing among these candidates
    return None
