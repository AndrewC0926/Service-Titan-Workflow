"""Block 4A Item 3 (Master Plan v3.6 section 31): Decision Notes
(app.pipeline.notes)."""
from datetime import timedelta

import pytest
from sqlmodel import select

from app.models import (
    Account, BasisOfDesign, DecisionNote, LeadSource, LostReasonCode, NoteType, NotePenHolder,
    NetsuiteRefType, Opportunity, OpportunityStage, Project, Category, Signal, SignalType, utcnow,
)
from app.pipeline.notes import (
    basis_of_design_options, lead_sources, log_note, note_types, pen_holders, three_deals_to_explain,
)


def _opportunity(db_session, created_at=None):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1)
    if created_at is not None:
        opp.created_at = created_at
    db_session.add(opp)
    db_session.commit()
    return opp


class TestLogNote:
    def test_creates_a_note_anchored_on_an_opportunity(self, db_session):
        opp = _opportunity(db_session)
        note = log_note(db_session, note_type=NoteType.decision, lead_source=LeadSource.rep_originated,
                        author="andrew", opportunity_id=opp.id, free_text="called the PE")
        db_session.commit()
        assert note.id is not None
        assert note.opportunity_id == opp.id
        assert note.free_text == "called the PE"
        assert note.pen_holder == NotePenHolder.unknown  # default
        assert note.basis_of_design == BasisOfDesign.open  # default

    def test_accepts_plain_strings_same_as_enum_members(self, db_session):
        opp = _opportunity(db_session)
        note = log_note(db_session, note_type="won", lead_source="relationship", author="andrew",
                        opportunity_id=opp.id, pen_holder="engineer", basis_of_design="ours")
        assert note.note_type == NoteType.won
        assert note.pen_holder == NotePenHolder.engineer
        assert note.basis_of_design == BasisOfDesign.ours

    def test_requires_at_least_one_anchor(self, db_session):
        with pytest.raises(ValueError, match="at least one anchor"):
            log_note(db_session, note_type=NoteType.intel, lead_source=LeadSource.inbound, author="andrew")

    def test_project_anchor_is_sufficient(self, db_session):
        p = Project(name="Test DC", category=Category.data_center)
        db_session.add(p)
        db_session.commit()
        note = log_note(db_session, note_type=NoteType.intel, lead_source=LeadSource.inbound,
                        author="andrew", project_id=p.id)
        assert note.project_id == p.id

    def test_account_anchor_is_sufficient(self, db_session):
        account = Account(name="Test Co", name_norm="test co")
        db_session.add(account)
        db_session.commit()
        note = log_note(db_session, note_type=NoteType.intel, lead_source=LeadSource.inbound,
                        author="andrew", account_id=account.id)
        assert note.account_id == account.id

    def test_netsuite_ref_alone_is_sufficient_a_deal_that_never_touched_scout(self, db_session):
        note = log_note(db_session, note_type=NoteType.won, lead_source=LeadSource.relationship,
                        author="andrew", netsuite_ref_type=NetsuiteRefType.sales_order,
                        netsuite_ref="SO-12345", free_text="closed direct, never in Scout")
        assert note.netsuite_ref == "SO-12345"
        assert note.opportunity_id is None

    def test_netsuite_ref_without_a_type_is_rejected(self, db_session):
        with pytest.raises(ValueError, match="netsuite_ref_type"):
            log_note(db_session, note_type=NoteType.won, lead_source=LeadSource.relationship,
                    author="andrew", netsuite_ref="SO-12345")

    def test_lost_to_competitor_reason_code_and_competitor_line_together(self, db_session):
        opp = _opportunity(db_session)
        note = log_note(db_session, note_type=NoteType.lost, lead_source=LeadSource.scout_signal,
                        author="andrew", opportunity_id=opp.id, reason_code=LostReasonCode.lost_to_competitor,
                        competitor_line="Sigler / Carrier")
        assert note.reason_code == LostReasonCode.lost_to_competitor
        assert note.competitor_line == "Sigler / Carrier"

    def test_multiple_anchors_can_be_set_together(self, db_session):
        """Section 31 lists the anchors as alternatives, but nothing stops
        a note that's genuinely about both an Opportunity and its Account
        -- not enforced as mutually exclusive."""
        opp = _opportunity(db_session)
        note = log_note(db_session, note_type=NoteType.decision, lead_source=LeadSource.rep_originated,
                        author="andrew", opportunity_id=opp.id, account_id=1)
        assert note.opportunity_id == opp.id and note.account_id == 1


class TestThreeDealsToExplain:
    def test_returns_open_opportunities_older_than_14_days(self, db_session):
        old_opp = _opportunity(db_session, created_at=utcnow() - timedelta(days=20))
        new_opp = _opportunity(db_session, created_at=utcnow() - timedelta(days=1))
        result = three_deals_to_explain(db_session)
        ids = [o.id for o in result]
        assert old_opp.id in ids
        assert new_opp.id not in ids

    def test_excludes_won_and_lost_opportunities(self, db_session):
        won_opp = _opportunity(db_session, created_at=utcnow() - timedelta(days=30))
        won_opp.stage = OpportunityStage.won
        db_session.add(won_opp)
        db_session.commit()
        result = three_deals_to_explain(db_session)
        assert won_opp.id not in [o.id for o in result]

    def test_caps_at_three_oldest_first(self, db_session):
        opps = [_opportunity(db_session, created_at=utcnow() - timedelta(days=15 + i)) for i in range(5)]
        result = three_deals_to_explain(db_session)
        assert len(result) == 3
        # Oldest (largest offset) should be first.
        assert result[0].id == opps[-1].id


class TestConfigDrivenLists:
    def test_note_types_reads_from_config(self):
        items = note_types()
        assert {"value": "decision", "label": "Decision"} in items
        assert len(items) == 5

    def test_pen_holders_reads_from_config(self):
        items = pen_holders()
        assert {"value": "gc", "label": "GC"} in items
        assert len(items) == 5

    def test_basis_of_design_reads_from_config(self):
        items = basis_of_design_options()
        assert {"value": "ours", "label": "Ours"} in items
        assert len(items) == 4

    def test_lead_sources_reads_from_config(self):
        items = lead_sources()
        assert {"value": "scout_signal", "label": "Scout signal"} in items
        assert len(items) == 5
