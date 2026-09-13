"""Block 4B-prep-3 Item 5: whether a CSLB Contractor is already a DMG
customer account, and who holds it -- surfaced on the ReasonBlock "win"
evidence.

Reads from DmgCustomerRoster, a MINIMAL table (company_name,
netsuite_internal_id, category, assigned_rep) -- NOT the real NetSuite
customer master, and this module never loads that file itself.
~/netsuite-exports/ScoutResults653.csv is a local, unauthorized-for-import
export sitting on Andrew's own machine; loading it into Scout's database
is Andrew's call to make and load separately, when he authorizes it (a
real customer-relationship table is not something this session imports on
its own initiative). Until then DmgCustomerRoster is empty, and
contractor_dmg_customer_status() says exactly that -- "unknown, customer
master not loaded" -- rather than silently reporting every contractor as
"no."

EXACT MATCH ONLY, never fuzzy -- same discipline as
app.pipeline.contact_contractor_match's own Contact-to-Contractor join: a
wrong fuzzy match here would misattribute a real DMG sales relationship
(and a real rep's name) to the wrong CSLB-licensed firm, worse than
reporting "no" when the true answer is "yes" under a slightly different
name. Checked against BOTH Contractor.business_name and
full_business_name, same reason app.pipeline.contact_contractor_match
checks both: a CSLB business_name is sometimes a "LAST FIRST MIDDLE"
sole-proprietor ordering with full_business_name carrying the natural-
order form.
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.models import Contractor, DmgCustomerRoster
from app.normalize import normalize_company_name


def roster_loaded(session: Session) -> bool:
    """False until Andrew loads the real customer master -- the one fact
    contractor_dmg_customer_status needs to distinguish "not a customer"
    from "we don't know yet, nothing has been loaded."""
    return session.exec(select(DmgCustomerRoster.id).limit(1)).first() is not None


def contractor_dmg_customer_status(session: Session, contractor: Contractor | None) -> tuple[bool | None, str | None]:
    """(is_dmg_customer, assigned_rep). is_dmg_customer is None -- not
    False -- whenever the roster is empty (customer master not loaded) or
    no contractor was identified at all; never guessed into a "no" that
    the loaded data might contradict.

    A normalized name matching more than one roster row (the real master
    file has duplicates -- see DmgCustomerRoster's own docstring, e.g. two
    distinct rows both named "GLM Heating and Air Conditioning Inc.")
    still reports is_dmg_customer=True (the company IS a customer under
    either row) and returns the first non-null assigned_rep found among
    the matches, rather than refusing to answer -- unlike the Contact-to-
    Contractor identity join (Block 4B-prep-2 Item 1), getting the
    specific rep wrong here costs a wrong name in one evidence sentence,
    not a wrongly-attached foreign key, so this doesn't need that join's
    same all-or-nothing ambiguity refusal."""
    if not roster_loaded(session):
        return None, None
    if contractor is None:
        return None, None

    candidate_names = {normalize_company_name(n) for n in (contractor.business_name, contractor.full_business_name) if n}
    if not candidate_names:
        return None, None

    matches = session.exec(
        select(DmgCustomerRoster).where(DmgCustomerRoster.name_norm.in_(candidate_names))
    ).all()
    if not matches:
        return False, None

    assigned_rep = next((m.assigned_rep for m in matches if m.assigned_rep), None)
    return True, assigned_rep


def dmg_customer_status_evidence(session: Session, contractor: Contractor | None) -> str:
    """The exact sentence Block 4B-prep-3 Item 5 asks for, ready to append
    to a ReasonBlock's "win" evidence: "renders 'unknown, customer master
    not loaded'" until Andrew loads the real roster."""
    if contractor is None:
        return "Contractor DMG-customer status: unknown, no contractor identified for this contact."
    is_customer, assigned_rep = contractor_dmg_customer_status(session, contractor)
    if is_customer is None:
        return "Contractor DMG-customer status: unknown, customer master not loaded."
    if is_customer:
        if assigned_rep:
            return f"Contractor DMG-customer status: yes, assigned rep {assigned_rep}."
        return "Contractor DMG-customer status: yes, no assigned rep on file."
    return "Contractor DMG-customer status: no."
