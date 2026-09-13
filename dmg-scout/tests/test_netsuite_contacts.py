"""Block 4B-prep Item 1: the NetSuite contacts importer
(app.importers.netsuite_contacts)."""
import pytest
from sqlmodel import select

from app.importers.netsuite_contacts import (
    _parse_company_field,
    _parse_name_field,
    _split_first_last,
    backfill_company_id_prefix,
    import_netsuite_contacts,
)
from app.models import Account, Contact

HEADER = "Internal ID,Internal ID,Name,Company,Job Title,Email,Phone,Mobile Phone,Inactive,Date Created"


def _csv(*rows: str) -> str:
    return "\n".join([HEADER, *rows])


class TestParseNameField:
    def test_colon_format(self):
        assert _parse_name_field("1364 Vision Mechanical: Brian Bellanca") == (
            "1364", "Vision Mechanical", "Brian Bellanca")

    def test_dash_format(self):
        assert _parse_name_field("13562 I.C.O. Air - Henrik Torossain") == (
            "13562", "I.C.O. Air", "Henrik Torossain")

    def test_plain_person_name_has_no_customer_ref(self):
        assert _parse_name_field("Aaron Aguilar") == (None, None, "Aaron Aguilar")

    def test_leading_digit_company_name_is_not_misparsed_as_an_id(self):
        """"3 SHELDON MECHANICAL" has no separator after the digit -- must
        not be treated as customer_ref_id=3, customer_ref_name=''."""
        assert _parse_name_field("3 SHELDON MECHANICAL") == (None, None, "3 SHELDON MECHANICAL")

    def test_dash_without_leading_id_is_not_misparsed(self):
        """"Air-Ex - Matthew Wilson" has a real ' - ' separator but no
        leading digit at all -- the leading-id regex must not match."""
        assert _parse_name_field("Air-Ex - Matthew Wilson") == (None, None, "Air-Ex - Matthew Wilson")

    def test_hyphen_inside_a_word_is_not_a_separator(self):
        """"14101 TRI T Technology-Kevin Chang" has a leading id but the
        dash has no surrounding spaces -- not the ' - ' shape."""
        cust_id, cust_name, person = _parse_name_field("14101 TRI T Technology-Kevin Chang")
        assert cust_id is None and cust_name is None


class TestParseCompanyField:
    def test_strips_leading_id(self):
        assert _parse_company_field("58 XCEL MECHANICAL SYSTEMS INC") == ("58", "XCEL MECHANICAL SYSTEMS INC")

    def test_verified_real_entity_id(self):
        """Matches the same entity id Contact.customer_ref_id's own
        docstring already verified by hand against the real NetSuite
        customer master for this exact company."""
        assert _parse_company_field("14006 RDK Mechanical") == ("14006", "RDK Mechanical")

    def test_no_leading_digits_passes_through_unchanged(self):
        assert _parse_company_field("ACCO Engineered Systems") == (None, "ACCO Engineered Systems")

    def test_no_space_after_digits_is_not_an_id(self):
        """A real CSLB-roster-shaped name like "5858 CONSTRUCTION COMPANY"
        still parses as an id here (Company column, not the Contractor
        roster) -- but a value with no separating space at all, e.g. a
        pure numeric company code, is left alone since there is no
        remaining text to be the customer name."""
        assert _parse_company_field("12345") == (None, "12345")


class TestSplitFirstLast:
    def test_two_tokens_splits(self):
        assert _split_first_last("Brian Bellanca") == ("Brian", "Bellanca")

    def test_one_token_does_not_split(self):
        assert _split_first_last("Aaron") == (None, None)

    def test_three_tokens_does_not_split(self):
        assert _split_first_last("Mary Ann Smith") == (None, None)


