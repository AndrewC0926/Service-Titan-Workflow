from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pytest

from committee.engines.allocator.rules import AllocatorInputs, decide, journal_decision
from committee.evaluation import cohorts, forecasts, portfolio, trials
from committee.journal.store import Journal

T = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def test_brier_and_skill_known_values() -> None:
    assert forecasts.brier([1, 0], [1, 0]) == 0
    assert forecasts.brier([0.5, 0.5], [1, 0]) == 0.25
    assert forecasts.brier_skill([0.9, 0.1], [0.5, 0.5], [1, 0]) == pytest.approx(1 - 0.01 / 0.25)
    with pytest.raises(ValueError):
        forecasts.brier([], [])


def test_calibrated_forecaster_is_reliable() -> None:
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 20000)
    o = (rng.uniform(0, 1, 20000) < p).astype(int)
    for b in forecasts.reliability(p, o):
        assert abs(b.mean_forecast - b.observed_rate) < 0.03
    m = forecasts.murphy(p, o)
    assert m["reliability"] < 0.001
    bs = forecasts.brier(p, o)
    assert bs == pytest.approx(m["reliability"] - m["resolution"] + m["uncertainty"], abs=0.01)


def test_score_forecasts_vs_base_rate() -> None:
    rng = np.random.default_rng(1)
    fs = []
    for i in range(400):
        truth = rng.uniform(0.2, 0.8)
        outcome = int(rng.uniform() < truth)
        fs.append(
            forecasts.Forecast(
                f"b{i}", f"t{i}", "base_rate", "beats_benchmark", 12, 0.5, T, "c1", outcome
            )
        )
        fs.append(
            forecasts.Forecast(
                f"g{i}", f"t{i}", "chair", "beats_benchmark", 12, truth, T, "c1", outcome
            )
        )
        fs.append(
            forecasts.Forecast(
                f"n{i}",
                f"t{i}",
                "noisy",
                "beats_benchmark",
                12,
                float(rng.uniform()),
                T,
                "c1",
                outcome,
            )
        )
    fs.append(
        forecasts.Forecast("u", "tx", "chair", "beats_benchmark", 12, 0.6, T, "c1", None)
    )  # unresolved ignored
    s = {x.agent: x for x in forecasts.score_forecasts(fs)}
    assert s["chair"].n == 400
    assert (
        s["chair"].brier_skill_vs_base_rate is not None and s["chair"].brier_skill_vs_base_rate > 0
    )
    assert (
        s["noisy"].brier_skill_vs_base_rate is not None and s["noisy"].brier_skill_vs_base_rate < 0
    )
    assert s["base_rate"].brier_skill_vs_base_rate is None


def test_resolve_from_path() -> None:
    assert forecasts.resolve_from_path([100, 210], "doubles") == 1
    assert forecasts.resolve_from_path([100, 150, 140], "doubles") == 0
    assert forecasts.resolve_from_path([100, 130, 90], "drawdown_exceeds_30pct") == 1
    assert forecasts.resolve_from_path([100, 49, 120], "loses_50pct") == 1
    assert (
        forecasts.resolve_from_path([100, 112], "beats_benchmark", bench_ret=0.10, cost_bps=100)
        == 1
    )
    assert (
        forecasts.resolve_from_path([100, 110.5], "beats_benchmark", bench_ret=0.10, cost_bps=100)
        == 0
    )
    with pytest.raises(ValueError):
        forecasts.resolve_from_path([100], "doubles")
    with pytest.raises(ValueError):
        forecasts.resolve_from_path([100, 101], "moon")


def test_agent_weights_shrinkage_and_ratio_cap() -> None:
    w = forecasts.agent_weights({"a": 0.30, "b": 0.01, "c": -0.2, "d": 0.1})
    assert sum(w.values()) == pytest.approx(1)
    assert max(w.values()) / min(w.values()) <= 2.0 + 1e-9
    assert w["a"] > w["d"] > w["b"]
    assert forecasts.agent_weights({}) == {}
    eq = forecasts.agent_weights({"a": -1, "b": -1})
    assert eq["a"] == pytest.approx(0.5)


