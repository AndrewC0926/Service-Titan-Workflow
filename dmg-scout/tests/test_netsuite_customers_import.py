"""app.importers.netsuite_customers -- Phase B of the NetSuite "Scout"
customer import. See that module's own docstring for the real file's
quirks (duplicate headers, no real parent Internal ID, normalize_name's
company-name collision problem) this test file exists to pin down."""
from datetime import datetime

import pytest
from sqlmodel import select

from app.importers.netsuite_customers import (
    NetsuiteImportInvalid,
    _EXPECTED_HEADER,
    apply_import,
    category_histogram,
    import_netsuite_customers,
    parse_and_validate,
    preview_import,
)
from app.models import Account

HEADER = ",".join(_EXPECTED_HEADER)


def _row(internal_id="100", entity_id="1", name="Acme Mechanical", category="Mechanical Contractor",
        sales_rep="J. Smith", address="1 Main St", city="Los Angeles", state="CA",
        last_modified="1/2/2024 3:04 pm", inactive="No", parent_name=""):
    company_name = name
    return (f"{internal_id},{internal_id},{entity_id},{name},{company_name},Customer,"
           f"CUSTOMER-Secured,{category},{sales_rep},,{address},{city},{state},90001,,,"
           f"1/1/2020 1:00 pm,{last_modified},{inactive},{parent_name}")


def _csv(*rows):
    return HEADER + "\n" + "\n".join(rows) + "\n"


# ---- header shape guard ------------------------------------------------

def test_header_shape_guard(db_session, cfg):
    """A header row that doesn't match this importer's own positional
    assumptions must fail the WHOLE file, not silently misread columns --
    this is the regression guard for the exact corruption class the real
    file's duplicate 'Internal ID'/'Name' columns create."""
    bad_header = ",".join(_EXPECTED_HEADER[:-1])  # drop the trailing duplicate "Name"
    raw = bad_header + "\n" + _row()
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any(e.field == "header row" for e in exc.value.errors)


def test_header_shape_guard_passes_on_real_shape(cfg):
    """The guard itself must not false-positive on the actual real-file
    header -- this is what test_header_shape_guard is a regression test
    FOR, not a tautology: a change to _EXPECTED_HEADER without a matching
    change to the column index constants would still pass this one."""
    raw = _csv(_row())
    rows = parse_and_validate(raw, cfg)
    assert len(rows) == 1


# ---- blank Category -> NULL, never guessed -----------------------------

def test_blank_category_becomes_none_not_mechanical_contractor(db_session, cfg):
    """The regression this whole migration exists for: a blank Category
    must import as NULL, never silently default to mechanical_contractor
    (the former Account.account_type default AND the former
    app.importers.accounts_csv fallback, both removed for this reason)."""
    raw = _csv(_row(internal_id="200", category=""))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].account_type is None

    import_netsuite_customers(db_session, raw, cfg)
    db_session.commit()
    account = db_session.exec(select(Account).where(Account.netsuite_internal_id == 200)).one()
    assert account.account_type is None


def test_populated_category_maps_via_config_table(db_session, cfg):
    raw = _csv(_row(internal_id="201", category="Mechanical Engineer"))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].account_type == "engineer"


def test_unmapped_category_fails_the_whole_file_loudly(db_session, cfg):
    """Acoustician and Ambient are real categories in the actual export with
    no entry in config.yaml's netsuite.category_to_account_type -- this
    must be a whole-file validation error (nothing imported), never a
    guessed mapping and never a silently skipped row."""
    raw = _csv(_row(internal_id="300", category="Acoustician"),
              _row(internal_id="301", category="Mechanical Contractor"))
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any("Acoustician" in str(e) for e in exc.value.errors)

    db_session.commit()
    assert db_session.exec(select(Account)).all() == []


# ---- is_dmg_internal ----------------------------------------------------

def test_dmg_office_category_sets_is_dmg_internal(cfg):
    raw = _csv(_row(internal_id="400", name="DMG Hawaii", category="DMG Office"))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].is_dmg_internal is True
    assert rows[0].account_type == "dmg_internal"


def test_name_matched_test_record_sets_is_dmg_internal_with_blank_category(cfg):
    """The one known non-customer test row: blank Category (so NOT caught by
    the DMG Office rule) but a name-matched special case in config.yaml's
    netsuite.dmg_internal_names."""
    raw = _csv(_row(internal_id="401", name="SCS Cloud Payments Test (DMG Corp)", category=""))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].is_dmg_internal is True
    assert rows[0].account_type is None


def test_ordinary_account_is_not_dmg_internal(cfg):
    raw = _csv(_row(internal_id="402", name="Acme Mechanical", category="Mechanical Contractor"))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].is_dmg_internal is False


# ---- is_active from Inactive --------------------------------------------

def test_inactive_yes_is_not_active(cfg):
    raw = _csv(_row(internal_id="500", inactive="Yes"))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].is_active is False


