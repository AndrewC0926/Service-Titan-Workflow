"""app/pipeline/hcai.py's per-building seismic layer: the SPC/NPC -> deadline
derivation (SB 1953's base statutory schedule), the CSV import (including the
cp1252 encoding fallback and the has_filed_extension join), and the OSP
capability-gap query's category-vs-role distinction (chillers_cooling must
never be conflated with the broader cooling_generation role, which also
contains vrf_split/fan-coil lines that DO hold a current OSP)."""
from __future__ import annotations

import pytest
from sqlmodel import select

from app.models import HospitalBuilding, ProductLine, SourceRun, utcnow
from app.pipeline import hcai


# ---- deadline derivation ---------------------------------------------

def test_spc_deadline_year_spc1_maps_to_2020():
    assert hcai.spc_deadline_year("1") == 2020
    assert hcai.spc_deadline_year("1s") == 2020  # unverified-rating suffix, same level


def test_spc_deadline_year_spc2_maps_to_2030():
    assert hcai.spc_deadline_year("2") == 2030
    assert hcai.spc_deadline_year("2s") == 2030


def test_spc_deadline_year_spc3_and_above_has_no_deadline():
    for raw in ("3", "3s", "4", "4s", "4D", "5", "5s"):
        assert hcai.spc_deadline_year(raw) is None


def test_spc_deadline_year_unrated_is_none_not_zero():
    for raw in ("N/A", "NYA", "", None, "-1"):
        assert hcai.spc_deadline_year(raw) is None


def test_npc_deadline_year_npc5_has_no_deadline():
    assert hcai.npc_deadline_year("5") is None


def test_npc_deadline_year_below_5_is_2030():
    for raw in ("1", "2", "3", "3R", "4", "4D-L1", "4D-L2"):
        assert hcai.npc_deadline_year(raw) == 2030


def test_npc_deadline_year_unrated_is_none():
    assert hcai.npc_deadline_year("N/A") is None
    assert hcai.npc_deadline_year("NYA") is None
    assert hcai.npc_deadline_year(None) is None


def test_meets_2030_standard_true_only_when_spc3plus_and_npc5():
    assert hcai.meets_2030_standard("3", "5") is True
    assert hcai.meets_2030_standard("5s", "5") is True
    assert hcai.meets_2030_standard("2", "5") is False   # SPC too low
    assert hcai.meets_2030_standard("5", "4") is False   # NPC too low
    assert hcai.meets_2030_standard("N/A", "5") is None  # unrated -- unknown, not non-compliant
    assert hcai.meets_2030_standard("5", "NYA") is None


# ---- import ------------------------------------------------------------

_SEISMIC_CSV = (
    "County Code,Perm ID,Facility Name,City,Building Nbr,Building Name,Building Status,"
    "SPC Rating ,2007 Hazus Score (%),2010 Hazus Score (%),HCAI NPC Rating,AB 1882 Notice,"
    "Latitude,Longitude,Count\n"
    '01 - Alameda,11210,Test Hospital,Alameda,BLD-01,Main Tower,OSHPD 1-In Service,1,,27.63,2,'
    '"does not significantly jeopardize life",37.76,-122.25,1\n'
    '19 - Los Angeles,22000,Other Hospital,LA,BLD-02,Wing,OSHPD 1-In Service,5,,0.10,5,,34.05,-118.25,1\n'
    ",,Skipped Row,,,,,,,,,,,,\n"  # missing perm_id/county/facility -- must be skipped, not crash
)

_EXTENSIONS_CSV = (
    "Perm ID,Building Nbr,SPC Extensions: SB 90 Extension,NPC: Extension\n"
    "11210,BLD-01,01/01/2025,\n"
    "22000,BLD-02,,\n"
)


