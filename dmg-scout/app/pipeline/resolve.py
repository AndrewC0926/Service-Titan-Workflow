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
import re

from rapidfuzz import fuzz
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import Config
from app.llm import LLMUnavailable, adjudicate
from app.models import (
    ACTIVE_STATUSES, DeveloperAlias, MatchCandidate, Project, ProjectSignal, Signal,
    Stage, StageObservation, utcnow,
)
from app.normalize import normalize_county, normalize_name
from app.runguard import STALE_RUN_HOURS, ConcurrentStage, running_stage, stage_run

log = logging.getLogger(__name__)

# Ceiling on similarity when nothing distinguishing (project name, APN, coordinates)
# was available to compare — county and developer agreement alone. Sits between
# resolution.review_threshold (0.55) and auto_merge_threshold (0.88) so these pairs
# get adjudicated or reviewed rather than merged on a coincidence of geography.
WEAK_EVIDENCE_CAP = 0.60


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


def alias_map(session: Session) -> dict[str, str]:
    """The whole alias table as {alias_norm: canonical}, in one query.

    canonical_developer() costs one indexed lookup, which is right for one name
    and wrong for a scan. _blocked_candidates called it once per active project
    per signal, so the developer blocking key alone cost one round trip per row
    on the board — against a database in another region. At 101 projects that is
    ~101 round trips per signal, and it grows with the board: resolve measured
    ~1.7 signals/min at 101 projects where it had managed 9/min at ~50.

    Equivalent to calling canonical_developer() per name, exactly: alias_norm
    carries a unique constraint (uq_alias_norm), so keying a dict on it cannot
    collapse two rows that .first() would have distinguished.

    Rebuilt per call rather than cached for the run, because _learn_alias() adds
    rows mid-run and a stale map would silently stop matching them.
    """
    return {a.alias_norm: a.canonical
            for a in session.exec(select(DeveloperAlias)).all()}


def canonical_with(aliases: dict[str, str], name: str | None) -> str | None:
    """canonical_developer() against an already-loaded alias map."""
    if not name:
        return None
    return aliases.get(normalize_name(name), name)


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


# Parcel numbers inside a free-text APN field. CEQAnet's APN is typed by a
# different agency clerk on every filing, so the same parcels arrive as
# "4090-021-032 through -034", "4090-021-032, 4090-021-033, 4090-021-034",
# "209-411-02, -03 & -04" and "... -03 and -04". Exact string comparison scored
# those as DISAGREEMENT at the heaviest weight in the function, which is how four
# projects with byte-identical names split into eight rows.
_APN_TOKEN = re.compile(r"\d[\d\-]{3,}")


def parse_apns(raw: str | None) -> set[str]:
    """Parcel identifiers in a free-text APN field, as a comparable set.

    Deliberately conservative: it does NOT expand ranges ("through -034"), because
    inventing parcel numbers to force a match is worse than abstaining. Returns an
    empty set when nothing parses, and callers must treat empty as "no opinion"
    rather than as disagreement.
    """
    if not raw:
        return set()
    out: set[str] = set()
    for tok in _APN_TOKEN.findall(raw):
        digits = tok.replace("-", "")
        if len(digits) >= 5:            # a full APN, not a "-034" suffix fragment
            out.add(digits)
        # "7426029006-007" is one parcel plus a range suffix, not a 13-digit
        # parcel. Emit the leading parcel too so it can match a filing that
        # writes the same lot on its own.
        head = tok.split("-")[0]
        if len(head) >= 8:
            out.add(head)
    return out


