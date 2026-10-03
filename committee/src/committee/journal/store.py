"""Append-only, SHA-256 hash-chained journal in SQLite (DESIGN 11).

hash = SHA-256(prev_hash + canonical_json({seq, entry_type, created_at_utc, payload}))

Covering seq, type and timestamp (not just the payload) means no field of a
row can be altered without breaking the chain. UPDATE and DELETE are blocked
by triggers; verify() recomputes every link.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from committee.journal.canonical import canonical_json

GENESIS_HASH = "0" * 64

ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "ingest_summary",
        "dq_report",
        "agent_output",
        "forecast",
        "forecast_resolution",
        "briefing",
        "state_transition",
        "decision",
        "approval",
        "order",
        "fill",
        "config_change",
        "allocator_decision",
        "incident",
        "kill_switch",
        "trial",
        "screen",
        "core_proposal",
        "harvest_proposal",
        "agent_weights",
        "recall_probe",
        "live_gate",
        "anchor",
        "backup",
        "note",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq            INTEGER PRIMARY KEY,
    entry_type     TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    prev_hash      TEXT NOT NULL,
    hash           TEXT NOT NULL UNIQUE,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_type ON journal(entry_type, seq);
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only: UPDATE blocked'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only: DELETE blocked'); END;
"""


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def compute_hash(
    prev_hash: str, seq: int, entry_type: str, created_at_utc: str, payload_json: str
) -> str:
    body = canonical_json(
        {
            "seq": seq,
            "entry_type": entry_type,
            "created_at_utc": created_at_utc,
            "payload": json.loads(payload_json),
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    entry_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at_utc: str

    @property
    def created_at(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.created_at_utc)


@dataclass
class VerifyReport:
    ok: bool
    entries: int
    last_seq: int
    last_hash: str
    errors: list[str] = field(default_factory=list)

    @property
    def first_break(self) -> str | None:
        return self.errors[0] if self.errors else None


class Journal:
    """The hash-chained journal. Every module writes through ``append``."""

    def __init__(self, path: Path | str, clock: Callable[[], dt.datetime] = utcnow) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute(
            "PRAGMA journal_mode=WAL" if str(path) != ":memory:" else "PRAGMA foreign_keys=ON"
        )
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ write
    def append(self, entry_type: str, payload: dict[str, Any] | Any) -> JournalEntry:
        if entry_type not in ENTRY_TYPES:
            raise ValueError(f"unknown journal entry_type {entry_type!r}")
        payload_json = canonical_json(payload)
        if not payload_json.startswith("{"):
            raise ValueError("journal payload must be a JSON object")
        created = self._clock()
        if created.tzinfo is None:
            raise ValueError("journal clock must return aware UTC datetimes")
        created_s = created.astimezone(dt.UTC).isoformat()
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (row[0] + 1) if row else 1
            prev = row[1] if row else GENESIS_HASH
            h = compute_hash(prev, seq, entry_type, created_s, payload_json)
            cur.execute(
                "INSERT INTO journal(seq, entry_type, payload_json, prev_hash, hash, created_at_utc)"
                " VALUES (?,?,?,?,?,?)",
                (seq, entry_type, payload_json, prev, h, created_s),
            )
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        return JournalEntry(seq, entry_type, json.loads(payload_json), prev, h, created_s)

    # ------------------------------------------------------------------- read
    @staticmethod
    def _row(r: tuple[Any, ...]) -> JournalEntry:
        return JournalEntry(r[0], r[1], json.loads(r[2]), r[3], r[4], r[5])

    def entries(self, entry_type: str | None = None, since_seq: int = 0) -> Iterator[JournalEntry]:
        q = "SELECT seq, entry_type, payload_json, prev_hash, hash, created_at_utc FROM journal WHERE seq > ?"
        args: list[Any] = [since_seq]
        if entry_type:
            q += " AND entry_type = ?"
            args.append(entry_type)
        for r in self._conn.execute(q + " ORDER BY seq", args):
            yield self._row(r)

    def latest(self, entry_type: str | None = None) -> JournalEntry | None:
        q = "SELECT seq, entry_type, payload_json, prev_hash, hash, created_at_utc FROM journal"
        args: list[Any] = []
        if entry_type:
            q += " WHERE entry_type = ?"
            args.append(entry_type)
        r = self._conn.execute(q + " ORDER BY seq DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def get(self, seq: int) -> JournalEntry | None:
        r = self._conn.execute(
            "SELECT seq, entry_type, payload_json, prev_hash, hash, created_at_utc FROM journal WHERE seq=?",
            (seq,),
        ).fetchone()
        return self._row(r) if r else None

    def find_by_hash(self, h: str) -> JournalEntry | None:
        r = self._conn.execute(
            "SELECT seq, entry_type, payload_json, prev_hash, hash, created_at_utc FROM journal WHERE hash=?",
            (h,),
        ).fetchone()
        return self._row(r) if r else None

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    # ----------------------------------------------------------------- verify
    def verify(self) -> VerifyReport:
        prev = GENESIS_HASH
        expected_seq = 1
        errors: list[str] = []
        n = 0
        last_hash = GENESIS_HASH
        for r in self._conn.execute(
            "SELECT seq, entry_type, payload_json, prev_hash, hash, created_at_utc FROM journal ORDER BY seq"
        ):
            seq, etype, pj, ph, h, created = r
            n += 1
            if seq != expected_seq:
                errors.append(f"seq gap: expected {expected_seq}, found {seq}")
            if ph != prev:
                errors.append(f"seq {seq}: prev_hash does not match previous entry")
            try:
                if canonical_json(json.loads(pj)) != pj:
                    errors.append(f"seq {seq}: payload is not canonical JSON")
                recomputed = compute_hash(ph, seq, etype, created, pj)
            except (ValueError, TypeError) as e:
                errors.append(f"seq {seq}: unreadable payload ({e})")
                recomputed = ""
            if recomputed != h:
                errors.append(f"seq {seq}: hash mismatch (row altered)")
            prev = h
            last_hash = h
            expected_seq = seq + 1
        return VerifyReport(
            ok=not errors, entries=n, last_seq=expected_seq - 1, last_hash=last_hash, errors=errors
        )

    def export_anchor(self) -> dict[str, Any]:
        last = self.latest()
        return {
            "seq": last.seq if last else 0,
            "hash": last.hash if last else GENESIS_HASH,
            "anchored_at_utc": self._clock().astimezone(dt.UTC).isoformat(),
        }
