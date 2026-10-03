"""After-tax hurdle for a taxable sale (DESIGN 9).

Notation: V = current market value, B = adjusted basis, T = tax due on sale
now (``rates.tax_on(V - B, term)``; negative for a loss), N = V - T the
after-tax proceeds reinvested, n = horizon in years, r = expected annual
return of the holding.

1. No-deferral hurdle (equal horizon, future taxes ignored):
   ``hurdle_total = V / N - 1``. The replacement must earn this much more, in
   total, than the holding would just to get back to where holding leaves you.

2. With deferral (both positions liquidated at the end of the horizon):
   hold:   ``H = V*g - t_h*(V*g - B)``  with ``g = (1+r)^n`` and ``t_h`` the
           rate for the lot's term at the horizon end;
   switch: ``S = N*g' - t_s*(N*g' - N)`` with ``t_s`` the rate for a new lot
           held ``n`` years.
   Setting ``S = H``: ``g' = (H - t_s*N) / (N*(1 - t_s))`` and the required
   annual return of the replacement is ``g'^(1/n) - 1``. The annual hurdle is
   that minus ``r``.

A loss (T < 0) gives a negative hurdle: the replacement can earn less than
the holding and still come out ahead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from committee.engines.tax.holding import Term, term_of
from committee.engines.tax.labels import labeled
from committee.engines.tax.rates import TaxRates


@dataclass(frozen=True)
class Hurdle:
    value: float
    basis: float
    term: Term
    tax_due: float
    net_proceeds: float
    hurdle_total_no_deferral: float
    horizon_years: float
    hold_expected_return: float
    hold_after_tax_at_horizon: float
    required_annual_return: float
    hurdle_annual: float
    explanation: str


def after_tax_hurdle(
    value: float,
    basis: float,
    holding_start: dt.date,
    sale_date: dt.date,
    rates: TaxRates,
    horizon_years: float = 1.0,
    hold_expected_return: float = 0.0,
) -> Hurdle:
    if value <= 0:
        raise ValueError("value must be positive")
    if horizon_years <= 0:
        raise ValueError("horizon_years must be positive")
    term = term_of(holding_start, sale_date)
    tax = rates.tax_on(value - basis, term)
    net = value - tax
    if net <= 0:
        raise ValueError("tax due exceeds value")
    hurdle_total = value / net - 1.0

    end = sale_date + dt.timedelta(days=round(horizon_years * 365.25))
    t_hold = rates.rate(term_of(holding_start, end))
    t_new = rates.rate(term_of(sale_date, end))
    g = (1.0 + hold_expected_return) ** horizon_years
    hold_end = value * g - t_hold * (value * g - basis)
    g_new = (hold_end - t_new * net) / (net * (1.0 - t_new))
    if g_new <= 0:
        raise ValueError("no positive growth path breaks even")
    required = g_new ** (1.0 / horizon_years) - 1.0
    hurdle_annual = required - hold_expected_return
    explanation = labeled(
        f"Selling now ({term}-term) costs {tax:,.2f} in tax, leaving {net:,.2f} of {value:,.2f}. "
        f"Without deferral the replacement must earn {hurdle_total:.2%} more in total "
        f"(V/(V-T)-1). Over {horizon_years:g} year(s) with the holding expected to return "
        f"{hold_expected_return:.2%} a year, the replacement must return {required:.2%} a year "
        f"({hurdle_annual:+.2%} vs. holding) after both are liquidated."
    )
    return Hurdle(
        value=value,
        basis=basis,
        term=term,
        tax_due=tax,
        net_proceeds=net,
        hurdle_total_no_deferral=hurdle_total,
        horizon_years=horizon_years,
        hold_expected_return=hold_expected_return,
        hold_after_tax_at_horizon=hold_end,
        required_annual_return=required,
        hurdle_annual=hurdle_annual,
        explanation=explanation,
    )
