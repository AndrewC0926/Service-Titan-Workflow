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
