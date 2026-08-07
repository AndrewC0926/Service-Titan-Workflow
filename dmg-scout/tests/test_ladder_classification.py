"""Two defects that made the flagship row say "No one" with two phones on it.

Vernon is the charter's reference implementation — 99 MW, LA County, a CEC project
manager with a direct line, and the developer's regional director with a mobile
and an email. Signal 301 carried both, linked to project 825 at confidence 1.0,
and the board rendered "No one to call". The link was fine; the ladder threw both
people away.

  1. `firm_type == "unknown"` is the DEFAULT and means "never classified" — the
     absence of a type. Returning the string made it truthy, and rung 7's
     fallback is guarded by `not org_type`, so the one case that fallback exists
     to catch was the exact case it rejected. 56 of ~400 firms carry it.
  2. Rung 5 required both an exact developer-name match and a construction/MEP
     manager title. "GIC Vernon LLC" vs the project's "GIC Vernon LLC (Goodman)"
     is not an exact match, and "Regional Director" is not that title, so a named
     human at the developer with a mobile fell off the ladder entirely.

Fixing both moved the board from 263 callable rows to 287.
"""
import pytest

from app.ladder import UNTYPED_FIRM, _classify_person, _same_org, build_ladder, contact_status
from app.models import (
    Category, Firm, Project, ProjectSignal, RawDocument, Signal, SignalType, Stage,
    TriageResult, Window,
)
from app.normalize import normalize_name

CEC = {"name": "Elizabeth Huber", "title": "Project Manager",
       "org": "California Energy Commission",
       "email": "Elizabeth.Huber@energy.ca.gov", "phone": "(916) 776-0609"}
DEV = {"name": "Ward Mace", "title": "Regional Director", "org": "GIC Vernon LLC",
       "email": "ward.mace@goodman.com", "phone": "(949) 407-0142"}
DEVELOPER = "GIC Vernon LLC (Goodman)"


# --- defect 1: "unknown" is not a classification ----------------------------

def test_untyped_firm_does_not_block_the_agency_fallback():
    """A phone in a public filing at an unclassifiable org is rung 7, not nothing."""
    firm_types = {}  # index excludes UNTYPED_FIRM, so the org resolves to None
    assert _classify_person(firm_types, CEC, "ceqanet", normalize_name(DEVELOPER)) == 7


def test_untyped_firms_are_absent_from_the_index(db_session):
    from app.ladder import firm_type_index
    db_session.add(Firm(name="California Energy Commission",
                        name_norm=normalize_name("California Energy Commission"),
                        firm_type=UNTYPED_FIRM))
    db_session.add(Firm(name="Acme MEP", name_norm=normalize_name("Acme MEP"),
                        firm_type="mep"))
    db_session.commit()
    idx = firm_type_index(db_session)
    assert normalize_name("California Energy Commission") not in idx
    assert idx[normalize_name("Acme MEP")] == "mep"


def test_a_real_firm_type_still_classifies():
    """The fix must not blunt the types that ARE known."""
    idx = {normalize_name("Acme MEP"): "mep", normalize_name("BuildCo"): "gc"}
    eor = {"name": "R Diaz", "title": "Principal", "org": "Acme MEP", "phone": "555"}
    precon = {"name": "T Ng", "title": "Preconstruction Lead", "org": "BuildCo"}
    assert _classify_person(idx, eor, "ceqanet", None) == 1
    assert _classify_person(idx, precon, "ceqanet", None) == 4


# --- defect 2: a human at the developer is somebody ------------------------

def test_developer_person_without_a_manager_title_is_rung_5():
    assert _classify_person({}, DEV, "ceqanet", normalize_name(DEVELOPER)) == 5


def test_developer_construction_manager_is_still_rung_5():
    cm = {"name": "P Ito", "title": "Construction Manager", "org": DEVELOPER}
    assert _classify_person({}, cm, "ceqanet", normalize_name(DEVELOPER)) == 5


