"""Firm profile: what a firm works on, where, with whom, and my history with them.

Four questions a rep asks before picking up the phone, and the fourth is the one
no public dataset can answer — it comes from this system's own Outreach and
outcome records. Keeping all four on one page is the point: "Southland is on six
of my industrial rows, mostly Riverside, usually alongside HPA, and I last spoke
to them in March about Hemet" is a call. Any one of those facts alone is trivia.

Everything here is a query. No LLM, no inference — a firm profile that guesses at
relationships would be inventing commercial intelligence, which is worse than
having none.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import (
    ACTIVE_STATUSES, Contact, Firm, OUTCOME_STATUSES, Outreach, Project, ProjectContact,
    ProjectFirm, utcnow,
)
from app.normalize import normalize_name


@dataclass
class FirmProfile:
    firm: Firm
    projects: list[Project] = field(default_factory=list)
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

    @property
    def n_projects(self) -> int:
        return len(self.projects)

    @property
    def last_touch(self):
        return self.outreach[0][0].date if self.outreach else None

    @property
    def days_since_touch(self) -> int | None:
        return (utcnow() - self.last_touch).days if self.last_touch else None


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

    prof = FirmProfile(firm=firm, projects=projects)
    for p in projects:
        prof.roles[role_by_project.get(p.id, "unknown")] += 1
        prof.counties[f"{p.county or '?'}, {p.state or '?'}"] += 1
        prof.categories[p.category.value] += 1
        prof.windows[p.window.value] += 1
        prof.tons_low += p.tons_estimate_low or 0
        prof.tons_high += p.tons_estimate_high or 0
        if p.status in OUTCOME_STATUSES:
            prof.outcomes[p.status] += 1

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


def firm_index(session: Session) -> list[dict]:
    """Every firm with at least one linked project, for the profile index."""
    counts: Counter = Counter()
    for pf in session.exec(select(ProjectFirm)).all():
        counts[pf.firm_id] += 1
    if not counts:
        return []
    firms = {f.id: f for f in session.exec(
        select(Firm).where(Firm.id.in_(list(counts)))).all()}
    active = {p.id for p in session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()}
    live: Counter = Counter()
    for pf in session.exec(select(ProjectFirm)).all():
        if pf.project_id in active:
            live[pf.firm_id] += 1
    rows = [{"firm": firms[fid], "n_projects": n, "n_active": live.get(fid, 0)}
            for fid, n in counts.items() if fid in firms]
    rows.sort(key=lambda r: (-r["n_active"], -r["n_projects"], r["firm"].name))
    return rows
