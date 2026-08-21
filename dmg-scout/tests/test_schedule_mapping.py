"""Map an extracted schedule onto the line card. See app/schedule_mapping.py.

Fixture line card: AAON (air_handling, ours), Marley (heat_rejection, ours).
Trane and Carrier are never seeded -- real-world accurate, neither is on
DMG's own card -- so any tag naming them as basis of design is a genuine
competitor, exactly the case this module exists to catch."""
from app.models import (
    Category,
    CompetitorLine,
    ProductLine,
    ProductLineBranch,
    Project,
    RepFirm,
    ScheduleEntry,
)
from app.normalize import normalize_name
from app.schedule_mapping import (
    actionable,
    displaceable,
    map_project_schedule_to_line_card,
    resolve_branch,
    resolve_displacement,
    role_gaps,
)


def _line(db_session, name, role, firm="DMG", category="air_handling"):
    line = ProductLine(name=name, name_norm=normalize_name(name), firm=firm,
                       category=category, building_role=role)
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)
    return line


def _project(db_session, county=None, state=None):
    p = Project(name="Test Project", category=Category.industrial, county=county, state=state)
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


# ---- resolve_branch --------------------------------------------------------

def test_resolve_branch_matches_documented_counties(db_session):
    assert resolve_branch(_project(db_session, county="Los Angeles")).branch == "DMG Los Angeles"
    assert resolve_branch(_project(db_session, county="San Diego")).branch == "DMG San Diego"
    assert resolve_branch(_project(db_session, county="San Luis Obispo")).branch == "DMG Central Coast"
    assert resolve_branch(_project(db_session, county="Fresno")).branch == "DMG Central Valley"
    assert resolve_branch(_project(db_session, state="HI")).branch == "DMG Hawaii"


def test_resolve_branch_never_guesses_a_nearby_county(db_session):
    """Kern is not Fresno, Orange is not Los Angeles -- even though both
    pairs sit in the same broad region, only a LITERAL match resolves."""
    r = resolve_branch(_project(db_session, county="Orange"))
    assert r.branch is None
    assert "Orange" in r.note

    r2 = resolve_branch(_project(db_session, county=None))
    assert r2.branch is None
    assert "no county" in r2.note


# ---- resolve_displacement ---------------------------------------------------

def _competitor(db_session, manufacturer, role, channel="rep_firm", rep_firm_id=None, status="confirmed"):
    cl = CompetitorLine(manufacturer=manufacturer, building_role=role, channel=channel,
                        rep_firm_id=rep_firm_id, status=status, source_url="https://example.com")
    db_session.add(cl)
    db_session.commit()
    return cl


def _rep_firm(db_session, name):
    firm = RepFirm(name=name)
    db_session.add(firm)
    db_session.commit()
    db_session.refresh(firm)
    return firm


def test_displacement_resolves_rep_firm_state(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    firm = _rep_firm(db_session, "Air Treatment Corporation")
    _competitor(db_session, "YORK Applied (AHUs, DOAS, ERV, fan coils, WSHP)", "air_handling",
               rep_firm_id=firm.id)
    _entry(db_session, p.id, tag="RTU-1", role="air_handling", basis_of_design_manufacturer="YORK")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    rows = resolve_displacement(db_session, p, mappings)
    assert len(rows) == 1
    r = rows[0]
    assert r.competitor_state == "rep_firm"
    assert r.competitor_rep_firm == "Air Treatment Corporation"
    assert r.role_gap is False
    assert [l.name for l in r.our_lines] == ["AAON"]


def test_displacement_resolves_factory_direct_state(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _competitor(db_session, "Trane", "air_handling", channel="factory_direct")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling", basis_of_design_manufacturer="Trane")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    r = resolve_displacement(db_session, p, mappings)[0]
    assert r.competitor_state == "factory_direct"
    assert r.competitor_rep_firm is None


def test_displacement_unknown_when_no_competitor_research_exists(db_session):
    """Carrier/Liebert have no CompetitorLine coverage in real Scout data
    (2026-08-21) -- must resolve unknown, never guessed into a state."""
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="AH-1", role="air_handling", basis_of_design_manufacturer="Carrier")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    r = resolve_displacement(db_session, p, mappings)[0]
    assert r.competitor_state == "unknown"
    assert r.competitor_rep_firm is None


def test_displacement_excludes_rows_with_no_bod_or_where_we_hold_it(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _entry(db_session, p.id, tag="NO-BOD", role="air_handling")  # no basis_of_design_manufacturer
    _entry(db_session, p.id, tag="OURS", role="air_handling", basis_of_design_manufacturer="AAON")
    _competitor(db_session, "Trane", "air_handling", channel="factory_direct")
    _entry(db_session, p.id, tag="COMPETITOR", role="air_handling", basis_of_design_manufacturer="Trane")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    rows = resolve_displacement(db_session, p, mappings)
    assert [r.tag for r in rows] == ["COMPETITOR"]


def test_displacement_role_gap_when_we_carry_nothing(db_session):
    p = _project(db_session)
    _competitor(db_session, "Marley", "heat_rejection", channel="factory_direct")
    _entry(db_session, p.id, tag="CT-1", role="heat_rejection", basis_of_design_manufacturer="Marley")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    rows = resolve_displacement(db_session, p, mappings)
    assert rows[0].role_gap is True
    assert role_gaps(rows) == rows
    assert displaceable(rows) == []


def test_displacement_is_branch_restricted_when_branch_resolves(db_session):
    """Two of our lines serve air_handling company-wide; only one is
    confirmed_covered at the LA branch -- displacement must show only that
    one, not the full company-wide list."""
    p = _project(db_session, county="Los Angeles")
    covered = _line(db_session, "AAON", "air_handling")
    not_covered = _line(db_session, "BASX", "air_handling")
    db_session.add(ProductLineBranch(
        product_line_id=covered.id, branch="DMG Los Angeles", status="confirmed_covered",
        verified=True, source_detail="test fixture"))
    db_session.add(ProductLineBranch(
        product_line_id=not_covered.id, branch="DMG Los Angeles", status="confirmed_not_covered",
        verified=True, source_detail="test fixture"))
    db_session.commit()
    _competitor(db_session, "Trane", "air_handling", channel="factory_direct")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling", basis_of_design_manufacturer="Trane")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    r = resolve_displacement(db_session, p, mappings)[0]
    assert r.branch == "DMG Los Angeles"
    assert [l.name for l in r.our_lines] == ["AAON"]


def test_displacement_falls_back_to_company_wide_when_branch_unresolved(db_session):
    p = _project(db_session, county="Orange")  # not one of the 5 documented branches
    _line(db_session, "AAON", "air_handling")
    _competitor(db_session, "Trane", "air_handling", channel="factory_direct")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling", basis_of_design_manufacturer="Trane")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    r = resolve_displacement(db_session, p, mappings)[0]
    assert r.branch is None
    assert r.branch_note is not None
    assert [l.name for l in r.our_lines] == ["AAON"]  # unfiltered, company-wide


def test_displacement_flags_unconfirmed_competitor_research(db_session):
    p = _project(db_session)
    _line(db_session, "AAON", "air_handling")
    _competitor(db_session, "Trane", "air_handling", channel="factory_direct", status="unconfirmed")
    _entry(db_session, p.id, tag="RTU-1", role="air_handling", basis_of_design_manufacturer="Trane")

    mappings = map_project_schedule_to_line_card(db_session, p.id)
    r = resolve_displacement(db_session, p, mappings)[0]
    assert r.competitor_unconfirmed is True
    assert "unconfirmed" in r.competitor_state_label
