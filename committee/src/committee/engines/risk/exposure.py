"""Current-book aggregates the limit checks need.

Denominators (documented choice):

* "% of satellite" limits are measured against **satellite capital**
  ``S = satellite_target_pct x total account value``: the allocator-controlled
  budget. This keeps limits meaningful while the satellite is being built
  (an empty satellite would otherwise make any first position 100%).
* "% of total" limits use total account value (including cash).
* Satellite holdings are those whose ``sleeve`` is ``satellite`` or
  ``speculative`` (the speculative sleeve is carved out of the satellite).
* Theme look-through for core holdings uses ``PolicyPortfolio.lookthrough``
  weights per ETF symbol; satellite holdings use their own theme tags.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from committee.domain import Holding, Trade
from committee.engines.scenario.sensitivity import SATELLITE_SLEEVES, normalize_sector

UNKNOWN_SECTOR = "unknown"


def sector_key(sector: str | None) -> str:
    return normalize_sector(sector) or UNKNOWN_SECTOR


@dataclass(frozen=True)
class Book:
    total_value: float
    satellite_capital: float
    satellite_holdings: list[Holding]
    position_mv: dict[str, float]  # satellite symbol -> market value
    held_mv: dict[str, float]  # any sleeve symbol -> market value
    bucket_of: dict[str, str | None]
    sector_mv: dict[str, float]
    theme_sat_mv: dict[str, float]  # theme -> exposed satellite dollars
    theme_core_mv: dict[str, float]  # theme -> exposed core (look-through) dollars
    speculative_mv: float
    theme_sat_by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)
    theme_core_by_symbol: dict[str, dict[str, float]] = field(default_factory=dict)
    opened_on: dict[str, dt.date] = field(default_factory=dict)  # earliest open, any sleeve

    @property
    def names(self) -> set[str]:
        return {s for s, v in self.position_mv.items() if v > 0}

    @property
    def satellite_mv(self) -> float:
        return sum(self.position_mv.values())

    def asym_names(self) -> set[str]:
        return {s for s in self.names if self.bucket_of.get(s) == "asymmetric_bet"}

    def asym_mv(self) -> float:
        return sum(
            v for s, v in self.position_mv.items() if self.bucket_of.get(s) == "asymmetric_bet"
        )


def build_book(
    holdings: Iterable[Holding],
    total_value: float,
    satellite_target_pct: float,
    lookthrough: Mapping[str, Mapping[str, float]] | None = None,
) -> Book:
    lt = lookthrough or {}
    sat: list[Holding] = []
    position_mv: dict[str, float] = {}
    held_mv: dict[str, float] = {}
    bucket_of: dict[str, str | None] = {}
    sector_mv: dict[str, float] = {}
    theme_sat: dict[str, float] = {}
    theme_core: dict[str, float] = {}
    spec = 0.0
    sat_by: dict[str, dict[str, float]] = {}
    core_by: dict[str, dict[str, float]] = {}
    opened: dict[str, dt.date] = {}
    for h in holdings:
        mv = h.market_value
        held_mv[h.symbol] = held_mv.get(h.symbol, 0.0) + mv
        if h.opened_on is not None:
            prev = opened.get(h.symbol)
            opened[h.symbol] = h.opened_on if prev is None else min(prev, h.opened_on)
        if h.sleeve in SATELLITE_SLEEVES:
            sat.append(h)
            position_mv[h.symbol] = position_mv.get(h.symbol, 0.0) + mv
            if bucket_of.get(h.symbol) is None:
                bucket_of[h.symbol] = h.bucket
            key = sector_key(h.sector)
            sector_mv[key] = sector_mv.get(key, 0.0) + mv
            for t, f in h.themes.items():
                theme_sat[t] = theme_sat.get(t, 0.0) + mv * f
                row = sat_by.setdefault(h.symbol, {})
                row[t] = row.get(t, 0.0) + mv * f
            if h.sleeve == "speculative":
                spec += mv
        else:
            for t, f in lt.get(h.symbol, {}).items():
                theme_core[t] = theme_core.get(t, 0.0) + mv * f
                row = core_by.setdefault(h.symbol, {})
                row[t] = row.get(t, 0.0) + mv * f
    return Book(
        total_value=total_value,
        satellite_capital=total_value * satellite_target_pct / 100.0,
        satellite_holdings=sat,
        position_mv=position_mv,
        held_mv=held_mv,
        bucket_of=bucket_of,
        sector_mv=sector_mv,
        theme_sat_mv=theme_sat,
        theme_core_mv=theme_core,
        speculative_mv=spec,
        theme_sat_by_symbol=sat_by,
        theme_core_by_symbol=core_by,
        opened_on=opened,
    )


def turnover_from_trades(
    trades: Iterable[Trade],
    satellite_symbols: Iterable[str],
    as_of: dt.date,
    satellite_capital: float,
    window_days: int = 365,
) -> float:
    """Trailing satellite turnover, % of satellite capital.

    Turnover = sell notional in satellite symbols over the trailing window
    (``as_of - window_days < traded_on <= as_of``) / satellite capital. Sales
    are used (not buys) so funding new money does not count as turnover.
    """
    if satellite_capital <= 0:
        raise ValueError("satellite_capital must be positive")
    symbols = set(satellite_symbols)
    start = as_of - dt.timedelta(days=window_days)
    sold = sum(
        t.qty * t.price
        for t in trades
        if t.side == "sell" and t.symbol in symbols and start < t.traded_on <= as_of
    )
    return 100.0 * sold / satellite_capital
