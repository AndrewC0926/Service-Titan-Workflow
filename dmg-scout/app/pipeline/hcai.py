"""HCAI healthcare data imports: a county-level AGGREGATE construction-
activity layer (Phase 6g, same shape as app/pipeline/iepr.py) plus a
per-building SB 1953 seismic-compliance layer (HospitalBuilding).

The plan-review/permit-stage workflow tracking before construction starts
(HCAI's real project pipeline) lives behind a login-gated eServices portal
(esp.hcai.ca.gov) — confirmed not publicly reachable without an account, and
that part of the original claim here stands. What does NOT hold: HCAI also
publishes a genuinely public, facility-NAMED per-building dataset on the
CHHS Open Data Portal — "Seismic Ratings and Collapse Probabilities of
California Hospitals" (SPC/NPC ratings, HAZUS collapse probability,
facility/building name, lat/lon) — confirmed 2026-08-17 by downloading and
reading the actual CSV, not by re-reading this module's own prior claim.
See HospitalBuilding's docstring and import_hcai_seismic_ratings below.

Both datasets share the same manual-import discipline as IEPR, for the same
mechanical reason: each CSV's own download URL embeds a generation date that
changes with every CHHS update (construction-cost: ~biweekly; seismic
ratings: irregular, filename-dated), and the JSON API path that would let
code discover the CURRENT filename automatically is robots.txt-disallowed on
data.chhs.ca.gov (/api/ and /datastore/* are both blocked for all crawlers).
There is no compliant way to auto-resolve "the latest file."

CHHS's Terms of Use (data.chhs.ca.gov/pages/terms, read directly 2026-08-17;
see the assumptions register's "Hospital seismic compliance" group for the
full finding) additionally raises the bar above robots.txt alone: its
"Public Participation" section states "You must not create accounts with
the CalHHS Open Data Portal or access the site through unauthorized means,
including but not limited to, by using an automated device, script, bot,
spider, crawler or scraper." That clause is textually scoped to the
portal's participatory features (accounts, comment forums, uploads), not
plainly to an unauthenticated file download — but manual download avoids
having to rely on that reading at all, which is the same reason IEPR
(app/pipeline/iepr.py) chose manual import before this question ever came
up. Separately, the Terms' "Intellectual Property" section requires
attribution (credit to the CalHHS department/office plus a citation to the
source webpage and publication date) on any reuse/redistribution, and
explicitly bars presenting a MODIFIED figure as "official government data"
without saying so — see spc_deadline_year/npc_deadline_year below, which
are this application's own derived reading of HCAI's published SPC/NPC
codes, never an HCAI-published field itself.

Download by hand:
- Construction activity: https://data.chhs.ca.gov/dataset/total-construction-cost-of-healthcare-projects
  → `scout import-hcai <path>`
- Seismic ratings: https://data.chhs.ca.gov/dataset/seismic-ratings-and-collapse-probabilities-of-california-hospitals
  → `scout import-hcai-seismic <path>`
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime

from sqlmodel import delete, select

from app.models import HcaiCountyActivity, HospitalBuilding, SourceRun, utcnow

log = logging.getLogger(__name__)

SEISMIC_SOURCE = "hcai_seismic_ratings"

# "01 - Alameda" -> "Alameda"
_COUNTY_PREFIX = re.compile(r"^\d+\s*-\s*")


def _clean_county(raw: str) -> str | None:
    if not raw:
        return None
    return _COUNTY_PREFIX.sub("", raw).strip() or None


def _parse_money(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def import_hcai_snapshot(session, path_or_text: str, *, source_url: str, is_path: bool = True) -> dict:
    """Import a downloaded CHHS 'Total Construction Cost of Healthcare
    Projects' CSV. The file is a historical log going back to 2013 (~330
    biweekly snapshots) — this keeps only the MOST RECENT snapshot date, on
    the same logic as IEPR: this represents current known state, not an
    accumulating time series. Replaces every prior row on each import."""
    text = open(path_or_text).read() if is_path else path_or_text
    reader = csv.DictReader(text.splitlines())

    rows = list(reader)
    if not rows:
        raise ValueError("CSV had no data rows")

    dates: dict[str, datetime] = {}
    max_date = None
    for row in rows:
        raw_date = row.get("Data Generation Date", "").strip()
        if raw_date not in dates:
            try:
                dates[raw_date] = datetime.strptime(raw_date, "%m/%d/%Y")
            except ValueError:
                continue
        parsed = dates[raw_date]
        if max_date is None or parsed > max_date:
            max_date = parsed
    if max_date is None:
        raise ValueError("no parseable 'Data Generation Date' values found")

    latest_str = next(d for d, parsed in dates.items() if parsed == max_date)

    session.exec(delete(HcaiCountyActivity))

    stored, skipped_counties = 0, set()
    for row in rows:
        if row.get("Data Generation Date", "").strip() != latest_str:
            continue
        county = _clean_county(row.get("County", ""))
        status = row.get("OSHPD Project Status", "").strip()
        if not county or not status:
            skipped_counties.add(row.get("County", "<blank>"))
            continue
        session.add(HcaiCountyActivity(
            county=county, status=status,
            total_cost=_parse_money(row.get("Total Costs of OSHPD Projects", "")),
            project_count=int(row["Number of OSHPD Projects"]) if row.get("Number of OSHPD Projects", "").strip().isdigit() else None,
            snapshot_date=max_date, source_url=source_url, imported_at=utcnow(),
        ))
        stored += 1

    session.flush()
    return {
        "snapshot_date": max_date.date().isoformat(),
        "rows_in_file": len(rows),
        "rows_stored": stored,
        "skipped": sorted(skipped_counties),
    }


# ---- SB 1953 seismic deadline schedule ------------------------------------
#
# Health & Safety Code §130000 et seq. (the Alfred E. Alquist Hospital
# Facilities Seismic Safety Act). Confirmed directly against HCAI's own
# published pages 2026-08-17 (hcai.ca.gov/facilities/building-safety/
# seismic-compliance-and-safety/, program-overview/ and seismic-performance-
# ratings/ subpages) -- see the assumptions register's "Hospital seismic
# compliance" group for the exact quoted findings. These are the BASE
# statutory dates only; an individual building may hold an HCAI-granted
# extension pushing its real deadline later -- see has_filed_extension on
# HospitalBuilding and this module's own docstring for why this deliberately
# does not try to compute an extended date.
SPC1_DEADLINE_YEAR = 2020   # SPC-1 buildings: removed from general acute care service by 1/1/2020
SPC2_DEADLINE_YEAR = 2030   # SPC-2 buildings: removed from general acute care service by 1/1/2030
NPC5_DEADLINE_YEAR = 2030   # same statutory date -- the 2030 standard also requires NPC 5, not SPC alone

_SPC_LEVEL_RE = re.compile(r"^(\d+)")  # leading digit only -- "2s" -> 2, "4D" -> 4; see HospitalBuilding docstring


def _spc_level(raw: str | None) -> int | None:
    if not raw:
        return None
    m = _SPC_LEVEL_RE.match(raw.strip())
    return int(m.group(1)) if m else None


def spc_deadline_year(raw_spc: str | None) -> int | None:
    """None for SPC>=3 (no removal-from-service deadline), N/A, NYA, or
    unrated -- a missing deadline here is "nothing to derive", never "no
    deadline exists". See module docstring for the SPC1/SPC2 dates."""
    level = _spc_level(raw_spc)
    if level == 1:
        return SPC1_DEADLINE_YEAR
    if level == 2:
        return SPC2_DEADLINE_YEAR
    return None


