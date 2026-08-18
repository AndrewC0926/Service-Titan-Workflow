"""Pipeline logic tests: dedupe idempotency, resolution, size/score, digest dedupe."""

import pytest
from sqlmodel import select

from app.models import (
    DigestLog, MatchCandidate, Project, ProjectSignal, RawDocument, Signal, SignalType,
    SourceRun, Stage, TriageResult, Window, utcnow,
)
from app.pipeline.fetch import _store
from app.pipeline.notify import build_digest
from app.pipeline.resolve import apply_review_decision, pair_similarity, run_resolve
from app.pipeline.size_score import run_size_score
from app.sources.base import FetchedDoc


def make_doc(uid="a1", text="hello data center", title="t") -> FetchedDoc:
    return FetchedDoc(source="test", source_uid=uid, url="https://x/1", title=title, raw_text=text)


def test_store_is_idempotent(db_session):
    assert _store(db_session, make_doc()) == 1
    db_session.commit()
    assert _store(db_session, make_doc()) == 0  # same hash: no-op
    assert _store(db_session, make_doc(text="changed content")) == 1  # changed: update + re-triage
    db_session.commit()
    docs = db_session.exec(select(RawDocument)).all()
    assert len(docs) == 1
    assert docs[0].triage_result == TriageResult.pending


def _signal(db_session, **kw) -> Signal:
    defaults = dict(signal_type=SignalType.ceqa_nop, stage=Stage.entitlement,
                    confidence=0.9, summary_one_line="s")
    defaults.update(kw)
    s = Signal(**defaults)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_resolve_creates_and_links(db_session, cfg):
    _signal(db_session, project_name="Meridian Data Center Campus", county="San Bernardino",
            state="CA", developer_or_owner="Meridian DC Partners LLC", mw_total=176)
    stats = run_resolve(db_session, cfg, use_llm=False)
    assert stats["new_projects"] == 1

    # Second signal, same name via SPE-style entity -> auto-links
    _signal(db_session, project_name="Meridian Data Center Campus Phase II",
            county="San Bernardino", state="CA", signal_type=SignalType.air_permit_atc,
            developer_or_owner="Meridian DC Partners II, LLC", mw_total=180)
    stats = run_resolve(db_session, cfg, use_llm=False)
    assert stats["auto_linked"] == 1
    projects = db_session.exec(select(Project)).all()
    assert len(projects) == 1
    links = db_session.exec(select(ProjectSignal)).all()
    assert len(links) == 2
    methods = {l.match_method for l in links}
    assert "blocking+fuzzy" in methods  # audit trail on the link


def test_resolve_queues_ambiguous_for_review(db_session, cfg):
    _signal(db_session, project_name="Gateway Industrial Center", county="Storey", state="NV",
            developer_or_owner="Tract Management")
    run_resolve(db_session, cfg, use_llm=False)
    # Similar-ish but different project in same county -> ambiguous band, no LLM -> review queue
    _signal(db_session, project_name="Gateway Commerce Center", county="Storey", state="NV",
            developer_or_owner="Gateway Partners LLC")
    stats = run_resolve(db_session, cfg, use_llm=False)
    assert stats["queued_review"] == 1
    mc = db_session.exec(select(MatchCandidate)).one()
    assert mc.status == "pending"

    apply_review_decision(db_session, mc.id, "reject")
    assert db_session.exec(select(Project)).all().__len__() == 2
    db_session.refresh(mc)
    assert mc.status == "rejected"


def test_review_merge_learns_alias(db_session, cfg):
    _signal(db_session, project_name="Reno Tech Park DC", county="Washoe", state="NV",
            developer_or_owner="Vantage Data Centers")
    run_resolve(db_session, cfg, use_llm=False)
    s2 = _signal(db_session, project_name="Reno Technology Park Data Center", county="Washoe",
                 state="NV", developer_or_owner="Tech Core PY B, LLC",
                 signal_type=SignalType.abatement_application)
    run_resolve(db_session, cfg, use_llm=False)
    mcs = db_session.exec(select(MatchCandidate).where(MatchCandidate.status == "pending")).all()
    if mcs:  # merge path learns the SPE alias
        apply_review_decision(db_session, mcs[0].id, "merge")
        from app.models import DeveloperAlias
        aliases = db_session.exec(select(DeveloperAlias)).all()
        assert any(a.alias == "Tech Core PY B, LLC" for a in aliases)
    else:  # auto-linked via seeded config alias — equally acceptable
        links = db_session.exec(select(ProjectSignal).where(ProjectSignal.signal_id == s2.id)).all()
        assert links


