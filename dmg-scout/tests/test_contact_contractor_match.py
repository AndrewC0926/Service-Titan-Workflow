"""Block 4B-prep-2 Item 1: match Contact.customer_ref_name to Contractor
(app.pipeline.contact_contractor_match)."""
from app.models import Contact, Contractor
from app.pipeline.contact_contractor_match import match_contacts_to_contractors


def _contractor(business_name, full_business_name=None, license_no="1"):
    return Contractor(license_no=license_no, business_name=business_name, full_business_name=full_business_name)


class TestMatchContactsToContractors:
    def test_exact_match_on_business_name(self, db_session):
        db_session.add(_contractor("Acme Mechanical Inc", license_no="1"))
        db_session.add(Contact(name="Jane", customer_ref_name="Acme Mechanical Inc"))
        db_session.commit()

        stats = match_contacts_to_contractors(db_session)
        assert stats == {"total_considered": 1, "matched": 1, "unmatched": 0}
        contact = select_first_contact(db_session)
        assert contact.contractor_id is not None

    def test_matches_via_full_business_name_too(self, db_session):
        db_session.add(_contractor("MACK STEVEN T CONSTRUCTION INC",
                                    full_business_name="Steven T Mack Construction Inc", license_no="2"))
        db_session.add(Contact(name="Jane", customer_ref_name="Steven T Mack Construction Inc"))
        db_session.commit()

        stats = match_contacts_to_contractors(db_session)
        assert stats["matched"] == 1

    def test_never_fuzzy_no_match_leaves_contractor_id_null(self, db_session):
        db_session.add(_contractor("Acme Mechanical Inc", license_no="3"))
        db_session.add(Contact(name="Jane", customer_ref_name="Acme Mechanicl Incorportaed"))  # typo'd, close but not exact
        db_session.commit()

        stats = match_contacts_to_contractors(db_session)
        assert stats == {"total_considered": 1, "matched": 0, "unmatched": 1}
        contact = select_first_contact(db_session)
        assert contact.contractor_id is None

    def test_ambiguous_collision_across_distinct_contractors_is_unmatched(self, db_session):
        db_session.add(_contractor("Same Name Mechanical", license_no="4"))
        db_session.add(_contractor("Same Name Mechanical", license_no="5"))
        db_session.add(Contact(name="Jane", customer_ref_name="Same Name Mechanical"))
        db_session.commit()

        stats = match_contacts_to_contractors(db_session)
        assert stats["matched"] == 0
        assert stats["unmatched"] == 1

    def test_no_customer_ref_name_is_never_considered(self, db_session):
        db_session.add(_contractor("Acme Mechanical Inc", license_no="6"))
        db_session.add(Contact(name="Jane", customer_ref_name=None))
        db_session.commit()

        stats = match_contacts_to_contractors(db_session)
        assert stats["total_considered"] == 0

    def test_idempotent_rerun_updates_in_place(self, db_session):
        db_session.add(_contractor("Acme Mechanical Inc", license_no="7"))
        contact = Contact(name="Jane", customer_ref_name="Acme Mechanical Inc")
        db_session.add(contact)
        db_session.commit()

        match_contacts_to_contractors(db_session)
        db_session.refresh(contact)
        first_id = contact.contractor_id
        assert first_id is not None

        stats = match_contacts_to_contractors(db_session)
        db_session.refresh(contact)
        assert contact.contractor_id == first_id
        assert stats["matched"] == 1

    def test_rerun_clears_a_stale_match_when_the_contractor_is_removed(self, db_session):
        contractor = _contractor("Acme Mechanical Inc", license_no="8")
        db_session.add(contractor)
        contact = Contact(name="Jane", customer_ref_name="Acme Mechanical Inc")
        db_session.add(contact)
        db_session.commit()
        match_contacts_to_contractors(db_session)
        db_session.refresh(contact)
        assert contact.contractor_id is not None

        db_session.delete(db_session.get(Contractor, contractor.id))
        db_session.commit()
        stats = match_contacts_to_contractors(db_session)
        db_session.refresh(contact)
        assert contact.contractor_id is None
        assert stats["unmatched"] == 1


def select_first_contact(db_session):
    from sqlmodel import select
    return db_session.exec(select(Contact)).first()
