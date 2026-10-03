"""Persistent lot ledger in the state DB (SQLite, tables prefixed ``tax_``).

The ledger is event-sourced: purchases and sales are appended to
``tax_events`` and the in-memory ``LotLedger`` is rebuilt by replaying them in
(event date, insertion order). Replay is deterministic, so split lot ids and
wash-sale adjustments are reproducible, and a backdated event is applied in
its correct place. An event that makes the replay fail is rejected and not
stored.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from committee.domain import Lot
from committee.engines.tax.equivalence import Equivalence
from committee.engines.tax.ledger import LotLedger
from committee.engines.tax.models import SaleRequest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tax_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('buy', 'sell')),
    ref_id       TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tax_events_order ON tax_events(event_date, seq);
"""


class TaxLedgerStore:
    def __init__(
        self,
        path: Path | str,
        window_days: int = 30,
        equivalence: Equivalence | None = None,
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.executescript(_SCHEMA)
        self.window_days = window_days
        self.equivalence = equivalence or Equivalence()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TaxLedgerStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _events(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT kind, payload_json FROM tax_events ORDER BY event_date, seq"
        ).fetchall()
        return [(str(k), str(p)) for k, p in rows]

    def _replay(self, events: list[tuple[str, str]]) -> LotLedger:
        ledger = LotLedger(self.window_days, self.equivalence)
        for kind, payload in events:
            if kind == "buy":
                ledger.add_purchase(Lot.model_validate_json(payload))
            else:
                ledger.apply_sale(SaleRequest.model_validate_json(payload))
        return ledger

    def ledger(self) -> LotLedger:
        """Rebuild the in-memory ledger from all stored events."""
        return self._replay(self._events())

    def _append(self, kind: str, on: dt.date, ref: str, payload: str) -> LotLedger:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO tax_events(event_date, kind, ref_id, payload_json) VALUES (?,?,?,?)",
                (on.isoformat(), kind, f"{kind}:{ref}", payload),
            )
            ledger = self.ledger()
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        return ledger

    def record_purchase(self, lot: Lot) -> LotLedger:
        return self._append("buy", lot.acquired_on, lot.lot_id, lot.model_dump_json())

    def record_sale(self, sale: SaleRequest) -> LotLedger:
        return self._append("sell", sale.sold_on, sale.sale_id, sale.model_dump_json())

    def import_events(self, events: list[dict[str, object]]) -> LotLedger:
        """Import a list of {"kind": "buy"|"sell", ...} records atomically."""
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for ev in events:
                body = {k: v for k, v in ev.items() if k != "kind"}
                if ev.get("kind") == "buy":
                    lot = Lot.model_validate(body)
                    row = ("buy", lot.acquired_on, lot.lot_id, lot.model_dump_json())
                elif ev.get("kind") == "sell":
                    sale = SaleRequest.model_validate(body)
                    row = ("sell", sale.sold_on, sale.sale_id, sale.model_dump_json())
                else:
                    raise ValueError(f"unknown event kind {ev.get('kind')!r}")
                cur.execute(
                    "INSERT INTO tax_events(event_date, kind, ref_id, payload_json)"
                    " VALUES (?,?,?,?)",
                    (row[1].isoformat(), row[0], f"{row[0]}:{row[2]}", row[3]),
                )
            ledger = self.ledger()
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        return ledger

    def event_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tax_events").fetchone()[0])


def load_events_file(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise ValueError("events file must be a JSON list of objects")
    return data
