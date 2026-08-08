"""LA City mechanical permit import (Phase 6c/6d) — the install-year evidence
SB 1206's R-410A inference needs, and the work-description text tonnage
mining runs against.

Source: `data.lacity.org`'s Socrata SODA API, dataset 67is-svtd ("Building
and Safety - Mechanical Permits Issued from 2020 to Present (N)") — LADBS's
actively-maintained mechanical-permit feed (confirmed live, daily-refreshed,
not one of the frozen/legacy permit datasets on the same portal). robots.txt
does not disallow the `/resource/*.json` API path. No auth required for read
queries.

This intentionally does NOT reach back before 2020: the pre-2020 mechanical
permit history lives in a separate, unverified-schema legacy dataset
(`5m3t-xjex`, "2010-2019") that this module has not been built against —
wiring that in needs its own field-name verification first, not a guess that
it matches this one. That means SB 1206 install-year coverage here spans
2020-2024 of the 2010-2024 R-410A window, not the full window — a real,
disclosed gap, not a silent one.

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
DATASET_ID = "67is-svtd"
BASE_URL = f"https://data.lacity.org/resource/{DATASET_ID}.json"
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
                                since: datetime | None = None, limit: int = 5000) -> dict:
    """Pull issued commercial/apartment HVAC permits, mine tonnage/count from
    work_desc, evaluate SB 1206 against the inferred install year, and
    upsert into EquipmentPermit. Safe to re-run: re-fetching a permit_nbr
    already stored updates that row rather than duplicating it."""
    where_parts = [
        "permit_type = 'HVAC'",
        "permit_sub_type in ('Commercial', 'Apartment')",
        "status_desc = 'Issued'",
    ]
    if since:
        where_parts.append(f"issue_date >= '{since.date().isoformat()}'")
    params = {
        "$where": " AND ".join(where_parts),
        "$limit": limit,
        "$order": "issue_date DESC",
    }

    try:
        rows = client.get_json(BASE_URL, params=params)
    except Exception as exc:  # noqa: BLE001 — reported, not raised past this stage
        log.error("LA mechanical permit fetch failed: %s", exc)
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
