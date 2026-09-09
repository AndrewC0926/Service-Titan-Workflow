"""South Coast AQMD facility grain -- see app.models.ScaqmdFacility's own
docstring for the full shape/access discussion. Short version: FIND
(xappprod.aqmd.gov) and Public Document Search (onbase-pub.aqmd.gov) both
carry a robots.txt Disallow: / for User-agent: * -- PlanetBids-shaped,
blocked regardless of method, checked 2026-09-08, never queried, not even
once. This module instead loads South Coast AQMD's own "Facilities
Notified" Annual Emissions Reporting list -- a plain XLSX linked directly
from an un-blocked page (aqmd.gov/home/rules-compliance/compliance/
annual-emission-reporting), robots.txt clean on that path. Facility grain
only: no equipment, no permit number, no capacity -- that data lives only
behind the two blocked hosts above, and a Public Records Act request is
the only path to it (see RUNBOOK.md's deferred-sources list and this
source's own assumptions-register entry).

CARB's own Facility Search Tool (ww2.arb.ca.gov/facility-search-tool):
Phase A research called this unreachable (it renders as a JavaScript
single-page app with no visible form). Corrected 2026-09-08 -- the actual
search form lives in a plain HTML iframe,
www.arb.ca.gov/app/emsinv/iframe/facinfo/facinfo.php, on a host with a
clean robots.txt (Allow: /, 2s crawl-delay, confirmed directly). Driven
once with Playwright (District = "SC", i.e. South Coast AQMD only) to
trigger its own "Download this data as a Comma Separated Value text file"
export -- the resulting CSV is checked in as a static file under
docs/carb/, and load_carb_facilities reads it from disk. This is NOT a
live fetcher: same "hand-pulled, statically stored, re-run by hand when
someone re-pulls it" precedent as app/pipeline/ab869.py's own PDF corpus
(docs/hcai/ab869/raw/) -- there is no scheduled or on-demand browser
automation anywhere in `app/`, only this one-off pull.

CADENCE: both sources are annual, hand-run -- `scout fetch-scaqmd-
facilities` (AER XLSX, live network fetch) and `scout load-carb-
facilities` (CARB CSV, reads the static file already on disk) are both
outside `scout pipeline`, same as ab802/opsc. Neither source carries a
year dimension of its own (the AER file is a live notification list; the
CARB export is a live district snapshot), so each is a full replace of
its OWN rows on every run -- WHERE source = 'aer_facilities_notified' or
WHERE source = 'carb' respectively, never the whole table, since the two
sources' own reload cadences are independent and neither run should ever
clobber the other's rows.

JOIN: to Ab802Building, normalized-address text (app.pipeline.retrofit.
normalize_address) -- neither side has lat/long here, so there is no
lat/long-first step like AB 802's own RetrofitBuilding join. Recomputed
across ALL years of Ab802Building on file (an address doesn't change year
to year), stored denormalized on Ab802Building.air_permit_facility_id.
Ambiguous on either side (two ScaqmdFacility rows sharing one normalized
address, or the reverse) is dropped, never guessed at -- same discipline
every other join in this codebase already holds itself to.

To Ab869/HospitalBuilding facilities: NOT an address join -- HospitalBuilding
and Ab869Plan carry no street address field at all, only city/county and
lat/long (confirmed by inspection, not assumed). The best available loose
join is normalized FACILITY NAME (app.normalize.normalize_name), restricted
to the same city to keep an unrelated same-named facility in a different
city from colliding. This is a real deviation from an address join and is
disclosed everywhere it surfaces (model docstring, this module, the board
UI, the build report) rather than silently presented as address-based.
Computed live in app.pipeline.ab869.ab869_board_rows (cheap, ~200
facilities), never stored -- there is no good column to denormalize it
onto: Ab869Plan doesn't exist for the ~11 facilities with NO_PLAN_ON_FILE,
and HospitalBuilding is building-, not facility-, grain.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

from sqlmodel import Session, delete, func, select

from app.config import Config
from app.http import PoliteClient
from app.models import Ab802Building, ScaqmdFacility, SourceRun, utcnow
from app.normalize import normalize_name
from app.pipeline.retrofit import normalize_address

log = logging.getLogger(__name__)

SOURCE = "scaqmd_facility"
SOURCE_AER = "aer_facilities_notified"
SOURCE_CARB = "carb"

# The sfvrsn suffix is Sitefinity's own cache-busting version stamp on the
# published file -- it changes whenever South Coast AQMD republishes the
# XLSX (e.g. a new year's list). Verified live 2026-09-08; if a future run
# 404s, the current URL should be re-found from the same landing page
# rather than guessed at (see this module's docstring for the page).
AER_XLSX_URL = (
    "https://www.aqmd.gov/docs/default-source/planning/annual-emission-reporting/"
    "2025-aer-facilities-notified-list.xlsx?sfvrsn=c7466b7e_2"
)
PORTAL_URL = "https://www.aqmd.gov/home/rules-compliance/compliance/annual-emission-reporting"

_EXPECTED_HEADER_PREFIX = "facility id"


def _bool(val) -> bool:
    """The source file marks a flag column with a checkmark glyph (or
    similar) when true and leaves the cell blank when false -- any
    non-blank value is True, never inferred further."""
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.lower() not in ("no", "n", "false", "0")


def parse_facility_rows(raw_bytes: bytes) -> list[dict]:
    """Parses the AER Facilities Notified XLSX. The workbook carries a
    "Notes" sheet (disclaimer text, not data) plus one data sheet whose
    name is year-specific ("2025 AER", "2026 AER", ...) -- picked by
    finding the sheet whose first cell reads "Facility ID", not by a
    hardcoded sheet name, so next year's file (a new sheet name) still
    parses without a code change."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    ws = None
    for name in wb.sheetnames:
        candidate = wb[name]
        first_cell = candidate.cell(row=1, column=1).value
        if first_cell and str(first_cell).strip().lower().startswith(_EXPECTED_HEADER_PREFIX):
            ws = candidate
            break
    if ws is None:
        raise ValueError(f"no sheet in the AER workbook starts with a '{_EXPECTED_HEADER_PREFIX}' header "
                         f"(sheets present: {wb.sheetnames})")

    rows_iter = ws.iter_rows(values_only=True)
    next(rows_iter)  # header row, positions are fixed by the source, not re-read by name
    out = []
    for row in rows_iter:
        facility_id = row[0]
        if facility_id is None:
            continue
        out.append({
            "facility_id": str(facility_id).strip(),
            "facility_name": (str(row[1]).strip() if row[1] else None),
            "address": (str(row[2]).strip() if row[2] else None),
            "city": (str(row[3]).strip() if row[3] else None),
            "zip_code": (str(row[4]).strip() if row[4] else None),
            "ab_2588": _bool(row[5]),
            "meets_ctr_threshold": _bool(row[6]),
            "core_ctr_facility": _bool(row[7]),
            "ctr_phase_3": _bool(row[8]),
            "rule_317_1": _bool(row[9] if len(row) > 9 else None),
        })
    return out


