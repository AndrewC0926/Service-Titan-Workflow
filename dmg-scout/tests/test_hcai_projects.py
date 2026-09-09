"""HCAI Facilities Development Division projects: CSV parsing (facility
ID/name/address split, county, the 23-value Status-to-stage collapse, the
regex-only is_mechanical flag), idempotent upsert by record_no, and the
facility_id join to HospitalBuilding.perm_id -- see
app/pipeline/hcai_projects.py's module docstring for the access
investigation (one hand-pulled report.hcai.ca.gov export, never a live
fetch) this rests on."""
import csv
import io
from datetime import datetime

from sqlmodel import select

from app.models import HcaiProject, HospitalBuilding
from app.pipeline.hcai_projects import (
    MECHANICAL_RE, OPEN_STAGES, STAGE_MAP, hcai_match_rate, hcai_report_date,
    load_hcai_projects, open_hcai_projects_by_facility_id, parse_report_date, parse_rows,
)

HEADER = ["Textbox145", "Textbox139", "Textbox140", "Textbox126", "ProjNo", "ParentId",
         "ScopeofWork", "FileDate", "Costs", "PctComplete", "Status", "Textbox28", "Textbox170"]

REPORT_TITLE = "All Projects by Facility within Counties as of 09/08/2026"


def _row(record_no="A1", parent_no="", facility_id="10190", facility_name="El Centro Hospital",
        facility_address="1415 Ross Avenue, El Centro, CA 92243", county="13 - Imperial",
        scope="HVAC Replacement", date_in="04/15/2024", cost="$100,000.00", pct="0",
        status="Pending Construction Start") -> dict:
    facility_block = f"{facility_id} - {facility_name}\n{facility_address}" if facility_address \
        else f"{facility_id} - {facility_name}"
    return {
        "Textbox145": REPORT_TITLE, "Textbox139": county, "Textbox140": facility_block,
        "Textbox126": "Compliance Officer: TEST", "ProjNo": record_no, "ParentId": parent_no,
        "ScopeofWork": scope, "FileDate": date_in, "Costs": cost, "PctComplete": pct,
        "Status": status, "Textbox28": "$1,000,000.00", "Textbox170": "50",
    }


def _csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER)
    w.writeheader()
    for r in rows:
        w.writerow({h: r.get(h, "") for h in HEADER})
    return buf.getvalue().encode("utf-8-sig")


# ---- parse_rows -------------------------------------------------------

def test_parse_rows_splits_facility_id_name_address():
    raw = _csv([_row()])
    rows = parse_rows(raw)
    r = rows[0]
    assert r["facility_id"] == "10190"
    assert r["facility_name"] == "El Centro Hospital"
    assert r["facility_address"] == "1415 Ross Avenue, El Centro, CA 92243"


def test_parse_rows_strips_county_prefix():
    raw = _csv([_row(county="19 - Los Angeles")])
    rows = parse_rows(raw)
    assert rows[0]["county"] == "Los Angeles"


def test_parse_rows_parses_report_date_from_title():
    raw = _csv([_row()])
    rows = parse_rows(raw)
    assert rows[0]["report_date"] == datetime(2026, 9, 8)


def test_parse_report_date_direct():
    assert parse_report_date("All Projects by Facility within Counties as of 09/08/2026") \
        == datetime(2026, 9, 8)
    assert parse_report_date("no date here") is None


def test_parse_rows_cost_and_date_and_pct():
    raw = _csv([_row(cost="$1,234,567.89", date_in="01/02/2025", pct="45")])
    r = parse_rows(raw)[0]
    assert r["cost_est"] == 1234567.89
    assert r["date_in"] == datetime(2025, 1, 2)
    assert r["pct_complete"] == 45.0


def test_parse_rows_dash_cost_is_none():
    raw = _csv([_row(cost="-")])
    assert parse_rows(raw)[0]["cost_est"] is None


def test_parse_rows_parent_no_blank_becomes_none():
    raw = _csv([_row(parent_no="")])
    assert parse_rows(raw)[0]["parent_no"] is None
    raw2 = _csv([_row(parent_no="A0")])
    assert parse_rows(raw2)[0]["parent_no"] == "A0"


