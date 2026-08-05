"""Contact identity: people and companies do not normalize the same way.

The ladder deduped people with `normalize_name`, which exists for company names and
strips legal suffixes, phase words, SPE codes and roman numerals. On people those
rules are destructive, and the failure is silent: two different people at one rung
merge and a callable human disappears from the board.
"""
import pytest
from sqlmodel import select

from app.ladder import build_ladder, ladder_distribution
from app.models import (
    Category, Firm, Project, ProjectSignal, RawDocument, Signal, SignalType, Stage,
    TriageResult,
)
from app.normalize import normalize_name, normalize_person_name


# --- the normalizer itself ------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("Heather Wessling-Grosz", "Heather Wessling Grosz"),   # hyphen vs space
    ("Jean-Luc Picard", "Jean Luc Picard"),
    ("John Smith Jr.", "John Smith Jr"),
    ("John Smith III", "John Smith"),                       # generational suffix
    ("Dana Reyes, PE", "Dana Reyes"),                       # credential
    ("  Kathy   Canfield ", "Kathy Canfield"),
])
def test_person_variants_are_one_identity(a, b):
    assert normalize_person_name(a) == normalize_person_name(b)


@pytest.mark.parametrize("name,expected", [
    ("Di Wu", "di wu"),           # every token is roman-numeral letters
    ("Max Li", "max li"),
    ("Clive Dix", "clive dix"),
    ("Livi Cid", "livi cid"),     # normalize_name reduced this to ""
    ("Mimi Cruz", "mimi cruz"),
    ("Vic Alvarez", "vic alvarez"),
])
def test_short_names_survive_intact(name, expected):
    """The real defect: normalize_name mauled these. 'Livi Cid' became the empty
    string, so any two such names collided."""
    assert normalize_person_name(name) == expected
    # And the company normalizer is why this needed its own function.
    assert normalize_name(name) != expected


def test_distinct_people_do_not_collide():
    """'Di Wu' and 'Vi Wu' are two people. Under normalize_name both became 'wu',
    because 'di' and 'vi' are made only of roman-numeral letters and were stripped."""
    assert normalize_person_name("Di Wu") != normalize_person_name("Vi Wu")
    assert normalize_name("Di Wu") == normalize_name("Vi Wu") == "wu"  # the bug, pinned


def test_person_normalizer_never_empties_a_real_name():
    for name in ("Livi Cid", "Di Wu", "Vic Mim", "I V X"):
        assert normalize_person_name(name).strip() != ""


# --- the ladder uses it --------------------------------------------------


def _project_with_people(db_session, people, source="ceqanet") -> Project:
    doc = RawDocument(source=source, source_uid="u1", url="https://x/1", title="t",
                      raw_text="filing", content_hash="h1",
                      triage_result=TriageResult.relevant)
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, stage=Stage.entitlement,
                 project_name="P", county="Storey", state="NV", named_people=people)
    db_session.add(sig)
    db_session.commit()
    proj = Project(name="P", category=Category.data_center, county="Storey", state="NV")
    db_session.add(proj)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id))
    db_session.commit()
    return proj


def test_ladder_keeps_two_distinct_short_named_people(db_session):
    """Regression: both classified to the same rung, both reduced to 'wu', so one
    was dropped and the board showed a single contact where two existed."""
    proj = _project_with_people(db_session, [
        {"name": "Di Wu", "title": "Planner", "org": "Storey County"},
        {"name": "Al Wu", "title": "Planner", "org": "Storey County"},
    ])
    rungs = build_ladder(db_session, proj)
    names = {r["name"] for r in rungs if r["kind"] == "person"}
    assert names == {"Di Wu", "Al Wu"}


def test_ladder_still_collapses_one_person_written_two_ways(db_session):
    proj = _project_with_people(db_session, [
        {"name": "Heather Wessling-Grosz", "title": "Planner", "org": "GOED"},
        {"name": "Heather Wessling Grosz", "title": "Planner", "org": "GOED"},
    ])
    people = [r for r in build_ladder(db_session, proj) if r["kind"] == "person"]
    assert len(people) == 1


def test_firms_keep_the_company_normalizer(db_session):
    """Firm rungs must keep normalize_name: legal suffixes are noise on a company,
    so 'Acme MEP, LLC' and 'Acme MEP' are one firm. The firms table enforces this
    with a UNIQUE constraint on name_norm, which is why a second row cannot even be
    inserted — the person normalizer would NOT collapse these, which is exactly why
    the two functions have to stay separate."""
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    assert normalize_name("Acme MEP, LLC") == normalize_name("Acme MEP")
    assert normalize_person_name("Acme MEP, LLC") != normalize_person_name("Acme MEP")

    db_session.add(Firm(name="Acme MEP, LLC", name_norm=normalize_name("Acme MEP, LLC"),
                        firm_type="mep"))
    db_session.commit()
    db_session.add(Firm(name="Acme MEP", name_norm=normalize_name("Acme MEP"),
                        firm_type="mep"))
    with _pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --- the diagnostic reports concentration -------------------------------


def test_distribution_reports_distinct_contacts_not_just_rungs(db_session, cfg):
    """Rung counts flattered the board: every project reaching 'a contact' read as a
    call, when they resolve to a handful of repeated names. One shared planner across
    three projects is one contact, not three."""
    from app.pipeline.size_score import run_size_score

    for i in range(3):
        doc = RawDocument(source="goed", source_uid=f"u{i}", url=f"https://x/{i}",
                          title="t", raw_text="filing", content_hash=f"h{i}",
                          triage_result=TriageResult.relevant)
        db_session.add(doc)
        db_session.commit()
        # Same human, written inconsistently across filings.
        spelling = ["Heather Wessling-Grosz", "Heather Wessling Grosz",
                    "heather wessling-grosz"][i]
        sig = Signal(raw_document_id=doc.id, signal_type=SignalType.abatement_application,
                     category=Category.industrial, stage=Stage.permitting,
                     project_name=f"Plant {i}", county="Washoe", state="NV",
                     named_people=[{"name": spelling, "title": "Consultant",
                                    "org": "GOED"}])
        db_session.add(sig)
        db_session.commit()
        proj = Project(name=f"Plant {i}", category=Category.industrial,
                       county="Washoe", state="NV")
        db_session.add(proj)
        db_session.commit()
        db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id))
        db_session.commit()
    run_size_score(db_session, cfg)

    dist = ladder_distribution(db_session)
    assert dist["n_projects"] == 3
    assert dist["counts"].get(None, 0) == 0        # all three "reachable"
    assert dist["distinct_contacts"] == 1          # ...by one person
    assert dist["distinct_people"] == 1
    assert dist["person_rungs"] == 3
    assert dist["top_contacts"][0][1] == 3

    from app.ladder import distribution_text
    text = distribution_text(dist)
    assert "distinct contacts behind those rows: 1" in text
    assert "3 reachable rows resolve to 1 contacts" in text
