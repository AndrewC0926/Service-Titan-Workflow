"""Strict/atomic account roster CSV import (app.importers.account_roster_csv)
and the raw account<->project join-quality functions it feeds
(app.accounts.accounts_matching_projects_by_address/_by_owner_name,
account_role_coverage). See tests/fixtures/account_roster_sample.csv (the
happy path, 40 rows) and account_roster_bad_row.csv (deliberately broken,
proves whole-file rejection)."""
from pathlib import Path

from sqlmodel import select

from app.accounts import (
    account_role_coverage,
    accounts_matching_projects_by_address,
    accounts_matching_projects_by_owner_name,
    seed_product_lines,
)
from app.importers.account_roster_csv import (
    AccountRosterInvalid,
    import_account_roster,
    parse_and_validate,
)
from app.models import Account, AccountCoverage, ProductLine, Project, ProjectSignal, Signal, SignalType
from app.normalize import normalize_name

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CSV = (FIXTURES / "account_roster_sample.csv").read_text()
BAD_ROW_CSV = (FIXTURES / "account_roster_bad_row.csv").read_text()


def _seed_lines(db_session, cfg):
    seed_product_lines(db_session, cfg)


# ---- the 40-row happy-path fixture ----------------------------------------

def test_sample_fixture_imports_all_40_rows_cleanly(db_session, cfg):
    _seed_lines(db_session, cfg)
    stats = import_account_roster(db_session, SAMPLE_CSV)
    assert stats["inserted"] == 40
    assert stats["updated"] == 0
    assert stats["skipped"] == 0
    assert db_session.exec(select(Account)).all().__len__() == 40


def test_sample_fixture_compound_key_keeps_same_name_different_address_distinct(db_session, cfg):
    """Row 2 and row 3: same company name (one 'Inc.', one 'INC' with a
    comma), completely different address -- two real branches, must import
    as two accounts, not merge on name alone."""
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    matches = db_session.exec(
        select(Account).where(Account.name_norm == normalize_name("Pacific Coast Mechanical Inc."))
    ).all()
    assert len(matches) == 2
    assert {a.city for a in matches} == {"Los Angeles", "San Diego"}


def test_sample_fixture_derives_county_from_city_when_blank(db_session, cfg):
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    acct = db_session.exec(select(Account).where(Account.city == "Riverside")).one()
    assert acct.county == "Riverside"


def test_sample_fixture_leaves_county_null_when_city_unmapped(db_session, cfg):
    """'Twin Peaks' is a real city not in app.geo's table on purpose --
    must land NULL, never a guess."""
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    acct = db_session.exec(select(Account).where(Account.city == "Twin Peaks")).one()
    assert acct.county is None


def test_sample_fixture_leaves_optional_fields_null_not_zero_when_blank(db_session, cfg):
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    acct = db_session.exec(select(Account).where(Account.city == "Ridgecrest")).one()
    assert acct.last_order_date is None
    assert acct.annual_revenue is None


def test_sample_fixture_sets_bought_status_for_named_product_lines(db_session, cfg):
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    acct = db_session.exec(
        select(Account).where(Account.name_norm == normalize_name("OC Mechanical Contractors"))
    ).one()
    marley = db_session.exec(select(ProductLine).where(ProductLine.name == "Marley")).one()
    cov = db_session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == acct.id,
                                      AccountCoverage.product_line_id == marley.id)
    ).one()
    assert cov.status == "bought"
    # every other line on the card still got a row (ensure_coverage_rows'
    # own contract), just not bumped to bought
    all_cov = db_session.exec(select(AccountCoverage).where(AccountCoverage.account_id == acct.id)).all()
    assert len(all_cov) == 70


# ---- idempotency ------------------------------------------------------

def test_reimport_of_unchanged_file_is_a_pure_no_op(db_session, cfg):
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    before = db_session.exec(select(Account)).all().__len__()
    stats = import_account_roster(db_session, SAMPLE_CSV)
    assert stats == {"inserted": 0, "updated": 0, "skipped": 40,
                     "counties_derived": 38, "counties_unresolved": 1}
    assert db_session.exec(select(Account)).all().__len__() == before


def test_reimport_after_a_value_changes_updates_in_place_not_a_duplicate(db_session, cfg):
    """The real scenario item 2 is about: get the format wrong (or a value
    wrong) the first time, fix it, reload -- must update the SAME row, not
    create a second account."""
    _seed_lines(db_session, cfg)
    import_account_roster(db_session, SAMPLE_CSV)
    fixed = SAMPLE_CSV.replace("Jim Torres", "James Torres", 1)  # first occurrence only, row 2
    stats = import_account_roster(db_session, fixed)
    assert stats["inserted"] == 0
    assert stats["updated"] == 1
    assert stats["skipped"] == 39
    assert db_session.exec(select(Account)).all().__len__() == 40
    acct = db_session.exec(
        select(Account).where(Account.name_norm == normalize_name("Pacific Coast Mechanical Inc."),
                              Account.city == "Los Angeles")
    ).one()
    assert acct.assigned_rep == "James Torres"


# ---- atomic failure -----------------------------------------------------

def test_bad_row_fixture_aborts_the_whole_file_and_writes_nothing(db_session, cfg):
    _seed_lines(db_session, cfg)
    try:
        import_account_roster(db_session, BAD_ROW_CSV)
        assert False, "should have raised AccountRosterInvalid"
    except AccountRosterInvalid as exc:
        messages = {(e.line_no, e.field) for e in exc.errors}
        assert (3, "city") in messages
        assert (4, "last order date") in messages
        assert (5, "annual revenue") in messages
        assert (6, "product lines bought") in messages
        assert (7, "account name + street address") in messages
        assert len(exc.errors) == 5
    # nothing written -- not even the one genuinely valid row (row 2)
    assert db_session.exec(select(Account)).all() == []


