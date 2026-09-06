"""SIZE + SCORE: recompute tonnage estimates and priority for every active project.
Pure recomputation from linked signals — safe to re-run any time."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES, Category, FacilityType, Project, ProjectSignal, Signal, SignalType, Window,
    utcnow,
)
from app.normalize import normalize_county
from app.pipeline.scoring import (
    certainty_detail, classify_window, days_to_estimated_bid, days_to_estimated_bid_range,
    identity_factor, priority_score, recency_decay, size_factor,
)
from app.pipeline.sizing import TonsEstimate, estimate_equipment_value, estimate_tons
from app.pipeline.spillover import county_spillover_mw, project_spillover, spillover_factor

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


def run_size_score(session: Session, cfg: Config, only_project_ids: list[int] | None = None) -> dict:
    """Pure recomputation from linked signals -- safe to re-run any time.

    `only_project_ids` scopes the pass to specific projects (still filtered
    to ACTIVE_STATUSES) instead of every active project on the board -- for
    a caller that just wrote facts onto one or a few rows outside the
    nightly pipeline (a review-queue decision, a project merge, an
    enrichment pass) and needs those rows' score/window correct in the same
    transaction, without paying for a full-board pass. Spillover still reads
    the same whole-board county totals either way (its inputs aren't scoped
    by this), so a scoped call costs one query more than a full one, not
    less work per project.
    """
    query = select(Project).where(Project.status.in_(ACTIVE_STATUSES))
    if only_project_ids is not None:
        query = query.where(Project.id.in_(only_project_ids))
    projects = session.exec(query).all()
    stats = {"sized": 0, "scored": 0}
    # Computed ONCE per run, not per project: every project's spillover input
    # reads the same county totals, so this stays a single pass over the
    # board + IEPR layer rather than one per row. See app/pipeline/spillover.py.
    spillover_enabled = cfg.get("scoring.spillover.enabled", False)
    spillover_now = utcnow()
    county_totals = county_spillover_mw(session, cfg, spillover_now) if spillover_enabled else {}
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
        score = base_score * identity_factor(cfg, project.name, project.developer, project.county)

        if spillover_enabled:
            mw, basis = project_spillover(cfg, county_totals, project, spillover_now)
            project.spillover_mw = mw or None
            project.spillover_basis = basis
            score *= spillover_factor(cfg, mw)
        else:
            project.spillover_mw = None
            project.spillover_basis = None

        project.score = round(score, 4)
        project.days_to_estimated_bid = days_to_estimated_bid(cfg, project.stage)
        project.days_to_estimated_bid_low, project.days_to_estimated_bid_high = (
            days_to_estimated_bid_range(cfg, project.stage))
        project.in_territory = in_territory(cfg, project.state, project.county)
        project.updated_at = utcnow()
        session.add(project)
        stats["scored"] += 1
    session.commit()
    return stats


@dataclass
class ScoreTerm:
    label: str
    value: float
    detail: str


@dataclass
class ScoreBreakdown:
    """Every multiplicative term that went into a project's score, in the
    order they're applied, plus the running product after each one. Exists
    so a rep can see WHY 1.42 is 1.42 instead of trusting it — the same
    "show your work" discipline app/pipeline/sizing.py already applies to
    tonnage estimates (every project carries an estimate_basis string),
    applied to the score itself."""
    terms: list[ScoreTerm] = field(default_factory=list)
    total: float = 0.0

    def as_lines(self) -> list[str]:
        lines = [f"{t.label}: {t.detail}" for t in self.terms]
        lines.append(f"= {self.total:.4f}")
        return lines

    def as_text(self) -> str:
        return "\n".join(self.as_lines())


def score_breakdown(cfg: Config, *, signal_types: list[SignalType], window: Window,
                    tons_midpoint: float | None, last_signal_at: datetime | None,
                    name: str | None, developer: str | None, county: str | None,
                    spillover_mw: float | None = None, spillover_enabled: bool = False,
                    now: datetime | None = None) -> ScoreBreakdown:
    """Recomputes the exact chain run_size_score() applies to a project
    (certainty x window x size x recency x identity [x spillover]), term by
    term, from inputs a caller already has on hand (or can fetch once and
    reuse across many projects — see app.web.main's board route, which
    batches signal types for the whole board rather than querying per row).

    Recomputed, not read back from a stored field: nothing on Project
    persists certainty/size_factor/recency_decay/identity_factor
    individually, only their product (Project.score). Recomputing from the
    same stored inputs (tons_estimate_low/high, last_signal_at, name,
    developer, county, spillover_mw) that fed the last run_size_score() pass
    reconstructs the same number; it can drift from Project.score only if
    config changed or the pipeline hasn't rerun since — the same staleness
    every other derived field in this system already tolerates.
    """
    now = now or utcnow()
    terms: list[ScoreTerm] = []
    running = 1.0

    cert, cert_detail = certainty_detail(cfg, signal_types)
    terms.append(ScoreTerm("Certainty", cert, cert_detail))
    running *= cert

    mult = cfg.get("scoring.window_multipliers", {}).get(window.value, 1.0)
    terms.append(ScoreTerm(
        "Window", mult, f"{window.value.replace('_', '-')} -> ×{mult:.2f}"))
    running *= mult

    sf = size_factor(cfg, tons_midpoint)
    sf_detail = (f"{tons_midpoint:,.0f} tons (midpoint) -> ×{sf:.2f}" if tons_midpoint
                else f"size unknown -> default ×{sf:.2f}")
    terms.append(ScoreTerm("Size", sf, sf_detail))
    running *= sf

    rd = recency_decay(cfg, last_signal_at, now)
    halflife = cfg.get("scoring.recency_halflife_days", 180)
    if last_signal_at:
        days = max(0.0, (now - last_signal_at).total_seconds() / 86400.0)
        rd_detail = f"{days:.0f}d since last signal, {halflife:.0f}d half-life -> ×{rd:.2f}"
    else:
        rd_detail = f"no dated signal -> ×{rd:.2f}"
    terms.append(ScoreTerm("Recency", rd, rd_detail))
    running *= rd

    idf = identity_factor(cfg, name, developer, county)
    missing = sum([not name or name.startswith("Unnamed"), not developer, not county])
    idf_detail = (f"name/developer/county all present -> ×{idf:.2f}" if missing == 0
                 else f"missing {missing} of name/developer/county -> ×{idf:.2f}")
    terms.append(ScoreTerm("Identity", idf, idf_detail))
    running *= idf

    if spillover_enabled and spillover_mw:
        spf = spillover_factor(cfg, spillover_mw)
        sp_detail = f"{spillover_mw:,.0f} MW nearby recent/queued DC activity -> ×{spf:.2f}"
        terms.append(ScoreTerm("Spillover", spf, sp_detail))
        running *= spf

    return ScoreBreakdown(terms=terms, total=round(running, 4))


def signal_types_by_project(session: Session, project_ids: list[int]) -> dict[int, list[SignalType]]:
    """Batched version of project_signals() -> [s.signal_type], for a caller
    (the board) that needs this for many projects at once. Two queries total
    regardless of how many project_ids are passed, instead of two per
    project — see app.web.main's board route, which otherwise builds a
    score_breakdown for every visible row on every page load."""
    if not project_ids:
        return {}
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id.in_(project_ids))).all()
    signal_ids = list({l.signal_id for l in links})
    signals_by_id = ({s.id: s for s in session.exec(
        select(Signal).where(Signal.id.in_(signal_ids))).all()} if signal_ids else {})
    out: dict[int, list[SignalType]] = {pid: [] for pid in project_ids}
    for link in links:
        s = signals_by_id.get(link.signal_id)
        if s is not None:
            out[link.project_id].append(s.signal_type)
    return out


def project_score_breakdown(session: Session, cfg: Config, project: Project) -> ScoreBreakdown:
    """Single-project convenience wrapper — fetches this one project's
    linked signal types itself. Fine for a detail page (one project); a
    board rendering many rows should batch signal types once and call
    score_breakdown() directly per row instead (see app/web/main.py)."""
    signals = project_signals(session, project.id)
    # Geometric mean, via TonsEstimate.midpoint itself -- not reimplemented
    # here. run_size_score() feeds priority_score() this SAME midpoint
    # (est.midpoint, geometric — see that property's own docstring for why
    # arithmetic is wrong for a band spanning orders of magnitude); this
    # wrapper exists specifically to reproduce that chain, so it must read
    # low/high through the one function that defines what "midpoint" means
    # rather than recomputing a different number under the same name.
    tons_midpoint = TonsEstimate(
        low=project.tons_estimate_low, high=project.tons_estimate_high, basis=None
    ).midpoint
    return score_breakdown(
        cfg,
        signal_types=[s.signal_type for s in signals],
        window=project.window,
        tons_midpoint=tons_midpoint,
        last_signal_at=project.last_signal_at,
        name=project.name, developer=project.developer, county=project.county,
        spillover_mw=project.spillover_mw,
        spillover_enabled=cfg.get("scoring.spillover.enabled", False),
    )
