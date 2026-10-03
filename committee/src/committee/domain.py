"""Shared domain types used across engines, broker and evaluation."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountKind = Literal["taxable", "ira", "k401"]
Bucket = Literal["core_pick", "asymmetric_bet"]
Side = Literal["buy", "sell"]
Recommendation = Literal["BUY", "ADD", "HOLD", "TRIM", "SELL", "PASS", "WATCH"]
RiskVerdict = Literal["PASS", "RESIZE", "VETO"]

TAX_DEFERRED: frozenset[str] = frozenset({"ira", "k401"})


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Lot(Model):
    """A tax lot. Specific identification across all accounts."""

    lot_id: str
    account: AccountKind
    symbol: str
    qty: float = Field(gt=0)
    cost_per_share: float = Field(ge=0)
    acquired_on: dt.date


class Holding(Model):
    """A position as the risk and core engines see it (aggregated lots)."""

    account: AccountKind
    symbol: str
    qty: float
    price: float = Field(ge=0)
    sleeve: str  # policy sleeve id, or "satellite"
    bucket: Bucket | None = None  # satellite only
    sector: str | None = None
    themes: dict[str, float] = Field(default_factory=dict)  # fraction of the position exposed
    annual_vol: float | None = None
    opened_on: dt.date | None = None

    @property
    def market_value(self) -> float:
        return self.qty * self.price


class Trade(Model):
    """An executed trade (fill), used for wash-sale and turnover checks."""

    trade_id: str
    account: AccountKind
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    price: float = Field(ge=0)
    traded_on: dt.date
    realized_loss: float = 0.0  # positive number when a sale realized a loss
