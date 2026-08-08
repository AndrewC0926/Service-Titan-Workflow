"""Phase 4: county-adjacency data center spillover — a scoring INPUT, not a
new detector and not a wider gate.

The argument (the user's own framing): recent or queued data-center
construction nearby is itself a modest positive signal about a project's
winnability — more contractors staffed up in the area, more confirmation
the region is a genuine build corridor — and "nearby" has to mean county
adjacency, not exact county match, because construction workforces commute
across county lines. Storey County (TRIC) is the case that forced this:
Tahoe Reno Industrial Center sits in Storey County, but its construction and
operations workers live in Reno/Sparks — Washoe County next door. A
same-county-only version of this signal would credit TRIC's own projects
with none of that.

Two sources of "recent DC MW" feed this, both already built:
  - Scout's own board (Project rows), decayed by a half-life because the
    workforce effect is front-loaded and temporary — see
    scoring.spillover.halflife_days.
  - The IEPR forward-MW layer (app/pipeline/iepr.py), discounted by
    iepr.churn_discount because an interconnection request is not a project.

What this module does NOT do: touch app/sources/base.py's keyword_match/
keyword_category gate. A residential or retail filing near a data center is
still not a data center or industrial filing, and nothing here makes it one
— spillover only adjusts the score of a row that already cleared that gate
and became a Project.
"""
from __future__ import annotations

from sqlmodel import select

from app.config import Config
from app.models import Category, Project, utcnow
from app.normalize import normalize_county
from app.pipeline.iepr import county_forward_mw


def _decayed_mw(cfg: Config, mw: float, anchor, now=None) -> float:
    """Uses the same 0.5**(days/halflife) shape as scoring.recency_decay, but
    a separate, longer half-life (see config): that function measures
    evidence staleness, this measures how long a build still plausibly holds
    local labor-market weight."""
    halflife = cfg.get("scoring.spillover.halflife_days", 365)
    now = now or utcnow()
    days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    return mw * (0.5 ** (days / halflife))


def _own_project_mw(cfg: Config, p: Project, now) -> float:
    if p.category is not Category.data_center:
        return 0.0
    mw = p.mw_total or p.mw_it
    if not mw:
        return 0.0
    anchor = p.last_signal_at or p.updated_at
    return _decayed_mw(cfg, mw, anchor, now)


def _own_board_mw(session, cfg: Config, now) -> dict[tuple[str, str], float]:
    """(state, county) -> recency-decayed MW from Scout's own data-center
    projects that are in a qualifying status (see config)."""
    qualifying = set(cfg.get("scoring.spillover.qualifying_statuses",
                             ["active", "contacted", "specified", "bidding", "won"]))
    totals: dict[tuple[str, str], float] = {}
    projects = session.exec(
        select(Project).where(Project.category == Category.data_center)).all()
    for p in projects:
        if p.status not in qualifying:
            continue
        county = normalize_county(p.county)
        if not county or not p.state:
            continue
        decayed = _own_project_mw(cfg, p, now)
        if not decayed:
            continue
        key = (p.state, county)
        totals[key] = totals.get(key, 0.0) + decayed
    return totals


def _iepr_mw(session, cfg: Config) -> dict[tuple[str, str], float]:
    """(state, county) -> IEPR forward MW, discounted by the churn rate.
    Every row in IeprForwardLoad is CA today (see app/pipeline/iepr.py); this
    stays state-keyed rather than CA-only so a future utility's filing in
    another state slots in without a code change here."""
    discount = 1.0 - cfg.get("iepr.churn_discount", 0.0)
    by_county = county_forward_mw(session)
    return {("CA", county): mw * discount for county, mw in by_county.items()}


def county_spillover_mw(session, cfg: Config, now=None) -> dict[tuple[str, str], float]:
    """(state, county) -> total recent/queued DC MW, own board + IEPR combined.
    Raw totals — no adjacency applied yet, no per-project self-exclusion.
    Exposed separately from apply_spillover() so the Phase 4 gate report can
    show exactly what each county carries before it becomes a score bump.

    `now` is accepted so a caller that also calls project_spillover() can
    pass the SAME instant to both — two independent utcnow() calls a few
    milliseconds apart would decay a project's own contribution by a hair
    more than what actually went into these totals, leaving a sub-cent MW
    residue that is not quite zero and not quite real."""
    now = now or utcnow()
    totals = _own_board_mw(session, cfg, now)
    for key, mw in _iepr_mw(session, cfg).items():
        totals[key] = totals.get(key, 0.0) + mw
    return totals


def _neighbors(cfg: Config, state: str, county: str) -> list[str]:
    table = cfg.get("scoring.spillover.county_adjacency", {}).get(state, {})
    return table.get(county, [])


def spillover_factor(cfg: Config, mw: float) -> float:
    """MW -> multiplier, saturating at 1 + weight. Deliberately modest and
    capped: this corroborates a ranking, it does not remake one."""
    if mw <= 0:
        return 1.0
    weight = cfg.get("scoring.spillover.weight", 0.15)
    saturation = cfg.get("scoring.spillover.saturation_mw", 500)
    return 1.0 + weight * min(1.0, mw / saturation)


def project_spillover(cfg: Config, county_totals: dict[tuple[str, str], float],
                      project: Project, now=None) -> tuple[float, str | None]:
    """One project's spillover MW and basis text. Excludes the project's own
    contribution to its own county's total, so a lone data center in a county
    cannot inflate its own score off its own presence — the signal has to be
    external activity, not self-reference.

    `now` should be the SAME instant passed to county_spillover_mw() when
    building `county_totals` — see that function's docstring."""
    state, county = project.state, normalize_county(project.county)
    if not state or not county:
        return 0.0, None
    now = now or utcnow()

    # Subtract this project's own DECAYED contribution (not its raw MW) —
    # county_totals was built from decayed figures, so excluding the raw
    # value would over-subtract for an old project and leave a stale row
    # crediting itself the same residual as a fresh one. See _own_project_mw.
    # Only subtract if this project was itself qualifying — a non-qualifying
    # project (e.g. status="dead") never contributed to county_totals in the
    # first place, so subtracting anything here would double-discount.
    qualifying = set(cfg.get("scoring.spillover.qualifying_statuses",
                             ["active", "contacted", "specified", "bidding", "won"]))
    own_mw = _own_project_mw(cfg, project, now) if project.status in qualifying else 0.0

    # Rounded before comparing to zero: two calls to _own_project_mw (one
    # folded into county_totals, one here) can differ by float dust even
    # with a shared `now`, and a sub-thousandth-MW residue is not a real
    # spillover signal.
    same_county = max(0.0, round(county_totals.get((state, county), 0.0) - own_mw, 6))
    neighbors = _neighbors(cfg, state, county)
    neighbor_mw = sum(county_totals.get((state, n), 0.0) for n in neighbors)
    total = round(same_county + neighbor_mw, 6)
    if total <= 0:
        return 0.0, None

    factor = spillover_factor(cfg, total)
    bonus_pct = round((factor - 1.0) * 100)
    contributing = [n for n in neighbors if county_totals.get((state, n), 0.0) > 0]
    if contributing:
        label = "county" if len(contributing) == 1 else "counties"
        basis = (f"{total:,.0f} MW recent/queued DC activity in {county} + "
                 f"{len(contributing)} adjacent {label} → +{bonus_pct}% score")
    else:
        basis = f"{total:,.0f} MW recent/queued DC activity in {county} → +{bonus_pct}% score"
    return round(total, 1), basis
