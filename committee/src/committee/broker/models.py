"""Broker-side models: proposals the gate can approve, approvals, orders, fills."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from committee.domain import AccountKind, Model, Side

APPROVABLE_ENTRY_TYPES: frozenset[str] = frozenset(
    {"briefing", "core_proposal", "harvest_proposal"}
)
Severity = Literal["none", "caution", "stop"]
Venue = Literal["broker", "manual"]


class OrderLeg(Model):
    """One recommended trade inside a briefing or proposal (the ceiling the human may approve)."""

    symbol: str
    side: Side
    account: AccountKind
    max_pct_total: float = Field(gt=0, le=100)


class Approvable(Model):
    """The part of a journaled briefing/proposal payload the approval gate reads.

    Briefings and proposals must include these keys in their payload.
    """

    recommendation: str
    legs: list[OrderLeg]
    cooling_off_hours: int = Field(ge=0)
    behavioral_severity: Severity = "none"


class ApprovedLeg(Model):
    symbol: str
    side: Side
    account: AccountKind
    pct_total: float = Field(gt=0, le=100)


class ApprovalRecord(Model):
    briefing_hash: str
    briefing_seq: int
    briefing_type: str
    action: str
    legs: list[ApprovedLeg]
    reason: str
    justification: str | None = None
    account_value_usd: float = Field(gt=0)
    approved_at_utc: dt.datetime
    approver: str = "human"


class OrderRequest(Model):
    client_order_id: str
    symbol: str
    side: Side
    qty: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    time_in_force: Literal["day", "gtc"]
    account: AccountKind


class BrokerOrder(Model):
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    limit_price: float | None
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    filled_at: dt.datetime | None = None


class Fill(Model):
    fill_id: str  # broker order id + cumulative qty: stable for dedupe
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    filled_at: dt.datetime
    account: AccountKind