def test_import_hcai_seismic_ratings_parses_and_derives(db_session, tmp_path):
    csv_path = tmp_path / "seismic.csv"
    csv_path.write_text(_SEISMIC_CSV, encoding="utf-8")

    stats = hcai.import_hcai_seismic_ratings(db_session, str(csv_path), source_url="https://example.com")
    db_session.commit()

    assert stats["rows_stored"] == 2
    assert stats["skipped"] == 1

    rows = {b.perm_id: b for b in db_session.exec(select(HospitalBuilding)).all()}
    assert rows["11210"].county == "Alameda"
    assert rows["11210"].spc_deadline_year == 2020
    assert rows["11210"].npc_deadline_year == 2030
    assert rows["22000"].spc_rating == "5"
    assert rows["22000"].meets_2030_standard is True


def test_import_hcai_seismic_ratings_cp1252_fallback(db_session, tmp_path):
    # A right single quotation mark (U+2019, byte 0x92 in cp1252) is invalid
    # utf-8 on its own -- this is the exact failure mode measured against
    # the real HCAI export (AB 1882 Notice column).
    csv_bytes = _SEISMIC_CSV.replace(
        "does not significantly jeopardize life", "doesn’t significantly jeopardize life"
    ).encode("cp1252")
    csv_path = tmp_path / "seismic_cp1252.csv"
    csv_path.write_bytes(csv_bytes)

    stats = hcai.import_hcai_seismic_ratings(db_session, str(csv_path), source_url="https://example.com")
    assert stats["rows_stored"] == 2
    row = db_session.exec(select(HospitalBuilding).where(HospitalBuilding.perm_id == "11210")).one()
    assert "doesn’t" in row.ab1882_notice


def test_import_hcai_seismic_ratings_sets_has_filed_extension(db_session, tmp_path):
    seismic_path = tmp_path / "seismic.csv"
    seismic_path.write_text(_SEISMIC_CSV, encoding="utf-8")
    ext_path = tmp_path / "extensions.csv"
    ext_path.write_text(_EXTENSIONS_CSV, encoding="utf-8")

    stats = hcai.import_hcai_seismic_ratings(
        db_session, str(seismic_path), source_url="https://example.com", extensions_path=str(ext_path))
    assert stats["extensions_matched"] == 1

    rows = {b.perm_id: b for b in db_session.exec(select(HospitalBuilding)).all()}
    assert rows["11210"].has_filed_extension is True    # has a non-blank SB 90 date
    assert rows["22000"].has_filed_extension is False    # both extension columns blank


def test_import_hcai_seismic_ratings_replaces_prior_rows(db_session, tmp_path):
    csv_path = tmp_path / "seismic.csv"
    csv_path.write_text(_SEISMIC_CSV, encoding="utf-8")
    hcai.import_hcai_seismic_ratings(db_session, str(csv_path), source_url="https://example.com")
    hcai.import_hcai_seismic_ratings(db_session, str(csv_path), source_url="https://example.com")
    assert len(db_session.exec(select(HospitalBuilding)).all()) == 2  # not 4 -- replaced, not appended


def test_import_hcai_seismic_ratings_records_source_run_on_success(db_session, tmp_path):
    csv_path = tmp_path / "seismic.csv"
    csv_path.write_text(_SEISMIC_CSV, encoding="utf-8")
    hcai.import_hcai_seismic_ratings(db_session, str(csv_path), source_url="https://example.com")
    run = db_session.exec(select(SourceRun).where(SourceRun.source == hcai.SEISMIC_SOURCE)).one()
    assert run.ok is True
    assert run.records_new == 2


def test_import_hcai_seismic_ratings_records_source_run_on_failure(db_session):
    with pytest.raises(Exception):
        hcai.import_hcai_seismic_ratings(db_session, "/no/such/file.csv", source_url="https://example.com")
    run = db_session.exec(select(SourceRun).where(SourceRun.source == hcai.SEISMIC_SOURCE)).one()
    assert run.ok is False
    assert run.error


# ---- capability gaps -------------------------------------------------