def test_parse_rows_skips_row_with_unparseable_facility_block():
    row = _row()
    row["Textbox140"] = "no dash here at all"
    raw = _csv([row])
    assert parse_rows(raw) == []


# ---- stage mapping: all 23 raw values ----------------------------------

REAL_23_STATUSES = [
    "Closed", "Closed Inactive", "Cancelled", "Closed Non-CAC Compliant",
    "Closed Non-CBSC Compliant", "Field Operations in Progress", "Pending Construction Start",
    "Approved", "Open - Remarked", "Withdrawn", "Pending Field Operations",
    "Active - Plan Review", "Void", "Pending", "Field Operations Suspended",
    "Open - Returned", "Closed Administrative", "Active", "Open", "Active - Triage",
    "Field Operations Resumed", "Issued", "Expired",
]


def test_stage_map_covers_all_23_real_values():
    assert len(REAL_23_STATUSES) == 23
    assert set(STAGE_MAP.keys()) == set(REAL_23_STATUSES)
    for status in REAL_23_STATUSES:
        assert STAGE_MAP[status] in ("plan_review", "pending_start", "in_construction", "closed", "other")


def test_stage_map_specific_placements():
    assert STAGE_MAP["Pending Construction Start"] == "pending_start"
    assert STAGE_MAP["Field Operations in Progress"] == "in_construction"
    assert STAGE_MAP["Active - Plan Review"] == "plan_review"
    assert STAGE_MAP["Closed"] == "closed"
    assert STAGE_MAP["Cancelled"] == "closed"
    assert STAGE_MAP["Pending"] == "other"
    assert STAGE_MAP["Active"] == "other"


def test_parse_rows_unmapped_status_becomes_other():
    raw = _csv([_row(status="Some Brand New Status HCAI Invents Later")])
    assert parse_rows(raw)[0]["stage"] == "other"


# ---- is_mechanical regex: positive and negative fixtures -------------

def test_mechanical_regex_positive_fixtures():
    positives = [
        "HVAC Replacement", "hvac replacement", "Chiller Replacement",
        "Boiler Upgrade", "Cooling Tower Replacement", "Central Plant Renovation",
        "Air Handler 23 Replacement", "AHU Replacement", "RTU Replacement",
        "VRF System Install", "Heat Pump Replacement", "Exhaust Fan Repair",
        "Existing mechanical revisions",
    ]
    for text in positives:
        assert MECHANICAL_RE.search(text), f"expected a match: {text!r}"


def test_mechanical_regex_negative_fixtures():
    negatives = [
        "Annual Building Permit", "Emergency Generator Replacement",
        "Roof Replacement", "Fire Alarm Upgrade", "Seismic Anchorage Package",
        "Electrical Panel Upgrade", "Parking Lot Resurfacing",
    ]
    for text in negatives:
        assert not MECHANICAL_RE.search(text), f"expected no match: {text!r}"


def test_mechanical_regex_word_boundary_does_not_false_positive():
    # "AHU" and "RTU" are short -- confirm they don't fire on unrelated
    # words that merely contain the letters.
    assert not MECHANICAL_RE.search("Bathurst Avenue Renovation")
    assert not MECHANICAL_RE.search("Virtual Reality Training Unit")


def test_parse_rows_sets_is_mechanical_from_scope_text():
    raw = _csv([_row(scope="Chiller Replacement"), _row(record_no="A2", scope="Roof Repair")])
    rows = parse_rows(raw)
    assert rows[0]["is_mechanical"] is True
    assert rows[1]["is_mechanical"] is False


# ---- load_hcai_projects: idempotent upsert -----------------------------

def test_load_is_idempotent_on_record_no(db_session, tmp_path):
    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([_row(record_no="A1")]))

    stats1 = load_hcai_projects(db_session, csv_path=str(path))
    assert stats1["fetched"] == 1 and stats1["new"] == 1 and stats1["updated"] == 0
    assert len(db_session.exec(select(HcaiProject)).all()) == 1

    stats2 = load_hcai_projects(db_session, csv_path=str(path))
    assert stats2["fetched"] == 1 and stats2["new"] == 0 and stats2["updated"] == 1
    rows = db_session.exec(select(HcaiProject)).all()
    assert len(rows) == 1
    assert rows[0].record_no == "A1"


