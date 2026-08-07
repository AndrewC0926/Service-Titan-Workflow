"""Saved searches, plain-language search, and firm profiles.

The three share one rule: a filter that does not understand part of its input must
REFUSE, never widen. A saved search that quietly matches everything, or an NL
question silently answered with a filter nobody asked for, both look exactly like
a correct answer — and the rep keeps trusting a question that is no longer the
question they asked.
"""
import pytest
from sqlmodel import select

from app.models import (
    Category, Contact, Firm, Outreach, Project, ProjectContact, ProjectFirm,
    SavedSearch, Stage, Window,
)
from app.searches import UnknownCriterion, evaluate, run_search, validate


def _p(session, name, **kw):
    base = dict(category=Category.industrial, stage=Stage.entitlement, status="active",
                in_territory=True, score=0.5, window=Window.PRE_BOD)
    p = Project(name=name, **{**base, **kw})
    session.add(p)
    session.commit()
    return p


# --- criteria validation ----------------------------------------------------

def test_unknown_criterion_raises_rather_than_widening():
    """The whole safety property. A filter that ignores what it does not
    understand matches everything."""
    with pytest.raises(UnknownCriterion) as exc:
        validate({"county": "Storey", "mw_over": 10})
    assert "mw_over" in str(exc.value)
    assert "matches everything" in str(exc.value)


def test_known_criteria_validate():
    validate({"county": "Storey", "min_mw": 10, "names": ["Southland"]})


# --- the three questions that were actually asked ---------------------------

def test_over_10_mw_in_storey_county(db_session):
    hit = _p(db_session, "Big Storey campus", county="Storey", state="NV", mw_total=42.0,
             category=Category.data_center)
    _p(db_session, "Small Storey shed", county="Storey", state="NV", mw_total=3.0)
    _p(db_session, "Big elsewhere", county="Washoe", state="NV", mw_total=99.0)
    found = run_search(db_session, {"county": "Storey", "min_mw": 10})
    assert [p.id for p in found] == [hit.id]


def test_mw_threshold_reads_either_column(db_session):
    """A project stating only total MW must not be silently excluded by a
    question about size."""
    a = _p(db_session, "States IT load", county="Storey", state="NV", mw_it=25.0)
    b = _p(db_session, "States total only", county="Storey", state="NV", mw_total=25.0)
    found = {p.id for p in run_search(db_session, {"county": "Storey", "min_mw": 10})}
    assert found == {a.id, b.id}


def test_naming_a_firm_looks_beyond_the_developer_field(db_session):
    """"Anything naming Southland" is a question about who is ON the job.

    Most of those parties never appear in Project.developer — matching only that
    field answers a much narrower question while looking like it answered this one.
    """
    as_developer = _p(db_session, "Job A", developer="Southland Industrial")
    via_firm = _p(db_session, "Job B", developer="Someone Else")
    _p(db_session, "Job C", developer="Unrelated Co")

    firm = Firm(name="Southland Industrial", name_norm="southland industrial",
                firm_type="mech_contractor", aliases=["Southland"])
    db_session.add(firm)
    db_session.commit()
    db_session.add(ProjectFirm(project_id=via_firm.id, firm_id=firm.id,
                               role="mech_contractor"))
    db_session.commit()

    found = {p.id for p in run_search(db_session, {"names": ["Southland"]})}
    assert found == {as_developer.id, via_firm.id}


def test_stage_change_alert_needs_a_previous_state(db_session):
    """"Changed" is a claim about two points in time.

    The first evaluation must report nothing as changed — announcing every current
    row as a change would make the first digest after creating a search pure
    noise, which trains people to ignore the section.
    """
    p = _p(db_session, "Watched", county="Storey", state="NV", mw_total=20.0)
    s = SavedSearch(name="Storey over 10", criteria={"county": "Storey", "min_mw": 10},
                    alert_on_change=True)
    db_session.add(s)
    db_session.commit()

    first = evaluate(db_session, s)
    assert first.changed == [] and first.entered == []
    assert first.projects == [p]

    p.stage = Stage.design
    db_session.add(p)
    db_session.commit()

    second = evaluate(db_session, s)
    assert [(pr.id, was, now) for pr, was, now in second.changed] == [
        (p.id, "entitlement", "design")]


def test_a_project_entering_the_search_is_reported_separately(db_session):
    """Entering is a new opportunity; moving stage is a closing window on one you
    already knew about. They prompt different actions."""
    _p(db_session, "Already there", county="Storey", state="NV", mw_total=20.0)
    s = SavedSearch(name="Storey", criteria={"county": "Storey", "min_mw": 10})
    db_session.add(s)
    db_session.commit()
    evaluate(db_session, s)

    newcomer = _p(db_session, "Just appeared", county="Storey", state="NV", mw_total=50.0)
    res = evaluate(db_session, s)
    assert [p.id for p in res.entered] == [newcomer.id]
    assert res.changed == []


