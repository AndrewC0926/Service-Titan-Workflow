"""LA EBEWE benchmark import — data.lacity.org's Socrata SODA API, dataset
9yda-i4ya ("Existing Buildings Energy & Water Efficiency (EBEWE) Program").
One row per building per filed Compliance Year: weather-normalized site
EUI, ENERGY STAR score, and the LADBS Building ID the A/RCx audit cycle is
keyed to (see app.pipeline.regulatory:arcx_compliance_status).

THE JOIN PROBLEM, measured 2026-08-16: this dataset's own `apn` field,
which Socrata's column metadata labels "AIN", is NOT a usable parcel join
key. Every one of 96,211 rows carries exactly 3 characters ("028", "900",
...); the dataset's own official description confirms why -- it is "the
last 3 digits of the Assessor Identification Number (AIN)", not the AIN
itself. Kept here as EbeweBenchmark.ain_last3 and used only as a checksum
(see ebewe_matches_by_normalized_address below), never as a standalone key.

Two join strategies were measured against real production data before this
shipped, both against the 18,301 assessor-derived ebewe_candidate=true
proxy population (a sqft-threshold SCOPE proxy, not measured performance --
see AssessorCandidate/RetrofitBuilding docstrings) and against each other:

  1. app.pipeline.retrofit:normalize_address text join (EBEWE
     building_address vs retrofit_buildings.address, both normalized the
     same way the masked-APN permit exclusion already does): 4,983 clean
     1:1 matches, of 11,497 EBEWE buildings that have EVER filed.
  2. US Census batch geocoder (app/geocode.py) on both sides, nearest-
     neighbor spatial match: 2,740 clean matches at the best-performing
     threshold tested (15m) -- WORSE than the text join, not better. LA's
     parcels are dense enough (many under 15-50m apart) that independent
     geocoding runs on each side produce enough jitter to make more
     buildings ambiguous, not fewer; loosening the radius only made this
     worse (25m: 2,087, 40m: 1,578). Of the 2,740, only 255 were NEW
     matches the text join had missed -- the union of both methods (5,238)
     is barely above the text join alone.

Conclusion: normalize_address was NOT the limiter -- if anything it
outperforms independent geocoding on this data -- so EBEWE's shortfall
against the 18,301-row proxy is substantially real (EBEWE only has 11,497
buildings that have ever filed against an ordinance with real non-
compliance, and the proxy population is itself inflated with non-LA-city
parcels; see AssessorCandidate's docstring). The geocoder path is
documented here, not built: it did not earn its complexity. Production
uses the text join alone, strengthened with the ain_last3 checksum below.
See app/assumptions.py for the exact method, sample sizes, and final
coverage figure.

THE FALSE-POSITIVE GUARD, same discipline as
app.pipeline.retrofit:find_replacement_candidates' masked-APN exclusion --
abstain, don't guess:
  - An EBEWE address matching MORE THAN ONE retrofit_buildings apn is
    dropped (which specific building? not decidable from text alone).
  - A retrofit_buildings apn matched by MORE THAN ONE distinct EBEWE
    building_id is also dropped -- real (a multi-building complex filing
    several EBEWE registrations under one street address), but this table
    is one row per apn, so it cannot represent "several" without silently
    picking one. Measured 2026-08-16: 5,073 raw matches from address text
    alone collapsed to 4,983 once this reverse case (40 apns) and the
    forward case (113 addresses) were both excluded properly -- a first,
    sloppier pass that let a Python dict overwrite silently pick a winner
    for the reverse case was caught and is NOT what shipped.
  - Of the address-text matches, 193 of 4,983 (3.9%) disagree with the
    ain_last3 checksum (the matched apn's own last 3 digits don't equal
    the EBEWE row's ain_last3) -- also dropped. This is a free, officially
    documented independent signal; a mismatch means either the address
    join found the wrong neighboring building or EBEWE's own AIN fragment
    is wrong, and either way this is exactly the abstain-don't-guess case.
Final shipped match count: 4,790 (see app/assumptions.py).
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import select

from app.config import Config
from app.http import PoliteClient
from app.models import EbeweBenchmark, SourceRun, utcnow

log = logging.getLogger(__name__)

SOURCE = "la_ebewe_benchmarking"
DATASET_ID = "9yda-i4ya"
PORTAL_URL = ("https://data.lacity.org/City-Infrastructure-Service-Requests/"
             "Existing-Buildings-Energy-Water-Efficiency-EBEWE-P/9yda-i4ya")

_NOT_AVAILABLE = {"", "not available", "na", "n/a"}


def _num(val) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in _NOT_AVAILABLE:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(val) -> int | None:
    n = _num(val)
    return int(n) if n is not None else None


def _parse_row(row: dict) -> dict:
    return {
        "building_id": row.get("building_id"),
        "program_year": _int(row.get("program_year")),
        "ain_last3": row.get("apn") or None,  # see module docstring -- 3 digits, checksum only
        "building_address": row.get("building_address"),
        "postal_code": row.get("postal_code"),
        "primary_property_type": row.get("primary_property_1"),
        "property_gfa": _num(row.get("property_gfa_1")),
        "year_built": _int(row.get("year_built")),
        "occupancy": _num(row.get("occupancy")),
        "compliance_status": row.get("compliance"),
        "organization": row.get("organization"),
        "number_of_buildings": _int(row.get("number_of_buildings")),
        "site_eui": _num(row.get("site_eui")),
        "source_eui": _num(row.get("source_eui")),
        "weather_normalized_site_eui": _num(row.get("weather_normalized_3")),
        "weather_normalized_source_eui": _num(row.get("weather_normalized_4")),
        "percent_diff_national_median_site_eui": _num(row.get("percent_difference_1")),
        "percent_diff_national_median_source_eui": _num(row.get("percent_difference_2")),
        "energy_star_score": _int(row.get("energy_star_score")),
        "energy_star_cert_years": row.get("energy_star_cert_1"),
        "total_ghg_emissions": _num(row.get("total_ghg_emissions")),
        "indoor_water_use": _num(row.get("indoor_water_use")),
        "indoor_water_use_intensity": _num(row.get("indoor_water_use_intensity")),
        "outdoor_water_use": _num(row.get("outdoor_water_use")),
        "total_water_use": _num(row.get("water_use")),
    }


def fetch_ebewe_benchmarks(session, cfg: Config, client: PoliteClient, *,
                           page_size: int = 5000, max_pages: int = 30) -> dict:
    """Pulls every EBEWE filing (96,211 rows as of 2026-08-16, ~11,500
    distinct buildings across 2016-2025) and upserts into EbeweBenchmark,
    keyed (building_id, program_year) -- safe to re-run.

    Records its OWN SourceRun row, unlike sibling LA-specific fetches
    (app/pipeline/assessor.py, app/pipeline/permits.py), which write
    neither a SourceRun nor appear in config.yaml's `sources:` block and so
    are invisible to `scout doctor`'s per-source staleness check -- a
    pre-existing gap, not fixed here. This source does both specifically so
    it can't fail silently the way those two already can; see config.yaml's
    new `sources.la_ebewe_benchmarking` entry, which is what makes `scout
    doctor` / source_health enumerate it at all."""
    base_url = f"https://data.lacity.org/resource/{DATASET_ID}.json"
    run = SourceRun(source=SOURCE)
    session.add(run)
    session.commit()

    existing = {
        (b.building_id, b.program_year): b
        for b in session.exec(select(EbeweBenchmark).where(EbeweBenchmark.source == SOURCE)).all()
    }

    fetched, stored, offset = 0, 0, 0
    error = None
    try:
        for _ in range(max_pages):
            page = client.get_json(base_url, params={
                "$limit": page_size, "$offset": offset, "$order": "building_id,program_year",
            })
            if not page:
                break
            fetched += len(page)
            for raw in page:
                parsed = _parse_row(raw)
                if not parsed["building_id"] or parsed["program_year"] is None:
                    continue  # unkeyable row -- can't upsert without both halves of the unique key
                key = (parsed["building_id"], parsed["program_year"])
                rec = existing.get(key)
                if rec is None:
                    rec = EbeweBenchmark(source=SOURCE, building_id=parsed["building_id"],
                                         program_year=parsed["program_year"], source_url=PORTAL_URL)
                    existing[key] = rec
                    session.add(rec)
                for field, value in parsed.items():
                    setattr(rec, field, value)
                rec.imported_at = utcnow()
                stored += 1
            offset += page_size
            if len(page) < page_size:
                break
        session.commit()
    except Exception as exc:  # noqa: BLE001 — recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("EBEWE benchmark fetch failed: %s", error)

    run.finished_at = utcnow()
    run.records_fetched = fetched
    run.records_new = stored
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()
    return {"fetched": fetched, "stored": stored, "error": error}


def ebewe_matches_by_normalized_address(session, candidate_addresses: dict[str, str]) -> dict[str, dict]:
    """{normalized_address: match_fields} for every EBEWE building that
    resolves UNAMBIGUOUSLY (both directions) to exactly one retrofit
    building address, checksum-confirmed -- see module docstring for the
    three exclusion criteria and their measured counts.

    candidate_addresses: {apn: address} for every RetrofitBuilding row the
    CURRENT build is about to (re)write for its own population. Combined
    with whatever's already in retrofit_buildings from the other
    population's last build, so the reverse-ambiguity check sees the WHOLE
    table's addresses, not just whichever population happened to already
    be on disk -- passing only the current population would under-detect
    reverse ambiguity against the other population's rows, and on a cold
    build (nothing in the table yet) would find zero reverse ambiguity by
    construction, which is wrong, not conservative.

    match_fields carries everything build_retrofit_buildings/
    find_replacement_candidates need to populate RetrofitBuilding's
    ebewe_* columns, plus the A/RCx flag from
    app.pipeline.regulatory:arcx_compliance_status (computed here, once,
    rather than per row, since it depends only on ebewe_building_id)."""
    from app.pipeline.regulatory import arcx_compliance_status
    from app.pipeline.retrofit import normalize_address

    latest: dict[str, EbeweBenchmark] = {}
    for rec in session.exec(select(EbeweBenchmark)).all():
        cur = latest.get(rec.building_id)
        if cur is None or rec.program_year > cur.program_year:
            latest[rec.building_id] = rec

    by_addr: dict[str, list[EbeweBenchmark]] = {}
    for rec in latest.values():
        norm = normalize_address(rec.building_address)
        if norm:
            by_addr.setdefault(norm, []).append(rec)
    unambiguous_ebewe = {addr: recs[0] for addr, recs in by_addr.items() if len(recs) == 1}

    from app.models import RetrofitBuilding
    rb_addrs: dict[str, set[str]] = {}
    existing_rows = session.exec(
        select(RetrofitBuilding.apn, RetrofitBuilding.address).where(RetrofitBuilding.address.is_not(None))
    ).all()
    for apn, address in (*existing_rows, *candidate_addresses.items()):
        norm = normalize_address(address)
        if norm:
            rb_addrs.setdefault(norm, set()).add(apn)
    unambiguous_rb = {addr: next(iter(apns)) for addr, apns in rb_addrs.items() if len(apns) == 1}

    out: dict[str, dict] = {}
    for addr, rec in unambiguous_ebewe.items():
        apn = unambiguous_rb.get(addr)
        if apn is None:
            continue
        if rec.ain_last3 and apn[-3:] != rec.ain_last3:
            continue  # checksum disagrees -- abstain, see module docstring
        arcx = arcx_compliance_status(rec.building_id)
        out[addr] = {
            "ebewe_matched": True,
            "ebewe_building_id": rec.building_id,
            "ebewe_program_year": rec.program_year,
            "ebewe_energy_star_score": rec.energy_star_score,
            "ebewe_site_eui": rec.site_eui,
            "ebewe_weather_normalized_site_eui": rec.weather_normalized_site_eui,
            "ebewe_property_type": rec.primary_property_type,
            "ebewe_arcx_due_this_year": bool(arcx and arcx["due_this_year"]),
            "ebewe_arcx_next_compliance_date": (
                datetime.combine(arcx["next_compliance_date"], datetime.min.time()) if arcx else None
            ),
        }
    return out
