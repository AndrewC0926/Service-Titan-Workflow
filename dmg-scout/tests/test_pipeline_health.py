"""Pipeline staleness: the root dashboard banner and the rate-limited
Resend alert, plus the CLI equivalent (`scout check-freshness`). See
app/pipeline_health.py for why this is separate from app.ops's
healthchecks.io dead man's switch."""
import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.access_log import KNOWN_IPS
from app.db import get_session
from app.models import PipelineRun, PipelineStageRun, RetrofitBuilding, StalenessAlert, utcnow
from app.pipeline_health import (
    CRON_MEMORY_LIMIT_BYTES,
    HEARTBEAT_STALE_MINUTES,
    MEMORY_WARN_FRACTION,
    RETROFIT_STALE_THRESHOLD_HOURS,
    check_and_alert_staleness,
    memory_pressure_status,
    reap_stale_runs,
    retrofit_population_hours_stale,
)
from app.web.main import app
from tests.conftest import assert_no_orm_objects, call_via_closing_session


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    app.dependency_overrides[get_session] = lambda: db_session
    # A known IP -- TestClient's default ('testclient') isn't in KNOWN_IPS,
    # so an authenticated hit here would ALSO trip app.access_log's new-IP
    # notification (a real cross-feature interference bug found 2026-08-13:
    # both features call the shared httpx.post, so the no_email fixture
    # below -- which only patches app.pipeline_health.httpx.post, the same
    # underlying httpx module object -- silently also captured the new-IP
    # email and broke every assertion expecting exactly one staleness call).
    yield TestClient(app, client=(next(iter(KNOWN_IPS)), 12345))
    app.dependency_overrides.clear()


AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def no_email(monkeypatch):
    calls = []
    monkeypatch.setattr("app.pipeline_health.httpx.post", lambda *a, **k: calls.append((a, k)) or _FakeResp())
    return calls


class _FakeResp:
    def raise_for_status(self):
        pass


def _run(db_session, *, hours_ago: float, status: str = "success"):
    started = utcnow() - timedelta(hours=hours_ago)
    run = PipelineRun(started_at=started, finished_at=started + timedelta(minutes=5),
                      status=status, records_processed=10)
    db_session.add(run)
    db_session.commit()
    return run


def _retrofit(db_session, *, population: str, hours_ago: float, apn: str | None = None):
    """One RetrofitBuilding row rebuilt `hours_ago` -- same role for the
    retrofit-freshness tests below as _run() plays for pipeline-freshness
    ones. apn only needs to be unique within a single test (the table's own
    unique constraint), so the default embeds population+hours_ago."""
    row = RetrofitBuilding(apn=apn or f"stale-test-{population}-{hours_ago}", population=population,
                           built_at=utcnow() - timedelta(hours=hours_ago))
    db_session.add(row)
    db_session.commit()
    return row


def _fresh_retrofit(db_session):
    """Seeds both populations well inside their own cadence -- for tests
    that care about pipeline-run freshness only and would otherwise trip
    the (correct, separately-tested) retrofit axis by having no
    RetrofitBuilding rows at all."""
    _retrofit(db_session, population="recently_active", hours_ago=1)
    _retrofit(db_session, population="replacement_candidate", hours_ago=1)


def test_fresh_run_shows_no_banner(client, db_session, no_email):
    _run(db_session, hours_ago=1)
    _fresh_retrofit(db_session)
    resp = client.get("/", headers=AUTH)
    assert resp.status_code == 200
    assert "Pipeline data is stale" not in resp.text
    assert no_email == []


def test_stale_run_shows_banner_and_sends_exactly_one_email(client, db_session, no_email):
    _run(db_session, hours_ago=40)
    resp = client.get("/", headers=AUTH)
    assert resp.status_code == 200
    assert "Pipeline data is stale" in resp.text
    assert "40 hours" in resp.text
    assert len(no_email) == 1
    _args, kwargs = no_email[0]
    assert kwargs["json"]["to"] == ["acrane988@gmail.com"]


