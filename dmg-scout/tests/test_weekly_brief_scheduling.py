"""Block 4C Item 6 (Master Plan v3.6 section 42/43): the automated Friday
06:00 Pacific per-user Weekly Brief run (app.pipeline.weekly_brief.
run_weekly_briefs) -- who gets one, when Resend actually sends it, and
what /reports shows when it didn't. See tests/test_weekly_brief.py for
the Block 4B Item 3 manual single-brief path this builds on."""
import base64
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.config import load_config
from app.db import get_session
from app.models import (
    Disposition, DecisionNote, LeadSource, NoteType, Opportunity, Outcome, PenState, Signal, SignalType,
    WeeklyBrief, utcnow,
)
from app.pipeline.weekly_brief import (
    active_users_this_week, latest_batch_email_status, run_weekly_briefs, send_weekly_brief_email,
    verified_sending_domain,
)
from app.web.main import app


class _FakeResp:
    def __init__(self, json_data=None):
        self._json = json_data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _raise_http_error(*_a, **_k):
    raise httpx.HTTPError("boom")


def _opportunity(db_session, **kw):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    kw = {"owner_user": "andrew", "pen_state": PenState.not_moved, **kw}
    opp = Opportunity(signal_id=signal.id, account_id=1, **kw)
    db_session.add(opp)
    db_session.commit()
    return opp


class TestActiveUsersThisWeek:
    def test_empty_with_no_activity(self, db_session):
        assert active_users_this_week(db_session, utcnow() - timedelta(days=7), utcnow()) == []

    def test_finds_outcome_and_note_authors_deduped_and_sorted(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="andrew"))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="jason"))
        db_session.commit()
        users = active_users_this_week(db_session, utcnow() - timedelta(days=7), utcnow())
        assert users == ["andrew", "jason"]

    def test_ownership_alone_without_a_note_or_outcome_does_not_count(self, db_session):
        _opportunity(db_session, owner_user="jason")
        assert active_users_this_week(db_session, utcnow() - timedelta(days=7), utcnow()) == []

    def test_excludes_activity_outside_the_window(self, db_session):
        opp = _opportunity(db_session)
        old = utcnow() - timedelta(days=30)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected,
                               created_at=old))
        db_session.commit()
        assert active_users_this_week(db_session, utcnow() - timedelta(days=7), utcnow()) == []


