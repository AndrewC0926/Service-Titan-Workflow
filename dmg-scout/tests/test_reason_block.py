"""Block 3 (Master Plan v3.2 section 12a): weakest-why decision rule
(app.pipeline.reason_block.weakest_why_rank). Block 4B-prep-3 Item 4 adds
compose_do_fields."""
import pytest

from app.models import Contact, PenState, ReasonStrength as S, TriggerType
from app.pipeline.reason_block import compose_do_fields, weakest_why_rank


def test_three_strong_ranks_above_two_strong_one_weak():
    three_strong = weakest_why_rank([S.Strong, S.Strong, S.Strong])
    two_strong_one_weak = weakest_why_rank([S.Strong, S.Strong, S.Weak])
    assert three_strong < two_strong_one_weak


def test_two_strong_one_weak_ranks_above_any_abstain():
    two_strong_one_weak = weakest_why_rank([S.Strong, S.Strong, S.Weak])
    one_abstain_two_strong = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    all_abstain = weakest_why_rank([S.ABSTAIN, S.ABSTAIN, S.ABSTAIN])
    assert two_strong_one_weak < one_abstain_two_strong
    assert two_strong_one_weak < all_abstain


def test_any_abstain_ranks_the_same_regardless_of_the_other_two_being_strong():
    """The rule is 'ranks by its weakest why, never a weighted sum': one
    ABSTAIN alone should push an opportunity into the ABSTAIN tier even when
    the other two whys are Strong -- it must not out-rank a weaker-but-no-
    ABSTAIN combination just because two of its three whys are strong."""
    one_abstain_two_strong = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    three_weak = weakest_why_rank([S.Weak, S.Weak, S.Weak])
    assert three_weak < one_abstain_two_strong


def test_order_of_the_three_whys_does_not_matter():
    assert weakest_why_rank([S.Strong, S.Weak, S.ABSTAIN]) == weakest_why_rank([S.ABSTAIN, S.Strong, S.Weak])


def test_fewer_abstains_ranks_above_more_abstains():
    one_abstain = weakest_why_rank([S.Strong, S.Strong, S.ABSTAIN])
    two_abstain = weakest_why_rank([S.Strong, S.ABSTAIN, S.ABSTAIN])
    three_abstain = weakest_why_rank([S.ABSTAIN, S.ABSTAIN, S.ABSTAIN])
    assert one_abstain < two_abstain < three_abstain


def test_rejects_anything_other_than_exactly_three_strengths():
    with pytest.raises(ValueError):
        weakest_why_rank([S.Strong, S.Strong])
    with pytest.raises(ValueError):
        weakest_why_rank([S.Strong, S.Strong, S.Strong, S.Weak])


def test_sorting_a_list_of_opportunities_by_this_key_orders_strongest_first():
    """The realistic use: Pipeline sorts opportunities by this key. Rady
    Children's-shaped (Strong/Strong/ABSTAIN) must not out-rank a plain
    3-Weak opportunity, even though it "looks" mostly strong."""
    rady = [S.Strong, S.Strong, S.ABSTAIN]
    uc_davis = [S.Weak, S.Strong, S.Weak]
    three_strong = [S.Strong, S.Strong, S.Strong]

    ordered = sorted([rady, uc_davis, three_strong], key=weakest_why_rank)
    assert ordered == [three_strong, uc_davis, rady]


class TestComposeDoFields:
    def test_composes_a_full_do_sentence_for_a_configured_combination(self, db_session):
        contact = Contact(name="Tim Yeany", phone="555-1234", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        do_person, do_ask, one_sentence = compose_do_fields(
            db_session, TriggerType.permit_activity, PenState.not_moved, contact.id)
        assert do_person == "Tim Yeany"
        assert do_ask == "whether the installed equipment is already due for replacement"
        assert one_sentence == (
            "Do: call Tim Yeany about the building's mechanical service life and ask "
            "whether the installed equipment is already due for replacement "
            "(no contractor has a hold on this yet)."
        )

    def test_different_trigger_type_and_pen_state_change_the_sentence(self, db_session):
        contact = Contact(name="Jane PE", email="jane@x.com", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        _, _, one_sentence = compose_do_fields(
            db_session, TriggerType.entitlement_milestone, PenState.moving, contact.id)
        assert "the project's entitlement/design timeline" in one_sentence
        assert "who is writing the basis of design" in one_sentence
        assert "a decision is already moving" in one_sentence

    def test_no_contact_id_abstains(self, db_session):
        assert compose_do_fields(db_session, TriggerType.permit_gap, PenState.not_moved, None) == (None, None, None)

    def test_contact_id_that_does_not_exist_abstains(self, db_session):
        assert compose_do_fields(db_session, TriggerType.permit_gap, PenState.not_moved, 999999) == (None, None, None)

    def test_unconfigured_trigger_type_abstains(self, db_session):
        """quiet_account has no config entry -- no FeedSignal source
        produces it yet, so nothing has ever exercised it."""
        contact = Contact(name="Someone", phone="555-0000", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()
        result = compose_do_fields(db_session, TriggerType.quiet_account, PenState.not_moved, contact.id)
        assert result == (None, None, None)

    def test_unconfigured_pen_state_abstains(self, db_session):
        """Not a real enum gap today (all four PenState values are
        configured) -- guards against a future PenState value being added
        without a matching config entry."""
        from unittest.mock import patch

        contact = Contact(name="Someone", phone="555-0000", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()
        with patch("app.pipeline.reason_block.load_config") as mock_cfg:
            mock_cfg.return_value.get.side_effect = lambda path, default=None: (
                {"topic": "x", "ask": "y"} if "by_trigger_type" in path else None
            )
            result = compose_do_fields(db_session, TriggerType.permit_gap, PenState.not_moved, contact.id)
        assert result == (None, None, None)
