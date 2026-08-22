"""Strict, atomic CSV import for a real DMG account roster export -- built
for the actual field trip (rep hands over a spreadsheet, it gets pasted
into a file, one command loads it), not for a general-purpose messy upload.
Deliberately separate from app.importers.accounts_csv, which exists for a
different job: a web-uploaded file with unknown/arbitrary column headers,
mapped by hand through a preview screen, applied row-by-row (a bad row is
skipped, not fatal). That tool is right for a one-off dormant-account
cleanup where nobody knows the column layout in advance. This one is right
for a known, fixed export format where a partially-loaded file would be
actively dangerous to debug standing in an office -- see README.md's
"Importing the account roster" section for the schema this expects.

Two-phase by design: parse_and_validate() checks every row against every
rule BEFORE anything touches the session -- a single bad cell anywhere in
the file surfaces every problem in the file at once (not just the first)
and writes nothing. apply_roster() only runs once validation reports zero
errors, and does the entire file in one uncommitted transaction (session.add
/session.flush, no intermediate session.commit()) so a failure during apply
itself -- a DB constraint this module's own validation didn't anticipate --
rolls back the whole file too, not just the row that hit it.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime

from sqlmodel import Session, select

from app.firms import match_firm
from app.geo import city_to_county
from app.models import Account, AccountCoverage, ProductLine
from app.normalize import normalize_name
from app.pipeline.retrofit import normalize_address

# Canonical header text (already lowercased/whitespace-collapsed) -> field
# key. Exact schema, not guessed -- see this module's own docstring for why.
REQUIRED_HEADERS: dict[str, str] = {
    "account name": "name",
    "street address": "address",
    "city": "city",
    "county": "county",
    "account owner": "account_owner",
}
OPTIONAL_HEADERS: dict[str, str] = {
    "last order date": "last_order_date",
    "annual revenue": "annual_revenue",
    "product lines bought": "product_lines_bought",
}
ALL_HEADERS: dict[str, str] = {**REQUIRED_HEADERS, **OPTIONAL_HEADERS}

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")
PRODUCT_LINE_DELIMITER = ";"


def _clean_header(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower())


def _address_signature(addr: str) -> str:
    """Case/whitespace-only normalization, deliberately with NO street-suffix
    truncation -- the lighter check that tells a true duplicate (same
    address, different formatting) apart from a normalize_address key
    collision (different addresses that happen to truncate the same way).
    Not a matching key on its own, only a comparison for two rows that
    ALREADY share a (name_norm, address_norm) key."""
    return re.sub(r"\s+", " ", addr.strip().upper())


@dataclass
class RosterError:
    line_no: int  # 0 = whole-file problem (e.g. missing header); else the CSV row number (header row = 1)
    field: str
    message: str

    def __str__(self) -> str:
        where = "file" if self.line_no == 0 else f"row {self.line_no}"
        return f"{where}: {self.field}: {self.message}"


class AccountRosterInvalid(Exception):
    """Raised by parse_and_validate() (never by apply_roster(), which only
    ever runs against an already-validated, error-free result). Carries
    every problem found in the file, not just the first."""

    def __init__(self, errors: list[RosterError]):
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s) -- nothing imported:\n" +
                          "\n".join(f"  {e}" for e in errors))


@dataclass
class ParsedRow:
    line_no: int
    name: str
    name_norm: str
    address: str
    address_norm: str
    city: str
    county: str | None
    county_source: str  # "csv" | "derived_from_city" | "unresolved"
    account_owner: str
    last_order_date: datetime | None
    annual_revenue: float | None
    product_line_ids: list[int] = dc_field(default_factory=list)
    product_line_names: list[str] = dc_field(default_factory=list)


def parse_and_validate(raw_text: str, session: Session) -> list[ParsedRow]:
    """Validates the ENTIRE file before returning anything. Raises
    AccountRosterInvalid (with every error found, across every row) if
    anything is wrong -- callers must not call apply_roster() unless this
    returns normally."""
    reader = csv.DictReader(io.StringIO(raw_text))
    headers = reader.fieldnames or []
    header_map: dict[str, str] = {}
    for h in headers:
        key = ALL_HEADERS.get(_clean_header(h))
        if key:
            header_map[h] = key
    have = set(header_map.values())
    missing = [canon for canon, key in REQUIRED_HEADERS.items() if key not in have]
    if missing or not headers:
        msg = ("no header row found" if not headers else
               f"missing required column(s): {', '.join(sorted(missing))}")
        raise AccountRosterInvalid([RosterError(0, "headers", msg)])

    lines = session.exec(select(ProductLine)).all()
    line_id_by_norm = {ln.name_norm: ln.id for ln in lines}
    line_name_by_norm = {ln.name_norm: ln.name for ln in lines}

    errors: list[RosterError] = []
    parsed: list[ParsedRow] = []
    seen_keys: dict[tuple[str, str], tuple[int, str]] = {}

    for line_no, raw_row in enumerate(reader, start=2):
        vals: dict[str, str] = {}
        for header, key in header_map.items():
            vals[key] = (raw_row.get(header) or "").strip()

        name = vals.get("name", "")
        address = vals.get("address", "")
        city = vals.get("city", "")
        account_owner = vals.get("account_owner", "")
        if not name:
            errors.append(RosterError(line_no, "account name", "required, blank"))
        if not address:
            errors.append(RosterError(line_no, "street address", "required, blank"))
        if not city:
            errors.append(RosterError(line_no, "city", "required, blank"))
        if not account_owner:
            errors.append(RosterError(line_no, "account owner", "required, blank"))
        # Downstream fields depend on name/address parsing cleanly -- skip
        # them for this row rather than compound one error into three.
        if not name or not address or not city or not account_owner:
            continue

        county_raw = vals.get("county", "")
        if county_raw:
            county, county_source = county_raw, "csv"
        else:
            derived = city_to_county(city)
            county, county_source = (derived, "derived_from_city") if derived else (None, "unresolved")

        last_order_date = None
        raw_date = vals.get("last_order_date", "")
        if raw_date:
            last_order_date = _parse_date(raw_date)
            if last_order_date is None:
                errors.append(RosterError(line_no, "last order date", f"unparseable date: {raw_date!r}"))

        annual_revenue = None
        raw_revenue = vals.get("annual_revenue", "")
        if raw_revenue:
            try:
                annual_revenue = float(raw_revenue.replace("$", "").replace(",", "").strip())
            except ValueError:
                errors.append(RosterError(line_no, "annual revenue", f"unparseable number: {raw_revenue!r}"))

        product_line_ids: list[int] = []
        product_line_names: list[str] = []
        raw_lines = vals.get("product_lines_bought", "")
        if raw_lines:
            for token in raw_lines.split(PRODUCT_LINE_DELIMITER):
                token = token.strip()
                if not token:
                    continue
                norm = normalize_name(token)
                line_id = line_id_by_norm.get(norm)
                if line_id is None:
                    errors.append(RosterError(line_no, "product lines bought",
                                              f"no line on the card matches {token!r}"))
                else:
                    product_line_ids.append(line_id)
                    product_line_names.append(line_name_by_norm[norm])

        name_norm = normalize_name(name)
        address_norm = normalize_address(address) or address.upper().strip()
        key = (name_norm, address_norm)
        if key in seen_keys:
            other_line_no, other_address = seen_keys[key]
            if _address_signature(address) == _address_signature(other_address):
                errors.append(RosterError(line_no, "account name + street address",
                                          f"duplicate of row {other_line_no} (same account, same address)"))
            else:
                # Same normalized key, but the RAW addresses actually differ --
                # normalize_address truncates at the first street-suffix token
                # (see its own docstring), which collapses two real, different
                # numbered-avenue addresses ("83-100 Ave 45" and "83-100 Ave
                # 47", both real Coachella Valley addresses, both normalize to
                # "83-100 AVE") into the same key. Reported as a distinct
                # failure from a literal duplicate -- this is NOT necessarily a
                # mistake in the file, it is a real limitation of reusing
                # app.pipeline.retrofit.normalize_address (built for permit-
                # to-parcel matching, not account identity) for this key.
                # Never silently pick one and drop the other.
                errors.append(RosterError(
                    line_no, "account name + street address",
                    f"same account name and normalizes to the same address key as row {other_line_no} "
                    f"({address_norm!r}), but the addresses are actually different ({address!r} vs "
                    f"{other_address!r}) -- likely a numbered-street truncation collision, not a real "
                    f"duplicate. Resolve by hand before re-running; this importer will not guess which "
                    f"row is right."))
            continue
        seen_keys[key] = (line_no, address)

        parsed.append(ParsedRow(
            line_no=line_no, name=name, name_norm=name_norm, address=address,
            address_norm=address_norm, city=city, county=county, county_source=county_source,
            account_owner=account_owner, last_order_date=last_order_date,
            annual_revenue=annual_revenue, product_line_ids=product_line_ids,
            product_line_names=product_line_names,
        ))

    if errors:
        raise AccountRosterInvalid(errors)
    return parsed


def _parse_date(value: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _target_fields(row: ParsedRow) -> dict:
    return {
        "address": row.address, "city": row.city, "county": row.county,
        "assigned_rep": row.account_owner, "last_order_date": row.last_order_date,
        "annual_revenue": row.annual_revenue,
    }


def _existing_accounts_by_key(session: Session) -> dict[tuple[str, str], Account]:
    """ACTIVE accounts only, keyed the same way apply_roster matches rows --
    a dormant/archived account is not silently revived by a coincidental
    name+address match on a later reload. Shared by apply_roster and
    preview_roster so the two can never disagree about what matches what."""
    existing = session.exec(select(Account).where(Account.status == "active")).all()
    by_key: dict[tuple[str, str], Account] = {}
    for a in existing:
        addr_norm = normalize_address(a.address) if a.address else None
        if addr_norm:
            by_key[(a.name_norm, addr_norm)] = a
    return by_key


@dataclass
class RosterPreviewRow:
    line_no: int
    name: str
    address: str
    action: str  # "insert" | "update" | "skip"
    account_id: int | None  # set for update/skip, None for insert
    changes: dict = dc_field(default_factory=dict)  # field -> (old, new); only set for "update"
    newly_bought_lines: list[str] = dc_field(default_factory=list)


def preview_roster(session: Session, rows: list[ParsedRow]) -> list[RosterPreviewRow]:
    """Read-only dry run: exactly the matching apply_roster would do, with
    NO session.add/flush anywhere in this function -- what --dry-run
    prints. Requires rows already came from parse_and_validate (so the file
    is already known to be clean); this only decides insert/update/skip and
    what would change, it does not re-validate."""
    by_key = _existing_accounts_by_key(session)
    bought_by_account: dict[int, set[int]] = {}

    out: list[RosterPreviewRow] = []
    for row in rows:
        account = by_key.get((row.name_norm, row.address_norm))
        target = _target_fields(row)
        if account is None:
            out.append(RosterPreviewRow(line_no=row.line_no, name=row.name, address=row.address,
                                        action="insert", account_id=None,
                                        newly_bought_lines=list(row.product_line_names)))
            continue

        changes = {k: (getattr(account, k), v) for k, v in target.items() if getattr(account, k) != v}
        if account.id not in bought_by_account:
            already = session.exec(
                select(AccountCoverage.product_line_id).where(
                    AccountCoverage.account_id == account.id, AccountCoverage.status == "bought")
            ).all()
            bought_by_account[account.id] = set(already)
        newly_bought = [name for line_id, name in zip(row.product_line_ids, row.product_line_names)
                        if line_id not in bought_by_account[account.id]]

        action = "update" if changes else "skip"
        out.append(RosterPreviewRow(line_no=row.line_no, name=row.name, address=row.address,
                                    action=action, account_id=account.id, changes=changes,
                                    newly_bought_lines=newly_bought))
    return out


def apply_roster(session: Session, rows: list[ParsedRow]) -> dict:
    """Applies an already-validated roster in ONE uncommitted transaction
    (session.add/flush only) -- the caller (see app.cli's session_scope)
    commits once at the very end, so any exception here leaves the database
    exactly as it was before this call, matching parse_and_validate's own
    all-or-nothing guarantee for the DB-write half of the same promise.

    Keyed on (name_norm, normalized street address) against ACTIVE accounts
    only, same scope app.importers.accounts_csv already matches against --
    a dormant/archived account is not silently revived by a coincidental
    name+address match on a later reload.
    """
    by_key = _existing_accounts_by_key(session)
    all_lines = session.exec(select(ProductLine)).all()

    inserted = updated = skipped = 0
    for row in rows:
        account = by_key.get((row.name_norm, row.address_norm))
        target = _target_fields(row)
        if account is None:
            account = Account(name=row.name, name_norm=row.name_norm, **target)
            firm = match_firm(session, row.name)
            if firm is not None:
                account.firm_id = firm.id
            session.add(account)
            session.flush()  # assigns account.id without committing
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

        existing_coverage = {
            c.product_line_id: c for c in session.exec(
                select(AccountCoverage).where(AccountCoverage.account_id == account.id)).all()
        }
        for line in all_lines:
            if line.id not in existing_coverage:
                cov = AccountCoverage(account_id=account.id, product_line_id=line.id)
                session.add(cov)
                existing_coverage[line.id] = cov
        for line_id in row.product_line_ids:
            cov = existing_coverage[line_id]
            if cov.status != "bought":
                cov.status = "bought"
                session.add(cov)

    session.flush()
    return {
        "inserted": inserted, "updated": updated, "skipped": skipped,
        "counties_derived": sum(1 for r in rows if r.county_source == "derived_from_city"),
        "counties_unresolved": sum(1 for r in rows if r.county_source == "unresolved"),
    }


def import_account_roster(session: Session, raw_text: str) -> dict:
    """The single entry point: validate the whole file, then apply it --
    or apply nothing at all. See parse_and_validate and apply_roster."""
    rows = parse_and_validate(raw_text, session)
    return apply_roster(session, rows)


def preview_account_roster(session: Session, raw_text: str) -> list[RosterPreviewRow]:
    """Validates the whole file (raises AccountRosterInvalid exactly like
    import_account_roster if anything is wrong -- a dry run still needs a
    clean file to preview against), then reports exactly what WOULD happen,
    writing nothing. See preview_roster."""
    rows = parse_and_validate(raw_text, session)
    return preview_roster(session, rows)
