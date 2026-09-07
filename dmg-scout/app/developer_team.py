"""A developer's usual design team -- see DeveloperDesignTeam's own docstring
in app/models.py for the full design. Two write paths:

  seed_from_project_firms -- a full, repeatable rollup of ProjectFirm +
  Project.developer into source='extracted' rows. Safe to re-run any time
  (a cron step, a one-off backfill); it deletes and rebuilds every
  source='extracted' row from scratch and never touches source='manual' ones.

  add_manual_team_entry -- a rep typing in what they know, from the
  developer page. Requires reason + confirmed_by, same discipline as
  app.pipeline.corrections.apply_manual_correction.

usual_team_for_developer is the one read path everything else (the project
page's "usual team for X" line, the developer page itself) calls.
"""
from __future__ import annotations

from collections import defaultdict

from sqlmodel import Session, delete, select

from app.firms import ROLE_TO_TYPE
from app.models import DeveloperDesignTeam, Firm, Project, ProjectFirm, utcnow
from app.normalize import normalize_name

# Scoped narrowly on purpose: civil/structural firms don't decide who
# specifies HVAC equipment, so they're extracted (see app/firms.py) but never
# rolled up into a "usual team" a rep would call about the mechanical spec.
DESIGN_TEAM_ROLES = ("architect", "mep_engineer", "engineer_of_record")

ROLE_LABELS = {
    "architect": "Architect",
    "mep_engineer": "MEP engineer",
    "engineer_of_record": "Engineer of record",
}


def developer_firm_role_rows(session: Session) -> list[tuple[str, str, str, int]]:
    """Every (developer, firm name, role, project_id) row live in ProjectFirm
    today, restricted to DESIGN_TEAM_ROLES and joined through Project.developer.
    One row per (project, firm, role) -- callers roll this up as needed."""
    return session.exec(
        select(Project.developer, Firm.name, ProjectFirm.role, ProjectFirm.project_id)
        .where(ProjectFirm.project_id == Project.id, ProjectFirm.firm_id == Firm.id,
               ProjectFirm.role.in_(DESIGN_TEAM_ROLES), Project.developer.is_not(None))
    ).all()


def seed_from_project_firms(session: Session) -> int:
    """Rebuild every source='extracted' DeveloperDesignTeam row from
    ProjectFirm + Project.developer. Fully repeatable -- deletes the old
    extracted rows and reinserts fresh ones, never touching source='manual'
    rows. Returns the number of (developer, firm, role) rows written."""
    rows = developer_firm_role_rows(session)

    grouped: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    display_name: dict[str, str] = {}
    for developer, firm_name, role, project_id in rows:
        norm = normalize_name(developer)
        if not norm:
            continue
        display_name.setdefault(norm, developer)
        grouped[(norm, firm_name, role)].append(project_id)

    firm_id_by_name = {f.name: f.id for f in session.exec(select(Firm)).all()}

    session.exec(delete(DeveloperDesignTeam).where(DeveloperDesignTeam.source == "extracted"))

    written = 0
    for (norm, firm_name, role), project_ids in grouped.items():
        firm_id = firm_id_by_name.get(firm_name)
        if firm_id is None:
            continue
        session.add(DeveloperDesignTeam(
            developer=display_name[norm], developer_norm=norm, firm_id=firm_id, role=role,
            evidence_project_ids=sorted(set(project_ids)), source="extracted",
        ))
        written += 1
    session.commit()
    return written


def usual_team_for_developer(session: Session, developer: str) -> list[dict]:
    """Every known design-team row for a developer (extracted + manual),
    largest evidence first. Empty for a developer with nothing recorded --
    callers must render that as "no team known," never guess."""
    norm = normalize_name(developer)
    if not norm:
        return []
    rows = session.exec(
        select(DeveloperDesignTeam, Firm)
        .where(DeveloperDesignTeam.developer_norm == norm, Firm.id == DeveloperDesignTeam.firm_id)
    ).all()
    out = [{
        "firm": firm.name, "firm_id": firm.id, "role": team.role,
        "role_label": ROLE_LABELS.get(team.role, team.role),
        "n": len(team.evidence_project_ids), "source": team.source,
        "confirmed_by": team.confirmed_by, "evidence_project_ids": team.evidence_project_ids,
    } for team, firm in rows]
    out.sort(key=lambda d: d["n"], reverse=True)
    return out


def add_manual_team_entry(session: Session, developer: str, firm_name: str, role: str,
                          reason: str, confirmed_by: str) -> DeveloperDesignTeam:
    """A rep's own knowledge, entered on the developer page. Requires a
    reason and a name, same discipline as apply_manual_correction -- there
    is no document behind this, so the audit trail is the only thing making
    it trustworthy. Upgrades an existing extracted row in place (same
    developer/firm/role) to source='manual' rather than duplicating it,
    since a human confirming what the rollup already found is worth more
    than a second row saying the same thing."""
    if role not in DESIGN_TEAM_ROLES:
        raise ValueError(f"role must be one of {DESIGN_TEAM_ROLES}, got {role!r}")
    if not reason.strip():
        raise ValueError("a reason is required")
    if not confirmed_by.strip():
        raise ValueError("your name is required")

    developer_norm = normalize_name(developer)
    if not developer_norm:
        raise ValueError("developer name is required")
    firm_norm = normalize_name(firm_name)
    if not firm_norm:
        raise ValueError("firm name is required")

    firm = session.exec(select(Firm).where(Firm.name_norm == firm_norm)).first()
    if firm is None:
        firm = Firm(name=firm_name, name_norm=firm_norm,
                    firm_type=ROLE_TO_TYPE.get(role, "unknown"), added_from="dashboard")
        session.add(firm)
        session.flush()

    existing = session.exec(
        select(DeveloperDesignTeam).where(
            DeveloperDesignTeam.developer_norm == developer_norm,
            DeveloperDesignTeam.firm_id == firm.id, DeveloperDesignTeam.role == role)
    ).first()
    if existing:
        existing.source = "manual"
        existing.reason = reason
        existing.confirmed_by = confirmed_by
        existing.updated_at = utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    row = DeveloperDesignTeam(developer=developer, developer_norm=developer_norm, firm_id=firm.id,
                              role=role, source="manual", reason=reason, confirmed_by=confirmed_by)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