def test_missing_required_column_rejects_the_whole_file_before_any_row_is_read(db_session, cfg):
    _seed_lines(db_session, cfg)
    no_county_column = "Account Name,Street Address,City,Account Owner\nAcme,1 Main St,Riverside,Jim\n"
    try:
        import_account_roster(db_session, no_county_column)
        assert False, "should have raised AccountRosterInvalid"
    except AccountRosterInvalid as exc:
        assert len(exc.errors) == 1
        assert exc.errors[0].line_no == 0
        assert "county" in exc.errors[0].message
    assert db_session.exec(select(Account)).all() == []


def test_unmatched_product_line_name_is_a_parse_failure_not_a_silent_drop(db_session, cfg):
    _seed_lines(db_session, cfg)
    csv_text = ("Account Name,Street Address,City,County,Account Owner,"
               "Last Order Date,Annual Revenue,Product Lines Bought\n"
               "Acme,1 Main St,Riverside,,Jim,,,NotARealLine\n")
    try:
        parse_and_validate(csv_text, db_session)
        assert False, "should have raised AccountRosterInvalid"
    except AccountRosterInvalid as exc:
        assert "NotARealLine" in exc.errors[0].message


# ---- join quality (item 4: raw joins, no scoring) --------------------

def _account(db_session, name, address=None, county=None):
    a = Account(name=name, name_norm=normalize_name(name), address=address, county=county)
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


def _project(db_session, name, developer=None, status="active"):
    p = Project(name=name, developer=developer, status=status)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _signal_with_address(db_session, project, street_address):
    s = Signal(signal_type=SignalType.ceqa_nop, street_address=street_address)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    db_session.add(ProjectSignal(project_id=project.id, signal_id=s.id))
    db_session.commit()
    return s


def test_join_by_address_matches_via_linked_signal(db_session):
    """Project itself has no address column -- the join necessarily goes
    through a linked signal's street_address, normalized the same way."""
    account = _account(db_session, "Vantage Mechanical", address="123 Main St, Suite 4")
    project = _project(db_session, "Vantage Campus")
    _signal_with_address(db_session, project, "123 MAIN STREET")
    matches = accounts_matching_projects_by_address(db_session, account)
    assert [p.id for p in matches] == [project.id]


def test_join_by_address_finds_nothing_when_no_signal_matches(db_session):
    account = _account(db_session, "Vantage Mechanical", address="999 Nowhere Ave")
    project = _project(db_session, "Vantage Campus")
    _signal_with_address(db_session, project, "123 Main St")
    assert accounts_matching_projects_by_address(db_session, account) == []


def test_join_by_address_ignores_non_active_projects(db_session):
    account = _account(db_session, "Vantage Mechanical", address="123 Main St")
    dead = _project(db_session, "Dead Deal", status="dead")
    _signal_with_address(db_session, dead, "123 Main St")
    assert accounts_matching_projects_by_address(db_session, account) == []


def test_join_by_owner_name_matches_account_as_the_project_developer(db_session):
    account = _account(db_session, "Vantage Data Centers")
    project = _project(db_session, "Vantage NV11", developer="Vantage Data Centers NV11, LLC")
    matches = accounts_matching_projects_by_owner_name(db_session, account)
    assert [p.id for p in matches] == [project.id]


def test_join_by_owner_name_finds_nothing_for_a_vendor_not_a_developer(db_session):
    account = _account(db_session, "Southland Air Systems")
    _project(db_session, "Vantage NV11", developer="Vantage Data Centers")
    assert accounts_matching_projects_by_owner_name(db_session, account) == []


def test_role_coverage_counts_distinct_bought_roles_not_bought_lines(db_session, cfg):
    """'Buys from us in N of 13 roles' -- two bought lines in the SAME role
    must still count as one role, not two."""
    seed_product_lines(db_session, cfg)
    account = _account(db_session, "Southland Air Systems")
    from app.accounts import ensure_coverage_rows
    ensure_coverage_rows(db_session, account)
    aaon = db_session.exec(select(ProductLine).where(ProductLine.name == "AAON")).one()  # air_handling
    energy_labs = db_session.exec(select(ProductLine).where(ProductLine.name == "Energy Labs")).one()  # air_handling
    marley = db_session.exec(select(ProductLine).where(ProductLine.name == "Marley")).one()  # heat_rejection
    for line in (aaon, energy_labs, marley):
        cov = db_session.exec(
            select(AccountCoverage).where(AccountCoverage.account_id == account.id,
                                          AccountCoverage.product_line_id == line.id)
        ).one()
        cov.status = "bought"
        db_session.add(cov)
    db_session.commit()

    report = account_role_coverage(db_session, account)
    assert report["total_roles"] == 13
    assert report["bought_roles"] == 2
    assert set(report["roles_bought"]) == {"air_handling", "heat_rejection"}
    assert "air_handling" not in report["roles_missing"]
    assert len(report["roles_missing"]) == 11


def test_role_coverage_is_zero_for_an_account_with_no_bought_lines(db_session, cfg):
    seed_product_lines(db_session, cfg)
    account = _account(db_session, "New Prospect Co")
    from app.accounts import ensure_coverage_rows
    ensure_coverage_rows(db_session, account)
    report = account_role_coverage(db_session, account)
    assert report["bought_roles"] == 0
    assert report["roles_bought"] == []
    assert len(report["roles_missing"]) == 13
