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
from app.models import AssessorCandidate, EquipmentPermit, RetrofitBuilding, ServiceFrequencyReport, utcnow
from app.pipeline.assessor import FEATURE_SERVER, PORTAL_URL as ASSESSOR_PORTAL_URL
from app.pipeline.permits import PORTAL_URL as PERMITS_PORTAL_URL
from app.pipeline.regulatory import infer_refrigerant, sb1206_status
from app.replacement import UnknownEquipment, generic_service_life, service_life

log = logging.getLogger(__name__)

PERMIT_OBSERVATION_START_YEAR = 2010
# 16,951 of 17,010 permits on file (2026-08-11) are 2010+ -- the 2010-2019 and
# present-day open-data sources (see app/pipeline/permits.py's dataset ids)
# cover the overwhelming majority. A small "before_2010" source exists
# (mcip-sa6g, frozen, ~900 rows total, only 59 currently fetched into this
# table) but is not treated as extending the observation window for the
# abstention rule in find_replacement_candidates below -- it's too sparse to
# change the conclusion "absence before 2010 is not observed", and treating
# it as if it did would be exactly the false-precision this rule removes.

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


# Street-suffix and directional normalization for normalize_address() below.
# Canonical form is the short one (ST not STREET, N not NORTH) since that's
# what LADBS permit addresses already use natively.
_ADDR_SUFFIX_MAP = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR",
    "PARKWAY": "PKWY", "PLACE": "PL", "ROAD": "RD", "COURT": "CT", "LANE": "LN",
    "HIGHWAY": "HWY", "CIRCLE": "CIR", "TERRACE": "TER", "SQUARE": "SQ",
}
_ADDR_DIR_MAP = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}
_ADDR_SUFFIX_TOKENS = frozenset(_ADDR_SUFFIX_MAP.values()) | {"WAY"}


def normalize_address(addr: str | None) -> str | None:
    """Street number + directional + street name + suffix, nothing past
    that -- used to match a candidate parcel's assessor address against a
    permit's address when the permit's APN can't be used for the join (see
    find_replacement_candidates: 27% of all permits carry a privacy-masked
    or missing APN and are otherwise invisible to the exclusion check).

    Truncates at the first recognized street-suffix token rather than
    trying to strip a suite/unit/floor suffix by keyword: sampled real
    LADBS permit addresses (2026-08-09) format a trailing unit as a bare
    token with no keyword at all -- "700 S MAIN ST 14", "6930 N DE CELIS PL
    UNIT 4", "215 S SANTA FE AVE NO     8" -- so a keyword-based strip
    (SUITE/UNIT/#) misses the bare-number case entirely. Truncating after
    the suffix token catches all of them the same way, and as a side
    effect also drops any trailing city/state/zip a caller's address
    string carries (assessor addresses do; permit addresses don't) without
    needing to know which format it's in.

    An address with no recognized suffix token is returned unchanged
    (uppercased/whitespace-normalized) rather than guessed at.
    """
    if not addr:
        return None
    tokens = addr.upper().replace(",", " ").split()
    if not tokens:
        return None
    tokens = [_ADDR_DIR_MAP.get(t, t) for t in tokens]
    tokens = [_ADDR_SUFFIX_MAP.get(t, t) for t in tokens]
    for i, t in enumerate(tokens):
        if t in _ADDR_SUFFIX_TOKENS:
            tokens = tokens[:i + 1]
            break
    return " ".join(tokens) or None


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


def latest_service_frequency_by_apn(session, apns: list[str]) -> dict[str, ServiceFrequencyReport]:
    """Most recent ServiceFrequencyReport per apn, for the apns given.
    Manual entry only (`scout report-service-frequency`) — see that model's
    docstring for why this lives in its own table instead of a column
    RetrofitBuilding's own rebuild would silently wipe."""
    if not apns:
        return {}
    reports = session.exec(
        select(ServiceFrequencyReport).where(ServiceFrequencyReport.apn.in_(apns))
    ).all()
    latest: dict[str, ServiceFrequencyReport] = {}
    for r in reports:
        cur = latest.get(r.apn)
        if cur is None or r.reported_at > cur.reported_at:
            latest[r.apn] = r
    return latest


