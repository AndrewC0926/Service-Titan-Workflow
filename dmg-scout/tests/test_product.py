"""Gate 5: firm roster resolution, brief, outcomes, territory filter."""
import pytest
from sqlmodel import select

from app.brief import brief_markdown, build_brief
from app.firms import match_firm, resolve_signal_firms, seed_firms
from app.manual import add_manual_signal
from app.models import Firm, Project, ProjectFirm, Signal, SignalType, Stage
from app.outcomes import outcomes_report, record_outcome
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import in_territory, run_size_score


def test_seed_firms_idempotent(db_session, cfg):
    added1 = seed_firms(db_session, cfg)
    assert added1 >= 30
    assert seed_firms(db_session, cfg) == 0  # second run adds nothing
    assert len(db_session.exec(select(Firm)).all()) == added1


def test_alias_matching(db_session, cfg):
    seed_firms(db_session, cfg)
    assert match_firm(db_session, "kW MCE").name == "kW Mission Critical Engineering"
    assert match_firm(db_session, "ACCO").name == "ACCO Engineered Systems"
    assert match_firm(db_session, "kW Mission Critical Engineering, Inc.").name \
        == "kW Mission Critical Engineering"
    assert match_firm(db_session, "Totally Unknown Engineers") is None


def test_named_firms_resolve_against_roster_not_duplicates(db_session, cfg):
    seed_firms(db_session, cfg)
    n_before = len(db_session.exec(select(Firm)).all())
    resolve_signal_firms(db_session, project_id=1, named_firms=[
        {"name": "Syska Hennessy Group, Inc.", "role": "engineer_of_record"},
        {"name": "Syska", "role": "engineer_of_record"},          # alias, same firm
        {"name": "Brand New Firm LLC", "role": "consultant"},     # unknown -> created once
        {"name": "Brand New Firm, LLC", "role": "consultant"},    # normalizes to same row
    ])
    db_session.commit()
    firms = db_session.exec(select(Firm)).all()
    assert len(firms) == n_before + 1  # only Brand New Firm was created
    links = db_session.exec(select(ProjectFirm)).all()
    assert len(links) == 2  # syska(eor) + brand new(consultant)


def test_extraction_firms_flow_through_resolve(db_session, cfg):
    s = add_manual_signal(db_session, "ceqa_nop", "NOP with named EOR",
                          project_name="Roster Flow DC", county="Orange", state="CA",
                          mw_it=40, stage="entitlement")
    sig = db_session.get(Signal, s.id)
    sig.named_firms = [{"name": "kW MCE", "role": "engineer_of_record"}]
    db_session.add(sig)
    db_session.commit()
    run_resolve(db_session, cfg, use_llm=False)
    link = db_session.exec(select(ProjectFirm)).one()
    firm = db_session.get(Firm, link.firm_id)
    assert firm.name == "kW Mission Critical Engineering"
    assert link.role == "engineer_of_record"


def _project(db_session, cfg, name="Brief DC", county="Riverside", state="CA") -> Project:
    add_manual_signal(db_session, "ceqa_nop", f"NOP for {name}", project_name=name,
                      developer="Vantage Data Centers", county=county, state=state,
                      mw_it=100, stage="entitlement", url="https://ceqanet.lci.ca.gov/2026999999")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    return db_session.exec(select(Project).where(Project.name == name)).one()


def test_brief_contains_traceable_numbers(db_session, cfg):
    p = _project(db_session, cfg)
    md = brief_markdown(build_brief(db_session, p.id))
    assert p.name in md
    assert "stated IT load 100 MW" in md
    assert "https://ceqanet.lci.ca.gov/2026999999" in md  # every number traces to a URL
    assert "ceqa_nop" in md
    assert "Days to estimated bid" in md


def test_outcome_flow_and_report(db_session, cfg):
    p1 = _project(db_session, cfg, "Won DC", "Riverside")
    p2 = _project(db_session, cfg, "Dead DC", "Clark", "NV")
    record_outcome(db_session, p1.id, "won", "our chillers in BOD")
    record_outcome(db_session, p2.id, "dead", "project cancelled")
    db_session.refresh(p1)
    assert p1.status == "won"
    r = outcomes_report(db_session)
    assert r["n_converted"] == 1 and r["n_died"] == 1
    row = next(x for x in r["rows"] if x["signal_type"] == "ceqa_nop")
    assert row["converted"] == 1 and row["died"] == 1
    assert not r["reliable"]  # honest about tiny samples

    with pytest.raises(ValueError):
        record_outcome(db_session, p1.id, "not_a_status")


def test_territory_filter(cfg):
    assert in_territory(cfg, "CA", "Riverside")
    assert in_territory(cfg, "NV", "Storey County")
    assert not in_territory(cfg, "TX", "Travis")        # out-of-territory state
    assert not in_territory(cfg, "CA", "Sacramento")    # in-state, wrong county
    assert in_territory(cfg, None, None)                # unknown stays on the board


def test_territory_applied_at_scoring(db_session, cfg):
    _project(db_session, cfg, "In Terr DC", "Riverside", "CA")
    _project(db_session, cfg, "Out Terr DC", "Travis", "TX")
    projects = {p.name: p for p in db_session.exec(select(Project)).all()}
    assert projects["In Terr DC"].in_territory is True
    assert projects["Out Terr DC"].in_territory is False
