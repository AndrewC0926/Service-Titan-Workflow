"""Address -> (latitude, longitude) via the U.S. Census Bureau's public
Geocoder API -- free, no API key, no stated rate limit for reasonable
batch use. Confirmed live 2026-08-14: a real 3-address batch call against
geocoding.geo.census.gov returned exact/non-exact matches with real
coordinates for real CSLB contractor addresses.

Chosen over a paid/keyed geocoder for the same reason this codebase
prefers CEQAnet/SAM.gov/CSLB over anything requiring a commercial API:
zero new credentials, zero new billing surface, a federal government
source with no terms against this use.

Batch, not one-at-a-time: at import-time contractor volume (tens of
thousands of addresses in the 7-county/3-classification CSLB scope) or
retrofit-building volume (tens of thousands of replacement candidates),
one-request-per-address would mean tens of thousands of round trips. The
batch endpoint accepts up to 10,000 addresses per call as an uploaded CSV
and returns matches for all of them in one response -- confirmed live.
"""
from __future__ import annotations

import csv
import io
import logging

import httpx

log = logging.getLogger(__name__)

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
MAX_BATCH_SIZE = 10_000


def batch_geocode(addresses: dict[str, tuple[str, str, str, str]], *,
                  timeout: float = 120.0) -> dict[str, tuple[float, float] | None]:
    """addresses: {id: (street, city, state, zip)}. Returns {id: (lat, lon)
    or None if unmatched}. id is caller-defined (a license number, an APN)
    and is round-tripped through the Census API's own CSV id column, so
    the mapping back to the caller's rows is exact, not positional.

    Raises ValueError if more than MAX_BATCH_SIZE addresses are given --
    callers are responsible for chunking, not this function silently
    truncating a batch and returning partial results with no signal that
    anything was dropped.
    """
    if len(addresses) > MAX_BATCH_SIZE:
        raise ValueError(f"{len(addresses)} addresses exceeds the batch endpoint's "
                         f"{MAX_BATCH_SIZE}-address limit -- chunk before calling")
    if not addresses:
        return {}

    buf = io.StringIO()
    writer = csv.writer(buf)
    for id_, (street, city, state, zip_) in addresses.items():
        writer.writerow([id_, street, city, state, zip_])
    csv_bytes = buf.getvalue().encode("utf-8")

    resp = httpx.post(
        BATCH_URL,
        files={"addressFile": ("addresses.csv", csv_bytes, "text/csv")},
        data={"benchmark": BENCHMARK},
        timeout=timeout,
    )
    resp.raise_for_status()

    results: dict[str, tuple[float, float] | None] = {id_: None for id_ in addresses}
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        if len(row) < 6:
            continue
        id_, _input_addr, match_status, _match_type, _matched_addr, coords = row[:6]
        if id_ not in results:
            continue  # defensive: the API should only echo back ids we sent
        if match_status != "Match" or not coords:
            continue
        try:
            lon_str, lat_str = coords.split(",")
            results[id_] = (float(lat_str), float(lon_str))
        except ValueError:
            log.warning("geocode: could not parse coordinates %r for id %r", coords, id_)
    return results


def chunk_dict(d: dict, size: int) -> list[dict]:
    """Split a dict into a list of dicts of at most `size` items each,
    preserving key order -- the caller's ids stay intact per chunk, no
    positional reassembly needed."""
    items = list(d.items())
    return [dict(items[i:i + size]) for i in range(0, len(items), size)]
