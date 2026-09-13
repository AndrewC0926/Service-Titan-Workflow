"""Block 3 (Master Plan v3.2 section 12a): weakest-why decision rule
(app.pipeline.reason_block.weakest_why_rank). Block 4B-prep-3 Item 4 adds
compose_do_fields; Block 4B Item 4 rewrites it for the handoff play and
the AB 869 deadline ask."""
import pytest

from app.models import (
    Ab869Plan, Contact, Contractor, PenState, ProductLine, ReasonStrength as S, RetrofitBuilding, TriggerType,
)
from app.pipeline.reason_block import compose_do_fields, weakest_why_rank
from app.pipeline.signals_feed import FeedSignal


def _fs(trigger_type, pen_state=PenState.not_moved, **kw):
    return FeedSignal(source="retrofit_building", source_id="1", trigger_type=trigger_type,
                      trigger_date=None, evidence="x", confidence=None, pen_state=pen_state, **kw)


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


class TestComposeDoFieldsGeneric:
    """The original config-driven pair -- still the fallback for a
    permit_gap/permit_activity contact NOT anchored on a Contractor
    firm, and for every trigger_type the handoff play/AB 869 rewrite
    doesn't touch."""

    def test_composes_a_full_do_sentence_for_a_configured_combination(self, db_session):
        contact = Contact(name="Tim Yeany", phone="555-1234", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        do_person, do_ask, one_sentence = compose_do_fields(
            db_session, _fs(TriggerType.permit_activity), contact.id)
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
            db_session, _fs(TriggerType.entitlement_milestone, PenState.moving), contact.id)
        assert "the project's entitlement/design timeline" in one_sentence
        assert "who is writing the basis of design" in one_sentence
        assert "a decision is already moving" in one_sentence

    def test_no_contact_id_abstains(self, db_session):
        assert compose_do_fields(db_session, _fs(TriggerType.permit_gap), None) == (None, None, None)

    def test_contact_id_that_does_not_exist_abstains(self, db_session):
        assert compose_do_fields(db_session, _fs(TriggerType.permit_gap), 999999) == (None, None, None)

    def test_unconfigured_trigger_type_abstains(self, db_session):
        """quiet_account has no config entry -- no FeedSignal source
        produces it yet, so nothing has ever exercised it."""
        contact = Contact(name="Someone", phone="555-0000", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()
        result = compose_do_fields(db_session, _fs(TriggerType.quiet_account), contact.id)
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
            result = compose_do_fields(db_session, _fs(TriggerType.permit_gap), contact.id)
        assert result == (None, None, None)

    def test_permit_gap_contact_not_contractor_anchored_uses_generic_template(self, db_session):
        """A project contact (no contractor_id) on a permit_gap/permit_
        activity signal still gets the original generic sentence -- the
        handoff play is scoped to contractor-anchored contacts only."""
        contact = Contact(name="A Project Contact", phone="555-9999", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()
        _, _, one_sentence = compose_do_fields(db_session, _fs(TriggerType.permit_gap), contact.id)
        assert "budgeted a replacement" in one_sentence


class TestComposeDoFieldsHandoffPlay:
    def test_full_handoff_play_sentence(self, db_session):
        contractor = Contractor(license_no="H1", business_name="Yeany Air Conditioning")
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Tim Yeany", phone="555-1234", reach_status="confirmed",
                          contractor_id=contractor.id)
        building = RetrofitBuilding(apn="hp-1", population="replacement_candidate",
                                    address="123 Main St", equipment_type=None)
        db_session.add(contact)
        db_session.add(building)
        db_session.commit()

        fs = _fs(TriggerType.permit_gap, building_id=building.id)
        do_person, do_ask, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert do_person == "Tim Yeany"
        assert do_ask == "who owns it and whether they'd take the list"
        assert one_sentence == (
            "Call Tim Yeany at Yeany Air Conditioning. Offer the list of buildings within 15 miles "
            "of their yard past HVAC service life with no replacement permit on record; 123 Main St "
            "is one of them (unknown, not guessed, unknown, not guessed). Ask who owns it and "
            "whether they'd take the list."
        )

    def test_permit_activity_includes_real_equipment_and_install_year(self, db_session):
        contractor = Contractor(license_no="H2", business_name="Acme Mechanical")
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Joe Acme", phone="555-2222", reach_status="confirmed",
                          contractor_id=contractor.id)
        building = RetrofitBuilding(apn="hp-2", population="recently_active", address="456 Oak Ave",
                                    equipment_type="split_dx", latest_install_year=2010)
        db_session.add(contact)
        db_session.add(building)
        db_session.commit()

        fs = _fs(TriggerType.permit_activity, building_id=building.id)
        _, _, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert "456 Oak Ave is one of them (split_dx, 2010)" in one_sentence

    def test_no_building_id_falls_back_to_generic_rather_than_a_broken_sentence(self, db_session):
        """The handoff play needs a real building to name (the "{address}
        is one of them" clause) -- without one, this falls back to the
        original generic template (still real, useful information: we do
        know the contact and trigger_type, just not enough for the more
        specific sentence) rather than ABSTAINing entirely and throwing
        that away."""
        contractor = Contractor(license_no="H3", business_name="Some Mechanical")
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Someone", phone="555-3333", reach_status="confirmed",
                          contractor_id=contractor.id)
        db_session.add(contact)
        db_session.commit()

        fs = _fs(TriggerType.permit_gap, building_id=None)
        _, _, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert "budgeted a replacement" in one_sentence

    def test_only_applies_to_permit_gap_and_permit_activity(self, db_session):
        """A contractor-anchored contact on some OTHER trigger_type still
        gets the generic template -- the handoff play is scoped to the
        two trigger types it was actually asked for."""
        contractor = Contractor(license_no="H4", business_name="Some Mechanical")
        db_session.add(contractor)
        db_session.flush()
        contact = Contact(name="Someone", phone="555-4444", reach_status="confirmed",
                          contractor_id=contractor.id)
        db_session.add(contact)
        db_session.commit()

        fs = _fs(TriggerType.entitlement_milestone)
        _, _, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert "entitlement/design timeline" in one_sentence


class TestComposeDoFieldsDeadlineAb869:
    def test_names_plan_status_and_osp_eligible_package(self, db_session):
        db_session.add(Ab869Plan(perm_id="P1", plan_status="Not Approved",
                                 source_pdf_path="/x.pdf", source_pdf_hash="h1"))
        db_session.add(ProductLine(name="Marley", name_norm="marley", category="cooling_towers",
                                   building_role="heat_rejection", oshpd_osp=True))
        contact = Contact(name="Someone", phone="555-5555", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        fs = _fs(TriggerType.deadline, facility_perm_id="P1")
        do_person, do_ask, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert do_person == "Someone"
        assert "Not Approved" in one_sentence
        assert "Heat rejection" in one_sentence

    def test_no_resolvable_plan_falls_back_to_generic(self, db_session):
        contact = Contact(name="Someone", phone="555-6666", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        fs = _fs(TriggerType.deadline, facility_perm_id="NONEXISTENT")
        _, _, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert "outstanding regulatory deadline" in one_sentence

    def test_no_facility_perm_id_falls_back_to_generic(self, db_session):
        contact = Contact(name="Someone", phone="555-7777", reach_status="confirmed")
        db_session.add(contact)
        db_session.commit()

        fs = _fs(TriggerType.deadline, facility_perm_id=None)
        _, _, one_sentence = compose_do_fields(db_session, fs, contact.id)
        assert "outstanding regulatory deadline" in one_sentence
