"""Block 4A Item 2 (Master Plan v3.6 sections 15/31): the Outcome writer
(app.pipeline.outcomes)."""
import pytest
from sqlmodel import select

from app.models import (
    Disposition, LostReasonCode, Opportunity, OpportunityStage, Outcome, OutcomeSource, Signal, SignalType,
)
from app.pipeline.outcomes import dispositions, log_outcome, reason_codes


def _opportunity(db_session, **kw):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1, **kw)
    db_session.add(opp)
    db_session.commit()
    return opp


class TestLogOutcome:
    def test_creates_a_row_with_every_field(self, db_session):
        opp = _opportunity(db_session)
        outcome = log_outcome(db_session, opportunity_id=opp.id, user="andrew",
                              disposition=Disposition.connected, note="talked to the PE")
        db_session.commit()
        assert outcome.id is not None
        assert outcome.opportunity_id == opp.id
        assert outcome.user == "andrew"
        assert outcome.disposition == Disposition.connected
        assert outcome.reason_code is None
        assert outcome.source == OutcomeSource.web

    def test_accepts_plain_string_values_same_as_enum_members(self, db_session):
        opp = _opportunity(db_session)
        outcome = log_outcome(db_session, opportunity_id=opp.id, user="andrew",
                              disposition="connected", source="capture")
        assert outcome.disposition == Disposition.connected
        assert outcome.source == OutcomeSource.capture

    def test_lost_without_reason_code_is_rejected(self, db_session):
        opp = _opportunity(db_session)
        with pytest.raises(ValueError, match="reason_code"):
            log_outcome(db_session, opportunity_id=opp.id, user="andrew", disposition=Disposition.lost)

    def test_lost_with_reason_code_succeeds(self, db_session):
        opp = _opportunity(db_session)
        outcome = log_outcome(db_session, opportunity_id=opp.id, user="andrew",
                              disposition=Disposition.lost, reason_code=LostReasonCode.price)
        assert outcome.reason_code == LostReasonCode.price

    def test_lost_to_competitor_without_a_named_competitor_is_rejected(self, db_session):
        opp = _opportunity(db_session)
        with pytest.raises(ValueError, match="competitor"):
            log_outcome(db_session, opportunity_id=opp.id, user="andrew",
                       disposition=Disposition.lost, reason_code=LostReasonCode.lost_to_competitor)

    def test_lost_to_competitor_with_a_named_competitor_succeeds(self, db_session):
        opp = _opportunity(db_session)
        outcome = log_outcome(db_session, opportunity_id=opp.id, user="andrew",
                              disposition=Disposition.lost, reason_code=LostReasonCode.lost_to_competitor,
                              competitor="Sigler")
        assert outcome.competitor == "Sigler"

    def test_won_and_lost_sync_opportunity_stage(self, db_session):
        won_opp = _opportunity(db_session)
        log_outcome(db_session, opportunity_id=won_opp.id, user="andrew", disposition=Disposition.won)
        db_session.refresh(won_opp)
        assert won_opp.stage == OpportunityStage.won

        lost_opp = _opportunity(db_session)
        log_outcome(db_session, opportunity_id=lost_opp.id, user="andrew",
                   disposition=Disposition.lost, reason_code=LostReasonCode.timing_deferred)
        db_session.refresh(lost_opp)
        assert lost_opp.stage == OpportunityStage.lost

    def test_other_dispositions_do_not_change_stage(self, db_session):
        opp = _opportunity(db_session)
        assert opp.stage == OpportunityStage.identified
        log_outcome(db_session, opportunity_id=opp.id, user="andrew", disposition=Disposition.connected)
        db_session.refresh(opp)
        assert opp.stage == OpportunityStage.identified

    def test_every_disposition_updates_last_touch(self, db_session):
        opp = _opportunity(db_session)
        assert opp.last_touch is None
        log_outcome(db_session, opportunity_id=opp.id, user="andrew", disposition=Disposition.no_answer)
        db_session.refresh(opp)
        assert opp.last_touch is not None

    def test_unknown_opportunity_id_is_rejected(self, db_session):
        with pytest.raises(ValueError, match="no Opportunity"):
            log_outcome(db_session, opportunity_id=999999, user="andrew", disposition=Disposition.connected)


class TestConfigDrivenLists:
    def test_dispositions_reads_from_config_yaml(self):
        items = dispositions()
        assert {"value": "lost", "label": "Lost"} in items
        assert len(items) == 8

    def test_reason_codes_reads_from_config_yaml(self):
        items = reason_codes()
        assert {"value": "price", "label": "Price"} in items
        assert len(items) == 8
