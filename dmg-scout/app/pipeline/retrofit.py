"""Retrofit building pipeline — collapses EquipmentPermit rows (one building
often carries several permits over the years) to one row per building,
joins LA County assessor parcel characteristics, evaluates which verified
regulatory triggers fire, and ranks by the same replacement-service-life
discipline as the rest of the system.

Deliberately NO owner name / mailing address: verified live (2026-08-08)
that no free, bulk-queryable source of that data exists for LA County — see
RetrofitBuilding's docstring in app/models.py. The building's situs address
and APN are the entity key; a rep still has to look the phone number up,
same as every "pending" enrichment contact elsewhere in this system.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlmodel import delete, select

from app.config import Config
from app.http import PoliteClient
from app.models import AssessorCandidate, EquipmentPermit, RetrofitBuilding, utcnow
from app.pipeline.assessor import FEATURE_SERVER, PORTAL_URL as ASSESSOR_PORTAL_URL
from app.pipeline.permits import PORTAL_URL as PERMITS_PORTAL_URL
from app.pipeline.regulatory import infer_refrigerant, sb1206_status
from app.replacement import UnknownEquipment, service_life

log = logging.getLogger(__name__)

# Keyword -> replacement.py equipment key. Checked in order; the first match
# wins, and a work description matching none of these leaves equipment_type
# null rather than defaulting to the most common type — service life must
# never be quoted for equipment the text didn't actually name. See
# app/replacement.py: "Never substitute another equipment type's life."
_EQUIPMENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brtus?\b|\brooftops?\b|\bpackage(d)?\s+units?\b", re.I), "packaged_rooftop"),
    (re.compile(r"\bmini.?splits?\b|\bsplit\s+systems?\b|\bsplit\s+units?\b|\bheat\s+pumps?\b", re.I), "split_dx"),
    (re.compile(r"\bwater.?cooled\s+chillers?\b", re.I), "water_cooled_chiller"),
    (re.compile(r"\bchillers?\b", re.I), "air_cooled_chiller"),
    (re.compile(r"\bboilers?\b", re.I), "boiler"),
    (re.compile(r"\bcooling\s+towers?\b", re.I), "cooling_tower"),
    (re.compile(r"\bair\s+handl(er|ing)s?\b|\bahus?\b", re.I), "air_handling_unit"),
    (re.compile(r"\bvavs?\b", re.I), "vav_terminal"),
]


def infer_equipment_type(work_desc: str) -> str | None:
    if not work_desc:
        return None
    for pattern, equipment in _EQUIPMENT_PATTERNS:
        if pattern.search(work_desc):
            return equipment
    return None


def fetch_parcel_characteristics(client: PoliteClient, ains: list[str],
                                 roll_year: str = "2025", batch_size: int = 50) -> dict[str, dict]:
    """AIN -> {use_code, use_desc, sqft, year_built, address} for exactly the
    given parcels, batched (ArcGIS has practical URL-length limits on large
    IN-lists)."""
    out: dict[str, dict] = {}
    for i in range(0, len(ains), batch_size):
        batch = ains[i:i + batch_size]
        ain_list = ",".join(f"'{a}'" for a in batch)
        where = f"AIN IN ({ain_list}) AND RollYear = '{roll_year}'"
        try:
            resp = client.get_json(FEATURE_SERVER, params={
                "where": where,
                "outFields": "AIN,PropertyLocation,UseCode,UseCodeDescChar1,YearBuilt,SQFTmain",
                "f": "json", "resultRecordCount": batch_size,
            })
        except Exception as exc:  # noqa: BLE001 — one failed batch shouldn't kill the whole build
            log.warning("parcel characteristics batch failed: %s", exc)
            continue
        for feature in resp.get("features", []):
            attrs = feature["attributes"]
            ain = attrs.get("AIN")
            if not ain:
                continue
            year_built = None
            if attrs.get("YearBuilt") and str(attrs["YearBuilt"]).isdigit():
                year_built = int(attrs["YearBuilt"])
            out[ain] = {
                "address": attrs.get("PropertyLocation"),
                "use_code": attrs.get("UseCode"),
                "use_desc": attrs.get("UseCodeDescChar1"),
                "sqft": attrs.get("SQFTmain"),
                "year_built": year_built,
            }
    return out


def rank_buildings(*, service_life_status: str | None, sqft: float | None,
                   sb1206_trigger_status: str | None, ebewe_candidate: bool,
                   carb_candidate: bool) -> float:
    """Hierarchy first, composite within tier — the same fix ladder.py's
    reachability-first sort applies to contacts, applied here to buildings.

    A first attempt weighted service life, size and regulatory proximity
    into one linear blend (0.5/0.3/0.2) and it was wrong the same way the
    ladder's proximity-first sort was wrong: a big enough "not_due" building
    (a downtown high-rise with equipment installed 2 years ago) outscored a
    genuinely "overdue" one, because size alone could buy back urgency. That
    is not a ranking bug at the margins — it puts the buildings LEAST worth
    calling at the top.

    So service life status is now a TIER, not a weighted term: every overdue
    building scores strictly higher than every due building, which scores
    strictly higher than every approaching building, and so on, regardless
    of size. Size and regulatory proximity only break ties WITHIN a tier —
    among buildings equally overdue, the bigger and more regulation-pressed
    one still sorts first, which is the deal-value/urgency signal the size
    and regulatory terms were meant to carry in the first place.
    """
    life_tier = {"overdue": 3, "due": 2, "approaching": 1, "not_due": 0}.get(service_life_status, -1)

    size_factor = 0.0
    if sqft and sqft > 0:
        import math
        size_factor = max(0.0, min(1.0, math.log10(sqft) / 6.0))

    reg_weight = 0.0
    if sb1206_trigger_status == "in_effect":
        reg_weight = 0.3
    elif sb1206_trigger_status == "upcoming":
        reg_weight = 0.2
    if ebewe_candidate:
        reg_weight = max(reg_weight, 0.1)
    if carb_candidate:
        reg_weight = max(reg_weight, 0.1)

    # size_factor and reg_weight are each capped well under 1.0 combined, so
    # they can only ever break ties WITHIN a tier, never cross one.
    within_tier = round(size_factor * 0.7 + reg_weight, 4)
    return round(life_tier + within_tier, 4)


def build_retrofit_buildings(session, cfg: Config, client: PoliteClient) -> dict:
    """The full pipeline: dedup permits to buildings, join assessor
    characteristics, evaluate regulatory triggers, compute service life and
    rank. Replaces the entire RetrofitBuilding table on each run — this is a
    derived view over EquipmentPermit/AssessorCandidate, not its own source
    of truth, so a full rebuild is the correct semantics (same reasoning as
    IEPR's replace-on-import)."""
    permits = session.exec(
        select(EquipmentPermit).where(EquipmentPermit.apn.is_not(None))).all()

    by_apn: dict[str, list[EquipmentPermit]] = {}
    masked = 0
    for p in permits:
        if "*" in p.apn:
            masked += 1
            continue
        by_apn.setdefault(p.apn, []).append(p)

    apns = list(by_apn.keys())
    characteristics = fetch_parcel_characteristics(client, apns) if apns else {}

    carb_by_ain: dict[str, str] = {}
    ebewe_ains: set[str] = set()
    for c in session.exec(
            select(AssessorCandidate).where(AssessorCandidate.ain.in_(apns))).all() if apns else []:
        if c.trigger_key == "carb_refrigerant_management_program":
            carb_by_ain[c.ain] = c.use_code
        elif c.trigger_key == "la_ebewe_audit_retrocommissioning":
            ebewe_ains.add(c.ain)

    session.exec(delete(RetrofitBuilding).where(RetrofitBuilding.population == "recently_active"))

    now = utcnow()
    built = 0
    for apn, group in by_apn.items():
        latest = max(group, key=lambda p: p.issue_date or datetime.min)
        equipment_type = None
        for p in sorted(group, key=lambda p: p.issue_date or datetime.min, reverse=True):
            equipment_type = infer_equipment_type(p.work_desc)
            if equipment_type:
                break

        chars = characteristics.get(apn, {})
        install_year = latest.issue_date.year if latest.issue_date else None
        refrigerant = infer_refrigerant(install_year)
        sb1206 = sb1206_status(cfg, install_year)

        age_years = None
        sl_status, sl_basis = None, None
        if latest.issue_date:
            age_years = round((now - latest.issue_date).days / 365.25, 1)
            if equipment_type:
                try:
                    sl = service_life(cfg, equipment_type, ownership=None)  # None -> default_ownership
                    sl_status = sl.status(age_years)
                    sl_basis = (f"{age_years:.0f}yr old {equipment_type.replace('_', ' ')}; "
                               f"expected life {sl.low}-{sl.high}yr under {sl.ownership} "
                               f"ownership (default — no owner data available) "
                               f"[{'VERIFIED' if sl.verified else 'UNVERIFIED'}: {sl.source}]")
                except UnknownEquipment:
                    pass

        carb_use_code = carb_by_ain.get(apn)
        rank = rank_buildings(
            service_life_status=sl_status, sqft=chars.get("sqft"),
            sb1206_trigger_status=sb1206["status"] if sb1206 else None,
            ebewe_candidate=apn in ebewe_ains, carb_candidate=carb_use_code is not None,
        )

        session.add(RetrofitBuilding(
            apn=apn, population="recently_active",
            address=chars.get("address") or latest.address,
            use_code=chars.get("use_code"), use_desc=chars.get("use_desc"),
            sqft=chars.get("sqft"), year_built=chars.get("year_built"),
            permit_count=len(group), latest_permit_nbr=latest.permit_nbr,
            latest_install_year=install_year, equipment_type=equipment_type,
            mined_tons_each=latest.tons_each, mined_equipment_count=latest.equipment_count,
            inferred_refrigerant=refrigerant["refrigerant"],
            sb1206_trigger_status=sb1206["status"] if sb1206 else None,
            sb1206_detail=sb1206["detail"] if sb1206 else None,
            carb_candidate=carb_use_code is not None, carb_use_code=carb_use_code,
            ebewe_candidate=apn in ebewe_ains,
            service_life_status=sl_status, service_life_basis=sl_basis,
            equipment_age_years=age_years, rank_score=rank,
            permit_source_url=PERMITS_PORTAL_URL, assessor_source_url=ASSESSOR_PORTAL_URL,
            built_at=now,
        ))
        built += 1

    session.commit()
    return {
        "permits_considered": len(permits),
        "permits_masked_apn_skipped": masked,
        "distinct_buildings": built,
        "assessor_matched": sum(1 for a in apns if a in characteristics),
        "assessor_unmatched": sum(1 for a in apns if a not in characteristics),
    }


def _fetch_commercial_parcels_built_before(client: PoliteClient, year: int,
                                           roll_year: str = "2025", page_size: int = 2000,
                                           max_pages: int = 200) -> list[dict]:
    """Every commercial-use parcel with a YearBuilt before `year`, county-
    wide -- the raw universe find_replacement_candidates() checks permit
    absence against."""
    where = f"UseCodeDescChar1 = 'Commercial' AND YearBuilt < '{year}' AND YearBuilt <> ''"
    if roll_year:
        where += f" AND RollYear = '{roll_year}'"
    fields = "AIN,PropertyLocation,UseCode,UseCodeDescChar1,YearBuilt,SQFTmain"
    out: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        resp = client.get_json(FEATURE_SERVER, params={
            "where": where, "outFields": fields, "f": "json",
            "resultOffset": offset, "resultRecordCount": page_size,
            "orderByFields": "AIN",
        })
        features = resp.get("features", [])
        if not features:
            break
        out.extend(f["attributes"] for f in features)
        offset += page_size
        if len(features) < page_size:
            break
    return out


def find_replacement_candidates(session, cfg: Config, client: PoliteClient, *,
                                year_built_before: int = 2010) -> dict:
    """The ABSENCE query: commercial parcels built before `year_built_before`
    with NO mechanical permit on record at all (across every EquipmentPermit
    row this system has, which after `scout fetch-permits --window all`
    spans 2010-present).

    Presence of a permit is evidence someone already replaced the equipment.
    Its absence, on a building old enough that a real replacement would
    almost certainly have needed one, means either the original equipment
    is still running or it was replaced without a permit -- either way it's
    a live candidate no permit-presence ranking can ever surface, because a
    building on this list has never (in this system's window) generated a
    permit to rank BY.

    No equipment type, no tonnage, no SB 1206 status: none of that can be
    known without a permit's work-description text. Ranked by building age
    (from year_built) and size only, and every row says exactly that in its
    basis -- weaker evidence than the permit-verified recently_active
    population, disclosed as such, not presented the same way.
    """
    parcels = _fetch_commercial_parcels_built_before(client, year_built_before)
    permitted_apns = {
        a for a in session.exec(select(EquipmentPermit.apn)).all() if a and "*" not in a
    }

    now = utcnow()
    session.exec(delete(RetrofitBuilding).where(RetrofitBuilding.population == "replacement_candidate"))

    candidates = 0
    for attrs in parcels:
        ain = attrs.get("AIN")
        if not ain or ain in permitted_apns:
            continue
        year_built = int(attrs["YearBuilt"]) if str(attrs.get("YearBuilt", "")).isdigit() else None
        age = round(now.year - year_built, 1) if year_built else None
        sqft = attrs.get("SQFTmain")
        size_factor = 0.0
        if sqft and sqft > 0:
            import math
            size_factor = max(0.0, min(1.0, math.log10(sqft) / 6.0))
        # No tier tricks needed here: every row in this population is
        # equally "no permit evidence," so age + size compose directly.
        age_factor = max(0.0, min(1.0, (age or 0) / 60.0))
        rank = round(age_factor * 0.6 + size_factor * 0.4, 4)

        session.add(RetrofitBuilding(
            apn=ain, population="replacement_candidate",
            address=attrs.get("PropertyLocation"),
            use_code=attrs.get("UseCode"), use_desc=attrs.get("UseCodeDescChar1"),
            sqft=sqft, year_built=year_built, building_age_years=age,
            permit_count=0, service_life_status=None,
            service_life_basis=(
                f"No mechanical permit on record since 2010 — original equipment "
                f"presumed still in place (or replaced without a permit). Age is "
                f"from year built ({year_built}), NOT permit-verified equipment "
                f"install date — weaker evidence than the recently_active population."
                if year_built else
                "No mechanical permit on record since 2010 and no year-built on file."
            ),
            rank_score=rank,
            permit_source_url=PERMITS_PORTAL_URL, assessor_source_url=ASSESSOR_PORTAL_URL,
            built_at=now,
        ))
        candidates += 1

    session.commit()
    return {
        "commercial_parcels_scanned": len(parcels),
        "already_permitted_excluded": len(parcels) - candidates,
        "replacement_candidates": candidates,
    }
