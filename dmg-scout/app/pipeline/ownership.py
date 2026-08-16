"""Change-of-ownership detection for the retrofit board.

COMPLIANCE (2026-08-16). The user's actual ask was LA County Recorder deed
records. Checked before building anything: the Recorder (Registrar-
Recorder/County Clerk, RR/CC) offers NO bulk or API access to its deed
index -- confirmed via lavote.gov/home/recorder and laalmanac.com. The only
access paths are (1) an in-person visit to the Norwalk headquarters, or (2)
a per-document request fulfilled through VitalChek Network, a paid
third-party document-fulfillment service, with a stated ~20-business-day
turnaround. That is a request queue for individual documents, not a
sanctioned bulk or API path -- the same shape of dead end as DIR's
contractor registry, so per instruction this stops here: no Recorder
scraper was built, and no workaround (e.g. automating per-document
requests) was attempted.

What IS built instead is a different, independently public data source
that happens to carry the same underlying fact for a different
administrative reason. California's Prop 13 requires the County Assessor
to reassess a parcel whenever a deed recording changes its ownership, and
the Assessor's own public parcel roll -- already used by this pipeline for
CARB/EBEWE candidate detection, see app/pipeline/assessor.py -- carries a
RecordingDate field for exactly that reason. This is NOT a substitute
obtained by working around the Recorder's restriction: it is the
Assessor's own, separately public dataset, on the same ArcGIS FeatureServer
this codebase already queries in production (confirmed live 2026-08-16;
the hosting portal egis-lacounty.hub.arcgis.com's robots.txt disallows only
/sites/, /admin/, /sessions/, /groups/, /people/, /workspace/ -- none of
which are the query API path used here).

Two real limitations, disclosed rather than papered over:
  1. Document type is NOT available. The Recorder's own deed index would
     carry deed type (grant deed, quitclaim, trustee's deed, etc.); the
     Assessor's roll does not distinguish -- only a recording date. Every
     row here has a date and no document type, and this module makes no
     claim otherwise.
  2. RecordingDate reflects a Prop 13 REASSESSMENT-triggering event, not
     necessarily an arms-length market sale -- some non-sale transfers
     (certain trust/entity restructurings) are also reassessable. It is
     still the strongest publicly-available proxy for "did this parcel
     change hands," just not proof of a market transaction.

Mechanism: the FeatureServer table carries one row per parcel PER ROLL YEAR
(2021-2025), so the same AIN can appear up to 5 times with a RecordingDate
that only changes between roll years if a reassessable event occurred in
between -- the max RecordingDate across an AIN's rows is its most recently
known one. Batched via POST (not GET) to stay well under URL length limits
at a batch size that also stays under the FeatureServer's own 2000-record
response cap (maxRecordCount, confirmed via the layer's own metadata).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.config import Config
from app.http import PoliteClient
from app.models import OwnershipRecency, RetrofitBuilding, SourceRun, utcnow

log = logging.getLogger(__name__)

SOURCE = "la_county_ownership"
FEATURE_SERVER = ("https://services.arcgis.com/RmCCgQtiZLDCtblq/arcgis/rest/services/"
                  "Parcel_Data_2021_Table/FeatureServer/0/query")
LAST_SALE_SOURCE = "la_county_assessor_recording_date"

BATCH_SIZE = 300   # AINs per request -- ~5 roll-year rows each, comfortably under
                   # the FeatureServer's own maxRecordCount=2000 response cap.
PAGE_SIZE = 2000


def _epoch_ms_to_datetime(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _fetch_batch(client: PoliteClient, apns: list[str]) -> dict[str, datetime]:
    """Max RecordingDate per AIN across every roll-year row returned --
    paginated within the batch in case a batch of AINs with unusually many
    roll-year rows exceeds one page."""
    quoted = ",".join(f"'{a}'" for a in apns)
    where = f"AIN IN ({quoted})"
    latest: dict[str, int] = {}
    offset = 0
    while True:
        resp = client.post_json(FEATURE_SERVER, data={
            "where": where, "outFields": "AIN,RecordingDate", "f": "json",
            "resultOffset": offset, "resultRecordCount": PAGE_SIZE,
        })
        features = resp.get("features", [])
        for feat in features:
            attrs = feat.get("attributes", {})
            ain, rd = attrs.get("AIN"), attrs.get("RecordingDate")
            if ain and rd is not None:
                latest[ain] = max(rd, latest.get(ain, rd))
        if not resp.get("exceededTransferLimit") or not features:
            break
        offset += len(features)
    return {ain: _epoch_ms_to_datetime(ms) for ain, ms in latest.items()}


def fetch_ownership_recency(session: Session, cfg: Config, client: PoliteClient) -> dict:
    """Batches every retrofit_buildings APN into the FeatureServer and
    upserts OwnershipRecency (by apn) -- NOT RetrofitBuilding directly,
    since build_retrofit_buildings/find_replacement_candidates DELETE and
    reinsert that table on every rebuild (see OwnershipRecency's docstring).
    app.pipeline.retrofit rejoins this onto RetrofitBuilding.last_sale_date
    at build time, same pattern as RetrofitGeocode. Records a SourceRun --
    see module docstring for the compliance finding and the two disclosed
    limitations (no document type, not proof of an arms-length sale)."""
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    error = None
    matched = 0
    checked_at = utcnow()
    apns = session.exec(select(RetrofitBuilding.apn)).all()
    try:
        existing = {r.apn: r for r in session.exec(select(OwnershipRecency)).all()}
        for i in range(0, len(apns), BATCH_SIZE):
            batch = apns[i:i + BATCH_SIZE]
            dates = _fetch_batch(client, batch)
            for apn, sale_date in dates.items():
                row = existing.get(apn)
                if row is None:
                    row = OwnershipRecency(apn=apn, last_sale_date=sale_date,
                                           source=LAST_SALE_SOURCE, checked_at=checked_at)
                    session.add(row)
                    existing[apn] = row
                else:
                    row.last_sale_date = sale_date
                    row.checked_at = checked_at
                    session.add(row)
                matched += 1
            session.commit()
    except Exception as exc:  # noqa: BLE001 — recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("Ownership-recency fetch failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = len(apns)
    run.records_new = matched
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()
    return {"apns_total": len(apns), "matched": matched, "ok": error is None, "error": error}


def ownership_recency_coverage(session: Session) -> dict:
    """Live counts for the assumptions register and /retrofit: how many of
    ALL retrofit_buildings rows have a recorded sale date on file (the
    match rate against the Assessor FeatureServer), and how many of those
    fall within the last 24 months -- the window the ranking term actually
    weights most heavily. Computed at call time, never cached, same
    discipline as app.pipeline.resolve:delivery_method_coverage."""
    from datetime import timedelta

    from sqlalchemy import func as sa_func

    total = session.exec(select(sa_func.count(RetrofitBuilding.id))).one()
    matched = session.exec(
        select(sa_func.count(RetrofitBuilding.id)).where(RetrofitBuilding.last_sale_date.is_not(None))
    ).one()
    recent_cutoff = utcnow() - timedelta(days=730)
    recent = session.exec(
        select(sa_func.count(RetrofitBuilding.id)).where(RetrofitBuilding.last_sale_date >= recent_cutoff)
    ).one()
    return {"retrofit_buildings_total": total, "matched": matched, "sold_last_24mo": recent}
