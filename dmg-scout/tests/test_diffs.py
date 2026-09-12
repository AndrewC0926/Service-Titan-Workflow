"""Nightly diff (item 1 of docs/DAILY-BRIEF-DESIGN.md, built alone -- no
brief, no Opportunity table, no UI). See app/pipeline/diffs.py."""
from sqlmodel import select

from app.models import (
    Ab802Building,
    Ab869Plan,
    HcaiProject,
    OpscProject,
    ScaqmdFacility,
    SourceRowSeen,
    utcnow,
)
from app.pipeline.diffs import (
    SOURCE_FINGERPRINT_FIELDS,
    SOURCE_ROW_FETCHERS,
    _ab802_buildings_rows,
    _ab869_plans_rows,
    _hcai_projects_rows,
    _opsc_projects_rows,
    _scaqmd_facilities_rows,
    diff_source,
    run_all_diffs,
)


def _hcai(record_no, stage, is_mechanical=False, facility_id="F1"):
    return HcaiProject(record_no=record_no, facility_id=facility_id, facility_name="Test Hospital",
                       county="Los Angeles", scope_text="", status_raw=stage, stage=stage,
                       is_mechanical=is_mechanical, report_date=utcnow())


def test_five_sources_registered_consistently():
    """The assumptions register, the CLI report, and the fetcher registry
    must all agree on the same five tables -- 'projects'/'signals' are
    deliberately excluded (already covered by DigestLog, see module docstring)."""
    assert set(SOURCE_FINGERPRINT_FIELDS) == set(SOURCE_ROW_FETCHERS)
    assert set(SOURCE_ROW_FETCHERS) == {
        "hcai_projects", "ab869_plans", "ab802_buildings", "opsc_projects", "scaqmd_facilities",
    }


# ---- baseline: first-ever run seeds, alerts nothing -----------------------

def test_baseline_seeds_every_row_with_zero_alerts(db_session):
    db_session.add_all([
        _hcai("H1", "plan_review"), _hcai("H2", "pending_start"), _hcai("H3", "closed"),
        Ab869Plan(perm_id="P1", plan_status="Compliant", delay_requested=False,
                  source_pdf_path="x.pdf", source_pdf_hash="h1"),
        Ab869Plan(perm_id="P2", plan_status="Non-Compliant", delay_requested=True,
                  source_pdf_path="y.pdf", source_pdf_hash="h2"),
        Ab802Building(portfolio_manager_property_id="B1", year_ending=2024, source_url="https://x"),
        Ab802Building(portfolio_manager_property_id="B2", year_ending=2024, source_url="https://x"),
        OpscProject(application_number="A1", status="Approved", source_url="https://data.ca.gov/x"),
        ScaqmdFacility(facility_id="FAC1", source_url="https://aqmd.gov/x"),
        ScaqmdFacility(facility_id="FAC2", source="carb", source_url="https://arb.ca.gov/x"),
    ])
    db_session.commit()

    results = run_all_diffs(db_session)
    db_session.commit()

    expected_seeded = {"hcai_projects": 3, "ab869_plans": 2, "ab802_buildings": 2,
                       "opsc_projects": 1, "scaqmd_facilities": 2}
    for source, r in results.items():
        assert r.baseline is True, source
        assert r.seeded == expected_seeded[source], source
        assert r.new == [] and r.changed == [] and r.unchanged == [] and r.removed == [], source

    seen = db_session.exec(select(SourceRowSeen)).all()
    assert len(seen) == sum(expected_seeded.values())


# ---- the requested fixture: one new, one changed, one unchanged, one removed

def test_fixture_new_changed_unchanged_removed(db_session):
    """Exact scenario asked for, on hcai_projects: run 1 seeds A (unchanged
    later), B (stage pending_start), C (removed later) as a baseline. Run 2
    has A unchanged, B changed (pending_start -> in_construction), D new,
    and C absent from the source entirely."""
    db_session.add_all([_hcai("A", "plan_review"), _hcai("B", "pending_start"), _hcai("C", "closed")])
    db_session.commit()

    baseline = diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()
    assert baseline.baseline is True
    assert baseline.seeded == 3
    assert baseline.new == [] and baseline.changed == [] and baseline.unchanged == [] and baseline.removed == []

    # Mutate the source table to the run-2 shape: A untouched, B's stage
    # changes, C is deleted outright (gone from the source file), D appears.
    b = db_session.get(HcaiProject, "B")
    b.stage = "in_construction"
    db_session.delete(db_session.get(HcaiProject, "C"))
    db_session.add(_hcai("D", "plan_review"))
    db_session.commit()

    result = diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()

    assert result.baseline is False
    assert result.seeded == 1
    assert result.new == ["D"]
    assert result.changed == [("B", "pending_start|False", "in_construction|False")]
    assert result.unchanged == ["A"]
    assert result.removed == ["C"]
    assert result.reappeared == []

    # Removed rows must be FLAGGED, not silently dropped: the SourceRowSeen
    # row for C still exists, with removed_at set -- never deleted.
    seen_c = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.source == "hcai_projects",
                                                          SourceRowSeen.natural_key == "C")).one()
    assert seen_c.removed_at is not None
    assert len(db_session.exec(select(SourceRowSeen)).all()) == 4  # A, B, C (removed), D -- not 3

    # changed_at: set ONLY on B (the one genuine fingerprint change), never
    # on A (unchanged, bulk-updated), D (new, not a change), or C (removed,
    # not a fingerprint change) -- see SourceRowSeen.changed_at's own
    # docstring for why last_seen_at alone can't answer this question (it
    # advances for A, C, and D too).
    by_key = {r.natural_key: r for r in db_session.exec(select(SourceRowSeen)).all()}
    assert by_key["B"].changed_at is not None
    assert by_key["A"].changed_at is None
    assert by_key["D"].changed_at is None
    assert by_key["C"].changed_at is None