def test_hospital_capability_gaps_true_chillers_not_conflated_with_vrf_split(db_session):
    """The bug this test exists to catch: a vrf_split-category fan-coil line
    with a current OSP must NOT make chillers_covered read True. Only
    category='chillers_cooling' counts as a chiller."""
    db_session.add(ProductLine(name="Real Chiller Co", name_norm="real chiller co", firm="X",
                               category="chillers_cooling", building_role="cooling_generation",
                               oshpd_osp=False))
    db_session.add(ProductLine(name="Fan Coil Brand", name_norm="fan coil brand", firm="Y",
                               category="vrf_split", building_role="cooling_generation",
                               oshpd_osp=True))
    db_session.commit()

    gaps = hcai.hospital_capability_gaps(db_session)
    assert gaps["chillers_checked"] == 1          # only the true chiller line counted
    assert gaps["chillers_covered"] is False       # the vrf_split OSP must not count


def test_hospital_capability_gaps_true_when_a_chiller_line_has_current_osp(db_session):
    db_session.add(ProductLine(name="Covered Chiller Co", name_norm="covered chiller co", firm="X",
                               category="chillers_cooling", building_role="cooling_generation",
                               oshpd_osp=True))
    db_session.commit()
    gaps = hcai.hospital_capability_gaps(db_session)
    assert gaps["chillers_covered"] is True


def test_hospital_capability_gaps_fans_use_role_resolution(db_session):
    db_session.add(ProductLine(name="MacroAir", name_norm="macroair", firm="Z", category="specialty_cooling",
                               building_role="heating_specialty", oshpd_osp=True))
    db_session.commit()
    gaps = hcai.hospital_capability_gaps(db_session)
    # MacroAir is a role-override to fans_ventilation regardless of category -- see
    # app.accounts.ROLE_OVERRIDE_BY_LINE.
    assert gaps["fans_checked"] >= 1
    assert gaps["fans_covered"] is True


def test_hospital_capability_gaps_covers_all_13_roles_not_just_chillers_and_fans(db_session):
    """The bug this generalization fixes: a role with lines but none holding
    a confirmed current OSP must show as gapped, the same distinction
    already enforced for chillers/fans -- now for every one of the 13
    roles, not just those two."""
    from app.accounts import ROLE_ORDER

    db_session.add(ProductLine(name="Controls Co", name_norm="controls co", firm="X",
                               category="controls", building_role="controls_valves",
                               oshpd_osp=True))
    db_session.add(ProductLine(name="Damper Co", name_norm="damper co", firm="Y",
                               category="dampers", building_role="dampers_life_safety",
                               oshpd_osp=False))
    db_session.commit()

    gaps = hcai.hospital_capability_gaps(db_session)
    assert {rg["role"] for rg in gaps["role_gaps"]} == set(ROLE_ORDER)
    by_role = {rg["role"]: rg for rg in gaps["role_gaps"]}
    assert by_role["controls_valves"]["covered"] is True
    assert by_role["controls_valves"]["gap"] is False
    assert by_role["dampers_life_safety"]["covered"] is False
    assert by_role["dampers_life_safety"]["gap"] is True
    assert by_role["dampers_life_safety"]["confirmed_expired"] == 1
    # a role nothing was seeded for is unresearched, not confirmed-absent -- still a gap
    assert by_role["water_treatment"]["checked"] == 0
    assert by_role["water_treatment"]["gap"] is True


def test_hospital_capability_gaps_prefers_hospital_brief_osp_facts_status(db_session):
    """The bug this fixes: a line researched and confirmed not_listed (e.g.
    the fan lines, or Marley/Recold) must not show as 'unresearched' just
    because oshpd_osp itself can't represent that third state. A line's
    real name from HOSPITAL_BRIEF_OSP_FACTS is used deliberately here so
    this exercises the actual dict, not a synthetic stand-in."""
    db_session.add(ProductLine(name="Recold", name_norm="recold", firm="DMG",
                               category="cooling_towers", building_role="heat_rejection", oshpd_osp=None))
    db_session.commit()
    gaps = hcai.hospital_capability_gaps(db_session)
    by_role = {rg["role"]: rg for rg in gaps["role_gaps"]}
    assert by_role["heat_rejection"]["not_listed"] == 1
    assert by_role["heat_rejection"]["unresearched"] == 0


