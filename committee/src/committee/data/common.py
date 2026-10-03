"""Shared ingestion plumbing: run stats, per-table batching, dedupe, time helpers.

Each ingest run writes every table at most once (one immutable part file per
table per ``ingest_id``), so rows are accumulated in a ``Batch`` and flushed at
the end of the run.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from committee.config.secrets import Secrets
from committee.data.lake import Lake
from committee.data.pit import PIT
from committee.data.security_master import resolve
from committee.journal.store import Journal

NY = ZoneInfo("America/New_York")
FAR_FUTURE = "9999-12-31"


class IngestError(Exception):
    """A run-level failure with a message fit for the CLI (no traceback)."""


class MissingSecretError(IngestError):
    pass


def require_secret(secrets: Secrets, name: str) -> str:
    v = secrets.get(name)
    if not v:
        raise MissingSecretError(
            f"secret {name} is not set; add it to the OS keychain (service 'committee') or .env"
        )
    return v


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def end_of_day_utc(d: dt.date) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(23, 59, 59), tzinfo=dt.UTC)


def ny_time(d: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    """Wall-clock time in New York on ``d``, as UTC."""
    return dt.datetime.combine(d, dt.time(hour, minute), tzinfo=NY).astimezone(dt.UTC)


def to_date(v: Any) -> dt.date | None:
    if v is None or v == "" or (not isinstance(v, str | dt.date) and pd.isna(v)):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return pd.Timestamp(str(v)).date()


def norm_key(v: Any) -> str:
    """Canonical string for dedupe keys (dates, timestamps and numbers from DuckDB or Python)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, dt.date | pd.Timestamp):
        ts = pd.Timestamp(v)
        if ts.tzinfo is not None:
            return ts.tz_convert("UTC").isoformat()
        return ts.date().isoformat() if ts == ts.normalize() else ts.isoformat()
    if isinstance(v, float):
        return repr(round(v, 10))
    if isinstance(v, int) and not isinstance(v, bool):
        return repr(float(v))
    return str(v)


def existing_keys(pit: PIT, table: str, cols: Sequence[str]) -> set[tuple[str, ...]]:
    """Every distinct ``cols`` tuple stored in ``table`` (all versions)."""
    if not pit.has(table):
        return set()
    sel = ", ".join(cols)
    df = pit.query(f"SELECT DISTINCT {sel} FROM {table} WHERE as_of(known_time, $asof)", FAR_FUTURE)
    return {tuple(norm_key(v) for v in r) for r in df.itertuples(index=False, name=None)}


def drop_known(
    rows: Iterable[dict[str, Any]], known: set[tuple[str, ...]], cols: Sequence[str]
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        k = tuple(norm_key(r.get(c)) for c in cols)
        if k not in known:
            known.add(k)
            out.append(r)
    return out


@dataclass
class Batch:
    """Rows accumulated per table during one run; flushed once per table."""

    rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    dtypes: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def add(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        self.rows.setdefault(table, []).extend(rows)

    def dead_letter(
        self, entity: str, ref: str, error: str, known_time: dt.datetime | None = None
    ) -> None:
        t = known_time or utcnow()
        self.add(
            "dead_letter",
            [
                {
                    "entity": entity,
                    "ref": ref,
                    "error": error[:2000],
                    "event_time": t,
                    "known_time": t,
                }
            ],
        )

    def flush(self, lake: Lake, source: str, ingest_id: str) -> dict[str, int]:
        written = {}
        for table, rows in self.rows.items():
            if rows:
                df = pd.DataFrame(rows)
                for col, dtype in self.dtypes.get(table, {}).items():
                    if col in df.columns:
                        df[col] = df[col].astype(dtype)  # type: ignore[call-overload]
                written[table] = lake.write(table, df, source=source, ingest_id=ingest_id)
        self.rows.clear()
        return written


@dataclass
class RunStats:
    """Counts and errors for one ingest run; journaled as ``ingest_summary``."""

    source: str
    ingest_id: str
    started: float = field(default_factory=time.monotonic)
    counts: Counter[str] = field(default_factory=Counter)
    written: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ingest_id": self.ingest_id,
            "counts": dict(sorted(self.counts.items())),
            "rows_written": dict(sorted(self.written.items())),
            "dead_letters": self.written.get("dead_letter", 0),
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "duration_s": round(time.monotonic() - self.started, 3),
            **self.extra,
        }

    def finish(
        self, journal: Journal | None, *, attempted: int = 0, succeeded: int = 0
    ) -> dict[str, Any]:
        """Journal the run summary; raise if every fetch failed (network or bad key)."""
        failed = bool(attempted) and not succeeded
        p = self.payload()
        p["status"] = "failed" if failed else ("partial" if self.errors else "ok")
        p["fetches"] = {"attempted": attempted, "succeeded": succeeded}
        if journal is not None:
            journal.append("ingest_summary", p)
        if failed:
            first = self.errors[0] if self.errors else "unknown error"
            raise IngestError(
                f"{self.source}: all {attempted} fetches failed; first error: {first}"
            )
        return p


def resolve_tickers(
    pit: PIT, tickers: Sequence[str], asof: dt.datetime | dt.date | str
) -> tuple[dict[str, str], list[str]]:
    """Map tickers to security_ids via the security master; returns (resolved, missing)."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for t in tickers:
        sid = resolve(pit, t, asof)
        if sid is None:
            missing.append(t.upper())
        else:
            found[t.upper()] = sid
    return found, missing


def resolve_universe(
    pit: PIT, tickers: Sequence[str], asof: dt.datetime, stats: RunStats
) -> dict[str, str]:
    """Resolve tickers, logging unknown ones; fail if none resolve."""
    found, missing = resolve_tickers(pit, tickers, asof)
    if tickers and not found:
        raise IngestError(
            f"none of {', '.join(missing)} are in the security master; "
            "run `committee ingest securities` first"
        )
    for t in missing:
        stats.error(f"{t}: not in security master")
    return found


def cik_of(security_id: str) -> int:
    return int(security_id.removeprefix("CIK"))
