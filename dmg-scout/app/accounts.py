"""Accounts module: line-card coverage for the ~100 dormant accounts, dollar-
ranked gap scoring against a tunable adjacency table, and replacement windows
off the existing ownership-branched service-life table. Three questions per
account — what they have, what they should have, what is forcing them to buy —
answered by three functions here: owned_categories/compute_gaps and
account_replacement_windows.

Deliberately separate from the project pipeline: nothing in this module reads
or writes Project, Signal, or anything under app/pipeline/. The only bridge is
Account.firm_id, an optional link to the existing Firm roster, used solely to
show live Scout projects naming this account on its brief.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.config import Config
from app.firms import match_firm
from app.models import (
    ACTIVE_STATUSES,
    Account,
    AccountCoverage,
    Firm,
    ProductLine,
    Project,
    ProjectFirm,
    utcnow,
)
from app.normalize import normalize_name
from app.replacement import ServiceLife, UnknownEquipment, replacement_basis, service_life

log = logging.getLogger(__name__)

BOUGHT = "bought"
QUOTED_NOT_WON = "quoted_not_won"
NEVER_QUOTED = "never_quoted"
UNKNOWN = "unknown"
COVERAGE_STATUSES = (BOUGHT, QUOTED_NOT_WON, NEVER_QUOTED, UNKNOWN)

ACCOUNT_TYPES = (
    "mechanical_contractor", "service_contractor", "gc", "owner", "developer",
    "distributor", "engineer",
)


# ---- seeding ---------------------------------------------------------------

def seed_product_lines(session: Session, cfg: Config) -> int:
    """Load the line card from config.yaml into product_lines. Idempotent,
    same shape as app.firms.seed_firms: keyed on normalized name, config wins
    over anything already in the row on every field, insert only if new."""
    added = 0
    for entry in cfg.get("accounts.line_card", []) or []:
        name = entry["name"]
        norm = normalize_name(name)
        existing = session.exec(select(ProductLine).where(ProductLine.name_norm == norm)).first()
        fields = {
            "firm": entry.get("firm", "DMG"), "category": entry["category"],
            "subcategory": entry.get("subcategory", ""), "description": entry.get("description", ""),
            "value_tier": int(entry.get("value_tier", 3)), "equipment_type": entry.get("equipment_type"),
            "heat_rejection_mode": entry.get("heat_rejection_mode"),
            "heat_rejection_mode_verified": bool(entry.get("heat_rejection_mode_verified", False)),
            "heat_rejection_mode_basis": entry.get("heat_rejection_mode_basis"),
        }
        if existing:
            changed = any(getattr(existing, k) != v for k, v in fields.items())
            if changed:
                for k, v in fields.items():
                    setattr(existing, k, v)
                session.add(existing)
            continue
        session.add(ProductLine(name=name, name_norm=norm, **fields))
        added += 1
    session.commit()
    return added


def ensure_coverage_rows(session: Session, account: Account) -> int:
    """Create a row for every product line this account has no coverage row for
    yet, status='unknown'. Called on account creation, and safe to call again
    (e.g. after `scout seed-lines` adds a new line) — only fills gaps, never
    touches an existing row.

    This is what makes the account page usable by hand from the first click:
    every line is already a row waiting for a status, not something a rep has
    to add before they can mark it.
    """
    existing_line_ids = {
        c.product_line_id for c in session.exec(
            select(AccountCoverage).where(AccountCoverage.account_id == account.id)).all()
    }
    added = 0
    for line in session.exec(select(ProductLine)).all():
        if line.id in existing_line_ids:
            continue
        session.add(AccountCoverage(account_id=account.id, product_line_id=line.id))
        added += 1
    session.commit()
    return added


def create_account(session: Session, **fields) -> Account:
    """Create an account, auto-linking Firm by normalized name if an existing
    roster entry matches (never creates a new Firm — that roster is the
    project pipeline's, and an account with no pipeline match yet is a normal,
    common case, not an error), then populate its coverage rows."""
    name = fields["name"]
    account = Account(name=name, name_norm=normalize_name(name), **{
        k: v for k, v in fields.items() if k != "name"
    })
    if account.firm_id is None:
        firm = match_firm(session, name)
        if firm is not None:
            account.firm_id = firm.id
    session.add(account)
    session.commit()
    session.refresh(account)
    ensure_coverage_rows(session, account)
    return account


# ---- adjacency scoring -------------------------------------------------

_AFFINITY_CACHE: dict[int, dict[tuple[str, str], float]] = {}


def _affinity_table(cfg: Config) -> dict[tuple[str, str], float]:
    """Sparse edges from config, expanded into a symmetric lookup dict, cached
    per Config instance — same pattern as _esco_match's pattern cache in
    app/sources/base.py, for the same reason: this gets called once per
    category pair per gap, on every account page load."""
    key = id(cfg)
    table = _AFFINITY_CACHE.get(key)
    if table is not None:
        return table
    table = {}
    for edge in cfg.get("accounts.adjacency.edges", []) or []:
        a, b, weight = edge[0], edge[1], float(edge[2])
        table[(a, b)] = weight
        table[(b, a)] = weight
    _AFFINITY_CACHE[key] = table
    return table


def category_affinity(cfg: Config, cat_a: str, cat_b: str) -> float:
    """0-1: how relevant category b is to an account that already owns
    category a. 0 for any pair not in accounts.adjacency.edges — an unlisted
    pair is "no fit", not "not yet rated"."""
    if cat_a == cat_b:
        return 0.0  # a category is not a gap against itself
    return _affinity_table(cfg).get((cat_a, cat_b), 0.0)


def account_type_modifier(cfg: Config, account_type: str, category: str) -> float:
    """How plausible it is that THIS account type buys in `category` at all,
    independent of what they already own — see accounts.adjacency.
    account_type_modifier in config.yaml."""
    table = cfg.get("accounts.adjacency.account_type_modifier", {}) or {}
    entry = table.get(account_type)
    if entry is None:
        return float(table.get("default", 1.0))
    if category in entry:
        return float(entry[category])
    return float(entry.get("default", table.get("default", 1.0)))


def value_tier_band(cfg: Config, tier: int) -> tuple[float, float]:
    table = cfg.get("accounts.value_tier_dollars", {}) or {}
    entry = table.get(tier) or table.get(str(tier))
    if not entry:
        return (0.0, 0.0)
    return (float(entry["low"]), float(entry["high"]))


def owned_categories(session: Session, account_id: int) -> set[str]:
    """Categories this account has at least one product line marked 'bought'
    in. Quoted-and-lost or never-quoted do not establish ownership for
    adjacency purposes — only a completed sale tells you what this account's
    buildings/scope actually run."""
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status == BOUGHT,
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()
    return {line.category for _cov, line in rows}


@dataclass
class Gap:
    line: ProductLine
    coverage_status: str  # never_quoted | quoted_not_won | unknown — always != bought
    relevance: float
    value_low: float
    value_high: float

    @property
    def value_mid(self) -> float:
        return (self.value_low + self.value_high) / 2

    @property
    def gap_score(self) -> float:
        return self.relevance * self.value_mid


def compute_gaps(session: Session, cfg: Config, account_id: int) -> list[Gap]:
    """What this account should have but does not, ranked by estimated dollar
    value — a missing tier-1 line at moderate relevance can and should outrank
    a missing tier-4 line at high relevance, because the question is "what is
    the biggest number on the table I'm not touching", not "what fits best".

    Relevance below accounts.adjacency.min_relevance_to_show drops the line
    from the list entirely rather than showing it at the bottom — this is the
    literal mechanism behind "a rooftop-only account has no use for Seresco
    natatorium units": it does not appear, full stop.
    """
    owned = owned_categories(session, account_id)
    account = session.get(Account, account_id)
    no_coverage_base = float(cfg.get("accounts.adjacency.no_coverage_base", 0.3))
    floor = float(cfg.get("accounts.adjacency.min_relevance_to_show", 0.15))

    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status != BOUGHT,
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()

    gaps = []
    for cov, line in rows:
        if owned:
            base = max((category_affinity(cfg, o, line.category) for o in owned), default=0.0)
        else:
            base = no_coverage_base
        relevance = base * account_type_modifier(cfg, account.account_type, line.category)
        if relevance < floor:
            continue
        low, high = value_tier_band(cfg, line.value_tier)
        gaps.append(Gap(line=line, coverage_status=cov.status, relevance=relevance,
                        value_low=low, value_high=high))
    gaps.sort(key=lambda g: g.gap_score, reverse=True)
    return gaps


# ---- replacement windows ----------------------------------------------

@dataclass
class ReplacementWindow:
    line: ProductLine
    install_year: int
    age_years: float
    service_life: ServiceLife
    status: str  # overdue | due | approaching | not_due
    basis: str
    # Calendar year the equipment enters (low) and exits (high) its window,
    # for a rep who wants a date rather than an age to carry into a meeting.
    window_start_year: int
    window_end_year: int


def account_replacement_windows(session: Session, cfg: Config, account_id: int,
                                include_not_due: bool = False) -> list[ReplacementWindow]:
    """What is forcing this account to buy: every covered line with a stated
    install year AND an equipment_type this system has a service-life band
    for, scored against replacement.service_life — the SAME table and the
    SAME ownership branch app/replacement.py already established, not a new
    number. A line with no install year, or whose equipment_type is null
    (most of the line card — diffusers, controls, filters have no ASHRAE-style
    service life on file), is simply not in this list; that is the "abstain
    rather than invent" rule replacement.py already applies to unknown
    equipment/ownership, applied here to unknown install year too.
    """
    account = session.get(Account, account_id)
    now_year = utcnow().year
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              AccountCoverage.status == BOUGHT,
              AccountCoverage.install_year.is_not(None),
              ProductLine.id == AccountCoverage.product_line_id)
    ).all()

    windows = []
    for cov, line in rows:
        if not line.equipment_type:
            continue
        try:
            sl = service_life(cfg, line.equipment_type, account.ownership_type)
        except UnknownEquipment:
            continue
        age = float(now_year - cov.install_year)
        status = sl.status(age)
        if status == "not_due" and not include_not_due:
            continue
        windows.append(ReplacementWindow(
            line=line, install_year=cov.install_year, age_years=age, service_life=sl,
            status=status, basis=replacement_basis(sl, age),
            window_start_year=cov.install_year + sl.low,
            window_end_year=cov.install_year + sl.high,
        ))
    # Most urgent first: overdue, then due, then approaching.
    order = {"overdue": 0, "due": 1, "approaching": 2, "not_due": 3}
    windows.sort(key=lambda w: (order.get(w.status, 9), -w.age_years))
    return windows


# ---- live Scout projects -----------------------------------------------

def live_scout_projects(session: Session, account: Account) -> list[dict]:
    """Active Scout board projects naming this account's company, via the Firm
    bridge — firm_id if set, else a normalized-name fallback so an account
    that was never explicitly linked still shows up if the roster already
    knows the name. No LLM, no fuzzy match beyond what Firm's own aliases
    already carry.
    """
    firm = session.get(Firm, account.firm_id) if account.firm_id else None
    if firm is None:
        firm = match_firm(session, account.name)
    if firm is None:
        return []
    links = session.exec(select(ProjectFirm).where(ProjectFirm.firm_id == firm.id)).all()
    out = []
    for link in links:
        project = session.get(Project, link.project_id)
        if project is None or project.status not in ACTIVE_STATUSES:
            continue
        out.append({"project": project, "role": link.role})
    out.sort(key=lambda r: -r["project"].score)
    return out


# ---- coverage summary + brief -------------------------------------------

def coverage_summary(session: Session, account_id: int) -> dict:
    rows = session.exec(
        select(AccountCoverage, ProductLine)
        .where(AccountCoverage.account_id == account_id,
              ProductLine.id == AccountCoverage.product_line_id)
        .order_by(ProductLine.category, ProductLine.name)
    ).all()
    by_status: dict[str, list] = {s: [] for s in COVERAGE_STATUSES}
    for cov, line in rows:
        by_status[cov.status].append({"coverage": cov, "line": line})
    return {
        "rows": rows,
        "by_status": by_status,
        "counts": {s: len(by_status[s]) for s in COVERAGE_STATUSES},
    }


@dataclass
class AccountBrief:
    account: Account
    coverage: dict
    gaps: list[Gap]
    replacement_windows: list[ReplacementWindow]
    live_projects: list[dict]
    generated_at: object = field(default_factory=utcnow)


def build_account_brief(session: Session, cfg: Config, account_id: int) -> AccountBrief:
    """Everything that goes on the printable one-pager: what they buy, what
    they don't, the ranked gaps with dollar estimates, replacement windows
    with dates, and any live Scout project naming them. See app/brief.py for
    the project-pipeline equivalent this deliberately mirrors."""
    account = session.get(Account, account_id)
    if account is None:
        raise ValueError(f"no account {account_id}")
    return AccountBrief(
        account=account,
        coverage=coverage_summary(session, account_id),
        gaps=compute_gaps(session, cfg, account_id),
        replacement_windows=account_replacement_windows(session, cfg, account_id),
        live_projects=live_scout_projects(session, account),
    )