def pair_similarity(signal: Signal, project: Project, radius_km: float) -> float:
    """0-1 similarity used for auto-merge / adjudication banding.

    Project-name similarity is the primary evidence; a shared developer is only
    WEAK evidence (weight 0.75) — Vantage builds many campuses, and two
    different projects by one developer must never auto-merge on the developer
    string alone. A known-county mismatch actively penalizes.

    Weak evidence alone is capped below the auto-merge threshold. Because the score
    is a weighted MEAN, a signal carrying only a county would otherwise score a
    perfect 1.0 off that single agreement and auto-merge — which is how a Storey
    County agenda packet with no project name at all attached itself to the SV RNO
    data center at confidence 1.000 and dragged its recency forward five months.
    Same county is not the same building.
    """
    # SCH number is not evidence to be weighed — it IS the project. CEQAnet
    # assigns one per project and reuses it across the whole filing series, so an
    # agreement short-circuits every other term. Disagreement deliberately does
    # NOT score 0: two SCH numbers can cover one development (a specific plan and
    # its subsequent tract map), so a mismatch abstains and lets the rest decide.
    if signal.sch_number and project.sch_number:
        if signal.sch_number.strip() == project.sch_number.strip():
            return 1.0

    scores: list[tuple[float, float]] = []  # (weight, score)
    strong_evidence = False  # an identifier that can distinguish two neighbours

    # APN is POSITIVE-ONLY evidence, and that asymmetry is the whole fix.
    #
    # A shared parcel is close to dispositive: two filings listing the same lot
    # are the same site even when one enumerates more lots than the other. But
    # non-overlap says almost nothing, because the field is a free-text parcel
    # list of varying completeness and formatting — one filing gives the main lot,
    # the next gives all eight, a third is truncated mid-string
    # ("7426029006-007, 742603004-015,74"). Scoring that disagreement at weight
    # 3.0 is what dragged four byte-identical project names down to ~0.59 and
    # split them into eight rows. So when the parcels do not overlap, the term
    # abstains rather than voting against. See tests/test_sch_dedupe.py.
    if parse_apns(signal.apn_parcel) & parse_apns(project.apn_parcel):
        scores.append((3.0, 1.0))
        strong_evidence = True

    # Both names must SURVIVE normalization, not merely exist. normalize_name is
    # built for company names and strips legal suffixes, phase words, SPE codes and
    # roman numerals, so a name like "Phase II LLC" reduces to "". Comparing that
    # to anything scores 0 at the second-heaviest weight in the function AND sets
    # strong_evidence, which is the APN failure mode exactly: a field that could
    # not be parsed into a comparable value voting against instead of abstaining.
    # Incidence on the 2026-08-06 corpus is zero, so this is a guard rather than a
    # repair — but invariant 4 is stated as an absolute, and a term that is only
    # safe because no filing has yet been named badly enough is not compliance.
    sig_name = normalize_name(signal.project_name) if signal.project_name else ""
    proj_name = (normalize_name(project.name)
                 if project.name and not project.name.startswith("Unnamed") else "")
    if sig_name and proj_name:
        scores.append((2.0, fuzz.token_sort_ratio(sig_name, proj_name) / 100.0))
        strong_evidence = True
    if signal.developer_or_owner and project.developer:
        sig_dev = normalize_name(signal.developer_or_owner)
        proj_dev = normalize_name(project.developer)
        if sig_dev and proj_dev:
            scores.append((0.75, fuzz.token_sort_ratio(sig_dev, proj_dev) / 100.0))

    # County agreement is only meaningful inside a state. "Washington County" is a
    # real county in eighteen of them, and the term scored 1.0 for any two of
    # those at weight 1.5. No collision exists in the current territory (CA + NV,
    # 17 distinct counties, checked 2026-08-06), which is precisely why it would
    # go unnoticed until Phase C widens the territory and it starts silently
    # agreeing across state lines.
    sig_county, proj_county = normalize_county(signal.county), normalize_county(project.county)
    if sig_county and proj_county:
        same_state = (not signal.state or not project.state
                      or signal.state == project.state)
        scores.append((1.5, 1.0 if (sig_county == proj_county and same_state) else 0.0))

    if None not in (signal.latitude, signal.longitude, project.latitude, project.longitude):
        d = haversine_km(signal.latitude, signal.longitude, project.latitude, project.longitude)
        scores.append((2.0, max(0.0, 1.0 - d / radius_km)))
        strong_evidence = True

    # IT load against IT load, total against total — never one against the other.
    # `signal.mw_it or signal.mw_total` vs the same expression on the project will
    # happily compare a signal's IT megawatts to a project's TOTAL megawatts, and
    # those are different quantities: IT load runs roughly 60-75% of facility
    # total, so the SAME campus described both ways scores ~0.65 here and votes
    # against its own match. Comparing incomparable values is the invariant-4
    # failure mode whatever the field, so when only unlike pairs are available the
    # term abstains. Latent today — nothing in the corpus carries mw_it as of
    # 2026-08-06 — and it bites the moment extraction starts filling that column.
    mw_pairs = [(signal.mw_it, project.mw_it), (signal.mw_total, project.mw_total)]
    comparable = [(a, b) for a, b in mw_pairs if a and b]
    if comparable:
        ratios = [min(a, b) / max(a, b) for a, b in comparable]
        scores.append((1.0, sum(ratios) / len(ratios)))

    if not scores:
        return 0.0
    total_w = sum(w for w, _ in scores)
    sim = sum(w * s for w, s in scores) / total_w
    if not strong_evidence:
        # Land it in the adjudication/review band instead of auto-merging or
        # silently splitting: a human or the LLM decides, and it stays visible.
        sim = min(sim, WEAK_EVIDENCE_CAP)
    return sim


