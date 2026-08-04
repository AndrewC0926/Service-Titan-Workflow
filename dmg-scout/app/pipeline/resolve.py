"""RESOLVE: match signals to canonical projects.

1. Block candidates by county (+5km radius when coords exist) or shared APN or
   shared developer alias.
2. Score pairs: name similarity (normalized), address, MW proximity, geography.
3. >= auto_merge_threshold: link. In the ambiguous band: LLM adjudication; a
   'match' verdict links, anything else queues for human review.
4. Every link records match_method and match_confidence for auditability.
"""
from __future__ import annotations

import logging
import math

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.config import Config
from app.llm import LLMUnavailable, adjudicate
from app.models import (
    DeveloperAlias, MatchCandidate, Project, ProjectSignal, Signal, utcnow,
)
from app.normalize import normalize_county, normalize_name

log = logging.getLogger(__name__)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def canonical_developer(session: Session, name: str | None) -> str | None:
    if not name:
        return None
    norm = normalize_name(name)
    alias = session.exec(
        select(DeveloperAlias).where(DeveloperAlias.alias_norm == norm)
    ).first()
    return alias.canonical if alias else name


def seed_aliases(session: Session, cfg: Config) -> int:
    added = 0
    for canonical, aliases in (cfg.get("resolution.developer_aliases") or {}).items():
        for alias in [canonical, *aliases]:
            norm = normalize_name(alias)
            if not norm:
                continue
            exists = session.exec(
                select(DeveloperAlias).where(DeveloperAlias.alias_norm == norm)
            ).first()
            if not exists:
                session.add(DeveloperAlias(canonical=canonical, alias=alias, alias_norm=norm))
                added += 1
    session.commit()
    return added


def pair_similarity(signal: Signal, project: Project, radius_km: float) -> float:
    """0-1 similarity used for auto-merge / adjudication banding."""
    scores: list[tuple[float, float]] = []  # (weight, score)

    # APN match is close to dispositive.
    if signal.apn_parcel and project.apn_parcel:
        scores.append((3.0, 1.0 if signal.apn_parcel.strip() == project.apn_parcel.strip() else 0.0))

    sig_names = [n for n in (signal.project_name, signal.developer_or_owner) if n]
    proj_names = [n for n in (project.name, project.developer) if n]
    if sig_names and proj_names:
        best = max(
            fuzz.token_sort_ratio(normalize_name(a), normalize_name(b)) / 100.0
            for a in sig_names for b in proj_names
        )
        scores.append((2.0, best))

    if None not in (signal.latitude, signal.longitude, project.latitude, project.longitude):
        d = haversine_km(signal.latitude, signal.longitude, project.latitude, project.longitude)
        scores.append((2.0, max(0.0, 1.0 - d / radius_km)))

    sig_mw = signal.mw_it or signal.mw_total
    proj_mw = project.mw_it or project.mw_total
    if sig_mw and proj_mw:
        ratio = min(sig_mw, proj_mw) / max(sig_mw, proj_mw)
        scores.append((1.0, ratio))

    if not scores:
        return 0.0
    total_w = sum(w for w, _ in scores)
    return sum(w * s for w, s in scores) / total_w


def _blocked_candidates(session: Session, signal: Signal, radius_km: float) -> list[Project]:
    county = normalize_county(signal.county)
    candidates: dict[int, Project] = {}
    if county:
        for p in session.exec(select(Project).where(Project.county == county,
                                                    Project.status == "active")).all():
            candidates[p.id] = p
    if signal.apn_parcel:
        for p in session.exec(select(Project).where(Project.apn_parcel == signal.apn_parcel)).all():
            candidates[p.id] = p
    if signal.latitude is not None and signal.longitude is not None:
        for p in session.exec(select(Project).where(Project.latitude.is_not(None))).all():
            if haversine_km(signal.latitude, signal.longitude, p.latitude, p.longitude) <= radius_km:
                candidates[p.id] = p
    # Shared canonical developer, same state — small enough set to scan.
    dev = canonical_developer(session, signal.developer_or_owner)
    if dev:
        dev_norm = normalize_name(dev)
        for p in session.exec(select(Project).where(Project.status == "active")).all():
            p_dev = canonical_developer(session, p.developer)
            if p_dev and normalize_name(p_dev) == dev_norm and (
                not signal.state or not p.state or signal.state == p.state
            ):
                candidates[p.id] = p
    return list(candidates.values())


def _signal_record(signal: Signal) -> dict:
    return {
        "project_name": signal.project_name, "developer": signal.developer_or_owner,
        "county": signal.county, "state": signal.state, "address": signal.street_address,
        "apn": signal.apn_parcel, "mw_it": signal.mw_it, "mw_total": signal.mw_total,
        "stage": signal.stage.value, "summary": signal.summary_one_line,
        "signal_type": signal.signal_type.value, "event_date": signal.event_date,
    }


def _project_record(project: Project) -> dict:
    return {
        "project_name": project.name, "developer": project.developer,
        "county": project.county, "state": project.state, "apn": project.apn_parcel,
        "mw_it": project.mw_it, "mw_total": project.mw_total, "stage": project.stage.value,
    }


def _link(session: Session, signal: Signal, project: Project, confidence: float, method: str) -> None:
    exists = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project.id,
                                    ProjectSignal.signal_id == signal.id)
    ).first()
    if not exists:
        session.add(ProjectSignal(project_id=project.id, signal_id=signal.id,
                                  match_confidence=confidence, match_method=method))
    _absorb(project, signal)
    session.add(project)


