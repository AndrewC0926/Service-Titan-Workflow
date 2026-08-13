"""Pipeline staleness: the root dashboard banner and the rate-limited
Resend alert, plus the CLI equivalent (`scout check-freshness`). See
app/pipeline_health.py for why this is separate from app.ops's
healthchecks.io dead man's switch."""
import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import PipelineRun, StalenessAlert, utcnow
from app.pipeline_health import check_and_alert_staleness
from app.web.main import app


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
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


def test_fresh_run_shows_no_banner(client, db_session, no_email):
    _run(db_session, hours_ago=1)
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


def _cfg():
    from app.config import load_config
    return load_config()


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
