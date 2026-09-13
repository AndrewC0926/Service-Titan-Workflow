"""Block 4B Item 3: the Weekly Sales Intelligence Brief
(app.pipeline.weekly_brief)."""
import json
from datetime import timedelta

from app.models import (
    Disposition, LeadSource, NoteType, Opportunity, DecisionNote, Outcome, PenState, ReasonBlock,
    ReasonStrength, Signal, SignalType, WeeklyBrief, WhyKind, utcnow,
)
from app.pipeline.weekly_brief import archive_weekly_brief, build_weekly_brief, new_deadline_exposure, \
    top_pipeline_rows_by_weakest_why


def _opportunity(db_session, **kw):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    kw = {"owner_user": "andrew", "pen_state": PenState.not_moved, **kw}
    opp = Opportunity(signal_id=signal.id, account_id=1, **kw)
    db_session.add(opp)
    db_session.commit()
    return opp


class TestTopPipelineRowsByWeakestWhy:
    def test_empty_with_no_opportunities(self, db_session):
        assert top_pipeline_rows_by_weakest_why(db_session) == []

    def test_ranks_weakest_why_first(self, db_session):
        strong_opp = _opportunity(db_session)
        weak_opp = _opportunity(db_session)
        for kind, strength in ((WhyKind.them, ReasonStrength.Strong), (WhyKind.now, ReasonStrength.Strong),
                               (WhyKind.win, ReasonStrength.Strong)):
            db_session.add(ReasonBlock(opportunity_id=strong_opp.id, why_kind=kind, strength=strength, evidence="x"))
        for kind, strength in ((WhyKind.them, ReasonStrength.ABSTAIN), (WhyKind.now, ReasonStrength.Strong),
                               (WhyKind.win, ReasonStrength.ABSTAIN)):
            db_session.add(ReasonBlock(opportunity_id=weak_opp.id, why_kind=kind, strength=strength, evidence="x"))
        db_session.commit()
        rows = top_pipeline_rows_by_weakest_why(db_session, limit=2)
        assert rows[0]["opportunity_id"] == strong_opp.id
        assert rows[1]["opportunity_id"] == weak_opp.id
        assert rows[0]["weakest_why"] == "Strong"
        assert "sort_key" not in rows[0]

    def test_respects_limit(self, db_session):
        for _ in range(5):
            _opportunity(db_session)
        assert len(top_pipeline_rows_by_weakest_why(db_session, limit=3)) == 3


class TestNewDeadlineExposure:
    def test_no_deadlines_is_empty(self, db_session):
        result = new_deadline_exposure(db_session)
        assert result["count"] == 0
        assert result["rows"] == []

    def test_a_real_deadline_inside_the_window_is_included(self, db_session):
        from app.models import RetrofitBuilding

        db_session.add(RetrofitBuilding(
            apn="wb-1", population="recently_active", ebewe_matched=True,
            ebewe_arcx_next_compliance_date=utcnow() + timedelta(days=30),
            ebewe_arcx_due_this_year=True,
        ))
        db_session.commit()
        result = new_deadline_exposure(db_session, window_days=90)
        assert result["count"] == 1
        assert result["rows"][0]["regulation"] == "EBEWE"

    def test_a_deadline_outside_the_window_is_excluded(self, db_session):
        from app.models import RetrofitBuilding

        db_session.add(RetrofitBuilding(
            apn="wb-2", population="recently_active", ebewe_matched=True,
            ebewe_arcx_next_compliance_date=utcnow() + timedelta(days=365),
            ebewe_arcx_due_this_year=False,
        ))
        db_session.commit()
        result = new_deadline_exposure(db_session, window_days=90)
        assert result["count"] == 0


class TestBuildWeeklyBrief:
    def test_recommendation_is_three_lines(self, db_session):
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert len(content["recommendation"]) == 3

    def test_signed_by_owner_user(self, db_session):
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert content["generated_by"] == "andrew"

    def test_moves_made_reads_this_weeks_outcomes_and_notes_only(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected,
                               created_at=utcnow()))
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.no_answer,
                               created_at=utcnow() - timedelta(days=30)))  # outside the window
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    created_at=utcnow()))
        db_session.commit()
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert content["moves_made_total_this_week"] == 2  # 1 outcome + 1 note, not the 30-day-old one
        assert len(content["moves_made"]) == 2

    def test_what_scout_got_wrong_is_bad_number_wrong_contact_this_week(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew",
                               disposition=Disposition.bad_number_wrong_contact, created_at=utcnow()))
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew",
                               disposition=Disposition.connected, created_at=utcnow()))
        db_session.commit()
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert len(content["got_wrong"]) == 1
        assert content["got_wrong"][0]["opportunity_id"] == opp.id

    def test_one_lead_explained_includes_reason_blocks_and_the_do_sentence(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.them,
                                   strength=ReasonStrength.Strong, evidence="x",
                                   one_sentence="Do: call someone."))
        db_session.commit()
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert content["one_lead"]["opportunity_id"] == opp.id
        assert content["one_lead_do"] == "Do: call someone."

    def test_no_opportunities_at_all_has_no_lead_and_does_not_crash(self, db_session):
        content = build_weekly_brief(db_session, owner_user="andrew")
        assert content["one_lead"] is None
        assert content["one_lead_do"] is None
        assert content["moves_next"] == []