def test_second_request_within_24h_sends_nothing(client, db_session, no_email):
    _run(db_session, hours_ago=40)
    client.get("/", headers=AUTH)
    client.get("/", headers=AUTH)
    client.get("/", headers=AUTH)
    assert len(no_email) == 1  # rate limited, not one per request


def test_alert_resends_after_the_cooldown_expires(db_session, no_email, monkeypatch):
    """Not rate-limited forever -- a StalenessAlert older than the cooldown
    must not suppress a new send."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    cfg = _cfg()
    _run(db_session, hours_ago=40)
    db_session.add(StalenessAlert(sent_at=utcnow() - timedelta(hours=25)))
    db_session.commit()
    result = check_and_alert_staleness(db_session, cfg)
    assert result["alert_sent"] is True
    assert len(no_email) == 1


def test_errored_run_does_not_count_as_successful(client, db_session, no_email):
    """A recent run exists, but it failed -- staleness must be measured
    against the last SUCCESSFUL run, not merely the last run of any kind."""
    _run(db_session, hours_ago=1, status="failed")
    _run(db_session, hours_ago=50, status="success")
    resp = client.get("/", headers=AUTH)
    assert "Pipeline data is stale" in resp.text
    assert len(no_email) == 1


def test_no_run_at_all_is_stale(client, db_session, no_email):
    resp = client.get("/", headers=AUTH)
    assert "Pipeline data is stale" in resp.text
    assert "No successful pipeline run has ever been recorded" in resp.text
    assert len(no_email) == 1


# ---- retrofit_buildings freshness: the pipeline-run alarm above must not
# be the only thing standing between a broken retrofit rebuild and nobody
# ever finding out -- see app.pipeline_health's module docstring. ----------

def test_retrofit_population_hours_stale_is_none_when_never_built(db_session):
    assert retrofit_population_hours_stale(db_session, "recently_active") is None


def test_retrofit_population_hours_stale_measures_from_latest_built_at(db_session):
    _retrofit(db_session, population="recently_active", hours_ago=5)
    hrs = retrofit_population_hours_stale(db_session, "recently_active")
    assert 4.9 <= hrs <= 5.1


def test_retrofit_population_hours_stale_ignores_the_other_population(db_session):
    _retrofit(db_session, population="recently_active", hours_ago=5)
    assert retrofit_population_hours_stale(db_session, "replacement_candidate") is None


def test_never_built_retrofit_population_is_stale_even_with_a_fresh_pipeline(client, db_session, no_email):
    """The exact gap this whole check exists to close: a pipeline that has
    never run build-retrofit-buildings/find-replacement-candidates at all
    must not read as fresh just because fetch/triage/.../notify are fine."""
    _run(db_session, hours_ago=1)  # pipeline itself is fresh
    resp = client.get("/", headers=AUTH)
    assert "Pipeline data is stale" in resp.text
    assert "recently_active" in resp.text
    assert "replacement_candidate" in resp.text
    assert len(no_email) == 1


def test_retrofit_population_within_its_own_threshold_is_not_stale(db_session, no_email, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    _run(db_session, hours_ago=1)
    _retrofit(db_session, population="recently_active", hours_ago=RETROFIT_STALE_THRESHOLD_HOURS["recently_active"] - 1)
    _retrofit(db_session, population="replacement_candidate",
             hours_ago=RETROFIT_STALE_THRESHOLD_HOURS["replacement_candidate"] - 1)
    result = check_and_alert_staleness(db_session, _cfg())
    assert result["stale"] is False
    assert result["retrofit"]["recently_active"]["stale"] is False
    assert result["retrofit"]["replacement_candidate"]["stale"] is False
    assert no_email == []


def test_retrofit_population_past_its_own_cadence_trips_the_alarm_alone(db_session, no_email, monkeypatch):
    """recently_active (daily cadence) stale, replacement_candidate (weekly)
    still fine, pipeline itself fine -- proves the two retrofit axes are
    independent of each other and of the pipeline-run axis, not one shared
    boolean that can't tell which thing actually broke."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    _run(db_session, hours_ago=1)
    _retrofit(db_session, population="recently_active",
             hours_ago=RETROFIT_STALE_THRESHOLD_HOURS["recently_active"] + 1)
    _retrofit(db_session, population="replacement_candidate", hours_ago=1)

    result = check_and_alert_staleness(db_session, _cfg())

    assert result["stale"] is True
    assert result["retrofit"]["recently_active"]["stale"] is True
    assert result["retrofit"]["replacement_candidate"]["stale"] is False
    assert len(no_email) == 1
    _args, kwargs = no_email[0]
    assert "recently_active" in kwargs["json"]["text"]
    assert "replacement_candidate" not in kwargs["json"]["text"]


