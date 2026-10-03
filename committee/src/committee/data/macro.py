"""FRED/ALFRED macro series with vintages -> ``macro_series``.

``series/observations`` is called with a real-time window, so each returned
observation carries the vintage (``realtime_start``) during which that value
was the published one. Each (obs_date, vintage) becomes a row with
``vintage_date = realtime_start`` and ``known_time`` = end of the vintage day
(23:59:59 UTC). Releases happen during the U.S. business day (typically 08:30
or 10:00 ET), so end of day is a conservative choice that never leaks a value
before it was public. ALFRED clips realtime_start to the requested window
start, which can only make the first vintage later (conservative).
Missing values (``"."``) are skipped.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

from committee.data.common import (
    Batch,
    RunStats,
    drop_known,
    end_of_day_utc,
    existing_keys,
    utcnow,
)
from committee.data.http import Fetcher, FetchError
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.journal.store import Journal

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_SERIES: tuple[str, ...] = (
    "DFF",
    "DGS10",
    "DGS2",
    "T10Y3M",
    "T10YIE",
    "THREEFYTP10",
    "CPIAUCSL",
    "PCEPILFE",
    "UNRATE",
    "DCOILBRENTEU",
    "DTWEXBGS",
)
SOURCE = "fred"
MACRO_KEY = ("series_id", "obs_date", "vintage_date", "value")


def parse_observations(body: bytes, series_id: str) -> list[dict[str, Any]]:
    data = json.loads(body)
    if "observations" not in data:
        raise ValueError(f"{series_id}: no observations ({data.get('error_message', '')})")
    rows = []
    for o in data["observations"]:
        if o.get("value") in (".", "", None):
            continue
        obs = dt.date.fromisoformat(o["date"])
        vintage = dt.date.fromisoformat(o["realtime_start"])
        rows.append(
            {
                "series_id": series_id,
                "obs_date": obs,
                "value": float(o["value"]),
                "vintage_date": vintage,
                "event_time": dt.datetime.combine(obs, dt.time(), tzinfo=dt.UTC),
                "known_time": end_of_day_utc(vintage),
            }
        )
    return rows


def ingest_macro(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    api_key: str,
    *,
    since: dt.date,
    series: Sequence[str] = DEFAULT_SERIES,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    stats = RunStats(SOURCE, new_ingest_id(), extra={"series": list(series)})
    pit = PIT(lake)
    try:
        known = existing_keys(pit, "macro_series", MACRO_KEY)
    finally:
        pit.close()
    batch, ok = Batch(), 0
    for sid in series:
        params = {
            "series_id": sid,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": since.isoformat(),
            "realtime_start": since.isoformat(),
            "realtime_end": "9999-12-31",
        }
        try:
            body = fetcher.get(FRED_OBS_URL, params)
            raw.put(SOURCE, sid, now, body)
            rows = parse_observations(body, sid)
        except (FetchError, ValueError, KeyError) as e:
            stats.error(f"{sid}: {e}")
            continue
        ok += 1
        rows = drop_known(rows, known, MACRO_KEY)
        batch.add("macro_series", rows)
        stats.counts[sid] += len(rows)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(series), succeeded=ok)