def test_hospital_capability_gaps_heat_rejection_covered_by_marley(db_session):
    """Marley (OSP-0171) confirmed current 2026-08-19 -- heat_rejection
    should now clear the bar, unlike its 2026-08-17 gapped state."""
    db_session.add(ProductLine(name="Marley", name_norm="marley", firm="DMG",
                               category="cooling_towers", building_role="heat_rejection", oshpd_osp=True))
    db_session.add(ProductLine(name="Recold", name_norm="recold", firm="DMG",
                               category="cooling_towers", building_role="heat_rejection", oshpd_osp=None))
    db_session.commit()
    gaps = hcai.hospital_capability_gaps(db_session)
    by_role = {rg["role"]: rg for rg in gaps["role_gaps"]}
    assert by_role["heat_rejection"]["covered"] is True
    assert by_role["heat_rejection"]["gap"] is False
    assert by_role["heat_rejection"]["current"] == 1
    assert gaps["roles_checked"] == 13
    assert gaps["roles_with_gap"] == 12  # every role except controls_valves


# ---- coverage ------------------------------------------------------------

def test_hospital_seismic_coverage_filters_by_territory(db_session, cfg):
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="In territory",
                                    county="Los Angeles", spc_deadline_year=2020,
                                    snapshot_date=utcnow(),
                                    source_url="https://example.com"))
    db_session.add(HospitalBuilding(perm_id="2", building_nbr="B1", facility_name="Out of territory",
                                    county="Alameda", spc_deadline_year=2030,
                                    snapshot_date=utcnow(),
                                    source_url="https://example.com"))
    db_session.commit()

    cov = hcai.hospital_seismic_coverage(db_session, cfg)
    assert cov["total_buildings"] == 2
    assert cov["in_territory"] == 1
    assert cov["spc_2020_deadline"] == 1
    assert cov["spc_2030_deadline"] == 0  # the SPC-2 building is Alameda, out of territory


# ---- contractor reachability -------------------------------------------

def test_hospital_contractor_reachability_counts_within_radius(db_session, cfg):
    from app.models import Contractor

    # A building needing NPC work by 2030, in territory, geocoded in LA.
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="Reach Test Hospital",
                                    county="Los Angeles", npc_deadline_year=2030,
                                    latitude=34.05, longitude=-118.25,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    # A mechanical contractor ~0.9mi away -- within any reasonable radius.
    db_session.add(Contractor(license_no="1", business_name="Nearby Mechanical", classifications="C20",
                              primary_status="CLEAR", latitude=34.06, longitude=-118.26))
    # A UA Local 250 signatory, also nearby.
    db_session.add(Contractor(license_no="2", business_name="Signatory Co", classifications="C20",
                              primary_status="CLEAR", latitude=34.06, longitude=-118.24,
                              ua_local_250_signatory=True))
    # A mechanical contractor 200mi away -- outside any reasonable radius.
    db_session.add(Contractor(license_no="3", business_name="Far Away HVAC", classifications="C38",
                              primary_status="CLEAR", latitude=36.5, longitude=-120.5))
    db_session.commit()

    result = hcai.hospital_contractor_reachability(db_session, cfg)
    assert result["npc_2030_total"] == 1
    assert result["near_mechanical_contractor"] == 1
    assert result["near_ua_local_250_signatory"] == 1
    assert result["near_mechanical_contractor_pct"] == 100.0
    assert result["near_ua_local_250_signatory_pct"] == 100.0


def test_hospital_contractor_reachability_zero_when_nothing_nearby(db_session, cfg):
    from app.models import Contractor
    db_session.add(HospitalBuilding(perm_id="1", building_nbr="B1", facility_name="Isolated Hospital",
                                    county="Los Angeles", npc_deadline_year=2030,
                                    latitude=34.05, longitude=-118.25,
                                    snapshot_date=utcnow(), source_url="https://example.com"))
    db_session.add(Contractor(license_no="1", business_name="Far Away HVAC", classifications="C20",
                              primary_status="CLEAR", latitude=36.5, longitude=-120.5))
    db_session.commit()

    result = hcai.hospital_contractor_reachability(db_session, cfg)
    assert result["near_mechanical_contractor"] == 0
    assert result["near_ua_local_250_signatory"] == 0


