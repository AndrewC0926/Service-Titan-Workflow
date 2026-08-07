"""Coverage report (Step 5) and pipeline completeness (Step 4 banner).

Coverage answers "what am I blind to": per territory county — projects, stage
breakdown, contributing sources, most recent signal. A county quiet for 90+
days is flagged SUSPECTED BLIND (broken adapter until proven empty market),
never presented as simply an empty market.

Completeness answers "is the board lying by omission": pending triage/extract
backlogs and unfinished backfill chunk sets make the board banner say
INCOMPLETE instead of letting a half-processed corpus look finished.
"""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES, BackfillCheckpoint, MatchCandidate, Project, ProjectSignal, RawDocument,
    Signal, TriageResult, utcnow,
)
from app.normalize import normalize_county

BLIND_DAYS = 90


def coverage_report(session: Session, cfg: Config) -> dict:
    signals = session.exec(select(Signal)).all()
    docs = {d.id: d for d in session.exec(select(RawDocument)).all()}
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()

    rows = []
    now = utcnow()
    for state, counties in (cfg.get("territory") or {}).items():
        for county in counties:
            county_n = normalize_county(county)
            c_projects = [p for p in projects
                          if normalize_county(p.county) == county_n
                          and (p.state or state).upper() == state]
            stages: dict[str, int] = {}
            for p in c_projects:
                stages[p.stage.value] = stages.get(p.stage.value, 0) + 1
            c_signals = [s for s in signals if normalize_county(s.county) == county_n
                         and (s.state or state).upper() == state]
            sources = sorted({
                docs[s.raw_document_id].source
                for s in c_signals if s.raw_document_id in docs
            })
            latest = max((s.event_date or s.created_at for s in c_signals), default=None)
            days_quiet = (now - latest).days if latest else None
            rows.append({
                "state": state, "county": county,
                "projects": len(c_projects), "stages": stages,
                "sources": sources,
                "latest_signal": latest,
                "days_quiet": days_quiet,
                "suspected_blind": days_quiet is None or days_quiet > BLIND_DAYS,
            })
    blind = [r for r in rows if r["suspected_blind"]]
    return {"rows": rows, "n_blind": len(blind),
            "blind_counties": [f"{r['county']}, {r['state']}" for r in blind]}


def coverage_text(report: dict) -> str:
    lines = [f"{'county':22s} {'proj':>4s} {'stages':28s} {'sources':26s} {'latest':>10s}  status"]
    for r in report["rows"]:
        stages = ",".join(f"{k}:{v}" for k, v in sorted(r["stages"].items())) or "—"
        latest = f"{r['latest_signal']:%Y-%m-%d}" if r["latest_signal"] else "never"
        status = ("SUSPECTED BLIND — verify adapter before trusting the empty market"
                  if r["suspected_blind"] else "ok")
        lines.append(f"{r['county'] + ', ' + r['state']:22s} {r['projects']:>4d} "
                     f"{stages[:28]:28s} {','.join(r['sources'])[:26]:26s} {latest:>10s}  {status}")
    lines.append("")
    lines.append(f"{report['n_blind']} of {len(report['rows'])} territory counties suspected blind "
                 f"(no signal in {BLIND_DAYS}d).")
    return "\n".join(lines)


def pipeline_completeness(session: Session, cfg: Config) -> dict:
    """Is the board presenting a complete picture? Used by the dashboard banner.

    "Unresolved" and "awaiting review" are different claims. An unlinked signal
    with no match_candidate means `scout resolve` has not looked at it yet — that
    is unfinished work, and the board is genuinely incomplete until it runs. An
    unlinked signal already sitting in match_candidates (status='pending') has
    been looked at: resolve queued it because the match was ambiguous, and it is
    waiting on a human merge decision in /review, not on a pipeline run. Counting
    the second as the first told a rep to "run scout resolve" to clear a queue
    that rerunning resolve cannot touch — resolve skips signals already queued
    for review by design (see app/pipeline/resolve.py).
    """
    pending_triage = len(session.exec(
        select(RawDocument).where(
            RawDocument.triage_result.in_([TriageResult.pending, TriageResult.error]))).all())
    pending_extract = len(session.exec(
        select(RawDocument).where(RawDocument.triage_result == TriageResult.relevant,
                                  RawDocument.processed_at.is_(None))).all())
    unresolved, pending_review = _unlinked_signal_counts(session)

    incomplete_backfills = []
    from app.sources import get_adapter
    cps = session.exec(select(BackfillCheckpoint)).all()
    by_source_since: dict[tuple[str, str], int] = {}
    for cp in cps:
        since = cp.chunk_key.split(":", 1)[0]
        key = (cp.source, since)
        by_source_since[key] = by_source_since.get(key, 0) + 1
    for (source, since), done in sorted(by_source_since.items()):
        try:
            from datetime import datetime
            expected = len(get_adapter(source).backfill_chunks(
                cfg, datetime.strptime(since, "%Y-%m-%d")))
        except Exception:  # noqa: BLE001
            continue
        if done < expected:
            incomplete_backfills.append(
                {"source": source, "since": since, "done": done, "expected": expected})

    # pending_review is deliberately excluded: it is correct steady state, not an
    # unfinished run, and must never make the board say INCOMPLETE.
    complete = not (pending_triage or pending_extract or unresolved or incomplete_backfills)
    return {"complete": complete, "pending_triage": pending_triage,
            "pending_extract": pending_extract, "unresolved_signals": unresolved,
            "pending_review": pending_review, "incomplete_backfills": incomplete_backfills}


def _unlinked_signal_counts(session: Session) -> tuple[int, int]:
    """(truly unresolved, awaiting human review) among signals with no project link.

    A signal can be unlinked for two different reasons and only one of them is
    unfinished work — see the docstring on pipeline_completeness for why the
    distinction matters to the banner.
    """
    linked = {ps.signal_id for ps in session.exec(select(ProjectSignal)).all()}
    queued = {mc.signal_id for mc in session.exec(
        select(MatchCandidate).where(MatchCandidate.status == "pending")).all()}
    unlinked_ids = [s.id for s in session.exec(select(Signal)).all() if s.id not in linked]
    pending_review = sum(1 for sid in unlinked_ids if sid in queued)
    unresolved = len(unlinked_ids) - pending_review
    return unresolved, pending_review
