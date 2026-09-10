"""AHJ A2L register -- Phase B (Phase A research: app/assumptions.py's
"AHJ A2L register" group). See app/pipeline/ahj_a2l.py."""
from sqlmodel import select

from app.models import AhjA2lGuidance, Project
from app.pipeline.ahj_a2l import (
    AHJ_A2L_STATUSES,
    hit_row_for_city,
    hit_row_for_project,
    load_ahj_a2l_guidance,
)


def test_load_seeds_all_212_rows_with_correct_status_counts(db_session):
    counts = load_ahj_a2l_guidance(db_session)
    db_session.commit()

    assert counts == {"HIT": 6, "NONE_FOUND": 156, "BLOCKED": 3, "NOT_REACHED": 47}
    assert sum(counts.values()) == 212

    rows = db_session.exec(select(AhjA2lGuidance)).all()
    assert len(rows) == 212


def test_load_idempotent_on_jurisdiction(db_session):
    """Re-running the load must not duplicate a single row -- upsert on the
    jurisdiction primary key, same discipline every other seed_*/load_*
    function in this codebase already uses."""
    load_ahj_a2l_guidance(db_session)
    db_session.commit()
    first_count = len(db_session.exec(select(AhjA2lGuidance)).all())

    counts = load_ahj_a2l_guidance(db_session)
    db_session.commit()
    second_count = len(db_session.exec(select(AhjA2lGuidance)).all())

    assert first_count == second_count == 212
    assert sum(counts.values()) == 212

    # Not just the same COUNT -- the same actual primary keys, re-updated in
    # place, not one dropped and a different one added.
    ladbs = db_session.get(AhjA2lGuidance, "Los Angeles")
    assert ladbs is not None
    assert ladbs.status == "HIT"


def test_status_enum_coverage(db_session):
    """Every row's status is one of the four documented values -- no fifth
    value ever silently introduced, and all four are actually exercised by
    real data, not a hypothetical enum with unused members."""
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    rows = db_session.exec(select(AhjA2lGuidance)).all()
    statuses_seen = {r.status for r in rows}

    assert statuses_seen == set(AHJ_A2L_STATUSES)
    for r in rows:
        assert r.status in AHJ_A2L_STATUSES, (r.jurisdiction, r.status)


def test_not_reached_rows_have_null_checked_at_hit_and_none_found_do_not(db_session):
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    rows = db_session.exec(select(AhjA2lGuidance)).all()
    for r in rows:
        if r.status == "NOT_REACHED":
            assert r.checked_at is None, r.jurisdiction
        else:
            assert r.checked_at is not None, r.jurisdiction


def test_null_preserved_not_invented(db_session):
    """Los Angeles County is a real HIT row (a real document was read) whose
    document simply doesn't state most of the 8 fields. Every one of those
    must render as NULL, not filled in from the seed row's pattern or any
    other row's values -- the exact case CHARTER.md invariant 12 exists
    for."""
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    row = db_session.get(AhjA2lGuidance, "Los Angeles County")
    assert row is not None
    assert row.status == "HIT"
    assert row.doc_title == "Residential A2L Refrigerant Plan Check Determination Checklist"

    for field in ("ashrae_15_edition", "ashrae_15_2_edition", "ashrae_34_edition",
                 "addendum_a_shaft_alt", "addenda_accepted", "edvc_regardless_of_charge",
                 "a1_resubmittal_rule", "express_permit_note", "doc_number", "doc_date"):
        assert getattr(row, field) is None, f"{field} should be NULL, got {getattr(row, field)!r}"


def test_fixture_row_all_eight_fields_null_renders_without_inventing_values(db_session):
    """A synthetic fixture, independent of the real Phase A data: a HIT row
    with every one of the 8 document columns explicitly NULL. Reading it
    back must show every field as None -- nothing a renderer or a caller
    could mistake for a real, sourced value."""
    db_session.add(AhjA2lGuidance(
        jurisdiction="Fixture City", jurisdiction_type="city", county="Los Angeles",
        status="HIT", doc_title="Fixture Document", checked_at=None,
        ashrae_15_edition=None, ashrae_15_2_edition=None, ashrae_34_edition=None,
        addendum_a_shaft_alt=None, addenda_accepted=None, edvc_regardless_of_charge=None,
        a1_resubmittal_rule=None, express_permit_note=None,
    ))
    db_session.commit()

    row = db_session.get(AhjA2lGuidance, "Fixture City")
    for field in ("ashrae_15_edition", "ashrae_15_2_edition", "ashrae_34_edition",
                 "addendum_a_shaft_alt", "addenda_accepted", "edvc_regardless_of_charge",
                 "a1_resubmittal_rule", "express_permit_note"):
        assert getattr(row, field) is None, field


def test_badge_fires_only_on_hit_project(db_session):
    """hit_row_for_project must return the real HIT row for a project in
    Los Angeles County (the only county-type HIT), and None for every other
    county -- including one with real NONE_FOUND/BLOCKED rows, proving the
    function checks status, not just county presence."""
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    la_project = Project(name="Fixture LA project", county="Los Angeles")
    riverside_project = Project(name="Fixture Riverside project", county="Riverside")
    orange_project = Project(name="Fixture Orange project", county="Orange")
    no_county_project = Project(name="Fixture no-county project", county=None)
    db_session.add_all([la_project, riverside_project, orange_project, no_county_project])
    db_session.commit()

    hit = hit_row_for_project(db_session, la_project)
    assert hit is not None
    assert hit.jurisdiction == "Los Angeles County"
    assert hit.status == "HIT"

    # Riverside County is BLOCKED, not HIT -- must not badge.
    assert hit_row_for_project(db_session, riverside_project) is None
    # Orange County is NONE_FOUND -- must not badge.
    assert hit_row_for_project(db_session, orange_project) is None
    # No county at all -- must not badge (and must not raise).
    assert hit_row_for_project(db_session, no_county_project) is None


def test_badge_fires_only_on_hit_city(db_session):
    """hit_row_for_city must exact-match a city-type HIT jurisdiction
    (Los Angeles, Escondido), never a substring, and never a county-type or
    non-HIT row."""
    load_ahj_a2l_guidance(db_session)
    db_session.commit()

    hit = hit_row_for_city(db_session, "Los Angeles", "Los Angeles")
    assert hit is not None
    assert hit.jurisdiction == "Los Angeles"
    assert hit.jurisdiction_type == "city"

    hit2 = hit_row_for_city(db_session, "escondido", "San Diego")  # case-insensitive
    assert hit2 is not None
    assert hit2.jurisdiction == "Escondido"

    # A NONE_FOUND city must not badge.
    assert hit_row_for_city(db_session, "Long Beach", "Los Angeles") is None
    # Wrong county for a real HIT city name must not badge (defense in depth;
    # no such collision exists in-territory today, but the gate is real).
    assert hit_row_for_city(db_session, "Los Angeles", "Orange") is None
    # No city at all -- must not badge, must not raise.
    assert hit_row_for_city(db_session, None, "Los Angeles") is None