def _blocked_candidates(session: Session, signal: Signal, radius_km: float) -> list[Project]:
    county = normalize_county(signal.county)
    candidates: dict[int, Project] = {}

    # ONE status filter, applied to every key. The county and developer keys used
    # to filter on status while SCH, APN and geography did not, so a merged-away
    # project stayed reachable through three of the five keys. That is not
    # theoretical: signal 503 still reaches project #963 via SCH, APN and geo, and
    # #963 is the duplicate that the concurrent-resolve bug created and a merge
    # cleaned up (see app/runguard.py). An SCH agreement short-circuits
    # pair_similarity to 1.0, so the next resolve would auto-link the signal
    # straight back onto the row that was merged away — silently rebuilding the
    # duplicate that invariant 3 exists to catch.
    #
    # ACTIVE_STATUSES, not status == "active", and that widens the net on purpose.
    # A project the rep has already contacted, specified or bid is emphatically
    # still a live project: under the old literal it was invisible to the county
    # and developer keys, so the next filing about it had nowhere to attach and
    # would open a second row for a job already in progress. Same invariant, other
    # direction.
    def active(stmt):
        return session.exec(stmt.where(Project.status.in_(ACTIVE_STATUSES))).all()

    if county:
        for p in active(select(Project).where(Project.county == county)):
            candidates[p.id] = p
    # SCH first: it is the one exact project key, and it must reach the scorer even
    # when the signal has no county (a filing whose county field came back null
    # would otherwise be blocked out of its own project).
    if signal.sch_number:
        for p in active(select(Project).where(Project.sch_number == signal.sch_number)):
            candidates[p.id] = p
    # Parsed parcel OVERLAP, not string equality. parse_apns exists because this
    # field is free text typed by a different agency clerk on every filing — the
    # same parcels arrive as "4090-021-032 through -034" and as "4090-021-032,
    # 4090-021-033, 4090-021-034". pair_similarity learned that and blocking did
    # not, so the scorer could treat a shared parcel as near-dispositive evidence
    # while the blocking key that feeds it never put the pair in front of it.
    # Measured 2026-08-06: 13 of 294 APN-bearing signals have a parcel-overlap
    # partner that exact equality misses. County blocking happens to reach all 13
    # today, so this is latent rather than live — but a key that works only
    # because a different key covers for it is one territory change from not
    # working, and Phase C adds territory.
    sig_apns = parse_apns(signal.apn_parcel)
    if sig_apns:
        for p in active(select(Project).where(Project.apn_parcel.is_not(None))):
            if sig_apns & parse_apns(p.apn_parcel):
                candidates[p.id] = p
    if signal.latitude is not None and signal.longitude is not None:
        for p in active(select(Project).where(Project.latitude.is_not(None))):
            if haversine_km(signal.latitude, signal.longitude, p.latitude, p.longitude) <= radius_km:
                candidates[p.id] = p
    # Shared canonical developer, same state. The scan is over the whole active
    # board, so the alias table is loaded once here rather than looked up per row
    # — see alias_map(). Same candidates, two queries instead of N+1.
    aliases = alias_map(session)
    dev = canonical_with(aliases, signal.developer_or_owner)
    if dev:
        dev_norm = normalize_name(dev)
        for p in active(select(Project)):
            p_dev = canonical_with(aliases, p.developer)
            if p_dev and normalize_name(p_dev) == dev_norm and (
                not signal.state or not p.state or signal.state == p.state
            ):
                candidates[p.id] = p
    # A data center and a factory are never the same building, and one developer
    # can be building both — so category is a hard blocking key, not a tiebreak.
    # Without this, a shared developer name alone would merge across the boards.
    return [p for p in candidates.values() if p.category == signal.category]


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
    from app.firms import resolve_signal_firms
    exists = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project.id,
                                    ProjectSignal.signal_id == signal.id)
    ).first()
    if not exists:
        session.add(ProjectSignal(project_id=project.id, signal_id=signal.id,
                                  match_confidence=confidence, match_method=method))
    _absorb(project, signal)
    _record_stage_observation(session, project, signal)
    session.add(project)
    resolve_signal_firms(session, project.id, signal.named_firms)