def npc_deadline_year(raw_npc: str | None) -> int | None:
    """HCAI's own 'meets the 2030 standard' bar requires NPC 5 specifically
    -- any building not already AT npc=5 (including N/A-adjacent partial
    ratings like '3R' or '4D-L1') has a live 2030 deadline for its
    nonstructural systems. N/A/NYA/blank -- nothing to derive."""
    if not raw_npc:
        return None
    v = raw_npc.strip()
    if v in ("N/A", "NYA"):
        return None
    return None if v == "5" else NPC5_DEADLINE_YEAR


def meets_2030_standard(raw_spc: str | None, raw_npc: str | None) -> bool | None:
    """None (not False) when either rating is unrated/N/A/NYA -- an unrated
    building's compliance is unknown, not non-compliant."""
    level = _spc_level(raw_spc)
    npc = (raw_npc or "").strip()
    if level is None or not npc or npc in ("N/A", "NYA"):
        return None
    return level >= 3 and npc == "5"


# ---- per-building seismic ratings import -----------------------------

def _read_text(path: str) -> str:
    """CHHS's export encodes smart quotes/em-dashes outside plain ASCII --
    measured 2026-08-17, the seismic-ratings CSV is cp1252, not utf-8
    (confirmed: a bare utf-8 read raises UnicodeDecodeError on byte 0x96 in
    the AB 1882 Notice column). Try utf-8 first (what the construction-cost
    CSV actually is) so this doesn't silently mis-decode a file that IS
    clean utf-8, and fall back to cp1252 only on failure."""
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def _parse_float(raw: str) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _row_get(row: dict, *names: str) -> str:
    """HCAI's own CSV header has at least one trailing-space column name
    ('SPC Rating ') -- match on the exact name first, then fall back to a
    whitespace-stripped comparison so a header-formatting change in a future
    export doesn't silently drop a whole column."""
    for name in names:
        if name in row:
            return row[name]
    stripped = {k.strip(): v for k, v in row.items()}
    for name in names:
        if name in stripped:
            return stripped[name]
    return ""


