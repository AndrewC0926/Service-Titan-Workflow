"""DuckDB analytics over the PIT lake, with the as_of() macro every feature query uses.

    as_of(known_time, :asof)   -- true when the row was knowable at :asof

Use ``PIT.query`` with SQL that applies ``as_of(known_time, $asof)`` to every PIT
table it reads, or ``PIT.latest`` for "latest version per key as of" reads.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import duckdb
import pandas as pd

from committee.data.lake import PIT_TABLES, TABLE_KEYS, Lake

ASOF_MACRO = "CREATE OR REPLACE MACRO as_of(kt, t) AS kt <= CAST(t AS TIMESTAMPTZ)"


def to_utc(t: dt.datetime | dt.date | str) -> dt.datetime:
    """Normalize an as-of value. A bare date means end of that day, 23:59:59 UTC."""
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t) if "T" in t or " " in t else dt.date.fromisoformat(t)
    if isinstance(t, dt.datetime):
        return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)
    return dt.datetime.combine(t, dt.time(23, 59, 59), tzinfo=dt.UTC)


class PIT:
    def __init__(self, lake: Lake) -> None:
        self.lake = lake
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone='UTC'")
        self.con.execute(ASOF_MACRO)
        self.refresh()

    def refresh(self) -> None:
        for t in PIT_TABLES:
            if self.lake.has_table(t):
                self.con.execute(
                    f"CREATE OR REPLACE VIEW {t} AS SELECT * EXCLUDE (known_date) FROM "
                    f"read_parquet('{self.lake.glob(t)}', hive_partitioning=true, union_by_name=true)"
                )
            else:
                self.con.execute(f"DROP VIEW IF EXISTS {t}")

    def has(self, table: str) -> bool:
        return self.lake.has_table(table)

    def close(self) -> None:
        self.con.close()

    def query(self, sql: str, asof: dt.datetime | dt.date | str, **params: Any) -> pd.DataFrame:
        """Run SQL with ``$asof`` bound. The SQL must filter PIT tables with as_of()."""
        params = {"asof": to_utc(asof), **params}
        used = {k: v for k, v in params.items() if f"${k}" in sql}
        return self.con.execute(sql, used).df()

    def latest(
        self,
        table: str,
        asof: dt.datetime | dt.date | str,
        where: str = "TRUE",
        **params: Any,
    ) -> pd.DataFrame:
        """Latest version of each key in ``table`` knowable at ``asof``."""
        if not self.has(table):
            return pd.DataFrame()
        keys = ", ".join(TABLE_KEYS[table])
        sql = (
            f"SELECT * FROM {table} WHERE as_of(known_time, $asof) AND ({where}) "
            f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY known_time DESC, ingest_id DESC) = 1"
        )
        return self.query(sql, asof, **params)