def rank_buildings(*, service_life_status: str | None, sqft: float | None,
                   sb1206_trigger_status: str | None, ebewe_candidate: bool,
                   carb_candidate: bool, service_life_years_past: float | None = None,
                   service_calls_per_year: float | None = None) -> float:
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
    of size. That discipline stands. But a second failure of the same shape
    showed up inside the tier: find_replacement_candidates' whole population
    is built pre-2010, so nearly every row (measured: 95 percent) reads
    "overdue" — the status stopped discriminating, and with the tier
    constant across almost the whole population, size (via the old 0.7
    weight) became the de facto sort while the board labelled it urgency.
    That is the spillover-weight failure again: a term that looks like
    signal but is actually just restating the one thing that varies.

    The fix is the same shape as before: promote the thing that actually
    varies WITHIN the saturated tier. service_life_years_past (age minus the
    low/"due" threshold — negative before the window, growing through due
    into overdue, see app/replacement.py) is now the lead term within a
    tier; size and regulatory proximity are secondary tie-breakers, not the
    sort key. All three terms are still capped well under 1.0 combined
    (0.55 + 0.25 + 0.4*0.3=0.12 = 0.92 max), so — same invariant as before —
    no combination of magnitude, size and regulatory pressure can ever cross
    a full tier step.

    service_calls_per_year (2026-08-11, manual entry only — see
    ServiceFrequencyReport) OVERRIDES all of the above when present: actual
    reported service frequency is a stronger replacement signal than the
    YearBuilt/install-year proxy this whole function otherwise runs on, so a
    reported building is placed above the entire proxy-ranked board, not
    just promoted within its own tier the way years-past is. When it's
    None — every row today, and the overwhelming majority of rows for a long
    time — this function's return value is IDENTICAL to before this
    parameter existed; nothing below this docstring changed. This is a
    hypothesis with exactly one data point (a contractor reporting 20+ calls
    in a year on one unit) and is deliberately not tuned further than "more
    reported calls ranks higher than fewer" until there's enough of a sample
    to tune against — see app.assumptions and
    app.pipeline.retrofit:service_calls_coverage.
    """
    if service_calls_per_year is not None:
        # 1000 exceeds the max possible proxy-based score (tier 3 + within_tier
        # capped at 0.92 = 3.92), so any reported row sorts above every
        # unreported one, unconditionally. The +min(calls, 200) term only
        # orders reported rows against EACH OTHER by call count -- capped
        # the same way service_life_years_past is capped, so one absurd
        # outlier figure can't be read as more informative than it is.
        return round(1000.0 + min(max(service_calls_per_year, 0.0), 200.0), 4)

    life_tier = {"overdue": 3, "due": 2, "approaching": 1, "not_due": 0}.get(service_life_status, -1)

    magnitude_factor = 0.0
    if service_life_years_past is not None:
        # Capped, not linear: a building 300yr past its service-life window
        # (a handful of these exist -- almost certainly assessor YearBuilt
        # data errors, e.g. "1806") shouldn't infinitely outrank a merely
        # 90yr-overdue one. The cap is set from the observed distribution,
        # not a round number picked in the abstract: on the replacement-
        # candidate population (measured 2026-08-09) the 99th percentile of
        # years-past among overdue rows is ~98 -- a 60yr cap saturated
        # magnitude for most of the top-ranked (biggest, oldest) buildings,
        # handing the sort back to size exactly at the top of the list,
        # which is the one failure mode issue #1 was raised to fix. 100yr
        # keeps the gradient live across the range that actually populates
        # a top-50, and still caps the handful of 150-300yr data-error rows
        # at parity with a genuinely ~100yr-overdue building instead of
        # letting them tower over everything.
        magnitude_factor = max(0.0, min(1.0, service_life_years_past / 100.0))

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

    within_tier = round(magnitude_factor * 0.55 + size_factor * 0.25 + reg_weight * 0.4, 4)
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

    service_freq = latest_service_frequency_by_apn(session, apns)

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
        sl_status, sl_basis, years_past = None, None, None
        if latest.issue_date:
            age_years = round((now - latest.issue_date).days / 365.25, 1)
            if equipment_type:
                try:
                    sl = service_life(cfg, equipment_type, ownership=None)  # None -> default_ownership
                    sl_status = sl.status(age_years)
                    years_past = round(age_years - sl.low, 1)
                    sl_basis = (f"{age_years:.0f}yr old {equipment_type.replace('_', ' ')}; "
                               f"expected life {sl.low}-{sl.high}yr under {sl.ownership} "
                               f"ownership (default — no owner data available) "
                               f"[{'VERIFIED' if sl.verified else 'UNVERIFIED'}: {sl.source}]")
                except UnknownEquipment:
                    pass

        carb_use_code = carb_by_ain.get(apn)
        freq = service_freq.get(apn)
        rank = rank_buildings(
            service_life_status=sl_status, sqft=chars.get("sqft"),
            sb1206_trigger_status=sb1206["status"] if sb1206 else None,
            ebewe_candidate=apn in ebewe_ains, carb_candidate=carb_use_code is not None,
            service_life_years_past=years_past,
            service_calls_per_year=freq.service_calls_per_year if freq else None,
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
            service_life_years_past=years_past,
            equipment_age_years=age_years, rank_score=rank,
            service_calls_per_year=freq.service_calls_per_year if freq else None,
            service_calls_per_year_source=freq.source if freq else None,
            service_calls_per_year_reported_at=freq.reported_at if freq else None,
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
        "service_frequency_reports_applied": len(service_freq),
    }


def _candidate_where(use_codes: list[str], year_built_before: int,
                     min_sqft: float | None, roll_year: str) -> str:
    codes = ",".join(f"'{c}'" for c in use_codes)
    where = f"UseCodeDescChar1 IN ({codes}) AND YearBuilt < '{year_built_before}' AND YearBuilt <> ''"
    if min_sqft:
        where += f" AND SQFTmain >= {min_sqft}"
    if roll_year:
        where += f" AND RollYear = '{roll_year}'"
    return where


def _count(client: PoliteClient, where: str, roll_year: str) -> int:
    full_where = f"{where} AND RollYear = '{roll_year}'" if roll_year else where
    resp = client.get_json(FEATURE_SERVER, params={"where": full_where, "f": "json", "returnCountOnly": "true"})
    return int(resp.get("count", 0))


def funnel_counts(client: PoliteClient, *, use_codes: list[str], year_built_before: int,
                  min_sqft: float | None, roll_year: str = "2025") -> dict:
    """How many parcels survive each filter, in order -- cheap COUNT-only
    queries (no feature payload), so this can run before the expensive full
    fetch. 95,963 raw absence-based candidates is not a call list; this is
    what shows where that population actually collapses."""
    codes = ",".join(f"'{c}'" for c in use_codes)
    use_code_where = f"UseCodeDescChar1 IN ({codes})"
    built_before_where = f"{use_code_where} AND YearBuilt < '{year_built_before}' AND YearBuilt <> ''"
    sqft_where = built_before_where + (f" AND SQFTmain >= {min_sqft}" if min_sqft else "")
    return {
        "use_code_match": _count(client, use_code_where, roll_year),
        "built_before": _count(client, built_before_where, roll_year),
        "sqft_floor_survivors": _count(client, sqft_where, roll_year),
    }


def _fetch_candidate_parcels(client: PoliteClient, *, use_codes: list[str], year_built_before: int,
                             min_sqft: float | None, roll_year: str = "2025", page_size: int = 2000,
                             max_pages: int = 200) -> list[dict]:
    """Every parcel surviving the full filter chain (use code + built-before
    + sqft floor), county-wide -- the raw universe find_replacement_candidates()
    checks permit absence against."""
    where = _candidate_where(use_codes, year_built_before, min_sqft, roll_year)
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


def estimate_tonnage(cfg: Config, use_desc: str | None, sqft: float | None) -> tuple[float | None, float | None, str | None]:
    """Rough (low, high, basis) tonnage band from square footage alone --
    NEVER permit-verified, so this must never be stored or displayed next to
    mined_tons_each as if it were the same kind of number. Only defined for
    use codes with a configured sqft/ton band (config.yaml
    retrofit.candidate_sqft_per_ton); anything else (missing use_desc, or a
    use code not in that table) returns (None, None, None) -- a category not
    covered by the ballpark table doesn't get a made-up one applied to it."""
    if not sqft or sqft <= 0 or not use_desc:
        return None, None, None
    bands = cfg.get("retrofit.candidate_sqft_per_ton", {}) or {}
    band = bands.get(use_desc)
    if not band:
        return None, None, None
    low = round(sqft / band["high"])
    high = round(sqft / band["low"])
    basis = (f"ESTIMATED from {sqft:,.0f} sqft @ {band['low']}-{band['high']} sqft/ton "
            f"industry rule of thumb for {use_desc.lower()} space -- NOT mined from a permit, "
            f"no equipment on file to measure; wide range on purpose, do not quote a point figure")
    return float(low), float(high), basis


