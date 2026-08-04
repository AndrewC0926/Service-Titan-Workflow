"""SIZE + SCORE: recompute tonnage estimates and priority for every active project.
Pure recomputation from linked signals — safe to re-run any time."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.models import Project, ProjectSignal, Signal, Window, utcnow
from app.pipeline.scoring import classify_window, days_to_estimated_bid, priority_score
from app.pipeline.sizing import estimate_tons

log = logging.getLogger(__name__)


def project_signals(session: Session, project_id: int) -> list[Signal]:
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project_id)
    ).all()
    ids = [l.signal_id for l in links]
    if not ids:
        return []
    return list(session.exec(select(Signal).where(Signal.id.in_(ids))).all())


def run_size_score(session: Session, cfg: Config) -> dict:
    projects = session.exec(select(Project).where(Project.status == "active")).all()
    stats = {"sized": 0, "scored": 0}
    for project in projects:
        signals = project_signals(session, project.id)

        # SIZE — best inputs across all signals (max: filings grow as design firms up)
        def best(attr):
            vals = [getattr(s, attr) for s in signals if getattr(s, attr) is not None]
            return max(vals) if vals else None

        est = estimate_tons(
            cfg,
            mw_it=project.mw_it or best("mw_it"),
            mw_total=project.mw_total or best("mw_total"),
            generator_count=best("generator_count"),
            generator_hp_each=best("generator_hp_each"),
            generator_kw_each=best("generator_kw_each"),
            building_sqft=best("building_sqft"),
        )
        project.tons_estimate_low, project.tons_estimate_high = est.low, est.high
        project.estimate_basis = est.basis
        if est.low is not None:
            stats["sized"] += 1

        # SCORE
        filing_types = [s.filing_type for s in signals if s.filing_type]
        window = classify_window(project.stage, filing_types)
        project.window = window
        types = [s.signal_type for s in signals]
        project.score = priority_score(
            cfg, types, window, est.midpoint, project.last_signal_at
        )
        project.days_to_estimated_bid = days_to_estimated_bid(cfg, project.stage)
        project.updated_at = utcnow()
        session.add(project)
        stats["scored"] += 1
    session.commit()
    return stats
