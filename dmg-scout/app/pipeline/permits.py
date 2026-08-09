"""LA City mechanical permit import (Phase 6c/6d) — the install-year evidence
SB 1206's R-410A inference needs, and the work-description text tonnage
mining runs against. Also the evidence side of the ABSENCE query
(app/pipeline/retrofit.py:find_replacement_candidates): a commercial
building with NO mechanical permit across this whole window either has
original equipment or had it replaced without a permit — either way it's
a candidate no permit-presence ranking can ever surface.

Source: `data.lacity.org`'s Socrata SODA API. LADBS splits its mechanical
permit history across three same-schema datasets by date window (field
lists verified identical across all three, 2026-08-08):
  - 67is-svtd  "...Issued from 2020 to Present (N)"   — live, daily-refreshed
  - 5m3t-xjex  "...Issued Between 2010 and 2019 (N)"  — frozen
  - mcip-sa6g  "...Issued Before 2010 (N)"            — frozen, small (~900 rows)
robots.txt does not disallow the `/resource/*.json` API path on any of them.
No auth required for read queries. All three write into the same
EquipmentPermit table under one `SOURCE` value — permit_nbr is the real
unique key regardless of which historical file LADBS happened to put a row
in.

Checked for a contractor/applicant name field on all three datasets
(2026-08-08): none exists. The permit schema has no contractor,
applicant, or licensee field at all — every field is either building/parcel
identification (APN, address, zone) or process metadata (dates, status,
permit type, work description). "Which mechanical contractors are working
in my territory" is not answerable from this dataset.

Not wired into the standard fetch->triage->extract->resolve pipeline: a
permit is not a Signal and never becomes a Project. It feeds
EquipmentPermit directly, evaluated against app/pipeline/regulatory.py at
import time.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import select

from app.config import Config
from app.http import PoliteClient
from app.models import EquipmentPermit, utcnow
from app.pipeline.regulatory import (
    infer_refrigerant, parse_equipment_from_work_desc, sb1206_status,
)

log = logging.getLogger(__name__)

SOURCE = "la_city_mechanical"
DATASETS = {
    "2020_present": "67is-svtd",
    "2010_2019": "5m3t-xjex",
    "before_2010": "mcip-sa6g",
}
PORTAL_URL = "https://data.lacity.org/Housing-and-Buildings/Building-and-Safety-Mechanical-Permits-Issued-fro/67is-svtd"


def _parse_permit(row: dict) -> dict:
    issue_date = None
    if row.get("issue_date"):
        try:
            issue_date = datetime.fromisoformat(row["issue_date"])
        except ValueError:
            pass

    mined = parse_equipment_from_work_desc(row.get("work_desc", ""))
    return {
        "permit_nbr": row["permit_nbr"],
        "apn": row.get("apn"),
        "address": row.get("primary_address"),
        "permit_type": row.get("permit_type"),
        "permit_sub_type": row.get("permit_sub_type"),
        "status_desc": row.get("status_desc"),
        "issue_date": issue_date,
        "work_desc": row.get("work_desc") or "",
        "equipment_count": mined["count"],
        "tons_each": mined["tons_each"],
    }


def fetch_la_mechanical_permits(session, cfg: Config, client: PoliteClient, *,
                                since: datetime | None = None, limit: int = 5000,
                                window: str = "2020_present") -> dict:
    """Pull issued commercial/apartment HVAC permits from one of the three
    date-window datasets (see DATASETS), mine tonnage/count from work_desc,
    evaluate SB 1206 against the inferred install year, and upsert into
    EquipmentPermit. Safe to re-run: re-fetching a permit_nbr already stored
    updates that row rather than duplicating it. `since` only makes sense
    for the live 2020_present window; the frozen legacy windows ignore it
    and are small enough to pull in full."""
    base_url = f"https://data.lacity.org/resource/{DATASETS[window]}.json"
    where_parts = [
        "permit_type = 'HVAC'",
        "permit_sub_type in ('Commercial', 'Apartment')",
        "status_desc = 'Issued'",
    ]
    if since and window == "2020_present":
        where_parts.append(f"issue_date >= '{since.date().isoformat()}'")
    params = {
        "$where": " AND ".join(where_parts),
        "$limit": limit,
        "$order": "issue_date DESC",
    }

    try:
        rows = client.get_json(base_url, params=params)
    except Exception as exc:  # noqa: BLE001 — reported, not raised past this stage
        log.error("LA mechanical permit fetch failed (%s): %s", window, exc)
        return {"fetched": 0, "stored": 0, "sb1206_flagged": 0, "error": str(exc)}

    existing = {
        p.permit_nbr: p for p in session.exec(
            select(EquipmentPermit).where(EquipmentPermit.source == SOURCE)).all()
    }

    stored, sb1206_flagged = 0, 0
    for raw in rows:
        parsed = _parse_permit(raw)
        install_year = parsed["issue_date"].year if parsed["issue_date"] else None
        refrigerant = infer_refrigerant(install_year)
        trigger = sb1206_status(cfg, install_year)
        if trigger:
            sb1206_flagged += 1

        permit = existing.get(parsed["permit_nbr"])
        if permit is None:
            permit = EquipmentPermit(source=SOURCE, permit_nbr=parsed["permit_nbr"],
                                     source_url=PORTAL_URL)
            session.add(permit)

        for field in ("apn", "address", "permit_type", "permit_sub_type", "status_desc",
                      "issue_date", "work_desc", "equipment_count", "tons_each"):
            setattr(permit, field, parsed[field])
        permit.inferred_refrigerant = refrigerant["refrigerant"]
        permit.sb1206_trigger_status = trigger["status"] if trigger else None
        permit.sb1206_detail = trigger["detail"] if trigger else None
        permit.imported_at = utcnow()
        stored += 1

    session.commit()
    return {"fetched": len(rows), "stored": stored, "sb1206_flagged": sb1206_flagged}
