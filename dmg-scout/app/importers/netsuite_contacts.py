"""Block 4B-prep Item 1: the NetSuite contacts export importer (checked
directly against the real 10,401-row file, ACContactsResults729.csv,
2026-09-13).

POSITIONAL COLUMNS, NOT csv.DictReader -- same discipline as
app.importers.netsuite_customers, for the same reason: the real header has
"Internal ID" twice:
  Internal ID, Internal ID, Name, Company, Job Title, Email, Phone,
  Mobile Phone, Inactive, Date Created
csv.DictReader keys by header text, so it would silently collapse the two
Internal ID columns into one (harmless here, both are identical on every
row -- but relying on that by accident is still the wrong habit) and is
refused categorically, matching the sibling importer's own rule.

THE NAME FIELD, measured directly, not assumed from the instruction that
prompted this module: "<id> <customer name>: <person>" or "<id> <customer>
- <person>" describes only 23 of 10,401 real rows (0.2%). The other 10,378
(99.8%) are plain person names with no customer text embedded at all --
confirmed directly, not a parsing bug: some rows with a LEADING DIGIT are
just company names that happen to start with one ("3 SHELDON MECHANICAL"),
and the " - " shape sometimes appears with no leading id at all ("Air-Ex -
Matthew Wilson"). _parse_name_field() only ever extracts a
customer_ref_id/customer_ref_name pair when the strict "^\\d+\\s" prefix
AND one of the two literal separators (": " or " - ") are both present --
never a looser heuristic that would misfire on the false positives above.

COMPANY MATCHING IS THE DOMINANT PATH, not the Name-field id: the CSV's
own Company column is populated on 96.3% of rows and is this importer's
real source for customer_ref_name on the vast majority of contacts (the
Name-field id is the fallback source, used only when Company is blank).
Account matching tries customer_ref_id -> Account.netsuite_entity_id
first (verified directly against the real NetSuite customer master --
see app.models.Account.netsuite_entity_id's own docstring for the
entity-id-not-internal-id finding this relies on), then falls back to an
EXACT (never fuzzy) normalize_company_name match against Account.
name_norm. A wrong fuzzy match here would silently attach a real person to
the wrong customer -- worse than leaving account_id null and
customer_ref_name raw for a later join once more of the customer master
loads.

Idempotent on netsuite_internal_id (unique): upserts, never duplicates a
contact across re-imports.
"""
from __future__ import annotations

import csv
import re
from io import StringIO

from sqlmodel import Session, select

from app.models import Account, Contact
from app.normalize import normalize_company_name

# "<id> <customer>: <person>" or "<id> <customer> - <person>" -- the
# LITERAL separators only (": " and " - ", with their own surrounding
# whitespace), never a bare ":" or "-" alone, which would misfire on
# company names that legitimately contain one ("Air-Tec Air Conditioning").
_LEADING_ID_RE = re.compile(r'^\s*(\d+)\s+(.*)$')
_SEPARATORS = (': ', ' - ')


def _parse_name_field(name: str) -> tuple[str | None, str | None, str]:
    """(customer_ref_id, customer_ref_name, person_name) -- the first two
    are None unless the strict leading-id + separator shape is present;
    person_name is always a real string (the substring after the
    separator when the shape matched, else the whole input unchanged)."""
    m = _LEADING_ID_RE.match(name)
    if not m:
        return None, None, name
    customer_id, rest = m.group(1), m.group(2)
    for sep in _SEPARATORS:
        if sep in rest:
            customer_name, _, person = rest.partition(sep)
            customer_name = customer_name.strip()
            person = person.strip()
            if customer_name and person:
                return customer_id, customer_name, person
    return None, None, name


def _split_first_last(person_name: str) -> tuple[str | None, str | None]:
    """Only where unambiguous: exactly two whitespace-separated tokens.
    A known, accepted approximation (not fixed up here): a two-word
    COMPANY name (e.g. "Alakai Mechanical") passes this same check and
    gets split as if it were first+last -- there is no way to tell the
    two apart from the string alone without guessing, and this module
    does not guess."""
    tokens = person_name.split()
    if len(tokens) == 2:
        return tokens[0], tokens[1]
    return None, None


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("yes", "true", "1")


def import_netsuite_contacts(session: Session, raw_text: str) -> dict:
    reader = csv.reader(StringIO(raw_text))
    header = next(reader)
    expected = ['Internal ID', 'Internal ID', 'Name', 'Company', 'Job Title',
               'Email', 'Phone', 'Mobile Phone', 'Inactive', 'Date Created']
    if header != expected:
        raise ValueError(f"unexpected header shape: {header!r} (expected {expected!r}) -- "
                        f"NetSuite may have reshaped this export; do not import unmapped")

    # Account lookup tables, built once -- name_norm collisions are real
    # elsewhere in this codebase (see app.importers.netsuite_customers'
    # own warning), so an exact-match dict keeps this importer from ever
    # picking a wrong Account silently: a name_norm with more than one
    # Account is treated as unmatchable, not resolved by guessing.
    accounts = session.exec(select(Account)).all()
    by_entity_id: dict[str, Account] = {}
    for a in accounts:
        if a.netsuite_entity_id:
            by_entity_id.setdefault(a.netsuite_entity_id, a)
    by_name_norm: dict[str, list[Account]] = {}
    for a in accounts:
        by_name_norm.setdefault(a.name_norm, []).append(a)

    existing_by_internal_id = {
        c.netsuite_internal_id: c for c in session.exec(select(Contact))
        if c.netsuite_internal_id is not None
    }

    total = 0
    reachable_count = 0
    matched_by_id = 0
    matched_by_company_name = 0
    unmatched = 0

    for row in reader:
        if not row:
            continue
        internal_id1, internal_id2, name, company, job_title, email, phone, mobile, inactive, date_created = row
        internal_id = int(internal_id1)
        name = name.strip()
        company = company.strip()

        customer_ref_id, name_customer, person_name = _parse_name_field(name)
        customer_ref_name = company or name_customer or None
        first_name, last_name = _split_first_last(person_name)

        account_id = None
        account = by_entity_id.get(customer_ref_id) if customer_ref_id else None
        if account is not None:
            account_id = account.id
            matched_by_id += 1
        elif customer_ref_name:
            candidates = by_name_norm.get(normalize_company_name(customer_ref_name), [])
            if len(candidates) == 1:
                account_id = candidates[0].id
                matched_by_company_name += 1
            else:
                unmatched += 1
        else:
            unmatched += 1

        reachable = bool(email.strip() or phone.strip() or mobile.strip())
        if reachable:
            reachable_count += 1

        existing = existing_by_internal_id.get(internal_id)
        contact = existing or Contact(netsuite_internal_id=internal_id)
        contact.name = name
        contact.first_name = first_name
        contact.last_name = last_name
        contact.title = job_title.strip() or None
        contact.company = customer_ref_name
        contact.email = email.strip() or None
        contact.phone = phone.strip() or None
        contact.mobile = mobile.strip() or None
        contact.is_active = not _parse_bool(inactive)
        contact.customer_ref_id = customer_ref_id
        contact.customer_ref_name = customer_ref_name
        contact.account_id = account_id
        contact.reachable = reachable
        contact.source = "netsuite"
        contact.reach_status = "confirmed"

        session.add(contact)
        total += 1

    session.commit()
    return {
        "rows": total,
        "reachable": reachable_count,
        "matched_by_id": matched_by_id,
        "matched_by_company_name": matched_by_company_name,
        "unmatched": unmatched,
    }
