"""Contact enrichment (Phase 5). See app/enrichment.py.

Covers: the worklist query (firms needing enrichment), the two distinct
write paths (confirmed-reachable vs. name-only-pending), and that an
enriched contact actually surfaces on the ladder rather than sitting inert
in a side table.
"""
import pytest

from app.enrichment import (
    firms_needing_enrichment, import_enriched_contact, import_pending_contact,
)
from app.ladder import build_ladder, contact_status, reach_tier
from app.models import (
    ACTIVE_STATUSES, Category, Contact, Firm, Project, ProjectFirm, Stage, Window,
)
from app.normalize import normalize_name


def _project(name, status="active"):
    return Project(name=name, category=Category.industrial, county="Los Angeles",
                   state="CA", status=status, in_territory=True, window=Window.PRE_BOD,
                   stage=Stage.entitlement)


def _firm(name, firm_type):
    return Firm(name=name, name_norm=normalize_name(name), firm_type=firm_type)


def test_worklist_excludes_firms_with_any_existing_contact(db_session):
    p = _project("Warehouse A")
    f1 = _firm("Acme Mechanical", "mech_contractor")
    f2 = _firm("Beta MEP", "mep")
    db_session.add(p); db_session.add(f1); db_session.add(f2)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p.id, firm_id=f1.id, role="mech_contractor"))
    db_session.add(ProjectFirm(project_id=p.id, firm_id=f2.id, role="engineer_of_record"))
    db_session.add(Contact(name="Someone", company="Acme Mechanical", phone="555-0100"))
    db_session.commit()

    worklist = firms_needing_enrichment(db_session)
    names = {w["firm"] for w in worklist}
    assert "Beta MEP" in names, "no contact yet -> should be on the worklist"
    assert "Acme Mechanical" not in names, "already has a contact -> should be excluded"


def test_worklist_excludes_consultants_and_developers(db_session):
    """A Director of Preconstruction doesn't work at an economic development
    agency -- see app/enrichment.py's ENRICHABLE_FIRM_TYPES."""
    p = _project("Industrial Park B")
    consultant = _firm("Some Development Alliance", "consultant")
    developer = _firm("Some Developer LLC", "developer")
    db_session.add(p); db_session.add(consultant); db_session.add(developer)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p.id, firm_id=consultant.id, role="consultant"))
    db_session.add(ProjectFirm(project_id=p.id, firm_id=developer.id, role="unknown"))
    db_session.commit()

    worklist = firms_needing_enrichment(db_session)
    assert worklist == []


def test_worklist_ranks_by_active_project_count(db_session):
    firm = _firm("Popular GC", "gc")
    db_session.add(firm)
    db_session.commit()
    for i in range(3):
        p = _project(f"Project {i}")
        db_session.add(p)
        db_session.commit()
        db_session.add(ProjectFirm(project_id=p.id, firm_id=firm.id, role="gc"))
    other = _firm("Quiet GC", "gc")
    db_session.add(other)
    p2 = _project("Project X")
    db_session.add(p2)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p2.id, firm_id=other.id, role="gc"))
    db_session.commit()

    worklist = firms_needing_enrichment(db_session)
    assert worklist[0]["firm"] == "Popular GC"
    assert worklist[0]["n_projects"] == 3


def test_import_enriched_contact_refuses_no_reach(db_session):
    with pytest.raises(ValueError, match="no phone and no email"):
        import_enriched_contact(db_session, firm_name="Acme", name="Jane Doe", source="apollo")


def test_import_enriched_contact_requires_known_source(db_session):
    with pytest.raises(ValueError, match="source must be"):
        import_enriched_contact(db_session, firm_name="Acme", name="Jane Doe",
                                source="manual", phone="555-0100")


def test_import_enriched_contact_writes_confirmed(db_session):
    c = import_enriched_contact(db_session, firm_name="Acme Mechanical", name="Jane Doe",
                                title="VP Operations", phone="555-0100", source="apollo")
    assert c.reach_status == "confirmed"
    assert c.source == "apollo"


def test_import_pending_contact_has_no_phone_or_email_by_design(db_session):
    """The function takes no phone/email parameters at all -- a caller cannot
    accidentally downgrade a real number into a 'pending' row."""
    c = import_pending_contact(db_session, firm_name="Ware Malcomb", name="Stanley Schultz",
                               title="Director of Data Center Design", source="lusha")
    assert c.reach_status == "pending"
    assert c.phone is None and c.email is None
    assert c.source == "lusha"


def test_pending_contact_surfaces_on_the_ladder_as_a_research_lead(db_session):
    """This is the whole point: a pending contact must upgrade the ladder from
    'no one' (a bare firm rung) to 'name_only' (a person, one phone call
    away) -- not sit inert in the contacts table."""
    p = _project("Data Center Project")
    firm = _firm("Ware Malcomb", "mep")
    db_session.add(p); db_session.add(firm)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p.id, firm_id=firm.id, role="engineer_of_record"))
    db_session.commit()

    # Before enrichment: bare firm rung, not a person -> "none".
    before = contact_status(db_session, p)
    assert before["status"] == "none"

    import_pending_contact(db_session, firm_name="Ware Malcomb", name="Stanley Schultz",
                           title="Director of Data Center Design", source="lusha",
                           project_ids=[p.id], firm_type="mep")

    after = contact_status(db_session, p)
    assert after["status"] == "name_only"
    assert after["n_people"] == 1
    ladder = build_ladder(db_session, p)
    person = next(r for r in ladder if r["kind"] == "person")
    assert person["name"] == "Stanley Schultz"
    assert person["title"] == "Director of Data Center Design"
    assert person["contact_source"] == "lusha"
    assert reach_tier(person) == 1, "no phone/email yet -> a name to research, not callable today"


def test_confirmed_contact_surfaces_as_callable_on_the_ladder(db_session):
    p = _project("Another Project")
    firm = _firm("Critchfield Mechanical", "mech_contractor")
    db_session.add(p); db_session.add(firm)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=p.id, firm_id=firm.id, role="mech_contractor"))
    db_session.commit()

    import_enriched_contact(db_session, firm_name="Critchfield Mechanical",
                            name="Roger Ladich", title="Project Director",
                            phone="555-0199", source="lusha",
                            project_ids=[p.id], firm_type="mech_contractor")

    cs = contact_status(db_session, p)
    assert cs["status"] == "contactable"
    assert cs["best_reachable"]["name"] == "Roger Ladich"
    assert cs["best_reachable"]["contact_source"] == "lusha"
