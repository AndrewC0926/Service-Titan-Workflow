"""StageObservation: every stage a project has ever been observed at, kept
forever, one row per signal — see the model's docstring in app/models.py.

Written in app.pipeline.resolve right after _absorb(), for every
signal.stage != unknown, whether or not it moved project.stage forward.
Repointed to the survivor on merge, same as any other signal-keyed link row.
"""
from sqlmodel import select

from app.merge import merge_projects
from app.models import Category, Project, Signal, SignalType, Stage, StageObservation
from app.pipeline.resolve import run_resolve


def _sig(session, **kw):
    base = dict(signal_type=SignalType.ceqa_nop, category=Category.data_center,
               stage=Stage.entitlement, county="Los Angeles", state="CA",
               summary_one_line="x")
    s = Signal(**{**base, **kw})
    session.add(s)
    session.commit()
    return s


def _observations(session, signal_id):
    return session.exec(
        select(StageObservation).where(StageObservation.signal_id == signal_id)).all()


def test_new_project_records_a_stage_observation(db_session, cfg):
    sig = _sig(db_session, project_name="Alpha Campus")
    run_resolve(db_session, cfg, use_llm=False)

    obs = _observations(db_session, sig.id)
    assert len(obs) == 1
    assert obs[0].stage == Stage.entitlement
    project = db_session.exec(select(Project)).first()
    assert obs[0].project_id == project.id


def test_unknown_stage_signal_records_nothing(db_session, cfg):
    sig = _sig(db_session, project_name="No Stage Yet", stage=Stage.unknown)
    run_resolve(db_session, cfg, use_llm=False)
    assert _observations(db_session, sig.id) == []


def test_later_signal_that_does_not_advance_stage_still_gets_an_observation(db_session, cfg):
    """A signal reporting an earlier stage than the project already shows is
    still a real observation — the forward-only rule governs Project.stage,
    not the ledger."""
    sig1 = _sig(db_session, project_name="Beta Campus", stage=Stage.permitting)
    run_resolve(db_session, cfg, use_llm=False)
    project = db_session.exec(select(Project)).first()
    assert project.stage == Stage.permitting

    sig2 = _sig(db_session, project_name="Beta Campus", county="Los Angeles",
               state="CA", stage=Stage.entitlement)
    run_resolve(db_session, cfg, use_llm=False)
    db_session.refresh(project)
    assert project.stage == Stage.permitting  # unchanged: stage moves forward only

    all_obs = _observations(db_session, sig1.id) + _observations(db_session, sig2.id)
    assert {o.stage for o in all_obs} == {Stage.permitting, Stage.entitlement}


def test_reresolve_does_not_duplicate_the_observation(db_session, cfg):
    """A signal that is already linked is skipped by the unlinked-signal
    snapshot, but a concurrent or backfilled re-absorb must not double the
    ledger — the unique constraint on signal_id is the backstop."""
    sig = _sig(db_session, project_name="Gamma Campus")
    run_resolve(db_session, cfg, use_llm=False)
    run_resolve(db_session, cfg, use_llm=False)
    assert len(_observations(db_session, sig.id)) == 1


def test_merge_repoints_stage_observations_to_the_survivor(db_session, cfg):
    a = Project(name="P", category=Category.data_center, county="Los Angeles",
               state="CA", stage=Stage.entitlement, status="active")
    b = Project(name="P", category=Category.data_center, county="Los Angeles",
               state="CA", stage=Stage.entitlement, status="active")
    db_session.add(a)
    db_session.add(b)
    db_session.commit()
    sig = _sig(db_session, project_name="P")
    db_session.add(StageObservation(project_id=b.id, stage=Stage.entitlement,
                                    observed_at=sig.created_at, signal_id=sig.id))
    db_session.commit()

    merge_projects(db_session, a, b)

    moved = _observations(db_session, sig.id)
    assert len(moved) == 1
    assert moved[0].project_id == a.id