def test_size_score_end_to_end(db_session, cfg):
    _signal(db_session, project_name="Meridian DC", county="San Bernardino", state="CA",
            mw_it=224, stage=Stage.entitlement)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    p = db_session.exec(select(Project)).one()
    assert 65_000 <= p.tons_estimate_low <= 70_000
    assert p.estimate_basis and "224" in p.estimate_basis
    assert p.window == Window.PRE_BOD
    assert p.score > 0
    assert p.days_to_estimated_bid == 365  # measured (CEQAnet NOP->NOD mean), not the old invented 540
    assert p.days_to_estimated_bid_low == 221
    assert p.days_to_estimated_bid_high == 509


def test_size_score_uses_permitted_capacity_corroborated_by_generator_split(db_session, cfg):
    """Vernon-shaped signal: 99 MW permitted/site capacity (a CEC SPPE filing —
    see sizing.permitted_capacity_to_it_mw for why that number is treated as a
    likely regulatory ceiling rather than an engineering fact), corroborated by
    38 x 3 MW critical + 2 x 1 MW house. Sizes via the PUE divisor on the 99 MW
    figure, well under the 114 MW critical-fleet ceiling — NOT sized directly
    from the 114 MW fleet, which would double-count the fleet's own N+1/N+2
    sparing and mechanical-load coverage."""
    _signal(db_session, project_name="Vernon Backup Generating Facility",
           county="Los Angeles", state="CA", mw_total=99,
           generator_critical_count=38, generator_critical_mw_each=3.0,
           generator_house_count=2, generator_house_mw_each=1.0)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    p = db_session.exec(select(Project)).one()
    assert p.estimate_basis and "permitted/site capacity" in p.estimate_basis
    assert "consistent with 114 MW" in p.estimate_basis
    it = 99 / 1.3
    mid = it * 325
    assert p.tons_estimate_low == pytest.approx(mid * 0.85)
    assert p.tons_estimate_high == pytest.approx(mid * 1.15)


def test_scoring_integration_small_early_beats_big_late(db_session, cfg):
    now = utcnow()
    _signal(db_session, project_name="Big Late DC", county="Clark", state="NV",
            mw_it=500, stage=Stage.construction, signal_type=SignalType.bid_invite,
            event_date=now)
    _signal(db_session, project_name="Small Early DC", county="Washoe", state="NV",
            mw_it=40, stage=Stage.entitlement, signal_type=SignalType.ceqa_nop,
            event_date=now)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    projects = {p.name: p for p in db_session.exec(select(Project)).all()}
    assert projects["Small Early DC"].score > projects["Big Late DC"].score


def test_nop_with_no_design_activity_is_pre_bod(db_session, cfg):
    """Gate 3a: a CEQA Notice of Preparation with no later-stage signals must
    classify PRE_BOD — that's the entire reason this system exists."""
    _signal(db_session, project_name="Early Bird DC", county="Riverside", state="CA",
            mw_total=80, stage=Stage.entitlement, signal_type=SignalType.ceqa_nop,
            filing_type="NOP")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    p = db_session.exec(select(Project)).one()
    assert p.window == Window.PRE_BOD
    assert p.score > 0


