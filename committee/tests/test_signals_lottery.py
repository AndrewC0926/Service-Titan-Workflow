"""Lottery filter (one test per exclusion rule) and deterministic bucket tagging."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from committee.config.loader import load_config
from committee.signals.buckets import asymmetric_profile, tag_bucket
from committee.signals.lottery import (
    LotteryInputs,
    lottery_filter,
    max_effect_cutoff,
    rule_attention_spike,
    rule_cash_runway,
    rule_chasing,
    rule_dilution,
    rule_max_effect,
    rule_negative_gross_margin,
    rule_penny_stock,
    rule_recent_ipo,
)

D = pd.Timestamp("2026-09-27")
CLEAN = LotteryInputs(
    security_id="S",
    asof_date=D,
    max_return_1m=0.03,
    max_effect_cutoff=0.10,
    gross_margin=0.4,
    cfo=1e7,
    cash=1e8,
    share_growth=0.02,
    return_3m=0.2,
    revenue_growth=0.3,
    earnings_growth=0.1,
    list_date=pd.Timestamp("2015-01-01"),
    last_close=20.0,
    news_30d=3,
    news_prior_90d=9,
)


def test_clean_name_passes_every_rule() -> None:
    r = lottery_filter(CLEAN)
    assert r.passed and r.reasons == ()


def test_max_effect_top_decile() -> None:
    vals = {f"n{i}": i / 100 for i in range(20)}  # 0.00 .. 0.19; top decile = top 2
    cutoff = max_effect_cutoff(vals)
    assert cutoff == pytest.approx(0.18)
    assert max_effect_cutoff({f"n{i}": 0.1 for i in range(9)}) is None  # < 10 names
    assert rule_max_effect(replace(CLEAN, max_return_1m=0.18, max_effect_cutoff=cutoff))
    assert rule_max_effect(replace(CLEAN, max_return_1m=0.17, max_effect_cutoff=cutoff)) is None
    assert rule_max_effect(replace(CLEAN, max_effect_cutoff=None)) is None


def test_negative_gross_margin() -> None:
    assert rule_negative_gross_margin(replace(CLEAN, gross_margin=-0.01))
    assert rule_negative_gross_margin(replace(CLEAN, gross_margin=0.0)) is None
    assert rule_negative_gross_margin(replace(CLEAN, gross_margin=None)) is None


def test_cash_runway_requires_ocf_or_24_months() -> None:
    assert rule_cash_runway(CLEAN) is None  # positive OCF
    burn = replace(CLEAN, cfo=-12e6)  # $1M a month
    assert rule_cash_runway(replace(burn, cash=24e6)) is None  # exactly 24 months
    assert rule_cash_runway(replace(burn, cash=23e6))  # 23 months
    assert rule_cash_runway(replace(CLEAN, cfo=None))  # requirement fails closed


def test_dilution_over_ten_percent() -> None:
    assert rule_dilution(replace(CLEAN, share_growth=0.11))
    assert rule_dilution(replace(CLEAN, share_growth=0.10)) is None


def test_chasing_without_matching_revision() -> None:
    hot = replace(CLEAN, return_3m=1.2, revenue_growth=0.1, earnings_growth=0.1)
    assert rule_chasing(hot)
    assert rule_chasing(replace(hot, revenue_growth=0.6)) is None  # revenue revision matches
    assert rule_chasing(replace(hot, earnings_growth=0.5)) is None  # earnings growth matches
    assert rule_chasing(replace(hot, eps_revision=0.3)) is None  # consensus revision matches
    assert rule_chasing(replace(hot, return_3m=1.0)) is None  # not more than +100%


def test_recent_ipo_or_spac_under_12_months() -> None:
    assert rule_recent_ipo(replace(CLEAN, list_date=pd.Timestamp("2026-01-15")))
    assert rule_recent_ipo(replace(CLEAN, list_date=pd.Timestamp("2025-09-01"))) is None


def test_penny_stock_under_five_dollars() -> None:
    assert rule_penny_stock(replace(CLEAN, last_close=4.99))
    assert rule_penny_stock(replace(CLEAN, last_close=5.0)) is None


def test_retail_attention_spike() -> None:
    # 30 in 30 days = 1/day vs 9 in 90 days = 0.1/day -> 10x
    assert rule_attention_spike(replace(CLEAN, news_30d=30, news_prior_90d=9))
    # 12 in 30 days = 0.4/day vs 0.1/day -> 4x
    assert rule_attention_spike(replace(CLEAN, news_30d=12, news_prior_90d=9)) is None
    assert rule_attention_spike(replace(CLEAN, news_30d=6, news_prior_90d=0))  # from nothing
    assert rule_attention_spike(replace(CLEAN, news_30d=4, news_prior_90d=0)) is None  # too few


def test_lottery_filter_collects_all_reasons() -> None:
    r = lottery_filter(replace(CLEAN, gross_margin=-0.1, last_close=2.0))
    assert not r.passed
    assert [x.split(":")[0] for x in r.reasons] == ["negative_gross_margin", "penny_stock"]


# ------------------------------------------------------------------ buckets
@pytest.fixture
def universe(project: Path):  # type: ignore[no-untyped-def]
    return load_config(project / "config").universe


def test_bucket_tagging(universe) -> None:  # type: ignore[no-untyped-def]
    assert tag_bucket(2e9, 20e6, universe) == "core_pick"
    assert tag_bucket(50e9, 500e6, universe) == "core_pick"
    assert tag_bucket(1.99e9, 50e6, universe) == "asymmetric_bet"  # under $2B
    assert tag_bucket(5e9, 19e6, universe) == "asymmetric_bet"  # under $20M ADV
    assert tag_bucket(300e6, 3e6, universe) == "asymmetric_bet"
    assert tag_bucket(15e9, 5e6, universe) is None  # too big for asym, too illiquid for core
    assert tag_bucket(299e6, 10e6, universe) is None
    assert tag_bucket(1e9, 2.9e6, universe) is None
    assert tag_bucket(None, 1e9, universe) is None


def test_asymmetric_profile() -> None:
    p = asymmetric_profile(
        revenue_growth=0.35, gross_margin=0.5, gross_margin_prior=0.45,
        opportunistic_buying=True, cluster_buy=False, momentum_12_1=0.2, analyst_count=None,
    )  # fmt: skip
    assert p.high_growth and p.insider_buying and p.positive_momentum
    assert p.underfollowed is None
    assert (p.score, p.available) == (3, 3)
    q = asymmetric_profile(
        revenue_growth=0.35, gross_margin=0.4, gross_margin_prior=0.45,
        opportunistic_buying=False, cluster_buy=True, momentum_12_1=-0.1, analyst_count=4,
    )  # fmt: skip
    assert q.high_growth is False and q.gross_margin_improving is False
    assert q.insider_buying and q.underfollowed
    assert (q.score, q.available) == (2, 4)
