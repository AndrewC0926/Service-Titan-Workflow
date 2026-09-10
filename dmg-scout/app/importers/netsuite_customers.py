"""Strict, atomic CSV import for the NetSuite "Scout" saved-search customer
export (checked directly against the real 7,093-row file, 2026-09-10/11).
Two-phase like app.importers.account_roster_csv: parse_and_validate() checks
every row before anything touches the session, import_netsuite_customers()
only ever applies an already-validated, error-free result. A file with any
row this module cannot place gets NOTHING imported, not a partial load --
see CATEGORY_TO_ACCOUNT_TYPE below for why that matters here specifically.

POSITIONAL COLUMNS, NOT csv.DictReader. The real export's header row is:
  Internal ID, Internal ID, ID, Name, Company Name, Stage, Status, Category,
  Sales Rep, Territory, Billing Address, Billing City, Billing State/Province,
  Billing Zip, Phone, Email, Date Created, Last Modified, Inactive, Name
"Internal ID" and "Name" each appear TWICE. csv.DictReader keys a row by
header text, so it would silently keep only the LAST occurrence of each --
wiping out the real Name (column 3) with column 19 (blank on 7,007 of 7,093
rows, so this corrupts nearly the whole file, not just a few rows). Column 19
is not a duplicate at all: it holds the PARENT's own bare name on the 86 rows
that have one (blank otherwise) -- a second, differently-meaning field
NetSuite exported under the same column label. See _COL_HEADER_SHAPE and
test_header_shape_guard below: an import that runs unmapped against a header
row NetSuite has silently reshaped is exactly this same corruption again,
just undetected. That guard is why THIS importer refuses csv.DictReader
categorically, not only for this one file.

PARENT LINKAGE: checked directly against the real file before writing this
-- the two "Internal ID" occurrences (columns 0 and 1) are IDENTICAL on
every single row, including all 86 rows that carry a parent name in column
19 (0 rows differ, of 7,093). NetSuite's own saved search does not actually
expose the parent's Internal ID as a separate value here, despite the
column being labeled "Internal ID" twice -- whatever produced this export,
column 1 is not "Parent Customer : Internal ID" in practice. Per the
instruction this was built under: Account.netsuite_parent_internal_id is
NOT populated by this importer (always NULL) because there is nothing to
put in it; app.models.Account.parent_id is therefore also never set here.
The only parent SIGNAL this file actually carries is the parent's NAME
(column 19) -- not used for linkage, since a name is never an identity key
in this codebase (see the normalize_name warning below) and this module was
told not to attempt one. A human wanting real parent/child accounts needs
either a cleaner NetSuite export or a manual pass.

normalize_name IS THE WRONG KEY FOR COMPANY NAMES and is never used for
identity anywhere in this module -- it populates Account.name_norm (a
pre-existing, required column other subsystems read, e.g. search and
fuzzy-dup UI) purely for THEIR benefit; this importer's own matching and
idempotency is netsuite_internal_id, full stop, never name_norm. It exists
for permit/project-signal identity
(stripping phase/parcel words, SPE codes, roman numerals) and, applied to
ordinary company names, silently collides unrelated companies -- "MCM
ENGINEERING, INC." (MCM parses as the roman numeral 1900), "L&D Engineering
Inc." (L and D are each roman numerals), and "P2S Engineering, Inc." (matches
the SPE-code shape) all normalize to the bare string "engineering". This
importer's identity key is netsuite_internal_id, full stop -- name is stored
for display, never matched on. See scout netsuite-blast-radius (or the
Phase B report this module was written for) for the measured scope of that
collision problem elsewhere in this codebase (the Firm table).

CATEGORY MAPPING: config.yaml's netsuite.category_to_account_type is the
ONLY source of truth for Category -> app.accounts.ACCOUNT_TYPES. A blank
Category stays NULL (Account.account_type has no default as of the
migration this importer shipped with -- see that migration's own
docstring). A NON-blank Category with no entry in that table is a
whole-file validation error, not a skipped row and not a guess -- two real
categories in the actual export (Acoustician, 7 rows; Ambient, 2 rows) have
no entry there today and will fail loudly until a human adds one.

DROPPED COLUMNS (by instruction): Phone, Email, Territory, Billing Zip --
Account has no fields for any of these and none were added. Billing
Address/City/State ARE kept (address/city/state).

Hawaii rows (512 in the real file, Billing State/Province == "HI") import
normally, state="HI" -- not filtered here. They are excluded from ranked
views by the existing territory default (see app.config's territory
county list), the same mechanism that already excludes any other
out-of-territory row; this importer does not special-case them.

is_dmg_internal: true when Category == "DMG Office" (47 rows in the real
file) OR the row's Name exactly matches an entry in config.yaml's
netsuite.dmg_internal_names (a single known non-customer test record,
"SCS Cloud Payments Test (DMG Corp)", whose own Category is blank -- a
name-matched special case, not a Category rule). These rows import (deleting
DMG's own branch-office history would be its own kind of data loss) but
every ranked view / join in this app must filter them out explicitly --
this importer does not do that filtering itself, it only sets the flag.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.models import Account
from app.normalize import normalize_name

# Positional column indices, 0-based, matching the real export's header row
# EXACTLY as checked 2026-09-10/11 -- see this module's own docstring for
# why csv.DictReader is never used here.
_COL_INTERNAL_ID = 0
_COL_INTERNAL_ID_DUP = 1  # verified identical to _COL_INTERNAL_ID on every row; not the parent id
_COL_ENTITY_ID = 2
_COL_NAME = 3
_COL_CATEGORY = 7
_COL_SALES_REP = 8
_COL_BILLING_ADDRESS = 10
_COL_BILLING_CITY = 11
_COL_BILLING_STATE = 12
_COL_LAST_MODIFIED = 17
_COL_INACTIVE = 18
_COL_PARENT_NAME = 19  # NOT stored; see module docstring -- no linkage column to put it in
_EXPECTED_COLUMN_COUNT = 20

# The exact header text, in order, this importer was built against. Compared
# verbatim (case-sensitive) by _check_header_shape -- a header that has
# changed shape (a reordered/renamed/added/removed column) invalidates every
# positional index above silently, so this importer refuses to guess and
# fails the whole file instead. See test_header_shape_guard.
_EXPECTED_HEADER = [
    "Internal ID", "Internal ID", "ID", "Name", "Company Name", "Stage", "Status",
    "Category", "Sales Rep", "Territory", "Billing Address", "Billing City",
    "Billing State/Province", "Billing Zip", "Phone", "Email", "Date Created",
    "Last Modified", "Inactive", "Name",
]

_LAST_MODIFIED_FORMAT = "%m/%d/%Y %I:%M %p"


@dataclass
class NetsuiteImportError:
    line_no: int  # 0 = whole-file problem (header shape, missing config); else the CSV row number
    field: str
    message: str

    def __str__(self) -> str:
        where = "file" if self.line_no == 0 else f"row {self.line_no}"
        return f"{where}: {self.field}: {self.message}"


class NetsuiteImportInvalid(Exception):
    """Raised by parse_and_validate() (never by apply_import()). Carries
    every problem found in the file, not just the first -- same discipline
    as app.importers.account_roster_csv.AccountRosterInvalid."""

    def __init__(self, errors: list[NetsuiteImportError]):
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s) -- nothing imported:\n" +
                          "\n".join(f"  {e}" for e in errors))


@dataclass
class ParsedNetsuiteRow:
    line_no: int
    internal_id: int
    entity_id: str
    name: str
    category_raw: str
    account_type: str | None  # None for a blank Category; never a guess
    sales_rep: str
    address: str
    city: str
    state: str
    last_modified: datetime | None
    is_active: bool
    is_dmg_internal: bool


def _check_header_shape(headers: list[str]) -> NetsuiteImportError | None:
    if headers != _EXPECTED_HEADER:
        return NetsuiteImportError(
            0, "header row",
            f"expected {_EXPECTED_HEADER!r}, got {headers!r} -- this importer uses positional "
            "column indices and refuses to guess a new shape; every index in this module must be "
            "re-checked by hand against the real file before this can run again."
        )
    return None


def parse_and_validate(raw_text: str, cfg: Config) -> list[ParsedNetsuiteRow]:
    """Validates the ENTIRE file before returning anything. Raises
    NetsuiteImportInvalid (every error found, across every row) if anything
    is wrong -- callers must not call apply_import() unless this returns
    normally."""
    reader = csv.reader(io.StringIO(raw_text))
    try:
        headers = next(reader)
    except StopIteration:
        raise NetsuiteImportInvalid([NetsuiteImportError(0, "file", "no header row found")])

    header_error = _check_header_shape(headers)
    if header_error:
        raise NetsuiteImportInvalid([header_error])

    category_map: dict[str, str] = cfg.get("netsuite.category_to_account_type", {}) or {}
    dmg_internal_names = set(cfg.get("netsuite.dmg_internal_names", []) or [])
    if not category_map:
        raise NetsuiteImportInvalid([NetsuiteImportError(
            0, "config", "netsuite.category_to_account_type is empty or missing in config.yaml -- "
            "refusing to import with no mapping table rather than leave every non-blank Category NULL")])

    errors: list[NetsuiteImportError] = []
    parsed: list[ParsedNetsuiteRow] = []
    seen_internal_ids: dict[int, int] = {}  # internal_id -> first line_no seen at

    for line_no, row in enumerate(reader, start=2):
        if len(row) != _EXPECTED_COLUMN_COUNT:
            errors.append(NetsuiteImportError(line_no, "row shape",
                                              f"expected {_EXPECTED_COLUMN_COUNT} columns, got {len(row)}"))
            continue

        raw_internal_id = row[_COL_INTERNAL_ID].strip()
        try:
            internal_id = int(raw_internal_id)
        except ValueError:
            errors.append(NetsuiteImportError(line_no, "Internal ID",
                                              f"not an integer: {raw_internal_id!r}"))
            continue

        if internal_id in seen_internal_ids:
            errors.append(NetsuiteImportError(
                line_no, "Internal ID",
                f"duplicate of row {seen_internal_ids[internal_id]} (same NetSuite Internal ID "
                f"{internal_id}) -- NetSuite's own internal id is supposed to be unique per customer"))
            continue
        seen_internal_ids[internal_id] = line_no

        name = row[_COL_NAME].strip()
        if not name:
            errors.append(NetsuiteImportError(line_no, "Name", "required, blank"))
            continue

        category_raw = row[_COL_CATEGORY].strip()
        account_type: str | None = None
        if category_raw:
            account_type = category_map.get(category_raw)
            if account_type is None:
                errors.append(NetsuiteImportError(
                    line_no, "Category",
                    f"{category_raw!r} has no entry in config.yaml's netsuite.category_to_account_type "
                    "-- add one (and to app.accounts.ACCOUNT_TYPES if it's genuinely new) before "
                    "re-running; this importer does not guess a mapping for an unlisted Category"))
                continue

        raw_last_modified = row[_COL_LAST_MODIFIED].strip()
        last_modified = None
        if raw_last_modified:
            try:
                last_modified = datetime.strptime(raw_last_modified, _LAST_MODIFIED_FORMAT)
            except ValueError:
                errors.append(NetsuiteImportError(line_no, "Last Modified",
                                                  f"unparseable date: {raw_last_modified!r}"))
                continue

        inactive_raw = row[_COL_INACTIVE].strip().lower()
        is_active = inactive_raw != "yes"

        is_dmg_internal = category_raw == "DMG Office" or name in dmg_internal_names

        parsed.append(ParsedNetsuiteRow(
            line_no=line_no, internal_id=internal_id, entity_id=row[_COL_ENTITY_ID].strip(),
            name=name, category_raw=category_raw, account_type=account_type,
            sales_rep=row[_COL_SALES_REP].strip(), address=row[_COL_BILLING_ADDRESS].strip(),
            city=row[_COL_BILLING_CITY].strip(), state=row[_COL_BILLING_STATE].strip(),
            last_modified=last_modified, is_active=is_active, is_dmg_internal=is_dmg_internal,
        ))

    if errors:
        raise NetsuiteImportInvalid(errors)
    return parsed


def _existing_by_internal_id(session: Session) -> dict[int, Account]:
    existing = session.exec(
        select(Account).where(Account.netsuite_internal_id.is_not(None))
    ).all()
    return {a.netsuite_internal_id: a for a in existing}


def _target_fields(row: ParsedNetsuiteRow) -> dict:
    return {
        "name": row.name, "netsuite_entity_id": row.entity_id, "account_type": row.account_type,
        "netsuite_sales_rep": row.sales_rep, "address": row.address or None, "city": row.city or None,
        "state": row.state or None, "netsuite_last_modified": row.last_modified,
        "is_active": row.is_active, "is_dmg_internal": row.is_dmg_internal,
    }


@dataclass
class NetsuitePreviewRow:
    line_no: int
    internal_id: int
    name: str
    action: str  # "insert" | "update" | "skip"
    changes: dict = dc_field(default_factory=dict)


def preview_import(session: Session, rows: list[ParsedNetsuiteRow]) -> list[NetsuitePreviewRow]:
    """Read-only dry run: exactly the matching apply_import would do, no
    session.add/flush anywhere in this function. Requires rows already came
    from parse_and_validate."""
    by_internal_id = _existing_by_internal_id(session)
    out: list[NetsuitePreviewRow] = []
    for row in rows:
        account = by_internal_id.get(row.internal_id)
        target = _target_fields(row)
        if account is None:
            out.append(NetsuitePreviewRow(line_no=row.line_no, internal_id=row.internal_id,
                                          name=row.name, action="insert"))
            continue
        changes = {k: (getattr(account, k), v) for k, v in target.items() if getattr(account, k) != v}
        action = "update" if changes else "skip"
        out.append(NetsuitePreviewRow(line_no=row.line_no, internal_id=row.internal_id,
                                      name=row.name, action=action, changes=changes))
    return out


def category_histogram(rows: list[ParsedNetsuiteRow]) -> dict[str, int]:
    hist: dict[str, int] = {}
    for row in rows:
        key = row.category_raw or "(blank)"
        hist[key] = hist.get(key, 0) + 1
    return hist


def apply_import(session: Session, rows: list[ParsedNetsuiteRow]) -> dict:
    """Applies an already-validated import in ONE uncommitted transaction
    (session.add/flush only) -- the caller commits once at the end, matching
    app.importers.account_roster_csv.apply_roster's own all-or-nothing
    guarantee. Idempotent on netsuite_internal_id: re-running the same file
    updates changed fields on existing rows and inserts nothing new for a
    row already present.

    netsuite_parent_internal_id / Account.parent_id are never set here -- see
    this module's own docstring for why (the real export carries no distinct
    parent Internal ID to set them from)."""
    by_internal_id = _existing_by_internal_id(session)
    inserted = updated = skipped = 0
    for row in rows:
        account = by_internal_id.get(row.internal_id)
        target = _target_fields(row)
        if account is None:
            # name_norm is a pre-existing, required Account column other
            # subsystems read (search, fuzzy-dup UI) -- populated here for
            # THEM, never read back by this importer's own matching, which
            # is netsuite_internal_id only. That's the distinction this
            # module's own "normalize_name is the wrong key" warning draws:
            # not "never call it," but "never key identity on it here."
            account = Account(netsuite_internal_id=row.internal_id,
                              name_norm=normalize_name(row.name), **target)
            session.add(account)
            session.flush()
            by_internal_id[row.internal_id] = account
            inserted += 1
        else:
            changed = any(getattr(account, k) != v for k, v in target.items())
            if changed:
                for k, v in target.items():
                    setattr(account, k, v)
                session.add(account)
                updated += 1
            else:
                skipped += 1
    session.flush()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def import_netsuite_customers(session: Session, raw_text: str, cfg: Config) -> dict:
    """The single entry point: validate the whole file, then apply it -- or
    apply nothing at all. See parse_and_validate and apply_import."""
    rows = parse_and_validate(raw_text, cfg)
    return apply_import(session, rows)