CARB_CSV_PATH = "docs/carb/2024-south-coast-aqmd-facilities.csv"
CARB_SOURCE_URL = (
    "https://www.arb.ca.gov/app/emsinv/iframe/facinfo/faccrit_output.csv"
    "?&dbyr=2024&ab_=&dis_=SC&co_=&fname_=&city_=&sort=FacilityNameA&fzip_=&fsic_=&facid_=&all_fac=C"
    "&chapis_only=&CERR=&dd="
)


def parse_carb_csv(raw_bytes: bytes) -> list[dict]:
    """Parses CARB's own Facility Search Tool export (CEIDARS-backed,
    triggered with District='SC' -- South Coast AQMD only, matching the
    AER population's own scope). Header row is CARB's own field names
    verbatim: FACID, FNAME, FSTREET, FCITY, FZIP, plus per-pollutant
    emissions tonnage columns this table does not store (facility grain
    only, see this module's docstring for why). Name-based column lookup,
    not positional -- unlike the AER XLSX, this CSV's header is plain
    ASCII and stable."""
    import csv
    import io as _io

    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(_io.StringIO(text))
    out = []
    for row in reader:
        facid = (row.get("FACID") or "").strip()
        if not facid:
            continue
        out.append({
            "facility_id": facid,
            "facility_name": (row.get("FNAME") or "").strip() or None,
            "address": (row.get("FSTREET") or "").strip() or None,
            "city": (row.get("FCITY") or "").strip() or None,
            "zip_code": (row.get("FZIP") or "").strip() or None,
        })
    return out


