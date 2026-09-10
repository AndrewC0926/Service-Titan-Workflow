"""docs/DAILY-BRIEF-DESIGN.md §3a item 3: "New or changed since yesterday"
and "Manual sources past re-pull date" -- brief integration only, no
Opportunity table, no /pipeline, no weekly xlsx. Zero LLM calls: every
render function here is a plain f-string, never handed to narrate_or_fallback.
See app/pipeline/notify.py's own comments for the design decisions
(changed_at vs. last_seen_at, "the existing ranking" = recency)."""
from datetime import timedelta

from sqlmodel import select

from app.models import (
    Ab802Building, Ab869Plan, AhjA2lGuidance, BpelsgEngineer, HcaiProject, IeprForwardLoad,
    OpscProject, ScaqmdFacility, SourceRowSeen, utcnow,
)
from app.pipeline.notify import (
    _render_manual_recadence,
    _render_new_or_changed,
    manual_recadence_status,
    manual_sources_past_recadence,
    new_or_changed_since_yesterday,
)


def _hcai(record_no, stage="pending_start", is_mechanical=True, facility_id="F1",
         facility_name="Test Hospital", county="Los Angeles"):
    return HcaiProject(record_no=record_no, facility_id=facility_id, facility_name=facility_name,
                       county=county, scope_text="", status_raw=stage, stage=stage,
                       is_mechanical=is_mechanical, report_date=utcnow())


def _seen(source, natural_key, first_seen_at=None, changed_at=None, last_seen_at=None):
    now = utcnow()
    return SourceRowSeen(source=source, natural_key=natural_key, fingerprint="x",
                         first_seen_at=first_seen_at or now, last_seen_at=last_seen_at or now,
                         changed_at=changed_at)


# ---- new_or_changed_since_yesterday -----------------------------------------

def test_empty_diff_renders_cleanly(db_session):
    """No SourceRowSeen activity in the last 24h at all -- the function
    returns an empty list, and the renderer says so explicitly rather than
    omitting the section, matching this digest's own "says so plainly when
    nothing did" rule."""
    rows = new_or_changed_since_yesterday(db_session)
    assert rows == []
    rendered = _render_new_or_changed(rows)
    assert "NEW OR CHANGED SINCE YESTERDAY" in rendered
    assert "Nothing new or changed" in rendered


