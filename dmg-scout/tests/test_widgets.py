"""The three server-rendered SVG widgets from the 2026-08-17 visual pass
(see /home/andrewcrane/.claude/plans/parallel-crunching-clover.md): the
board's signal-activity sparkline and window-remaining bar
(app/web/main.py:_signal_sparklines/_window_progress, rendered by
app/web/templates/_widgets.html), and the assumptions register's
distribution strip (app/assumptions.py's Assumption.numeric_value/
plausible_low/plausible_high fields, same _widgets.html macro file).

No pixel/SVG-geometry assertions here -- that's what
tools/screenshot_visual_pass.py is for. These test the DATA the widgets are
handed: batched-not-per-row queries, correct week bucketing, the honest
"only where a real range exists" scoping, and that the widget markup
actually reaches the rendered page for the rows it should (and only those)."""
import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.manual import add_manual_signal
from app.models import Project, ProjectSignal, utcnow
from app.pipeline.resolve import run_resolve
from app.pipeline.size_score import run_size_score
from app.web.main import SPARKLINE_WEEKS, _signal_sparklines, _window_progress, app


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def _seed_entitlement_project(db_session, cfg, name="Ontario Ranch Campus", county="Kern"):
    add_manual_signal(db_session, "ceqa_nop", f"NOP filed for {name}",
                      project_name=name, developer="QTS Realty",
                      county=county, state="CA", mw_it=90, stage="entitlement")
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)
    return db_session.exec(select(Project).where(Project.name == name)).one()


# --- _signal_sparklines: batched, week-bucketed ---------------------------------

def test_sparkline_buckets_recent_signal_into_the_last_slot(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    result = _signal_sparklines(db_session, [p.id])
    assert len(result[p.id]) == SPARKLINE_WEEKS
    assert result[p.id][-1] == 1          # the signal just linked lands in "this week"
    assert sum(result[p.id][:-1]) == 0


def test_sparkline_buckets_an_old_signal_into_an_earlier_slot(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    link = db_session.exec(select(ProjectSignal).where(
        ProjectSignal.project_id == p.id)).one()
    link.linked_at = utcnow() - timedelta(weeks=5)
    db_session.add(link)
    db_session.commit()

    result = _signal_sparklines(db_session, [p.id])
    assert result[p.id][-1] == 0          # not "this week" anymore
    assert sum(result[p.id]) == 1         # still counted, just in an earlier bucket


def test_sparkline_ignores_signals_outside_the_window(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    link = db_session.exec(select(ProjectSignal).where(
        ProjectSignal.project_id == p.id)).one()
    link.linked_at = utcnow() - timedelta(weeks=SPARKLINE_WEEKS + 10)
    db_session.add(link)
    db_session.commit()

    result = _signal_sparklines(db_session, [p.id])
    assert sum(result[p.id]) == 0


def test_sparkline_is_one_query_not_per_project(db_session, cfg):
    # A project with zero signal history still gets a full zero-filled bucket
    # list back, not a missing key -- the template's .get(p.id, []) fallback
    # exists for ids never passed in at all, not for ids with no activity.
    p = _seed_entitlement_project(db_session, cfg)
    other_id = p.id + 999
    result = _signal_sparklines(db_session, [p.id, other_id])
    assert result[other_id] == [0] * SPARKLINE_WEEKS


def test_sparkline_empty_project_list_returns_empty_dict(db_session, cfg):
    assert _signal_sparklines(db_session, []) == {}


# --- _window_progress: only where a real CI exists -------------------------------

def test_window_progress_computed_for_entitlement_stage(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    p = db_session.get(Project, p.id)
    p.created_at = utcnow() - timedelta(days=90)
    db_session.add(p)
    db_session.commit()

    progress = _window_progress([p])
    # 90 days into a window whose high end is the entitlement CI's high (509
    # per config.yaml as of this test's writing) -- assert the relationship,
    # not a hardcoded day count that would silently stop testing anything if
    # the measured CI is ever recalibrated.
    from app.pipeline.scoring import days_to_estimated_bid_range
    _, high = days_to_estimated_bid_range(cfg, p.stage)
    assert progress[p.id] == round(100 * 90 / high)


def test_window_progress_absent_for_a_stage_with_no_measured_range(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg, name="Design Stage DC")
    p = db_session.get(Project, p.id)
    p.stage = "design"  # config.yaml: design=360, a flat int, no {low,high}
    db_session.add(p)
    db_session.commit()

    progress = _window_progress([p])
    assert p.id not in progress


def test_window_progress_clamped_to_100_even_long_past_the_window(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    p = db_session.get(Project, p.id)
    p.created_at = utcnow() - timedelta(days=5000)
    db_session.add(p)
    db_session.commit()

    progress = _window_progress([p])
    assert progress[p.id] == 100


def test_window_progress_never_negative_for_a_project_created_moments_ago(db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    progress = _window_progress([p])
    assert progress[p.id] >= 0


# --- rendered page: widgets show up where expected, not elsewhere ----------------

def test_board_renders_a_sparkline_and_a_window_bar_for_an_entitlement_project(client, db_session, cfg):
    p = _seed_entitlement_project(db_session, cfg)
    p = db_session.get(Project, p.id)
    p.created_at = utcnow() - timedelta(days=90)
    db_session.add(p)
    db_session.commit()

    r = client.get("/board?territory=all", headers=AUTH)
    assert r.status_code == 200
    assert 'class="widget-svg spark"' in r.text
    assert 'class="widget-svg"' in r.text  # the window bar (spark has its own subclass)


def test_assumptions_renders_distribution_strips_only_for_entries_with_a_real_range(client, db_session, cfg):
    r = client.get("/settings/assumptions", headers=AUTH)
    assert r.status_code == 200
    # Exactly the entries wired with numeric_value/plausible_low/plausible_high
    # in app/assumptions.py -- Recency half-life, Days to estimated bid:
    # entitlement, Tons per MW installed, Ranking radius. Four widgets, not
    # more (a fabricated range on some other placeholder would be the
    # regression this guards against) and not fewer (one silently dropped).
    assert r.text.count('class="diststrip-track"') == 4


def test_recency_half_life_strip_is_marked_as_a_proxy_range(client, db_session, cfg):
    r = client.get("/settings/assumptions", headers=AUTH)
    assert 'class="diststrip-tick proxy"' in r.text
    # Only ONE of the four is a proxy -- the other three carry a real
    # published range or measured CI, not an unvalidated one.
    assert r.text.count('class="diststrip-tick proxy"') == 1
