"""Firm index and firm profile: pipeline-touched ranking, equipment value
rollup, four-state contact status, recency/staleness, coverage-gap
breakdown, and firm-pair co-occurrence. See app/firmprofile.py.

No LLM, no scoring invented here -- every assertion below traces to a row
this test itself created."""
from datetime import timedelta

from app.firmprofile import contact_state, firm_index, firm_profile
from app.models import Contact, Firm, Outreach, Project, ProjectFirm, utcnow


def _firm(session, name, firm_type="mep", aliases=None):
    f = Firm(name=name, name_norm=name.lower(), firm_type=firm_type, aliases=aliases or [])
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


def _project(session, name, **kw):
    p = Project(name=name, **kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _link(session, project, firm, role="unknown"):
    session.add(ProjectFirm(project_id=project.id, firm_id=firm.id, role=role))
    session.commit()


# --- contact_state: pure function --------------------------------------


def test_contact_state_no_contact():
    assert contact_state([], has_outreach=False) == "no_contact"


def test_contact_state_name_only_when_none_confirmed():
    pending = Contact(name="X", reach_status="pending")
    assert contact_state([pending], has_outreach=False) == "name_only"


def test_contact_state_reachable_when_any_confirmed():
    pending = Contact(name="X", reach_status="pending")
    confirmed = Contact(name="Y", reach_status="confirmed", phone="555-0100")
    assert contact_state([pending, confirmed], has_outreach=False) == "reachable"


def test_contact_state_worked_with_beats_everything_else():
    confirmed = Contact(name="Y", reach_status="confirmed", phone="555-0100")
    assert contact_state([confirmed], has_outreach=True) == "worked_with"
    assert contact_state([], has_outreach=True) == "worked_with"


# --- firm_index: ranking -------------------------------------------------


def test_firm_index_ranks_by_project_count_not_alphabetical(db_session, cfg):
    zebra = _firm(db_session, "Zebra Engineering")
    apex = _firm(db_session, "Apex Engineering")
    p1 = _project(db_session, "P1")
    p2 = _project(db_session, "P2")
    p3 = _project(db_session, "P3")
    _link(db_session, p1, zebra)
    _link(db_session, p1, apex)
    _link(db_session, p2, apex)
    _link(db_session, p3, apex)

    idx = firm_index(db_session)
    names = [r["firm"].name for r in idx["rows"]]
    assert names[0] == "Apex Engineering", "3 projects must rank above 1, alphabetical order be damned"
    assert names.index("Apex Engineering") < names.index("Zebra Engineering")


def test_firm_index_project_count_dedupes_multiple_roles_on_one_project(db_session, cfg):
    """A firm linked to the same project under two roles (measured: this
    happens in production -- Ware Malcomb as both engineer_of_record and
    unknown on one job) must count as ONE project, not two."""
    firm = _firm(db_session, "Ware Malcomb")
    p = _project(db_session, "DJT4 Parcel Delivery Facility")
    _link(db_session, p, firm, role="engineer_of_record")
    _link(db_session, p, firm, role="unknown")

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["n_projects"] == 1


# --- firm_index: equipment value -----------------------------------------


def test_firm_index_sums_equipment_value_and_counts_priced_projects(db_session, cfg):
    firm = _firm(db_session, "Southland Industries", firm_type="mech_contractor")
    priced1 = _project(db_session, "Priced One", equipment_value_low=100.0, equipment_value_high=200.0)
    priced2 = _project(db_session, "Priced Two", equipment_value_low=300.0, equipment_value_high=400.0)
    unpriced = _project(db_session, "Unpriced")
    for p in (priced1, priced2, unpriced):
        _link(db_session, p, firm)

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["n_projects"] == 3
    assert row["n_valued"] == 2
    assert row["value_low"] == 400.0
    assert row["value_high"] == 600.0


# --- firm_index: contact state per firm -----------------------------------


def test_firm_index_contact_state_reflects_matched_contacts(db_session, cfg):
    firm = _firm(db_session, "Critchfield Mechanical", firm_type="mech_contractor")
    p = _project(db_session, "Some Project")
    _link(db_session, p, firm)
    db_session.add(Contact(name="Roger Ladich", company="Critchfield Mechanical", reach_status="pending"))
    db_session.commit()

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["contact_state"] == "name_only"
    assert row["n_contacts"] == 1


def test_firm_index_worked_with_requires_outreach_scoped_to_this_firms_project(db_session, cfg):
    """The narrow definition: an outreach record must be on ONE of this
    firm's own projects AND name one of this firm's own contacts. A call
    logged against an unrelated project must not count."""
    firm = _firm(db_session, "Holder Construction", firm_type="gc")
    their_project = _project(db_session, "Their Project")
    other_project = _project(db_session, "Someone Elses Project")
    _link(db_session, their_project, firm)
    contact = Contact(name="Beth Lowry", company="Holder Construction", reach_status="confirmed", phone="555-0101")
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)

    # Outreach on a DIFFERENT project must not count.
    db_session.add(Outreach(project_id=other_project.id, contact_id=contact.id, channel="call"))
    db_session.commit()
    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["contact_state"] == "reachable", "outreach on an unrelated project must not count as worked_with"

    # Outreach on THEIR project, with THEIR contact, must count.
    db_session.add(Outreach(project_id=their_project.id, contact_id=contact.id, channel="call"))
    db_session.commit()
    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["contact_state"] == "worked_with"


# --- firm_index: recency / staleness --------------------------------------


