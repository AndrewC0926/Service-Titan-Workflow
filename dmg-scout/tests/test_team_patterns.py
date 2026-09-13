"""Block 4B Item 5: manager-scoped Outcomes/Notes patterns
(app.pipeline.team_patterns)."""
from app.models import (
    DecisionNote, Disposition, LeadSource, LostReasonCode, NotePenHolder, NoteType, Opportunity,
    Outcome, PenState, Signal, SignalType,
)
from app.pipeline.team_patterns import competitor_by_line, pen_holder_by_signal_type, reason_code_counts


def _opportunity(db_session):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(account_id=1, signal_id=signal.id, pen_state=PenState.not_moved, owner_user="andrew")
    db_session.add(opp)
    db_session.commit()
    return opp


class TestReasonCodeCounts:
    def test_empty_with_no_rows(self, db_session):
        assert reason_code_counts(db_session) == []

    def test_combines_outcome_and_note_reason_codes(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.lost,
                               reason_code=LostReasonCode.price))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.lost,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    reason_code=LostReasonCode.price))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.lost,
                                    lead_source=LeadSource.scout_signal, author="jason",
                                    reason_code=LostReasonCode.timing_deferred))
        db_session.commit()
        counts = reason_code_counts(db_session)
        by_code = {c["reason_code"]: c["count"] for c in counts}
        assert by_code["price"] == 2
        assert by_code["timing_deferred"] == 1

    def test_null_reason_code_rows_are_excluded_not_bucketed(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(Outcome(opportunity_id=opp.id, user="andrew", disposition=Disposition.connected))
        db_session.commit()
        assert reason_code_counts(db_session) == []


class TestCompetitorByLine:
    def test_empty_with_no_competitor_named(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="andrew"))
        db_session.commit()
        assert competitor_by_line(db_session) == []

    def test_groups_by_line_and_competitor(self, db_session):
        opp = _opportunity(db_session)
        for _ in range(2):
            db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.lost,
                                        lead_source=LeadSource.scout_signal, author="andrew",
                                        line="AAON", competitor_line="Daikin"))
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.lost,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    line="LG", competitor_line="Mitsubishi"))
        db_session.commit()
        rows = competitor_by_line(db_session)
        assert {"line": "AAON", "competitor_line": "Daikin", "count": 2} in rows
        assert {"line": "LG", "competitor_line": "Mitsubishi", "count": 1} in rows

    def test_no_line_named_falls_back_to_unknown(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.lost,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    line=None, competitor_line="Daikin"))
        db_session.commit()
        rows = competitor_by_line(db_session)
        assert rows[0]["line"] == "unknown, not guessed"


class TestPenHolderBySignalType:
    def test_empty_with_no_notes(self, db_session):
        assert pen_holder_by_signal_type(db_session) == []

    def test_note_with_no_signal_id_groups_under_no_signal_linked(self, db_session):
        opp = _opportunity(db_session)
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    pen_holder=NotePenHolder.engineer))
        db_session.commit()
        rows = pen_holder_by_signal_type(db_session)
        assert rows == [{"pen_holder": "engineer", "signal_type": "no signal linked", "count": 1}]

    def test_note_with_a_real_signal_id_uses_its_signal_type(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        opp = _opportunity(db_session)
        db_session.add(DecisionNote(opportunity_id=opp.id, note_type=NoteType.intel,
                                    lead_source=LeadSource.scout_signal, author="andrew",
                                    pen_holder=NotePenHolder.contractor, signal_id=signal.id))
        db_session.commit()
        rows = pen_holder_by_signal_type(db_session)
        assert rows == [{"pen_holder": "contractor", "signal_type": "ceqa_nop", "count": 1}]
