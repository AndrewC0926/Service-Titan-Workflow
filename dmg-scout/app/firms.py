"""Firm roster: seeding, alias-aware resolution, project linking.

Extracted `named_firms` match against the roster by normalized name (aliases
included) so "kW MCE" and "kW Mission Critical Engineering, Inc." land on one
row. Unmatched firms still get created — once, keyed on normalized name — and
are marked added_from='extraction' so the roster stays auditable.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.models import Firm, ProjectFirm
from app.normalize import normalize_name

log = logging.getLogger(__name__)

ROLE_TO_TYPE = {
    "engineer_of_record": "mep",
    "gc": "gc",
    "mech_contractor": "mech_contractor",
    "developer": "developer",
    "consultant": "consultant",
}


def seed_firms(session: Session, cfg: Config) -> int:
    added = 0
    for firm_type, entries in (cfg.get("roster") or {}).items():
        for entry in entries:
            name = entry["name"]
            norm = normalize_name(name)
            existing = session.exec(select(Firm).where(Firm.name_norm == norm)).first()
            if existing:
                # Keep roster typing/aliases authoritative over extraction guesses.
                merged = sorted(set(existing.aliases) | set(entry.get("aliases", [])))
                if existing.firm_type != firm_type or merged != sorted(existing.aliases):
                    existing.firm_type = firm_type
                    existing.aliases = merged
                    session.add(existing)
                continue
            session.add(Firm(name=name, name_norm=norm, firm_type=firm_type,
                             aliases=entry.get("aliases", []), added_from="roster"))
            added += 1
    session.commit()
    return added


def _alias_index(session: Session) -> dict[str, Firm]:
    index: dict[str, Firm] = {}
    for firm in session.exec(select(Firm)).all():
        index[firm.name_norm] = firm
        for alias in firm.aliases or []:
            index.setdefault(normalize_name(alias), firm)
    return index


def match_firm(session: Session, name: str) -> Firm | None:
    norm = normalize_name(name)
    if not norm:
        return None
    return _alias_index(session).get(norm)


def resolve_signal_firms(session: Session, project_id: int, named_firms: list[dict]) -> int:
    """Link a signal's named_firms to the project via the roster. Returns links made."""
    linked = 0
    index = _alias_index(session)
    for item in named_firms or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        role = item.get("role") or "unknown"
        norm = normalize_name(name)
        firm = index.get(norm)
        if firm is None:
            firm = Firm(name=name, name_norm=norm,
                        firm_type=ROLE_TO_TYPE.get(role, "unknown"),
                        added_from="extraction")
            session.add(firm)
            session.flush()
            index[norm] = firm
        exists = session.exec(
            select(ProjectFirm).where(ProjectFirm.project_id == project_id,
                                      ProjectFirm.firm_id == firm.id,
                                      ProjectFirm.role == role)).first()
        if not exists:
            session.add(ProjectFirm(project_id=project_id, firm_id=firm.id, role=role))
            linked += 1
    return linked