def test_load_updates_status_and_stage_on_a_later_file(db_session, tmp_path):
    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([_row(record_no="A1", status="Active - Plan Review")]))
    load_hcai_projects(db_session, csv_path=str(path))

    path.write_bytes(_csv([_row(record_no="A1", status="Field Operations in Progress")]))
    load_hcai_projects(db_session, csv_path=str(path))

    row = db_session.get(HcaiProject, "A1")
    assert row.status_raw == "Field Operations in Progress"
    assert row.stage == "in_construction"


def test_load_records_error_on_missing_file(db_session):
    stats = load_hcai_projects(db_session, csv_path="/nonexistent/path.csv")
    assert stats["error"] is not None


def test_load_two_rows_one_with_parent(db_session, tmp_path):
    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([
        _row(record_no="A1", parent_no=""),
        _row(record_no="A2", parent_no="A1"),
    ]))
    load_hcai_projects(db_session, csv_path=str(path))
    rows = db_session.exec(select(HcaiProject)).all()
    assert len(rows) == 2
    amendment = db_session.get(HcaiProject, "A2")
    assert amendment.parent_no == "A1"


# ---- facility_id join to HospitalBuilding.perm_id ----------------------

def test_match_rate_and_a_facility_with_no_hospitalbuilding_row_still_loads(db_session, tmp_path):
    """A facility_id with no matching HospitalBuilding row (e.g. a SNF or
    clinic HCAI tracks but HospitalBuilding's own SB 1953 scope doesn't
    cover) must still load into hcai_projects -- the join reports its miss
    rate, it never gates the load."""
    db_session.add(HospitalBuilding(perm_id="10190", building_nbr="B1", facility_name="El Centro Hospital",
                                    county="Imperial", state="CA", snapshot_date=datetime(2026, 1, 1),
                                    source_url="https://example.com"))
    db_session.commit()

    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([
        _row(record_no="A1", facility_id="10190"),   # matches HospitalBuilding
        _row(record_no="A2", facility_id="99999", facility_name="Some SNF"),  # no match
    ]))
    stats = load_hcai_projects(db_session, csv_path=str(path))
    assert stats["error"] is None
    assert len(db_session.exec(select(HcaiProject)).all()) == 2  # both loaded regardless

    match = hcai_match_rate(db_session)
    assert match["hcai_facilities"] == 2
    assert match["matched"] == 1
    assert match["unmatched"] == 1
    assert match["unmatched_facility_ids"] == ["99999"]


# ---- open_hcai_projects_by_facility_id / hcai_report_date --------------

def test_open_projects_excludes_closed_and_sorts_mechanical_first(db_session, tmp_path):
    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([
        _row(record_no="A1", facility_id="10190", status="Closed", scope="HVAC Replacement"),
        _row(record_no="A2", facility_id="10190", status="Pending Construction Start", scope="Roof Repair"),
        _row(record_no="A3", facility_id="10190", status="Active - Plan Review", scope="Chiller Replacement"),
    ]))
    load_hcai_projects(db_session, csv_path=str(path))

    open_by_facility = open_hcai_projects_by_facility_id(db_session)
    projects = open_by_facility["10190"]
    assert {p.record_no for p in projects} == {"A2", "A3"}  # A1 (Closed) excluded
    assert projects[0].record_no == "A3"  # mechanical (Chiller Replacement) sorted first


def test_hcai_report_date_reads_back_the_loaded_value(db_session, tmp_path):
    assert hcai_report_date(db_session) is None
    path = tmp_path / "hcai.csv"
    path.write_bytes(_csv([_row()]))
    load_hcai_projects(db_session, csv_path=str(path))
    assert hcai_report_date(db_session) == datetime(2026, 9, 8)


def test_open_stages_constant_matches_stage_map_values():
    assert set(OPEN_STAGES) == {"plan_review", "pending_start", "in_construction"}
    assert set(OPEN_STAGES) <= set(STAGE_MAP.values())
