"""Block 4B-prep-3 Item 5: whether a Contractor is a DMG customer
(app.pipeline.dmg_customer_roster)."""
from app.models import Contractor, DmgCustomerRoster
from app.pipeline.dmg_customer_roster import (
    contractor_dmg_customer_status, dmg_customer_status_evidence, roster_loaded,
)


def _contractor(business_name, full_business_name=None, license_no="1"):
    return Contractor(license_no=license_no, business_name=business_name, full_business_name=full_business_name)


class TestRosterLoaded:
    def test_false_when_empty(self, db_session):
        assert roster_loaded(db_session) is False

    def test_true_once_a_row_exists(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical"))
        db_session.commit()
        assert roster_loaded(db_session) is True


class TestContractorDmgCustomerStatus:
    def test_unknown_when_roster_empty(self, db_session):
        contractor = _contractor("Acme Mechanical Inc")
        db_session.add(contractor)
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, contractor) == (None, None)

    def test_unknown_when_no_contractor_identified(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical"))
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, None) == (None, None)

    def test_exact_match_on_business_name_reports_customer(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical",
                                         assigned_rep="James Burwell"))
        contractor = _contractor("Acme Mechanical Inc", license_no="1")
        db_session.add(contractor)
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, contractor) == (True, "James Burwell")

    def test_match_via_full_business_name_too(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Steven T Mack Construction",
                                         name_norm="steven t mack construction"))
        contractor = _contractor("MACK STEVEN T CONSTRUCTION INC",
                                 full_business_name="Steven T Mack Construction Inc", license_no="2")
        db_session.add(contractor)
        db_session.commit()
        is_customer, _ = contractor_dmg_customer_status(db_session, contractor)
        assert is_customer is True

    def test_never_fuzzy_no_match_reports_false(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical"))
        contractor = _contractor("Acme Mechanicl Incorportaed", license_no="3")  # typo'd, close but not exact
        db_session.add(contractor)
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, contractor) == (False, None)

    def test_no_assigned_rep_on_file_reports_customer_with_null_rep(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical",
                                         assigned_rep=None))
        contractor = _contractor("Acme Mechanical Inc", license_no="4")
        db_session.add(contractor)
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, contractor) == (True, None)

    def test_duplicate_roster_rows_still_report_customer_and_first_real_rep(self, db_session):
        """The real master file has been observed to carry more than one
        row for the same company name under distinct internal ids (e.g.
        two "GLM Heating and Air Conditioning Inc." rows) -- reports
        True and whichever row actually names a rep, rather than
        refusing to answer."""
        db_session.add(DmgCustomerRoster(company_name="GLM Heating and Air Conditioning Inc.",
                                         name_norm="glm heating and air conditioning", assigned_rep=None))
        db_session.add(DmgCustomerRoster(company_name="GLM Heating and Air Conditioning Inc.",
                                         name_norm="glm heating and air conditioning", assigned_rep="Evan Brown"))
        contractor = _contractor("GLM Heating and Air Conditioning Inc.", license_no="5")
        db_session.add(contractor)
        db_session.commit()
        assert contractor_dmg_customer_status(db_session, contractor) == (True, "Evan Brown")


class TestDmgCustomerStatusEvidence:
    def test_no_contractor_identified(self, db_session):
        assert dmg_customer_status_evidence(db_session, None) == \
            "Contractor DMG-customer status: unknown, no contractor identified for this contact."

    def test_roster_not_loaded(self, db_session):
        contractor = _contractor("Acme Mechanical Inc", license_no="6")
        db_session.add(contractor)
        db_session.commit()
        assert dmg_customer_status_evidence(db_session, contractor) == \
            "Contractor DMG-customer status: unknown, customer master not loaded."

    def test_is_customer_with_rep(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical",
                                         assigned_rep="James Burwell"))
        contractor = _contractor("Acme Mechanical Inc", license_no="7")
        db_session.add(contractor)
        db_session.commit()
        assert dmg_customer_status_evidence(db_session, contractor) == \
            "Contractor DMG-customer status: yes, assigned rep James Burwell."

    def test_is_customer_without_rep(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Acme Mechanical", name_norm="acme mechanical"))
        contractor = _contractor("Acme Mechanical Inc", license_no="8")
        db_session.add(contractor)
        db_session.commit()
        assert dmg_customer_status_evidence(db_session, contractor) == \
            "Contractor DMG-customer status: yes, no assigned rep on file."

    def test_not_a_customer(self, db_session):
        db_session.add(DmgCustomerRoster(company_name="Some Other Company", name_norm="some other company"))
        contractor = _contractor("Acme Mechanical Inc", license_no="9")
        db_session.add(contractor)
        db_session.commit()
        assert dmg_customer_status_evidence(db_session, contractor) == "Contractor DMG-customer status: no."
