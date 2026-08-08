"""HCAI county-level healthcare construction activity import (Phase 6g) — a
county-level AGGREGATE layer, same shape as app/pipeline/iepr.py: no facility
names, no addresses, nothing a lead could be built from.

HCAI's per-project detail (facility names, the real plan-review-stage
tracking before construction starts) lives behind a login-gated eServices
portal (esp.hcai.ca.gov) — confirmed not publicly reachable without an
account. The only genuinely public HCAI data is the CHHS Open Data CSV,
aggregated by county + project status + a "data generation date" snapshot.

Manual import, not a live fetch, and not only because IEPR set that
precedent: the CSV's own download URL embeds a generation date that changes
with every ~biweekly CHHS update
(...download/ca-hcai-total-construction-cost-07232026.csv), and the JSON API
path that would let code discover the CURRENT filename automatically is
robots.txt-disallowed on data.chhs.ca.gov (/api/ and /datastore/* are both
blocked for all crawlers). There is no compliant way to auto-resolve "the
latest file" — download it by hand from
https://data.chhs.ca.gov/dataset/total-construction-cost-of-healthcare-projects
and import with `scout import-hcai`.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime

from sqlmodel import delete

from app.models import HcaiCountyActivity, utcnow

# "01 - Alameda" -> "Alameda"
_COUNTY_PREFIX = re.compile(r"^\d+\s*-\s*")


def _clean_county(raw: str) -> str | None:
    if not raw:
        return None
    return _COUNTY_PREFIX.sub("", raw).strip() or None


def _parse_money(raw: str) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def import_hcai_snapshot(session, path_or_text: str, *, source_url: str, is_path: bool = True) -> dict:
    """Import a downloaded CHHS 'Total Construction Cost of Healthcare
    Projects' CSV. The file is a historical log going back to 2013 (~330
    biweekly snapshots) — this keeps only the MOST RECENT snapshot date, on
    the same logic as IEPR: this represents current known state, not an
    accumulating time series. Replaces every prior row on each import."""
    text = open(path_or_text).read() if is_path else path_or_text
    reader = csv.DictReader(text.splitlines())

    rows = list(reader)
    if not rows:
        raise ValueError("CSV had no data rows")

    dates: dict[str, datetime] = {}
    max_date = None
    for row in rows:
        raw_date = row.get("Data Generation Date", "").strip()
        if raw_date not in dates:
            try:
                dates[raw_date] = datetime.strptime(raw_date, "%m/%d/%Y")
            except ValueError:
                continue
        parsed = dates[raw_date]
        if max_date is None or parsed > max_date:
            max_date = parsed
    if max_date is None:
        raise ValueError("no parseable 'Data Generation Date' values found")

    latest_str = next(d for d, parsed in dates.items() if parsed == max_date)

    session.exec(delete(HcaiCountyActivity))

    stored, skipped_counties = 0, set()
    for row in rows:
        if row.get("Data Generation Date", "").strip() != latest_str:
            continue
        county = _clean_county(row.get("County", ""))
        status = row.get("OSHPD Project Status", "").strip()
        if not county or not status:
            skipped_counties.add(row.get("County", "<blank>"))
            continue
        session.add(HcaiCountyActivity(
            county=county, status=status,
            total_cost=_parse_money(row.get("Total Costs of OSHPD Projects", "")),
            project_count=int(row["Number of OSHPD Projects"]) if row.get("Number of OSHPD Projects", "").strip().isdigit() else None,
            snapshot_date=max_date, source_url=source_url, imported_at=utcnow(),
        ))
        stored += 1

    session.flush()
    return {
        "snapshot_date": max_date.date().isoformat(),
        "rows_in_file": len(rows),
        "rows_stored": stored,
        "skipped": sorted(skipped_counties),
    }