def import_hcai_seismic_ratings(session, path_or_text: str, *, source_url: str, is_path: bool = True,
                                extensions_path: str | None = None) -> dict:
    """Import a downloaded CHHS 'Seismic Ratings and Collapse Probabilities
    of California Hospitals' CSV -- one row per seismically-separate
    building. Replaces every prior row on each import, same discipline as
    import_hcai_snapshot: this is current known state, not an accumulating
    time series.

    extensions_path, if given, is a separately-downloaded copy of the
    companion 'Seismic Deadline Extensions Granted for California Hospitals'
    CSV, used ONLY to set has_filed_extension -- a coarse yes/no, never a
    computed extended deadline date. See HospitalBuilding's docstring for
    why.

    Records its own SourceRun (source=SEISMIC_SOURCE) so `scout doctor` /
    source_health surfaces a stale or failed import the same way it would a
    failed automated fetch -- see config.yaml's sources.hcai_seismic_ratings
    entry (stale_hours set for a manual, not-daily cadence) and
    app.ops.doctor's per-source stale_hours override."""
    run = SourceRun(source=SEISMIC_SOURCE)
    session.add(run)
    session.commit()

    stored, skipped, error = 0, 0, None
    try:
        text = _read_text(path_or_text) if is_path else path_or_text
        rows = list(csv.DictReader(text.splitlines()))
        if not rows:
            raise ValueError("CSV had no data rows")

        # This dataset has no per-row 'Data Generation Date' column (unlike
        # the construction-cost CSV) -- the generation date lives only in
        # the download filename. snapshot_date is therefore "as of when
        # this was imported", an honest claim about Scout's own data, not a
        # claim about when HCAI itself last regenerated the file.
        snapshot = utcnow()

        extended_keys: set[tuple[str, str]] = set()
        if extensions_path:
            ext_text = _read_text(extensions_path)
            ext_rows = list(csv.DictReader(ext_text.splitlines()))
            ext_cols = [c for c in (ext_rows[0].keys() if ext_rows else []) if "Extension" in c]
            for row in ext_rows:
                key = (_row_get(row, "Perm ID").strip(), _row_get(row, "Building Nbr").strip())
                if any(row.get(c, "").strip() for c in ext_cols):
                    extended_keys.add(key)

        session.exec(delete(HospitalBuilding))

        for row in rows:
            perm_id = _row_get(row, "Perm ID").strip()
            building_nbr = _row_get(row, "Building Nbr").strip()
            county = _clean_county(_row_get(row, "County Code"))
            facility_name = _row_get(row, "Facility Name").strip()
            if not perm_id or not building_nbr or not county or not facility_name:
                skipped += 1
                continue
            spc_raw = _row_get(row, "SPC Rating", "SPC Rating ").strip() or None
            npc_raw = _row_get(row, "HCAI NPC Rating").strip() or None
            session.add(HospitalBuilding(
                perm_id=perm_id, building_nbr=building_nbr, facility_name=facility_name,
                building_name=_row_get(row, "Building Name").strip() or None,
                building_status=_row_get(row, "Building Status").strip() or None,
                city=_row_get(row, "City").strip() or None,
                county=county, state="CA",
                spc_rating=spc_raw, npc_rating=npc_raw,
                hazus_2010_pct=_parse_float(_row_get(row, "2010 Hazus Score (%)")),
                ab1882_notice=_row_get(row, "AB 1882 Notice").strip() or None,
                latitude=_parse_float(_row_get(row, "Latitude")),
                longitude=_parse_float(_row_get(row, "Longitude")),
                spc_deadline_year=spc_deadline_year(spc_raw),
                npc_deadline_year=npc_deadline_year(npc_raw),
                meets_2030_standard=meets_2030_standard(spc_raw, npc_raw),
                has_filed_extension=(perm_id, building_nbr) in extended_keys,
                snapshot_date=snapshot, source_url=source_url, imported_at=snapshot,
            ))
            stored += 1

        session.commit()
    except Exception as exc:  # noqa: BLE001 — recorded on the SourceRun, not raised past this stage
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        log.error("HCAI seismic ratings import failed: %s", error)
        rows, extended_keys, snapshot = [], set(), utcnow()

    run.finished_at = utcnow()
    run.records_fetched = len(rows)
    run.records_new = stored
    run.ok = error is None
    run.error = error
    session.add(run)
    session.commit()

    if error:
        raise RuntimeError(error)
    return {
        "snapshot_date": snapshot.date().isoformat(),
        "rows_in_file": len(rows),
        "rows_stored": stored,
        "skipped": skipped,
        "extensions_matched": len(extended_keys),
    }