def _record_stage_observation(session: Session, project: Project, signal: Signal) -> None:
    """One row per signal that stated a stage, kept forever — see StageObservation's
    docstring. Written for every signal.stage != unknown, whether or not it moved
    project.stage forward, and deduped on signal_id so a re-resolve of an
    already-linked signal (concurrent run, backfill) never double-records it.
    """
    if signal.stage == Stage.unknown:
        return
    exists = session.exec(
        select(StageObservation).where(StageObservation.signal_id == signal.id)
    ).first()
    if exists:
        return
    observed_at = signal.event_date or signal.created_at
    session.add(StageObservation(
        project_id=project.id, stage=signal.stage, observed_at=observed_at,
        from_event=signal.event_date is not None, signal_id=signal.id,
    ))


def _absorb(project: Project, signal: Signal) -> None:
    """Fill project gaps from the signal; never overwrite a known value with null."""
    if signal.project_name and (not project.name or project.name.startswith("Unnamed")):
        project.name = signal.project_name
    project.developer = project.developer or signal.developer_or_owner
    project.county = project.county or normalize_county(signal.county)
    project.state = project.state or signal.state
    project.apn_parcel = project.apn_parcel or signal.apn_parcel
    project.sch_number = project.sch_number or signal.sch_number
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
    """Create the project this signal implies — unless it already has one.

    The last-moment check is the one that closes the #961/#963 race. The loop
    re-checks at the top of each iteration, but candidate blocking happens there
    and project creation happens *after* an LLM call, so the window between
    "nobody has a project for this signal" and "I am creating one" is not
    microseconds — it is however long adjudication takes, tens of seconds. Two
    runs whose blocking queries both landed inside that window each saw no
    candidate and each created a project, which is exactly the shape of the
    observed failure. The SCH key was never involved: neither run could match on
    a row the other had not committed yet.
    """
    existing = session.exec(
        select(ProjectSignal).where(ProjectSignal.signal_id == signal.id)).first()
    if existing is not None:
        project = session.get(Project, existing.project_id)
        if project is not None:
            log.warning("signal %s acquired project #%d while it was being resolved; "
                        "absorbing into it instead of creating a duplicate",
                        signal.id, project.id)
            _absorb(project, signal)
            _record_stage_observation(session, project, signal)
            session.add(project)
            return project

    name = signal.project_name or (
        f"Unnamed {signal.developer_or_owner}" if signal.developer_or_owner
        else f"Unnamed project ({signal.county or signal.jurisdiction or 'unknown location'})"
    )
    project = Project(name=name, category=signal.category,
                      developer=signal.developer_or_owner,
                      county=normalize_county(signal.county), state=signal.state)
    session.add(project)
    session.flush()  # need project.id
    _link(session, signal, project, 1.0, "direct")
    return project


