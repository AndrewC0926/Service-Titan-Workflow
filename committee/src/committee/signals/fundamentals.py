"""Per-security fundamentals snapshot (TTM flows, latest balances) as of a date.

Conventions (``fundamentals`` table, one row per security/metric/fiscal_period):

* ``fiscal_period`` is ``YYYYQn`` (discrete quarter) or ``FYYYYY`` (fiscal year).
* Flow metrics use TTM = sum of the last 4 consecutive fiscal quarters when available.
  A missing Q4 is derived as FY - Q1 - Q2 - Q3 (10-K filers rarely tag Q4). If the
  latest fiscal year ends after the last available 4-quarter run, the FY value is used.
  The prior-year flow is the same construction ending 4 quarters (or 1 FY) earlier.
* Balance metrics use the value at the latest ``period_end``; the year-ago value is the
  one whose period_end is closest to 365 days earlier (within 60 days).
* ``capex`` is treated as a magnitude (outflow); FCF = cfo - |capex|.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from committee.signals.common import date_col, num, safe_div

FLOW_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "cfo",
    "capex",
    "ebit",
    "ebitda",
    "interest_expense",
)
STOCK_METRICS = (
    "total_assets",
    "total_liabilities",
    "equity",
    "cash",
    "debt",
    "shares_outstanding",
    "book_value",
)
TAX_RATE = 0.21  # statutory rate for NOPAT in ROIC
_Q = re.compile(r"^(\d{4})Q([1-4])$")
_FY = re.compile(r"^FY(\d{4})$")


@dataclass(frozen=True)
class Flow:
    value: float | None
    prior: float | None
    basis: str | None  # "ttm" | "fy" | None


def _flow(rows: pd.DataFrame) -> Flow:
    """TTM (or FY) value and its year-earlier counterpart for one metric."""
    quarters: dict[int, float] = {}
    years: dict[int, float] = {}
    for fp, v in zip(rows["fiscal_period"].astype(str), rows["value"], strict=True):
        x = num(v)
        if x is None:
            continue
        if m := _Q.match(fp):
            quarters[int(m[1]) * 4 + int(m[2]) - 1] = x
        elif m := _FY.match(fp):
            years[int(m[1])] = x
    for y, total in years.items():  # derive missing Q4
        q = [quarters.get(y * 4 + i) for i in range(3)]
        if y * 4 + 3 not in quarters and all(v is not None for v in q):
            quarters[y * 4 + 3] = total - sum(v for v in q if v is not None)

    def ttm(end: int) -> float | None:
        vals = [quarters.get(end - i) for i in range(4)]
        return sum(v for v in vals if v is not None) if all(v is not None for v in vals) else None

    runs = [e for e in sorted(quarters, reverse=True) if ttm(e) is not None]
    last_fy = max(years) if years else None
    if runs and (last_fy is None or runs[0] >= last_fy * 4 + 3):
        return Flow(ttm(runs[0]), ttm(runs[0] - 4), "ttm")
    if last_fy is not None:
        return Flow(years[last_fy], years.get(last_fy - 1), "fy")
    return Flow(None, None, None)


def _stock(rows: pd.DataFrame) -> tuple[float | None, float | None]:
    """(latest value, value about one year earlier) for a balance metric."""
    r = rows.assign(pe=date_col(rows, "period_end")).dropna(subset=["pe"])
    r = r[r["value"].map(num).notna()].sort_values("pe")
    if r.empty:
        return None, None
    latest = r.iloc[-1]
    target = latest["pe"] - pd.Timedelta(days=365)
    gap = (r["pe"] - target).abs()
    ok = gap[gap <= pd.Timedelta(days=60)]
    prior = num(r.loc[ok.sort_values(kind="stable").index[0], "value"]) if not ok.empty else None
    return num(latest["value"]), prior


@dataclass(frozen=True)
class Fundamentals:
    flows: dict[str, Flow]
    stocks: dict[str, float | None]
    stocks_year_ago: dict[str, float | None]

    def ttm(self, metric: str) -> float | None:
        f = self.flows.get(metric)
        return f.value if f else None

    def prior(self, metric: str) -> float | None:
        f = self.flows.get(metric)
        return f.prior if f else None

    def stock(self, metric: str) -> float | None:
        return self.stocks.get(metric)

    # ------------------------------------------------------------ derived
    @property
    def ebit(self) -> float | None:
        e = self.ttm("ebit")
        return e if e is not None else self.ttm("operating_income")

    @property
    def book(self) -> float | None:
        b = self.stock("book_value")
        return b if b is not None else self.stock("equity")

    @property
    def fcf(self) -> float | None:
        cfo, capex = self.ttm("cfo"), self.ttm("capex")
        if cfo is None:
            return None
        return cfo - abs(capex or 0.0)

    @property
    def gross_margin(self) -> float | None:
        return safe_div(self.ttm("gross_profit"), self.ttm("revenue"))

    @property
    def gross_margin_prior(self) -> float | None:
        return safe_div(self.prior("gross_profit"), self.prior("revenue"))

    @property
    def revenue_growth(self) -> float | None:
        prev, cur = self.prior("revenue"), self.ttm("revenue")
        if prev is None or cur is None or prev <= 0:
            return None
        return cur / prev - 1

    @property
    def earnings_growth(self) -> float | None:
        prev, cur = self.prior("net_income"), self.ttm("net_income")
        if prev is None or cur is None or prev <= 0:
            return None
        return cur / prev - 1

    @property
    def share_growth(self) -> float | None:
        """Trailing-12-month change in shares outstanding."""
        now, ago = (
            self.stocks.get("shares_outstanding"),
            self.stocks_year_ago.get("shares_outstanding"),
        )
        if now is None or ago is None or ago <= 0:
            return None
        return now / ago - 1


EMPTY = Fundamentals({}, {}, {})


def fundamentals_by_security(df: pd.DataFrame) -> dict[str, Fundamentals]:
    out: dict[str, Fundamentals] = {}
    if df.empty:
        return out
    for sid, g in df.groupby("security_id"):
        by_metric = dict(tuple(g.groupby("metric")))
        flows = {m: _flow(by_metric[m]) for m in FLOW_METRICS if m in by_metric}
        stocks: dict[str, float | None] = {}
        ago: dict[str, float | None] = {}
        for m in STOCK_METRICS:
            if m in by_metric:
                stocks[m], ago[m] = _stock(by_metric[m])
        out[str(sid)] = Fundamentals(flows, stocks, ago)
    return out


@dataclass(frozen=True)
class FactorMetrics:
    """Raw value and quality inputs; orientation noted per field."""

    earnings_yield: float | None  # higher better
    fcf_yield: float | None  # higher better
    book_to_market: float | None  # higher better
    ebit_to_ev: float | None  # = 1 / (EV/EBIT); higher better (handles negative EBIT)
    gross_profitability: float | None  # gross_profit / total_assets; higher better
    roic: float | None  # EBIT*(1-21%) / (equity + debt - cash); higher better
    accruals: float | None  # (net_income - cfo) / total_assets; LOWER better
    leverage: float | None  # debt / total_assets; LOWER better


def factor_metrics(f: Fundamentals, market_cap: float | None) -> FactorMetrics:
    debt, cash = f.stock("debt"), f.stock("cash")
    assets = f.stock("total_assets")
    ev = None if market_cap is None else market_cap + (debt or 0.0) - (cash or 0.0)
    ebit = f.ebit
    equity = f.stock("equity")
    invested = None if equity is None else equity + (debt or 0.0) - (cash or 0.0)
    ni, cfo = f.ttm("net_income"), f.ttm("cfo")
    return FactorMetrics(
        earnings_yield=safe_div(ni, market_cap),
        fcf_yield=safe_div(f.fcf, market_cap),
        book_to_market=safe_div(f.book, market_cap),
        ebit_to_ev=safe_div(ebit, ev) if ev is not None and ev > 0 else None,
        gross_profitability=safe_div(f.ttm("gross_profit"), assets),
        roic=(
            safe_div(ebit * (1 - TAX_RATE), invested)
            if ebit is not None and invested is not None and invested > 0
            else None
        ),
        accruals=safe_div(ni - cfo, assets) if ni is not None and cfo is not None else None,
        leverage=safe_div(debt, assets) if debt is not None else None,
    )
