"""SIZE + SCORE: recompute tonnage estimates and priority for every active project.
Pure recomputation from linked signals — safe to re-run any time."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES, Category, FacilityType, Project, ProjectSignal, Signal, Window, utcnow,
)
from app.normalize import normalize_county
from app.pipeline.scoring import (
    classify_window, days_to_estimated_bid, identity_factor, priority_score,
)
from app.pipeline.sizing import estimate_equipment_value, estimate_tons

log = logging.getLogger(__name__)


def in_territory(cfg: Config, state: str | None, county: str | None) -> bool:
    """Hard geography boundary. Unknown location stays on the board (in-territory)
    rather than silently vanishing to the watch list."""
    territory = cfg.get("territory") or {}
    if not territory:
        return True
    if not state and not county:
        return True
    county_n = normalize_county(county)
    if state:
        counties = territory.get(state.strip().upper())
        if counties is None:
            return False
        return county_n is None or county_n in [normalize_county(c) for c in counties]
    return any(county_n in [normalize_county(c) for c in counties]
               for counties in territory.values())


def project_signals(session: Session, project_id: int) -> list[Signal]:
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project_id)
    ).all()
    ids = [l.signal_id for l in links]
    if not ids:
        return []
    return list(session.exec(select(Signal).where(Signal.id.in_(ids))).all())


def _facility_type(signals: list[Signal]) -> FacilityType:
    """The most specific facility type any linked signal states.

    A packet that mentions a building in passing yields `unknown`; the abatement
    application for the same project states it outright. Taking the first stated
    value means one good filing settles the type for the project, rather than a
    vague later signal dragging it back to the full 50-2500 band.
    """
    for s in signals:
        if s.facility_type is not FacilityType.unknown:
            return s.facility_type
    return FacilityType.unknown


def run_size_score(session: Session, cfg: Config) -> dict:
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    stats = {"sized": 0, "scored": 0}
    for project in projects:
        signals = project_signals(session, project.id)

        # SIZE — best inputs across all signals (max: filings grow as design firms up)
        def best(attr):
            vals = [getattr(s, attr) for s in signals if getattr(s, attr) is not None]
            return max(vals) if vals else None

        facility_type = _facility_type(signals)
        est = estimate_tons(
            cfg,
            mw_it=project.mw_it or best("mw_it"),
            mw_total=project.mw_total or best("mw_total"),
            generator_count=best("generator_count"),
            generator_hp_each=best("generator_hp_each"),
            generator_kw_each=best("generator_kw_each"),
            generator_critical_count=best("generator_critical_count"),
            generator_critical_mw_each=best("generator_critical_mw_each"),
            generator_house_count=best("generator_house_count"),
            generator_house_mw_each=best("generator_house_mw_each"),
            building_sqft=best("building_sqft"),
            category=project.category,
            facility_type=facility_type,
        )
        # Data centers report facility_type=unknown on most filings (the category
        # already says what the building is), so value them as data centers rather
        # than dropping every one for want of a redundant field.
        value_type = facility_type
        if value_type is FacilityType.unknown and project.category is Category.data_center:
            value_type = FacilityType.data_center
        val = estimate_equipment_value(cfg, est.low, est.high, value_type)
        project.equipment_value_low, project.equipment_value_high = val.low, val.high
        project.equipment_value_basis = val.basis
        if est.rejected_inputs:
            log.warning("project %s (%s): discarded implausible size input(s): %s",
                        project.id, project.name, "; ".join(est.rejected_inputs))
        project.tons_estimate_low, project.tons_estimate_high = est.low, est.high
        project.estimate_basis = est.basis
        project.estimate_low_confidence = est.low_confidence
        if est.low is not None:
            stats["sized"] += 1

        # SCORE
        filing_types = [s.filing_type for s in signals if s.filing_type]
        window = classify_window(project.stage, filing_types)
        project.window = window
        types = [s.signal_type for s in signals]
        base_score = priority_score(
            cfg, types, window, est.midpoint, project.last_signal_at
        )
        project.score = round(
            base_score * identity_factor(cfg, project.name, project.developer, project.county), 4
        )
        project.days_to_estimated_bid = days_to_estimated_bid(cfg, project.stage)
        project.in_territory = in_territory(cfg, project.state, project.county)
        project.updated_at = utcnow()
        session.add(project)
        stats["scored"] += 1
    session.commit()
    return stats
