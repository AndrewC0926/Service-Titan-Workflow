"""CAEATFA (California Alternative Energy and Advanced Transportation Financing
Authority) Sales and Use Tax Exclusion (STE) Program -- board-approved
manufacturers building or expanding a California facility, with a company
name, a location, a dollar figure, and a board approval date on every row.

Compliance, checked before writing any of this (2026-08-20): fetched
treasurer.ca.gov/robots.txt directly (200 OK, standard Drupal boilerplate) --
it disallows /core/, /profiles/, /admin/, /search/, /user/*, /comment/reply/,
/node/add/, /media/oembed, and a short list of named README/config files.
It does NOT disallow /caeatfa/ or /sites/default/files/, where this source
lives, and PoliteClient enforces that at request time regardless. ca.gov's
site-wide Conditions of Use (ca.gov/legal/conditions-of-use/) impose no
restriction on automated or bulk access -- the only "unauthorized use"
language concerns defeating security controls, and public information "may
be distributed or copied as permitted by law." CAEATFA's own STE program
page (treasurer.ca.gov/caeatfa/ste/index.asp) publishes AWARDS_URL below as
a self-service download for the public, titled "Sales and Use Tax Exclusion
Program Awards" -- the sanctioned intended use, same standing as CSLB's bulk
download (see app/pipeline/cslb.py's module docstring), not a scrape of a
search UI meant for one-record-at-a-time lookup.

The workbook has many sheets; only "Apps Approved" is read here -- every
application CAEATFA's board has approved AND that has a signed regulatory
agreement. Its columns (App No., Date Approved, Applicant Name, City, Primary
County, County, Project Type, Use of Proceeds, Qualified Property Amount
Approved, % Reported -- verified against a live download 2026-08-20) are
transcribed into raw_text verbatim; nothing here infers a figure or location
the row doesn't state. Two other sheets exist and are deliberately not read:
"Apps Denied" (a denial is not a project signal) and "Apps up for
Consideration" (no board date yet, so no trigger -- see below).

Scope is `territory.CA` from config.yaml, matched against the row's own
"Primary County" column only. Some approvals cover multiple sites across
several counties (e.g. an aerospace prime with plants in five cities) --
Primary County is CAEATFA's own single-county designation for the award, not
a Scout inference, so that is what territory matching uses; every county the
row actually names is still passed through verbatim in raw_text; do not
resolve or filter further as by-county totals.

Trigger and window: `Date Approved` is the board date and becomes
`published_at` / (via the standard extract stage) `Signal.event_date`.
CAEATFA's own program rules (treasurer.ca.gov/caeatfa/ste/faq) require every
Qualified Property purchase to complete within five years of approval -- that
is the program's own definition of the buildout window, not a guessed one,
and app.assumptions cites it directly rather than inventing a lookback.

No SignalType of its own: a CAEATFA STE award and a Nevada GOED abatement are
the same thing described by two different states' statutes -- a public
financing authority approving a named manufacturer's committed capital ahead
of construction -- so this reuses SignalType.abatement_application rather
than adding a schema-migrating variant for a distinction without a
difference.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Iterator

import openpyxl

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.base import FetchedDoc, SourceAdapter, SourceFailure

log = logging.getLogger(__name__)

SOURCE_PAGE = "https://www.treasurer.ca.gov/caeatfa/ste/index.asp"
AWARDS_URL = "https://www.treasurer.ca.gov/sites/default/files/caeatfa/salesandtax.xlsx"
SHEET = "Apps Approved"
HEADER_ROW = 2
FIRST_DATA_ROW = 3
# 1-indexed column positions, fixed against a live download 2026-08-20 --
# verify against SHEET's header row (row 2) before trusting these blindly if
# the workbook's layout ever changes; there is no column-name lookup here on
# purpose, same "confirm against the live service" discipline as every other
# adapter's hand-verified identifiers (see README.md's Source verification
# section).
COL_APP_NO = 2
COL_DATE_APPROVED = 4
COL_APPLICANT = 5
COL_CITY = 6
COL_PRIMARY_COUNTY = 7
COL_COUNTY = 8
COL_PROJECT_TYPE = 9
COL_USE_OF_PROCEEDS = 10
COL_QP_AMOUNT_APPROVED = 11
COL_PCT_REPORTED = 15


def _money(v) -> str:
    if not isinstance(v, (int, float)):
        return "not stated"
    return f"${v:,.2f}"


def _pct(v) -> str:
    if not isinstance(v, (int, float)):
        return "not stated"
    return f"{v * 100:.1f}%"


class CaeatfaAdapter(SourceAdapter):
    name = "caeatfa"
    default_min_doc_chars = 250

    def fetch(self, cfg: Config, client: PoliteClient,
              since: datetime | None = None) -> Iterator[FetchedDoc]:
        src = cfg.source(self.name)
        url = src.get("awards_url", AWARDS_URL)
        territory = set(cfg.get("territory.CA", []) or [])
        if not territory:
            raise SourceFailure("caeatfa: no territory.CA counties configured")

        try:
            raw = client.get_bytes(url)
        except Exception as exc:  # noqa: BLE001
            raise SourceFailure(f"CAEATFA awards workbook unreachable: {exc}") from exc

        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
            ws = wb[src.get("sheet", SHEET)]
        except Exception as exc:  # noqa: BLE001
            raise SourceFailure(f"CAEATFA awards workbook unreadable: {exc}") from exc

        n_total = n_territory = 0
        for row in ws.iter_rows(min_row=FIRST_DATA_ROW):
            applicant = row[COL_APPLICANT - 1].value
            if not applicant:
                continue
            n_total += 1
            primary_county = row[COL_PRIMARY_COUNTY - 1].value
            if primary_county not in territory:
                continue
            date_approved = row[COL_DATE_APPROVED - 1].value
            if not isinstance(date_approved, datetime):
                continue  # a row without a real board date has nothing to trigger on
            if since is not None and date_approved < since:
                continue
            n_territory += 1

            app_no = row[COL_APP_NO - 1].value
            city = row[COL_CITY - 1].value
            county_field = row[COL_COUNTY - 1].value
            project_type = row[COL_PROJECT_TYPE - 1].value
            use_of_proceeds = row[COL_USE_OF_PROCEEDS - 1].value
            qp_amount = row[COL_QP_AMOUNT_APPROVED - 1].value
            pct_reported = row[COL_PCT_REPORTED - 1].value

            raw_text = (
                "CAEATFA Sales and Use Tax Exclusion (STE) Program -- board-approved award, "
                "signed regulatory agreement.\n"
                f"Applicant: {applicant}\n"
                f"City: {city or 'not stated'}\n"
                f"Primary County: {primary_county}\n"
                f"County field as published (may list more than one site): {county_field or 'not stated'}\n"
                f"Project Type: {project_type or 'not stated'}\n"
                f"Use of Proceeds: {use_of_proceeds or 'not stated'}\n"
                f"Qualified Property Amount Approved: {_money(qp_amount)}\n"
                f"Date Approved by CAEATFA Board: {date_approved:%Y-%m-%d}\n"
                f"Qualified Property Reported Purchased to Date: {_pct(pct_reported)} of the approved amount "
                "(CAEATFA's own program rules require all Qualified Property purchases to complete within "
                "five years of this approval date).\n"
                f"CAEATFA application number: {app_no or 'not stated'}\n"
                "Source: CAEATFA Sales and Use Tax Exclusion Program Awards, State of California Office of "
                f"the State Treasurer ({url})."
            )
            yield FetchedDoc(
                source=self.name,
                source_uid=str(app_no) if app_no else f"{applicant}:{date_approved:%Y-%m-%d}",
                url=SOURCE_PAGE,
                title=f"CAEATFA STE approval: {applicant} ({city or primary_county})",
                raw_text=raw_text,
                published_at=date_approved,
                meta={"kind": "ste_award", "primary_county": primary_county},
                default_signal_type=SignalType.abatement_application,
            )
        log.info("caeatfa: %d approved+signed rows scanned, %d in territory.CA", n_total, n_territory)