# ---- DMG's own OSP capability gap, live -----------------------------------

def hospital_capability_gaps(session) -> dict:
    """Which of DMG's 13 building-systems roles (app.accounts.ROLE_ORDER)
    can currently field a hospital-grade (HCAI OSP-preapproved) package,
    computed LIVE from ProductLine.oshpd_osp -- never hardcoded, so this
    self-corrects the moment a line wins a new OSP instead of silently
    going stale on a hospital board people read to decide whether to call.

    A role is "covered" here ONLY if at least one of its lines holds a
    CONFIRMED current OSP (oshpd_osp is True) -- not merely if the role has
    lines on the card at all. That distinction is the entire point of this
    function: app.accounts.line_offering_by_role's own RoleOffering.gap
    (the general-purpose "does a project's line card have a gap" flag used
    for early-signal Projects) only asks "does a line exist in this role,"
    which is a materially different, weaker question -- fine for a
    data_center/industrial/esco project, since Scout doesn't track
    healthcare projects at all and nothing there is ever HCAI-governed
    (see ROLE_PROJECT_RELEVANCE's own scoping comment), but exactly the gap
    that would mislead someone reading THIS board, where every building is
    HCAI-governed by definition. "A line exists" and "a line is legally
    preapproved to go into a hospital" are not the same fact, confirmed by
    the chillers/fans research below: DMG carries 4 chiller lines and ~14
    fan lines -- "role has lines" is True for both -- and zero of either
    hold a confirmed current OSP.

    Chillers are checked by CATEGORY ("chillers_cooling"), deliberately NOT
    by the broader "cooling_generation" building_role: that role also
    contains category=vrf_split (fan-coil/PTAC/VRF terminal equipment,
    e.g. Nailor's "Engineered Comfort" brand, which DOES hold a current OSP
    for its fan coil units) -- rolling that up as "chillers covered" would
    be exactly the false-precision this system exists to avoid. Fans use
    app.accounts.resolve_building_role's own category+override logic
    (fans_exhaust/process_exhaust/residential_light_commercial categories
    plus the MacroAir/Berner role overrides) rather than the stored
    ProductLine.building_role, so a role override applies here even if a
    row's own stored value hasn't been re-seeded since -- see
    test_hospital_capability_gaps_fans_use_role_resolution. The other 11
    roles use ProductLine.building_role directly; if one of them turns out
    to share cooling_generation's or fans_ventilation's need for a
    recompute, special-case it here the same way, not by widening this
    comment's claim without evidence.

    Measured 2026-08-17 (chillers/fans) and 2026-08-19 (all 13 roles):
    9 of 13 roles have ZERO lines with a confirmed current OSP -- only
    air_handling, air_distribution_terminal, controls_valves, and
    humidification clear the bar. Most of the other 9 are unresearched
    (oshpd_osp=None: absence of evidence, not confirmed absence), not
    confirmed-absent outright -- role_gaps below carries checked/
    confirmed_expired so that distinction stays visible, the same
    discipline chillers/fans already applied. See config.yaml's per-line
    oshpd_osp_basis entries for the individual research trail behind every
    figure here."""
    from app.accounts import ROLE_LABELS, ROLE_ORDER, resolve_building_role
    from app.models import ProductLine

    lines = session.exec(select(ProductLine)).all()
    by_role: dict[str, list] = {}
    for l in lines:
        by_role.setdefault(l.building_role, []).append(l)

    role_gaps = []
    for role in ROLE_ORDER:
        if role == "cooling_generation":
            role_lines = [l for l in lines if l.category == "chillers_cooling"]
        elif role == "fans_ventilation":
            role_lines = [l for l in lines if resolve_building_role(l.name, l.category) == "fans_ventilation"]
        else:
            role_lines = by_role.get(role, [])
        covered = any(l.oshpd_osp is True for l in role_lines)
        role_gaps.append({
            "role": role, "label": ROLE_LABELS[role],
            "covered": covered, "gap": not covered,
            "checked": len(role_lines),
            "confirmed_expired": sum(1 for l in role_lines if l.oshpd_osp is False),
            "unresearched": sum(1 for l in role_lines if l.oshpd_osp is None),
        })
    by_role_key = {rg["role"]: rg for rg in role_gaps}

    return {
        "role_gaps": role_gaps,
        "roles_with_gap": sum(1 for rg in role_gaps if rg["gap"]),
        "roles_checked": len(role_gaps),
        "chillers_covered": by_role_key["cooling_generation"]["covered"],
        "chillers_checked": by_role_key["cooling_generation"]["checked"],
        "chillers_confirmed_expired": by_role_key["cooling_generation"]["confirmed_expired"],
        "fans_covered": by_role_key["fans_ventilation"]["covered"],
        "fans_checked": by_role_key["fans_ventilation"]["checked"],
        "fans_confirmed_expired": by_role_key["fans_ventilation"]["confirmed_expired"],
    }


