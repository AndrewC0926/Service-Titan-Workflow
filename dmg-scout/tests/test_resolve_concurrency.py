"""Two resolve runs must not process the same signal twice.

The failure this pins is real, not hypothetical. On 2026-08-06 a detached resolve
and a foreground resolve overlapped for 3.5 minutes. Each had snapshotted
`unlinked` at its own start, signal 503 was unlinked in both, and both created a
project for it — #961 and #963, byte-identical down to the SCH number and the
parcel list. The standing duplicate check caught it; nothing prevented it.

Backfill already had this guard, learned the same way ("observed for real when
two backfill processes overlapped"). Resolve did not. Now it does, with the same
shape: refuse by default, treat a long-unfinished run as a corpse, allow --force.

Two layers are tested here, because the guard alone is not enough:
  * the guard refuses a second run while one is in flight
  * the loop re-checks each signal, so even a forced overlap cannot double-link
"""
import pytest
from sqlmodel import select

from app.models import Category, Project, ProjectSignal, Signal, SignalType, SourceRun, Stage
from app.pipeline.resolve import (
    RESOLVE_RUN_SOURCE,
    ConcurrentResolve,
    _running_resolve,
    run_resolve,
)


def _signal(session, name="Probe Project", sch=None, county="Riverside"):
    sig = Signal(raw_document_id=None, signal_type=SignalType.ceqa_nop,
                 category=Category.industrial, project_name=name,
                 developer_or_owner=f"{name} Developer LLC", county=county,
                 state="CA", sch_number=sch, stage=Stage.entitlement,
                 summary_one_line="x")
    session.add(sig)
    session.commit()
    return sig


def _inflight(session, hours_ago=0.0):
    from datetime import timedelta

    from app.models import utcnow
    run = SourceRun(source=RESOLVE_RUN_SOURCE)
    run.started_at = utcnow() - timedelta(hours=hours_ago)
    session.add(run)
    session.commit()
    return run


def test_refuses_to_start_while_another_run_is_in_flight(db_session, cfg):
    _inflight(db_session)
    with pytest.raises(ConcurrentResolve, match="already running"):
        run_resolve(db_session, cfg, use_llm=False)


def test_force_overrides_the_guard(db_session, cfg):
    _inflight(db_session)
    stats = run_resolve(db_session, cfg, use_llm=False, force=True)
    assert "new_projects" in stats


def test_a_finished_run_does_not_block(db_session, cfg):
    from app.models import utcnow
    run = _inflight(db_session)
    run.finished_at = utcnow()
    db_session.add(run)
    db_session.commit()
    run_resolve(db_session, cfg, use_llm=False)  # no raise


def test_a_long_unfinished_run_is_a_corpse_not_a_competitor(db_session, cfg):
    """Otherwise one SIGKILLed run wedges resolve forever and --force becomes reflex."""
    _inflight(db_session, hours_ago=13)
    assert _running_resolve(db_session) is None
    run_resolve(db_session, cfg, use_llm=False)  # no raise


def test_run_is_closed_even_when_the_loop_raises(db_session, cfg, monkeypatch):
    """A failed run must not wedge the next one for 12 hours."""
    import app.pipeline.resolve as mod

    _signal(db_session)

    def boom(*a, **k):
        raise RuntimeError("loop exploded")
    monkeypatch.setattr(mod, "_resolve_loop", boom)

    with pytest.raises(RuntimeError, match="loop exploded"):
        run_resolve(db_session, cfg, use_llm=False)

    assert _running_resolve(db_session) is None, "a crashed run left the guard latched"


def test_run_is_recorded_and_closed_on_success(db_session, cfg):
    run_resolve(db_session, cfg, use_llm=False)
    runs = db_session.exec(
        select(SourceRun).where(SourceRun.source == RESOLVE_RUN_SOURCE)).all()
    assert len(runs) == 1
    assert runs[0].finished_at is not None


