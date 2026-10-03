"""Read holdings from CSV and build an illustrative demo portfolio.

CSV columns (header required; only the first four plus ``sleeve`` are
mandatory)::

    account,symbol,qty,price,sleeve,bucket,sector,themes,annual_vol,opened_on

``themes`` is ``name:fraction`` pairs separated by ``;`` (for example
``ai_capex_chain:0.6;taiwan_supply_chain:0.3``). Empty cells mean "unset".
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any

from committee.config.schema import PolicyPortfolio
from committee.domain import Holding

DEMO_TOTAL_VALUE = 1_000_000.0
DEMO_PRICE = 100.0

# (symbol, bucket, sector, themes, annual_vol, pct of total)
DEMO_SATELLITE: tuple[tuple[str, str, str, dict[str, float], float, float], ...] = (
    (
        "DEMO_SEMI",
        "core_pick",
        "Information Technology",
        {"ai_capex_chain": 0.8, "taiwan_supply_chain": 0.5},
        0.45,
        4.0,
    ),
    ("DEMO_ENERGY", "core_pick", "Energy", {"energy_price": 1.0}, 0.30, 3.0),
    ("DEMO_UTIL", "core_pick", "Utilities", {"ai_capex_chain": 0.3}, 0.20, 3.0),
    ("DEMO_BANK", "core_pick", "Financials", {}, 0.28, 3.0),
    ("DEMO_CHINA", "core_pick", "Consumer Discretionary", {"china_revenue": 0.4}, 0.35, 3.0),
    (
        "DEMO_ASYM_AI",
        "asymmetric_bet",
        "Information Technology",
        {"ai_capex_chain": 0.6},
        0.80,
        2.0,
    ),
    ("DEMO_ASYM_BIO", "asymmetric_bet", "Health Care", {}, 0.90, 2.0),
)


def parse_themes(cell: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in cell.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, val = part.partition(":")
        if not sep:
            raise ValueError(f"theme entry {part!r} must look like name:fraction")
        out[name.strip()] = float(val)
    return out


def _opt(row: dict[str, str], key: str) -> str | None:
    v = (row.get(key) or "").strip()
    return v or None


def read_holdings_csv(path: Path) -> list[Holding]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        out: list[Holding] = []
        for i, row in enumerate(reader, start=2):
            try:
                data: dict[str, Any] = {
                    "account": row["account"].strip(),
                    "symbol": row["symbol"].strip(),
                    "qty": float(row["qty"]),
                    "price": float(row["price"]),
                    "sleeve": row["sleeve"].strip(),
                    "bucket": _opt(row, "bucket"),
                    "sector": _opt(row, "sector"),
                    "themes": parse_themes(row.get("themes") or ""),
                }
                vol = _opt(row, "annual_vol")
                if vol is not None:
                    data["annual_vol"] = float(vol)
                opened = _opt(row, "opened_on")
                if opened is not None:
                    data["opened_on"] = dt.date.fromisoformat(opened)
                out.append(Holding.model_validate(data))
            except (KeyError, ValueError) as e:
                raise ValueError(f"{path}:{i}: {e}") from e
    return out


def demo_portfolio(policy: PolicyPortfolio, total_value: float = DEMO_TOTAL_VALUE) -> list[Holding]:
    """Policy sleeves at target weight plus an illustrative 20% satellite.

    Every price is $100. Satellite names are placeholders (``DEMO_*``), not
    recommendations. The satellite is scaled to the policy's satellite target.
    """
    holdings: list[Holding] = []
    for s in policy.sleeves:
        if s.holding is None or s.target_pct <= 0:
            continue
        value = total_value * s.target_pct / 100.0
        holdings.append(
            Holding(
                account=s.location[0],
                symbol=s.holding,
                qty=value / DEMO_PRICE,
                price=DEMO_PRICE,
                sleeve=s.id,
            )
        )
    sat_target = sum(s.target_pct for s in policy.sleeves if s.kind == "satellite")
    demo_sum = sum(row[5] for row in DEMO_SATELLITE)
    scale = sat_target / demo_sum if demo_sum else 0.0
    for symbol, bucket, sector, themes, vol, pct in DEMO_SATELLITE:
        if scale <= 0:
            break
        value = total_value * pct * scale / 100.0
        holdings.append(
            Holding.model_validate(
                {
                    "account": "ira",
                    "symbol": symbol,
                    "qty": value / DEMO_PRICE,
                    "price": DEMO_PRICE,
                    "sleeve": "satellite",
                    "bucket": bucket,
                    "sector": sector,
                    "themes": themes,
                    "annual_vol": vol,
                }
            )
        )
    return holdings
