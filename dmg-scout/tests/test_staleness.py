"""stage_ages(): evidence age dated to the CURRENT stage specifically.

The bug this replaces: dating a row from the newest linked signal of any kind,
so a late-arriving filing reporting an EARLIER stage than the project already
shows (which never moves project.stage — stage only moves forward) made a stale
claim read as freshly evidenced. See app/staleness.py's module docstring.
"""
from datetime import timedelta

from app.models import Category, Project, ProjectSignal, Signal, SignalType, Stage, StageObservation, utcnow
from app.staleness import stage_ages


def _project(session, stage, **kw):
    p = Project(name="P", category=Category.data_center, county="Los Angeles",
               state="CA", stage=stage, status="active", **kw)
    session.add(p)
    session.commit()
    return p


def _signal(session, stage, event_date):
    s = Signal(signal_type=SignalType.ceqa_nop, category=Category.data_center,
              stage=stage, county="Los Angeles", state="CA",
              summary_one_line="x", event_date=event_date)
    session.add(s)
    session.commit()
    return s


def _observe(session, project, stage, observed_at, signal=None):
    session.add(StageObservation(project_id=project.id, stage=stage,
                                 observed_at=observed_at, signal_id=signal.id if signal else None))
    session.commit()


def _link(session, project, signal):
    session.add(ProjectSignal(project_id=project.id, signal_id=signal.id,
                              match_confidence=1.0, match_method="direct"))
    session.commit()


def test_dates_from_the_current_stages_own_observation(db_session):
    now = utcnow()
    p = _project(db_session, Stage.permitting)
    old_permitting = _signal(db_session, Stage.permitting, now - timedelta(days=400))
    _link(db_session, p, old_permitting)
    _observe(db_session, p, Stage.permitting, now - timedelta(days=400), old_permitting)

    # A late-arriving filing restates an EARLIER stage — it is the newest
    # signal touching the project, but it never confirms "permitting".
    stale_entitlement = _signal(db_session, Stage.entitlement, now - timedelta(days=10))
    _link(db_session, p, stale_entitlement)
    _observe(db_session, p, Stage.entitlement, now - timedelta(days=10), stale_entitlement)

    ages = stage_ages(db_session, [p], now=now)
    # Must report ~400 days (the last real confirmation of "permitting"), not
    # ~10 days (the newest signal, which confirmed a different stage).
    assert ages[p.id].days == 400
    assert ages[p.id].unverified()


def test_a_later_signal_confirming_the_same_stage_refreshes_it(db_session):
    now = utcnow()
    p = _project(db_session, Stage.permitting)
    first = _signal(db_session, Stage.permitting, now - timedelta(days=400))
    _link(db_session, p, first)
    _observe(db_session, p, Stage.permitting, now - timedelta(days=400), first)

    confirm = _signal(db_session, Stage.permitting, now - timedelta(days=5))
    _link(db_session, p, confirm)
    _observe(db_session, p, Stage.permitting, now - timedelta(days=5), confirm)

    ages = stage_ages(db_session, [p], now=now)
    assert ages[p.id].days == 5
    assert not ages[p.id].unverified()


def test_falls_back_to_any_signal_date_before_backfill(db_session):
    """No StageObservation yet (pre-ledger project) — must not go blank."""
    now = utcnow()
    p = _project(db_session, Stage.permitting)
    sig = _signal(db_session, Stage.permitting, now - timedelta(days=45))
    _link(db_session, p, sig)
    # deliberately no _observe() call

    ages = stage_ages(db_session, [p], now=now)
    assert ages[p.id].days == 45


def test_no_evidence_at_all_reports_none(db_session):
    p = _project(db_session, Stage.unknown)
    ages = stage_ages(db_session, [p])
    assert ages[p.id].days is None
    assert ages[p.id].unverified()
