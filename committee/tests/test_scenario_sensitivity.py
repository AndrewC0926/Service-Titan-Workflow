from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from committee.config.loader import load_config
from committee.domain import Holding
from committee.engines.scenario import (
    FactorSensitivity,
    default_sensitivity,
    estimate_sensitivity,
    sensitivities_for,
)
from committee.engines.scenario.sensitivity import normalize_sector, sector_row

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_config(ROOT / "config").policy_portfolio


def synthetic(n: int = 500, seed: int = 7, noise: float = 0.002) -> tuple[pd.Series, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    f = pd.DataFrame(
        {
            "market": rng.normal(0, 0.01, n),
            "rates_10y_bp": rng.normal(0, 5.0, n),
            "oil": rng.normal(0, 0.02, n),
            "dollar": rng.normal(0, 0.004, n),
        },
        index=idx,
    )
    y = (
        0.0001
        + 1.3 * f["market"]
        - 0.0006 * f["rates_10y_bp"]  # -0.06 per 100bp
        + 0.25 * f["oil"]
        - 0.4 * f["dollar"]
        + rng.normal(0, noise, n)
    )
    return pd.Series(y, index=idx), f


def test_estimator_recovers_true_betas() -> None:
    y, f = synthetic()
    est = estimate_sensitivity(y, f, themes={"energy_price": 0.5})
    s = est.sensitivity
    assert s.beta_market == pytest.approx(1.3, abs=0.03)
    assert s.beta_rates_100bp == pytest.approx(-0.06, abs=0.005)
    assert s.beta_oil == pytest.approx(0.25, abs=0.02)
    assert s.beta_dollar == pytest.approx(-0.4, abs=0.05)
    assert s.themes == {"energy_price": 0.5}
    assert s.source == "estimated"
    assert est.intercept == pytest.approx(0.0001, abs=0.0005)
    assert est.r_squared > 0.9
    assert est.n_obs == 500


def test_estimator_exact_without_noise() -> None:
    y, f = synthetic(n=60, noise=0.0)
    s = estimate_sensitivity(y, f).sensitivity
    assert s.beta_market == pytest.approx(1.3, abs=1e-9)
    assert s.beta_rates_100bp == pytest.approx(-0.06, abs=1e-9)


def test_estimator_subset_and_alignment() -> None:
    y, f = synthetic(n=200)
    y = y.copy()
    y.iloc[:10] = np.nan
    est = estimate_sensitivity(y.iloc[5:], f[["market"]])
    assert est.n_obs == 190  # 10 NaN rows dropped
    assert est.sensitivity.beta_oil == 0.0
    assert est.sensitivity.beta_market == pytest.approx(1.3, abs=0.2)


def test_estimator_errors() -> None:
    y, f = synthetic(n=100)
    with pytest.raises(ValueError, match="unknown"):
        estimate_sensitivity(y, f.assign(gold=0.0))
    with pytest.raises(ValueError, match="no recognised"):
        estimate_sensitivity(y, f.iloc[:, :0])
    with pytest.raises(ValueError, match="at least"):
        estimate_sensitivity(y.iloc[:10], f)
    with pytest.raises(ValueError, match="singular"):
        estimate_sensitivity(y, f.assign(oil=1.0))
    flat = pd.Series(0.0, index=y.index)
    assert estimate_sensitivity(flat, f).r_squared == 0.0


def h(symbol: str, sleeve: str, **kw: object) -> Holding:
    return Holding.model_validate(
        {"account": "ira", "symbol": symbol, "qty": 1.0, "price": 1.0, "sleeve": sleeve, **kw}
    )


def test_default_sleeve_table_and_lookthrough() -> None:
    vti = default_sensitivity(h("VTI", "us_total"), POLICY)
    assert vti.beta_market == 1.0 and vti.source == "default_sleeve"
    assert vti.themes == POLICY.lookthrough["VTI"]
    vwo = default_sensitivity(h("VWO", "em"), POLICY)
    assert vwo.beta_dollar < 0
    trend = default_sensitivity(h("DBMF", "trend"), POLICY)
    assert trend.beta_market == 0.0 and trend.beta_market_down == pytest.approx(-0.2)
    bills = default_sensitivity(h("SGOV", "tbills"), POLICY)
    assert (bills.beta_market, bills.beta_rates_100bp, bills.beta_oil, bills.beta_dollar) == (
        0.0,
        0.0,
        0.0,
        0.0,
    )
    # holding tags override look-through for the same theme
    tagged = default_sensitivity(h("VTI", "us_total", themes={"ai_capex_chain": 0.5}), POLICY)
    assert tagged.themes["ai_capex_chain"] == 0.5
    assert tagged.themes["energy_price"] == POLICY.lookthrough["VTI"]["energy_price"]


def test_default_kind_fallback_and_unknown() -> None:
    from committee.config.schema import PolicyPortfolio

    raw = POLICY.model_dump()
    raw["sleeves"][0]["id"] = "custom_core"
    policy = PolicyPortfolio.model_validate(raw)
    s = default_sensitivity(h("VTI", "custom_core"), policy)
    assert s.beta_market == 1.0 and s.source == "default_sleeve"
    unknown = default_sensitivity(h("ZZZ", "mystery"), policy)
    assert unknown.source == "zero" and unknown.beta_market == 0.0
    assert default_sensitivity(h("ZZZ", "mystery")).source == "zero"


def test_default_satellite_by_sector_and_bucket() -> None:
    energy = default_sensitivity(h("XOM", "satellite", bucket="core_pick", sector="Energy"))
    assert energy.beta_oil == pytest.approx(0.40)
    assert energy.beta_market == pytest.approx(0.90)
    assert energy.source == "default_sector"
    util = default_sensitivity(h("U", "satellite", bucket="core_pick", sector="Utilities"))
    reit = default_sensitivity(h("R", "satellite", bucket="core_pick", sector="real_estate"))
    assert util.beta_rates_100bp < 0 and reit.beta_rates_100bp < util.beta_rates_100bp
    asym = default_sensitivity(
        h(
            "A",
            "satellite",
            bucket="asymmetric_bet",
            sector="Technology",
            themes={"ai_capex_chain": 0.7},
        )
    )
    assert asym.beta_market == pytest.approx(1.3 * 1.2)
    assert asym.themes == {"ai_capex_chain": 0.7}
    spec = default_sensitivity(h("BTC", "speculative"))
    assert spec.beta_market == pytest.approx(1.5)
    nobucket = default_sensitivity(h("N", "satellite", sector="Weird Sector"))
    assert nobucket.beta_market == 1.0 and nobucket.beta_rates_100bp == pytest.approx(-0.02)


def test_sector_normalization() -> None:
    assert normalize_sector(None) is None
    assert normalize_sector("  Health_Care ") == "health care"
    assert normalize_sector("Tech") == "information technology"
    assert sector_row("Financial Services").rates > 0


def test_sensitivities_for_merges_estimates() -> None:
    holdings = [h("VTI", "us_total"), h("VTI", "us_total"), h("SEMI", "satellite", sector="Tech")]
    est = {"VTI": FactorSensitivity(beta_market=0.95, source="estimated")}
    out = sensitivities_for(holdings, POLICY, est)
    assert set(out) == {"VTI", "SEMI"}
    assert out["VTI"].beta_market == 0.95
    assert out["VTI"].themes == POLICY.lookthrough["VTI"]  # inherited look-through
    tagged = {"VTI": FactorSensitivity(beta_market=0.9, themes={"x": 1.0}, source="estimated")}
    assert sensitivities_for(holdings, POLICY, tagged)["VTI"].themes == {"x": 1.0}
    assert out["SEMI"].source == "default_sector"
