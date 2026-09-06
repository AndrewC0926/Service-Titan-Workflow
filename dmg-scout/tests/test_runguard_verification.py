"""SCOUT_VERIFYING_AGAINST_PROD: the mechanical backstop added after a
2026-09-06 incident where a "read-only" production check of
apply_review_decision landed for real, because run_size_score() (called
inside it) commits internally and defeated a wrapping
session.begin()/session.rollback(). See app/runguard.py's own docstring
and CHANGELOG.md's 2026-09-06 entry for the full account.

Every write path this guards must refuse BEFORE touching the database, so
each test below asserts both the raise and that nothing changed."""
import pytest
from sqlmodel import select

from app.models import (
    Category, MatchCandidate, Project, ProjectSignal, Signal, SignalType, Stage,
)
from app.pipeline.resolve import apply_review_decision
from app.runguard import (
    VERIFYING_AGAINST_PROD_ENV,
    VerifyingAgainstProduction,
    refuse_if_verifying_against_prod,
)


def test_refuses_when_flag_set(monkeypatch):
    monkeypatch.setenv(VERIFYING_AGAINST_PROD_ENV, "1")
    with pytest.raises(VerifyingAgainstProduction, match="some_caller"):
        refuse_if_verifying_against_prod("some_caller")


def test_does_not_raise_when_flag_unset(monkeypatch):
    monkeypatch.delenv(VERIFYING_AGAINST_PROD_ENV, raising=False)
    refuse_if_verifying_against_prod("some_caller")  # must not raise


def test_apply_review_decision_refuses_and_writes_nothing(db_session, cfg, monkeypatch):
    project = Project(name="P", category=Category.data_center, county="Storey", state="NV",
                      stage=Stage.permitting, status="active")
    db_session.add(project)
    db_session.commit()
    signal = Signal(signal_type=SignalType.job_posting, stage=Stage.construction,
                    county="Storey", state="NV", summary_one_line="s")
    db_session.add(signal)
    db_session.commit()
    mc = MatchCandidate(signal_id=signal.id, project_id=project.id, similarity=0.8,
                        llm_verdict="uncertain", status="pending")
    db_session.add(mc)
    db_session.commit()
    mc_id, starting_stage, starting_score = mc.id, project.stage, project.score

    monkeypatch.setenv(VERIFYING_AGAINST_PROD_ENV, "1")
    with pytest.raises(VerifyingAgainstProduction):
        apply_review_decision(db_session, cfg, mc_id, "merge")

    db_session.refresh(project)
    db_session.refresh(mc)
    assert project.stage == starting_stage, "the guard let a fact write through"
    assert project.score == starting_score, "the guard let a rescore through"
    assert mc.status == "pending", "the guard let the candidate's status change"
    assert not db_session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project.id)).all(), \
        "the guard let the link write through"


def test_merge_projects_refuses_and_writes_nothing(db_session, cfg, monkeypatch):
    from app.merge import merge_projects

    a = Project(name="P", category=Category.industrial, county="Riverside", state="CA",
               stage=Stage.entitlement, status="active")
    b = Project(name="P", category=Category.industrial, county="Riverside", state="CA",
               stage=Stage.construction, status="active")
    db_session.add_all([a, b])
    db_session.commit()

    monkeypatch.setenv(VERIFYING_AGAINST_PROD_ENV, "1")
    with pytest.raises(VerifyingAgainstProduction):
        merge_projects(db_session, cfg, a, b)

    db_session.refresh(a)
    db_session.refresh(b)
    assert a.stage == Stage.entitlement, "the guard let the absorb through"
    assert b.status == "active", "the guard let the dup get retired"


def test_run_dc_news_enrichment_refuses_and_writes_nothing(db_session, cfg, monkeypatch):
    from app.http import PoliteClient
    from app.pipeline.dc_news_enrichment import run_dc_news_enrichment

    monkeypatch.setenv(VERIFYING_AGAINST_PROD_ENV, "1")
    with pytest.raises(VerifyingAgainstProduction):
        run_dc_news_enrichment(db_session, cfg, PoliteClient(interval=0, max_retries=0))
    # Refuses before any network call or DB write -- nothing to assert on the
    # (untouched) database beyond the raise itself.
