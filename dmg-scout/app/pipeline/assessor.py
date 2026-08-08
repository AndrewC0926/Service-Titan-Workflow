"""LA County Assessor parcel roll import (Phase 6e) — identifies CANDIDATE
parcels for two triggers by use code / building size, since neither
regulation's real filer list is public:

  - CARB Refrigerant Management Program: no public R3 facility list exists
    (confirmed — the R3 portal is login-gated for filers, and data.ca.gov has
    no matching dataset). Assessor use codes for supermarkets, small food
    stores, restaurants and cold-storage/heavy-manufacturing parcels are the
    only automatable proxy for "likely operates >=50 lbs of GWP>=150
    refrigerant" — see app/pipeline/regulatory.py's carb_refrigerant_management
    _program trigger.
  - LA EBEWE: any building over the 20,000 sqft threshold is IN SCOPE for the
    5-year ASHRAE Level II audit/retro-commissioning cycle, whether or not it
    has actually filed one.

"Candidate" is the operative word throughout — see AssessorCandidate's
docstring in app/models.py. This can never say a parcel confirmedly reports
to CARB or has confirmedly completed an EBEWE audit, only that it is in the
segment the trigger applies to.

Source: LA County's own ArcGIS FeatureServer (Assessor Parcel Data, Rolls
2021-2025) — confirmed live and queryable, no auth required.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import select

from app.config import Config
from app.http import PoliteClient
from app.models import AssessorCandidate, utcnow

log = logging.getLogger(__name__)

SOURCE = "la_county_assessor"
FEATURE_SERVER = ("https://services.arcgis.com/RmCCgQtiZLDCtblq/arcgis/rest/services/"
                  "Parcel_Data_2021_Table/FeatureServer/0/query")
PORTAL_URL = "https://egis-lacounty.hub.arcgis.com/datasets/lacounty::assessor-parcel-data-rolls-2021-2025"

# Use codes for the CARB RMP candidate segment: supermarkets, small food
# stores, restaurants/fast food (walk-in coolers), and cold storage /
# heavy-manufacturing-classified plants. See app/pipeline/regulatory.py's
# carb_refrigerant_management_program trigger.
CARB_USE_CODES = ("1400", "1410", "1420", "2100", "2110", "2120", "3240")
EBEWE_SQFT_THRESHOLD = 20000


def _fetch_page(client: PoliteClient, where: str, out_fields: str, offset: int, page_size: int) -> list[dict]:
    resp = client.get_json(FEATURE_SERVER, params={
        "where": where, "outFields": out_fields, "f": "json",
        "resultOffset": offset, "resultRecordCount": page_size,
        "orderByFields": "AIN",
    })
    return [f["attributes"] for f in resp.get("features", [])]


def fetch_carb_candidates(session, cfg: Config, client: PoliteClient, *,
                          roll_year: str = "2025", max_pages: int = 20, page_size: int = 2000) -> dict:
    """CARB RMP candidates: parcels whose use code matches the commercial-
    refrigeration segment. Upserts AssessorCandidate rows keyed by
    (source, ain, trigger_key) — safe to re-run."""
    codes = ",".join(f"'{c}'" for c in CARB_USE_CODES)
    where = f"UseCode IN ({codes}) AND RollYear = '{roll_year}'"
    fields = "AIN,PropertyLocation,UseCode,UseCodeDescChar1,YearBuilt,SQFTmain"
    return _upsert_candidates(session, client, where=where, fields=fields,
                              trigger_key="carb_refrigerant_management_program",
                              max_pages=max_pages, page_size=page_size)


def fetch_ebewe_candidates(session, cfg: Config, client: PoliteClient, *,
                           roll_year: str = "2025", max_pages: int = 20, page_size: int = 2000) -> dict:
    """LA EBEWE candidates: parcels over the 20,000 sqft audit threshold.
    City-of-LA scope is not distinguishable from this county-wide dataset
    alone (EBEWE is LA CITY only, not county) -- rows outside LA city limits
    are stored anyway since PropertyLocation is address text, not a
    jurisdiction flag; the doctor/gate report caller is responsible for
    treating this as "county-wide over-threshold parcels," not confirmed
    LA-city EBEWE scope."""
    where = f"SQFTmain > {EBEWE_SQFT_THRESHOLD} AND RollYear = '{roll_year}'"
    fields = "AIN,PropertyLocation,UseCode,UseCodeDescChar1,YearBuilt,SQFTmain"
    return _upsert_candidates(session, client, where=where, fields=fields,
                              trigger_key="la_ebewe_audit_retrocommissioning",
                              max_pages=max_pages, page_size=page_size)


def _upsert_candidates(session, client: PoliteClient, *, where: str, fields: str,
                       trigger_key: str, max_pages: int, page_size: int) -> dict:
    existing = {
        c.ain for c in session.exec(
            select(AssessorCandidate).where(AssessorCandidate.source == SOURCE,
                                            AssessorCandidate.trigger_key == trigger_key)).all()
    }

    fetched, stored, offset = 0, 0, 0
    for _ in range(max_pages):
        try:
            page = _fetch_page(client, where, fields, offset, page_size)
        except Exception as exc:  # noqa: BLE001 — reported, not raised past this stage
            log.error("assessor candidate fetch failed (%s): %s", trigger_key, exc)
            return {"fetched": fetched, "stored": stored, "error": str(exc)}
        if not page:
            break
        fetched += len(page)
        for attrs in page:
            ain = attrs.get("AIN")
            if not ain or ain in existing:
                continue
            existing.add(ain)
            year_built = None
            if attrs.get("YearBuilt") and str(attrs["YearBuilt"]).isdigit():
                year_built = int(attrs["YearBuilt"])
            session.add(AssessorCandidate(
                source=SOURCE, ain=ain, trigger_key=trigger_key,
                use_code=attrs.get("UseCode"), use_desc=attrs.get("UseCodeDescChar1"),
                address=attrs.get("PropertyLocation"), year_built=year_built,
                sqft=attrs.get("SQFTmain"), source_url=PORTAL_URL, imported_at=utcnow(),
            ))
            stored += 1
        offset += page_size
        if len(page) < page_size:
            break

    session.commit()
    return {"fetched": fetched, "stored": stored}