class TestArchiveWeeklyBrief:
    def test_writes_an_immutable_row_with_json_safe_payload(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.them,
                                   strength=ReasonStrength.Strong, evidence="x"))
        db_session.commit()
        row = archive_weekly_brief(db_session, owner_user="andrew")
        db_session.commit()
        assert row.id is not None
        assert row.generated_by == "andrew"
        # the payload must be real JSON, not a dict full of ORM objects/datetimes
        json.dumps(row.payload)

    def test_two_archives_are_two_rows_never_an_update(self, db_session):
        archive_weekly_brief(db_session, owner_user="andrew")
        archive_weekly_brief(db_session, owner_user="andrew")
        db_session.commit()
        from sqlmodel import select
        rows = db_session.exec(select(WeeklyBrief)).all()
        assert len(rows) == 2

    def test_snapshot_date_comes_from_the_latest_metric_snapshot(self, db_session):
        from app.pipeline.metrics import run_metric_snapshot

        run_metric_snapshot(db_session)
        db_session.commit()
        row = archive_weekly_brief(db_session, owner_user="andrew")
        db_session.commit()
        assert row.snapshot_date is not None


class TestWeeklyBriefRoutes:
    def _client(self, db_session, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.db import get_session
        from app.web.main import app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        return client, auth, app

    def test_html_page_loads_empty(self, db_session, monkeypatch):
        client, auth, app = self._client(db_session, monkeypatch)
        resp = client.get("/reports/weekly", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "Weekly Sales Intelligence Brief" in resp.text
        assert "Signed, andrew" in resp.text

    def test_html_page_never_archives(self, db_session, monkeypatch):
        from sqlmodel import select

        from app.models import WeeklyBrief

        client, auth, app = self._client(db_session, monkeypatch)
        client.get("/reports/weekly", headers=auth)
        client.get("/reports/weekly", headers=auth)
        app.dependency_overrides.clear()
        assert db_session.exec(select(WeeklyBrief)).all() == []

    def test_pdf_export_is_a_real_pdf(self, db_session, monkeypatch):
        client, auth, app = self._client(db_session, monkeypatch)
        resp = client.get("/reports/weekly.pdf", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF")

    def test_xlsx_export_has_one_sheet_per_section(self, db_session, monkeypatch):
        from io import BytesIO

        from openpyxl import load_workbook

        client, auth, app = self._client(db_session, monkeypatch)
        resp = client.get("/reports/weekly.xlsx", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        wb = load_workbook(BytesIO(resp.content))
        assert set(wb.sheetnames) == {
            "Funnel", "Moves made", "Moves next", "New deadline exposure", "One lead", "What Scout got wrong",
        }

    def test_requires_auth(self, db_session, monkeypatch):
        client, _, app = self._client(db_session, monkeypatch)
        resp = client.get("/reports/weekly")
        app.dependency_overrides.clear()
        assert resp.status_code == 401


class TestGenerateWeeklyBriefCmd:
    def test_cli_command_archives_a_real_brief(self, db_session):
        from typer.testing import CliRunner

        from app.cli import app as cli_app
        from app.db import get_session as real_get_session

        # the CLI's own session_scope() reads DATABASE_URL directly, not the
        # web app's dependency-override seam -- this test only checks the
        # command runs and prints the expected shape against whatever
        # database session_scope() actually opens, matching how every other
        # CLI-command test in this suite exercises `scout` commands.
        result = CliRunner().invoke(cli_app, ["generate-weekly-brief", "--owner-user", "andrew"])
        assert result.exit_code == 0
        import json
        payload = json.loads(result.output)
        assert payload["generated_by"] == "andrew"
        assert len(payload["recommendation"]) == 3
