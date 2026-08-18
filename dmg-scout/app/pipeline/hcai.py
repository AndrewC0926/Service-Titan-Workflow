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
    """Which HVAC building-systems roles DMG can currently field a hospital-
    grade (HCAI OSP-preapproved) package for, computed LIVE from
    ProductLine.oshpd_osp -- never hardcoded, so this self-corrects the
    moment a line wins a new OSP instead of silently going stale on a
    hospital board people read to decide whether to call.

    Chillers are checked by CATEGORY ("chillers_cooling"), deliberately NOT
    by the broader "cooling_generation" building_role: that role also
    contains category=vrf_split (fan-coil/PTAC/VRF terminal equipment,
    e.g. Nailor's "Engineered Comfort" brand, which DOES hold a current OSP
    for its fan coil units) -- rolling that up as "chillers covered" would
    be exactly the false-precision this system exists to avoid. Fans use
    app.accounts.resolve_building_role's own category+override logic
    (fans_exhaust/process_exhaust/residential_light_commercial categories
    plus the MacroAir/Berner role overrides), since there's no equivalent
    role-conflation risk on that side.

    Measured 2026-08-17: zero of DMG's 4 true-chiller lines (DB, ClimaCool,
    Geoclima, Hecoclima) and zero of DMG's ~14 fan lines hold a CONFIRMED
    current OSP -- ClimaCool and TCF/Twin City Fan are confirmed EXPIRED,
    the rest unresearched (oshpd_osp=None: absence of evidence, not
    confirmed absence). Air handling, air distribution/terminal, and
    humidification each have at least one confirmed current OSP. See
    config.yaml's per-line oshpd_osp_basis entries for the individual
    research trail behind every figure here."""
    from app.accounts import resolve_building_role
    from app.models import ProductLine

    lines = session.exec(select(ProductLine)).all()
    chillers = [l for l in lines if l.category == "chillers_cooling"]
    fans = [l for l in lines if resolve_building_role(l.name, l.category) == "fans_ventilation"]

    return {
        "chillers_covered": any(l.oshpd_osp is True for l in chillers),
        "chillers_checked": len(chillers),
        "chillers_confirmed_expired": sum(1 for l in chillers if l.oshpd_osp is False),
        "fans_covered": any(l.oshpd_osp is True for l in fans),
        "fans_checked": len(fans),
        "fans_confirmed_expired": sum(1 for l in fans if l.oshpd_osp is False),
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