def hospital_seismic_coverage(session, cfg) -> dict:
    """Live counts for the assumptions register and the hospital board's own
    summary line: how many imported HospitalBuilding rows fall inside
    Scout's California territory counties (config.yaml's
    territories.california.counties -- HCAI's own dataset covers all 58 CA
    counties statewide, most of which are out of scope), and how many carry
    each base statutory deadline."""
    territory_counties = set(cfg.get("territories.california.counties", []))
    buildings = session.exec(select(HospitalBuilding)).all()
    in_territory = [b for b in buildings if b.county in territory_counties]
    return {
        "total_buildings": len(buildings),
        "in_territory": len(in_territory),
        "spc_2020_deadline": sum(1 for b in in_territory if b.spc_deadline_year == SPC1_DEADLINE_YEAR),
        "spc_2030_deadline": sum(1 for b in in_territory if b.spc_deadline_year == SPC2_DEADLINE_YEAR),
        "npc_2030_deadline": sum(1 for b in in_territory if b.npc_deadline_year == NPC5_DEADLINE_YEAR),
        "meets_2030_standard": sum(1 for b in in_territory if b.meets_2030_standard is True),
        "has_filed_extension": sum(1 for b in in_territory if b.has_filed_extension),
    }


def hospital_contractor_reachability(session, cfg) -> dict:
    """Of the in-territory HospitalBuilding rows with a live NPC-2030
    deadline, how many sit within radius_miles of AT LEAST ONE CSLB
    mechanical (C-20/C-38) contractor, and separately of at least one UA
    Local 250 signatory contractor. Existence checks only -- this is NOT
    nearest_mechanical_contractor_bulk (app/contractors.py), which finds
    the single nearest contractor per building for a tel: link; this just
    answers "is anyone plausibly reachable at all", the input this brief
    needs.

    radius_miles defaults to app.contractors.default_radius_miles (15mi) --
    reused deliberately, not a new number invented for this report: it is
    the SAME radius the app already treats as "realistically reachable"
    for a contractor dispatch list (app.contractors.nearest_mechanical_contractors,
    the retrofit building detail page). Neither count is evidence of actual
    hospital/OSHPD experience -- CSLB licensing and UA Local 250 signatory
    status are both proxies (general mechanical licensure, and a prevailing-
    wage/institutional-labor signal respectively), not a claim that a given
    contractor has ever worked on a hospital."""
    import math

    from app.contractors import (
        MILES_PER_DEGREE_LAT,
        _is_mechanical_classification,
        default_radius_miles,
        haversine_miles,
    )
    from app.models import Contractor

    radius_miles = default_radius_miles(cfg)
    territory_counties = set(cfg.get("territories.california.counties", []))

    buildings = session.exec(
        select(HospitalBuilding).where(
            HospitalBuilding.npc_deadline_year == NPC5_DEADLINE_YEAR,
            HospitalBuilding.county.in_(territory_counties),
        )
    ).all()
    geocoded = [b for b in buildings if b.latitude is not None and b.longitude is not None]

    if not geocoded:
        return {"radius_miles": radius_miles, "npc_2030_total": len(buildings),
                "npc_2030_geocoded": 0, "near_mechanical_contractor": 0,
                "near_mechanical_contractor_pct": 0.0,
                "near_ua_local_250_signatory": 0, "near_ua_local_250_signatory_pct": 0.0}

    # One bounding box covering every target building at once (same
    # discipline as app.contractors.nearest_mechanical_contractor_bulk) --
    # a per-building query would be 1,790 separate round trips.
    lats = [b.latitude for b in geocoded]
    lons = [b.longitude for b in geocoded]
    lat_pad = radius_miles / MILES_PER_DEGREE_LAT
    lon_pad = radius_miles / (MILES_PER_DEGREE_LAT * max(math.cos(math.radians(sum(lats) / len(lats))), 0.01))
    lat_min, lat_max = min(lats) - lat_pad, max(lats) + lat_pad
    lon_min, lon_max = min(lons) - lon_pad, max(lons) + lon_pad

    candidates = session.exec(
        select(Contractor.latitude, Contractor.longitude, Contractor.classifications,
              Contractor.ua_local_250_signatory)
        .where(Contractor.latitude.is_not(None),
              Contractor.latitude.between(lat_min, lat_max),
              Contractor.longitude.between(lon_min, lon_max))
    ).all()
    mechanical = [(lat, lon) for lat, lon, cls, _sig in candidates if _is_mechanical_classification(cls)]
    signatory = [(lat, lon) for lat, lon, _cls, sig in candidates if sig]

    near_mechanical = near_signatory = 0
    for b in geocoded:
        if any(haversine_miles(b.latitude, b.longitude, lat, lon) <= radius_miles for lat, lon in mechanical):
            near_mechanical += 1
        if any(haversine_miles(b.latitude, b.longitude, lat, lon) <= radius_miles for lat, lon in signatory):
            near_signatory += 1

    total = len(geocoded)
    return {
        "radius_miles": radius_miles,
        "npc_2030_total": len(buildings),
        "npc_2030_geocoded": total,
        "near_mechanical_contractor": near_mechanical,
        "near_mechanical_contractor_pct": round(100 * near_mechanical / total, 1) if total else 0.0,
        "near_ua_local_250_signatory": near_signatory,
        "near_ua_local_250_signatory_pct": round(100 * near_signatory / total, 1) if total else 0.0,
    }