def test_max_drawdown_and_ir() -> None:
    assert portfolio.max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(-0.5)
    assert portfolio.max_drawdown([]) == 0.0
    assert portfolio.years_to_significance(0.5) == pytest.approx(16)
    assert portfolio.years_to_significance(0) == float("inf")
    assert np.isnan(portfolio.information_ratio([0.01, 0.01]))


def test_deflated_sharpe_penalizes_many_trials() -> None:
    rng = np.random.default_rng(2)
    r = rng.normal(0.01, 0.04, 120)  # decent monthly strategy
    one = portfolio.deflated_sharpe(r, n_trials=1)
    many = portfolio.deflated_sharpe(r, n_trials=500, trial_sr_variance=0.02)
    assert one > many
    noise = rng.normal(0, 0.04, 120)
    assert portfolio.deflated_sharpe(noise, n_trials=100, trial_sr_variance=0.01) < 0.5
    assert np.isnan(portfolio.deflated_sharpe([0.1, 0.2], 1))


def test_min_track_record_length() -> None:
    rng = np.random.default_rng(3)
    good = rng.normal(0.02, 0.04, 240)
    weak = rng.normal(0.003, 0.04, 240)
    assert portfolio.min_track_record_length(good) < portfolio.min_track_record_length(weak)
    assert portfolio.min_track_record_length(-np.abs(good)) == float("inf")


def test_pbo_detects_overfitting() -> None:
    rng = np.random.default_rng(4)
    noise = rng.normal(0, 0.01, (240, 30))  # 30 variants, no skill: PBO near 0.5
    assert 0.2 < portfolio.pbo_cscv(noise) < 0.8
    skill = noise.copy()
    skill[:, 0] += 0.01  # one genuinely better variant: low PBO
    assert portfolio.pbo_cscv(skill) < 0.1
    assert np.isnan(portfolio.pbo_cscv(np.zeros((5, 1))))


def test_factor_regression_recovers_betas_and_fmb() -> None:
    rng = np.random.default_rng(5)
    f = rng.normal(0, 0.01, (500, 3))
    y = 0.001 + f @ np.array([1.1, 0.4, -0.2]) + rng.normal(0, 0.001, 500)
    b = portfolio.factor_regression(y, f, ["mkt", "smb", "hml"])
    assert b["mkt"] == pytest.approx(1.1, abs=0.03) and b["smb"] == pytest.approx(0.4, abs=0.03)
    w = portfolio.factor_matched_weights(
        {"mkt": 1.0, "smb": 0.5},
        {"VTI": {"mkt": 1.0, "smb": 0.0}, "IJS": {"mkt": 1.0, "smb": 1.0}},
    )
    assert w["VTI"] == pytest.approx(0.5, abs=0.01) and w["IJS"] == pytest.approx(0.5, abs=0.01)


def test_turnover_and_costs() -> None:
    assert portfolio.turnover([10_000, -10_000], 100_000) == pytest.approx(0.1)
    assert portfolio.after_cost_returns([0.01], [0.1], 10)[0] == pytest.approx(0.01 - 0.0002)


def _r(
    rec: str, verdict: str = "PASS", human: str | None = None, init: bool = False, ex: float = 0.01
) -> cohorts.ReviewOutcome:
    return cohorts.ReviewOutcome("t", rec, verdict, human, init, "c", {3: ex, 6: ex, 12: ex})


