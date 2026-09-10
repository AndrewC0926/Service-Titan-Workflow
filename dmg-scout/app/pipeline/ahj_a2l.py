"""AHJ A2L register -- Phase B build on the Phase A research recorded in
app/assumptions.py's "AHJ A2L register" group (commits 884e2d0, d1cdd6a).
No fetcher here: this module has no HTTP client, no crawl-delay, no
robots.txt check of its own. Every row it writes is a verbatim transcription
of a document (or a documented absence/block/gap) this app already read by
hand in Phase A -- see that assumptions-register group for every citation.
If a future session wants to add a jurisdiction or refresh one that already
has a row, that is new Phase A research first, a code change to this file
second, never the other way around.

load_ahj_a2l_guidance() is idempotent on AhjA2lGuidance.jurisdiction (the
primary key): re-running it updates existing rows in place from the same
constants below rather than duplicating them, same discipline every other
`seed_*`/`load_*` function in this codebase already uses.

Jurisdiction universe (212 rows): the 7 territory counties + 201 cities
from app.geo._CITY_TO_COUNTY (208), HCAI/OSHPD + DSA + State Fire Marshal
(3, state agencies with mechanical-plan-check authority over their own
building types), and one separate "State of California" row carrying the
State Fire Marshal's own rulemaking status (not a local AHJ's guidance) --
208 + 3 + 1 = 212, matching Phase A's own count.

Coverage as of this Phase A pass (2026-09-10): 5 HIT, 3 BLOCKED, 156
NONE_FOUND, 47 NOT_REACHED among the 211 AHJ rows, plus the 1 State of
California row (HIT, in the sense that real material was found, though it
describes pending rulemaking rather than settled local guidance). The 47
NOT_REACHED rows are not a coverage judgment -- see STATUS docstring on
AhjA2lGuidance for why they are marked differently from NONE_FOUND."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.geo import _CITY_TO_COUNTY
from app.models import AhjA2lGuidance, utcnow

# ---- status vocabulary ------------------------------------------------------
# Deliberately a plain tuple, not a models.py-level Python enum bound to the
# column type -- see AhjA2lGuidance's own docstring for why the column itself
# is a plain string. This tuple is what tests check row values against.
AHJ_A2L_STATUSES: tuple[str, ...] = ("HIT", "NONE_FOUND", "BLOCKED", "NOT_REACHED")

_RESEARCH_DATE = datetime(2026, 9, 10)

# ---- title-case exceptions ---------------------------------------------------
# str.title() gets every in-territory city name right except this one
# (capitalizes the preposition: "City Of Industry"). Hand-fixed rather than
# adding a general title-casing library for a single exception.
_DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "city of industry": "City of Industry",
}

_TERRITORY_COUNTIES = (
    "Los Angeles", "Orange", "San Bernardino", "Riverside", "San Diego", "Imperial", "Kern",
)

_STATE_AGENCIES = ("HCAI/OSHPD", "DSA", "State Fire Marshal")

# ---- BLOCKED: robots.txt confirmed disallowing this app, prior CIP research
# (2026-09-08/09) -- not re-checked this pass, per that research's own
# disclosure. Never fetched; per CHARTER.md invariant 10, never worked around.
_BLOCKED = {"Riverside County", "Riverside", "Kern County"}

# ---- NOT_REACHED: never searched this pass, for a reason that has nothing to
# do with the jurisdiction -- this session's WebSearch tool quota (200 of 200
# calls, shared across the whole session and every parallel research
# subagent) ran out mid-batch on the first list, and the second batch's
# subagent got stuck in a broken internal loop and was terminated before the
# quota ran out either way. Both facts are recorded in `notes` per row, not
# just here.
_NOT_REACHED = {
    "Diamond Bar", "Redondo Beach", "Rosemead", "San Gabriel", "El Segundo",
    "Signal Hill", "Vernon", "Commerce",
    "City of Industry", "Santa Fe Springs", "Cerritos", "Azusa", "Covina",
    "West Hollywood", "Manhattan Beach", "Hermosa Beach", "Glendora", "La Puente",
    "La Mirada", "Bell", "Bell Gardens", "Temple City", "Walnut", "Duarte",
    "Monrovia", "Claremont", "La Verne", "San Dimas", "Hawaiian Gardens",
    "Lawndale", "Lomita", "Malibu", "Calabasas", "Agoura Hills",
    "Westlake Village", "Rolling Hills Estates", "Palos Verdes Estates",
    "Rancho Palos Verdes", "South Gate", "Maywood", "Cudahy", "South El Monte",
    "Irwindale", "Bradbury", "Sierra Madre", "San Marino", "Artesia",
}
assert len(_NOT_REACHED) == 47, len(_NOT_REACHED)

_NOT_REACHED_NOTE = (
    "NOT_REACHED: never searched this pass. This session's WebSearch tool quota "
    "(200 of 200 calls, shared across the whole session and every parallel research "
    "subagent it ran) was exhausted before this jurisdiction's turn came up. Needs "
    "either a fresh quota or a different search method in a later session -- see "
    "app/assumptions.py's \"AHJ A2L register\" / \"Jurisdiction coverage as of this "
    "pass\" entry for the full account, including which of the two remaining LA "
    "County research batches this jurisdiction was in."
)

# ---- HIT rows: every field below is a verbatim transcription of Phase A's
# own findings (app/assumptions.py, "AHJ A2L register" group) -- see that
# group's source_detail for the page citations. NULL means the source
# document does not state that field; never filled in from another row's
# pattern.
_HITS: dict[str, dict] = {
    "Los Angeles": {
        "jurisdiction_type": "city",
        "county": "Los Angeles",
        "ashrae_15_edition": "2022",
        "ashrae_15_2_edition": "2022",
        "ashrae_34_edition": "2022",
        "addendum_a_shaft_alt": "NO",
        "addenda_accepted": (
            "ASHRAE 34(2022) Addendum A -- updated LFL values; plans already submitted with the "
            "old values also acceptable. ASHRAE 15(2022) Addenda E, F, H, L -- all enforced. "
            "ASHRAE 15.2(2022) Addenda A, C, F/Errata -- accepted, EACH one explicitly carrying "
            "its own stated rule: 'No mixing between the two versions -- the equation and values "
            "must be consistent.' (The ASHRAE 34 and ASHRAE 15 series addenda accept old-or-new "
            "values but do not repeat that exact no-mixing sentence.)"
        ),
        "edvc_regardless_of_charge": "YES",
        "a1_resubmittal_rule": (
            "Approved plans using A1 refrigerant systems shall be re-submitted and re-approved by "
            "Building and Safety, Mechanical Plan Check, to accommodate another refrigerant type if "
            "the A1 refrigerant is not available at the time of construction; responsibility of the "
            "owner to ensure full compliance."
        ),
        "express_permit_note": (
            "If the installation meets the express permit qualifications, an express permit can be "
            "obtained. Plan check review may still be triggered due to other components of the plan."
        ),
        "doc_title": "Guideline: HVAC Systems Using A2L Refrigerants",
        "doc_number": "P/MC 2026-007",
        "doc_date": datetime(2026, 1, 1),  # document's own "Revised" date
        "source_url": "https://dbs.lacity.gov/sites/default/files/efs/forms/pc17/"
                      "P-MC-2026-007-Guideline-HVAC-Systems.pdf",
        "notes": (
            "Seed row for this register, read in full (25 pages). Reference No. CMC 1104.6, "
            "Effective 07-01-2024. dbs.lacity.gov/robots.txt returns HTTP 403 regardless of "
            "user-agent while the PDF itself and the site's homepage both return 200 -- a "
            "path-specific anomaly on /robots.txt only, disclosed, not treated as a block."
        ),
    },
    "Los Angeles County": {
        "jurisdiction_type": "county",
        "county": "Los Angeles",
        "ashrae_15_edition": None,
        "ashrae_15_2_edition": None,  # referenced without a year ("ASHRAE 15.2 Table 9-1")
        "ashrae_34_edition": None,
        "addendum_a_shaft_alt": None,
        "addenda_accepted": None,
        "edvc_regardless_of_charge": None,
        "a1_resubmittal_rule": None,
        "express_permit_note": None,
        "doc_title": "Residential A2L Refrigerant Plan Check Determination Checklist",
        "doc_number": None,
        "doc_date": None,  # not stated in the document
        "source_url": "https://pw.lacounty.gov/bsd/content/publications.aspx",
        "notes": (
            "Residential-scope-only checklist, not a technical guideline -- all 8 columns NULL "
            "except that ASHRAE 15.2 is referenced (no year given). source_url is the publications "
            "index page the document was found on (not indexed under any obvious link text, located "
            "by searching the page's own rendered text), not a direct PDF permalink -- none was "
            "recorded. Disclosed oddity: the published PDF's pages 3-6 are unrelated Carrier HVAC "
            "manufacturer product-data sheets (watermarked 'EXAMPLE') appended after the real "
            "2-page form; only pages 1-2 were treated as the actual document."
        ),
    },
    "Escondido": {
        "jurisdiction_type": "city",
        "county": "San Diego",
        "ashrae_15_edition": "2024",
        "ashrae_15_2_edition": "2024",  # printed "ASHRAE 15.2-2-24" [sic] in the source
        "ashrae_34_edition": None,
        "addendum_a_shaft_alt": None,
        "addenda_accepted": None,
        "edvc_regardless_of_charge": None,
        "a1_resubmittal_rule": None,
        "express_permit_note": None,
        "doc_title": "A2L HVAC System Affidavit / Installation Checklist",
        "doc_number": None,
        "doc_date": None,  # no date printed anywhere in the document
        "source_url": "https://www.escondido.gov/DocumentCenter/View/8485/A2L-HVAC-System-Affidavit-PDF",
        "notes": (
            "Residential/IRC-2024-framed installer checklist-affidavit, not LADBS's commercial CMC "
            "Chapter 11 material -- narrower scope, flagged not treated as equivalent. The only AHJ "
            "found in this pass requiring the 2024 edition rather than 2022. Refrigerant charge "
            "threshold stated as 34.5 lbs per independent system, a different figure and basis (IRC, "
            "not ASHRAE 15.2 Table 9-1) than Los Angeles County's own residential form."
        ),
    },
    "HCAI/OSHPD": {
        "jurisdiction_type": "state_agency",
        "county": None,
        "ashrae_15_edition": None,
        "ashrae_15_2_edition": None,
        "ashrae_34_edition": None,
        "addendum_a_shaft_alt": None,
        "addenda_accepted": None,
        "edvc_regardless_of_charge": None,
        "a1_resubmittal_rule": None,
        "express_permit_note": None,
        "doc_title": "2025 Title 24 Triennial Code Cycle Update, Part 4 & Part 5, Q and A",
        "doc_number": None,
        "doc_date": datetime(2025, 10, 23),
        "source_url": "https://hcai.ca.gov/wp-content/uploads/2025/11/QA-HCAI_OSHPD_-2025-Title-24-"
                      "Triennial-Code-Cycle-Update-–-Part-4-and-Part-5-Final-A-1.pdf",
        "notes": (
            "A webinar Q&A transcript, not a dedicated A2L guideline -- one direct question on point "
            "(Q9: 'Allowance for refrigerant type systems (mini-split) for IDF rooms? Storage rooms?'"
            "). Answer, verbatim: 'Refrigerant types are as permitted in accordance with Chapter 11 "
            "of the CMC and ASHRAE 15. With the prohibition of R410a, new A1 and A2L refrigerants are "
            "being employed.' Confirms A2L is permitted; states no edition year and no EDVC/addenda/"
            "shaft position -- thin hit, kept because it is a real, on-point statement from the AHJ, "
            "not a false positive."
        ),
    },
    "State Fire Marshal": {
        "jurisdiction_type": "state_agency",
        "county": None,
        "ashrae_15_edition": None,
        "ashrae_15_2_edition": None,
        "ashrae_34_edition": None,
        "addendum_a_shaft_alt": "INFORMATIONAL",
        "addenda_accepted": None,
        "edvc_regardless_of_charge": None,
        "a1_resubmittal_rule": None,
        "express_permit_note": None,
        "doc_title": "Information Bulletin 26-003: ASHRAE Standard 15-2024 Addendum A -- "
                     "Exempted Spaces and Shaft Alternatives",
        "doc_number": "IB 26-003",
        "doc_date": datetime(2026, 2, 10),
        "source_url": "https://34c031f8-c9fd-4018-8c5a-4159cdff6b0d-cdn-endpoint.azureedge.net/-/media/"
                      "osfm-website/resources/information-bulletins/2026/"
                      "ib-26-003-ashrae-standard-15-2024.pdf",
        "notes": (
            "This is the SFM speaking TO local jurisdictions, not a jurisdiction's own guidance: it "
            "tells them the 2024 Addendum A shaft alternative exists and that CMC Section 1.2.3 "
            "already lets a local building/fire official approve it as an alternate method of "
            "construction case by case, independent of whether the SFM's own base-code adoption "
            "(see the separate 'State of California' row) ever completes. Direct tension with "
            "Los Angeles (City)'s own seed-row position: an explicit NO on this same shaft "
            "alternative. addendum_a_shaft_alt is 'INFORMATIONAL' here, not YES/NO -- the bulletin "
            "informs and permits local discretion, it does not itself mandate or ban the alternative "
            "statewide."
        ),
    },
}

# ---- the one non-AHJ row: state rulemaking status, not local guidance -------
_STATE_OF_CALIFORNIA_ROW = {
    "jurisdiction": "State of California",
    "jurisdiction_type": "state_agency",
    "county": None,
    "status": "HIT",
    "ashrae_15_edition": None,
    "ashrae_15_2_edition": None,
    "ashrae_34_edition": None,
    "addendum_a_shaft_alt": None,
    "addenda_accepted": None,
    "edvc_regardless_of_charge": None,
    "a1_resubmittal_rule": None,
    "express_permit_note": None,
    "doc_title": "Final Statement of Reasons -- SFM 02/25, 2025 California Mechanical Code, "
                 "Title 24 Part 4 (2025 Intervening Code Cycle)",
    "doc_number": "SFM 02/25",
    "doc_date": datetime(2026, 6, 5),
    "source_url": "https://www.dgs.ca.gov/-/media/Divisions/BSC/03-Rulemaking/2025-Intervening-Cycle/"
                  "CAC/2026-02-11-GREEN-PEME/SFM-02-25_ISOR_PT4.pdf",
    "checked_at": _RESEARCH_DATE,
    "notes": (
        "SFM's petition to adopt ASHRAE 15-2024 (including Addendum A, the shaft alternative) "
        "cleared CAC review (recommended Approve, 2026-02-11) and a 45-day public comment period "
        "(2026-03-27 to 2026-05-11; one comment, an 8-association industry coalition in support, no "
        "opposition). Final Statement of Reasons dated 2026-06-05 affirms the update, finding it "
        "qualifies as a 'technical update' under AB 130's intervening-cycle exception (H&S "
        "§18942(a)(2)(A)). Scheduled as Agenda Item 20 of the August 25-27, 2026 Commission "
        "meeting. VOTE OUTCOME UNCONFIRMED AS OF 2026-09-10 -- no document or page fetched in Phase A "
        "confirmed whether that meeting adopted, disapproved, or continued this item; not searched "
        "for in this Phase B pass, per instruction. If approved, the cycle's own general effective "
        "date is stated as 2027-07-01 (a cycle-wide date, not confirmed specific to this item). "
        "Separate, unresolved legal question also on record: AB 209 (Health & Safety Code "
        "§18944.21) may already require statewide acceptance of the 'most recent version' of "
        "ASHRAE 15 by operation of law, independent of this rulemaking's outcome -- flagged, not "
        "resolved."
    ),
}


def _display_name(raw: str) -> str:
    return _DISPLAY_NAME_OVERRIDES.get(raw, raw.title())


def _build_rows() -> list[dict]:
    rows: dict[str, dict] = {}

    for county in _TERRITORY_COUNTIES:
        jurisdiction = f"{county} County"
        rows[jurisdiction] = {
            "jurisdiction": jurisdiction, "jurisdiction_type": "county", "county": county,
        }

    for raw_city, county in _CITY_TO_COUNTY.items():
        jurisdiction = _display_name(raw_city)
        rows[jurisdiction] = {
            "jurisdiction": jurisdiction, "jurisdiction_type": "city", "county": county,
        }

    for agency in _STATE_AGENCIES:
        rows[agency] = {"jurisdiction": agency, "jurisdiction_type": "state_agency", "county": None}

    assert len(rows) == 211, len(rows)

    for jurisdiction, row in rows.items():
        if jurisdiction in _HITS:
            hit = _HITS[jurisdiction]
            row.update(hit)
            row["status"] = "HIT"
            row["checked_at"] = _RESEARCH_DATE
        elif jurisdiction in _BLOCKED:
            row["status"] = "BLOCKED"
            row["checked_at"] = _RESEARCH_DATE
            row["notes"] = (
                "BLOCKED: robots.txt confirmed disallowing this app before any document fetch was "
                "attempted -- see app/assumptions.py's CIP jurisdiction-list research (2026-09-08/09) "
                "for the verbatim disallow rule. Never fetched, never re-checked this pass."
            )
        elif jurisdiction in _NOT_REACHED:
            row["status"] = "NOT_REACHED"
            row["checked_at"] = None
            row["notes"] = _NOT_REACHED_NOTE
        else:
            row["status"] = "NONE_FOUND"
            row["checked_at"] = _RESEARCH_DATE
            row["notes"] = (
                "One targeted search run 2026-09-10; no plausible on-point A2L/R-32/R-454B/"
                "ASHRAE-15-or-15.2/mechanical-plan-check document found."
            )

    out = list(rows.values())
    out.append(dict(_STATE_OF_CALIFORNIA_ROW))
    assert len(out) == 212, len(out)
    return out


def sorted_rows(session: Session) -> list[AhjA2lGuidance]:
    """HIT rows first, then by county (state-agency rows, county=None, sort
    last within their bucket), then by jurisdiction name -- the /reference
    tab's own default sort, factored out here so a test can check it
    without going through the web layer."""
    rows = session.exec(select(AhjA2lGuidance)).all()
    return sorted(rows, key=lambda r: (0 if r.status == "HIT" else 1, r.county or "￿", r.jurisdiction))


def status_counts(rows: list[AhjA2lGuidance]) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in AHJ_A2L_STATUSES}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def checked_at_range(rows: list[AhjA2lGuidance]) -> tuple[datetime | None, datetime | None]:
    """Min/max checked_at among rows that HAVE one -- NOT_REACHED rows
    (checked_at NULL) are excluded from the range itself, which is exactly
    what makes them visible as unfinished rather than folded into "as of
    <date>" as if they'd been checked too."""
    dates = [r.checked_at for r in rows if r.checked_at is not None]
    if not dates:
        return None, None
    return min(dates), max(dates)