def find_replacement_candidates(session, cfg: Config, client: PoliteClient, *,
                                year_built_before: int = 2010,
                                use_codes: list[str] | None = None,
                                min_sqft: float | None = None) -> dict:
    """The ABSENCE query: commercial/industrial parcels built before
    `year_built_before`, at or above `min_sqft`, with NO mechanical permit
    on record at all (across every EquipmentPermit row this system has,
    which after `scout fetch-permits --window all` spans 2010-present).

    Presence of a permit is evidence someone already replaced the equipment.
    Its absence, on a building old enough that a real replacement would
    almost certainly have needed one, means either the original equipment
    is still running or it was replaced without a permit -- either way it's
    a live candidate no permit-presence ranking can ever surface, because a
    building on this list has never (in this system's window) generated a
    permit to rank BY.

    That absence is an INFERENCE, not proof, in both directions:
      - false positive: a like-for-like swap that never pulled a permit (or
        pulled one under a mismatched/typo'd APN this system's address join
        missed) reads as "original equipment" when it was actually replaced.
      - false negative (rarer, not this population's failure mode): a
        permit on file for a cosmetic repair, not a full replacement, would
        wrongly exclude a building that still needs one.
    The expected skew is toward false positives -- unpermitted like-for-like
    swaps are common in this trade, and this system has no way to see them.
    Treat this list as upper-bound opportunity, not a confirmed one; every
    row and the board itself say so.

    Measured 2026-08-09: 27 percent of ALL permits (4,631 of 17,010) carry
    a privacy-masked or missing APN and are invisible to the APN-exact
    exclusion above -- a hand audit of the (then-)top 50 found a real hit
    this way (a permitted building excluded from the exclusion, not the
    opportunity list). Every such permit's address IS usable, just not its
    APN, so those permits get a second, address-normalized exclusion pass
    (normalize_address()) -- exact-match only, not fuzzy: an automated
    exclusion needs to be a deterministic join, same reasoning
    docs/CHARTER.md gives structured Pipeline B for not using an LLM where
    a join will do. Fuzzy/partial address similarity stays a human-review
    tool (see the hand-verification workflow), not something that silently
    drops a row from the opportunity list on a guess.

    No equipment type, no permit-verified tonnage, no permit-sourced SB 1206
    read: none of that can be known without a permit's work-description
    text. service_life_status IS computed here (YearBuilt as an install-year
    proxy under the default private_commercial ownership, generic across
    equipment type -- see app/replacement.py:generic_service_life), and
    every such row's basis is prefixed YEARBUILT-DERIVED so it can never be
    mistaken for the permit-verified figure the recently_active population
    carries. CARB/EBEWE candidacy is also evaluated -- both come from
    assessor use code and size alone, independent of any permit, so absence
    of a permit doesn't block them the way it blocks SB 1206 (which needs an
    inferred refrigerant, which needs an install year inside R-410A's
    window -- never true for a pre-2010-built proxy, so it's always None
    here, correctly).

    2026-08-11: service_life_status/service_life_years_past ABSTAIN (both
    null) rather than compute when YearBuilt precedes
    PERMIT_OBSERVATION_START_YEAR by more than two average service cycles.
    "No permit on record" is a 16-year observation window, not evidence
    nothing happened -- for a building old enough to have plausibly cycled
    through its equipment multiple times unobserved, treating that silence
    as "maximally overdue" fabricates urgency the same way an unparseable
    field silently voting instead of abstaining would. These rows still get
    ranked -- rank_buildings already treats a null status/years_past as "no
    tier, no magnitude", so they fall out of the urgency ranking and sort on
    size and use code alone, same as any other abstained field elsewhere in
    this system.
    """
    use_codes = use_codes if use_codes is not None else (cfg.get("retrofit.candidate_use_codes") or ["Commercial"])
    min_sqft = min_sqft if min_sqft is not None else cfg.get("retrofit.candidate_min_sqft")

    funnel = funnel_counts(client, use_codes=use_codes, year_built_before=year_built_before, min_sqft=min_sqft)
    parcels = _fetch_candidate_parcels(client, use_codes=use_codes, year_built_before=year_built_before,
                                       min_sqft=min_sqft)
    all_permits = session.exec(select(EquipmentPermit.apn, EquipmentPermit.address)).all()
    permitted_apns = {apn for apn, _addr in all_permits if apn and "*" not in apn}
    # Permits whose APN can't be used for the join at all (masked or
    # missing) -- their address is the only usable evidence, so it gets its
    # own exclusion set. See the ABSENCE-query docstring above.
    unmatched_apn_addresses = {
        norm for apn, addr in all_permits
        if not apn or "*" in apn
        for norm in [normalize_address(addr)] if norm
    }

    ains = [a["AIN"] for a in parcels if a.get("AIN")]
    carb_by_ain: dict[str, str] = {}
    ebewe_ains: set[str] = set()
    for c in session.exec(
            select(AssessorCandidate).where(AssessorCandidate.ain.in_(ains))).all() if ains else []:
        if c.trigger_key == "carb_refrigerant_management_program":
            carb_by_ain[c.ain] = c.use_code
        elif c.trigger_key == "la_ebewe_audit_retrocommissioning":
            ebewe_ains.add(c.ain)

    service_freq = latest_service_frequency_by_apn(session, ains)

    now = utcnow()
    session.exec(delete(RetrofitBuilding).where(RetrofitBuilding.population == "replacement_candidate"))

    candidates = 0
    apn_excluded = 0
    address_excluded = 0
    service_life_abstained = 0
    for attrs in parcels:
        ain = attrs.get("AIN")
        if not ain:
            continue
        if ain in permitted_apns:
            apn_excluded += 1
            continue
        if normalize_address(attrs.get("PropertyLocation")) in unmatched_apn_addresses:
            address_excluded += 1
            continue
        year_built = int(attrs["YearBuilt"]) if str(attrs.get("YearBuilt", "")).isdigit() else None
        age = round(now.year - year_built, 1) if year_built else None
        sqft = attrs.get("SQFTmain")
        use_desc = attrs.get("UseCodeDescChar1")

        sl_status, sl_basis, years_past = None, None, None
        if age is not None:
            gsl = generic_service_life(cfg, ownership=None)  # None -> default_ownership
            # Two average service cycles: "more than two equipment lifetimes
            # could have turned over, unobserved" -- 2 x the midpoint of
            # low/high, which is exactly low+high. Below this, "no permit on
            # record since 2010" is still informative (a plausible original-
            # equipment-still-there story survives the unobserved gap).
            # Above it, absence stops being evidence of anything: the
            # building could be on original equipment or its sixth
            # replacement, and this system cannot tell the difference --
            # see PERMIT_OBSERVATION_START_YEAR above and the charter rule
            # that an unknowable field abstains rather than votes. Silently
            # scoring these as maximally overdue was the same rule broken in
            # reverse: a missing field voting maximally FOR, not abstaining.
            service_cycle_years_x2 = gsl.low + gsl.high
            unobserved_years = PERMIT_OBSERVATION_START_YEAR - year_built if year_built else None
            if unobserved_years is not None and unobserved_years > service_cycle_years_x2:
                service_life_abstained += 1
                sl_basis = (
                    f"ABSTAINED: built {year_built}, {unobserved_years}yr before permit records "
                    f"begin ({PERMIT_OBSERVATION_START_YEAR}) -- more than two average service "
                    f"cycles ({service_cycle_years_x2}yr = 2 x ~{round(service_cycle_years_x2 / 2)}yr "
                    f"under {gsl.ownership} ownership) unobserved. Absence of a permit here is not "
                    f"evidence of anything: the building could be on its original equipment or its "
                    f"sixth replacement, and this system cannot tell the difference. "
                    f"service_life_status and service_life_years_past are null on purpose -- this "
                    f"row ranks on size and use code alone, not service-life urgency."
                )
                # sl_status and years_past stay None -- rank_buildings already
                # treats both as "abstain, don't vote" (life_tier falls to -1
                # via its status.get(..., -1) default, magnitude_factor stays
                # 0.0), so no change to rank_buildings itself was needed.
            else:
                sl_status = gsl.status(age)
                years_past = round(age - gsl.low, 1)
                sl_basis = (
                    f"YEARBUILT-DERIVED (not permit-verified): {age:.0f}yr since built "
                    f"({year_built}); no mechanical permit on record since 2010, so actual "
                    f"install year and equipment type are unknown -- age assumes original "
                    f"equipment or an unpermitted like-for-like swap. Composite expected life "
                    f"{gsl.low}-{gsl.high}yr under {gsl.ownership} ownership (default -- no "
                    f"owner data available), averaged across equipment types since none is "
                    f"confirmed [{'VERIFIED' if gsl.verified else 'UNVERIFIED'}: {gsl.source}]. "
                    f"-> {sl_status.replace('_', ' ')}"
                )

        # SB 1206 needs an inferred refrigerant, which needs an install year
        # inside R-410A's 2010-2024 window (see app/pipeline/regulatory.py).
        # year_built_before caps this population below 2010, so it never
        # applies here -- not called, to avoid implying it was checked.
        carb_use_code = carb_by_ain.get(ain)
        tons_low, tons_high, tons_basis = estimate_tonnage(cfg, use_desc, sqft)
        freq = service_freq.get(ain)

        rank = rank_buildings(
            service_life_status=sl_status, sqft=sqft,
            sb1206_trigger_status=None,
            ebewe_candidate=ain in ebewe_ains, carb_candidate=carb_use_code is not None,
            service_life_years_past=years_past,
            service_calls_per_year=freq.service_calls_per_year if freq else None,
        )

        session.add(RetrofitBuilding(
            apn=ain, population="replacement_candidate",
            address=attrs.get("PropertyLocation"),
            use_code=attrs.get("UseCode"), use_desc=use_desc,
            sqft=sqft, year_built=year_built, building_age_years=age,
            permit_count=0,
            carb_candidate=carb_use_code is not None, carb_use_code=carb_use_code,
            ebewe_candidate=ain in ebewe_ains,
            service_life_status=sl_status, service_life_basis=sl_basis,
            service_life_years_past=years_past,
            equipment_age_years=age,
            estimated_tons_low=tons_low, estimated_tons_high=tons_high, estimated_tons_basis=tons_basis,
            rank_score=rank,
            service_calls_per_year=freq.service_calls_per_year if freq else None,
            service_calls_per_year_source=freq.source if freq else None,
            service_calls_per_year_reported_at=freq.reported_at if freq else None,
            permit_source_url=PERMITS_PORTAL_URL, assessor_source_url=ASSESSOR_PORTAL_URL,
            built_at=now,
        ))
        candidates += 1

    session.commit()
    return {
        "use_code_match": funnel["use_code_match"],
        "built_before": funnel["built_before"],
        "sqft_floor_survivors": funnel["sqft_floor_survivors"],
        "commercial_parcels_scanned": len(parcels),
        "already_permitted_excluded": apn_excluded,
        "masked_or_null_apn_address_excluded": address_excluded,
        "replacement_candidates": candidates,
        "use_codes": use_codes, "min_sqft": min_sqft, "year_built_before": year_built_before,
        "service_frequency_reports_applied": len(service_freq),
        "service_life_abstained": service_life_abstained,
    }


def service_calls_coverage(session) -> dict:
    """How many RetrofitBuilding rows currently carry a reported
    service_calls_per_year, against the total -- "so I know when the sample
    is too small to mean anything." Read at call time from the live board,
    not cached, so it can never drift from what's actually populated."""
    total = len(session.exec(select(RetrofitBuilding.id)).all())
    reported = len(session.exec(
        select(RetrofitBuilding.id).where(RetrofitBuilding.service_calls_per_year.is_not(None))).all())
    distinct_apns_reported = len(session.exec(select(ServiceFrequencyReport.apn).distinct()).all())
    return {
        "retrofit_buildings_total": total,
        "retrofit_buildings_with_service_calls": reported,
        "distinct_apns_with_a_report": distinct_apns_reported,
    }