def test_firm_index_flags_stale_past_twelve_months(db_session, cfg):
    from app.staleness import DEFAULT_UNVERIFIED_MONTHS
    firm = _firm(db_session, "Old News Engineering")
    stale_date = utcnow() - timedelta(days=int(DEFAULT_UNVERIFIED_MONTHS * 30.44) + 5)
    p = _project(db_session, "Old Project", last_signal_at=stale_date)
    _link(db_session, p, firm)

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["stale"] is True
    assert row["last_signal_at"] == stale_date


def test_firm_index_no_dated_signal_is_stale_not_fresh(db_session, cfg):
    """No evidence date at all must never render the same as fresh -- same
    discipline as app/staleness.py's stage age."""
    firm = _firm(db_session, "No Signal Engineering")
    p = _project(db_session, "Undated Project")  # last_signal_at defaults to None
    _link(db_session, p, firm)

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["last_signal_at"] is None
    assert row["stale"] is True


def test_firm_index_recent_signal_is_not_stale(db_session, cfg):
    firm = _firm(db_session, "Fresh Engineering")
    p = _project(db_session, "Fresh Project", last_signal_at=utcnow() - timedelta(days=10))
    _link(db_session, p, firm)

    idx = firm_index(db_session)
    row = next(r for r in idx["rows"] if r["firm"].id == firm.id)
    assert row["stale"] is False


# --- firm_index: coverage-gap breakdown -----------------------------------


def test_firm_index_pins_mep_and_mech_contractor_first_regardless_of_count(db_session, cfg):
    mep = _firm(db_session, "One MEP Firm", firm_type="mep")
    mc = _firm(db_session, "One Mech Contractor", firm_type="mech_contractor")
    developers = [_firm(db_session, f"Developer {i}", firm_type="developer") for i in range(5)]
    p = _project(db_session, "Shared Project")
    for f in [mep, mc, *developers]:
        _link(db_session, p, f)

    idx = firm_index(db_session)
    types_in_order = [t for t, _n in idx["type_breakdown"]]
    assert types_in_order[0] == "mep"
    assert types_in_order[1] == "mech_contractor"
    assert "developer" in types_in_order[2:]
    # Developer count (5) dwarfs mep/mech_contractor (1 each) -- pinning
    # must not be confused with "biggest count sorts wherever."
    counts = dict(idx["type_breakdown"])
    assert counts["developer"] == 5
    assert counts["mep"] == 1


# --- firm_index: pairings -------------------------------------------------


def test_firm_index_pairings_count_shared_projects(db_session, cfg):
    gc = _firm(db_session, "Bryco General Contracting", firm_type="gc")
    mep = _firm(db_session, "Ware Malcomb", firm_type="mep")
    for i in range(3):
        p = _project(db_session, f"Shared {i}")
        _link(db_session, p, gc)
        _link(db_session, p, mep)

    idx = firm_index(db_session)
    pair = next(pr for pr in idx["pairings"]
               if {pr["a"].id, pr["b"].id} == {gc.id, mep.id})
    assert pair["n"] == 3
    assert pair["is_gc_mep"] is True


def test_firm_index_pairings_dedupe_same_firm_two_roles_does_not_self_pair(db_session, cfg):
    """The exact bug this guards: Ware Malcomb appears under
    engineer_of_record AND unknown on the same project in production. Firm
    pairing must never produce a (firm, firm) self-pair."""
    firm = _firm(db_session, "Ware Malcomb")
    p = _project(db_session, "Solo Project")
    _link(db_session, p, firm, role="engineer_of_record")
    _link(db_session, p, firm, role="unknown")

    idx = firm_index(db_session)
    assert all(pr["a"].id != pr["b"].id for pr in idx["pairings"])


def test_firm_index_pairings_gc_mep_sorted_before_larger_non_gc_mep_pairs(db_session, cfg):
    gc = _firm(db_session, "Some GC", firm_type="gc")
    mep = _firm(db_session, "Some MEP", firm_type="mep")
    dev = _firm(db_session, "Some Developer", firm_type="developer")
    consultant = _firm(db_session, "Some Consultant", firm_type="consultant")

    p1 = _project(db_session, "GC-MEP job")
    _link(db_session, p1, gc)
    _link(db_session, p1, mep)

    # A non-GC/MEP pair with MORE shared projects.
    for i in range(5):
        p = _project(db_session, f"Dev-consultant job {i}")
        _link(db_session, p, dev)
        _link(db_session, p, consultant)

    idx = firm_index(db_session)
    assert idx["pairings"][0]["is_gc_mep"] is True, "GC-MEP must sort first even against a bigger non-GC/MEP pair"


def test_firm_index_empty_when_no_links(db_session, cfg):
    idx = firm_index(db_session)
    assert idx == {"rows": [], "total_linked": 0, "type_breakdown": [], "pairings": []}


# --- firm_profile: equipment value + recency on the detail page ----------


def test_firm_profile_aggregates_equipment_value_and_recency(db_session, cfg):
    firm = _firm(db_session, "Ware Malcomb")
    priced = _project(db_session, "Priced", equipment_value_low=100.0, equipment_value_high=200.0,
                      last_signal_at=utcnow() - timedelta(days=5))
    unpriced = _project(db_session, "Unpriced")
    _link(db_session, priced, firm)
    _link(db_session, unpriced, firm)

    prof = firm_profile(db_session, firm.id)
    assert prof.n_projects == 2
    assert prof.n_valued_projects == 1
    assert prof.equipment_value_low == 100.0
    assert prof.equipment_value_high == 200.0
    assert prof.is_stale is False
    assert prof.role_by_project[priced.id] == "unknown"


def test_firm_profile_unknown_firm_returns_none(db_session, cfg):
    assert firm_profile(db_session, 999999) is None
