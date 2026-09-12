"""Block 4A Item 5 (Master Plan v3.6): Today re-sourced from Pipeline
(app.pipeline.today_calls)."""
from datetime import datetime

from app.models import (
    Ab869Plan, HospitalBuilding, Opportunity, OpportunityStage, ReasonBlock, ReasonStrength, Signal,
    SignalType, WhyKind,
)
from app.pipeline.today_calls import three_calls_from_pipeline


def _opportunity_with_reason_block(db_session, strengths, **opp_kw):
    signal = Signal(signal_type=SignalType.ceqa_nop)
    db_session.add(signal)
    db_session.flush()
    opp = Opportunity(signal_id=signal.id, account_id=1, **opp_kw)
    db_session.add(opp)
    db_session.flush()
    for kind, strength in zip((WhyKind.them, WhyKind.now, WhyKind.win), strengths):
        db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=kind, strength=strength, evidence="x"))
    db_session.commit()
    return opp


class TestThreeCallsFromPipeline:
    def test_empty_pipeline_fills_entirely_from_deadlines(self, db_session):
        db_session.add(HospitalBuilding(
            perm_id="P1", building_nbr="B1", facility_name="Test Hospital", county="Los Angeles",
            npc_rating="2", npc_deadline_year=2030, snapshot_date=datetime(2026, 1, 1), source_url="https://x",
        ))
        db_session.add(Ab869Plan(perm_id="P1", plan_status="Not Approved",
                                 source_pdf_path="/x.pdf", source_pdf_hash="h1"))
        db_session.commit()

        cards = three_calls_from_pipeline(db_session)
        assert len(cards) == 1  # only one deadline row exists in this seed
        assert cards[0]["kind"] == "deadline"
        assert cards[0]["no_contact_yet"] is True

    def test_opportunities_rank_ahead_of_deadline_fill(self, db_session):
        strong_opp = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        db_session.add(HospitalBuilding(
            perm_id="P2", building_nbr="B1", facility_name="Fallback Hospital", county="Los Angeles",
            npc_rating="2", npc_deadline_year=2030, snapshot_date=datetime(2026, 1, 1), source_url="https://x",
        ))
        db_session.add(Ab869Plan(perm_id="P2", plan_status="Not Approved",
                                 source_pdf_path="/x.pdf", source_pdf_hash="h2"))
        db_session.commit()

        cards = three_calls_from_pipeline(db_session)
        assert cards[0]["kind"] == "opportunity"
        assert cards[0]["opportunity"].id == strong_opp.id
        assert cards[1]["kind"] == "deadline"

    def test_three_strong_opportunities_fill_all_slots_no_deadline_fallback(self, db_session):
        for _ in range(3):
            _opportunity_with_reason_block(
                db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        cards = three_calls_from_pipeline(db_session)
        assert len(cards) == 3
        assert all(c["kind"] == "opportunity" for c in cards)

    def test_won_and_lost_opportunities_are_excluded(self, db_session):
        _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong],
            stage=OpportunityStage.won)
        cards = three_calls_from_pipeline(db_session)
        assert not any(c["kind"] == "opportunity" for c in cards)

    def test_incomplete_reason_block_is_skipped_not_guessed(self, db_session):
        signal = Signal(signal_type=SignalType.ceqa_nop)
        db_session.add(signal)
        db_session.flush()
        opp = Opportunity(signal_id=signal.id, account_id=1)
        db_session.add(opp)
        db_session.flush()
        db_session.add(ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.them,
                                   strength=ReasonStrength.Strong, evidence="x"))
        db_session.commit()
        cards = three_calls_from_pipeline(db_session)
        assert not any(c["kind"] == "opportunity" and c["opportunity"].id == opp.id for c in cards)

    def test_contact_present_means_no_contact_yet_is_false(self, db_session):
        from app.models import Contact

        contact = Contact(name="Jane PE", phone="555-1234", reach_status="confirmed")
        db_session.add(contact)
        db_session.flush()
        opp = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong],
            contact_id=contact.id)
        cards = three_calls_from_pipeline(db_session)
        card = next(c for c in cards if c["kind"] == "opportunity" and c["opportunity"].id == opp.id)
        assert card["no_contact_yet"] is False
        assert card["contact"].name == "Jane PE"

    def test_active_learning_spread_forces_one_weak_slot(self, db_session):
        """With 3+ Strong opportunities and one Weak one, the Weak one
        must still occupy a slot -- displacing the natural 3rd place, not
        the top 2."""
        strongest = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        second = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        third_natural = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        weak_opp = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Weak, ReasonStrength.Strong])

        cards = three_calls_from_pipeline(db_session)
        ids = [c["opportunity"].id for c in cards]
        assert weak_opp.id in ids
        assert third_natural.id not in ids  # displaced, not one of the two strongest
        assert cards[-1]["active_learning_pick"] is True
        assert cards[-1]["opportunity"].id == weak_opp.id

    def test_no_active_learning_marker_when_no_weak_candidate_exists(self, db_session):
        _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Strong, ReasonStrength.Strong])
        cards = three_calls_from_pipeline(db_session)
        assert not any(c.get("active_learning_pick") for c in cards)

    def test_weak_candidate_already_in_top_three_is_not_duplicated(self, db_session):
        """If the natural top 3 already includes the best Weak-why
        opportunity, the spread logic must not add a 4th card or swap
        anything."""
        only_opp = _opportunity_with_reason_block(
            db_session, [ReasonStrength.Strong, ReasonStrength.Weak, ReasonStrength.Strong])
        cards = three_calls_from_pipeline(db_session)
        opp_cards = [c for c in cards if c["kind"] == "opportunity"]
        assert len(opp_cards) == 1
        assert opp_cards[0]["opportunity"].id == only_opp.id
        assert opp_cards[0]["active_learning_pick"] is True
