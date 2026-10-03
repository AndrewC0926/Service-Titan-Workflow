"""Weekly macro regime snapshot for the Macro and Scenario agent packet.

The snapshot is deterministic: the agent cites it, it never computes it.
Values are plain floats in percent (rates, yields, inflation, unemployment)
or index levels (oil, dollar, GPR, EPU). Reading from the PIT lake is wired
later; :func:`regime_snapshot` accepts any mapping (or a pandas Series) keyed
by the names below or by the FRED ids in :data:`FRED_ALIASES`.

Classification rule
-------------------
Growth (votes; ``down`` if the score is negative, else ``up``):

* Sahm-style gap ``unemployment - unemployment_low_12m``: >= 0.3pt -> -1,
  <= 0.1pt -> +1, otherwise 0.
* Curve ``slope_10y_3m``: < 0 (inverted) -> -1, > 1.0 -> +1, otherwise 0.

Inflation (votes; ``up`` if the score is positive, ``down`` if negative):

* ``core_pce_yoy - core_pce_yoy_prior`` (prior = 6 months earlier): > +0.1 -> +1,
  < -0.1 -> -1.
* ``cpi_yoy - cpi_yoy_prior``: same thresholds.
* ``breakeven_10y``: > 2.5 -> +1, < 2.0 -> -1.
* Tie (or no data): ``up`` if core PCE (else CPI) yoy is above 2.5, else ``down``.

Real rate = ``ust_10y - breakeven_10y``: < 0 ``negative``, < 1.0 ``low``,
< 2.0 ``neutral``, otherwise ``restrictive``; ``unknown`` if either is missing.

Quadrants: up/down -> goldilocks, up/up -> reflation, down/up -> stagflation,
down/down -> deflationary_slowdown.

Stress flags: GPR and EPU are both normalized to a long-run mean of 100;
``elevated`` above 150 (1.5x the long-run mean).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Literal

import pandas as pd
from pydantic import Field

from committee.engines.scenario.models import Frozen

Direction = Literal["up", "down"]
RealRateLevel = Literal["negative", "low", "neutral", "restrictive", "unknown"]
Quadrant = Literal["goldilocks", "reflation", "stagflation", "deflationary_slowdown"]

GPR_ELEVATED = 150.0
EPU_ELEVATED = 150.0
SAHM_DOWN = 0.3
SAHM_UP = 0.1
SLOPE_UP = 1.0
INFLATION_DELTA = 0.1
BREAKEVEN_HIGH = 2.5
BREAKEVEN_LOW = 2.0
INFLATION_LEVEL_TIE = 2.5

FRED_ALIASES: dict[str, str] = {
    "DFF": "fed_funds",
    "DGS10": "ust_10y",
    "DGS2": "ust_2y",
    "T10Y3M": "slope_10y_3m",
    "T10YIE": "breakeven_10y",
    "THREEFYTP10": "term_premium_10y",
    "UNRATE": "unemployment",
    "DCOILBRENTEU": "oil",
    "DTWEXBGS": "dollar",
}


class MacroInputs(Frozen):
    fed_funds: float | None = None
    ust_10y: float | None = None
    ust_2y: float | None = None
    slope_10y_3m: float | None = None
    breakeven_10y: float | None = None
    term_premium_10y: float | None = None
    core_pce_yoy: float | None = None
    core_pce_yoy_prior: float | None = None
    cpi_yoy: float | None = None
    cpi_yoy_prior: float | None = None
    unemployment: float | None = None
    unemployment_low_12m: float | None = None
    oil: float | None = None
    dollar: float | None = None
    gpr: float | None = None
    epu: float | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | pd.Series) -> MacroInputs:
        fields = set(cls.model_fields)
        data: dict[str, float] = {}
        for raw_key, raw_val in values.items():
            key = FRED_ALIASES.get(str(raw_key), str(raw_key))
            if key not in fields or raw_val is None:
                continue
            val = float(raw_val)  # type: ignore[arg-type]
            if math.isnan(val):
                continue
            data[key] = val
        return cls(**data)


class RegimeSnapshot(Frozen):
    as_of: dt.date | None = None
    growth: Direction
    inflation: Direction
    quadrant: Quadrant
    growth_score: int
    inflation_score: int
    real_rate: float | None
    real_rate_level: RealRateLevel
    curve_2s10s: float | None
    gpr_elevated: bool
    epu_elevated: bool
    inputs: MacroInputs
    citations: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def _vote(value: float, up: float, down: float) -> int:
    """+1 above ``up``, -1 below ``down``, else 0 (``down`` <= ``up``)."""
    if value > up:
        return 1
    if value < down:
        return -1
    return 0


def _quadrant(growth: Direction, inflation: Direction) -> Quadrant:
    table: dict[tuple[Direction, Direction], Quadrant] = {
        ("up", "down"): "goldilocks",
        ("up", "up"): "reflation",
        ("down", "up"): "stagflation",
        ("down", "down"): "deflationary_slowdown",
    }
    return table[(growth, inflation)]


def real_rate_level(real_rate: float | None) -> RealRateLevel:
    if real_rate is None:
        return "unknown"
    if real_rate < 0:
        return "negative"
    if real_rate < 1.0:
        return "low"
    if real_rate < 2.0:
        return "neutral"
    return "restrictive"


def classify(inputs: MacroInputs, as_of: dt.date | None = None) -> RegimeSnapshot:
    """Apply the documented regime rule to typed inputs."""
    cites: list[str] = []
    missing: list[str] = []

    def have(name: str) -> float | None:
        v: float | None = getattr(inputs, name)
        if v is None:
            missing.append(name)
        else:
            cites.append(f"{name}={v:g}")
        return v

    g = 0
    un, low = have("unemployment"), have("unemployment_low_12m")
    if un is not None and low is not None:
        gap = un - low
        g += 1 if gap <= SAHM_UP else (-1 if gap >= SAHM_DOWN else 0)
    slope = have("slope_10y_3m")
    if slope is not None:
        g += _vote(slope, SLOPE_UP, 0.0)
    growth: Direction = "down" if g < 0 else "up"

    i = 0
    pce, pce_prior = have("core_pce_yoy"), have("core_pce_yoy_prior")
    if pce is not None and pce_prior is not None:
        i += _vote(pce - pce_prior, INFLATION_DELTA, -INFLATION_DELTA)
    cpi, cpi_prior = have("cpi_yoy"), have("cpi_yoy_prior")
    if cpi is not None and cpi_prior is not None:
        i += _vote(cpi - cpi_prior, INFLATION_DELTA, -INFLATION_DELTA)
    be = have("breakeven_10y")
    if be is not None:
        i += _vote(be, BREAKEVEN_HIGH, BREAKEVEN_LOW)
    if i > 0:
        inflation: Direction = "up"
    elif i < 0:
        inflation = "down"
    else:
        level = pce if pce is not None else cpi
        inflation = "up" if level is not None and level > INFLATION_LEVEL_TIE else "down"

    y10, y2 = have("ust_10y"), have("ust_2y")
    real = y10 - be if y10 is not None and be is not None else None
    for name in ("fed_funds", "term_premium_10y", "oil", "dollar"):
        have(name)
    gpr, epu = have("gpr"), have("epu")
    return RegimeSnapshot(
        as_of=as_of,
        growth=growth,
        inflation=inflation,
        quadrant=_quadrant(growth, inflation),
        growth_score=g,
        inflation_score=i,
        real_rate=real,
        real_rate_level=real_rate_level(real),
        curve_2s10s=y10 - y2 if y10 is not None and y2 is not None else None,
        gpr_elevated=gpr is not None and gpr > GPR_ELEVATED,
        epu_elevated=epu is not None and epu > EPU_ELEVATED,
        inputs=inputs,
        citations=cites,
        missing=missing,
    )


def regime_snapshot(
    values: Mapping[str, object] | pd.Series | MacroInputs, as_of: dt.date | None = None
) -> RegimeSnapshot:
    """Build the weekly regime snapshot from latest macro values."""
    inputs = values if isinstance(values, MacroInputs) else MacroInputs.from_mapping(values)
    return classify(inputs, as_of)
