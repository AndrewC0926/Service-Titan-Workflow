"""Wiring between subsystems for the CLI, scheduler and dashboard.

Everything here is glue: it builds the real objects (lake, PIT, broker, tax
ledger, agent runtime) from an ``AppContext`` and passes them explicitly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from committee.agents.base_rate_table import (
    Profile,
    load_samples_from_pit,
    reference_class_table,
    size_bucket,
)
from committee.agents.budget import BudgetGuard
from committee.agents.llm import AnthropicClient
from committee.agents.prompts import PromptRegistry
from committee.agents.runtime import AgentRuntime
from committee.broker.models import Fill
from committee.context import AppContext
from committee.core.holdings import CASH, HoldingsStore, Position, apply_fill
from committee.data.lake import Lake, RawZone
from committee.data.pit import PIT
from committee.data.security_master import resolve
from committee.domain import AccountKind, Bucket, Holding, Lot
from committee.engines.risk import PortfolioState
from committee.engines.scenario import run_scenarios
from committee.engines.scenario.models import ScenarioReport
from committee.engines.tax.equivalence import Equivalence
from committee.engines.tax.models import SaleRequest
from committee.engines.tax.rates import TaxRates
from committee.engines.tax.selection import select_lots
from committee.engines.tax.store import TaxLedgerStore
from committee.journal.store import Journal
from committee.orchestration.cache import CachingClient
from committee.orchestration.pit_source import PITSource
from committee.orchestration.review import Orchestrator, ReviewInputs

BENCHMARK = "SPY"


def lake(ctx: AppContext) -> Lake:
    return Lake(ctx.data_dir)


def raw(ctx: AppContext) -> RawZone:
    return RawZone(ctx.raw_dir)


def pit(ctx: AppContext) -> PIT:
    return PIT(lake(ctx))


def tax_store(ctx: AppContext) -> TaxLedgerStore:
    groups = ctx.config.tax.equivalence_groups
    return TaxLedgerStore(
        ctx.state_db,
        ctx.config.tax.wash_sale_window_days,
        Equivalence.from_lists(groups) if groups else None,
    )


def price_lookup(p: PIT, asof: dt.date) -> Callable[[str], float]:
    """Last close knowable at ``asof`` by ticker; 0.0 when unknown (callers must check)."""
    src = PITSource(p)
    cache: dict[str, float] = {}

    def last(symbol: str) -> float:
        if symbol == CASH:
            return 1.0
        if symbol not in cache:
            sid = resolve(p, symbol, asof)
            cache[symbol] = (src.last_close(sid, asof) if sid else None) or 0.0
        return cache[symbol]

    return last


def satellite_buckets(journal: Journal) -> dict[str, Bucket]:
    out: dict[str, Bucket] = {}
    for e in journal.entries("briefing"):
        b = e.payload.get("bucket")
        if b in ("core_pick", "asymmetric_bet"):
            out[str(e.payload["symbol"])] = b
    return out


def positions(ctx: AppContext) -> list[Position]:
    store = HoldingsStore(ctx.state_db)
    try:
        return store.current()
    finally:
        store.close()


def portfolio_state(
    ctx: AppContext, journal: Journal, p: PIT, asof: dt.date
) -> tuple[PortfolioState, dict[AccountKind, float]]:
    pos = positions(ctx)
    price = price_lookup(p, asof)
    by_holding = {s.holding: s.id for s in ctx.config.policy_portfolio.sleeves if s.holding}
    buckets = satellite_buckets(journal)
    src = PITSource(p)
    holdings: list[Holding] = []
    cash: dict[AccountKind, float] = {}
    total = 0.0
    for x in pos:
        if x.symbol == CASH:
            cash[x.account] = cash.get(x.account, 0.0) + x.qty
            total += x.qty
            continue
        px = price(x.symbol) or (x.cost_per_share or 0.0)
        total += px * x.qty
        sleeve = by_holding.get(x.symbol, "satellite")
        sector = None
        sid = resolve(p, x.symbol, asof) if sleeve == "satellite" else None
        if sid:
            try:
                sector = src.security(sid, asof).sector
            except KeyError:
                sector = None
        holdings.append(
            Holding(
                account=x.account,
                symbol=x.symbol,
                qty=x.qty,
                price=px,
                sleeve=sleeve,
                bucket=buckets.get(x.symbol, "core_pick") if sleeve == "satellite" else None,
                sector=sector,
                opened_on=x.acquired_on,
            )
        )
    state = PortfolioState(holdings=holdings, total_value=max(total, 1.0), as_of=asof)
    return state, cash


def scenario_report(ctx: AppContext, state: PortfolioState) -> ScenarioReport | None:
    if not state.holdings:
        return None
    return run_scenarios(
        state.holdings,
        ctx.config.scenarios,
        policy=ctx.config.policy_portfolio,
        total_value=state.total_value,
    )


def runtime(ctx: AppContext, journal: Journal, run_id: str | None = None) -> AgentRuntime:
    key = ctx.secrets.require("ANTHROPIC_API_KEY")
    client = CachingClient(AnthropicClient(api_key=key), ctx.var_dir / "llm_cache.sqlite")
    budget = BudgetGuard(journal, ctx.config.models.budget)
    return AgentRuntime(
        client,
        ctx.config.models,
        PromptRegistry.load(ctx.root / "prompts"),
        run_id=run_id or f"run-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
        journal=journal,
        budget=budget,
    )


def latest_agent_weights(journal: Journal) -> dict[str, float]:
    e = journal.latest("agent_weights")
    return {str(k): float(v) for k, v in (e.payload.get("weights") or {}).items()} if e else {}


@dataclass(frozen=True)
class NameFacts:
    security_id: str
    symbol: str
    sector: str
    market_cap_usd: float
    adv_usd: float
    annual_vol: float
    price: float


def name_facts(p: PIT, symbol: str, asof: dt.date) -> NameFacts:
    sid = resolve(p, symbol, asof)
    if not sid:
        raise KeyError(
            f"{symbol}: not in the security master as of {asof} (run `committee ingest securities`)"
        )
    src = PITSource(p)
    info = src.security(sid, asof)
    t = src.trading_facts(sid, asof)
    mcap = src.market_cap(sid, asof)
    missing = [
        k
        for k, v in (
            ("price", t["price"]),
            ("adv", t["adv_usd"]),
            ("vol", t["annual_vol"]),
            ("market cap", mcap),
        )
        if not v
    ]
    if missing:
        raise KeyError(
            f"{symbol}: missing {', '.join(missing)} as of {asof} (ingest prices and EDGAR facts first)"
        )
    assert t["price"] and t["adv_usd"] and t["annual_vol"] and mcap
    return NameFacts(
        sid, info.ticker, info.sector or "Unknown", mcap, t["adv_usd"], t["annual_vol"], t["price"]
    )


def base_rate_extras(
    p: PIT, facts: NameFacts, asof: dt.date, signals: frozenset[str] = frozenset()
) -> list[dict[str, object]]:
    bench = resolve(p, BENCHMARK, asof)
    if not bench:
        return []
    samples = load_samples_from_pit(p, asof, benchmark_security_id=bench)
    if samples.empty:
        return []
    table = reference_class_table(
        samples,
        Profile(
            sector=facts.sector, size_bucket=size_bucket(facts.market_cap_usd), signals=signals
        ),
        exclude_security=facts.security_id,
    )
    return table.as_evidence()


def build_review_inputs(
    ctx: AppContext,
    journal: Journal,
    p: PIT,
    symbol: str,
    asof: dt.date,
    bucket: Bucket,
    *,
    lottery_pass: bool | None = None,
    themes: dict[str, float] | None = None,
    origin: str = "weekly screen",
) -> tuple[ReviewInputs, ScenarioReport | None]:
    facts = name_facts(p, symbol, asof)
    state, cash = portfolio_state(ctx, journal, p, asof)
    with tax_store(ctx) as ts:
        ledger = ts.ledger()
    history = [
        {
            "decided_on": e.created_at_utc[:10],
            "decision": e.payload.get("decision") or "APPROVED",
            "reason": e.payload.get("human_reason") or e.payload.get("reason"),
        }
        for e in [*journal.entries("decision"), *journal.entries("approval")]
        if (dt.datetime.now(dt.UTC) - e.created_at).days <= 365
    ]
    extras: dict[str, list[dict[str, object]]] = {
        "base_rate_table": base_rate_extras(p, facts, asof),
        "exposures": [{"theme": k, "fraction": v} for k, v in sorted((themes or {}).items())],
    }
    inp = ReviewInputs(
        security_id=facts.security_id,
        symbol=facts.symbol,
        asof=asof,
        bucket=bucket,
        sector=facts.sector,
        market_cap_usd=facts.market_cap_usd,
        adv_usd=facts.adv_usd,
        annual_vol=facts.annual_vol,
        price=facts.price,
        source=PITSource(p, ctx.config.scenarios),
        portfolio=state,
        cash_by_account=cash,
        tax_ledger=ledger,
        lottery_pass=lottery_pass,
        themes=themes or {},
        extras=extras,
        decision_history=history,
        origin=origin,
    )
    return inp, scenario_report(ctx, state)


def orchestrator(ctx: AppContext, journal: Journal, rep: ScenarioReport | None) -> Orchestrator:
    return Orchestrator(
        runtime(ctx, journal),
        journal,
        ctx.active_config(journal),
        scenario_report=rep,
        brier_weights=latest_agent_weights(journal),
    )


def fill_to_ledger(ctx: AppContext) -> Callable[[Fill], None]:
    """Reconciled fills become tax lots (buys) or specific-identification sales (sells)."""
    rates = TaxRates.from_config(ctx.config.tax)

    def apply(f: Fill) -> None:
        store = HoldingsStore(ctx.state_db)
        try:
            apply_fill(
                store,
                f.account,
                f.symbol,
                f.side,
                f.qty,
                f.price,
                f.filled_at.date(),
                dt.datetime.now(dt.UTC),
            )
        finally:
            store.close()
        with tax_store(ctx) as ts:
            day = f.filled_at.date()
            if f.side == "buy":
                ts.record_purchase(
                    Lot(
                        lot_id=f"L-{f.fill_id}",
                        account=f.account,
                        symbol=f.symbol,
                        qty=f.qty,
                        cost_per_share=f.price,
                        acquired_on=day,
                    )
                )
                return
            lots = ts.ledger().open_lots(account=f.account, symbol=f.symbol)
            sel = select_lots(list(lots), f.qty, f.price, day, rates)
            ts.record_sale(
                SaleRequest(
                    sale_id=f"S-{f.fill_id}",
                    account=f.account,
                    symbol=f.symbol,
                    sold_on=day,
                    price=f.price,
                    picks=sel.picks,
                )
            )

    return apply
