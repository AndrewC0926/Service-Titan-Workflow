"""Board audit (Step 7): field accuracy is not ranking correctness.

For the top-N projects by score: one line each on WHY it scored there, plus
flags for the failure modes where every field is right but the rank is wrong:
  SINGLE_SIGNAL     — high score on one uncorroborated signal type
  SQFT_BASIS        — tonnage from square footage ranked high (low confidence)
  AGGRESSIVE_STAGE  — stage beyond entitlement on a single signal type
  MARKED_FP         — I already marked this row a false positive
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES, FalsePositiveMark, Project, Stage, utcnow,
)
from app.pipeline.size_score import project_signals


def audit_board(session: Session, cfg: Config, top: int = 25) -> list[dict]:
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == True)  # noqa: E712
        .order_by(Project.score.desc()).limit(top)).all()
    fp_ids = {m.project_id for m in session.exec(select(FalsePositiveMark)).all()}
    late_stages = {Stage.design, Stage.permitting, Stage.procurement, Stage.construction}

    rows = []
    for rank, p in enumerate(projects, 1):
        signals = project_signals(session, p.id)
        types = sorted({s.signal_type.value for s in signals})
        days_quiet = (utcnow() - p.last_signal_at).days if p.last_signal_at else None
        flags = []
        if len(types) <= 1:
            flags.append("SINGLE_SIGNAL")
        if p.estimate_low_confidence:
            flags.append("SQFT_BASIS")
        if p.stage in late_stages and len(types) <= 1:
            flags.append("AGGRESSIVE_STAGE")
        if p.id in fp_ids:
            flags.append("MARKED_FP")
        why = (f"{p.window.value} x{cfg.get('scoring.window_multipliers', {}).get(p.window.value)}"
               f" · {len(types)} signal type(s): {'+'.join(types) or 'none'}"
               f" · size: {p.estimate_basis or 'unknown'}"
               f" · last signal {days_quiet if days_quiet is not None else '?'}d ago")
        rows.append({"rank": rank, "id": p.id, "name": p.name, "score": p.score,
                     "why": why, "flags": flags,
                     "defensible": not flags})
    return rows


def audit_text(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        flag_str = " ".join(f"[{f}]" for f in r["flags"])
        mark = "OK " if r["defensible"] else "?? "
        lines.append(f"{mark}#{r['rank']:>2d} {r['name'][:38]:38s} {r['score']:5.2f}  {r['why']}")
        if flag_str:
            lines.append(f"       {flag_str}")
    flagged = sum(1 for r in rows if not r["defensible"])
    lines.append("")
    lines.append(f"{flagged} of {len(rows)} top rows flagged — review before quoting the board.")
    return "\n".join(lines)