def test_hospital_contractor_reachability_no_buildings(db_session, cfg):
    """Guards the zero-division bug caught by test_hospitals_brief_renders_
    no_dollar_estimate: with no NPC-2030 buildings, the pct fields must be
    0.0, not a ZeroDivisionError."""
    result = hcai.hospital_contractor_reachability(db_session, cfg)
    assert result["npc_2030_total"] == 0
    assert result["near_mechanical_contractor"] == 0
    assert result["near_mechanical_contractor_pct"] == 0.0
    assert result["near_ua_local_250_signatory_pct"] == 0.0


# ---- OSP breakdown -------------------------------------------------

def test_hospital_osp_breakdown_covers_all_four_chillers_and_fourteen_fans(db_session):
    """The brief must report on every chiller/fan line, not a subset."""
    from app.accounts import seed_product_lines
    from app.config import load_config
    seed_product_lines(db_session, load_config())
    breakdown = hcai.hospital_osp_breakdown(db_session)
    assert len(breakdown["chillers"]) == 4
    assert len(breakdown["fans"]) == 14


def test_hospital_osp_breakdown_climacool_shows_expired_status(db_session):
    from app.accounts import seed_product_lines
    from app.config import load_config
    seed_product_lines(db_session, load_config())
    breakdown = hcai.hospital_osp_breakdown(db_session)
    climacool = next(l for l in breakdown["chillers"] if l["name"] == "ClimaCool")
    assert climacool["status"] == "expired"
    assert climacool["osp_number"] == "OSP-0048"


def test_hospital_osp_breakdown_discloses_socal_scope(db_session):
    from app.accounts import SOCAL_CARD_DISCLOSURE
    breakdown = hcai.hospital_osp_breakdown(db_session)
    assert breakdown["socal_card_disclosure"] == SOCAL_CARD_DISCLOSURE
    assert "SoCal" in breakdown["socal_card_disclosure"]


def test_hospital_capability_gaps_discloses_socal_scope(db_session):
    from app.accounts import SOCAL_CARD_DISCLOSURE
    gaps = hcai.hospital_capability_gaps(db_session)
    assert gaps["socal_card_disclosure"] == SOCAL_CARD_DISCLOSURE


def test_hospital_osp_breakdown_every_fan_line_has_a_definitive_status(db_session):
    """All 14 fan lines have now been researched (2026-08-09 + 2026-08-19) --
    none should be reporting the 'unresearched' fallback status any more."""
    from app.accounts import seed_product_lines
    from app.config import load_config
    seed_product_lines(db_session, load_config())
    breakdown = hcai.hospital_osp_breakdown(db_session)
    statuses = {l["name"]: l["status"] for l in breakdown["fans"]}
    assert statuses == {
        "TCF/Twin City Fan": "expired",
        "Berner": "not_listed", "Canarm": "not_listed", "FanAm": "not_listed",
        "MacroAir": "not_listed", "Panasonic": "not_listed", "Delta Breez": "not_listed",
        "Broan NuTone": "not_listed", "Systemair": "not_listed", "Monoxivent": "not_listed",
        "Strobic Air": "not_listed", "Howden": "not_listed", "Penn Barry": "not_listed",
        "Soler & Palau": "not_listed",
    }