def hit_row_for_project(session: Session, project) -> AhjA2lGuidance | None:
    """Badge target for a Project row. DISCLOSED DEGRADATION: the task asks
    for a badge keyed on the project's CITY, but app.models.Project has no
    city field at all -- only county, state, lat/long, and apn_parcel (only
    checked directly, not assumed). This can therefore only ever match a
    COUNTY-type HIT row (today, only 'Los Angeles County'); a city-type HIT
    ('Los Angeles', 'Escondido') can never badge a Project, because there is
    no project-level city value to compare it against. Not worked around by
    inventing a city from lat/long (reverse geocoding) or from apn_parcel --
    that would be exactly the kind of invented field CHARTER.md invariant 12
    forbids. See app/assumptions.py's "AHJ A2L register" group for this same
    disclosure."""
    if not project.county:
        return None
    return session.exec(
        select(AhjA2lGuidance).where(
            AhjA2lGuidance.status == "HIT",
            AhjA2lGuidance.jurisdiction_type == "county",
            AhjA2lGuidance.county == project.county,
        )
    ).first()


def hit_row_for_city(session: Session, city: str | None, county: str | None = None) -> AhjA2lGuidance | None:
    """Badge target for anything that HAS a real city value -- exact,
    case-insensitive match against city-type HIT jurisdictions, optionally
    gated by county (belt-and-suspenders against an unrelated same-named
    place in a different county; none exists in-territory today, but the
    gate costs nothing). HcaiProject itself carries no city column, but
    app.pipeline.ab869.ab869_board_rows already resolves one per facility
    via HospitalBuilding.city (the same join open_hcai_projects_by_facility_id
    uses) -- pass that value in here rather than parsing free-text
    facility_address, which this function deliberately does not touch."""
    if not city:
        return None
    q = select(AhjA2lGuidance).where(
        AhjA2lGuidance.status == "HIT", AhjA2lGuidance.jurisdiction_type == "city")
    if county:
        q = q.where(AhjA2lGuidance.county == county)
    city_norm = city.strip().lower()
    for row in session.exec(q).all():
        if row.jurisdiction.lower() == city_norm:
            return row
    return None


def load_ahj_a2l_guidance(session: Session) -> dict[str, int]:
    """Idempotent load: upserts all 212 rows by jurisdiction (the primary
    key). Re-running against the same constants above updates existing rows
    in place -- never duplicates, never drops a row not present in this
    build's own fixed universe. Returns counts by status for the caller
    (CLI command) to report."""
    rows = _build_rows()
    existing = {r.jurisdiction: r for r in session.exec(select(AhjA2lGuidance)).all()}

    counts: dict[str, int] = {s: 0 for s in AHJ_A2L_STATUSES}
    for data in rows:
        counts[data["status"]] = counts.get(data["status"], 0) + 1
        row = existing.get(data["jurisdiction"])
        if row is None:
            row = AhjA2lGuidance(jurisdiction=data["jurisdiction"])
            session.add(row)
        for field, value in data.items():
            setattr(row, field, value)

    return counts
