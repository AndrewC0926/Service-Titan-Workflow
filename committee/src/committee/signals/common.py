"""Small shared helpers for the signal library (no I/O)."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pandas as pd


def as_date(t: dt.datetime | dt.date | str | pd.Timestamp) -> pd.Timestamp:
    """A naive, normalized (midnight) Timestamp for calendar-date comparisons.

    Aware datetimes are converted to UTC first so ``asof`` (end of day UTC) maps to its date.
    """
    ts = pd.Timestamp(t)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def num(x: Any) -> float | None:
    """A finite float or None (NaN, None, inf and non-numbers become None)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return num(a / b)


def date_col(df: pd.DataFrame, col: str) -> pd.Series:
    """A column as naive normalized timestamps (dates)."""
    s = pd.to_datetime(df[col], utc=False)
    if isinstance(s.dtype, pd.DatetimeTZDtype):
        s = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return s.dt.normalize()
