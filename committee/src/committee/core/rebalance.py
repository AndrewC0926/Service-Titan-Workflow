"""Rebalance proposals when a core sleeve breaches its band (DESIGN 3, Prompt 13).

Order of preference for closing the gap:
1. new cash (buy underweight sleeves with idle cash, in each sleeve's preferred account),
2. tax-free swaps inside IRA / 401(k) (sell overweight, buy underweight in the same account),
3. taxable sales with the lowest tax cost (``tax_cost`` estimates dollars of tax per dollar sold).

The satellite sleeve is sized by the capital allocator, never by the rebalancer.
Proposals are journaled as ``core_proposal`` entries and go through the same
approval gate as committee briefings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from committee.config.schema import PolicyPortfolio
from committee.core.drift import DriftReport
from committee.core.holdings import CASH, Position
from committee.domain import TAX_DEFERRED

MIN_TRADE_USD = 100.0

TaxCost = Callable[[str, str, float], float]  # (symbol, account, dollars sold) -> tax dollars


@dataclass
class Leg:
    symbol: str
    side: str
    account: str
    notional_usd: float
    sleeve: str
    why: str


@dataclass
class Proposal:
    total_value_usd: float
    legs: list[Leg] = field(default_factory=list)
    est_tax_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    def payload(self, cooling_off_hours: int = 24) -> dict[str, object]:
        """Journal payload, including the fields the approval gate reads."""
        return {
            "kind": "core_rebalance",
            "recommendation": "REBALANCE" if self.legs else "HOLD",
            "cooling_off_hours": cooling_off_hours,
            "behavioral_severity": "none",
            "legs": [
                {
                    "symbol": leg.symbol,
                    "side": leg.side,
                    "account": leg.account,
                    "max_pct_total": round(100 * leg.notional_usd / self.total_value_usd, 4),
                }
                for leg in self.legs
            ],
            "detail": [leg.__dict__ for leg in self.legs],
            "est_tax_usd": round(self.est_tax_usd, 2),
            "notes": self.notes,
        }


def _no_tax(symbol: str, account: str, dollars: float) -> float:
    return 0.0


def propose_rebalance(
    policy: PolicyPortfolio,
    drift: DriftReport,
    positions: list[Position],
    price: Callable[[str], float],
    tax_cost: TaxCost = _no_tax,
    cash_reserve_usd: float = 0.0,
) -> Proposal:
    prop = Proposal(drift.total_value_usd)
    if not drift.breaches:
        prop.notes.append("all sleeves within bands")
        return prop
    total = drift.total_value_usd
    core = {
        s.id: s for s in policy.sleeves if s.kind not in ("satellite", "liquidity") and s.holding
    }
    gap = {
        d.sleeve_id: (d.target_pct - d.actual_pct) / 100 * total
        for d in drift.sleeves
        if d.sleeve_id in core
    }
    # value of each holding per account
    held: dict[tuple[str, str], float] = {}
    cash: dict[str, float] = {}
    for p in positions:
        if p.symbol == CASH:
            cash[p.account] = cash.get(p.account, 0.0) + p.qty
        else:
            held[(p.account, p.symbol)] = held.get((p.account, p.symbol), 0.0) + p.qty * price(
                p.symbol
            )
    if cash:
        first = sorted(cash)[0]
        cash[first] = max(0.0, cash[first] - cash_reserve_usd)

    def buy(sid: str, account: str, amount: float, why: str) -> float:
        amt = min(amount, gap[sid], cash.get(account, 0.0))
        if amt < MIN_TRADE_USD:
            return 0.0
        prop.legs.append(Leg(core[sid].holding or "", "buy", account, amt, sid, why))
        cash[account] -= amt
        gap[sid] -= amt
        return amt

    def sell(sid: str, account: str, amount: float, why: str) -> float:
        sym = core[sid].holding or ""
        amt = min(amount, -gap[sid], held.get((account, sym), 0.0))
        if amt < MIN_TRADE_USD:
            return 0.0
        prop.legs.append(Leg(sym, "sell", account, amt, sid, why))
        held[(account, sym)] -= amt
        cash[account] = cash.get(account, 0.0) + amt
        gap[sid] += amt
        prop.est_tax_usd += tax_cost(sym, account, amt)
        return amt

    under = sorted((s for s in gap if gap[s] > 0), key=lambda s: -gap[s])
    over = sorted((s for s in gap if gap[s] < 0), key=lambda s: gap[s])

    # 1. new cash, in each sleeve's preferred account order
    for sid in under:
        for acct in core[sid].location:
            buy(sid, acct, gap[sid], "new cash")
    # 2. tax-free swaps inside IRA / 401(k)
    for deferred in sorted(TAX_DEFERRED):
        for sid in over:
            want = sum(max(0.0, gap[u]) for u in under if deferred in core[u].location)
            if want < MIN_TRADE_USD:
                break
            sell(sid, deferred, min(-gap[sid], want), "tax-free rebalance")
        for sid in under:
            if deferred in core[sid].location:
                buy(sid, deferred, gap[sid], "tax-free rebalance")
    # 3. taxable sales, cheapest tax per dollar first
    remaining_under = sum(max(0.0, gap[u]) for u in under if "taxable" in core[u].location)
    if remaining_under >= MIN_TRADE_USD:
        candidates = sorted(over, key=lambda s: tax_cost(core[s].holding or "", "taxable", 1000.0))
        for sid in candidates:
            if remaining_under < MIN_TRADE_USD:
                break
            remaining_under -= sell(
                sid, "taxable", min(-gap[sid], remaining_under), "taxable sale (lowest tax cost)"
            )
        for sid in under:
            if "taxable" in core[sid].location:
                buy(sid, "taxable", gap[sid], "proceeds of taxable sale")
    left = {s: round(g, 2) for s, g in gap.items() if abs(g) >= MIN_TRADE_USD}
    if left:
        prop.notes.append(f"residual gaps after location-aware rebalance: {left}")
    if drift.unmapped:
        prop.notes.append(
            f"symbols not in the policy counted as satellite: {sorted(drift.unmapped)}"
        )
    return prop