def _cfg():
    from app.config import load_config
    return load_config()


def test_reap_stale_runs_reclassifies_a_hard_killed_process(db_session):
    """Simulates the real failure mode (confirmed 2026-08-13: Render's OOM
    killer took the cron mid-fetch): the process is torn down from outside,
    so finish_pipeline_run() never runs and heartbeat_at simply stops
    advancing. Nothing in-process can distinguish that from a run that is
    merely between heartbeats -- the row itself carries no signal that its
    process is gone. reap_stale_runs() is the only thing that can tell the
    two apart, and only by heartbeat age."""
    started = utcnow() - timedelta(hours=2)
    dead = PipelineRun(
        status="running",
        started_at=started,
        heartbeat_at=utcnow() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1),
    )
    db_session.add(dead)
    db_session.commit()
    db_session.refresh(dead)
    dead_id = dead.id

    # Before reaping, the row is exactly what a stuck row looks like: no
    # trace in the data itself that the process behind it is gone.
    assert db_session.get(PipelineRun, dead_id).status == "running"

    reaped = reap_stale_runs(db_session)

    assert reaped == 1
    row = db_session.get(PipelineRun, dead_id)
    assert row.status == "failed"
    assert row.finished_at is not None
    assert "reaped" in row.error.lower()
    # And it must not still be selectable as a running row -- the whole
    # point is that nothing downstream (last_successful_run, the dashboard,
    # a fresh `scout pipeline` invocation) can mistake it for live.
    still_running = db_session.exec(
        select(PipelineRun).where(PipelineRun.status == "running")
    ).all()
    assert dead_id not in [r.id for r in still_running]


def test_reap_stale_runs_catches_legacy_rows_with_no_heartbeat_at_all(db_session):
    """The three real production rows (ids 3, 4, 5) this fix has to clean up
    predate the heartbeat_at column -- they were created, killed by the same
    OOM/composition bugs, and left at status="running" with heartbeat_at
    NULL forever, since nothing ever wrote to a column that didn't exist
    yet. A NULL heartbeat on an old row is exactly as dead as a stale one."""
    legacy = PipelineRun(status="running", started_at=utcnow() - timedelta(hours=5), heartbeat_at=None)
    db_session.add(legacy)
    db_session.commit()
    db_session.refresh(legacy)

    reaped = reap_stale_runs(db_session)

    assert reaped == 1
    row = db_session.get(PipelineRun, legacy.id)
    assert row.status == "failed"


def test_reap_stale_runs_leaves_a_genuinely_live_run_alone(db_session):
    """A run with a heartbeat inside the threshold is still alive and must
    not be reaped -- this is the case that distinguishes reap_stale_runs()
    from something that just fails every "running" row on a timer."""
    alive = PipelineRun(status="running", started_at=utcnow() - timedelta(hours=2), heartbeat_at=utcnow())
    db_session.add(alive)
    db_session.commit()
    db_session.refresh(alive)

    reaped = reap_stale_runs(db_session)

    assert reaped == 0
    assert db_session.get(PipelineRun, alive.id).status == "running"