# ---- OSP number/expiration for the hospital brief (/hospitals/brief) -----
#
# HAND-TRANSCRIBED from each line's own research (config.yaml's per-line
# oshpd_osp_basis field for the 2026-08-09 round; direct HCAI/live-directory
# checks for the nine fan lines completed 2026-08-19, see below), not
# parsed/regexed from source text -- pattern-extracting a date out of free
# prose is exactly the silent-misextraction risk this system abstains from
# elsewhere (see HospitalBuilding's own has_filed_extension for the same
# discipline applied to a different source). If a line's research is
# updated, this entry must be updated by hand too -- a known coupling, not
# an automatic one. osp_number/expires are None where HCAI's own listing
# does not state one (shown on the brief as "unknown", never computed).
#
# status is one of three DIFFERENT findings that lead to different asks --
# never collapsed into one "gap" bucket:
#   "current"     -- confirmed active OSP on file (also gated live on
#                     ProductLine.oshpd_osp=True in hospital_osp_breakdown,
#                     so this can't drift stale if the DB record changes)
#   "expired"     -- HCAI's OWN RECORD shows a specific preapproval that
#                     lapsed -- the ask is a RENEWAL of an existing filing.
#   "not_listed"  -- searched directly, no HCAI record under any name/
#                     spelling checked, ever (current or historical) -- the
#                     ask is a NEW APPLICATION, or this manufacturer has
#                     simply never pursued HCAI certification. Absence of
#                     evidence, not proof no OSP could exist under an
#                     unchecked name variant.
#
# 2026-08-19 fan research (Berner, Canarm, FanAm, MacroAir, Panasonic,
# Delta Breez, Broan/NuTone, Systemair, Monoxivent -- the nine that were
# previously unresearched): checked directly against (1) HCAI's LIVE OSP
# directory (hcai.ca.gov/facilities/building-safety/preapproval-programs/
# osp/, fetched and full-text-searched directly -- 902 distinct OSP numbers
# as of this check, current/active records only), (2) HCAI's own "OSP by
# Category" historical PDF (hcai.ca.gov/document/osp-list-by-category/,
# fetched and text-extracted directly with pdftotext -- footer-dated
# 03/01/2022 and confirmed this is STILL the version HCAI serves at that
# URL today, so it also captures anything expired/cancelled through that
# date that the live-only directory would have already dropped), covering
# ALL 25 of HCAI's OSP categories, not just the fan-relevant ones, and (3)
# individual web searches per name plus spelling variants (Broan-NuTone,
# DeltaBreez, etc.). Zero matches for all nine, in either source. Scope: no
# newly-issued OSP that was ALSO already expired again within the narrow
# 2022-03 to now window would be visible to either check -- a real but
# narrow gap, disclosed rather than hidden.
#
# Scope: every line in category='chillers_cooling' (4) and every line
# resolving to building_role='fans_ventilation' (14) -- ALL of them, covered
# or not -- plus only the CONFIRMED-CURRENT (oshpd_osp=True) lines in
# air_handling, air_distribution_terminal, and humidification, since those
# roles are reported on the brief only as "what's covered", not
# exhaustively.
HOSPITAL_BRIEF_OSP_FACTS = {
    # ---- chillers (category=chillers_cooling) -- GAP, 0 of 4 confirmed current
    "DB": {"status": "not_listed", "osp_number": None, "expires": None,
          "note": "No HCAI record found under Dunham-Bush in either chiller category, checked 2026-08-09."},
    "ClimaCool": {"status": "expired", "osp_number": "OSP-0048", "expires": "expired after 2019-12-31",
                 "note": "Chillers - Water Cooled. HCAI's own listing shows no valid-through date beyond the expiry -- unlike active peer entries, which show a specific future date. No renewal on file."},
    "Geoclima": {"status": "not_listed", "osp_number": None, "expires": None,
                "note": "No HCAI record found under Geoclima in either chiller category, checked 2026-08-09."},
    "Hecoclima": {"status": "not_listed", "osp_number": None, "expires": None,
                 "note": "No HCAI record found under Hecoclima in either chiller category, checked 2026-08-09."},
    # ---- fans (building_role=fans_ventilation) -- GAP, 0 of 14 confirmed current
    "TCF/Twin City Fan": {"status": "expired", "osp_number": "OSP-0195, -0271, -0355, -0395", "expires": "expired 2022-12-31",
                          "note": "All four filings (Exhaust/Smoke Control Fans + Air Handling Units>Fans) expired the same date. No renewal on file."},
    "Strobic Air": {"status": "not_listed", "osp_number": None, "expires": None,
                    "note": "Searched HCAI's Exhaust/Smoke Control Fans category directly, checked 2026-08-09."},
    "Howden": {"status": "not_listed", "osp_number": None, "expires": None,
              "note": "Searched HCAI's Exhaust/Smoke Control Fans category directly, checked 2026-08-09."},
    "Penn Barry": {"status": "not_listed", "osp_number": None, "expires": None,
                  "note": "Searched HCAI's Exhaust/Smoke Control Fans category directly, checked 2026-08-09."},
    "Soler & Palau": {"status": "not_listed", "osp_number": None, "expires": None,
                      "note": "Searched HCAI's Exhaust/Smoke Control Fans and Inline Fans/Terminal categories directly, checked 2026-08-09."},
    "Berner": {"status": "not_listed", "osp_number": None, "expires": None,
              "note": "Not in HCAI's live OSP directory or the full 25-category historical listing under 'Berner' or any variant checked. Air curtains are not an obvious fit for any existing HCAI category. Checked 2026-08-19."},
    "Canarm": {"status": "not_listed", "osp_number": None, "expires": None,
              "note": "Not in HCAI's live OSP directory or the full 25-category historical listing. Checked 2026-08-19."},
    "FanAm": {"status": "not_listed", "osp_number": None, "expires": None,
             "note": "Not in HCAI's live OSP directory or the full 25-category historical listing. Checked 2026-08-19."},
    "MacroAir": {"status": "not_listed", "osp_number": None, "expires": None,
                "note": "Not in HCAI's live OSP directory or the full 25-category historical listing. HVLS ceiling fans are not an obvious fit for any existing HCAI category (not exhaust/smoke-control, not AHU fans). Checked 2026-08-19."},
    "Panasonic": {"status": "not_listed", "osp_number": None, "expires": None,
                 "note": "No standalone Panasonic OSP found. Panasonic-brand scroll compressors do appear as a qualified SUBCOMPONENT inside at least one other manufacturer's packaged-unit OSP filing -- a different Panasonic product division (refrigeration compressors), not the ventilation fans on DMG's card, and not a Panasonic OSP in its own right. Checked 2026-08-19."},
    "Delta Breez": {"status": "not_listed", "osp_number": None, "expires": None,
                    "note": "Not in HCAI's live OSP directory or the full 25-category historical listing, including the 'DeltaBreez' spelling. Checked 2026-08-19."},
    "Broan NuTone": {"status": "not_listed", "osp_number": None, "expires": None,
                     "note": "Not in HCAI's live OSP directory or the full 25-category historical listing under 'Broan', 'NuTone', or 'Broan-NuTone'. Checked 2026-08-19."},
    "Systemair": {"status": "not_listed", "osp_number": None, "expires": None,
                 "note": "Not in HCAI's live OSP directory or the full 25-category historical listing. Checked 2026-08-19."},
    "Monoxivent": {"status": "not_listed", "osp_number": None, "expires": None,
                  "note": "Not in HCAI's live OSP directory or the full 25-category historical listing. Industrial dust/fume collection is not an obvious fit for any existing HCAI category. Checked 2026-08-19."},
    # ---- covered (confirmed current only)
    "ClimateCraft": {"status": "current", "osp_number": "OSP-0272", "expires": "2029-07-18",
                     "note": "Air Handling Units (FanMatrix fan-tower assembly specifically -- not a blanket AHU preapproval)."},
    "AAON": {"status": "current", "osp_number": "OSP-0180", "expires": "2030-03-20",
            "note": "Air Conditioning Units - Packaged (RQ, RN-A/B/C). A second number, OSP-0181 (Chillers - Condensers), is also listed active but its own expiry was not independently confirmed."},
    "Energy Labs": {"status": "current", "osp_number": "OSP-0069", "expires": None,
                    "note": "Air Conditioning Units - Custom. HCAI's own listing shows no expired/cancelled flag but also states no specific future expiration date."},
    "Titus": {"status": "current", "osp_number": "OSP-0352", "expires": "2032-01-06",
             "note": "Air Conditioning Units - Inline Fan and Terminal (VAV/fan-powered terminal units only, not grilles/diffusers/registers)."},
    "Nailor": {"status": "current", "osp_number": "OSP-0561", "expires": "2029-04-17",
              "note": "Single/Dual Duct and Fan Powered Terminal Units."},
    "Carel": {"status": "current", "osp_number": "OSP-0705", "expires": "2028-08-11",
             "note": "Humidification Systems (HeaterSteam boilers, UltimateSAM distribution grids). WATCH ITEM: nearest of every confirmed-current OSP on this brief to its own expiration -- about 2 years out."},
}


