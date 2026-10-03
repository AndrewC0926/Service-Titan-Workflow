"""Weekly screen (DESIGN 6): universe -> signals -> composite -> shortlist.

Shortlist rule, in DESIGN order:
1. Eligible = universe members with a bucket; asymmetric-bucket names must also pass
   the lottery filter.
2. Take the top ``shortlist_size`` eligible names by composite (ties by security_id),
   plus every eligible name with a recent cluster buy (last ``cluster_buy_lookback_days``).
3. Remove holdings already under review and names on the wash-sale block list. Both
   lists match a security_id or a ticker (case-insensitive). Removal happens after the
   top-N cut, so the shortlist can be shorter than ``shortlist_size``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from committee.config.schema import SignalsConfig, UniverseConfig
from committee.data.pit import PIT, to_utc
from committee.signals.buckets import AsymmetricProfile, BucketName, asymmetric_profile, tag_bucket
from committee.signals.common import num
from committee.signals.composite import contributions, raw_signals, zscores
from committee.signals.flags import (
    Crowding,
    NewsCounts,
    ShortInterest,
    crowding,
    earnings_revisions,
    news_counts,
    short_interest,
)
from committee.signals.fundamentals import fundamentals_by_security
from committee.signals.inputs import SignalInputs, load_inputs
from committee.signals.insider import InsiderSignal, insider_signals
from committee.signals.lazy_prices import LazyPricesResult, lazy_prices
from committee.signals.lottery import (
    LotteryInputs,
    LotteryResult,
    lottery_filter,
    max_effect_cutoff,
)
from committee.signals.prices import price_stats
from committee.signals.universe import Member, build_universe


@dataclass(frozen=True)
class ScreenOptions:
    holdings_under_review: frozenset[str] = frozenset()
    wash_sale_blocked: frozenset[str] = frozenset()
    active_mna: frozenset[str] = frozenset()
    market_returns: pd.Series | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ScreenRow:
    security_id: str
    ticker: str
    sector: str
    bucket: BucketName | None
    market_cap: float
    adv: float
    composite: float
    contributions: dict[str, float]
    signals: dict[str, float | None]
    zscores: dict[str, float | None]
    metrics: dict[str, float | None]
    flags: dict[str, Any]
    lottery: LotteryResult | None
    asymmetric_profile: AsymmetricProfile | None
    shortlist_reason: str | None = None


@dataclass(frozen=True)
class ScreenResult:
    asof: dt.datetime
    universe_size: int
    candidates: tuple[ScreenRow, ...]  # every universe member, composite-descending
    shortlist: tuple[ScreenRow, ...]
    excluded: dict[str, str]  # security_id -> reason (universe, eligibility, removals)
    lazy_prices: dict[str, LazyPricesResult]
    earnings_revision_enabled: bool

    @property
    def asof_date(self) -> dt.date:
        return self.asof.date()


def _f(x: Any) -> float | None:
    return num(x)


def _matches(row: ScreenRow, ids: frozenset[str]) -> bool:
    upper = {i.upper() for i in ids}
    return row.security_id in ids or row.ticker.upper() in upper


def _flags(
    sig: InsiderSignal | None,
    si: ShortInterest | None,
    cr: Crowding | None,
    news: NewsCounts | None,
    lp: LazyPricesResult | None,
) -> dict[str, Any]:
    return {
        "cluster_buy": bool(sig and sig.cluster_buy),
        "recent_cluster_buy": bool(sig and sig.recent_cluster_buy),
        "cluster_insiders": list(sig.cluster_insiders) if sig else [],
        "opportunistic_buyers": list(sig.opportunistic_buyers) if sig else [],
        "routine_buyers": list(sig.routine_buyers) if sig else [],
        "insider_dollars": sig.dollars if sig else 0.0,
        "short_interest": dataclasses.asdict(si) if si else None,
        "high_short_interest": bool(si and si.high),
        "crowding_13f": dataclasses.asdict(cr) if cr else None,
        "news_30d": news.last_30d if news else 0,
        "lazy_prices_form": lp.form if lp else None,
    }


def compute_screen(
    inputs: SignalInputs,
    signals_cfg: SignalsConfig,
    universe_cfg: UniverseConfig,
    options: ScreenOptions | None = None,
) -> ScreenResult:
    opts = options or ScreenOptions()
    d = inputs.asof_date
    stats = price_stats(inputs.prices, d, universe_cfg.adv_window_days, opts.market_returns)
    funds = fundamentals_by_security(inputs.fundamentals)
    members, excluded = build_universe(
        inputs.master, d, stats, funds, universe_cfg, opts.active_mna
    )
    ids = [m.security_id for m in members]
    sectors = pd.Series([m.sector for m in members], index=ids, dtype=object)
    insider = insider_signals(
        inputs.insider,
        {m.security_id: m.market_cap for m in members},
        d,
        signals_cfg.insider,
        universe_cfg.cluster_buy_lookback_days,
    )
    lazy = {i: r for i in ids if (r := lazy_prices(inputs.sections, i)) is not None}
    revisions = (
        earnings_revisions(inputs.estimates, d) if signals_cfg.earnings_revision_enabled else None
    )
    raw, sub = raw_signals(members, insider, lazy, revisions)
    z = zscores(raw, sectors, signals_cfg.winsorize_z)
    contrib = contributions(z, signals_cfg, {i: s.cluster_buy for i, s in insider.items()})
    composite = contrib.sum(axis=1) if not contrib.empty else pd.Series(dtype=float)
    si, cr, news = (
        short_interest(inputs.short_interest, d),
        crowding(inputs.holdings_13f),
        news_counts(inputs.news, d),
    )
    cutoff = max_effect_cutoff({m.security_id: m.price.max_return_1m for m in members})
    analysts = _analyst_counts(inputs.estimates)
    rows = [
        _row(m, raw, z, sub, contrib, composite, insider, lazy, si, cr, news, cutoff,
             revisions, analysts, universe_cfg, d)
        for m in members
    ]  # fmt: skip
    rows.sort(key=lambda r: (-r.composite, r.security_id))
    shortlist = _shortlist(rows, universe_cfg, opts, excluded)
    return ScreenResult(
        asof=inputs.asof,
        universe_size=len(members),
        candidates=tuple(rows),
        shortlist=tuple(shortlist),
        excluded=excluded,
        lazy_prices=lazy,
        earnings_revision_enabled=signals_cfg.earnings_revision_enabled,
    )


def _analyst_counts(estimates: pd.DataFrame) -> dict[str, int]:
    """Analyst coverage if the estimates feed provides a ``num_analysts`` column."""
    if estimates.empty or "num_analysts" not in estimates.columns:
        return {}
    out: dict[str, int] = {}
    for sid, g in estimates.dropna(subset=["num_analysts"]).groupby("security_id"):
        out[str(sid)] = int(g.sort_values("snapshot").iloc[-1]["num_analysts"])
    return out


def _row(
    m: Member,
    raw: pd.DataFrame,
    z: pd.DataFrame,
    sub: pd.DataFrame,
    contrib: pd.DataFrame,
    composite: pd.Series,
    insider: dict[str, InsiderSignal],
    lazy: dict[str, LazyPricesResult],
    si: dict[str, ShortInterest],
    cr: dict[str, Crowding],
    news: dict[str, NewsCounts],
    cutoff: float | None,
    revisions: dict[str, float] | None,
    analysts: dict[str, int],
    ucfg: UniverseConfig,
    d: pd.Timestamp,
) -> ScreenRow:
    sid, f, p = m.security_id, m.fundamentals, m.price
    sig = insider.get(sid)
    bucket = tag_bucket(m.market_cap, m.adv, ucfg)
    lottery: LotteryResult | None = None
    profile: AsymmetricProfile | None = None
    if bucket == "asymmetric_bet":
        n = news.get(sid)
        lottery = lottery_filter(
            LotteryInputs(
                security_id=sid,
                asof_date=d,
                max_return_1m=p.max_return_1m,
                max_effect_cutoff=cutoff,
                gross_margin=f.gross_margin,
                cfo=f.ttm("cfo"),
                cash=f.stock("cash"),
                share_growth=f.share_growth,
                return_3m=p.return_3m,
                revenue_growth=f.revenue_growth,
                earnings_growth=f.earnings_growth,
                eps_revision=(revisions or {}).get(sid),
                list_date=m.list_date,
                last_close=p.last_close,
                min_price=ucfg.min_price_usd,
                news_30d=n.last_30d if n else 0,
                news_prior_90d=n.prior_90d if n else 0,
            )
        )
        profile = asymmetric_profile(
            revenue_growth=f.revenue_growth,
            gross_margin=f.gross_margin,
            gross_margin_prior=f.gross_margin_prior,
            opportunistic_buying=bool(sig and sig.n_purchases > 0),
            cluster_buy=bool(sig and sig.cluster_buy),
            momentum_12_1=p.momentum_12_1,
            analyst_count=analysts.get(sid),
        )
    metrics = {k: _f(v) for k, v in sub.loc[sid].items()} if sid in sub.index else {}
    metrics.update(
        market_cap=m.market_cap, adv=m.adv, last_close=p.last_close, return_3m=p.return_3m,
        max_return_1m=p.max_return_1m, revenue_growth=f.revenue_growth,
        gross_margin=f.gross_margin, share_growth=f.share_growth,
    )  # fmt: skip
    return ScreenRow(
        security_id=sid,
        ticker=m.ticker,
        sector=m.sector,
        bucket=bucket,
        market_cap=m.market_cap,
        adv=m.adv,
        composite=float(num(composite.get(sid)) or 0.0),
        contributions={str(k): num(v) or 0.0 for k, v in contrib.loc[sid].items()},
        signals={str(k): _f(v) for k, v in raw.loc[sid].items()},
        zscores={str(k): _f(v) for k, v in z.loc[sid].items()},
        metrics={str(k): v for k, v in metrics.items()},
        flags=_flags(sig, si.get(sid), cr.get(sid), news.get(sid), lazy.get(sid)),
        lottery=lottery,
        asymmetric_profile=profile,
    )


def _shortlist(
    rows: list[ScreenRow], ucfg: UniverseConfig, opts: ScreenOptions, excluded: dict[str, str]
) -> list[ScreenRow]:
    eligible: list[ScreenRow] = []
    for r in rows:
        if r.bucket is None:
            excluded[r.security_id] = "no_eligible_bucket"
        elif r.lottery is not None and not r.lottery.passed:
            excluded[r.security_id] = "lottery_filter: " + "; ".join(r.lottery.reasons)
        else:
            eligible.append(r)
    picked = [
        dataclasses.replace(r, shortlist_reason="top_composite")
        for r in eligible[: ucfg.shortlist_size]
    ]
    chosen = {r.security_id for r in picked}
    picked += [
        dataclasses.replace(r, shortlist_reason="cluster_buy")
        for r in eligible
        if r.security_id not in chosen and r.flags["recent_cluster_buy"]
    ]
    out: list[ScreenRow] = []
    for r in picked:
        if _matches(r, opts.holdings_under_review):
            excluded[r.security_id] = "holding_under_review"
        elif _matches(r, opts.wash_sale_blocked):
            excluded[r.security_id] = "wash_sale_block"
        else:
            out.append(r)
    return out


def run_screen(
    pit: PIT,
    asof: dt.datetime | dt.date | str,
    signals_cfg: SignalsConfig,
    universe_cfg: UniverseConfig,
    *,
    holdings_under_review: Iterable[str] = (),
    wash_sale_blocked: Iterable[str] = (),
    active_mna: Iterable[str] = (),
    market_returns: pd.Series | None = None,
) -> ScreenResult:
    """Load PIT inputs as of ``asof`` and compute the screen (no side effects)."""
    inputs = load_inputs(
        pit, to_utc(asof), insider_history_years=signals_cfg.insider.routine_lookback_years
    )
    return compute_screen(
        inputs,
        signals_cfg,
        universe_cfg,
        ScreenOptions(
            holdings_under_review=frozenset(holdings_under_review),
            wash_sale_blocked=frozenset(wash_sale_blocked),
            active_mna=frozenset(active_mna),
            market_returns=market_returns,
        ),
    )