def test_check_and_alert_staleness_reaps_before_checking(db_session, no_email):
    """The staleness check itself must not be fooled by a stuck row --
    without reaping first, a dead run sitting at status="running" would
    correctly be excluded from last_successful_run() (it isn't "success"),
    so this specifically guards that reap_stale_runs() runs as part of the
    normal staleness path, not just when someone remembers to call it."""
    dead = PipelineRun(
        status="running",
        started_at=utcnow() - timedelta(hours=2),
        heartbeat_at=utcnow() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1),
    )
    db_session.add(dead)
    db_session.commit()

    check_and_alert_staleness(db_session, _cfg())

    assert db_session.get(PipelineRun, dead.id).status == "failed"


def test_check_freshness_cli_exits_nonzero_when_stale(db_session, monkeypatch):
    from typer.testing import CliRunner

    from app.cli import app as cli_app

    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr("app.pipeline_health.httpx.post", lambda *a, **k: calls.append((a, k)) or _FakeResp())
    _run(db_session, hours_ago=40)

    result = CliRunner().invoke(cli_app, ["check-freshness"])
    assert result.exit_code == 1
    assert "STALE" in result.output
    assert len(calls) == 1


# ---- peak memory survives its own session closing --------------------------
#
# memory_pressure_status() and check_and_alert_staleness() are exactly the
# shape that broke in production before: functions that take an OPEN session,
# query PipelineRun/PipelineStageRun, and hand a result back to a caller
# (scout check-freshness, the root dashboard view) that reads it only AFTER
# its own session_scope() has committed and closed. Every test above calls
# these with db_session, whose session never closes during a test -- so none
# of them could have caught memory_pressure_status returning {"run": run}
# (a live ORM object) instead of {"run_id": run.id}, the actual bug this
# session fixed. These use call_via_closing_session/assert_no_orm_objects
# (see tests/conftest.py) specifically to close that gap.

def test_memory_pressure_status_survives_a_closed_session(db_session):
    run = PipelineRun(status="success", started_at=utcnow(), finished_at=utcnow(),
                      peak_rss_bytes=int(CRON_MEMORY_LIMIT_BYTES * 0.5))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    db_session.add(PipelineStageRun(pipeline_run_id=run.id, stage="fetch",
                                    peak_rss_bytes=int(CRON_MEMORY_LIMIT_BYTES * 0.4)))
    db_session.commit()

    mem = call_via_closing_session(memory_pressure_status)

    assert_no_orm_objects(mem)
    # Every field a real caller (app.cli:check_freshness_cmd, health.html) reads:
    assert mem["run_id"] == run.id
    assert mem["peak_bytes"] == int(CRON_MEMORY_LIMIT_BYTES * 0.5)  # max(run, stages)
    assert mem["limit_bytes"] == CRON_MEMORY_LIMIT_BYTES
    assert mem["warn"] is False


def test_check_and_alert_staleness_survives_a_closed_session(db_session, no_email, monkeypatch):
    """Same proof as above, but through check_and_alert_staleness -- the
    actual function scout check-freshness and the root dashboard view call
    -- with a run over the memory-warn threshold, so the "memory" reason
    string (built from mem['run_id'], not a live PipelineRun) is exercised
    too, not just the happy path."""
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    _run(db_session, hours_ago=1, status="success")
    db_session.exec(select(PipelineRun)).one().peak_rss_bytes = int(
        CRON_MEMORY_LIMIT_BYTES * (MEMORY_WARN_FRACTION + 0.05))
    db_session.commit()
    _fresh_retrofit(db_session)

    result = call_via_closing_session(check_and_alert_staleness, _cfg())

    assert_no_orm_objects(result)
    assert result["memory"]["warn"] is True
    assert result["stale"] is True  # memory pressure alone trips it
    assert len(no_email) == 1
    assert "memory pressure" in no_email[0][1]["json"]["subject"].lower()