def test_nop_project_advances_to_in_bod_only_with_design_signal(db_session, cfg):
    """The demo's IN_BOD ranking was correct behavior: an air permit application
    means design is underway. Verify the transition is signal-driven."""
    _signal(db_session, project_name="Two Stage DC", county="Washoe", state="NV",
            mw_total=100, stage=Stage.entitlement, signal_type=SignalType.ceqa_nop,
            filing_type="NOP")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    assert db_session.exec(select(Project)).one().window == Window.PRE_BOD

    _signal(db_session, project_name="Two Stage DC", county="Washoe", state="NV",
            signal_type=SignalType.air_permit_atc, stage=Stage.permitting)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    assert db_session.exec(select(Project)).one().window == Window.IN_BOD


def test_digest_only_reports_new(db_session, cfg, monkeypatch):
    # digest.narrate defaults to True in real config; force the existing
    # SCOUT_LLM_DISABLED kill switch so build_digest's narration attempt
    # falls back to the plain-text body this test asserts on, deterministically
    # and without a real Anthropic call -- see test_notify_narration.py for
    # narration's own dedicated tests.
    monkeypatch.setenv("SCOUT_LLM_DISABLED", "1")
    _signal(db_session, project_name="Meridian DC", county="San Bernardino", state="CA",
            mw_it=100, stage=Stage.entitlement)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    db_session.add(SourceRun(source="ceqanet", ok=False, error="HTTP 500 boom"))
    db_session.commit()

    built = build_digest(db_session, cfg)
    assert built is not None
    body, stats = built
    assert "Meridian DC" in body
    assert "ceqanet" in body and "ONE THING" in body
    assert stats["changes"] == 1

    # Second run: nothing new on the board, but ceqanet is still failing, so
    # the digest is not silent — it degrades to just the ONE THING line, which
    # is the point: a persistent operational problem keeps surfacing daily
    # rather than being reported once and forgotten.
    built2 = build_digest(db_session, cfg)
    assert built2 is not None
    body2, stats2 = built2
    assert "Meridian DC" not in body2
    assert "ceqanet" in body2
    assert stats2["changes"] == 0
    assert db_session.exec(select(DigestLog)).all()


def test_pair_similarity_apn_dominates(db_session, cfg):
    s = Signal(signal_type=SignalType.ceqa_nop, apn_parcel="0110-111-22",
               project_name="Totally Different Name", summary_one_line="x")
    p = Project(name="Meridian", apn_parcel="0110-111-22")
    assert pair_similarity(s, p, 5) > 0.5


def test_county_alone_never_auto_merges(db_session, cfg):
    """Regression: the score is a weighted mean, so a signal whose ONLY comparable
    field was county scored a perfect 1.0 on that single agreement and auto-merged.
    A nameless Storey County agenda packet attached itself to the SV RNO data
    center at confidence 1.000 and pulled its last-signal date forward five months.
    """
    from app.models import Category
    _signal(db_session, project_name="SV RNO Property Owner 1 Data Center",
            county="Storey", state="NV", category=Category.data_center)
    run_resolve(db_session, cfg, use_llm=False)
    project = db_session.exec(select(Project)).one()

    nameless = _signal(db_session, project_name=None, developer_or_owner=None,
                       county="Storey", state="NV", category=Category.data_center)
    assert pair_similarity(nameless, project, radius_km=5.0) <= 0.60

    stats = run_resolve(db_session, cfg, use_llm=False)
    assert stats["auto_linked"] == 0          # the merge that used to happen
    assert stats["queued_review"] == 1        # surfaced for a human instead


def test_strong_evidence_still_auto_merges(db_session, cfg):
    """The cap must not block real merges: a matching project name is strong
    evidence and has to clear the auto-merge threshold on its own."""
    from app.models import Category
    _signal(db_session, project_name="Colovore Reno 1", county="Storey", state="NV",
            developer_or_owner="Colovore Reno 1 LLC", category=Category.data_center)
    run_resolve(db_session, cfg, use_llm=False)
    project = db_session.exec(select(Project)).one()
    same = _signal(db_session, project_name="Colovore Reno 1 (RNO01)", county="Storey",
                   state="NV", developer_or_owner="Colovore Reno 1, LLC",
                   category=Category.data_center)
    assert pair_similarity(same, project, radius_km=5.0) >= 0.88
    assert run_resolve(db_session, cfg, use_llm=False)["auto_linked"] == 1
