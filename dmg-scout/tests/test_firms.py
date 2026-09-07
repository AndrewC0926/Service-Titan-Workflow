"""Firm roster role handling: ROLE_TO_TYPE, resolve_signal_firms linking new
architect/mep_engineer/civil_engineer/structural_engineer roles the same way
the original five roles already work, and the extraction schema actually
offering the model those roles. See app/firms.py and app/schemas.py."""
from sqlmodel import select

from app.firms import ROLE_TO_TYPE, resolve_signal_firms
from app.models import Firm, Project, ProjectFirm
from app.schemas import EXTRACTION_JSON_SCHEMA

NEW_ROLES = ["architect", "mep_engineer", "civil_engineer", "structural_engineer"]


def _project(session, name="Test Project", **kw):
    p = Project(name=name, **kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_extraction_schema_offers_the_new_roles():
    role_enum = EXTRACTION_JSON_SCHEMA["properties"]["named_firms"]["items"]["properties"]["role"]["enum"]
    for role in NEW_ROLES + ["engineer_of_record", "gc", "mech_contractor", "developer",
                             "consultant", "unknown"]:
        assert role in role_enum


def test_role_to_type_maps_every_new_role():
    assert ROLE_TO_TYPE["architect"] == "architect"
    assert ROLE_TO_TYPE["mep_engineer"] == "mep"
    assert ROLE_TO_TYPE["civil_engineer"] == "civil"
    assert ROLE_TO_TYPE["structural_engineer"] == "structural"
    # engineer_of_record and mep_engineer are the same discipline for roster purposes
    assert ROLE_TO_TYPE["engineer_of_record"] == ROLE_TO_TYPE["mep_engineer"] == "mep"


def test_resolve_signal_firms_creates_a_new_firm_with_the_right_type_per_role(db_session):
    project = _project(db_session)
    for role in NEW_ROLES:
        named_firms = [{"name": f"Acme {role}", "role": role}]
        linked = resolve_signal_firms(db_session, project.id, named_firms)
        assert linked == 1
    db_session.commit()

    for role in NEW_ROLES:
        firm = db_session.exec(select(Firm).where(Firm.name == f"Acme {role}")).first()
        assert firm is not None
        assert firm.firm_type == ROLE_TO_TYPE[role]
        pf = db_session.exec(
            select(ProjectFirm).where(
                ProjectFirm.project_id == project.id, ProjectFirm.firm_id == firm.id)
        ).first()
        assert pf.role == role


def test_architect_and_mep_engineer_are_distinct_firms_even_with_similar_names(db_session):
    """A civil/structural/architect role must never collapse into engineer_of_record's
    firm_type bucket by accident -- each keeps its own distinct roster type."""
    project = _project(db_session)
    resolve_signal_firms(db_session, project.id, [
        {"name": "Acme Architecture", "role": "architect"},
        {"name": "Acme MEP", "role": "mep_engineer"},
        {"name": "Acme Structural", "role": "structural_engineer"},
    ])
    db_session.commit()

    from sqlmodel import select
    firms = {f.name: f.firm_type for f in db_session.exec(select(Firm)).all()}
    assert firms["Acme Architecture"] == "architect"
    assert firms["Acme MEP"] == "mep"
    assert firms["Acme Structural"] == "structural"
