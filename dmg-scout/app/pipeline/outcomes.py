"""Block 4A Item 2 (Master Plan v3.6 sections 15/31): the one Outcome
writer -- every caller that wants to log a disposition against an
Opportunity (Pipeline's one-tap buttons, Today's call cards once Item 5
re-sources them, the /capture voice workflow, the log_outreach MCP tool)
goes through log_outcome() here, same "exactly one write path" discipline
as app.outreach.log_outreach.

Config-driven lists: config.yaml's outcomes.dispositions/outcomes.
reason_codes give the human-readable label and display order for each
value -- see dispositions()/reason_codes() below, read by the web
templates and this module's own validation. The actual valid VALUE SET
stays a closed, migration-defined Postgres enum (app.models.Disposition/
LostReasonCode) -- "at most eight" is the plan's own literal constraint,
not something a config edit should be able to silently widen.
"""
from __future__ import annotations

from app.config import load_config
from app.models import Disposition, LostReasonCode, Opportunity, OpportunityStage, Outcome, OutcomeSource, utcnow

_DISPOSITION_TO_STAGE = {
    Disposition.won: OpportunityStage.won,
    Disposition.lost: OpportunityStage.lost,
}


def dispositions() -> list[dict]:
    """[{value, label}, ...] in display order, from config.yaml's
    outcomes.dispositions -- falls back to the enum's own values/titled
    labels if config.yaml has no entry, so a missing config section never
    breaks the page, only loses custom label text."""
    cfg = load_config()
    configured = cfg.get("outcomes.dispositions", [])
    if configured:
        return configured
    return [{"value": d.value, "label": d.value.replace("_", " ").title()} for d in Disposition]


def reason_codes() -> list[dict]:
    cfg = load_config()
    configured = cfg.get("outcomes.reason_codes", [])
    if configured:
        return configured
    return [{"value": r.value, "label": r.value.replace("_", " ").title()} for r in LostReasonCode]


def log_outcome(session, *, opportunity_id: int, user: str, disposition: Disposition | str,
                reason_code: LostReasonCode | str | None = None, competitor: str | None = None,
                note: str = "", source: OutcomeSource | str = OutcomeSource.web) -> Outcome:
    """Validates, writes the Outcome row, and syncs the Opportunity's own
    stage/last_touch -- the two are never allowed to drift apart (a
    Pipeline row showing "lost" with no matching Outcome row, or vice
    versa, would be exactly the kind of silent disagreement this
    codebase's own discipline elsewhere refuses to allow).

    disposition == lost requires reason_code (section 15: "Reason codes on
    lost, required dropdown, at most eight"); reason_code ==
    lost_to_competitor additionally requires competitor ("lost_to_
    competitor (competitor named)") -- both enforced here, in the one
    write path, not left to callers to remember."""
    disposition = Disposition(disposition)
    if reason_code is not None:
        reason_code = LostReasonCode(reason_code)
    source = OutcomeSource(source)

    if disposition == Disposition.lost and reason_code is None:
        raise ValueError("disposition 'lost' requires a reason_code (one of the eight configured lost reasons)")
    if reason_code == LostReasonCode.lost_to_competitor and not (competitor or "").strip():
        raise ValueError("reason_code 'lost_to_competitor' requires competitor to be named")

    opportunity = session.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"no Opportunity #{opportunity_id}")

    outcome = Outcome(opportunity_id=opportunity_id, user=user, disposition=disposition,
                      reason_code=reason_code, competitor=competitor or None, note=note, source=source)
    session.add(outcome)

    opportunity.last_touch = utcnow()
    new_stage = _DISPOSITION_TO_STAGE.get(disposition)
    if new_stage is not None:
        opportunity.stage = new_stage
    session.add(opportunity)

    session.flush()
    return outcome