class TestImportNetsuiteContacts:
    def test_rejects_an_unexpected_header(self, db_session):
        with pytest.raises(ValueError, match="unexpected header shape"):
            import_netsuite_contacts(db_session, "Wrong,Header\n1,2")

    def test_basic_row_creates_a_contact(self, db_session):
        stats = import_netsuite_contacts(db_session, _csv(
            "100,100,Aaron Aguilar,ACCO Engineered Systems,PE,aaron@acco.com,555-1111,,No,1/1/2020"))
        assert stats["rows"] == 1
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 100)).one()
        assert contact.name == "Aaron Aguilar"
        assert contact.first_name == "Aaron" and contact.last_name == "Aguilar"
        assert contact.title == "PE"
        assert contact.email == "aaron@acco.com"
        assert contact.reachable is True
        assert contact.is_active is True
        assert contact.source == "netsuite"
        assert contact.reach_status == "confirmed"
        assert contact.customer_ref_name == "ACCO Engineered Systems"

    def test_no_contact_info_is_not_reachable(self, db_session):
        stats = import_netsuite_contacts(db_session, _csv(
            "101,101,No Contact Info,,,,,,No,1/1/2020"))
        assert stats["reachable"] == 0
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 101)).one()
        assert contact.reachable is False

    def test_mobile_alone_counts_as_reachable(self, db_session):
        stats = import_netsuite_contacts(db_session, _csv(
            "102,102,Mobile Only,,,,,555-2222,No,1/1/2020"))
        assert stats["reachable"] == 1

    def test_inactive_yes_sets_is_active_false(self, db_session):
        import_netsuite_contacts(db_session, _csv(
            "103,103,Inactive Person,,,,,,Yes,1/1/2020"))
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 103)).one()
        assert contact.is_active is False

    def test_matches_by_customer_ref_id_via_entity_id(self, db_session):
        account = Account(name="Vision Mechanical Services", name_norm="vision mechanical services",
                          netsuite_entity_id="1364")
        db_session.add(account)
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "200,200,1364 Vision Mechanical: Brian Bellanca,,,brian@visionmep.com,,,No,1/1/2020"))
        assert stats["matched_by_id"] == 1
        assert stats["matched_by_company_name"] == 0
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 200)).one()
        assert contact.account_id == account.id
        assert contact.customer_ref_id == "1364"

    def test_matches_by_exact_company_name_when_no_id_available(self, db_session):
        account = Account(name="ACCO Engineered Systems", name_norm="acco engineered systems")
        db_session.add(account)
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "201,201,Aaron Aguilar,ACCO Engineered Systems,,aaron@acco.com,,,No,1/1/2020"))
        assert stats["matched_by_company_name"] == 1
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 201)).one()
        assert contact.account_id == account.id

    def test_company_name_match_is_exact_never_fuzzy(self, db_session):
        """"ACCO Engineered Sys" (a near-miss) must NOT match "ACCO
        Engineered Systems" -- exact normalize_company_name match only."""
        account = Account(name="ACCO Engineered Systems", name_norm="acco engineered systems")
        db_session.add(account)
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "202,202,Someone Else,ACCO Engineered Sys,,x@x.com,,,No,1/1/2020"))
        assert stats["matched_by_company_name"] == 0
        assert stats["unmatched"] == 1
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 202)).one()
        assert contact.account_id is None
        assert contact.customer_ref_name == "ACCO Engineered Sys"  # kept raw for a later join

    def test_ambiguous_name_norm_across_two_accounts_is_left_unmatched(self, db_session):
        db_session.add(Account(name="Acme Air", name_norm="acme air"))
        db_session.add(Account(name="ACME AIR", name_norm="acme air"))
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "203,203,Someone,Acme Air,,x@x.com,,,No,1/1/2020"))
        assert stats["unmatched"] == 1
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 203)).one()
        assert contact.account_id is None

    def test_no_company_and_no_customer_ref_is_unmatched(self, db_session):
        stats = import_netsuite_contacts(db_session, _csv(
            "204,204,Plain Person,,,,,,No,1/1/2020"))
        assert stats["unmatched"] == 1

    def test_idempotent_on_internal_id_no_duplicate_on_rerun(self, db_session):
        csv_text = _csv("300,300,First Time,,,x@x.com,,,No,1/1/2020")
        import_netsuite_contacts(db_session, csv_text)
        import_netsuite_contacts(db_session, csv_text)
        rows = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 300)).all()
        assert len(rows) == 1

    def test_rerun_updates_fields_in_place(self, db_session):
        import_netsuite_contacts(db_session, _csv("301,301,Old Email,,,old@x.com,,,No,1/1/2020"))
        import_netsuite_contacts(db_session, _csv("301,301,Old Email,,,new@x.com,,,No,1/1/2020"))
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 301)).one()
        assert contact.email == "new@x.com"

    def test_company_column_leading_id_is_stripped_and_wins_over_name_field_id(self, db_session):
        account = Account(name="Xcel Mechanical Systems Inc", name_norm="xcel mechanical systems",
                          netsuite_entity_id="58")
        db_session.add(account)
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "205,205,Jane Smith,58 XCEL MECHANICAL SYSTEMS INC,,jane@xcel.com,,,No,1/1/2020"))
        assert stats["matched_by_id"] == 1
        contact = db_session.exec(select(Contact).where(Contact.netsuite_internal_id == 205)).one()
        assert contact.customer_ref_id == "58"
        assert contact.customer_ref_name == "XCEL MECHANICAL SYSTEMS INC"
        assert contact.company == "XCEL MECHANICAL SYSTEMS INC"
        assert contact.account_id == account.id

    def test_report_totals_add_up(self, db_session):
        account = Account(name="Vision Mechanical Services", name_norm="vision mechanical services",
                          netsuite_entity_id="1364")
        db_session.add(account)
        db_session.commit()
        stats = import_netsuite_contacts(db_session, _csv(
            "400,400,1364 Vision Mechanical: Brian Bellanca,,,brian@x.com,,,No,1/1/2020",
            "401,401,Aaron Aguilar,,,,,,No,1/1/2020",
        ))
        assert stats["rows"] == 2
        assert stats["matched_by_id"] + stats["matched_by_company_name"] + stats["unmatched"] == 2


