"""Map an extracted schedule onto the line card. See app/schedule_mapping.py.

Fixture line card: AAON (air_handling, ours), Marley (heat_rejection, ours).
Trane and Carrier are never seeded -- real-world accurate, neither is on
DMG's own card -- so any tag naming them as basis of design is a genuine
competitor, exactly the case this module exists to catch."""
from app.models import Category, CompetitorLine, ProductLine, Project, ScheduleEntry
from app.normalize import normalize_name
from app.schedule_mapping import actionable, map_project_schedule_to_line_card


def _line(db_session, name, role, firm="DMG", category="air_handling"):
    line = ProductLine(name=name, name_norm=normalize_name(name), firm=firm,
                       category=category, building_role=role)
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)
    return line


def _project(db_session):
    p = Project(name="Test Project", category=Category.industrial)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _entry(db_session, project_id, doc_id=1, **kw):
    base = {"project_document_id": doc_id, "project_id": project_id, "tag": "T-1",
           "role": "air_handling", "basis_of_design_manufacturer": None, "approved_equals": []}
    base.update(kw)
    e = ScheduleEntry(**base)
    db_session.add(e)
    db_session.commit()
    db_session.refresh(e)
    return e


def test_no_entries_returns_empty(db_session):
    p = _project(db_session)
    assert map_project_schedule_to_line_card(db_session, p.id) == []


def test_competitor_bod_with_our_approved_equal_is_actionable(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling",
          basis_of_design_manufacturer="Trane", approved_equals=["AAON", "Carrier"])

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    assert len(mappings) == 1
    m = mappings[0]
    assert m.status == "actionable_equal"
    assert m.bod_is_ours is None
    assert [l.name for l in m.equal_lines_that_are_ours] == ["AAON"]
    act = actionable(mappings)
    assert len(act) == 1 and act[0].tag == "RTU-1"


def test_we_already_hold_bod(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling",
          basis_of_design_manufacturer="AAON", approved_equals=["Carrier"])

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert m.status == "we_hold_bod"
    assert m.bod_is_ours.name == "AAON"
    assert actionable([m]) == []


def test_competitor_bod_no_matching_equal_is_a_gap(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling",
          basis_of_design_manufacturer="Trane", approved_equals=["Carrier", "York"])

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert m.status == "gap_no_equal"
    assert m.equal_lines_that_are_ours == []


def test_no_line_for_role_at_all(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")  # nothing seeded for heat_rejection
    _entry(db_session, p.id, tag="CT-1", role="heat_rejection",
          basis_of_design_manufacturer="Marley", approved_equals=["Recold"])

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert m.status == "no_line_for_role"
    assert m.our_lines_for_role == []


def test_unclassified_role_never_forced_into_a_status(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="X-1", role=None, basis_of_design_manufacturer="Trane")

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert m.status == "unclassified"
    assert m.role_label is None
    assert actionable([m]) == []


def test_manufacturer_matching_is_exact_normalized_not_fuzzy(db_session):
    """A name that doesn't reduce to one of our 70 lines is not one of
    ours, not a maybe -- no partial/substring matching."""
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling",
          basis_of_design_manufacturer="AAON Coil Products",  # a different, real company
          approved_equals=[])

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert m.bod_is_ours is None
    assert m.status == "gap_no_equal"


def test_competing_lines_enrichment_scoped_to_the_tag_s_role(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    db_session.add(CompetitorLine(manufacturer="Trane", building_role="air_handling",
                                  channel="factory_direct", source_url="https://example.com"))
    db_session.add(CompetitorLine(manufacturer="Marley", building_role="heat_rejection",
                                  channel="factory_direct", source_url="https://example.com"))
    db_session.commit()
    _entry(db_session, p.id, tag="RTU-1", role="air_handling")

    m = map_project_schedule_to_line_card(db_session, p.id)[0]
    assert [c.manufacturer for c in m.competing_lines] == ["Trane"]