@pytest.mark.parametrize("filing_org,project_developer", [
    ("GIC Vernon LLC", "GIC Vernon LLC (Goodman)"),      # parenthetical parent
    ("GIC Vernon LLC (Goodman)", "GIC Vernon LLC"),      # and the other way round
    ("Vantage Data Centers", "Vantage Data Centers LLC"),
])
def test_same_org_tolerates_how_filings_write_a_parent(filing_org, project_developer):
    assert _same_org(normalize_name(filing_org), normalize_name(project_developer))


@pytest.mark.parametrize("a,b", [
    ("Orbis Real Estate Partners", "GIC Vernon LLC"),
    ("Prologis", "Rexford Industrial"),
    ("", "GIC Vernon LLC"),
])
def test_same_org_does_not_match_unrelated_companies(a, b):
    assert not _same_org(normalize_name(a), normalize_name(b) if b else None)


def test_a_person_at_a_different_company_is_not_developer_side():
    """Being named on the filing is not the same as working for the developer."""
    other = {"name": "J Roe", "title": "Regional Director", "org": "Orbis Real Estate"}
    assert _classify_person({}, other, "edgar", normalize_name(DEVELOPER)) is None


# --- the Vernon row, end to end --------------------------------------------

def test_vernon_shaped_row_is_callable(db_session):
    """The regression that matters: this row must never again read "No one"."""
    doc = RawDocument(source="ceqanet", source_uid="2026080118",
                      url="https://ceqanet.lci.ca.gov/2026080118", title="NOP",
                      raw_text="t", content_hash="vernon")
    db_session.add(doc)
    db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 triage_result=TriageResult.relevant, category=Category.data_center,
                 project_name="Vernon Backup Generating Facility",
                 county="Los Angeles", state="CA", stage=Stage.permitting,
                 named_people=[CEC, DEV])
    proj = Project(name="Vernon Backup Generating Facility (Goodman Energy Park)",
                   category=Category.data_center, developer=DEVELOPER,
                   county="Los Angeles", state="CA", stage=Stage.permitting,
                   status="active", in_territory=True, score=1.23, window=Window.IN_BOD)
    # The org is in the firm table typed `unknown`, exactly as the live board has it.
    db_session.add(Firm(name="California Energy Commission",
                        name_norm=normalize_name("California Energy Commission"),
                        firm_type=UNTYPED_FIRM))
    db_session.add(Firm(name="GIC Vernon LLC", name_norm=normalize_name("GIC Vernon LLC"),
                        firm_type="developer"))
    db_session.add(sig)
    db_session.add(proj)
    db_session.commit()
    db_session.add(ProjectSignal(project_id=proj.id, signal_id=sig.id,
                                 match_confidence=1.0, match_method="direct"))
    db_session.commit()

    ladder = build_ladder(db_session, proj)
    names = [r["name"] for r in ladder]
    assert "Ward Mace" in names and "Elizabeth Huber" in names

    cs = contact_status(db_session, proj)
    assert cs["status"] == "contactable"
    # Reachability first, then proximity: both are callable, so the developer's
    # director (rung 5) outranks the agency project manager (rung 7).
    assert cs["best_reachable"]["name"] == "Ward Mace"
    assert cs["best_reachable"]["phone"] == "(949) 407-0142"


def test_company_level_fallback_only_fires_with_no_real_rungs(db_session):
    """The bare-developer placeholder must not mask a real person."""
    proj = Project(name="Empty Project", category=Category.data_center,
                   developer=DEVELOPER, county="Los Angeles", state="CA",
                   stage=Stage.entitlement, status="active", in_territory=True,
                   score=0.1, window=Window.PRE_BOD)
    db_session.add(proj)
    db_session.commit()
    ladder = build_ladder(db_session, proj)
    assert len(ladder) == 1 and ladder[0]["kind"] == "firm"
    assert contact_status(db_session, proj)["status"] == "none"
