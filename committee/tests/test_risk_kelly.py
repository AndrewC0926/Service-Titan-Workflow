from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from committee.engines.risk import Outcome, kelly_binary, kelly_ceiling, kelly_discrete
from committee.engines.risk.kelly import expected_log_growth


def oc(*rows: tuple[float, float]) -> list[Outcome]:
    return [Outcome(label=f"o{i}", probability=p, ret=r) for i, (p, r) in enumerate(rows)]


def test_even_money_coin() -> None:
    # p=0.6, +100% / -100%: f* = 2p - 1 = 0.2
    outs = oc((0.6, 1.0), (0.4, -1.0))
    assert kelly_discrete(outs) == pytest.approx(0.2, abs=1e-9)
    assert kelly_binary(0.6, outs) == pytest.approx(0.2)


def test_binary_closed_form_uneven_payoff() -> None:
    # win +50% w.p. p, lose 20%: f = p/a - q/b = 0.5/0.2 - 0.5/0.5 = 1.5 -> clamped to 1
    outs = oc((0.5, 0.5), (0.5, -0.2))
    assert kelly_binary(0.5, outs) == 1.0
    # p=0.4: 0.4/0.2 - 0.6/0.5 = 0.8
    assert kelly_binary(0.4, outs) == pytest.approx(0.8)
    # discrete Kelly for the same 2-outcome bet: f = p/a - q/b with outcome probs (0.5)
    assert kelly_discrete(outs) == 1.0  # growth still increasing at f=1 (no leverage)


def test_bear_base_bull_matches_grid_search() -> None:
    outs = oc((0.25, -0.40), (0.50, 0.08), (0.25, 0.45))
    f = kelly_discrete(outs)
    grid = max((i / 10000 for i in range(10000)), key=lambda x: expected_log_growth(outs, x))
    assert f == pytest.approx(grid, abs=2e-4)
    # binary: b = 0.1533.. (avg win), a = 0.40 with p=0.55
    b = (0.5 * 0.08 + 0.25 * 0.45) / 0.75
    assert kelly_binary(0.55, outs) == pytest.approx(max(0.0, 0.55 / 0.40 - 0.45 / b))


def test_no_edge_and_degenerate_cases() -> None:
    assert kelly_discrete(oc((0.5, 0.1), (0.5, -0.2))) == 0.0
    assert kelly_discrete([]) == 0.0
    assert kelly_discrete(oc((1.0, 0.2), (0.0, -1.0))) == 1.0  # zero-prob loss ignored
    assert kelly_binary(0.0, oc((1.0, 0.2))) == 0.0
    assert kelly_binary(0.7, oc((1.0, -0.2))) == 0.0  # no winning outcome
    assert kelly_binary(0.7, oc((1.0, 0.2))) == 1.0  # no losing outcome
    assert kelly_binary(0.1, oc((0.5, 0.1), (0.5, -0.5))) == 0.0  # clamped at zero


def test_total_loss_outcome_bounds_fraction() -> None:
    outs = oc((0.1, -1.0), (0.9, 0.5))
    f = kelly_discrete(outs)
    assert 0 < f < 1
    assert math.isfinite(expected_log_growth(outs, f))
    assert expected_log_growth(outs, 1.0) == -math.inf


def test_ceiling_uses_min_and_cap() -> None:
    outs = oc((0.25, -0.40), (0.50, 0.08), (0.25, 0.45))
    k = kelly_ceiling(0.45, outs, 0.5)
    assert k.f_used == min(k.f_discrete, k.f_binary)
    assert k.ceiling_pct_total == pytest.approx(100 * 0.5 * k.f_used)
    assert k.fraction_cap == 0.5


outcome_rows = st.lists(
    st.tuples(st.floats(0.01, 1.0), st.floats(-1.0, 3.0)), min_size=1, max_size=5
)


@given(outcome_rows, st.floats(0.0, 1.0))
def test_kelly_bounds_and_optimality(rows: list[tuple[float, float]], p: float) -> None:
    total = sum(r[0] for r in rows)
    outs = oc(*((w / total, r) for w, r in rows))
    f = kelly_discrete(outs)
    assert 0.0 <= f <= 1.0
    fb = kelly_binary(p, outs)
    assert 0.0 <= fb <= 1.0
    g = expected_log_growth(outs, f)
    for alt in (0.0, f / 2, min(1.0, f + 0.01), max(0.0, f - 0.01), 0.999):
        assert g >= expected_log_growth(outs, alt) - 1e-7
    k = kelly_ceiling(p, outs, 0.5)
    assert 0.0 <= k.ceiling_pct_total <= 50.0