def test_new_row_within_24h_appears_as_new(db_session):
    db_session.add(_hcai("H1", stage="plan_review", is_mechanical=True))
    db_session.commit()
    db_session.add(_seen("hcai_projects", "H1", first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["event"] == "new"
    assert rows[0]["source"] == "HCAI project"
    assert rows[0]["stage_or_status"] == "plan_review"
    assert "mechanical scope" in rows[0]["why"]
    assert "Test Hospital" in rows[0]["why"]

    rendered = _render_new_or_changed(rows)
    assert "[new] HCAI project" in rendered


def test_changed_row_within_24h_appears_as_changed_not_new(db_session):
    """A row with an OLD first_seen_at but a RECENT changed_at must be
    labeled 'changed', not 'new' -- this is exactly what changed_at exists
    to distinguish from last_seen_at (which advances every night either
    way, see SourceRowSeen's own docstring)."""
    db_session.add(_hcai("H2", stage="in_construction", is_mechanical=False))
    db_session.commit()
    old = utcnow() - timedelta(days=30)
    db_session.add(_seen("hcai_projects", "H2", first_seen_at=old, changed_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["event"] == "changed"
    assert rows[0]["stage_or_status"] == "in_construction"


def test_row_older_than_24h_is_excluded(db_session):
    db_session.add(_hcai("H3"))
    db_session.commit()
    old = utcnow() - timedelta(days=2)
    db_session.add(_seen("hcai_projects", "H3", first_seen_at=old, last_seen_at=old))
    db_session.commit()

    assert new_or_changed_since_yesterday(db_session) == []


def test_deleted_source_row_is_skipped_not_invented(db_session):
    """SourceRowSeen says H4 was new in the last 24h, but no HcaiProject row
    with that record_no exists (e.g. a same-day full-replace dropped it) --
    must be skipped, never rendered with made-up fields."""
    db_session.add(_seen("hcai_projects", "H4", first_seen_at=utcnow()))
    db_session.commit()
    assert new_or_changed_since_yesterday(db_session) == []


def test_ranked_by_recency_most_recent_first(db_session):
    db_session.add_all([_hcai("H5"), _hcai("H6", facility_id="F2", facility_name="Other Hospital")])
    db_session.commit()
    older = utcnow() - timedelta(hours=20)
    newer = utcnow() - timedelta(hours=1)
    db_session.add(_seen("hcai_projects", "H5", first_seen_at=older))
    db_session.add(_seen("hcai_projects", "H6", first_seen_at=newer))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert [r["key"] for r in rows] == ["H6", "H5"]


def test_ab869_plan_lookup_by_perm_id_not_pk(db_session):
    """Ab869Plan's real primary key is the autoincrement `id`, not perm_id --
    a natural_key lookup that used session.get(Ab869Plan, natural_key) would
    silently find nothing. Regression test for that exact bug."""
    db_session.add(Ab869Plan(perm_id="P1", plan_status="In Progress", delay_requested=True,
                             financially_responsible_party="Acme Health District",
                             source_pdf_path="x.pdf", source_pdf_hash="h1"))
    db_session.commit()
    db_session.add(_seen("ab869_plans", "P1", first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["source"] == "AB 869 plan"
    assert rows[0]["stage_or_status"] == "In Progress"
    assert "Acme Health District" in rows[0]["why"]
    assert "delay requested" in rows[0]["why"]


def test_ab802_building_lookup_by_composite_key(db_session):
    """Natural key is 'pmid:year' -- must split and query both
    portfolio_manager_property_id and year_ending, not treat it as one PK."""
    db_session.add(Ab802Building(portfolio_manager_property_id="PM1", year_ending=2025,
                                 property_name="Acme Warehouse", air_permit_facility_id="F99",
                                 source_url="https://example.test/ab802.csv"))
    db_session.commit()
    db_session.add(_seen("ab802_buildings", "PM1:2025", first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["source"] == "AB 802 filing"
    assert rows[0]["stage_or_status"] == "air permit matched"
    assert "Acme Warehouse" in rows[0]["why"]


def test_opsc_project_lookup_by_application_number(db_session):
    db_session.add(OpscProject(application_number="APP1", status="Funded",
                               school_name="Lincoln Elementary", district="Sample USD",
                               source_url="https://example.test/opsc.csv"))
    db_session.commit()
    db_session.add(_seen("opsc_projects", "APP1", first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["source"] == "OPSC application"
    assert rows[0]["stage_or_status"] == "Funded"
    assert "Lincoln Elementary" in rows[0]["why"]


def test_scaqmd_facility_lookup_by_composite_key(db_session):
    """Natural key is 'facility_id:source' -- both columns, not facility_id alone
    (the same facility can legitimately appear under two different sources)."""
    db_session.add(ScaqmdFacility(facility_id="FAC1", source="aer_facilities_notified",
                                  facility_name="Acme Plant",
                                  source_url="https://example.test/scaqmd.xlsx"))
    db_session.commit()
    db_session.add(_seen("scaqmd_facilities", "FAC1:aer_facilities_notified", first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session)
    assert len(rows) == 1
    assert rows[0]["source"] == "SCAQMD/CARB facility"
    assert rows[0]["stage_or_status"] == "new facility registration"
    assert "Acme Plant" in rows[0]["why"]


def test_limit_caps_at_ten(db_session):
    keys = []
    for i in range(15):
        rn = f"K{i}"
        keys.append(rn)
        db_session.add(_hcai(rn, facility_id=f"F{i}"))
        db_session.add(_seen("hcai_projects", rn, first_seen_at=utcnow()))
    db_session.commit()

    rows = new_or_changed_since_yesterday(db_session, limit=10)
    assert len(rows) == 10


# ---- manual_sources_past_recadence ------------------------------------------

def test_stale_manual_source_appears(db_session, cfg):
    """HCAI report's own cadence is 2160h (90 days) -- an import 200 days
    ago is well past it."""
    stale_date = utcnow() - timedelta(days=200)
    db_session.add(HcaiProject(record_no="S1", facility_id="F1", facility_name="X",
                               county="Los Angeles", scope_text="", status_raw="Open",
                               stage="plan_review", is_mechanical=False, report_date=stale_date,
                               imported_at=stale_date))
    db_session.commit()

    status = manual_recadence_status(db_session, cfg)
    hcai_row = next(r for r in status if r["name"] == "HCAI report")
    assert hcai_row["is_stale"] is True
    assert hcai_row["days_since"] > 90

    stale_only = manual_sources_past_recadence(db_session, cfg)
    assert any(r["name"] == "HCAI report" for r in stale_only)

    rendered = _render_manual_recadence(stale_only)
    assert "HCAI report" in rendered
    assert "days since last pull" in rendered


def test_fresh_manual_source_does_not_appear(db_session, cfg):
    """A pull from yesterday is nowhere near HCAI's own 2160h (90 day)
    cadence -- must NOT show up in the past-due list."""
    fresh_date = utcnow() - timedelta(days=1)
    db_session.add(HcaiProject(record_no="F1", facility_id="F1", facility_name="X",
                               county="Los Angeles", scope_text="", status_raw="Open",
                               stage="plan_review", is_mechanical=False, report_date=fresh_date,
                               imported_at=fresh_date))
    db_session.commit()

    status = manual_recadence_status(db_session, cfg)
    hcai_row = next(r for r in status if r["name"] == "HCAI report")
    assert hcai_row["is_stale"] is False
    assert hcai_row["days_since"] < 2

    stale_only = manual_sources_past_recadence(db_session, cfg)
    assert not any(r["name"] == "HCAI report" for r in stale_only)


def test_never_pulled_source_counts_as_stale(db_session, cfg):
    """No AhjA2lGuidance rows at all (never seeded) -- last_pull is None,
    which must count as past due, not be silently skipped."""
    status = manual_recadence_status(db_session, cfg)
    ahj_row = next(r for r in status if r["name"] == "AHJ register")
    assert ahj_row["last_pull"] is None
    assert ahj_row["is_stale"] is True

    rendered = _render_manual_recadence([ahj_row])
    assert "never pulled" in rendered


def test_all_five_sources_present_in_status(db_session, cfg):
    status = manual_recadence_status(db_session, cfg)
    names = {r["name"] for r in status}
    assert names == {"HCAI report", "AB 869", "IEPR", "DCA BPELSG file", "AHJ register"}