def test_changed_at_never_set_on_baseline_run(db_session):
    """A baseline run seeds every row via the `row is None` branch, which
    never touches changed_at at all -- confirms the column stays NULL for
    a source's first-ever diff, not accidentally stamped 'changed' just
    because it's the first time anything was recorded."""
    db_session.add(_hcai("A", "plan_review"))
    db_session.commit()
    diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()
    seen = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.natural_key == "A")).one()
    assert seen.changed_at is None


def test_changed_at_is_stamped_on_a_genuine_fingerprint_change(db_session):
    """Block 2 closeout: changed_at is NULL on all 118,742 real rows
    locally -- this checks whether that's a bug in the diff or a genuine
    quiet period. A row whose fingerprint (hcai_projects keys on `stage`,
    see SOURCE_FINGERPRINT_FIELDS) actually changes between two diff_source
    runs must get changed_at stamped on the second run, not left NULL."""
    db_session.add(_hcai("A", "plan_review"))
    db_session.commit()
    diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))  # baseline
    db_session.commit()
    seen = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.natural_key == "A")).one()
    assert seen.changed_at is None  # baseline: nothing "changed" yet

    row = db_session.get(HcaiProject, "A")
    row.stage = "bidding"
    db_session.commit()
    r = diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()
    assert r.changed == [("A", "plan_review|False", "bidding|False")]

    db_session.expire_all()
    seen = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.natural_key == "A")).one()
    assert seen.changed_at is not None


def test_removed_row_that_reappears_is_flagged_reappeared_and_unremoved(db_session):
    db_session.add(_hcai("X", "plan_review"))
    db_session.commit()
    diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))  # baseline
    db_session.commit()

    db_session.delete(db_session.get(HcaiProject, "X"))
    db_session.commit()
    r1 = diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()
    assert r1.removed == ["X"]
    seen = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.natural_key == "X")).one()
    assert seen.removed_at is not None

    db_session.add(_hcai("X", "plan_review"))
    db_session.commit()
    r2 = diff_source(db_session, "hcai_projects", _hcai_projects_rows(db_session))
    db_session.commit()
    assert r2.reappeared == ["X"]
    seen = db_session.exec(select(SourceRowSeen).where(SourceRowSeen.natural_key == "X")).one()
    assert seen.removed_at is None


# ---- per-table fingerprint correctness -------------------------------------

def test_ab802_fingerprint_is_air_permit_only_not_the_annual_filing(db_session):
    """AB 802 is an annual filing -- a new (property, year) row every year,
    never a 'change' to an existing one. The one field that legitimately
    changes on an existing row is the AB 869/802 air-permit join."""
    db_session.add(Ab802Building(portfolio_manager_property_id="B1", year_ending=2024,
                                 property_name="original name", source_url="https://x"))
    db_session.commit()
    diff_source(db_session, "ab802_buildings", _ab802_buildings_rows(db_session))
    db_session.commit()

    row = db_session.exec(select(Ab802Building).where(
        Ab802Building.portfolio_manager_property_id == "B1")).one()
    row.property_name = "renamed, not a real-world change worth alerting on"
    db_session.commit()
    r = diff_source(db_session, "ab802_buildings", _ab802_buildings_rows(db_session))
    db_session.commit()
    assert r.changed == [] and r.unchanged == ["B1:2024"]

    row.air_permit_facility_id = "FAC9"
    db_session.commit()
    r2 = diff_source(db_session, "ab802_buildings", _ab802_buildings_rows(db_session))
    assert r2.changed == [("B1:2024", "None", "FAC9")]


def test_scaqmd_facilities_never_reports_changed_by_design(db_session):
    db_session.add(ScaqmdFacility(facility_id="FAC1", facility_name="Original Co",
                                  source_url="https://aqmd.gov/x"))
    db_session.commit()
    diff_source(db_session, "scaqmd_facilities", _scaqmd_facilities_rows(db_session))
    db_session.commit()

    row = db_session.exec(select(ScaqmdFacility).where(ScaqmdFacility.facility_id == "FAC1")).one()
    row.facility_name = "Renamed Co"
    db_session.commit()
    r = diff_source(db_session, "scaqmd_facilities", _scaqmd_facilities_rows(db_session))
    assert r.changed == []
    assert r.unchanged == ["FAC1:aer_facilities_notified"]


def test_opsc_fingerprint_is_status(db_session):
    db_session.add(OpscProject(application_number="A1", status="Preliminary", source_url="https://x"))
    db_session.commit()
    diff_source(db_session, "opsc_projects", _opsc_projects_rows(db_session))
    db_session.commit()

    row = db_session.exec(select(OpscProject).where(OpscProject.application_number == "A1")).one()
    row.status = "Approved"
    db_session.commit()
    r = diff_source(db_session, "opsc_projects", _opsc_projects_rows(db_session))
    assert r.changed == [("A1", "Preliminary", "Approved")]


def test_ab869_fingerprint_is_plan_status_and_delay_requested(db_session):
    db_session.add(Ab869Plan(perm_id="P1", plan_status="Compliant", delay_requested=False,
                             source_pdf_path="x.pdf", source_pdf_hash="h1"))
    db_session.commit()
    diff_source(db_session, "ab869_plans", _ab869_plans_rows(db_session))
    db_session.commit()

    row = db_session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == "P1")).one()
    row.delay_requested = True
    db_session.commit()
    r = diff_source(db_session, "ab869_plans", _ab869_plans_rows(db_session))
    assert r.changed == [("P1", "Compliant|False", "Compliant|True")]
