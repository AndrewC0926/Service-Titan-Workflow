"""RATCHET OVERRIDE: manual corrections pin a field against being moved by
_absorb()'s and run_size_score()'s best() forward-only/max-only rules. See
app/pipeline/corrections.py's module docstring for the full design.
"""
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import (
    Category, PinnedFieldConflict, Project, ProjectSignal, Signal, SignalType, Stage, utcnow,
)
from app.pipeline.corrections import apply_manual_correction, latest_correction
from app.pipeline.resolve import link_signal_to_project


def _project(session, **kw) -> Project:
    base = dict(name="P", category=Category.data_center, county="Storey", state="NV",
               stage=Stage.entitlement, status="active")
    base.update(kw)
    p = Project(**base)
    session.add(p)
    session.commit()
    return p


def _signal(session, **kw) -> Signal:
    base = dict(signal_type=SignalType.job_posting, stage=Stage.entitlement,
               county="Storey", state="NV", summary_one_line="s")
    base.update(kw)
    s = Signal(**base)
    session.add(s)
    session.commit()
    return s


# --- apply_manual_correction: the only path allowed to move a field back ----


def test_reads_old_value_from_the_live_project_row(db_session, cfg):
    p = _project(db_session, stage=Stage.construction)
    correction = apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                                         reason="filing was wrong", corrected_by="Andrew")
    assert correction.old_value == "construction"
    assert correction.new_value == "entitlement"
    db_session.refresh(p)
    assert p.stage == Stage.entitlement, "correction did not move stage backward"


def test_requires_a_reason_and_who(db_session, cfg):
    p = _project(db_session)
    with pytest.raises(ValueError, match="reason"):
        apply_manual_correction(db_session, cfg, p.id, "stage", "concept",
                                reason="  ", corrected_by="Andrew")
    with pytest.raises(ValueError, match="who"):
        apply_manual_correction(db_session, cfg, p.id, "stage", "concept",
                                reason="wrong", corrected_by="  ")


def test_stage_correction_writes_a_manual_stage_observation(db_session, cfg):
    from app.models import StageObservation

    p = _project(db_session, stage=Stage.construction)
    apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                            reason="filing was wrong", corrected_by="Andrew")
    obs = db_session.exec(select(StageObservation).where(
        StageObservation.project_id == p.id, StageObservation.source == "manual")).all()
    assert len(obs) == 1
    assert obs[0].stage == Stage.entitlement
    assert obs[0].signal_id is None


def test_rescores_in_the_same_transaction(db_session, cfg):
    p = _project(db_session, stage=Stage.construction, last_signal_at=utcnow())
    sig = _signal(db_session, stage=Stage.construction)
    db_session.add(ProjectSignal(project_id=p.id, signal_id=sig.id,
                                 match_confidence=1.0, match_method="direct"))
    db_session.commit()
    from app.pipeline.size_score import run_size_score
    run_size_score(db_session, cfg)
    db_session.refresh(p)
    starting_score = p.score

    apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                            reason="filing was wrong", corrected_by="Andrew")

    db_session.refresh(p)
    assert p.score != starting_score, "correction did not trigger a rescore"


# --- the pin: _absorb() ------------------------------------------------------


def test_pin_blocks_a_signal_observed_at_or_before_the_correction(db_session, cfg):
    p = _project(db_session, stage=Stage.construction)
    correction = apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                                         reason="filing was wrong", corrected_by="Andrew")

    stale_signal = _signal(db_session, stage=Stage.construction,
                           event_date=correction.corrected_at - timedelta(days=1))
    link_signal_to_project(db_session, stale_signal, p, 1.0, "direct")

    db_session.refresh(p)
    assert p.stage == Stage.entitlement, "a signal that predates the correction moved the pinned field"
    assert not db_session.exec(select(PinnedFieldConflict)).all(), \
        "a stale signal should be silently discarded, not queued"


def test_pin_blocks_a_newer_forward_signal_and_queues_it_for_review(db_session, cfg):
    p = _project(db_session, stage=Stage.construction)
    correction = apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                                         reason="filing was wrong", corrected_by="Andrew")

    newer_signal = _signal(db_session, stage=Stage.construction,
                           event_date=correction.corrected_at + timedelta(days=1))
    link_signal_to_project(db_session, newer_signal, p, 1.0, "direct")

    db_session.refresh(p)
    assert p.stage == Stage.entitlement, "a post-pin signal was applied straight to Project"
    conflicts = db_session.exec(select(PinnedFieldConflict)).all()
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.field == "stage" and c.signal_id == newer_signal.id
    assert c.correction_id == correction.id
    assert c.pinned_value == "entitlement" and c.candidate_value == "construction"
    assert c.status == "pending"


def test_pin_blocks_mw_it_the_same_way(db_session, cfg):
    p = _project(db_session, mw_it=40.0)
    correction = apply_manual_correction(db_session, cfg, p.id, "mw_it", "10.0",
                                         reason="filing overstated it", corrected_by="Andrew")

    newer_signal = _signal(db_session, mw_it=40.0,
                           event_date=correction.corrected_at + timedelta(days=1))
    link_signal_to_project(db_session, newer_signal, p, 1.0, "direct")

    db_session.refresh(p)
    assert p.mw_it == 10.0, "a post-pin signal re-inflated a pinned mw_it"
    conflicts = db_session.exec(select(PinnedFieldConflict)).all()
    assert len(conflicts) == 1 and conflicts[0].field == "mw_it"


def test_pin_does_not_affect_an_unpinned_field(db_session, cfg):
    """Correcting stage must not change how mw_it (or anything else
    un-pinned) behaves -- exactly today's fill-only-if-null/max rule."""
    p = _project(db_session, stage=Stage.construction, mw_it=10.0)
    apply_manual_correction(db_session, cfg, p.id, "stage", "entitlement",
                            reason="filing was wrong", corrected_by="Andrew")

    bigger_mw_signal = _signal(db_session, mw_it=40.0, stage=Stage.unknown)
    link_signal_to_project(db_session, bigger_mw_signal, p, 1.0, "direct")

    db_session.refresh(p)
    assert p.mw_it == 40.0, "un-pinned mw_it should still take the larger signal value"


# --- the pin: run_size_score()'s best() --------------------------------------


def test_best_excludes_an_already_linked_pre_pin_signal_from_the_max(db_session, cfg):
    """A signal linked BEFORE a correction existed (so _absorb() never had a
    pin to check) must not let its stale mw_it value win best()'s max on a
    later run_size_score() pass. project.mw_it or best(...) already short-
    circuits once project.mw_it is any truthy value, so this test corrects
    mw_it to 0.0 (falsy) specifically to force best() itself to be
    consulted -- exactly the case the short-circuit alone would NOT catch."""
    from app.pipeline.size_score import run_size_score

    p = _project(db_session, mw_it=40.0)
    old_signal = _signal(db_session, mw_it=40.0, event_date=utcnow() - timedelta(days=10))
    db_session.add(ProjectSignal(project_id=p.id, signal_id=old_signal.id,
                                 match_confidence=1.0, match_method="direct"))
    db_session.commit()

    apply_manual_correction(db_session, cfg, p.id, "mw_it", "0.0",
                            reason="filing overstated it -- no stated IT load at all",
                            corrected_by="Andrew")

    run_size_score(db_session, cfg)  # a later, ordinary nightly-shaped rescore

    db_session.refresh(p)
    assert p.tons_estimate_low is None, \
        "best() let the pre-pin signal's mw_it=40 size the project despite the pin"