def hospital_osp_breakdown(session) -> dict:
    """Line-by-line OSP status for /hospitals/brief -- chillers and fans in
    full (every line, covered or not), air handling/distribution/terminal/
    humidification limited to the confirmed-current ones (what's covered).
    oshpd_osp itself is read LIVE from ProductLine; osp_number/expires/note
    come from HOSPITAL_BRIEF_OSP_FACTS (hand-transcribed, see that dict's
    own docstring) -- a line with no entry there falls back to a generic
    unknown/not-yet-transcribed row rather than raising, so a future new
    line added to one of these categories doesn't break the brief, it just
    shows up honestly incomplete."""
    from app.accounts import resolve_building_role
    from app.models import ProductLine

    def _fact(name: str) -> dict:
        return HOSPITAL_BRIEF_OSP_FACTS.get(
            name, {"status": "unresearched", "osp_number": None, "expires": None,
                  "note": "not yet transcribed onto this brief"})

    def _row(line) -> dict:
        f = _fact(line.name)
        # oshpd_osp is read LIVE from the DB -- if it now says True and the
        # hand-transcribed fact hasn't caught up yet, live wins (this is
        # what keeps "current" self-correcting). The expired/not_listed/
        # unresearched split only matters when the DB does NOT show a
        # confirmed current OSP, since that finer distinction is not
        # something the DB's 3-valued oshpd_osp column can represent.
        status = "current" if line.oshpd_osp is True else f["status"]
        return {"name": line.name, "firm": line.firm, "status": status,
               "osp_number": f["osp_number"], "expires": f["expires"], "note": f["note"]}

    lines = session.exec(select(ProductLine)).all()
    chillers = sorted((_row(l) for l in lines if l.category == "chillers_cooling"), key=lambda r: r["name"])
    fans = sorted((_row(l) for l in lines if resolve_building_role(l.name, l.category) == "fans_ventilation"),
                 key=lambda r: r["name"])

    covered_names = {"ClimateCraft", "AAON", "Energy Labs", "Titus", "Nailor", "Carel"}
    covered = sorted((_row(l) for l in lines if l.name in covered_names and l.oshpd_osp is True),
                     key=lambda r: r["name"])

    return {"chillers": chillers, "fans": fans, "covered": covered}