def load_carb_facilities(session: Session, csv_path: str = CARB_CSV_PATH,
                         source_url: str = CARB_SOURCE_URL) -> dict:
    """Reads the static CARB export already on disk (see this module's
    docstring for how it got there -- a one-off Playwright pull, not a
    live fetch) and full-replaces every source='carb' row. Recomputes the
    AB 802 join afterward the same as fetch_scaqmd_facilities, since CARB
    rows are just as eligible to match as AER rows. Returns fetched/stored/
    new/already_present (against the AER-sourced facility_id set already
    on file) plus duplicates_dropped (CARB's own export carries a handful
    of genuine duplicate FACID rows -- last one in file order wins, same
    discipline as app.pipeline.ab802's own duplicate-id handling),
    ab802_flagged, error."""
    run = SourceRun(source=f"{SOURCE}_carb")
    session.add(run)
    session.commit()

    fetched, stored, new_count, already_present, duplicates_dropped, ab802_flagged, error = \
        0, 0, 0, 0, 0, 0, None
    try:
        with open(csv_path, "rb") as fh:
            raw = fh.read()
        rows = parse_carb_csv(raw)
        fetched = len(rows)

        # CARB's own export carries a handful of genuine duplicate FACID
        # rows (2026-09-08: 15 of 5,569, same name/address/city, different
        # county code -- a data quirk in CARB's own system, not a parsing
        # bug). Same discipline as app.pipeline.ab802's own duplicate-
        # property-id handling: keep the LAST row per id in file order,
        # report the dropped count rather than silently discarding or
        # crashing on the unique constraint.
        deduped: dict[str, dict] = {}
        for r in rows:
            deduped[r["facility_id"]] = r
        duplicates_dropped = fetched - len(deduped)

        aer_facility_ids = set(session.exec(
            select(ScaqmdFacility.facility_id).where(ScaqmdFacility.source == SOURCE_AER)).all())

        session.exec(delete(ScaqmdFacility).where(ScaqmdFacility.source == SOURCE_CARB))
        now = utcnow()
        for r in deduped.values():
            session.add(ScaqmdFacility(
                facility_id=r["facility_id"], source=SOURCE_CARB, facility_name=r["facility_name"],
                address=r["address"], city=r["city"], zip_code=r["zip_code"],
                in_territory=True, source_url=source_url, imported_at=now,
            ))
            if r["facility_id"] in aer_facility_ids:
                already_present += 1
            else:
                new_count += 1
        session.flush()
        stored = len(deduped)

        ab802_flagged = _link_ab802(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("SCAQMD CARB facility load failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = new_count
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()

    return {"fetched": fetched, "stored": stored, "new": new_count,
           "already_present": already_present, "duplicates_dropped": duplicates_dropped,
           "ab802_flagged": ab802_flagged, "error": error}


def _link_ab802(session: Session) -> int:
    """See module docstring's JOIN section. Recomputes every Ab802Building
    row's air_permit_facility_id fresh against the just-loaded
    scaqmd_facilities table (a stale match from a facility no longer on
    file must clear, not linger). Returns the total count of Ab802Building
    rows now flagged, across every year on file.

    Grouped by facility_id (a SET, not a list) per normalized address --
    the same real facility legitimately appears twice now, once per
    source (AER and CARB), often at the identical address, and that must
    collapse to one candidate, not read as "two facilities sharing an
    address" and dropped as ambiguous. Only a normalized address actually
    claimed by two DIFFERENT facility_ids is real ambiguity."""
    facilities = session.exec(select(ScaqmdFacility)).all()
    by_addr: dict[str, set[str]] = {}
    for f in facilities:
        na = normalize_address(f.address)
        if na:
            by_addr.setdefault(na, set()).add(f.facility_id)
    addr_to_facility = {addr: next(iter(ids)) for addr, ids in by_addr.items() if len(ids) == 1}

    for b in session.exec(select(Ab802Building)).all():
        na = normalize_address(b.address_1)
        match = addr_to_facility.get(na) if na else None
        b.air_permit_facility_id = match
        b.air_permit_match_method = "normalized_address" if match else None
        session.add(b)
    session.commit()

    return session.exec(
        select(func.count()).where(Ab802Building.air_permit_facility_id.is_not(None))
    ).one()


def _collapse_same_facility(candidates: list[ScaqmdFacility]) -> str | None:
    """Given every ScaqmdFacility row sharing one (normalized name, city)
    key, collapses candidates that are the SAME real facility -- sharing a
    facility_id (trivial, they're already the same key), or sharing a
    normalized street address across different facility_ids (the two
    sources don't always agree on facility_id for the same real place) --
    into one survivor, via union-find. Returns that survivor's facility_id
    if exactly one real facility remains after collapsing, else None (a
    genuine, different-facility ambiguity -- abstain, don't guess).

    This exists because the SAME real facility legitimately has a row
    from BOTH sources (AER and CARB) now -- two rows, one real facility --
    and the pre-fix version (2026-09-08) read that as "two candidates,
    ambiguous" and dropped several real AB 869 matches that had been found
    correctly before CARB was added."""
    parent: dict[str, str] = {c.facility_id: c.facility_id for c in candidates}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    addr_index: dict[str, str] = {}
    for c in candidates:
        na = normalize_address(c.address) if c.address else None
        if na is None:
            continue
        if na in addr_index:
            union(c.facility_id, addr_index[na])
        else:
            addr_index[na] = c.facility_id

    survivors = {find(c.facility_id) for c in candidates}
    return next(iter(survivors)) if len(survivors) == 1 else None


def scaqmd_matches_for_ab869(session: Session, facility_rows: list[tuple[str, str]]) -> dict[str, str]:
    """facility_rows: (perm_id, facility_name, city) triples the caller
    already has in hand (app.pipeline.ab869.ab869_board_rows' own
    HospitalBuilding query) -- see module docstring for why this is a
    NAME match, not an address match, and why it is computed live here
    rather than stored. Returns perm_id -> ScaqmdFacility.facility_id for
    unambiguous matches only -- see _collapse_same_facility for how a
    same-facility, two-source pair is told apart from a real ambiguity."""
    scaqmd_rows = session.exec(select(ScaqmdFacility)).all()
    by_name_city: dict[tuple[str, str], list[ScaqmdFacility]] = {}
    for f in scaqmd_rows:
        nn = normalize_name(f.facility_name) if f.facility_name else None
        nc = (f.city or "").strip().lower()
        if nn and nc:
            by_name_city.setdefault((nn, nc), []).append(f)
    name_city_to_facility = {
        k: survivor for k, candidates in by_name_city.items()
        if (survivor := _collapse_same_facility(candidates)) is not None
    }

    out = {}
    for perm_id, facility_name, city in facility_rows:
        nn = normalize_name(facility_name) if facility_name else None
        nc = (city or "").strip().lower()
        if not nn or not nc:
            continue
        match = name_city_to_facility.get((nn, nc))
        if match:
            out[perm_id] = match
    return out


def fetch_scaqmd_facilities(session: Session, cfg: Config, client: PoliteClient) -> dict:
    """Full-replaces scaqmd_facilities from the live AER XLSX, then
    recomputes the AB 802 address join. Own SourceRun for `scout doctor`/
    source_health visibility, same pattern as fetch_ab802_benchmarks and
    fetch_opsc_projects."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    fetched, stored, ab802_flagged, error = 0, 0, 0, None
    try:
        raw = client.get_bytes(AER_XLSX_URL)
        rows = parse_facility_rows(raw)
        fetched = len(rows)

        session.exec(delete(ScaqmdFacility).where(ScaqmdFacility.source == SOURCE_AER))
        now = utcnow()
        for r in rows:
            session.add(ScaqmdFacility(
                facility_id=r["facility_id"], source=SOURCE_AER, facility_name=r["facility_name"],
                address=r["address"], city=r["city"], zip_code=r["zip_code"],
                ab_2588=r["ab_2588"], meets_ctr_threshold=r["meets_ctr_threshold"],
                core_ctr_facility=r["core_ctr_facility"], ctr_phase_3=r["ctr_phase_3"],
                rule_317_1=r["rule_317_1"], in_territory=True,
                source_url=AER_XLSX_URL, imported_at=now,
            ))
        session.flush()
        stored = fetched

        ab802_flagged = _link_ab802(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("SCAQMD facility fetch failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = stored
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()

    return {"fetched": fetched, "stored": stored, "in_territory": stored,
           "ab802_flagged": ab802_flagged, "error": error}
