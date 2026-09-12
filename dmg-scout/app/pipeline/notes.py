"""Block 4A Item 3 (Master Plan v3.6 section 31): the one Decision Note
writer -- log_note() -- and the weekly "three deals, tell us why" list.

Config-driven lists, same discipline as app.pipeline.outcomes: config.yaml
gives label/display-order for note_type/pen_holder/basis_of_design/
lead_source; the valid VALUE SET stays the closed, migration-defined
Postgres enum.
"""
from __future__ import annotations

from app.config import load_config
from app.models import (
    BasisOfDesign, DecisionNote, LeadSource, NetsuiteRefType, NoteType, NotePenHolder, LostReasonCode,
    Opportunity, OpportunityStage, OutcomeSource,
)

def note_types() -> list[dict]:
    cfg = load_config()
    configured = cfg.get("notes.note_types", [])
    if configured:
        return configured
    return [{"value": t.value, "label": t.value.replace("_", " ").title()} for t in NoteType]


def pen_holders() -> list[dict]:
    cfg = load_config()
    configured = cfg.get("notes.pen_holders", [])
    if configured:
        return configured
    return [{"value": p.value, "label": p.value.upper() if p.value == "gc" else p.value.title()}
            for p in NotePenHolder]


def basis_of_design_options() -> list[dict]:
    cfg = load_config()
    configured = cfg.get("notes.basis_of_design", [])
    if configured:
        return configured
    return [{"value": b.value, "label": b.value.replace("_", " ").title()} for b in BasisOfDesign]


def lead_sources() -> list[dict]:
    cfg = load_config()
    configured = cfg.get("notes.lead_sources", [])
    if configured:
        return configured
    return [{"value": s.value, "label": s.value.replace("_", " ").title()} for s in LeadSource]


def log_note(session, *, note_type, lead_source, author: str, opportunity_id: int | None = None,
            project_id: int | None = None, building_id: int | None = None, account_id: int | None = None,
            signal_id: int | None = None, netsuite_ref_type=None, netsuite_ref: str | None = None,
            pen_holder=NotePenHolder.unknown, basis_of_design=BasisOfDesign.open, reason_code=None,
            line: str | None = None, competitor_line: str | None = None, dollars: float | None = None,
            free_text: str = "", role: str | None = None, source=OutcomeSource.web) -> DecisionNote:
    """"A note is the smallest unit of institutional memory... It attaches
    to a Scout Opportunity, Project, Building, Account or Signal, or to a
    typed NetSuite Opportunity, Project or Sales Order reference, so a
    deal that never touched Scout still counts." At least one anchor
    (5 Scout-side FKs, or netsuite_ref) is required -- same "no
    unresolved-entity concept" discipline as app.outreach.log_outreach."""
    note_type = NoteType(note_type)
    lead_source = LeadSource(lead_source)
    pen_holder = NotePenHolder(pen_holder)
    basis_of_design = BasisOfDesign(basis_of_design)
    if reason_code is not None:
        reason_code = LostReasonCode(reason_code)
    if netsuite_ref_type is not None:
        netsuite_ref_type = NetsuiteRefType(netsuite_ref_type)
    source = OutcomeSource(source)

    scout_anchors = dict(opportunity_id=opportunity_id, project_id=project_id, building_id=building_id,
                        account_id=account_id, signal_id=signal_id)
    if not any(scout_anchors.values()) and not netsuite_ref:
        raise ValueError("log_note requires at least one anchor: opportunity_id, project_id, "
                        "building_id, account_id, signal_id, or netsuite_ref")
    if netsuite_ref and netsuite_ref_type is None:
        raise ValueError("netsuite_ref requires netsuite_ref_type")

    note = DecisionNote(
        note_type=note_type, lead_source=lead_source, author=author, role=role, source=source,
        pen_holder=pen_holder, basis_of_design=basis_of_design, reason_code=reason_code,
        line=line or None, competitor_line=competitor_line or None, dollars=dollars,
        free_text=free_text, netsuite_ref_type=netsuite_ref_type, netsuite_ref=netsuite_ref or None,
        **scout_anchors,
    )
    session.add(note)
    session.flush()
    return note


def three_deals_to_explain(session, min_age_days: int = 14, limit: int = 3) -> list[Opportunity]:
    """Master Plan v3.6 section 31: "a weekly per-user list, 'three deals,
    tell us why,' generated from NetSuite closes once loaded and from
    Scout opportunities until then... Thirty seconds each."

    "For now" (the item's own words): Opportunity has no owner/assigned-
    rep field anywhere in Scout today, so this returns ONE shared
    candidate list, not a real per-user partition -- a true per-user split
    needs either an Opportunity.owner field or an attribution rule (e.g.
    the last Outcome.user to touch it), neither of which exists yet. Every
    open (not won/lost) Opportunity older than min_age_days, oldest first,
    capped at `limit`."""
    from datetime import timedelta

    from sqlmodel import select

    from app.models import utcnow

    cutoff = utcnow() - timedelta(days=min_age_days)
    return session.exec(
        select(Opportunity)
        .where(Opportunity.stage.not_in((OpportunityStage.won, OpportunityStage.lost)))
        .where(Opportunity.created_at <= cutoff)
        .order_by(Opportunity.created_at.asc())
        .limit(limit)
    ).all()