def _absorb(project: Project, signal: Signal) -> None:
    """Fill project gaps from the signal; never overwrite a known value with null."""
    if signal.project_name and (not project.name or project.name.startswith("Unnamed")):
        project.name = signal.project_name
    project.developer = project.developer or signal.developer_or_owner
    project.county = project.county or normalize_county(signal.county)
    project.state = project.state or signal.state
    project.apn_parcel = project.apn_parcel or signal.apn_parcel
    if project.latitude is None and signal.latitude is not None:
        project.latitude, project.longitude = signal.latitude, signal.longitude
    if signal.mw_it and (project.mw_it or 0) < signal.mw_it:
        project.mw_it = signal.mw_it
    if signal.mw_total and (project.mw_total or 0) < signal.mw_total:
        project.mw_total = signal.mw_total
    stage_order = ["unknown", "concept", "entitlement", "design", "permitting",
                   "procurement", "construction", "operating"]
    if stage_order.index(signal.stage.value) > stage_order.index(project.stage.value):
        project.stage = signal.stage  # stage only moves forward
    when = signal.event_date or signal.created_at
    if project.last_signal_at is None or (when and when > project.last_signal_at):
        project.last_signal_at = when
    project.updated_at = utcnow()


def _new_project(session: Session, signal: Signal) -> Project:
    name = signal.project_name or (
        f"Unnamed {signal.developer_or_owner}" if signal.developer_or_owner
        else f"Unnamed project ({signal.county or signal.jurisdiction or 'unknown location'})"
    )
    project = Project(name=name, developer=signal.developer_or_owner,
                      county=normalize_county(signal.county), state=signal.state)
    session.add(project)
    session.flush()  # need project.id
    _link(session, signal, project, 1.0, "direct")
    return project


def run_resolve(session: Session, cfg: Config, use_llm: bool = True) -> dict:
    seed_aliases(session, cfg)
    radius = cfg.get("resolution.block_radius_km", 5)
    auto_t = cfg.get("resolution.auto_merge_threshold", 0.88)
    review_t = cfg.get("resolution.review_threshold", 0.55)

    linked_ids = {ps.signal_id for ps in session.exec(select(ProjectSignal)).all()}
    pending_review = {mc.signal_id for mc in session.exec(
        select(MatchCandidate).where(MatchCandidate.status == "pending")).all()}
    unlinked = [s for s in session.exec(select(Signal)).all()
                if s.id not in linked_ids and s.id not in pending_review]

    stats = {"auto_linked": 0, "llm_linked": 0, "queued_review": 0, "new_projects": 0}
    for signal in unlinked:
        candidates = _blocked_candidates(session, signal, radius)
        scored = sorted(
            ((pair_similarity(signal, p, radius), p) for p in candidates),
            key=lambda t: t[0], reverse=True,
        )
        best_sim, best = scored[0] if scored else (0.0, None)

        if best is not None and best_sim >= auto_t:
            _link(session, signal, best, best_sim, "blocking+fuzzy")
            stats["auto_linked"] += 1
        elif best is not None and best_sim >= review_t:
            verdict, reasoning = "uncertain", "LLM adjudication unavailable"
            if use_llm:
                try:
                    result = adjudicate(_signal_record(signal), _project_record(best))
                    verdict = result.get("verdict", "uncertain")
                    reasoning = result.get("reasoning", "")
                except LLMUnavailable:
                    use_llm = False
                except Exception as exc:  # noqa: BLE001
                    reasoning = f"adjudication error: {exc}"
            if verdict == "match":
                _link(session, signal, best, best_sim, "llm_adjudicated")
                _learn_alias(session, signal, best)
                stats["llm_linked"] += 1
            elif verdict == "no_match":
                _new_project(session, signal)
                stats["new_projects"] += 1
            else:
                session.add(MatchCandidate(signal_id=signal.id, project_id=best.id,
                                           similarity=best_sim, llm_verdict=verdict,
                                           llm_reasoning=reasoning))
                stats["queued_review"] += 1
        else:
            _new_project(session, signal)
            stats["new_projects"] += 1
        session.commit()
    return stats


def _learn_alias(session: Session, signal: Signal, project: Project) -> None:
    """A confirmed merge teaches the alias table the signal's developer entity."""
    if not (signal.developer_or_owner and project.developer):
        return
    norm = normalize_name(signal.developer_or_owner)
    if not norm or norm == normalize_name(project.developer):
        return
    exists = session.exec(select(DeveloperAlias).where(DeveloperAlias.alias_norm == norm)).first()
    if not exists:
        session.add(DeveloperAlias(canonical=project.developer, alias=signal.developer_or_owner,
                                   alias_norm=norm, learned_from="merge_confirmation"))


def apply_review_decision(session: Session, candidate_id: int, decision: str) -> None:
    """One-click merge/reject from the dashboard review queue."""
    mc = session.get(MatchCandidate, candidate_id)
    if mc is None or mc.status != "pending":
        return
    signal = session.get(Signal, mc.signal_id)
    project = session.get(Project, mc.project_id)
    if decision == "merge" and signal and project:
        _link(session, signal, project, mc.similarity, "manual_merge")
        _learn_alias(session, signal, project)
        mc.status = "merged"
    else:
        if signal:
            _new_project(session, signal)
        mc.status = "rejected"
    mc.resolved_at = utcnow()
    session.add(mc)
    session.commit()
