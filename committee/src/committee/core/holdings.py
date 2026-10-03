"""Holdings across taxable, IRA and 401(k).

The broker supplies the taxable account; IRA and 401(k) positions come from a
manual CSV import (``account,symbol,qty`` with optional ``cost_per_share,acquired_on``).
Cash is the pseudo-symbol ``CASH`` (qty = dollars). Snapshots are append-only.
"""

from __future__ import annotations

import csv
import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from committee.domain import AccountKind

CASH = "CASH"
ACCOUNTS: tuple[str, ...] = get_args(AccountKind)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_positions (
    snapshot_id TEXT NOT NULL,
    account     TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    qty         REAL NOT NULL,
    cost_per_share REAL,
    acquired_on TEXT,
    source      TEXT NOT NULL,
    taken_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS core_positions_acct ON core_positions(account, taken_at_utc);
"""


@dataclass(frozen=True)
class Position:
    account: AccountKind
    symbol: str
    qty: float
    cost_per_share: float | None = None
    acquired_on: dt.date | None = None


class ImportError_(ValueError):
    pass


def parse_positions_csv(path: Path) -> list[Position]:
    out: list[Position] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = {"account", "symbol", "qty"} - set(reader.fieldnames or [])
        if missing:
            raise ImportError_(f"{path.name}: missing columns {sorted(missing)}")
        for i, row in enumerate(reader, start=2):
            acct = row["account"].strip().lower().replace("401k", "k401").replace("401(k)", "k401")
            if acct not in ACCOUNTS:
                raise ImportError_(f"{path.name}:{i}: unknown account {row['account']!r}")
            try:
                qty = float(row["qty"])
                cps = float(row["cost_per_share"]) if row.get("cost_per_share") else None
                acq = dt.date.fromisoformat(row["acquired_on"]) if row.get("acquired_on") else None
            except ValueError as e:
                raise ImportError_(f"{path.name}:{i}: {e}") from None
            if qty < 0:
                raise ImportError_(
                    f"{path.name}:{i}: negative quantity (short positions are prohibited)"
                )
            out.append(Position(acct, row["symbol"].strip().upper(), qty, cps, acq))  # type: ignore[arg-type]
    return out


class HoldingsStore:
    def __init__(self, db: Path) -> None:
        db.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(db)
        self.con.executescript(_SCHEMA)

    def close(self) -> None:
        self.con.close()

    def snapshot(
        self, account: AccountKind, positions: list[Position], source: str, now: dt.datetime
    ) -> str:
        """Replace the account's current positions with a new snapshot (old ones kept)."""
        sid = f"{account}-{now.strftime('%Y%m%dT%H%M%S%f')}"
        rows = [
            (
                sid,
                account,
                p.symbol,
                p.qty,
                p.cost_per_share,
                p.acquired_on.isoformat() if p.acquired_on else None,
                source,
                now.isoformat(),
            )
            for p in positions
            if p.account == account
        ]
        if not rows:  # an empty account is still a snapshot
            rows = [(sid, account, CASH, 0.0, None, None, source, now.isoformat())]
        with self.con:
            self.con.executemany("INSERT INTO core_positions VALUES (?,?,?,?,?,?,?,?)", rows)
        return sid

    def import_csv(self, path: Path, now: dt.datetime) -> dict[str, int]:
        positions = parse_positions_csv(path)
        counts: dict[str, int] = {}
        for acct in sorted({p.account for p in positions}):
            self.snapshot(acct, positions, f"csv:{path.name}", now)
            counts[acct] = sum(1 for p in positions if p.account == acct)
        return counts

    def current(self) -> list[Position]:
        out: list[Position] = []
        for acct in ACCOUNTS:
            r = self.con.execute(
                "SELECT snapshot_id FROM core_positions WHERE account=? ORDER BY taken_at_utc DESC, rowid DESC LIMIT 1",
                (acct,),
            ).fetchone()
            if not r:
                continue
            for sym, qty, cps, acq in self.con.execute(
                "SELECT symbol, qty, cost_per_share, acquired_on FROM core_positions WHERE snapshot_id=?",
                (r[0],),
            ):
                if qty:
                    out.append(
                        Position(acct, sym, qty, cps, dt.date.fromisoformat(acq) if acq else None)  # type: ignore[arg-type]
                    )
        return out


def apply_fill(
    store: HoldingsStore,
    account: AccountKind,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    on: dt.date,
    now: dt.datetime,
) -> str:
    """New snapshot of ``account`` after a fill: shares and CASH move together."""
    cur = [p for p in store.current() if p.account == account]
    sign = 1.0 if side == "buy" else -1.0
    out: list[Position] = []
    found = False
    for p in cur:
        if p.symbol == symbol:
            found = True
            q = p.qty + sign * qty
            if q < -1e-9:
                raise ValueError(
                    f"fill would make {symbol} negative in {account} (short selling is prohibited)"
                )
            if q > 1e-9:
                out.append(Position(account, symbol, q, p.cost_per_share, p.acquired_on))
        elif p.symbol != CASH:
            out.append(p)
    if not found:
        if side == "sell":
            raise ValueError(f"sell fill for {symbol} not held in {account}")
        out.append(Position(account, symbol, qty, price, on))
    cash = sum(p.qty for p in cur if p.symbol == CASH) - sign * qty * price
    out.append(Position(account, CASH, cash))
    return store.snapshot(account, out, f"fill:{symbol}", now)