def test_cohorts() -> None:
    assert cohorts.cohort_of(_r("BUY")) == "BUY"
    assert cohorts.cohort_of(_r("BUY", verdict="VETO")) == "VETO"
    assert cohorts.cohort_of(_r("BUY", human="rejected")) == "OVERRIDE"
    assert cohorts.cohort_of(_r("PASS", init=True)) == "OVERRIDE"
    assert cohorts.cohort_of(_r("WATCH")) == "PASS"
    stats = cohorts.cohort_stats(
        [_r("BUY", ex=0.05)] * 12 + [_r("BUY", human="rejected", ex=-0.02)] * 3
    )
    d = {(s.cohort, s.horizon): s for s in stats}
    assert d[("BUY", 12)].n == 12 and d[("BUY", 12)].hit_rate == 1.0
    assert d[("BUY", 12)].warning and "far too few" in d[("BUY", 12)].warning
    assert d[("VETO", 12)].mean_excess is None
    assert cohorts.overrides_underperform(stats) is True
    assert cohorts.small_sample_warning(500) is None


def test_trial_registry(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    assert trials.trial_count(j) == 1
    assert trials.register_trial(j, "prompt", "chair", "abc")
    assert not trials.register_trial(j, "prompt", "chair", "abc")
    trials.register_trial(j, "model", "top", "def")
    assert trials.trial_count(j) == 2
    with pytest.raises(ValueError):
        trials.register_trial(j, "vibes", "x", "y")


GOOD = dict(
    live_months=40,
    current_pct=20.0,
    min_pct=10.0,
    max_pct=35.0,
    deflated_sharpe_vs_fmb=0.97,
    cum_excess_after_tax_vs_spy=0.05,
    cum_excess_after_tax_vs_fmb=0.03,
    satellite_brier=0.20,
    base_rate_brier=0.23,
    satellite_max_dd=0.30,
    benchmark_max_dd=0.25,
    excess_24m_after_tax_vs_fmb=0.02,
    quarters_brier_worse_than_base=0,
    guardrail_breaches_this_quarter=0,
)


def test_allocator_freeze_first_36_months() -> None:
    d = decide(AllocatorInputs(**{**GOOD, "live_months": 35, "guardrail_breaches_this_quarter": 5}))  # type: ignore[arg-type]
    assert d.action == "FREEZE" and d.recommended_pct == 20.0


def test_allocator_increase_only_if_all_hold() -> None:
    assert decide(AllocatorInputs(**GOOD)).recommended_pct == 25.0  # type: ignore[arg-type]
    for k, v in [
        ("deflated_sharpe_vs_fmb", 0.95),
        ("cum_excess_after_tax_vs_spy", -0.01),
        ("cum_excess_after_tax_vs_fmb", 0.0),
        ("satellite_brier", 0.24),
        ("satellite_max_dd", 0.40),
        ("deflated_sharpe_vs_fmb", None),
    ]:
        d = decide(AllocatorInputs(**{**GOOD, k: v}))  # type: ignore[arg-type]
        assert d.action == "HOLD" and d.recommended_pct == 20.0, k
    assert decide(AllocatorInputs(**{**GOOD, "current_pct": 35.0})).action == "HOLD"  # type: ignore[arg-type]


def test_allocator_decrease_if_any_and_floor() -> None:
    for k, v in [
        ("excess_24m_after_tax_vs_fmb", -0.08),
        ("quarters_brier_worse_than_base", 4),
        ("guardrail_breaches_this_quarter", 2),
    ]:
        d = decide(AllocatorInputs(**{**GOOD, k: v}))  # type: ignore[arg-type]
        assert d.action == "DECREASE" and d.recommended_pct == 15.0, k
    d = decide(
        AllocatorInputs(**{**GOOD, "current_pct": 10.0, "guardrail_breaches_this_quarter": 3})
    )  # type: ignore[arg-type]
    assert d.recommended_pct == 10.0


def test_allocator_journaled(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    i = AllocatorInputs(**GOOD)  # type: ignore[arg-type]
    e = journal_decision(j, i, decide(i))
    assert e.payload["inputs"]["live_months"] == 40 and e.payload["requires_human_approval"] is True