# The guard lives in app/runguard.py now — extract needed the identical thing, so
# copying it a third time was not the answer. These names are kept because the
# resolve-specific tests and the CLI import them.
RESOLVE_RUN_SOURCE = "resolve"
ConcurrentResolve = ConcurrentStage


def _running_resolve(session: Session, stale_after_hours: float = STALE_RUN_HOURS):
    return running_stage(session, RESOLVE_RUN_SOURCE, stale_after_hours)


def run_resolve(session: Session, cfg: Config, use_llm: bool = True,
                force: bool = False) -> dict:
    """Match unlinked signals to canonical projects.

    Refuses to start while another resolve is in flight. Two concurrent runs each
    snapshot `unlinked` at their own start, so a signal unlinked in both snapshots
    is processed twice and creates the project twice — which is exactly what
    happened on 2026-08-06: a detached run and a foreground run overlapped for
    3.5 minutes, and signal 503 became projects #961 and #963, byte-identical
    down to the SCH number. Backfill learned this same lesson first; resolve is
    now guarded the same way.
    """
    from app.firms import seed_firms

    with stage_run(session, RESOLVE_RUN_SOURCE, force=force) as run:
        seed_aliases(session, cfg)
        seed_firms(session, cfg)
        radius = cfg.get("resolution.block_radius_km", 5)
        auto_t = cfg.get("resolution.auto_merge_threshold", 0.88)
        review_t = cfg.get("resolution.review_threshold", 0.55)

        linked_ids = {ps.signal_id for ps in session.exec(select(ProjectSignal)).all()}
        pending_review = {mc.signal_id for mc in session.exec(
            select(MatchCandidate).where(MatchCandidate.status == "pending")).all()}
        unlinked = [s for s in session.exec(select(Signal)).all()
                    if s.id not in linked_ids and s.id not in pending_review]
        run.records_fetched = len(unlinked)

        stats = {"auto_linked": 0, "llm_linked": 0, "queued_review": 0,
                 "new_projects": 0, "skipped_already_linked": 0}
        stats = _resolve_loop(session, cfg, unlinked, stats,
                              radius, auto_t, review_t, use_llm)
    return stats


def _resolve_loop(session: Session, cfg: Config, unlinked: list[Signal], stats: dict,
                  radius: float, auto_t: float, review_t: float, use_llm: bool) -> dict:
    for signal in unlinked:
        # Defence in depth behind the concurrency guard: the snapshot above can be
        # minutes or hours stale by the time we reach this signal, and --force
        # exists. Linking a signal that is already linked would duplicate its
        # project, so re-check rather than trust the snapshot. One indexed query.
        if session.exec(select(ProjectSignal)
                        .where(ProjectSignal.signal_id == signal.id)).first() is not None:
            log.warning("signal %s was linked after this run's snapshot; skipping "
                        "(concurrent resolve?)", signal.id)
            stats["skipped_already_linked"] += 1
            continue

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


def backfill_stage_observations(session: Session) -> int:
    """One-time reconstruction of the ledger for signals linked before
    StageObservation existed. Idempotent (dedups on signal_id via
    _record_stage_observation), so re-running it after this is safe and free —
    everything past the first run is a no-op.

    Every ProjectSignal already names both sides; no re-matching, no LLM.
    """
    n_before = session.exec(select(func.count()).select_from(StageObservation)).one()
    for link in session.exec(select(ProjectSignal)).all():
        project = session.get(Project, link.project_id)
        signal = session.get(Signal, link.signal_id)
        if project is not None and signal is not None:
            _record_stage_observation(session, project, signal)
    session.commit()
    n_after = session.exec(select(func.count()).select_from(StageObservation)).one()
    return n_after - n_before


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
