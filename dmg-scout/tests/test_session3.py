"""Session 3: contact ladder, coverage/blind spots, board audit, completeness
banner, deliverables."""
import csv
import json
from datetime import timedelta

from sqlmodel import select

from app.audit import audit_board
from app.coverage import coverage_report, pipeline_completeness
from app.deliverables import write_baseline, write_briefs, write_call_list
from app.firms import seed_firms
from app.ladder import best_contact, build_ladder, ladder_distribution
from app.manual import add_manual_signal
from app.models import Project, RawDocument, Signal, TriageResult, utcnow
from app.pipeline.notify import _call_recommendation
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score


def make_project(db_session, cfg, name="Ladder DC", county="San Bernardino", state="CA",
                 people=None, firms=None, signal_type="ceqa_nop", stage="entitlement",
                 mw_it=80.0):
    s = add_manual_signal(db_session, signal_type, f"signal for {name}", project_name=name,
                          developer="Vantage Data Centers", county=county, state=state,
                          mw_it=mw_it, stage=stage, url=f"https://example.gov/{name.replace(' ', '')}")
    sig = db_session.get(Signal, s.id)
    if people:
        sig.named_people = people
    if firms:
        sig.named_firms = firms
    db_session.add(sig)
    db_session.commit()
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    return db_session.exec(select(Project).where(Project.name == name)).one()


def test_rung1_mechanical_eor(db_session, cfg):
    seed_firms(db_session, cfg)
    p = make_project(db_session, cfg, people=[
        {"name": "Dana Reyes", "title": "Principal Mechanical Engineer", "org": "kW MCE"}])
    best = best_contact(db_session, p)
    assert best["rung"] == 1 and best["name"] == "Dana Reyes"


def test_rung2_mep_firm_without_individual(db_session, cfg):
    seed_firms(db_session, cfg)
    p = make_project(db_session, cfg, firms=[
        {"name": "Syska Hennessy Group", "role": "engineer_of_record"}])
    best = best_contact(db_session, p)
    assert best["rung"] == 2 and "Syska" in best["name"] and best["kind"] == "firm"


def test_rung7_planner_from_ceqa_contact(db_session, cfg):
    """The most common real case: filing names only the lead agency planner."""
    seed_firms(db_session, cfg)
    p = make_project(db_session, cfg, people=[
        {"name": "Maria Lopez", "title": "Senior Planner", "org": "City of Ontario",
         "phone": "(909) 555-0100"}])
    ladder = build_ladder(db_session, p)
    planner = next(r for r in ladder if r["name"] == "Maria Lopez")
    assert planner["rung"] == 7
    assert planner["source_url"]  # traceable to the filing
    # The named human beats the bare-developer fallback (which only appears
    # when NO other rung exists):
    best = best_contact(db_session, p)
    assert best["name"] == "Maria Lopez" and best["rung"] == 7


def test_every_project_reaches_some_rung(db_session, cfg):
    seed_firms(db_session, cfg)
    make_project(db_session, cfg, "Bare DC", people=None, firms=None)
    dist = ladder_distribution(db_session)
    assert dist["counts"].get(None, 0) == 0  # developer fallback guarantees a rung


def test_digest_call_pick_uses_ladder(db_session, cfg):
    seed_firms(db_session, cfg)
    make_project(db_session, cfg, people=[
        {"name": "Dana Reyes", "title": "Principal Mechanical Engineer", "org": "kW MCE"}])
    rec = _call_recommendation(db_session)
    assert rec is not None
    assert "Dana Reyes" in rec and "rung 1" in rec


def test_coverage_flags_blind_counties(db_session, cfg):
    seed_firms(db_session, cfg)
    make_project(db_session, cfg, "Fresh DC", "San Bernardino", "CA")
    stale = make_project(db_session, cfg, "Stale DC", "Clark", "NV")
    sig = db_session.exec(select(Signal).where(Signal.project_name == "Stale DC")).one()
    sig.event_date = utcnow() - timedelta(days=200)
    sig.created_at = utcnow() - timedelta(days=200)
    db_session.add(sig)
    db_session.commit()
    run_size_score(db_session, cfg)

    report = coverage_report(db_session, cfg)
    rows = {f"{r['county']},{r['state']}": r for r in report["rows"]}
    assert rows["San Bernardino,CA"]["projects"] == 1
    assert not rows["San Bernardino,CA"]["suspected_blind"]
    assert rows["Clark,NV"]["suspected_blind"]        # stale = blind, not "empty market"
    assert rows["Riverside,CA"]["suspected_blind"]    # zero signals ever = blind
    assert "Riverside, CA" in report["blind_counties"]


def test_completeness_banner_conditions(db_session, cfg):
    assert pipeline_completeness(db_session, cfg)["complete"] is True
    db_session.add(RawDocument(source="ceqanet", source_uid="x", url="u", title="t",
                               content_hash="h", raw_text="pending doc",
                               triage_result=TriageResult.pending))
    db_session.commit()
    st = pipeline_completeness(db_session, cfg)
    assert st["complete"] is False and st["pending_triage"] == 1


def test_audit_flags(db_session, cfg):
    seed_firms(db_session, cfg)
    make_project(db_session, cfg, "Solo Signal DC", mw_it=200.0)
    aggressive = make_project(db_session, cfg, "Aggressive DC", county="Washoe", state="NV",
                              signal_type="air_permit_atc", stage="permitting")
    sqft_p = make_project(db_session, cfg, "Sqft DC", county="Orange", mw_it=None)
    sig = db_session.exec(select(Signal).where(Signal.project_name == "Sqft DC")).one()
    sig.building_sqft = 800_000
    db_session.add(sig)
    db_session.commit()
    run_size_score(db_session, cfg)

    rows = {r["name"]: r for r in audit_board(db_session, cfg)}
    assert "SINGLE_SIGNAL" in rows["Solo Signal DC"]["flags"]
    assert "AGGRESSIVE_STAGE" in rows["Aggressive DC"]["flags"]
    assert "SQFT_BASIS" in rows["Sqft DC"]["flags"]
    assert all(not r["defensible"] for r in rows.values())


def test_deliverables_outputs(db_session, cfg, tmp_path):
    seed_firms(db_session, cfg)
    make_project(db_session, cfg, "Deliverable DC", people=[
        {"name": "Dana Reyes", "title": "Principal Mechanical Engineer", "org": "kW MCE"}])
    make_project(db_session, cfg, "Out Of Terr DC", county="Travis", state="TX")

    n = write_call_list(db_session, tmp_path / "call-list.csv")
    assert n == 1  # PRE_BOD in-territory only; Texas project excluded
    rows = list(csv.DictReader(open(tmp_path / "call-list.csv")))
    assert rows[0]["project"] == "Deliverable DC"
    assert rows[0]["contact_name"] == "Dana Reyes"
    assert rows[0]["ladder_rung"] == "1"
    assert rows[0]["contact_source_url"].startswith("https://")

    briefs = write_briefs(db_session, tmp_path / "briefs", top=10)
    assert len(briefs) == 1 and briefs[0].endswith(".md")

    base = write_baseline(db_session, tmp_path / "baseline.json")
    snap = json.loads((tmp_path / "baseline.json").read_text())
    assert snap["n_pre_bod_in_territory"] == 1
    assert snap["projects"][0]["score"] > 0
    assert base["projects"] == 2  # baseline freezes everything, incl. watch list
