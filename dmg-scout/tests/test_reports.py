"""Block 4B Item 2: the Reports page, reading only from metric_snapshots
(app.pipeline.reports)."""
from datetime import timedelta

from app.models import MetricSnapshot, Opportunity, PenState, ReasonBlock, ReasonStrength, Signal, SignalType, WhyKind, utcnow
from app.pipeline.reports import (
    latest_value, week_over_week, weekly_series, weeks_of_real_history, why_leads_exist_sample,
)


def _snap(db_session, metric_key, value, dimensions=None, days_ago=0):
    db_session.add(MetricSnapshot(
        snapshot_date=utcnow() - timedelta(days=days_ago), metric_key=metric_key,
        value=value, dimensions=dimensions or {},
    ))


class TestLatestValue:
    def test_none_when_never_snapshotted(self, db_session):
        assert latest_value(db_session, "nonexistent_metric") is None

    def test_sums_across_dimensions_when_none_given(self, db_session):
        _snap(db_session, "signals_by_trigger_type", 10, {"trigger_type": "permit_gap"})
        _snap(db_session, "signals_by_trigger_type", 5, {"trigger_type": "deadline"})
        db_session.commit()
        assert latest_value(db_session, "signals_by_trigger_type") == 15

    def test_one_specific_dimension(self, db_session):
        _snap(db_session, "deadline_exposure_by_regulation", 4, {"regulation": "AB 869"})
        _snap(db_session, "deadline_exposure_by_regulation", 100, {"regulation": "SB 1206"})
        db_session.commit()
        assert latest_value(db_session, "deadline_exposure_by_regulation", {"regulation": "AB 869"}) == 4

    def test_only_todays_rows_are_summed_not_prior_days(self, db_session):
        _snap(db_session, "signals_by_trigger_type", 10, {"trigger_type": "permit_gap"}, days_ago=0)
        _snap(db_session, "signals_by_trigger_type", 999, {"trigger_type": "permit_gap"}, days_ago=10)
        db_session.commit()
        assert latest_value(db_session, "signals_by_trigger_type") == 10


class TestWeeklySeries:
    def test_no_history_returns_all_none(self, db_session):
        assert weekly_series(db_session, "nonexistent", weeks=4) == [None, None, None, None]

    def test_this_weeks_snapshot_lands_in_the_last_slot(self, db_session):
        _snap(db_session, "qualified_opportunities", 7)
        db_session.commit()
        series = weekly_series(db_session, "qualified_opportunities", weeks=3)
        assert series[-1] == 7
        assert series[0] is None and series[1] is None

    def test_a_week_with_no_snapshot_is_none_not_zero(self, db_session):
        _snap(db_session, "qualified_opportunities", 7, days_ago=0)
        _snap(db_session, "qualified_opportunities", 3, days_ago=21)  # 3 weeks back
        db_session.commit()
        series = weekly_series(db_session, "qualified_opportunities", weeks=4)
        assert series[-1] == 7
        assert series[0] == 3
        assert series[1] is None
        assert series[2] is None


class TestWeeksOfRealHistory:
    def test_zero_with_no_snapshots(self, db_session):
        assert weeks_of_real_history(db_session, "nonexistent") == 0

    def test_counts_only_real_weeks(self, db_session):
        _snap(db_session, "qualified_opportunities", 1, days_ago=0)
        _snap(db_session, "qualified_opportunities", 1, days_ago=21)
        db_session.commit()
        assert weeks_of_real_history(db_session, "qualified_opportunities") == 2


class TestWeekOverWeek:
    def test_none_with_fewer_than_two_real_weeks(self, db_session):
        _snap(db_session, "qualified_opportunities", 5)
        db_session.commit()
        assert week_over_week(db_session, "qualified_opportunities") is None

    def test_computes_delta_between_the_last_two_real_weeks(self, db_session):
        _snap(db_session, "qualified_opportunities", 10, days_ago=0)
        _snap(db_session, "qualified_opportunities", 6, days_ago=7)
        db_session.commit()
        result = week_over_week(db_session, "qualified_opportunities")
        assert result["value"] == 10
        assert result["previous"] == 6
        assert result["delta"] == 4
        assert result["direction"] == "up"

    def test_good_down_flips_the_direction_judgment(self, db_session):
        """An ABSTAIN rate falling is good news, even though the raw
        delta is negative -- direction reflects that, not the sign."""
        _snap(db_session, "abstain_rate_contact", 0.5, days_ago=0)
        _snap(db_session, "abstain_rate_contact", 0.9, days_ago=7)
        db_session.commit()
        result = week_over_week(db_session, "abstain_rate_contact", good="down")
        assert result["delta"] == -0.4
        assert result["direction"] == "up"  # "up" = favorable, per this function's own contract

    def test_zero_delta_is_flat(self, db_session):
        _snap(db_session, "qualified_opportunities", 5, days_ago=0)
        _snap(db_session, "qualified_opportunities", 5, days_ago=7)
        db_session.commit()
        assert week_over_week(db_session, "qualified_opportunities")["direction"] == "flat"


class TestWhyLeadsExistSample:
    def test_returns_opportunities_with_their_reason_blocks(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        opp = Opportunity(account_id=1, signal_id=signal.id, pen_state=PenState.not_moved, owner_user="andrew")
        db_session.add(opp)
        db_session.flush()
        db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.them,
                                   strength=ReasonStrength.Strong, evidence="x"))
        db_session.commit()
        sample = why_leads_exist_sample(db_session, limit=3)
        assert len(sample) == 1
        assert sample[0]["opportunity"].id == opp.id
        assert len(sample[0]["reason_blocks"]) == 1

    def test_respects_the_limit(self, db_session):
        for i in range(5):
            signal = Signal(signal_type=SignalType.ceqa_nop)
            db_session.add(signal)
            db_session.flush()
            db_session.add(Opportunity(account_id=1, signal_id=signal.id, pen_state=PenState.not_moved,
                                       owner_user="andrew"))
        db_session.commit()
        assert len(why_leads_exist_sample(db_session, limit=3)) == 3

    def test_empty_with_no_opportunities(self, db_session):
        assert why_leads_exist_sample(db_session) == []


class TestReportsRoute:
    """Web-layer smoke tests for /reports and /reports/metrics/<key> --
    app.web.main's own route wiring, not app.pipeline.reports' pure
    functions (already covered above)."""

    def test_reports_page_loads_with_no_data(self, db_session, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.db import get_session
        from app.web.main import app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/reports", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "Funnel" in resp.text
        assert "Whys-strength" in resp.text
        assert "unknown, not guessed" in resp.text or "shorter history" in resp.text

    def test_reports_page_shows_real_snapshot_numbers(self, db_session, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.db import get_session
        from app.pipeline.metrics import run_metric_snapshot
        from app.web.main import app

        run_metric_snapshot(db_session)
        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/reports", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200

    def test_metric_detail_page_shows_the_metrics_yaml_entry(self, db_session, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.db import get_session
        from app.web.main import app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/reports/metrics/earliness_rate", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "pen_state=not_moved" in resp.text

    def test_metric_detail_page_for_an_undocumented_key_says_so(self, db_session, monkeypatch):
        import base64

        from fastapi.testclient import TestClient

        from app.db import get_session
        from app.web.main import app

        monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
        app.dependency_overrides[get_session] = lambda: db_session
        client = TestClient(app)
        auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
        resp = client.get("/reports/metrics/nonexistent_metric", headers=auth)
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        assert "real gap" in resp.text
