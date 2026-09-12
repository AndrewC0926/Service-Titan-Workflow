"""Block 4A Item 4 (Master Plan v3.6 sections 30/32): metric snapshots
(app.pipeline.metrics)."""
from datetime import datetime, timedelta

import yaml

from app.models import (
    Category, DecisionNote, DeliveryMethodClass, Disposition, LeadSource, MetricSnapshot, NoteType,
    Opportunity, OpportunityStage, Origin, Outcome, PenState, Project, ReasonBlock, ReasonStrength,
    Signal, SignalType, SourceRowSeen, TriggerType, WhyKind, utcnow,
)
from app.pipeline.metrics import run_metric_snapshot, write_snapshot

METRICS_YAML_PATH = "metrics.yaml"


def _opportunity(db_session, **kw):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1, **kw)
    db_session.add(opp)
    db_session.commit()
    return opp


class TestWriteSnapshot:
    def test_creates_a_row(self, db_session):
        row = write_snapshot(db_session, metric_key="test_metric", value=42.0, dimensions={"a": "b"})
        db_session.commit()
        assert row.id is not None
        assert row.value == 42.0
        assert row.dimensions == {"a": "b"}

    def test_idempotent_within_the_same_day(self, db_session):
        write_snapshot(db_session, metric_key="test_metric", value=1.0, dimensions={"a": "b"})
        write_snapshot(db_session, metric_key="test_metric", value=2.0, dimensions={"a": "b"})
        db_session.commit()
        from sqlmodel import select
        rows = db_session.exec(
            select(MetricSnapshot).where(MetricSnapshot.metric_key == "test_metric")).all()
        assert len(rows) == 1
        assert rows[0].value == 2.0

    def test_different_dimensions_are_separate_rows(self, db_session):
        write_snapshot(db_session, metric_key="test_metric", value=1.0, dimensions={"a": "b"})
        write_snapshot(db_session, metric_key="test_metric", value=2.0, dimensions={"a": "c"})
        db_session.commit()
        from sqlmodel import select
        rows = db_session.exec(
            select(MetricSnapshot).where(MetricSnapshot.metric_key == "test_metric")).all()
        assert len(rows) == 2

    def test_a_past_day_is_never_touched(self, db_session):
        yesterday = utcnow() - timedelta(days=1)
        write_snapshot(db_session, metric_key="test_metric", value=1.0, snapshot_date=yesterday)
        write_snapshot(db_session, metric_key="test_metric", value=2.0)  # today
        db_session.commit()
        from sqlmodel import select
        rows = db_session.exec(
            select(MetricSnapshot).where(MetricSnapshot.metric_key == "test_metric")).all()
        assert len(rows) == 2
        assert {r.value for r in rows} == {1.0, 2.0}


class TestMetricsYamlConsistency:
    def test_every_written_metric_key_is_documented(self, db_session, cfg):
        """The guard: run the real computation against a real (seeded)
        local-shaped restore and confirm every metric_key it writes has a
        metrics.yaml entry."""
        _opportunity(db_session)
        with open(METRICS_YAML_PATH) as f:
            documented = yaml.safe_load(f)
        written = run_metric_snapshot(db_session)
        db_session.commit()
        for metric_key in written:
            assert metric_key in documented, f"{metric_key!r} was written but has no metrics.yaml entry"

    def test_every_documented_metric_has_the_required_fields(self):
        with open(METRICS_YAML_PATH) as f:
            documented = yaml.safe_load(f)
        for metric_key, entry in documented.items():
            for field in ("formula", "sources", "owner", "cadence", "last_validated"):
                assert field in entry, f"{metric_key!r} is missing {field!r}"


class TestIndividualMetrics:
    def test_signals_by_trigger_type(self, db_session, cfg):
        p = Project(name="Test DC", category=Category.data_center)
        db_session.add(p)
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert any(e["dimensions"] == {"trigger_type": "entitlement_milestone"}
                  for e in written["signals_by_trigger_type"])

    def test_qualified_opportunities_counts_all_opportunities(self, db_session):
        _opportunity(db_session)
        _opportunity(db_session)
        written = run_metric_snapshot(db_session)
        assert written["qualified_opportunities"] == [{"dimensions": {}, "value": 2}]

    def test_opportunities_by_stage(self, db_session):
        opp = _opportunity(db_session)
        opp.stage = OpportunityStage.won
        db_session.add(opp)
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert {"dimensions": {"stage": "won"}, "value": 1} in written["opportunities_by_stage"]

    def test_opportunities_by_engine_uses_origin(self, db_session):
        opp = _opportunity(db_session, origin=Origin.relationship_intro)
        written = run_metric_snapshot(db_session)
        assert {"dimensions": {"engine": "relationship_intro"}, "value": 1} in written["opportunities_by_engine"]

    def test_opportunities_by_owner_is_one_abstain_bucket(self, db_session):
        _opportunity(db_session)
        _opportunity(db_session)
        written = run_metric_snapshot(db_session)
        assert written["opportunities_by_owner"] == [{"dimensions": {"owner": "ABSTAIN"}, "value": 2}]

    def test_whys_strength_distribution(self, db_session):
        opp = _opportunity(db_session)
        for kind, strength in ((WhyKind.them, ReasonStrength.Strong), (WhyKind.now, ReasonStrength.Strong),
                               (WhyKind.win, ReasonStrength.Strong)):
            db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=kind, strength=strength, evidence="x"))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert {"dimensions": {"strong_count": "3"}, "value": 1} in written["whys_strength_distribution"]

    def test_earliness_rate(self, db_session):
        _opportunity(db_session, pen_state=PenState.not_moved)
        _opportunity(db_session, pen_state=PenState.moved)
        written = run_metric_snapshot(db_session)
        assert written["earliness_rate"] == [{"dimensions": {}, "value": 0.5}]

    def test_earliness_rate_with_no_opportunities_is_zero_not_a_crash(self, db_session):
        written = run_metric_snapshot(db_session)
        assert written["earliness_rate"] == [{"dimensions": {}, "value": 0.0}]

    def test_outcomes_logged_per_user(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.no_answer))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert {"dimensions": {"user": "andrew"}, "value": 2} in written["outcomes_logged_per_user"]

    def test_notes_per_user(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.inbound, author="andrew"))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert {"dimensions": {"user": "andrew"}, "value": 1} in written["notes_per_user"]

    def test_abstain_rate_delivery_method_class(self, db_session):
        db_session.add(Project(name="Abstained", category=Category.data_center,
                               delivery_method_class=DeliveryMethodClass.ABSTAIN))
        db_session.add(Project(name="Known", category=Category.data_center,
                               delivery_method_class=DeliveryMethodClass.design_bid_build))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert written["abstain_rate_delivery_method_class"] == [{"dimensions": {}, "value": 0.5}]

    def test_abstain_rate_pen_state_measured_against_signals(self, db_session):
        db_session.add(Signal(signal_type=SignalType.ceqa_nop, pen_state=PenState.ABSTAIN))
        db_session.add(Signal(signal_type=SignalType.ceqa_nop, pen_state=PenState.not_moved))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        assert written["abstain_rate_pen_state"] == [{"dimensions": {}, "value": 0.5}]

    def test_source_freshness_days(self, db_session):
        db_session.add(SourceRowSeen(source="hcai_projects", natural_key="k1",
                                     last_seen_at=utcnow() - timedelta(days=3)))
        db_session.commit()
        written = run_metric_snapshot(db_session)
        entry = next(e for e in written["source_freshness_days"] if e["dimensions"] == {"source": "hcai_projects"})
        assert 2.9 < entry["value"] < 3.1
