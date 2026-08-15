"""Contractor licensing data from CSLB (California Contractors State
License Board) -- see app/models.py's Contractor docstring for what gets
stored and why.

Compliance, checked before writing any of this (2026-08-14): no robots.txt
exists on www.cslb.ca.gov, cslb.ca.gov, or web.cslb.ca.gov (confirmed via
direct fetch -- real 404s). CSLB's own Conditions of Use (linked from their
data portal, ca.gov/use/) states no restriction on automated or bulk
access. CSLB's own Public Data Portal (cslb.ca.gov/onlineservices/
dataportal/) publishes a free, no-login, self-service "License Master"
bulk download refreshed monthly -- this is the sanctioned intended use,
not a scrape of a search UI meant for one-record-at-a-time human lookups.

Mechanism: the portal is ASP.NET WebForms. Selecting "License Master" from
a dropdown, then clicking its CSV download control, are both postbacks
(there is no static download URL) -- _fetch_license_master_csv replays
that exact sequence: GET the form, POST the dropdown selection, POST the
CSV button, follow the redirect to the actual file at
/OnlineServices/DataPortal/DownLoadFile.ashx. Confirmed working end to end
2026-08-14 against the real site (244,100 real license records).

Scope: CLASSIFICATIONS x COUNTIES, applied here after download -- CSLB's
own bulk file is statewide/all-classification; there is no server-side
filter to request narrower. Nevada (Washoe, Storey) has no equivalent:
NSCB (Nevada State Contractors Board) publishes no bulk download, only a
live per-record search tool and a manual Public Records Request process --
scraping the search tool to reconstruct a bulk list would be exactly the
workaround this project was told not to do, so Nevada coverage does not
exist here.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime

import httpx
from sqlmodel import Session, select

from app.geocode import batch_geocode, chunk_dict
from app.models import Contractor, utcnow

log = logging.getLogger(__name__)

PORTAL_URL = "https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList"
DOWNLOAD_URL = "https://www.cslb.ca.gov/OnlineServices/DataPortal/DownLoadFile.ashx"
SOURCE_URL = "https://www.cslb.ca.gov/onlineservices/dataportal/"

COUNTIES = frozenset({
    "Los Angeles", "Orange", "Riverside", "San Bernardino", "Ventura", "San Diego", "Imperial",
})
# Matched against a normalized (uppercased, hyphens stripped) form of
# CSLB's own "Classifications(s)" field -- see _classification_tokens.
# The stored Contractor.classifications value is always CSLB's raw string,
# never this normalized form.
CLASSIFICATIONS = frozenset({"C20", "C38", "B"})

_VIEWSTATE_RE = re.compile(r'id="__VIEWSTATE"[^>]*value="([^"]*)"')
_VIEWSTATEGEN_RE = re.compile(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]*)"')
_EVENTVALIDATION_RE = re.compile(r'id="__EVENTVALIDATION"[^>]*value="([^"]*)"')


def _extract_form_tokens(html: str) -> dict[str, str]:
    tokens = {}
    for name, pattern in (("__VIEWSTATE", _VIEWSTATE_RE),
                          ("__VIEWSTATEGENERATOR", _VIEWSTATEGEN_RE),
                          ("__EVENTVALIDATION", _EVENTVALIDATION_RE)):
        m = pattern.search(html)
        tokens[name] = m.group(1) if m else ""
    return tokens


def _fetch_license_master_csv(client: httpx.Client) -> str:
    """Replays the portal's real postback sequence -- see module docstring.
    Raises httpx.HTTPError / RuntimeError on any unexpected response rather
    than returning a partial/empty result silently."""
    resp = client.get(PORTAL_URL)
    resp.raise_for_status()
    tokens = _extract_form_tokens(resp.text)

    resp = client.post(PORTAL_URL, data={
        "__EVENTTARGET": "ctl00$MainContent$ddlStatus",
        "__EVENTARGUMENT": "",
        "ctl00$MainContent$ddlStatus": "M",
        **tokens,
    })
    resp.raise_for_status()
    tokens = _extract_form_tokens(resp.text)
    if "MainContent_lblMasterFile" not in resp.text:
        raise RuntimeError("CSLB portal did not return the expected License Master selection state "
                          "-- the site's form structure may have changed")

    resp = client.post(PORTAL_URL, data={
        "__EVENTTARGET": "ctl00$MainContent$lbMasterCSV",
        "__EVENTARGUMENT": "",
        "ctl00$MainContent$ddlStatus": "M",
        **tokens,
    })
    if resp.status_code not in (200, 302):
        resp.raise_for_status()

    dl = client.get(DOWNLOAD_URL, params={"fName": "MasterLicenseData", "type": "C"})
    dl.raise_for_status()
    if "text/csv" not in dl.headers.get("content-type", "") and not dl.text.startswith("LicenseNo,"):
        raise RuntimeError(f"CSLB download did not return CSV (content-type={dl.headers.get('content-type')!r})")
    return dl.text


def _classification_tokens(raw: str) -> set[str]:
    return {t.replace("-", "") for t in re.split(r"[,\s]+", raw.upper()) if t}


def _in_scope(row: dict) -> bool:
    county = (row.get("County") or "").strip()
    if county not in COUNTIES:
        return False
    return bool(_classification_tokens(row.get("Classifications(s)") or "") & CLASSIFICATIONS)


def _parse_date(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y")
    except ValueError:
        return None


def _parse_amount(value: str | None) -> float | None:
    value = (value or "").strip().replace(",", "").replace("$", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _row_to_fields(row: dict) -> dict:
    return dict(
        business_name=row.get("BusinessName") or "",
        full_business_name=row.get("FullBusinessName") or None,
        business_type=row.get("BusinessType") or None,
        business_address=row.get("MailingAddress") or None,
        city=row.get("City") or None,
        county=row.get("County") or None,
        state=row.get("State") or None,
        zip_code=row.get("ZIPCode") or None,
        business_phone=row.get("BusinessPhone") or None,
        issue_date=_parse_date(row.get("IssueDate")),
        expiration_date=_parse_date(row.get("ExpirationDate")),
        primary_status=row.get("PrimaryStatus") or None,
        secondary_status=row.get("SecondaryStatus") or None,
        classifications=row.get("Classifications(s)") or None,
        workers_comp_coverage_type=row.get("WorkersCompCoverageType") or None,
        workers_comp_insurance_company=row.get("WCInsuranceCompany") or None,
        workers_comp_policy_number=row.get("WCPolicyNumber") or None,
        workers_comp_effective_date=_parse_date(row.get("WCEffectiveDate")),
        workers_comp_expiration_date=_parse_date(row.get("WCExpirationDate")),
        bond_company=row.get("CBSuretyCompany") or None,
        bond_number=row.get("CBNumber") or None,
        bond_effective_date=_parse_date(row.get("CBEffectiveDate")),
        bond_cancellation_date=_parse_date(row.get("CBCancellationDate")),
        bond_amount=_parse_amount(row.get("CBAmount")),
        last_update=_parse_date(row.get("LastUpdate")),
    )


def run_cslb_import(session: Session, *, csv_text: str | None = None,
                    geocode_batch_limit: int | None = None) -> dict:
    """Downloads (unless csv_text is given, for tests/replays), filters to
    CLASSIFICATIONS x COUNTIES, upserts Contractor rows keyed by
    license_no, then geocodes any contractor whose business_address has
    never been geocoded (or has changed since) in batches of up to
    geocode.MAX_BATCH_SIZE. geocode_batch_limit caps how many BATCHES
    (not addresses) this run spends, for bounded/test runs -- None means
    no cap.
    """
    stats = {"total_rows": 0, "in_scope": 0, "upserted": 0, "unchanged": 0,
             "geocoded": 0, "geocode_unmatched": 0, "geocode_batches": 0}

    if csv_text is None:
        with httpx.Client(timeout=60, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (dmg-scout contractor import)"}) as client:
            csv_text = _fetch_license_master_csv(client)

    existing = {c.license_no: c for c in session.exec(select(Contractor)).all()}
    to_geocode: dict[str, tuple[str, str, str, str]] = {}

    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        stats["total_rows"] += 1
        if not _in_scope(row):
            continue
        stats["in_scope"] += 1
        license_no = (row.get("LicenseNo") or "").strip()
        if not license_no:
            continue
        fields = _row_to_fields(row)

        current = existing.get(license_no)
        if current is None:
            contractor = Contractor(license_no=license_no, retrieved_at=utcnow(),
                                    source_url=SOURCE_URL, **fields)
            session.add(contractor)
            stats["upserted"] += 1
        else:
            changed = any(getattr(current, k) != v for k, v in fields.items())
            address_changed = current.business_address != fields["business_address"]
            for k, v in fields.items():
                setattr(current, k, v)
            if changed:
                current.retrieved_at = utcnow()
                session.add(current)
                stats["upserted"] += 1
            else:
                stats["unchanged"] += 1
            if address_changed:
                current.latitude = None
                current.longitude = None
                current.geocoded_at = None
            contractor = current

        needs_geocode = (contractor.latitude is None and fields.get("business_address")
                         and fields.get("city") and fields.get("state"))
        if needs_geocode:
            to_geocode[license_no] = (
                fields["business_address"], fields["city"], fields["state"] or "CA",
                fields.get("zip_code") or "",
            )

    session.commit()

    for i, chunk in enumerate(chunk_dict(to_geocode, 10_000)):
        if geocode_batch_limit is not None and i >= geocode_batch_limit:
            break
        stats["geocode_batches"] += 1
        try:
            results = batch_geocode(chunk)
        except httpx.HTTPError as exc:
            log.error("CSLB contractor geocoding batch failed: %s", exc)
            continue
        for license_no, coords in results.items():
            contractor = existing.get(license_no) or session.exec(
                select(Contractor).where(Contractor.license_no == license_no)).first()
            if contractor is None:
                continue
            if coords is None:
                stats["geocode_unmatched"] += 1
                continue
            contractor.latitude, contractor.longitude = coords
            contractor.geocoded_at = utcnow()
            contractor.geocode_source = "us_census_bureau"
            session.add(contractor)
            stats["geocoded"] += 1
        session.commit()

    return stats
