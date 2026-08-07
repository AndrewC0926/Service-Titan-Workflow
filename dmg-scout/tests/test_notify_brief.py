"""The redesigned digest: three calls ranked by score x reachability x window
proximity, changed-since-yesterday, overdue/due next actions, and the one
thing worth knowing — see app/pipeline/notify.py's module docstring for why
these four and nothing else."""
from datetime import timedelta

from app.models import (
    Category, Outreach, Project, Signal, SignalType, SourceRun, Stage, Window, utcnow,
)
from app.pipeline.notify import (
    _one_thing_worth_knowing,
    _overdue_and_due,
    _quietest_county,
    _stale_sources,
    three_calls_today,
)


def _project(session, name="P", **kw):
    defaults = dict(category=Category.data_center, stage=Stage.entitlement,
                    status="active", score=0.5, in_territory=True, window=Window.PRE_BOD,
                    county="Los Angeles", state="CA")
    defaults.update(kw)
    p = Project(name=name, **defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _contactable_signal(session, project, name="Jane Doe", phone="555-1000", score=None):
    """A signal naming a mechanically-titled person at the project's developer
    — the shape app.ladder._classify_person recognizes as callable."""
    sig = Signal(signal_type=SignalType.ceqa_nop, category=project.category,
                stage=project.stage, project_name=project.name, county=project.county,
                state=project.state, summary_one_line="x", confidence=0.9,
                developer_or_owner="Acme Development",
                named_people=[{"name": name, "title": "Mechanical Engineer",
                             "org": "Acme Development", "phone": phone}])
    session.add(sig)
    session.commit()
    from app.models import ProjectSignal
    session.add(ProjectSignal(project_id=project.id, signal_id=sig.id,
                              match_confidence=1.0, match_method="direct"))
    session.commit()


# ---- three calls today ------------------------------------------------------

def test_three_calls_only_includes_contactable_projects(db_session, cfg):
    p1 = _project(db_session, "Reachable", score=0.9)
    _contactable_signal(db_session, p1)
    _project(db_session, "Unreachable", score=0.99)  # higher score, no contact

    calls = three_calls_today(db_session)
    assert len(calls) == 1
    assert calls[0]["project"].name == "Reachable"


def test_three_calls_caps_at_three_and_ranks_by_score(db_session, cfg):
    for i, score in enumerate([0.9, 0.5, 0.7, 0.3, 0.99]):
        p = _project(db_session, f"P{i}", score=score, county=f"County{i}")
        _contactable_signal(db_session, p, name=f"Contact {i}")

    calls = three_calls_today(db_session)
    assert len(calls) == 3
    names = [c["project"].name for c in calls]
    assert names == ["P4", "P0", "P2"]  # scores 0.99, 0.9, 0.7


def test_three_calls_reason_names_window_and_bid_estimate(db_session, cfg):
    p = _project(db_session, "P", score=0.9, window=Window.IN_BOD, days_to_estimated_bid=45)
    _contactable_signal(db_session, p)
    calls = three_calls_today(db_session)
    assert "IN-BOD" in calls[0]["reason"]
    assert "45d" in calls[0]["reason"]


def test_no_contactable_projects_returns_empty(db_session, cfg):
    _project(db_session, "Nobody callable", score=0.9)
    assert three_calls_today(db_session) == []


# ---- overdue and due --------------------------------------------------------

def test_overdue_sorts_oldest_first(db_session, cfg):
    now = utcnow()
    p1 = _project(db_session, "Recent")
    p2 = _project(db_session, "Ancient", county="Riverside")
    db_session.add(Outreach(project_id=p1.id, notes="x", next_action="call back",
                            next_action_date=now - timedelta(days=2)))
    db_session.add(Outreach(project_id=p2.id, notes="x", next_action="follow up",
                            next_action_date=now - timedelta(days=10)))
    db_session.commit()

    lines = _overdue_and_due(db_session, cfg)
    assert len(lines) == 2
    assert "Ancient" in lines[0] and "10d overdue" in lines[0]
    assert "Recent" in lines[1] and "2d overdue" in lines[1]


def test_due_soon_included_overdue_labeled_separately(db_session, cfg):
    now = utcnow()
    p = _project(db_session, "P")
    db_session.add(Outreach(project_id=p.id, notes="x", next_action="check in",
                            next_action_date=now + timedelta(days=2)))
    db_session.commit()
    lines = _overdue_and_due(db_session, cfg)
    assert len(lines) == 1
    assert lines[0].startswith("DUE:") and "due in 2d" in lines[0]


def test_far_future_next_action_not_included(db_session, cfg):
    now = utcnow()
    p = _project(db_session, "P")
    db_session.add(Outreach(project_id=p.id, notes="x", next_action="someday",
                            next_action_date=now + timedelta(days=30)))
    db_session.commit()
    assert _overdue_and_due(db_session, cfg) == []


def test_only_latest_outreach_per_project_counts(db_session, cfg):
    """A newer outreach entry supersedes an older one's next_action, same as
    the dashboard's single next_action field."""
    now = utcnow()
    p = _project(db_session, "P")
    db_session.add(Outreach(project_id=p.id, notes="first call", next_action="old action",
                            next_action_date=now - timedelta(days=20),
                            date=now - timedelta(days=5)))
    db_session.commit()
    db_session.add(Outreach(project_id=p.id, notes="second call", next_action=None,
                            next_action_date=None, date=now))
    db_session.commit()
    assert _overdue_and_due(db_session, cfg) == []


# ---- one thing worth knowing -------------------------------------------------

def test_stale_sources_skips_sources_with_no_run_ever(db_session, cfg):
    assert _stale_sources(db_session, cfg) == []


def test_stale_sources_flags_failed_source(db_session, cfg):
    db_session.add(SourceRun(source="ceqanet", ok=False, error="boom"))
    db_session.commit()
    assert "ceqanet" in _stale_sources(db_session, cfg)


def test_stale_sources_ignores_a_healthy_recent_source(db_session, cfg):
    db_session.add(SourceRun(source="ceqanet", ok=True))
    db_session.commit()
    assert "ceqanet" not in _stale_sources(db_session, cfg)


def test_quietest_county_requires_minimum_projects(db_session, cfg):
    old = utcnow() - timedelta(days=90)
    _project(db_session, "P", county="LonelyCounty", last_signal_at=old)
    assert _quietest_county(db_session) is None  # only 1 project, below the floor


def test_quietest_county_flags_real_silence(db_session, cfg):
    old = utcnow() - timedelta(days=90)
    for i in range(3):
        _project(db_session, f"P{i}", county="QuietCounty", last_signal_at=old)
    result = _quietest_county(db_session)
    assert result is not None
    assert result[0] == "QuietCounty"
    assert result[1] >= 89


def test_one_thing_priority_budget_beats_stale_source(db_session, cfg, monkeypatch):
    monkeypatch.setattr("app.spend.budget_status",
                        lambda: {"exhausted": True, "warn": False, "today_usd": 15,
                               "month_usd": 15, "daily_budget_usd": 15})
    db_session.add(SourceRun(source="ceqanet", ok=False, error="boom"))
    db_session.commit()
    result = _one_thing_worth_knowing(db_session, cfg, n_changes=5)
    assert "EXHAUSTED" in result


def test_one_thing_falls_back_to_change_count(db_session, cfg):
    result = _one_thing_worth_knowing(db_session, cfg, n_changes=4)
    assert "4 item(s) changed" in result


def test_one_thing_none_when_nothing_notable(db_session, cfg):
    assert _one_thing_worth_knowing(db_session, cfg, n_changes=0) is None
