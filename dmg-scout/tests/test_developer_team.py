"""Developer's usual design team -- see app/developer_team.py and
DeveloperDesignTeam's docstring in app/models.py."""
import pytest

from app.developer_team import (
    add_manual_team_entry, seed_from_project_firms, usual_team_for_developer,
)
from app.models import Firm, Project, ProjectFirm


def _firm(session, name, firm_type="mep"):
    f = Firm(name=name, name_norm=name.lower(), firm_type=firm_type)
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


def _project(session, name, developer=None, **kw):
    p = Project(name=name, developer=developer, **kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _link(session, project, firm, role):
    session.add(ProjectFirm(project_id=project.id, firm_id=firm.id, role=role))
    session.commit()


# --- seed_from_project_firms: the repeatable rollup ------------------------


def test_seed_rolls_up_engineer_of_record_by_developer(db_session):
    dev = "Vantage Data Centers"
    mep = _firm(db_session, "kW Mission Critical Engineering", firm_type="mep")
    p1 = _project(db_session, "Project A", developer=dev)
    p2 = _project(db_session, "Project B", developer=dev)
    _link(db_session, p1, mep, "engineer_of_record")
    _link(db_session, p2, mep, "engineer_of_record")

    written = seed_from_project_firms(db_session)
    assert written == 1

    team = usual_team_for_developer(db_session, dev)
    assert len(team) == 1
    assert team[0]["firm"] == "kW Mission Critical Engineering"
    assert team[0]["role"] == "engineer_of_record"
    assert team[0]["n"] == 2
    assert team[0]["source"] == "extracted"


def test_seed_ignores_roles_outside_the_design_team_scope(db_session):
    """A GC, mech contractor, or civil/structural engineer never becomes a
    'usual team' entry -- see DESIGN_TEAM_ROLES's own comment."""
    dev = "Some Developer LLC"
    gc = _firm(db_session, "Some GC", firm_type="gc")
    civil = _firm(db_session, "Some Civil Firm", firm_type="civil")
    p = _project(db_session, "Project C", developer=dev)
    _link(db_session, p, gc, "gc")
    _link(db_session, p, civil, "civil_engineer")

    seed_from_project_firms(db_session)
    assert usual_team_for_developer(db_session, dev) == []


def test_seed_groups_developers_by_normalized_name(db_session):
    """'Vantage Data Centers' and 'Vantage Data Centers, LLC' are the same
    developer for rollup purposes -- see app.normalize.normalize_name."""
    mep = _firm(db_session, "Acme MEP", firm_type="mep")
    p1 = _project(db_session, "Project A", developer="Vantage Data Centers")
    p2 = _project(db_session, "Project B", developer="Vantage Data Centers, LLC")
    _link(db_session, p1, mep, "mep_engineer")
    _link(db_session, p2, mep, "mep_engineer")

    seed_from_project_firms(db_session)
    team = usual_team_for_developer(db_session, "Vantage Data Centers")
    assert len(team) == 1
    assert team[0]["n"] == 2


def test_seed_is_fully_repeatable_and_never_duplicates(db_session):
    mep = _firm(db_session, "Acme MEP", firm_type="mep")
    p = _project(db_session, "Project A", developer="Some Dev")
    _link(db_session, p, mep, "mep_engineer")

    seed_from_project_firms(db_session)
    seed_from_project_firms(db_session)
    seed_from_project_firms(db_session)
    assert len(usual_team_for_developer(db_session, "Some Dev")) == 1


def test_seed_never_touches_manual_rows(db_session):
    add_manual_team_entry(db_session, "Some Dev", "Hand-Entered Firm", "architect",
                          reason="GC told me on site", confirmed_by="Andy")
    seed_from_project_firms(db_session)
    team = usual_team_for_developer(db_session, "Some Dev")
    assert len(team) == 1
    assert team[0]["source"] == "manual"
    assert team[0]["confirmed_by"] == "Andy"


# --- usual_team_for_developer: empty cases ---------------------------------


def test_usual_team_empty_for_unknown_developer(db_session):
    assert usual_team_for_developer(db_session, "Nobody Ever Heard Of LLC") == []


def test_usual_team_empty_for_blank_developer(db_session):
    assert usual_team_for_developer(db_session, "") == []
    assert usual_team_for_developer(db_session, None or "") == []


# --- add_manual_team_entry: validation + upgrade-in-place ------------------


def test_manual_entry_requires_reason(db_session):
    with pytest.raises(ValueError, match="reason"):
        add_manual_team_entry(db_session, "Some Dev", "Some Firm", "architect",
                              reason="   ", confirmed_by="Andy")


def test_manual_entry_requires_confirmed_by(db_session):
    with pytest.raises(ValueError, match="name"):
        add_manual_team_entry(db_session, "Some Dev", "Some Firm", "architect",
                              reason="told me directly", confirmed_by="")


def test_manual_entry_rejects_role_outside_design_team_scope(db_session):
    with pytest.raises(ValueError, match="role"):
        add_manual_team_entry(db_session, "Some Dev", "Some Firm", "gc",
                              reason="told me directly", confirmed_by="Andy")


def test_manual_entry_creates_a_new_firm_when_none_matches(db_session):
    row = add_manual_team_entry(db_session, "Some Dev", "Brand New Firm", "mep_engineer",
                                reason="rep told me on a call", confirmed_by="Andy")
    assert row.source == "manual"
    firm = db_session.get(Firm, row.firm_id)
    assert firm.name == "Brand New Firm"
    assert firm.firm_type == "mep"


def test_manual_entry_upgrades_an_existing_extracted_row_in_place(db_session):
    mep = _firm(db_session, "Acme MEP", firm_type="mep")
    p = _project(db_session, "Project A", developer="Some Dev")
    _link(db_session, p, mep, "mep_engineer")
    seed_from_project_firms(db_session)
    assert usual_team_for_developer(db_session, "Some Dev")[0]["source"] == "extracted"

    add_manual_team_entry(db_session, "Some Dev", "Acme MEP", "mep_engineer",
                          reason="confirmed by the developer directly", confirmed_by="Andy")

    team = usual_team_for_developer(db_session, "Some Dev")
    assert len(team) == 1  # upgraded in place, not duplicated
    assert team[0]["source"] == "manual"
    assert team[0]["confirmed_by"] == "Andy"
