"""Firm profile and firm index: what a firm works on, where, with whom, and my
history with them.

Four questions a rep asks before picking up the phone, and the fourth is the one
no public dataset can answer — it comes from this system's own Outreach and
outcome records. Keeping all four on one page is the point: "Southland is on six
of my industrial rows, mostly Riverside, usually alongside HPA, and I last spoke
to them in March about Hemet" is a call. Any one of those facts alone is trivia.

Everything here is a query. No LLM, no inference, no synthesized "relationship
strength" score — a firm profile that guesses at relationships would be
inventing commercial intelligence, which is worse than having none. Every
number below is a count of rows that exist, traceable back to ProjectFirm,
Contact, or Outreach.

Why this file exists at all, not just app/firms.py's roster CRUD: Division 23
mechanical specs name a Basis of Design plus a short acceptable-manufacturers
list, and firms — not the project filings themselves — are who write those
lists. The firm view was previously the least developed page in the system
despite that; this closes the gap the roster/resolve machinery already had the
data for.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from app.models import (
    ACTIVE_STATUSES, Contact, Firm, OUTCOME_STATUSES, Outreach, Project, ProjectContact,
    ProjectFirm, utcnow,
)
from app.normalize import normalize_name
from app.staleness import DEFAULT_UNVERIFIED_MONTHS

# Four situations that look nothing alike and previously rendered identically:
# no name at this firm at all; a name with no phone/email yet (reach pending);
# a phone or email on file (Contact.reach_status == "confirmed") never yet
# called; and an actual logged conversation. Ordered least to most advanced.
CONTACT_STATE_ORDER = ["no_contact", "name_only", "reachable", "worked_with"]
CONTACT_STATE_LABELS = {
    "no_contact": "No contact",
    "name_only": "Name only — reach pending",
    "reachable": "Reachable",
    "worked_with": "Worked with",
}


def contact_state(contacts: list[Contact], has_outreach: bool) -> str:
    """The single most-advanced state for a firm, from what's actually on
    file. A firm can have several contacts in different states; this reports
    the best one, since "can I get in" is the practical question, not a
    census of every name found."""
    if has_outreach:
        return "worked_with"
    if not contacts:
        return "no_contact"
    if any(c.reach_status == "confirmed" for c in contacts):
        return "reachable"
    return "name_only"


def _is_stale(last_signal_at: datetime | None, now: datetime | None = None) -> bool:
    # Same discipline as app/staleness.py's stage age: no evidence date at all
    # is unverified too, never rendered the same as "fresh."
    if last_signal_at is None:
        return True
    now = now or utcnow()
    return (now - last_signal_at).days > DEFAULT_UNVERIFIED_MONTHS * 30.44


@dataclass
class FirmProfile:
    firm: Firm
    projects: list[Project] = field(default_factory=list)
    role_by_project: dict = field(default_factory=dict)
    roles: Counter = field(default_factory=Counter)
    counties: Counter = field(default_factory=Counter)
    categories: Counter = field(default_factory=Counter)
    windows: Counter = field(default_factory=Counter)
    # Firms appearing on the same projects, with how many they share.
    collaborators: list[tuple[Firm, int, str]] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    outreach: list[tuple[Outreach, Project]] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)
    tons_low: float = 0.0
    tons_high: float = 0.0
    # Sum of Project.equipment_value_low/high across this firm's projects.
    # Only projects that HAVE a value (tonnage + facility type both known)
    # contribute; n_valued says how many of n_projects that was, so a $0
    # total reads as "unpriced," never as "worth nothing."
    equipment_value_low: float = 0.0
    equipment_value_high: float = 0.0
    n_valued_projects: int = 0
    last_signal_at: datetime | None = None

    @property
    def n_projects(self) -> int:
        return len(self.projects)

    @property
    def last_touch(self):
        return self.outreach[0][0].date if self.outreach else None

    @property
    def days_since_touch(self) -> int | None:
        return (utcnow() - self.last_touch).days if self.last_touch else None

    @property
    def contact_state_key(self) -> str:
        return contact_state(self.contacts, bool(self.outreach))

    @property
    def is_stale(self) -> bool:
        return _is_stale(self.last_signal_at)

    @property
    def days_since_signal(self) -> int | None:
        return (utcnow() - self.last_signal_at).days if self.last_signal_at else None


def firm_profile(session: Session, firm_id: int) -> FirmProfile | None:
    firm = session.get(Firm, firm_id)
    if firm is None:
        return None

    links = session.exec(
        select(ProjectFirm).where(ProjectFirm.firm_id == firm_id)).all()
    project_ids = [l.project_id for l in links]
    role_by_project = {l.project_id: l.role for l in links}
    projects = (session.exec(select(Project).where(Project.id.in_(project_ids))).all()
                if project_ids else [])
    projects.sort(key=lambda p: p.score or 0, reverse=True)

    prof = FirmProfile(firm=firm, projects=projects, role_by_project=role_by_project)
    signal_dates = []
    for p in projects:
        prof.roles[role_by_project.get(p.id, "unknown")] += 1
        prof.counties[f"{p.county or '?'}, {p.state or '?'}"] += 1
        prof.categories[p.category.value] += 1
        prof.windows[p.window.value] += 1
        prof.tons_low += p.tons_estimate_low or 0
        prof.tons_high += p.tons_estimate_high or 0
        if p.equipment_value_low is not None and p.equipment_value_high is not None:
            prof.equipment_value_low += p.equipment_value_low
            prof.equipment_value_high += p.equipment_value_high
            prof.n_valued_projects += 1
        if p.last_signal_at is not None:
            signal_dates.append(p.last_signal_at)
        if p.status in OUTCOME_STATUSES:
            prof.outcomes[p.status] += 1
    prof.last_signal_at = max(signal_dates) if signal_dates else None

    # Who else shows up on these jobs. Counted across the firm's own projects, so
    # the number means "we have been on N jobs together", not "this firm is big".
    if project_ids:
        others: Counter = Counter()
        other_role: dict[int, str] = {}
        for pf in session.exec(
                select(ProjectFirm).where(ProjectFirm.project_id.in_(project_ids),
                                          ProjectFirm.firm_id != firm_id)).all():
            others[pf.firm_id] += 1
            other_role.setdefault(pf.firm_id, pf.role)
        if others:
            firms = {f.id: f for f in session.exec(
                select(Firm).where(Firm.id.in_(list(others)))).all()}
            prof.collaborators = [
                (firms[fid], n, other_role.get(fid, "unknown"))
                for fid, n in others.most_common() if fid in firms]

        # People we know at this firm, and every conversation on its projects.
        # Contacts are matched on the normalized company string rather than a
        # foreign key because Contact.company is free text typed by whoever
        # entered it; a join would silently show zero.
        norm = {firm.name_norm, *(normalize_name(a) for a in firm.aliases or [])}
        prof.contacts = [c for c in session.exec(select(Contact)).all()
                         if normalize_name(c.company or "") in norm]

        by_project = {p.id: p for p in projects}
        touches = session.exec(
            select(Outreach).where(Outreach.project_id.in_(project_ids))
            .order_by(Outreach.date.desc())).all()
        # Narrowed to conversations with THIS firm's people where a contact is
        # recorded. A call to the developer about a job Southland is also on is
        # not history with Southland, and counting it as such would overstate a
        # relationship — the one number here nobody should be able to fake.
        contact_ids = {c.id for c in prof.contacts}
        prof.outreach = [(o, by_project[o.project_id]) for o in touches
                         if o.project_id in by_project
                         and (o.contact_id in contact_ids if contact_ids else False)]
    return prof


def firm_index(session: Session) -> dict:
    """Every firm with at least one linked project, ranked by how much
    pipeline they've actually touched — not alphabetically. Also the
    portfolio-wide view a single firm's profile page can't show: the
    coverage gap by firm type (how few MEP/mech-contractor rows exist
    against the developer/consultant roster), and which firms keep
    co-occurring on the same jobs.
    """
    links = session.exec(select(ProjectFirm)).all()
    if not links:
        return {"rows": [], "total_linked": 0, "type_breakdown": [], "pairings": []}

    project_ids_by_firm: dict[int, set[int]] = {}
    for l in links:
        project_ids_by_firm.setdefault(l.firm_id, set()).add(l.project_id)

    firm_ids = list(project_ids_by_firm)
    firms = {f.id: f for f in session.exec(select(Firm).where(Firm.id.in_(firm_ids))).all()}

    all_project_ids = {pid for pids in project_ids_by_firm.values() for pid in pids}
    projects = {p.id: p for p in session.exec(
        select(Project).where(Project.id.in_(list(all_project_ids)))).all()}
    active_ids = {pid for pid, p in projects.items() if p.status in ACTIVE_STATUSES}

    all_contacts = session.exec(select(Contact)).all()
    contacts_by_company: dict[str, list[Contact]] = {}
    for c in all_contacts:
        contacts_by_company.setdefault(normalize_name(c.company or ""), []).append(c)

    all_outreach = session.exec(select(Outreach)).all()

    now = utcnow()
    rows = []
    for fid, pids in project_ids_by_firm.items():
        firm = firms.get(fid)
        if firm is None:  # a ProjectFirm row can't outlive its firm, but don't assume
            continue
        firm_projects = [projects[pid] for pid in pids if pid in projects]
        n_active = sum(1 for pid in pids if pid in active_ids)

        value_low = value_high = 0.0
        n_valued = 0
        for p in firm_projects:
            if p.equipment_value_low is not None and p.equipment_value_high is not None:
                value_low += p.equipment_value_low
                value_high += p.equipment_value_high
                n_valued += 1

        last_signal_at = max((p.last_signal_at for p in firm_projects if p.last_signal_at),
                             default=None)

        norms = {firm.name_norm} | {normalize_name(a) for a in firm.aliases or []}
        contacts = [c for norm in norms for c in contacts_by_company.get(norm, [])]
        contact_ids = {c.id for c in contacts}
        has_outreach = bool(contact_ids) and any(
            o.project_id in pids and o.contact_id in contact_ids for o in all_outreach)

        rows.append({
            "firm": firm,
            "n_projects": len(pids),
            "n_active": n_active,
            "value_low": value_low, "value_high": value_high, "n_valued": n_valued,
            "n_contacts": len(contacts),
            "contact_state": contact_state(contacts, has_outreach),
            "last_signal_at": last_signal_at,
            "stale": _is_stale(last_signal_at, now),
        })

    # Pipeline touched, not alphabetical -- the whole point of this rebuild.
    rows.sort(key=lambda r: (-r["n_projects"], r["firm"].name))

    # Coverage gap: how thin MEP/mech-contractor coverage is against the rest
    # of the linked roster. Pinned first regardless of count -- that's the
    # finding, and sorting purely by count would bury it under developer/
    # consultant rows that dwarf it in volume.
    type_counts = Counter(r["firm"].firm_type for r in rows)
    pinned = [t for t in ("mep", "mech_contractor") if t in type_counts]
    rest = sorted((t for t in type_counts if t not in pinned), key=lambda t: -type_counts[t])
    type_breakdown = [(t, type_counts[t]) for t in [*pinned, *rest]]

    # Collaborators, portfolio-wide: which firms keep landing on the same
    # projects. A firm linked under two roles on one project (measured: this
    # happens) must count as ONE co-occurrence, not pair with itself, so
    # dedupe to a firm-id set per project before pairing.
    firms_by_project: dict[int, set[int]] = {}
    for fid, pids in project_ids_by_firm.items():
        for pid in pids:
            firms_by_project.setdefault(pid, set()).add(fid)

    pair_counts: Counter = Counter()
    for fids in firms_by_project.values():
        ordered = sorted(fids)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pair_counts[(ordered[i], ordered[j])] += 1

    pairings = []
    for (a, b), n in pair_counts.items():
        if a not in firms or b not in firms:
            continue
        fa, fb = firms[a], firms[b]
        pairings.append({
            "a": fa, "b": fb, "n": n,
            "is_gc_mep": {fa.firm_type, fb.firm_type} == {"gc", "mep"},
        })
    # GC-to-MEP pairings surfaced first -- explicitly the pairing that matters
    # most (a GC a rep keeps landing with via a particular engineer) -- then
    # by shared-project count within each group.
    pairings.sort(key=lambda r: (not r["is_gc_mep"], -r["n"], r["a"].name, r["b"].name))

    return {
        "rows": rows,
        "total_linked": len(rows),
        "type_breakdown": type_breakdown,
        "pairings": pairings,
    }
