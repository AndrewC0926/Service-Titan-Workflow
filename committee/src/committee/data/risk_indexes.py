"""Geopolitical Risk (Caldara-Iacoviello) and Economic Policy Uncertainty indexes.

Parsed from CSV exports (the publishers' spreadsheets saved as CSV):

- ``gpr``: monthly, columns ``month`` (a date) and ``GPR``.
- ``gpr_daily``: daily, columns ``date`` (YYYYMMDD or ISO) and ``GPRD``.
- ``epu``: U.S. monthly, columns ``Year``, ``Month`` and
  ``News_Based_Policy_Uncert_Index`` (falls back to ``Three_Component_Index``).

Monthly observations are dated at month end. known_time is the release date:
neither file states it, so the first time an observation is seen we use the
publisher's normal release lag after the period (``RELEASE_LAG``), capped at
download time; a later change to an already stored value is a revision and is
known at its download time.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
from collections.abc import Callable
from typing import Any

import pandas as pd

from committee.data.common import (
    Batch,
    RunStats,
    end_of_day_utc,
    norm_key,
    utcnow,
)
from committee.data.http import Fetcher, FetchError
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.journal.store import Journal

# Publisher files are spreadsheets (.xls / .xlsx). They are read with pandas when an
# Excel engine (xlrd / openpyxl) is installed; otherwise point these at CSV exports.
URLS: dict[str, str] = {
    "gpr": "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls",
    "gpr_daily": "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls",
    "epu": "https://www.policyuncertainty.com/media/US_Policy_Uncertainty_Data.xlsx",
}
RELEASE_LAG: dict[str, dt.timedelta] = {
    "gpr": dt.timedelta(days=10),
    "gpr_daily": dt.timedelta(days=7),
    "epu": dt.timedelta(days=30),
}
SOURCE = "risk_indexes"
_XLS_MAGIC = (b"\xd0\xcf\x11\xe0", b"PK\x03\x04")


def _month_end(y: int, m: int) -> dt.date:
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def _rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse_gpr_monthly(text: str) -> list[tuple[dt.date, float]]:
    out = []
    for r in _rows(text):
        v = _num(r.get("GPR", ""))
        if v is None or not r.get("month"):
            continue
        d = pd.Timestamp(r["month"])
        out.append((_month_end(d.year, d.month), v))
    return out


def parse_gpr_daily(text: str) -> list[tuple[dt.date, float]]:
    out = []
    for r in _rows(text):
        v, s = _num(r.get("GPRD", "")), r.get("date", "").removesuffix(".0")
        if v is None or not s:
            continue
        d = dt.date(int(s[:4]), int(s[4:6]), int(s[6:8])) if s.isdigit() else pd.Timestamp(s).date()
        out.append((d, v))
    return out


def parse_epu(text: str) -> list[tuple[dt.date, float]]:
    out = []
    for r in _rows(text):
        y, m = r.get("Year", ""), r.get("Month", "")
        v = _num(r.get("News_Based_Policy_Uncert_Index") or r.get("Three_Component_Index") or "")
        if v is None or not y.isdigit() or not m.isdigit():
            continue
        out.append((_month_end(int(y), int(m)), v))
    return out


PARSERS: dict[str, Callable[[str], list[tuple[dt.date, float]]]] = {
    "gpr": parse_gpr_monthly,
    "gpr_daily": parse_gpr_daily,
    "epu": parse_epu,
}


def decode_csv(body: bytes) -> str:
    """CSV text from a CSV body or, when an Excel engine is installed, a spreadsheet."""
    if body[:4] not in _XLS_MAGIC:
        return body.decode("utf-8", errors="replace")
    try:
        df = pd.read_excel(io.BytesIO(body))
    except (ImportError, ValueError) as e:
        raise ValueError(
            f"cannot read spreadsheet ({e}); install an Excel engine or use a CSV export"
        ) from None
    return str(df.to_csv(index=False))


def index_rows(
    index_id: str,
    obs: list[tuple[dt.date, float]],
    stored: dict[tuple[str, str], str],
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """New or revised observations with their release-date known_time."""
    rows = []
    for d, v in obs:
        key = (index_id, d.isoformat())
        prev = stored.get(key)
        if prev == norm_key(v):
            continue
        known = now if prev is not None else min(now, end_of_day_utc(d + RELEASE_LAG[index_id]))
        stored[key] = norm_key(v)
        rows.append(
            {
                "index_id": index_id,
                "obs_date": d,
                "value": v,
                "event_time": dt.datetime.combine(d, dt.time(), tzinfo=dt.UTC),
                "known_time": known,
            }
        )
    return rows


def ingest_risk_indexes(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    *,
    urls: dict[str, str] | None = None,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    urls = urls or URLS
    stats = RunStats(SOURCE, new_ingest_id())
    pit = PIT(lake)
    try:
        # Latest stored value per key, to tell revisions from first sightings.
        latest = pit.latest("risk_indexes", now)
    finally:
        pit.close()
    stored = (
        {
            (str(r["index_id"]), norm_key(r["obs_date"])): norm_key(float(r["value"]))
            for _, r in latest.iterrows()
        }
        if not latest.empty
        else {}
    )
    batch, ok = Batch(), 0
    for index_id, url in urls.items():
        try:
            body = fetcher.get(url)
            raw.put(SOURCE, index_id, now, body, ext="csv")
            obs = PARSERS[index_id](decode_csv(body))
        except (FetchError, ValueError, KeyError) as e:
            stats.error(f"{index_id}: {e}")
            continue
        ok += 1
        rows = index_rows(index_id, obs, stored, now)
        batch.add("risk_indexes", rows)
        stats.counts[index_id] += len(rows)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(urls), succeeded=ok)