class TestBackfillCompanyIdPrefix:
    def test_strips_prefix_from_an_already_imported_row(self, db_session):
        contact = Contact(netsuite_internal_id=500, name="Jane Smith", source="netsuite",
                          customer_ref_name="58 XCEL MECHANICAL SYSTEMS INC",
                          company="58 XCEL MECHANICAL SYSTEMS INC")
        db_session.add(contact)
        db_session.commit()

        stats = backfill_company_id_prefix(db_session)
        assert stats == {"considered": 1, "fixed": 1, "newly_matched_by_id": 0,
                         "newly_matched_by_company_name": 0}
        db_session.refresh(contact)
        assert contact.customer_ref_name == "XCEL MECHANICAL SYSTEMS INC"
        assert contact.company == "XCEL MECHANICAL SYSTEMS INC"
        assert contact.customer_ref_id == "58"

    def test_newly_resolves_an_account_that_id_based_matching_could_not_before(self, db_session):
        account = Account(name="RDK Mechanical", name_norm="rdk mechanical", netsuite_entity_id="14006")
        db_session.add(account)
        contact = Contact(netsuite_internal_id=501, name="Someone", source="netsuite",
                          customer_ref_name="14006 RDK Mechanical", company="14006 RDK Mechanical")
        db_session.add(contact)
        db_session.commit()

        stats = backfill_company_id_prefix(db_session)
        assert stats["newly_matched_by_id"] == 1
        db_session.refresh(contact)
        assert contact.account_id == account.id

    def test_already_clean_row_is_untouched(self, db_session):
        contact = Contact(netsuite_internal_id=502, name="Someone", source="netsuite",
                          customer_ref_name="ACCO Engineered Systems", company="ACCO Engineered Systems")
        db_session.add(contact)
        db_session.commit()

        stats = backfill_company_id_prefix(db_session)
        assert stats == {"considered": 1, "fixed": 0, "newly_matched_by_id": 0,
                         "newly_matched_by_company_name": 0}

    def test_non_netsuite_contact_is_never_considered(self, db_session):
        contact = Contact(name="Someone", source="manual", customer_ref_name="58 XCEL MECHANICAL SYSTEMS INC")
        db_session.add(contact)
        db_session.commit()

        stats = backfill_company_id_prefix(db_session)
        assert stats["considered"] == 0

    def test_idempotent_rerun_finds_nothing_further(self, db_session):
        contact = Contact(netsuite_internal_id=503, name="Jane Smith", source="netsuite",
                          customer_ref_name="58 XCEL MECHANICAL SYSTEMS INC",
                          company="58 XCEL MECHANICAL SYSTEMS INC")
        db_session.add(contact)
        db_session.commit()

        backfill_company_id_prefix(db_session)
        stats = backfill_company_id_prefix(db_session)
        assert stats["fixed"] == 0

    def test_row_with_an_existing_account_link_is_not_re_matched(self, db_session):
        existing_account = Account(name="Some Other Account", name_norm="some other account")
        rdk = Account(name="RDK Mechanical", name_norm="rdk mechanical", netsuite_entity_id="14006")
        db_session.add(existing_account)
        db_session.add(rdk)
        contact = Contact(netsuite_internal_id=504, name="Someone", source="netsuite",
                          customer_ref_name="14006 RDK Mechanical", company="14006 RDK Mechanical",
                          account_id=None)
        db_session.add(contact)
        db_session.commit()
        contact.account_id = existing_account.id
        db_session.add(contact)
        db_session.commit()

        backfill_company_id_prefix(db_session)
        db_session.refresh(contact)
        assert contact.account_id == existing_account.id  # unchanged, never overwritten
        assert contact.customer_ref_name == "RDK Mechanical"  # prefix still stripped
