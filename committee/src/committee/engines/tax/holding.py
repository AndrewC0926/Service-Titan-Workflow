"""Holding-period clock.

Rule (IRC 1222, Pub. 550): a gain or loss is long-term when the asset was held
*more than one year*. The holding period starts the day after acquisition, so
a lot is long-term when ``sale_date > anniversary(acquired_on)``, where the
anniversary is the same calendar date one year later. A lot acquired on
February 29 has its anniversary on February 28 of the next year (a sale on
February 28 is still short-term; March 1 is the first long-term day).

Calendar arithmetic is used instead of a 365-day count so leap years are
handled exactly; ``long_term_days`` in the config is documentation only.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

Term = Literal["short", "long"]


def anniversary(acquired_on: dt.date) -> dt.date:
    """Same calendar date one year later; Feb 29 maps to Feb 28."""
    try:
        return acquired_on.replace(year=acquired_on.year + 1)
    except ValueError:  # Feb 29 -> non-leap year
        return dt.date(acquired_on.year + 1, 2, 28)


def first_long_term_date(acquired_on: dt.date) -> dt.date:
    """The first sale date on which the lot is long-term."""
    return anniversary(acquired_on) + dt.timedelta(days=1)


def is_long_term(acquired_on: dt.date, sold_on: dt.date) -> bool:
    return sold_on > anniversary(acquired_on)


def term_of(acquired_on: dt.date, sold_on: dt.date) -> Term:
    return "long" if is_long_term(acquired_on, sold_on) else "short"


def days_until_long_term(acquired_on: dt.date, asof: dt.date) -> int:
    """Days from ``asof`` until the lot first qualifies as long-term (0 if it already does)."""
    return max(0, (first_long_term_date(acquired_on) - asof).days)


def tacked_holding_start(
    replacement_acquired_on: dt.date, sold_holding_start: dt.date, sold_on: dt.date
) -> dt.date:
    """Holding-period start of wash-sale replacement shares (IRC 1223(3)).

    The replacement's holding period includes the period the sold shares were
    held, so its start date moves back by that many days.
    """
    held = (sold_on - sold_holding_start).days
    return replacement_acquired_on - dt.timedelta(days=held)