def test_a_signal_linked_after_the_snapshot_is_not_processed_twice(db_session, cfg):
    """The #961/#963 reproduction, at the layer below the guard.

    Simulates the overlap exactly: this run snapshots the signal as unlinked, a
    concurrent run links it, and then this run reaches it. Before the re-check
    that produced a second project for one signal.

    The concurrent project is deliberately NOT discoverable by blocking (no SCH,
    different county), which is what makes this test discriminating. Give it the
    same SCH and the SCH short-circuit auto-merges instead, masking the defect —
    and matching the real #961/#963, where the second run demonstrably did not
    find the first run's project as a candidate: both creations were preceded by
    an adjudicate call, so both went down the LLM 'no_match' path rather than
    auto-merging.
    """
    import app.pipeline.resolve as mod

    sig = _signal(db_session, name="First Industrial Commerce Center II",
                  sch=None)

    real_loop = mod._resolve_loop

    def other_run_links_it_first(session, cfg_, unlinked, stats, *a, **k):
        # run_resolve has already snapshotted `unlinked` (it contains our signal).
        # Now the other process gets there first, exactly as it did on 08-06.
        other = Project(name="Some Other Row The Blocker Will Not Find",
                        category=sig.category, developer="Unrelated Holdings LLC",
                        county="Kern", state="CA", sch_number=None,
                        status="active", stage=Stage.entitlement)
        session.add(other)
        session.flush()
        session.add(ProjectSignal(project_id=other.id, signal_id=sig.id,
                                  match_confidence=1.0, match_method="direct"))
        session.commit()
        return real_loop(session, cfg_, unlinked, stats, *a, **k)

    monkeypatch_target = other_run_links_it_first
    orig = mod._resolve_loop
    mod._resolve_loop = monkeypatch_target
    try:
        stats = run_resolve(db_session, cfg, use_llm=False)
    finally:
        mod._resolve_loop = orig

    # The invariant that actually broke on 08-06: one signal, one project.
    links = db_session.exec(
        select(ProjectSignal).where(ProjectSignal.signal_id == sig.id)).all()
    assert len(links) == 1, (
        f"signal {sig.id} ended up linked to {len(links)} projects — this is the "
        f"#961/#963 defect")
    assert stats["skipped_already_linked"] >= 1
    assert len(db_session.exec(select(Project)).all()) == 1, "a second project was created"


def test_a_signal_linked_during_adjudication_does_not_get_a_second_project(db_session, cfg):
    """The actual #961/#963 race, in the window the loop-top re-check cannot cover.

    Candidate blocking happens at the top of the iteration; project creation
    happens after the LLM call. That window is tens of seconds wide, and two runs
    whose blocking queries both landed inside it each saw no candidate and each
    created a project. The SCH key is not implicated — neither run could match on
    a row the other had not committed yet (see tests/test_sch_shortcircuit.py,
    which proves the key itself is sound).

    So the guard has to be at the moment of creation, not only at the top of the
    loop.
    """
    import app.pipeline.resolve as mod

    sig = _signal(db_session, name="Race In The LLM Window", sch=None)
    real_new = mod._new_project

    def other_run_wins_the_race(session, cfg, signal):
        # Stand in for the concurrent run committing first, mid-adjudication.
        if not session.exec(select(ProjectSignal)
                            .where(ProjectSignal.signal_id == signal.id)).first():
            other = Project(name="Committed By The Other Run",
                            category=signal.category, developer="Other LLC",
                            county="Kern", state="CA", status="active",
                            stage=Stage.entitlement)
            session.add(other)
            session.flush()
            session.add(ProjectSignal(project_id=other.id, signal_id=signal.id,
                                      match_confidence=1.0, match_method="direct"))
            session.commit()
        return real_new(session, cfg, signal)

    mod._new_project = other_run_wins_the_race
    try:
        run_resolve(db_session, cfg, use_llm=False)
    finally:
        mod._new_project = real_new

    links = db_session.exec(
        select(ProjectSignal).where(ProjectSignal.signal_id == sig.id)).all()
    assert len(links) == 1, (
        f"signal {sig.id} got {len(links)} projects — the creation-time guard "
        f"did not hold")
    assert len(db_session.exec(select(Project)).all()) == 1
