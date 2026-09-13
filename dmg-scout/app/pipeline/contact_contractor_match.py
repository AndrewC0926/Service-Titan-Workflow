"""Block 4B-prep-2 Item 1: match Contact.customer_ref_name to the CSLB
Contractor roster by exact normalize_company_name, storing contractor_id
on Contact.

EXACT MATCH ONLY, never fuzzy -- same discipline as
app.importers.netsuite_contacts' own Account matching (Block 4B-prep Item
1): a wrong fuzzy match here silently attaches a real customer's contact
to the wrong CSLB-licensed firm, worse than leaving contractor_id null. A
normalized name that collides across 2+ DISTINCT Contractor rows is
treated as unmatchable and left null, never resolved by guessing which
one was meant.

MATCHES AGAINST BOTH business_name AND full_business_name, same as
app.pipeline.local250.match_local_250 -- a CSLB business_name is
sometimes a "LAST FIRST MIDDLE" sole-proprietor ordering with
full_business_name carrying the natural-order form (or vice versa); a
customer_ref_name from NetSuite could plausibly match either. If the two
fields on the SAME Contractor row normalize to different strings, both
keys point at that one row (not a collision); if two DIFFERENT Contractor
rows normalize to the same string via either field, that string is
unmatchable.
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.models import Contact, Contractor
from app.normalize import normalize_company_name


def _build_name_index(session: Session) -> dict[str, int | None]:
    """normalize_company_name(name) -> Contractor.id, or None when 2+
    distinct Contractor rows share that normalized name (ambiguous, never
    guessed)."""
    index: dict[str, int | None] = {}

    def add(norm: str, contractor_id: int) -> None:
        if norm not in index:
            index[norm] = contractor_id
        elif index[norm] != contractor_id:
            index[norm] = None  # ambiguous -- collides across distinct contractors

    for contractor_id, business_name, full_business_name in session.exec(
        select(Contractor.id, Contractor.business_name, Contractor.full_business_name)
    ):
        for name in (business_name, full_business_name):
            if name:
                add(normalize_company_name(name), contractor_id)
    return index


def match_contacts_to_contractors(session: Session) -> dict:
    """Idempotent: re-running re-evaluates every Contact with a
    customer_ref_name and sets/clears contractor_id to match the current
    Contractor roster and Contact data -- safe to re-run after either
    changes. Returns {"total_considered", "matched", "unmatched"}."""
    name_index = _build_name_index(session)

    total_considered = 0
    matched = 0
    unmatched = 0

    contacts = session.exec(select(Contact).where(Contact.customer_ref_name.is_not(None))).all()
    for contact in contacts:
        total_considered += 1
        norm = normalize_company_name(contact.customer_ref_name)
        contractor_id = name_index.get(norm)
        contact.contractor_id = contractor_id
        if contractor_id is not None:
            matched += 1
        else:
            unmatched += 1
        session.add(contact)

    session.commit()
    return {"total_considered": total_considered, "matched": matched, "unmatched": unmatched}
