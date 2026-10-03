"""Ken French Data Library factor returns -> ``factor_returns``.

The zipped CSVs have a free-text preamble, a header row starting with a comma,
data rows keyed YYYYMMDD (daily) or YYYYMM (monthly), then further blocks
(annual factors) and a copyright footer. Only the first data block is read.
Values are percent and are stored as decimals; -99.99 / -999 mean missing.
Monthly rows are dated at month end.

known_time = download time. The library publishes no per-observation release
date and revises history on each update, so the moment we fetched a value is
the earliest time we can prove it was public. Unchanged values are not
rewritten; revised values become new rows known at their download time.
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import re
import zipfile
from typing import Any

from committee.data.common import (
    Batch,
    RunStats,
    drop_known,
    existing_keys,
    utcnow,
)
from committee.data.http import Fetcher, FetchError
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.journal.store import Journal

FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
DATASETS: dict[str, str] = {
    "ff5_daily": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
    "ff5_monthly": "F-F_Research_Data_5_Factors_2x3_CSV.zip",
    "mom_daily": "F-F_Momentum_Factor_daily_CSV.zip",
    "mom_monthly": "F-F_Momentum_Factor_CSV.zip",
}
FACTOR_NAMES = {
    "mkt-rf": "mkt_rf",
    "smb": "smb",
    "hml": "hml",
    "rmw": "rmw",
    "cma": "cma",
    "rf": "rf",
    "mom": "mom",
}
SOURCE = "ken_french"
FACTOR_KEY = ("dataset", "factor", "date", "value")
_DATE = re.compile(r"^\d{6}(\d{2})?$")


def unzip_single(body: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = [n for n in z.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not names:
            raise ValueError("zip contains no CSV")
        return z.read(names[0]).decode("latin-1")


def _parse_date(s: str) -> dt.date:
    y, m = int(s[:4]), int(s[4:6])
    if len(s) == 8:
        return dt.date(y, m, int(s[6:]))
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def parse_french_csv(text: str) -> list[tuple[dt.date, str, float]]:
    """(date, factor, decimal value) from the first data block of a French CSV."""
    header: list[str] | None = None
    out: list[tuple[dt.date, str, float]] = []
    for raw in text.splitlines():
        cells = [c.strip() for c in raw.split(",")]
        if header is None:
            if raw.lstrip().startswith(",") and len(cells) > 1:
                header = [FACTOR_NAMES.get(c.lower(), c.lower()) for c in cells[1:]]
            continue
        if not _DATE.match(cells[0]):
            if out:
                break
            continue
        d = _parse_date(cells[0])
        for name, v in zip(header, cells[1:], strict=False):
            if not v:
                continue
            x = float(v)
            if x <= -99.99:
                continue
            out.append((d, name, round(x / 100.0, 10)))
    if header is None:
        raise ValueError("no header row found")
    return out


def ingest_factors(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    *,
    datasets: dict[str, str] | None = None,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    datasets = datasets or DATASETS
    stats = RunStats(SOURCE, new_ingest_id())
    pit = PIT(lake)
    try:
        known = existing_keys(pit, "factor_returns", FACTOR_KEY)
    finally:
        pit.close()
    batch, ok = Batch(), 0
    for ds, fname in datasets.items():
        try:
            body = fetcher.get(FRENCH_BASE + fname)
            raw.put(SOURCE, ds, now, body, ext="zip")
            obs = parse_french_csv(unzip_single(body))
        except (FetchError, ValueError, zipfile.BadZipFile) as e:
            stats.error(f"{ds}: {e}")
            continue
        ok += 1
        rows = [
            {
                "dataset": ds,
                "factor": f,
                "date": d,
                "value": v,
                "event_time": dt.datetime.combine(d, dt.time(), tzinfo=dt.UTC),
                "known_time": now,
            }
            for d, f, v in obs
        ]
        rows = drop_known(rows, known, FACTOR_KEY)
        batch.add("factor_returns", rows)
        stats.counts[ds] += len(rows)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(datasets), succeeded=ok)
