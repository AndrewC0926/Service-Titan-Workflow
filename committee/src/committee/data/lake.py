"""Raw zone and point-in-time Parquet lake (DESIGN 5).

Raw zone: every API response or filing stored once, keyed by source, entity and
known time; never mutated.

PIT lake: each table is a directory of immutable Parquet part files
(``<table>/known_date=YYYY-MM-DD/part-<ingest_id>.parquet``). Every row carries
``event_time``, ``known_time``, ``source`` and ``ingest_id``. Corrections are new
rows with a later known_time, never edits.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

LINEAGE_COLUMNS = ("event_time", "known_time", "source", "ingest_id")

# Point-in-time tables. Any query against these must use the as_of() macro
# (enforced by tests/test_lookahead_lint.py).
PIT_TABLES: tuple[str, ...] = (
    "security_master",
    "ticker_history",
    "prices_daily",
    "corporate_actions",
    "filings",
    "filing_sections",
    "insider_txns",
    "fundamentals",
    "macro_series",
    "news",
    "factor_returns",
    "risk_indexes",
    "short_interest",
    "holdings_13f",
    "estimates",
    "signals",
)

# Tables keyed by entity whose rows are "versions": the as-of value is the
# latest known_time per key.
TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "security_master": ("security_id",),
    "ticker_history": ("security_id", "ticker", "start_date"),
    "prices_daily": ("security_id", "date"),
    "corporate_actions": ("security_id", "ex_date", "kind"),
    "filings": ("accession",),
    "filing_sections": ("accession", "item"),
    "insider_txns": ("accession", "line"),
    "fundamentals": ("security_id", "metric", "fiscal_period"),
    "macro_series": ("series_id", "obs_date"),
    "news": ("news_id",),
    "factor_returns": ("dataset", "factor", "date"),
    "risk_indexes": ("index_id", "obs_date"),
    "short_interest": ("security_id", "settle_date"),
    "holdings_13f": ("filer_cik", "period", "security_id"),
    "estimates": ("security_id", "fiscal_period", "metric", "snapshot"),
    "signals": ("security_id", "signal_name", "asof"),
}


def new_ingest_id() -> str:
    """Sortable run id. ``PIT.latest`` breaks known_time ties by ingest_id, so ids must
    increase between runs; microseconds keep two runs in one second ordered (REVIEW R-09)."""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:8]


def _utc(t: dt.datetime) -> dt.datetime:
    if t.tzinfo is None:
        raise ValueError("known_time must be timezone-aware (UTC)")
    return t.astimezone(dt.UTC)


@dataclass(frozen=True)
class RawZone:
    root: Path

    def put(
        self, source: str, entity: str, known_time: dt.datetime, body: bytes, ext: str = "json"
    ) -> Path:
        """Store a response once. Identical content at the same key is a no-op."""
        kt = _utc(known_time).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha256(body).hexdigest()[:16]
        safe_entity = "".join(c if c.isalnum() or c in "-_." else "_" for c in entity)
        path = self.root / source / safe_entity / f"{kt}-{digest}.{ext}.gz"
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wb") as f:
            f.write(body)
        tmp.rename(path)
        path.chmod(0o444)
        return path

    def put_json(self, source: str, entity: str, known_time: dt.datetime, obj: Any) -> Path:
        return self.put(source, entity, known_time, json.dumps(obj, sort_keys=True).encode())

    def read(self, path: Path) -> bytes:
        with gzip.open(path, "rb") as f:
            return f.read()

    def list(self, source: str, entity: str | None = None) -> list[Path]:
        base = self.root / source
        if entity:
            base = base / entity
        return sorted(base.rglob("*.gz")) if base.exists() else []


@dataclass(frozen=True)
class Lake:
    root: Path

    def table_dir(self, table: str) -> Path:
        if table not in PIT_TABLES and table not in ("dead_letter",):
            raise ValueError(f"unknown lake table {table!r}")
        return self.root / table

    def write(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]] | pd.DataFrame,
        *,
        source: str,
        ingest_id: str,
        known_time_col: str = "known_time",
    ) -> int:
        """Append rows as a new immutable part file. Returns rows written."""
        df = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(list(rows))
        if df.empty:
            return 0
        if known_time_col not in df.columns:
            raise ValueError(f"{table}: every row needs {known_time_col}")
        kt = pd.to_datetime(df[known_time_col], utc=True)
        if kt.isna().any():
            raise ValueError(f"{table}: null known_time")
        df[known_time_col] = kt
        if "event_time" not in df.columns:
            raise ValueError(f"{table}: every row needs event_time")
        df["event_time"] = pd.to_datetime(df["event_time"], utc=True)
        df["source"] = source
        df["ingest_id"] = ingest_id
        written = 0
        for day, part in df.groupby(df[known_time_col].dt.strftime("%Y-%m-%d")):
            d = self.table_dir(table) / f"known_date={day}"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"part-{ingest_id}.parquet"
            if path.exists():
                raise FileExistsError(f"{path} already exists; parts are immutable")
            pq.write_table(pa.Table.from_pandas(part, preserve_index=False), path)
            written += len(part)
        return written

    def has_table(self, table: str) -> bool:
        d = self.table_dir(table)
        return d.exists() and any(d.rglob("*.parquet"))

    def glob(self, table: str) -> str:
        return str(self.table_dir(table) / "**" / "*.parquet")
