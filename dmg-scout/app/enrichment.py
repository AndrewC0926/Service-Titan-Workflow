"""Contact enrichment from paid data sources (Apollo.io, Lusha) — Phase 5.

The board names roughly thirty MEP firms, GCs and mechanical contractors with
no individual attached: real companies, no person to call. That is exactly
what a contact database is for, and unlike the rest of this pipeline it costs
real money per lookup, so this is deliberately NOT a source adapter that runs
on the nightly cron. It is a two-step, human-gated flow:

  1. `firms_needing_enrichment()` builds the worklist — same free query every
     time, no credits spent, safe to run as often as you like to see what
     would be worth enriching.
  2. A human (or an assistant acting on the human's explicit approval) runs
     the actual Apollo/Lusha lookups OUTSIDE this codebase — Scout has no
     Apollo/Lusha API keys of its own (see config.yaml: this intentionally
     has no `apollo:`/`lusha:` block, unlike every fetched source) — and
     feeds the results back in with `import_enriched_contact()` or
     `scout import-enriched-contacts <file.json>`.

Every contact written this way carries `source="apollo"` or `source="lusha"`
(see Contact.source in app/models.py) precisely so it never renders the same
as a name extracted from a public filing — see app/ladder.py, which surfaces
these on the board with that source visible, and RUNG_LABELS.
"""
from __future__ import annotations

from collections import defaultdict

from sqlmodel import select

from app.models import (
    ACTIVE_STATUSES, Contact, Firm, Project, ProjectContact, ProjectFirm, utcnow,
)
from app.normalize import normalize_name

# Which Firm.firm_type values are worth paying to enrich. Consultants
# (environmental/planning firms, economic development agencies) and bare
# "developer" rows dominate the firm roster by count but are not who the
# user meant by "MEP firms, GCs, mechanical contractors" -- an economic
# development agency does not have a Director of Preconstruction.
ENRICHABLE_FIRM_TYPES = ("mep", "gc", "mech_contractor")

# Firm role -> the Contact.company_type / ProjectContact.role this enrichment
# should carry, mirroring app/ladder.py's firm_rung mapping.
_ROLE_BY_FIRM_TYPE = {"mep": "engineer_of_record", "mech_contractor": "mech_contractor", "gc": "gc"}


def firms_needing_enrichment(session, limit: int | None = None) -> list[dict]:
    """Firms on active projects, typed MEP/GC/mech-contractor, with no
    existing Contact at that company (matched by normalized name) -- ranked
    by how many active projects they appear on, since a firm on three
    projects is worth more to enrich than one on a single filing."""
    rows = session.exec(
        select(ProjectFirm, Firm, Project)
        .where(ProjectFirm.firm_id == Firm.id, Project.id == ProjectFirm.project_id,
               Project.status.in_(ACTIVE_STATUSES))
    ).all()

    by_firm: dict[int, dict] = defaultdict(lambda: {"firm": None, "projects": set(), "roles": set()})
    for pf, firm, project in rows:
        d = by_firm[firm.id]
        d["firm"] = firm
        d["projects"].add((project.id, project.name))
        d["roles"].add(pf.role)

    existing = {
        normalize_name(c.company) for c in session.exec(select(Contact)).all() if c.company
    }

    worklist = []
    for d in by_firm.values():
        firm = d["firm"]
        if firm.firm_type not in ENRICHABLE_FIRM_TYPES:
            continue
        if firm.name_norm in existing:
            continue
        worklist.append({
            "firm_id": firm.id,
            "firm": firm.name,
            "firm_type": firm.firm_type,
            "n_projects": len(d["projects"]),
            "project_ids": sorted(pid for pid, _ in d["projects"]),
            "projects": sorted(name for _, name in d["projects"]),
        })
    worklist.sort(key=lambda w: -w["n_projects"])
    return worklist[:limit] if limit else worklist


def _write_contact(session, *, firm_name: str, name: str, source: str, reach_status: str,
                   title: str | None, phone: str | None, email: str | None,
                   project_ids: list[int] | None, firm_type: str | None) -> Contact:
    contact = Contact(name=name, title=title, company=firm_name, company_type=firm_type,
                      phone=phone, email=email, source=source, reach_status=reach_status)
    session.add(contact)
    session.flush()  # need contact.id for the links below

    role = _ROLE_BY_FIRM_TYPE.get(firm_type, "other")
    for project_id in project_ids or []:
        session.add(ProjectContact(project_id=project_id, contact_id=contact.id, role=role))

    return contact


def import_enriched_contact(session, *, firm_name: str, name: str, source: str,
                            title: str | None = None, phone: str | None = None,
                            email: str | None = None, project_ids: list[int] | None = None,
                            firm_type: str | None = None) -> Contact:
    """Write one CONFIRMED-reachable enriched Contact and link it to the
    given projects.

    `source` must be "apollo" or "lusha" -- this function is the only write
    path for those values (the manual dashboard form writes source="manual";
    extraction never writes to Contact at all, it writes named_people onto
    Signal, which is a different table -- see app/ladder.py's docstring).

    Refuses a contact with no phone and no email -- see import_pending_contact
    for the separate, explicit path for "we found the name, not the reach
    yet." This function's refusal is not relaxed by that path existing."""
    if source not in ("apollo", "lusha"):
        raise ValueError(f'source must be "apollo" or "lusha", got {source!r}')
    if not phone and not email:
        raise ValueError(f"refusing to store {name!r} at {firm_name!r} with no phone and no email — "
                         f"an enriched contact with no way to reach them is not worth the credit spent")

    return _write_contact(session, firm_name=firm_name, name=name, source=source,
                          reach_status="confirmed", title=title, phone=phone, email=email,
                          project_ids=project_ids, firm_type=firm_type)


def import_pending_contact(session, *, firm_name: str, name: str, source: str,
                           title: str | None = None, project_ids: list[int] | None = None,
                           firm_type: str | None = None) -> Contact:
    """Write a NAME-ONLY Contact from the free search layer -- no phone, no
    email, no reveal spent. This is a distinct, honest state ("we know who to
    ask for"), not a relaxed version of import_enriched_contact: a rep can
    call the firm's main line and ask for this person by name and title,
    which the ladder surfaces as reach tier 1 ("a name to research") rather
    than tier 2 ("no individual at all") -- see app/ladder.py's reach_tier.
    Takes no phone/email parameters at all, so a caller cannot accidentally
    downgrade a real number into a "pending" row."""
    if source not in ("apollo", "lusha"):
        raise ValueError(f'source must be "apollo" or "lusha", got {source!r}')

    return _write_contact(session, firm_name=firm_name, name=name, source=source,
                          reach_status="pending", title=title, phone=None, email=None,
                          project_ids=project_ids, firm_type=firm_type)