class TestVerifiedSendingDomain:
    def test_none_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        assert verified_sending_domain(load_config()) is None

    def test_returns_the_verified_domain_name(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get", lambda *a, **k: _FakeResp({"data": [
            {"name": "unverified.example.com", "status": "pending"},
            {"name": "dmgscout.com", "status": "verified"},
        ]}))
        assert verified_sending_domain(load_config()) == "dmgscout.com"

    def test_none_when_no_domain_is_verified(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get",
                            lambda *a, **k: _FakeResp({"data": [{"name": "x.com", "status": "pending"}]}))
        assert verified_sending_domain(load_config()) is None

    def test_none_on_http_error(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get", _raise_http_error)
        assert verified_sending_domain(load_config()) is None


_PAYLOAD = {"week_start": "2026-09-01T00:00:00", "week_end": "2026-09-08T00:00:00",
           "recommendation": ["line one"], "one_lead_do": None}


class TestSendWeeklyBriefEmail:
    def test_sends_and_returns_true(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        calls = []
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.post",
                            lambda *a, **k: calls.append((a, k)) or _FakeResp())
        sent = send_weekly_brief_email(load_config(), to_email="andrew@x.com", domain="dmgscout.com",
                                       payload=_PAYLOAD)
        assert sent is True
        assert calls[0][1]["json"]["from"] == "weekly-brief@dmgscout.com"
        assert calls[0][1]["json"]["to"] == ["andrew@x.com"]

    def test_false_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        assert send_weekly_brief_email(load_config(), to_email="a@b.com", domain="d.com",
                                       payload=_PAYLOAD) is False

    def test_false_on_http_error(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.post", _raise_http_error)
        assert send_weekly_brief_email(load_config(), to_email="a@b.com", domain="d.com",
                                       payload=_PAYLOAD) is False


class TestRunWeeklyBriefs:
    def test_archives_one_row_per_active_user(self, db_session, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="jason"))
        db_session.commit()
        result = run_weekly_briefs(db_session, load_config())
        assert len(result["users"]) == 2
        rows = db_session.exec(select(WeeklyBrief)).all()
        assert {r.generated_by for r in rows} == {"andrew", "jason"}

    def test_no_verified_domain_archives_without_emailing(self, db_session, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        row = db_session.exec(select(WeeklyBrief).where(WeeklyBrief.generated_by == "andrew")).first()
        assert row.emailed is False
        assert row.email_skip_reason == "no verified Resend sending domain"

    def test_verified_domain_and_configured_email_sends(self, db_session, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get", lambda *a, **k: _FakeResp(
            {"data": [{"name": "dmgscout.com", "status": "verified"}]}))
        sent_calls = []
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.post",
                            lambda *a, **k: sent_calls.append((a, k)) or _FakeResp())
        opp = _opportunity(db_session)
        # andrew has an email configured in config.yaml's dashboard.users
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        row = db_session.exec(select(WeeklyBrief).where(WeeklyBrief.generated_by == "andrew")).first()
        assert row.emailed is True
        assert row.email_skip_reason is None
        assert len(sent_calls) == 1

    def test_verified_domain_but_no_email_configured_for_user(self, db_session, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get", lambda *a, **k: _FakeResp(
            {"data": [{"name": "dmgscout.com", "status": "verified"}]}))
        opp = _opportunity(db_session)
        # jason is not in config.yaml's dashboard.users -- no email on file
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="jason"))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        row = db_session.exec(select(WeeklyBrief).where(WeeklyBrief.generated_by == "jason")).first()
        assert row.emailed is False
        assert row.email_skip_reason == "no email configured for user"

    def test_second_run_within_the_same_week_does_not_duplicate(self, db_session, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        result2 = run_weekly_briefs(db_session, load_config())
        rows = db_session.exec(select(WeeklyBrief).where(WeeklyBrief.generated_by == "andrew")).all()
        assert len(rows) == 1
        assert result2["users"][0]["skipped"] is True


class TestLatestBatchEmailStatus:
    def test_none_when_no_briefs_exist(self, db_session):
        assert latest_batch_email_status(db_session) is None

    def test_reports_the_latest_weeks_counts_and_reasons(self, db_session, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="jason"))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        status = latest_batch_email_status(db_session)
        assert status["emailed_count"] == 0
        assert status["skipped_count"] == 2
        # no RESEND_API_KEY at all -- neither user's own email situation
        # (andrew has one configured, jason doesn't) even gets checked.
        assert status["skip_reasons"] == ["no verified Resend sending domain"]

    def test_mixed_skip_reasons_with_a_verified_domain(self, db_session, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.get", lambda *a, **k: _FakeResp(
            {"data": [{"name": "dmgscout.com", "status": "verified"}]}))
        monkeypatch.setattr("app.pipeline.weekly_brief.httpx.post", lambda *a, **k: _FakeResp())
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="jason"))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        status = latest_batch_email_status(db_session)
        assert status["emailed_count"] == 1  # andrew, who has an email on file
        assert status["skipped_count"] == 1  # jason, who doesn't
        assert status["skip_reasons"] == ["no email configured for user"]


class TestInternalWeeklyBriefsRoute:
    @pytest.fixture()
    def client(self, db_session, monkeypatch):
        monkeypatch.setenv("WEEKLY_BRIEF_API_KEY", "test-weekly-key")
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        app.dependency_overrides[get_session] = lambda: db_session
        yield TestClient(app)
        app.dependency_overrides.clear()

    def test_requires_auth(self, client):
        assert client.post("/internal/weekly-briefs").status_code == 401

    def test_wrong_token_is_401(self, client):
        resp = client.post("/internal/weekly-briefs", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_503_when_key_not_configured(self, db_session, monkeypatch):
        monkeypatch.delenv("WEEKLY_BRIEF_API_KEY", raising=False)
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        resp = client.post("/internal/weekly-briefs", headers={"Authorization": "Bearer anything"})
        app.dependency_overrides.clear()
        assert resp.status_code == 503

    def test_archives_active_users(self, client, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        resp = client.post("/internal/weekly-briefs", headers={"Authorization": "Bearer test-weekly-key"})
        assert resp.status_code == 200
        assert resp.json()["users"][0]["user"] == "andrew"


class TestReportsWeeklyBriefNote:
    def _auth(self):
        return {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}

    def test_note_shown_when_the_latest_batch_skipped_emails(self, db_session, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        run_weekly_briefs(db_session, load_config())
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        resp = client.get("/reports", headers=self._auth())
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "no verified Resend sending domain" in resp.text

    def test_no_note_when_nothing_has_run(self, db_session, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        resp = client.get("/reports", headers=self._auth())
        app.dependency_overrides.clear()
        assert resp.status_code == 200
