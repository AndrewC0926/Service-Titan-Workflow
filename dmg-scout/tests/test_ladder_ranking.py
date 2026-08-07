"""Phase B: the ladder is ranked by reachability first, proximity second.

The ladder was ordered by proximity to the mechanical specification decision,
which is the right order for a research list and the wrong one for a call list.
On 26 of 31 contactable rows the headline contact had no phone number while a
callable one sat further down the same ladder — the rep got a name and no way to
use it.

These tests pin the new order and, just as importantly, pin what the reorder must
NOT change: the proximity measurement. `distribution_text` answers "how close to
the spec decision does this board get", and that question has the same answer
before and after. Keying it on the headline rung would have silently rewritten a
gate metric.
"""
import pytest

from app.ladder import (
    build_ladder, contact_status, contactability_text, ladder_distribution,
    reach_tier,
)
from app.models import (
    Category, Project, ProjectSignal, RawDocument, Signal, SignalType, Stage,
    TriageResult, Window,
)


# --- ordering ---------------------------------------------------------------

def _rung(rung: int, name: str, kind: str = "person",
          phone: str | None = None, email: str | None = None) -> dict:
    return {"rung": rung, "rung_label": "", "name": name, "title": None,
            "company": None, "source_url": None, "kind": kind,
            "phone": phone, "email": email}


def test_reach_tier_separates_a_call_from_a_research_task():
    assert reach_tier(_rung(7, "Planner", phone="555-0100")) == 0
    assert reach_tier(_rung(7, "Planner", email="p@city.gov")) == 0
    assert reach_tier(_rung(1, "EOR")) == 1                      # name, no method
    assert reach_tier(_rung(2, "Acme MEP", kind="firm")) == 2    # no individual
    assert reach_tier(_rung(5, "Acme (hiring)", kind="evidence")) == 2


def test_callable_planner_outranks_unreachable_eor():
    """The whole point: rung 7 with a phone beats rung 1 with nothing."""
    from app.ladder import _reachability_first
    ladder = sorted([_rung(1, "Unreachable EOR"),
                     _rung(7, "Callable Planner", phone="555-0100")],
                    key=_reachability_first)
    assert [r["name"] for r in ladder] == ["Callable Planner", "Unreachable EOR"]


def test_proximity_still_decides_inside_a_tier():
    """No information is lost — it is subordinated. Among callable people the
    closest to the spec decision is still first."""
    from app.ladder import _reachability_first
    ladder = sorted([_rung(7, "Planner", phone="555-0100"),
                     _rung(1, "EOR", phone="555-0200"),
                     _rung(4, "GC Precon", email="gc@x.com")],
                    key=_reachability_first)
    assert [r["name"] for r in ladder] == ["EOR", "GC Precon", "Planner"]


def test_people_still_outrank_firms_at_the_same_rung():
    from app.ladder import _reachability_first
    ladder = sorted([_rung(2, "Acme MEP", kind="firm"),
                     _rung(2, "Acme (hiring)", kind="evidence")],
                    key=_reachability_first)
    assert ladder[0]["name"] == "Acme MEP"  # firm before bare evidence


# --- against the real builder ----------------------------------------------

@pytest.fixture()
def project_with_two_contacts(db_session):
    """A name-only mechanical EOR (rung 1) and a callable planner (rung 7)."""
    doc = RawDocument(source="ceqanet", source_uid="u1", url="https://x/1",
                      title="NOP", raw_text="t", content_hash="h1")
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 triage_result=TriageResult.relevant, category=Category.data_center,
                 project_name="Test Campus", county="Los Angeles", state="CA",
                 stage=Stage.entitlement,
                 named_people=[
                     {"name": "Dana Reyes", "title": "Mechanical Engineer, PE",
                      "org": "Reyes Mechanical"},
                     {"name": "Sam Okafor", "title": "Senior Planner",
                      "org": "County of Los Angeles", "phone": "555-0100"},
                 ])
    proj = Project(name="Test Campus", category=Category.data_center,
                   county="Los Angeles", state="CA", stage=Stage.entitlement,
                   status="active", in_territory=True, score=0.9,
                   window=Window.PRE_BOD)
    db_session.add(sig)
    db_session.add(proj)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id,
                                 match_confidence=1.0, match_method="manual"))
    db_session.commit()
    return proj


def test_board_headlines_the_person_it_can_call(db_session, project_with_two_contacts):
    ladder = build_ladder(db_session, project_with_two_contacts)
    assert ladder[0]["name"] == "Sam Okafor"
    assert ladder[0]["phone"] == "555-0100"
    # the EOR is still there, one row down — subordinated, not dropped
    assert "Dana Reyes" in [r["name"] for r in ladder]


def test_headline_and_callable_contact_are_the_same_row(db_session,
                                                        project_with_two_contacts):
    cs = contact_status(db_session, project_with_two_contacts)
    assert cs["status"] == "contactable"
    assert cs["top_rung_is_reachable"] is True
    assert cs["best_reachable"]["name"] == "Sam Okafor"


def test_proximity_given_up_is_reported(db_session, project_with_two_contacts):
    """The diagnostic that replaces top_rung_is_reachable: what the reorder cost."""
    cs = contact_status(db_session, project_with_two_contacts)
    assert cs["closest_person_rung"] == 1     # the EOR
    assert cs["proximity_given_up"] == 6      # calling rung 7 instead


def test_reorder_does_not_change_the_proximity_measurement(
        db_session, project_with_two_contacts):
    """The gate metric must mean the same thing before and after the reorder.

    This project headlines rung 7 and still reaches rung 1. Keyed on the headline,
    "mechanical EOR named" would read 0 here and the reorder would look like a
    loss of coverage it did not cause.
    """
    dist = ladder_distribution(db_session)
    assert dist["counts"] == {1: 1}           # closest rung reached
    assert dist["headline_counts"] == {7: 1}  # rung of the contact we display
    assert dist["per_project"][0]["closest_rung"] == 1
    assert dist["per_project"][0]["best_rung"] == 7


def test_contactability_text_reports_the_trade(db_session, project_with_two_contacts):
    text = contactability_text(ladder_distribution(db_session))
    assert "every contactable row headlines the callable person" in text
    assert "1 of 1 contactable rows the callable person sits further" in text
    assert "mean 6.0 rungs given up" in text


def test_no_trade_when_the_closest_person_is_also_callable(db_session):
    doc = RawDocument(source="ceqanet", source_uid="u2", url="https://x/2",
                      title="NOP", raw_text="t", content_hash="h2")
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 triage_result=TriageResult.relevant, category=Category.data_center,
                 project_name="Reachable EOR Campus", county="Los Angeles", state="CA",
                 stage=Stage.entitlement,
                 named_people=[{"name": "Dana Reyes", "title": "Mechanical Engineer, PE",
                                "org": "Reyes Mechanical", "email": "dana@reyes.com"}])
    proj = Project(name="Reachable EOR Campus", category=Category.data_center,
                   county="Los Angeles", state="CA", stage=Stage.entitlement,
                   status="active", in_territory=True, score=0.9, window=Window.PRE_BOD)
    db_session.add(sig)
    db_session.add(proj)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id,
                                 match_confidence=1.0, match_method="manual"))
    db_session.commit()
    cs = contact_status(db_session, proj)
    assert cs["proximity_given_up"] == 0
    assert cs["closest_person_rung"] == 1
