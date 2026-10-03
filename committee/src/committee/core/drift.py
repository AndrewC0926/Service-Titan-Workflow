"""Policy-portfolio drift across all accounts (DESIGN 3)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from committee.config.schema import PolicyPortfolio
from committee.core.holdings import CASH, Position

LIQUIDITY_SLEEVE_KINDS = frozenset({"liquidity"})


@dataclass(frozen=True)
class SleeveDrift:
    sleeve_id: str
    holding: str | None
    target_pct: float
    actual_pct: float
    band_pct: float | None
    value_usd: float

    @property
    def drift_pct(self) -> float:
        return self.actual_pct - self.target_pct

    @property
    def breached(self) -> bool:
        return self.band_pct is not None and abs(self.drift_pct) > self.band_pct + 1e-9


@dataclass(frozen=True)
class DriftReport:
    total_value_usd: float
    cash_usd: float
    sleeves: list[SleeveDrift]
    unmapped: dict[str, float]  # symbol -> value counted as satellite

    @property
    def breaches(self) -> list[SleeveDrift]:
        return [s for s in self.sleeves if s.breached]


def sleeve_of(policy: PolicyPortfolio) -> dict[str, str]:
    return {s.holding: s.id for s in policy.sleeves if s.holding}


def compute_drift(
    policy: PolicyPortfolio,
    positions: Iterable[Position],
    price: Callable[[str], float],
    satellite_symbols: set[str] | None = None,
) -> DriftReport:
    """Weights by sleeve. Cash counts toward the liquidity sleeve; non-core symbols
    count as satellite (``satellite_symbols`` lists them explicitly when known)."""
    by_holding = sleeve_of(policy)
    liquidity = next((s.id for s in policy.sleeves if s.kind in LIQUIDITY_SLEEVE_KINDS), None)
    values: dict[str, float] = {s.id: 0.0 for s in policy.sleeves}
    unmapped: dict[str, float] = {}
    cash = 0.0
    for p in positions:
        v = p.qty if p.symbol == CASH else p.qty * price(p.symbol)
        if p.symbol == CASH:
            cash += v
            if liquidity:
                values[liquidity] += v
        elif p.symbol in by_holding:
            values[by_holding[p.symbol]] += v
        else:
            values["satellite"] = values.get("satellite", 0.0) + v
            if satellite_symbols is None or p.symbol not in satellite_symbols:
                unmapped[p.symbol] = unmapped.get(p.symbol, 0.0) + v
    total = sum(values.values())
    sleeves = [
        SleeveDrift(
            s.id,
            s.holding,
            s.target_pct,
            100 * values[s.id] / total if total else 0.0,
            s.band_pct,
            values[s.id],
        )
        for s in policy.sleeves
    ]
    return DriftReport(total, cash, sleeves, unmapped)
