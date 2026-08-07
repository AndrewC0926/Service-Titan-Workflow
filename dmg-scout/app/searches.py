"""Saved searches: standing questions about the board, and alerts when they change.

Three question shapes, and the third is not like the other two:

    over 10 MW in Storey County        a filter over the board right now
    anything naming Southland or ACCO  a filter over the board right now
    a flagged project changes stage    a comparison between two points in time

The first two are answerable from the current board. The third is not — "changed"
is a claim about a previous state, so the previous answer has to be stored. That
is what SavedSearch.last_seen is for, and it is why evaluating a change-alert has
a side effect while evaluating a filter does not.

**Unknown criteria keys raise.** A free-form filter that ignores what it does not
understand degrades silently into "match everything", and a saved search that
quietly widens is worse than one that errors: the rep keeps trusting a question
that is no longer the question they asked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import (
    ACTIVE_STATUSES, Category, Firm, Project, ProjectFirm, ProjectSignal, SavedSearch,
    Signal, Stage, utcnow,
)
from app.normalize import normalize_county, normalize_name

log = logging.getLogger(__name__)

# Every key a saved search may carry. Adding one here is the whole cost of adding
# a new askable question; anything not here is a typo or a stale search.
CRITERIA_KEYS = frozenset({
    "min_mw", "max_mw", "county", "state", "category", "stage", "window",
    "min_score", "names", "min_tons", "in_territory", "project_ids",
})


class UnknownCriterion(ValueError):
    """A saved search used a key the evaluator does not implement."""


@dataclass
class SearchResult:
    search: SavedSearch
    projects: list[Project] = field(default_factory=list)
    # For change alerts: (project, previous stage, current stage)
    changed: list[tuple[Project, str, str]] = field(default_factory=list)
    entered: list[Project] = field(default_factory=list)


def validate(criteria: dict) -> None:
    unknown = sorted(set(criteria) - CRITERIA_KEYS)
    if unknown:
        raise UnknownCriterion(
            f"unknown search criteria: {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(CRITERIA_KEYS))}. "
            f"A filter that ignores what it does not understand matches "
            f"everything, so this refuses rather than widening silently.")


def _name_hits(session: Session, needles: list[str]) -> set[int]:
    """Project ids where any of `needles` appears as a company anywhere on the row.

    Deliberately broader than the project's own developer field. "Anything naming
    Southland or ACCO" is a question about who is ON the job — the mechanical
    contractor, the GC, the firm named in a filing — and most of those never
    appear in Project.developer. Matching only that field would answer a
    different, much narrower question while looking like it answered this one.
    """
    wanted = {normalize_name(n) for n in needles if n and normalize_name(n)}
    if not wanted:
        return set()

    def matches(value: str | None) -> bool:
        norm = normalize_name(value or "")
        return bool(norm) and any(w in norm or norm in w for w in wanted)

    hits: set[int] = set()
    for p in session.exec(select(Project)).all():
        if matches(p.developer) or matches(p.name):
            hits.add(p.id)

    firm_ids = {f.id for f in session.exec(select(Firm)).all()
                if matches(f.name) or any(matches(a) for a in f.aliases or [])}
    if firm_ids:
        for pf in session.exec(
                select(ProjectFirm).where(ProjectFirm.firm_id.in_(firm_ids))).all():
            hits.add(pf.project_id)

    # Firms named in a signal but never promoted to the roster still count — the
    # question is "is this company on the job", not "is it in our firm table".
    links = {l.signal_id: l.project_id for l in session.exec(select(ProjectSignal)).all()}
    if links:
        for s in session.exec(select(Signal).where(Signal.id.in_(list(links)))).all():
            names = [f.get("name") if isinstance(f, dict) else f
                     for f in (s.named_firms or [])]
            if any(matches(n) for n in names) or matches(s.developer_or_owner):
                hits.add(links[s.id])
    return hits


def run_search(session: Session, criteria: dict) -> list[Project]:
    """Projects matching `criteria`, best score first. Pure — no side effects."""
    validate(criteria)
    q = select(Project).where(Project.status.in_(ACTIVE_STATUSES))

    if criteria.get("in_territory", True):
        q = q.where(Project.in_territory == True)  # noqa: E712
    if (county := criteria.get("county")):
        q = q.where(Project.county == normalize_county(county))
    if (state := criteria.get("state")):
        q = q.where(Project.state == state)
    if (category := criteria.get("category")):
        q = q.where(Project.category == Category(category))
    if (stage := criteria.get("stage")):
        q = q.where(Project.stage == Stage(stage))
    if (window := criteria.get("window")):
        q = q.where(Project.window == window)
    if (min_score := criteria.get("min_score")) is not None:
        q = q.where(Project.score >= float(min_score))
    if (min_tons := criteria.get("min_tons")) is not None:
        q = q.where(Project.tons_estimate_low >= float(min_tons))
    if (ids := criteria.get("project_ids")):
        q = q.where(Project.id.in_(list(ids)))

    projects = session.exec(q.order_by(Project.score.desc())).all()

    # MW lives in two columns and a project may state either. Filtered in Python
    # so "over 10 MW" means "over 10 MW by whichever figure this project states",
    # rather than silently excluding every project that gave only a total.
    if (min_mw := criteria.get("min_mw")) is not None:
        projects = [p for p in projects
                    if (p.mw_it or p.mw_total or 0) >= float(min_mw)]
    if (max_mw := criteria.get("max_mw")) is not None:
        projects = [p for p in projects
                    if (p.mw_it or p.mw_total or 0) <= float(max_mw)]

    if (names := criteria.get("names")):
        hits = _name_hits(session, names if isinstance(names, list) else [names])
        projects = [p for p in projects if p.id in hits]
    return projects


def evaluate(session: Session, search: SavedSearch, *, commit: bool = True) -> SearchResult:
    """Run a saved search and work out what is NEW since it last ran.

    Membership and transition are reported separately because they prompt
    different actions: a project entering "over 10 MW in Storey County" is a new
    opportunity, while a project already in it moving from entitlement to design
    is a closing window on one you already knew about.

    The first evaluation of a change-alert reports NOTHING as changed. There is no
    previous state to compare against, and announcing every current row as a
    change would make the first digest after creating a search pure noise — which
    trains people to ignore the section.
    """
    projects = run_search(session, search.criteria)
    result = SearchResult(search=search, projects=projects)

    previous = search.last_seen or {}
    current = {str(p.id): p.stage.value for p in projects}

    if previous:
        for p in projects:
            was = previous.get(str(p.id))
            if was is None:
                result.entered.append(p)
            elif was != p.stage.value:
                result.changed.append((p, was, p.stage.value))

    search.last_seen = current
    search.last_run_at = utcnow()
    session.add(search)
    if commit:
        session.commit()
    return result


def digest_section(session: Session) -> tuple[str, int]:
    """The saved-search block for the daily digest, and how many items it holds.

    Only searches with alert=True are evaluated for the digest. Searches are still
    stored and runnable from the dashboard when alerting is off — a question worth
    asking on demand is not always worth an email every morning.
    """
    lines: list[str] = []
    count = 0
    searches = session.exec(
        select(SavedSearch).where(SavedSearch.alert == True)).all()  # noqa: E712
    for search in searches:
        try:
            res = evaluate(session, search, commit=False)
        except UnknownCriterion as exc:
            # Loud, not silent: a saved search that cannot run is a question the
            # rep believes is being asked and is not.
            lines.append(f"  • [{search.name}] BROKEN — {exc}")
            count += 1
            log.error("saved search %r cannot run: %s", search.name, exc)
            continue

        if search.alert_on_change:
            for p, was, now in res.changed:
                lines.append(f"  • [{search.name}] {p.name} moved {was} → {now} "
                             f"({p.window.value}, score {p.score:.2f})")
                count += 1
        for p in res.entered:
            tons = (f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f} tons"
                    if p.tons_estimate_low else "size unknown")
            lines.append(f"  • [{search.name}] NEW: {p.name} — {p.county or '?'} Co — "
                         f"{tons} — score {p.score:.2f}")
            count += 1
    if not lines:
        return "", 0
    return "SAVED SEARCHES\n" + "\n".join(lines), count
