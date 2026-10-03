"""Evidence packets from the point-in-time lake (production ``PacketSource``).

Every read goes through ``PIT.latest`` or an ``as_of`` query, so a packet only
contains what was knowable at the review's as-of date.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pandas as pd

from committee.agents.base_rate_table import size_bucket
from committee.agents.packets import SecurityInfo
from committee.config.schema import ScenariosConfig
from committee.data.pit import PIT, to_utc
from committee.signals.lazy_prices import lazy_prices

FUND_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "cfo",
    "capex",
    "total_assets",
    "total_liabilities",
    "equity",
    "cash",
    "debt",
    "shares_outstanding",
    "interest_expense",
    "ebit",
)
MACRO_SERIES = (
    "DFF",
    "DGS10",
    "DGS2",
    "T10Y3M",
    "T10YIE",
    "THREEFYTP10",
    "CPIAUCSL",
    "PCEPILFE",
    "UNRATE",
    "DCOILBRENTEU",
    "DTWEXBGS",
)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _records(df: pd.DataFrame, cols: list[str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = []
    for r in df[[c for c in cols if c in df.columns]].to_dict("records"):
        rec: dict[str, Any] = {}
        for key, v in r.items():
            k = str(key)
            if isinstance(v, pd.Timestamp):
                rec[k] = (
                    v.date().isoformat()
                    if k.endswith("date") or k in ("period_end", "obs_date")
                    else v.isoformat()
                )
            elif isinstance(v, float) and math.isnan(v):
                rec[k] = None
            else:
                rec[k] = v
        out.append(rec)
    return out


class PITSource:
    def __init__(self, pit: PIT, scenarios: ScenariosConfig | None = None) -> None:
        self.pit = pit
        self.scenarios = scenarios

    # -------------------------------------------------------------- security
    def security(self, security_id: str, asof: dt.date) -> SecurityInfo:
        sm = self.pit.latest("security_master", asof, "security_id = $sid", sid=security_id)
        if sm.empty:
            raise KeyError(f"{security_id} not in the security master as of {asof}")
        r = sm.iloc[0]
        hist = self.pit.latest("ticker_history", asof, "security_id = $sid", sid=security_id)
        aliases = (
            sorted({str(t) for t in hist["ticker"]} - {str(r["ticker"])}) if not hist.empty else []
        )
        return SecurityInfo(
            security_id=security_id,
            ticker=str(r["ticker"]),
            name=str(r["name"]),
            sector=str(r["sector"]),
            size_bucket=size_bucket(self.market_cap(security_id, asof)),
            aliases=aliases,
        )

    def last_close(self, security_id: str, asof: dt.date) -> float | None:
        px = (
            self.pit.query(
                "SELECT close FROM prices_daily WHERE as_of(known_time, $asof) AND security_id = $sid "
                "QUALIFY row_number() OVER (PARTITION BY date ORDER BY known_time DESC) = 1 ORDER BY date DESC LIMIT 1",
                asof,
                sid=security_id,
            )
            if self.pit.has("prices_daily")
            else pd.DataFrame()
        )
        return None if px.empty else _num(px.iloc[0]["close"])

    def market_cap(self, security_id: str, asof: dt.date) -> float | None:
        close = self.last_close(security_id, asof)
        f = self.pit.latest(
            "fundamentals",
            asof,
            "security_id = $sid AND metric = 'shares_outstanding'",
            sid=security_id,
        )
        if close is None or f.empty:
            return None
        shares = _num(f.sort_values("period_end").iloc[-1]["value"])
        return None if shares is None else close * shares

    # ---------------------------------------------------------------- fetch
    def fetch(self, kind: str, security_id: str, asof: dt.date) -> list[dict[str, Any]]:
        fn = getattr(self, f"_k_{kind}", None)
        return fn(security_id, asof) if fn else []

    def _k_signals(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest("signals", asof, "security_id = $sid", sid=sid)
        if df.empty:
            return []
        df = df.sort_values("asof").groupby("signal_name").tail(1)
        return _records(df.sort_values("signal_name"), ["signal_name", "value", "zscore", "asof"])

    def _k_fundamentals(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest("fundamentals", asof, "security_id = $sid", sid=sid)
        if df.empty:
            return []
        df = df[df["metric"].isin(FUND_METRICS) & df["fiscal_period"].astype(str).str.contains("Q")]
        keep = sorted(df["period_end"].unique())[-12:]
        return _records(
            df[df["period_end"].isin(keep)].sort_values(["metric", "period_end"]),
            ["metric", "fiscal_period", "period_end", "value"],
        )

    def _ttm(self, sid: str, asof: dt.date, metric: str) -> float | None:
        df = self.pit.latest(
            "fundamentals", asof, "security_id = $sid AND metric = $m", sid=sid, m=metric
        )
        if df.empty:
            return None
        q = df[df["fiscal_period"].astype(str).str.contains("Q")].sort_values("period_end")
        if len(q) >= 4:
            return float(q["value"].tail(4).astype(float).sum())
        fy = df[df["fiscal_period"].astype(str).str.startswith("FY")].sort_values("period_end")
        return _num(fy.iloc[-1]["value"]) if not fy.empty else None

    def _latest(self, sid: str, asof: dt.date, metric: str) -> float | None:
        df = self.pit.latest(
            "fundamentals", asof, "security_id = $sid AND metric = $m", sid=sid, m=metric
        )
        return None if df.empty else _num(df.sort_values("period_end").iloc[-1]["value"])

    def _k_valuation(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        mcap = self.market_cap(sid, asof)
        if mcap is None:
            return []
        debt, cash = self._latest(sid, asof, "debt") or 0.0, self._latest(sid, asof, "cash") or 0.0
        ev = mcap + debt - cash
        ebit = self._ttm(sid, asof, "ebit") or self._ttm(sid, asof, "operating_income")
        rev = self._ttm(sid, asof, "revenue")
        cfo, capex = self._ttm(sid, asof, "cfo"), self._ttm(sid, asof, "capex")
        fcf = (cfo - abs(capex)) if cfo is not None and capex is not None else None
        ni = self._ttm(sid, asof, "net_income")
        book = self._latest(sid, asof, "equity")

        def ratio(a: float | None, b: float | None) -> float | None:
            return round(a / b, 3) if a is not None and b is not None and b > 0 else None

        return [
            {
                "computed_by": "code (point-in-time)",
                "market_cap_musd": round(mcap / 1e6, 1),
                "ev_musd": round(ev / 1e6, 1),
                "ev_ebit": ratio(ev, ebit),
                "ev_sales": ratio(ev, rev),
                "p_fcf": ratio(mcap, fcf),
                "p_e": ratio(mcap, ni),
                "p_b": ratio(mcap, book),
                "note": "history percentiles and peer medians unavailable: insufficient evidence",
            }
        ]

    def _k_insider_txns(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest(
            "insider_txns",
            asof,
            "security_id = $sid AND txn_date >= $since",
            sid=sid,
            since=asof - dt.timedelta(days=365),
        )
        return _records(
            df.sort_values("txn_date"),
            [
                "txn_date",
                "filed_at",
                "txn_code",
                "acquired_disposed",
                "shares",
                "price",
                "role",
                "officer_title",
                "is_10b5_1",
                "is_derivative",
                "shares_owned_after",
            ],
        )

    def _k_filing_diffs(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        sections = self.pit.latest("filing_sections", asof, "security_id = $sid", sid=sid)
        if sections.empty:
            return []
        res = lazy_prices(sections, sid)
        if res is None:
            return []
        out = []
        for item, d in sorted(res.diffs.items()):
            out.append(
                {
                    "form": res.form,
                    "item": item,
                    "similarity": res.item_similarity.get(item),
                    "added": "\n\n".join(getattr(d, "added", [])[:5]),
                    "removed": "\n\n".join(getattr(d, "removed", [])[:5]),
                }
            )
        return out

    def _k_filings_8k(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest(
            "filings", asof, "security_id = $sid AND form IN ('8-K', '8-K/A')", sid=sid
        )
        if df.empty:
            return []
        df = df[
            pd.to_datetime(df["accepted_at"], utc=True) >= to_utc(asof - dt.timedelta(days=365))
        ]
        return _records(df.sort_values("accepted_at"), ["accepted_at", "form", "items"])

    def _k_news(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest("news", asof, "security_id = $sid", sid=sid)
        if df.empty:
            return []
        df = df[
            pd.to_datetime(df["published_at"], utc=True) >= to_utc(asof - dt.timedelta(days=30))
        ]
        return _records(
            df.sort_values("published_at").tail(40),
            ["published_at", "headline", "summary", "publisher", "source_url"],
        )

    def _k_macro(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest("macro_series", asof)
        if df.empty:
            return []
        df = df[df["series_id"].isin(MACRO_SERIES)].sort_values("obs_date")
        rows = []
        for _, g in df.groupby("series_id"):
            last = g.iloc[-1]
            earlier = g[
                pd.to_datetime(g["obs_date"])
                <= pd.Timestamp(last["obs_date"]) - pd.Timedelta(days=90)
            ]
            rows += _records(
                pd.DataFrame([last] + ([earlier.iloc[-1]] if not earlier.empty else [])),
                ["series_id", "obs_date", "value"],
            )
        return rows

    def _k_risk_indexes(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        df = self.pit.latest("risk_indexes", asof)
        if df.empty:
            return []
        return _records(
            df.sort_values("obs_date").groupby("index_id").tail(1),
            ["index_id", "obs_date", "value"],
        )

    def _k_scenarios(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        if self.scenarios is None:
            return []
        return [
            {"id": s.id, "definition": s.definition, "probability": s.probability}
            for s in self.scenarios.scenarios
        ]

    def _k_prices(self, sid: str, asof: dt.date) -> list[dict[str, Any]]:
        if not self.pit.has("prices_daily"):
            return []
        df = self.pit.query(
            "SELECT date, adj_close FROM prices_daily WHERE as_of(known_time, $asof) AND security_id = $sid AND date >= $since "
            "QUALIFY row_number() OVER (PARTITION BY date ORDER BY known_time DESC) = 1 ORDER BY date",
            asof,
            sid=sid,
            since=asof - dt.timedelta(days=370),
        )
        return _records(df, ["date", "adj_close"])

    # ------------------------------------------------------------ name facts
    def trading_facts(self, sid: str, asof: dt.date) -> dict[str, float | None]:
        """Price, 60-day ADV and 1-year annualized vol for the risk engine."""
        if not self.pit.has("prices_daily"):
            return {"price": None, "adv_usd": None, "annual_vol": None}
        df = self.pit.query(
            "SELECT date, close, adj_close, volume FROM prices_daily WHERE as_of(known_time, $asof) AND security_id = $sid "
            "AND date >= $since QUALIFY row_number() OVER (PARTITION BY date ORDER BY known_time DESC) = 1 ORDER BY date",
            asof,
            sid=sid,
            since=asof - dt.timedelta(days=380),
        )
        if df.empty:
            return {"price": None, "adv_usd": None, "annual_vol": None}
        adv = float((df["close"] * df["volume"]).tail(60).mean())
        rets = df["adj_close"].astype(float).pct_change().dropna().tail(252)
        vol = float(rets.std() * math.sqrt(252)) if len(rets) > 20 else None
        return {"price": float(df["close"].iloc[-1]), "adv_usd": adv, "annual_vol": vol}
