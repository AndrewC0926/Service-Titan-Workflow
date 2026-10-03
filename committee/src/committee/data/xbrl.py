"""EDGAR XBRL company facts -> ``fundamentals`` rows.

Each metric maps to candidate concepts in priority order (companies switch
concepts over time, e.g. SalesRevenueNet -> RevenueFromContractWith...). For a
given period and filing (accession) the highest-priority concept present wins.

Every filing that reports a value for a (metric, period) is a candidate version;
a row is emitted for the first report and whenever a later filing reports a
different value (a restatement), with known_time = that filing's acceptance
datetime (from ``filings`` when known, else the end of the ``filed`` date UTC).

fiscal_period labels: duration facts of ~1 year -> ``FY<year>``, ~1 quarter ->
``<year>Q<n>``; instants (balance sheet) -> ``<year>Q<n>``. Year/quarter are the
calendar quarter of (period end - 7 days), so 52/53-week years ending in the
first days of a month label as the prior month. Year-to-date (6/9 month)
durations are skipped. ``period_end`` holds the exact date.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from committee.data.common import end_of_day_utc

GAAP = "us-gaap"
DEI = "dei"
FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (
        (GAAP, "Revenues"),
        (GAAP, "RevenueFromContractWithCustomerExcludingAssessedTax"),
        (GAAP, "RevenueFromContractWithCustomerIncludingAssessedTax"),
        (GAAP, "SalesRevenueNet"),
    ),
    "gross_profit": ((GAAP, "GrossProfit"),),
    "operating_income": ((GAAP, "OperatingIncomeLoss"),),
    "net_income": ((GAAP, "NetIncomeLoss"), (GAAP, "ProfitLoss")),
    "cfo": (
        (GAAP, "NetCashProvidedByUsedInOperatingActivities"),
        (GAAP, "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    ),
    "capex": (
        (GAAP, "PaymentsToAcquirePropertyPlantAndEquipment"),
        (GAAP, "PaymentsToAcquireProductiveAssets"),
    ),
    "total_assets": ((GAAP, "Assets"),),
    "total_liabilities": ((GAAP, "Liabilities"),),
    "equity": (
        (GAAP, "StockholdersEquity"),
        (GAAP, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ),
    "cash": (
        (GAAP, "CashAndCashEquivalentsAtCarryingValue"),
        (GAAP, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "debt": (
        (GAAP, "LongTermDebt"),
        (GAAP, "LongTermDebtNoncurrent"),
        (GAAP, "DebtInstrumentCarryingAmount"),
    ),
    "shares_outstanding": (
        (GAAP, "CommonStockSharesOutstanding"),
        (DEI, "EntityCommonStockSharesOutstanding"),
    ),
    "interest_expense": (
        (GAAP, "InterestExpense"),
        (GAAP, "InterestExpenseNonoperating"),
        (GAAP, "InterestExpenseDebt"),
    ),
    "_da": (
        (GAAP, "DepreciationDepletionAndAmortization"),
        (GAAP, "DepreciationAndAmortization"),
        (GAAP, "DepreciationAmortizationAndAccretionNet"),
    ),
}
UNIT = {"shares_outstanding": "shares"}
# Derived: alias metrics and ebitda = operating income + D&A (same period and filing).
ALIASES = {"ebit": "operating_income", "book_value": "equity"}


def fiscal_label(start: dt.date | None, end: dt.date) -> str | None:
    ref = end - dt.timedelta(days=7)
    q = f"{ref.year}Q{(ref.month - 1) // 3 + 1}"
    if start is None:
        return q
    days = (end - start).days
    if 350 <= days <= 380:
        return f"FY{ref.year}"
    if 80 <= days <= 100:
        return q
    return None


@dataclass(frozen=True)
class _Fact:
    metric: str
    label: str
    accession: str
    value: float
    period_end: dt.date
    form: str
    filed: dt.date
    priority: int


def _collect(facts: Mapping[str, Any]) -> dict[tuple[str, str, str], _Fact]:
    best: dict[tuple[str, str, str], _Fact] = {}
    for metric, candidates in CONCEPTS.items():
        unit = UNIT.get(metric, "USD")
        for prio, (tax, concept) in enumerate(candidates):
            units = facts.get(tax, {}).get(concept, {}).get("units", {})
            for f in units.get(unit, []):
                form = str(f.get("form", ""))
                if form not in FORMS or "accn" not in f or "end" not in f:
                    continue
                end = dt.date.fromisoformat(f["end"])
                start = dt.date.fromisoformat(f["start"]) if f.get("start") else None
                label = fiscal_label(start, end)
                if label is None:
                    continue
                key = (metric, label, str(f["accn"]))
                cur = best.get(key)
                if cur is None or prio < cur.priority:
                    best[key] = _Fact(
                        metric,
                        label,
                        str(f["accn"]),
                        float(f["val"]),
                        end,
                        form,
                        dt.date.fromisoformat(f["filed"]),
                        prio,
                    )
    for (m, label, accn), fact in list(best.items()):
        for alias, src in ALIASES.items():
            if m == src:
                best[(alias, label, accn)] = replace(fact, metric=alias)
        da = best.get(("_da", label, accn))
        if m == "operating_income" and da is not None:
            best[("ebitda", label, accn)] = replace(
                fact, metric="ebitda", value=fact.value + da.value
            )
    return {k: v for k, v in best.items() if k[0] != "_da"}


def parse_companyfacts(
    body: bytes, security_id: str, accepted_at: Mapping[str, dt.datetime]
) -> list[dict[str, Any]]:
    """fundamentals rows: first report of each (metric, period) plus each restatement."""
    data = json.loads(body)
    best = _collect(data.get("facts", {}))

    def known(f: _Fact) -> dt.datetime:
        return accepted_at.get(f.accession) or end_of_day_utc(f.filed)

    series: dict[tuple[str, str], list[_Fact]] = {}
    for f in best.values():
        series.setdefault((f.metric, f.label), []).append(f)
    rows: list[dict[str, Any]] = []
    for (metric, label), versions in sorted(series.items()):
        last: float | None = None
        for f in sorted(versions, key=lambda f: (known(f), f.accession)):
            if last is not None and abs(f.value - last) <= 1e-9 * max(1.0, abs(last)):
                continue
            last = f.value
            rows.append(
                {
                    "security_id": security_id,
                    "metric": metric,
                    "fiscal_period": label,
                    "period_end": f.period_end,
                    "value": f.value,
                    "form": f.form,
                    "accession": f.accession,
                    "event_time": dt.datetime.combine(f.period_end, dt.time(), tzinfo=dt.UTC),
                    "known_time": known(f),
                }
            )
    return rows