def test_inactive_no_is_active(cfg):
    raw = _csv(_row(internal_id="501", inactive="No"))
    rows = parse_and_validate(raw, cfg)
    assert rows[0].is_active is True


# ---- required-field / shape errors --------------------------------------

def test_blank_name_is_a_validation_error(cfg):
    raw = _csv(_row(internal_id="600", name=""))
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any(e.field == "Name" for e in exc.value.errors)


def test_duplicate_internal_id_within_file_is_an_error(cfg):
    raw = _csv(_row(internal_id="700", name="Acme A"), _row(internal_id="700", name="Acme B"))
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any("duplicate" in e.message.lower() for e in exc.value.errors)


def test_non_integer_internal_id_is_an_error(cfg):
    raw = _csv(_row(internal_id="not-a-number"))
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any(e.field == "Internal ID" for e in exc.value.errors)


def test_unparseable_last_modified_is_an_error(cfg):
    raw = _csv(_row(internal_id="800", last_modified="not-a-date"))
    with pytest.raises(NetsuiteImportInvalid) as exc:
        parse_and_validate(raw, cfg)
    assert any(e.field == "Last Modified" for e in exc.value.errors)


# ---- apply/idempotency ---------------------------------------------------

def test_import_inserts_new_account_keyed_on_internal_id(db_session, cfg):
    raw = _csv(_row(internal_id="900", name="Acme Mechanical", address="1 Main St", city="LA", state="CA"))
    result = import_netsuite_customers(db_session, raw, cfg)
    db_session.commit()
    assert result == {"inserted": 1, "updated": 0, "skipped": 0}
    account = db_session.exec(select(Account).where(Account.netsuite_internal_id == 900)).one()
    assert account.name == "Acme Mechanical"
    assert account.address == "1 Main St"
    assert account.netsuite_parent_internal_id is None
    assert account.parent_id is None


def test_reimport_same_file_is_idempotent(db_session, cfg):
    raw = _csv(_row(internal_id="901", name="Acme Mechanical"))
    import_netsuite_customers(db_session, raw, cfg)
    db_session.commit()
    result = import_netsuite_customers(db_session, raw, cfg)
    db_session.commit()
    assert result == {"inserted": 0, "updated": 0, "skipped": 1}
    assert len(db_session.exec(select(Account).where(Account.netsuite_internal_id == 901)).all()) == 1


def test_reimport_with_changed_field_updates_not_inserts(db_session, cfg):
    raw1 = _csv(_row(internal_id="902", name="Acme Mechanical", sales_rep="J. Smith"))
    import_netsuite_customers(db_session, raw1, cfg)
    db_session.commit()
    raw2 = _csv(_row(internal_id="902", name="Acme Mechanical", sales_rep="M. Jones"))
    result = import_netsuite_customers(db_session, raw2, cfg)
    db_session.commit()
    assert result == {"inserted": 0, "updated": 1, "skipped": 0}
    account = db_session.exec(select(Account).where(Account.netsuite_internal_id == 902)).one()
    assert account.netsuite_sales_rep == "M. Jones"


def test_apply_is_atomic_no_commit_inside(db_session, cfg):
    """apply_import must only add/flush, never commit -- a caller wrapping
    it in a transaction that later fails should be able to roll back the
    whole batch."""
    raw = _csv(_row(internal_id="903"))
    rows = parse_and_validate(raw, cfg)
    apply_import(db_session, rows)
    db_session.rollback()
    assert db_session.exec(select(Account).where(Account.netsuite_internal_id == 903)).first() is None


# ---- preview / dry-run ----------------------------------------------------

def test_preview_marks_new_row_insert_and_writes_nothing(db_session, cfg):
    raw = _csv(_row(internal_id="1000"))
    rows = parse_and_validate(raw, cfg)
    preview = preview_import(db_session, rows)
    assert preview[0].action == "insert"
    db_session.commit()
    assert db_session.exec(select(Account).where(Account.netsuite_internal_id == 1000)).first() is None


def test_preview_marks_unchanged_existing_row_skip(db_session, cfg):
    raw = _csv(_row(internal_id="1001", name="Acme Mechanical"))
    import_netsuite_customers(db_session, raw, cfg)
    db_session.commit()
    rows = parse_and_validate(raw, cfg)
    preview = preview_import(db_session, rows)
    assert preview[0].action == "skip"


def test_category_histogram_counts_blank_as_its_own_bucket():
    raw = _csv(_row(internal_id="1100", category="Mechanical Contractor"),
              _row(internal_id="1101", category=""),
              _row(internal_id="1102", category="Mechanical Contractor"))
    from app.config import load_config
    rows = parse_and_validate(raw, load_config())
    hist = category_histogram(rows)
    assert hist == {"Mechanical Contractor": 2, "(blank)": 1}