def test_broken_search_is_reported_not_skipped(db_session):
    """A saved search that cannot run is a question the rep believes is being
    asked and is not."""
    from app.searches import digest_section
    _p(db_session, "Anything")
    db_session.add(SavedSearch(name="Typo search", criteria={"mw_over": 10}, alert=True))
    db_session.commit()
    text, count = digest_section(db_session)
    assert "BROKEN" in text and "Typo search" in text and count == 1


# --- firm profiles ----------------------------------------------------------

def test_firm_profile_answers_the_four_questions(db_session):
    from app.firmprofile import firm_profile

    firm = Firm(name="Southland Industrial", name_norm="southland industrial",
                firm_type="mech_contractor")
    partner = Firm(name="HPA Inc", name_norm="hpa", firm_type="gc")
    db_session.add(firm)
    db_session.add(partner)
    db_session.commit()

    a = _p(db_session, "Hemet Logistics", county="Riverside", state="CA")
    b = _p(db_session, "Perris Distribution", county="Riverside", state="CA")
    for proj in (a, b):
        db_session.add(ProjectFirm(project_id=proj.id, firm_id=firm.id,
                                   role="mech_contractor"))
    db_session.add(ProjectFirm(project_id=a.id, firm_id=partner.id, role="gc"))
    db_session.commit()

    contact = Contact(name="Dana Reyes", company="Southland Industrial",
                      title="Preconstruction", phone="555-0100")
    db_session.add(contact)
    db_session.commit()
    db_session.add(Outreach(project_id=a.id, contact_id=contact.id, channel="call",
                            notes="talked chillers"))
    db_session.commit()

    prof = firm_profile(db_session, firm.id)
    assert prof.n_projects == 2                              # what they work on
    assert prof.counties.most_common(1)[0][0] == "Riverside, CA"   # where
    assert prof.collaborators[0][0].id == partner.id          # with whom
    assert len(prof.outreach) == 1                            # my history
    assert prof.days_since_touch is not None


def test_history_is_not_overstated(db_session):
    """A call to the developer about a job this firm is also on is not history
    with this firm. This is the one number nobody should be able to fake."""
    from app.firmprofile import firm_profile

    firm = Firm(name="ACCO Engineered Systems", name_norm="acco engineered systems",
                firm_type="mech_contractor")
    db_session.add(firm)
    db_session.commit()
    p = _p(db_session, "Shared job")
    db_session.add(ProjectFirm(project_id=p.id, firm_id=firm.id, role="mech_contractor"))
    other = Contact(name="Someone Else", company="Developer Co", phone="555-0200")
    db_session.add(other)
    db_session.commit()
    db_session.add(Outreach(project_id=p.id, contact_id=other.id, channel="call",
                            notes="spoke to the developer"))
    db_session.commit()

    prof = firm_profile(db_session, firm.id)
    assert prof.outreach == []
    assert prof.last_touch is None


# --- plain-language search --------------------------------------------------

def test_nl_search_emits_criteria_not_sql(monkeypatch):
    """The model never writes SQL and never touches the database.

    It fills a closed vocabulary that is validated before execution, so a
    hallucinated filter is an error rather than a silent match-everything.
    """
    from app import nlsearch

    monkeypatch.setattr(nlsearch, "_tool_call", lambda *a, **k: {
        "county": "Storey", "min_mw": 10, "reading": "Storey County, over 10 MW",
        "unsupported": []})
    out = nlsearch.interpret("anything over 10 MW in Storey County")
    assert out["criteria"] == {"county": "Storey", "min_mw": 10}
    assert out["unsupported"] == []


def test_nl_search_reports_what_it_cannot_express(monkeypatch):
    """Approximating an unanswerable question into a nearby filter is worse than
    saying it cannot be answered — the rows look like an answer."""
    from app import nlsearch

    monkeypatch.setattr(nlsearch, "_tool_call", lambda *a, **k: {
        "county": "Riverside", "reading": "Riverside",
        "unsupported": ["where the mechanical engineer is named"]})
    out = nlsearch.interpret("Riverside jobs where the mechanical engineer is named")
    assert out["criteria"] == {"county": "Riverside"}
    assert out["unsupported"] == ["where the mechanical engineer is named"]


def test_nl_search_rejects_an_invented_filter(monkeypatch):
    from app import nlsearch

    monkeypatch.setattr(nlsearch, "_tool_call", lambda *a, **k: {
        "county": "Storey", "category": "aerospace", "reading": "x"})
    with pytest.raises(UnknownCriterion):
        nlsearch.interpret("aerospace in Storey")


def test_nl_search_refuses_to_answer_nothing(monkeypatch):
    """An empty filter would render as "no matches" and read like an answer."""
    from app import nlsearch

    monkeypatch.setattr(nlsearch, "_tool_call", lambda *a, **k: {"reading": "unclear"})
    with pytest.raises(nlsearch.Uninterpretable):
        nlsearch.interpret("what is the weather")