def test_hospital_osp_breakdown_status_split_never_conflates_expired_and_not_listed(db_session):
    """The whole point of the status field: 'expired' (renewal ask) and
    'not_listed' (new-application ask) must be distinguishable, not lumped
    into one gap bucket."""
    from app.accounts import seed_product_lines
    from app.config import load_config
    seed_product_lines(db_session, load_config())
    breakdown = hcai.hospital_osp_breakdown(db_session)
    all_gap_rows = breakdown["chillers"] + breakdown["fans"]
    expired = [l for l in all_gap_rows if l["status"] == "expired"]
    not_listed = [l for l in all_gap_rows if l["status"] == "not_listed"]
    assert {l["name"] for l in expired} == {"ClimaCool", "TCF/Twin City Fan"}
    assert len(not_listed) == 16  # 3 chillers + 13 fans
    assert all(l["osp_number"] is not None for l in expired)  # expired always cites a number
    assert all(l["osp_number"] is None for l in not_listed)   # not_listed never fabricates one


def test_hospital_osp_breakdown_unknown_line_falls_back_gracefully(db_session):
    """A line not in HOSPITAL_BRIEF_OSP_FACTS (e.g. added to the card after
    this brief's facts were transcribed) must not crash the page -- it
    should show up honestly incomplete instead."""
    from app.models import ProductLine
    db_session.add(ProductLine(name="Brand New Chiller Co", name_norm="brand new chiller co", firm="X",
                               category="chillers_cooling", building_role="cooling_generation",
                               oshpd_osp=None))
    db_session.commit()
    breakdown = hcai.hospital_osp_breakdown(db_session)
    row = next(l for l in breakdown["chillers"] if l["name"] == "Brand New Chiller Co")
    assert row["status"] == "unresearched"
    assert row["osp_number"] is None
    assert "not yet transcribed" in row["note"]


def test_hospital_osp_breakdown_live_db_wins_over_stale_fact(db_session):
    """If ProductLine.oshpd_osp later flips to True for a line HCAI_BRIEF_
    OSP_FACTS still marks not_listed/expired, the live DB value must win --
    this is what keeps 'current' self-correcting instead of silently
    drifting stale on a page people read to decide who to call."""
    from app.models import ProductLine
    db_session.add(ProductLine(name="ClimaCool", name_norm="climacool", firm="X",
                               category="chillers_cooling", building_role="cooling_generation",
                               oshpd_osp=True))  # hypothetical: ClimaCool renews
    db_session.commit()
    breakdown = hcai.hospital_osp_breakdown(db_session)
    climacool = next(l for l in breakdown["chillers"] if l["name"] == "ClimaCool")
    assert climacool["status"] == "current"


def test_hospital_osp_breakdown_gapped_roles_covers_the_six_newly_researched_roles(db_session):
    db_session.add(ProductLine(name="LFSystems", name_norm="lfsystems", firm="DMG",
                               category="controls", building_role="dampers_life_safety", oshpd_osp=None))
    db_session.add(ProductLine(name="Suburban", name_norm="suburban", firm="DMG",
                               category="heaters", building_role="heating_specialty", oshpd_osp=None))
    db_session.commit()
    breakdown = hcai.hospital_osp_breakdown(db_session)
    assert "dampers_life_safety" in breakdown["gapped_roles"]
    assert breakdown["gapped_roles"]["dampers_life_safety"]["label"] == "Dampers & life safety"
    names = {r["name"] for r in breakdown["gapped_roles"]["dampers_life_safety"]["rows"]}
    assert "LFSystems" in names
    assert "heating_specialty" in breakdown["gapped_roles"]


def test_hospital_osp_breakdown_heat_rejection_drops_out_of_gapped_roles_once_covered(db_session):
    """Marley confirmed current -- heat_rejection must NOT appear in
    gapped_roles even though Recold (same role) is still not_listed."""
    db_session.add(ProductLine(name="Marley", name_norm="marley", firm="DMG",
                               category="cooling_towers", building_role="heat_rejection", oshpd_osp=True))
    db_session.add(ProductLine(name="Recold", name_norm="recold", firm="DMG",
                               category="cooling_towers", building_role="heat_rejection", oshpd_osp=None))
    db_session.commit()
    breakdown = hcai.hospital_osp_breakdown(db_session)
    assert "heat_rejection" not in breakdown["gapped_roles"]
    covered_names = {r["name"] for r in breakdown["covered"]}
    assert "Marley" in covered_names
