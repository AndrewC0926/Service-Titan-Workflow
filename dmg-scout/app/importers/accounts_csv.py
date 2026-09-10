"""CSV import for accounts: column mapping + entity normalization, built ON TOP
of app.accounts (create_account, ensure_coverage_rows) rather than duplicating
it — the account module works completely without this file. This exists only
because ~100 dormant accounts is too many to type in by hand from a
spreadsheet, not because the module needs it.

Two-step, stateless flow (no server-side session): upload -> preview (parses
the CSV, guesses a column mapping, flags exact and near-duplicate matches
against existing accounts) -> commit (re-parses the SAME csv text, now posted
back as a hidden field alongside the confirmed mapping). Nothing is written to
the database until commit.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.accounts import ACCOUNT_TYPES, create_account, ensure_coverage_rows
from app.models import Account
from app.normalize import normalize_name

# (field key, label, required)
ACCOUNT_FIELDS: list[tuple[str, str, bool]] = [
    ("name", "Account name", True),
    ("account_type", "Type", False),
    ("address", "Address", False),
    ("city", "City", False),
    ("county", "County", False),
    ("state", "State", False),
    ("assigned_rep", "Assigned rep", False),
    ("first_order_date", "First order date", False),
    ("last_order_date", "Last order date", False),
    ("notes", "Notes", False),
]

# Header text (lowercased, substring match) -> field key. First match wins, so
# more specific keywords are listed before generic ones.
_GUESS_KEYWORDS: list[tuple[str, str]] = [
    ("account name", "name"), ("company name", "name"), ("customer", "name"),
    ("account", "name"), ("company", "name"), ("name", "name"),
    ("type", "account_type"),
    ("address", "address"), ("street", "address"),
    ("city", "city"),
    ("county", "county"),
    ("state", "state"),
    ("rep", "assigned_rep"), ("sales rep", "assigned_rep"), ("owner", "assigned_rep"),
    ("first order", "first_order_date"), ("first sale", "first_order_date"),
    ("last order", "last_order_date"), ("last sale", "last_order_date"),
    ("note", "notes"), ("comment", "notes"),
]

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")

# Below this token-sort-ratio, two names are unrelated and not worth flagging.
# Above uq_account_norm's exact match, they are the same row. Between the two
# is "close enough to check by hand before assuming it's a new account."
FUZZY_FLAG_THRESHOLD = 0.85


def guess_mapping(headers: list[str]) -> dict[str, str]:
    """Best-effort header -> field key, for pre-filling the mapping form.
    Never auto-commits off this — it only saves clicks on the preview step."""
    mapping: dict[str, str] = {}
    used_headers: set[str] = set()
    for header in headers:
        low = header.strip().lower()
        for keyword, field_key in _GUESS_KEYWORDS:
            if field_key in mapping.values():
                continue
            if keyword in low and header not in used_headers:
                mapping[header] = field_key
                used_headers.add(header)
                break
    return mapping


def parse_csv(raw_text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(raw_text))
    headers = reader.fieldnames or []
    rows = [dict(r) for r in reader]
    return headers, rows


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _mapped_row(raw_row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    """CSV row -> {field_key: raw string value}, header -> field per `mapping`."""
    out: dict[str, str] = {}
    for header, field_key in mapping.items():
        val = (raw_row.get(header) or "").strip()
        if val:
            out[field_key] = val
    if "account_type" in out:
        # A spreadsheet says "Mechanical Contractor" or "GC", not the DB's
        # snake_case — match loosely, but an unrecognized value is dropped
        # (leaves account_type unset for this row) rather than guessed as
        # "mechanical_contractor" -- that former fallback is exactly the
        # invented-precision this system's own register exists to catch
        # (removed 2026-09-11 alongside Account.account_type becoming
        # nullable; see app.importers.netsuite_customers's own docstring).
        norm = out["account_type"].strip().lower().replace(" ", "_").replace("-", "_")
        if norm in ACCOUNT_TYPES:
            out["account_type"] = norm
        else:
            del out["account_type"]
    return out


@dataclass
class PreviewRow:
    raw: dict[str, str]
    fields: dict[str, str]
    error: str | None = None
    exact_match: Account | None = None
    # (label, score) — label names either an existing account or another row
    # in this same file. A duplicate two rows apart in a hand-built spreadsheet
    # of ~100 accounts is exactly as real a collision as one already in the
    # database, and only comparing against the database missed it entirely.
    fuzzy_candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def action(self) -> str:
        if self.error:
            return "skip"
        if self.exact_match:
            return "update"
        return "create"


def _fuzzy_matches(norm: str, pool: list[tuple[str, str]]) -> list[tuple[str, float]]:
    hits = []
    for label, other_norm in pool:
        score = fuzz.token_sort_ratio(norm, other_norm) / 100.0
        if score >= FUZZY_FLAG_THRESHOLD:
            hits.append((label, score))
    hits.sort(key=lambda t: -t[1])
    return hits[:3]


def preview_import(session: Session, headers: list[str], rows: list[dict[str, str]],
                   mapping: dict[str, str]) -> list[PreviewRow]:
    """One PreviewRow per CSV row: what it will do (create/update/skip) and any
    near-duplicate to check by hand before trusting that — against the
    database AND against earlier rows in this same file. Read-only — nothing
    is written here."""
    existing = session.exec(select(Account).where(Account.status == "active")).all()
    by_norm = {a.name_norm: a for a in existing}
    pool: list[tuple[str, str]] = [(a.name, a.name_norm) for a in existing]

    out: list[PreviewRow] = []
    for i, raw_row in enumerate(rows):
        fields_ = _mapped_row(raw_row, mapping)
        name = fields_.get("name", "")
        if not name:
            out.append(PreviewRow(raw=raw_row, fields=fields_, error="no account name"))
            continue
        norm = normalize_name(name)
        exact = by_norm.get(norm)
        fuzzy = [] if exact else _fuzzy_matches(norm, pool)
        out.append(PreviewRow(raw=raw_row, fields=fields_, exact_match=exact,
                              fuzzy_candidates=fuzzy))
        pool.append((f'"{name}" (row {i + 2} of this file)', norm))  # +2: header + 1-indexed
    return out


def commit_import(session: Session, headers: list[str], rows: list[dict[str, str]],
                  mapping: dict[str, str]) -> dict:
    """Apply every row: exact-name match updates the existing account (only
    non-empty CSV cells overwrite — a blank cell never erases a value entered
    by hand since), no match creates one via the same create_account() the
    manual "+ New account" form uses. Near-fuzzy-duplicates are NOT merged
    automatically — they import as new accounts, flagged in the result, for a
    human to check; guessing two spreadsheet rows are the same company is
    exactly the kind of silent merge this system avoids everywhere else.
    """
    existing = session.exec(select(Account).where(Account.status == "active")).all()
    by_norm = {a.name_norm: a for a in existing}
    # Grows as rows commit, same as preview_import's pool — a fuzzy check
    # against only the pre-import database would miss two near-duplicate rows
    # in the same file, since neither exists yet when the batch starts.
    pool: list[tuple[str, str]] = [(a.name, a.name_norm) for a in existing]

    created, updated, skipped, flagged = 0, 0, 0, []
    for raw_row in rows:
        fields_ = _mapped_row(raw_row, mapping)
        name = fields_.get("name", "")
        if not name:
            skipped += 1
            continue
        norm = normalize_name(name)
        account = by_norm.get(norm)
        date_fields = {"first_order_date": _parse_date(fields_.get("first_order_date", "")),
                       "last_order_date": _parse_date(fields_.get("last_order_date", ""))}
        if account:
            for key in ("account_type", "address", "city", "county", "state",
                       "assigned_rep", "notes"):
                if fields_.get(key):
                    setattr(account, key, fields_[key])
            for key, val in date_fields.items():
                if val is not None:
                    setattr(account, key, val)
            session.add(account)
            session.commit()
            ensure_coverage_rows(session, account)
            updated += 1
        else:
            fuzzy = _fuzzy_matches(norm, pool)
            account = create_account(
                # No fallback: an unmapped/blank type stays NULL, never
                # guessed as "mechanical_contractor" -- see _mapped_row.
                session, name=name, account_type=fields_.get("account_type"),
                address=fields_.get("address"), city=fields_.get("city"),
                county=fields_.get("county"), state=fields_.get("state"),
                assigned_rep=fields_.get("assigned_rep"), notes=fields_.get("notes", ""),
                **date_fields,
            )
            by_norm[norm] = account
            created += 1
            for label, score in fuzzy:
                flagged.append({"new": account.name, "similar_to": label, "score": round(score, 2)})
        pool.append((name, norm))
    return {"created": created, "updated": updated, "skipped": skipped, "flagged": flagged}
