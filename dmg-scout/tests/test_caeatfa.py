"""CAEATFA STE adapter: territory filtering, literal (non-inferred) raw_text,
and the board date as the trigger. See app/sources/caeatfa.py's docstring for
the compliance check this source relies on."""
import io
from datetime import datetime

import httpx
import openpyxl
import respx

from app.config import Config
from app.http import PoliteClient
from app.models import SignalType
from app.sources.caeatfa import AWARDS_URL, CaeatfaAdapter


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _workbook(rows: list[tuple]) -> bytes:
    """Build a minimal 'Apps Approved' sheet matching the real column layout
    (verified 2026-08-20 against a live download) -- see caeatfa.py's
    COL_* constants."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Apps Approved"
    ws.append(["Applications Previously Approved w/ Signed Agreement"])
    ws.append([" ", "App. No.", "Year Approved", "Date Approved", "Applicant Name", "City",
               "Primary County", "County", "Project Type", "Use of Proceeds",
               "Qualified Property Amount Approved", "Estimated STE", "Estimated STE Used to Date",
               "Qualified Property Amount Reported", "% Reported"])
    for i, (app_no, date, applicant, city, county, ptype, use, amount, pct) in enumerate(rows, start=1):
        ws.append([i, app_no, date.year if date else None, date, applicant, city, county, county,
                   ptype, use, amount, None, None, None, pct])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cfg(base: Config) -> Config:
    data = dict(base.data)
    data = {**data, "sources": {**data["sources"], "caeatfa": {"enabled": True}}}
    return Config(data)


@respx.mock
def test_caeatfa_filters_to_territory_and_carries_board_date(cfg):
    xlsx = _workbook([
        ("24-SM001", datetime(2024, 7, 16), "Territory Manufacturer, Inc.", "Bakersfield",
         "Kern", "Advanced Manufacturing", "Widget Manufacturing", 10_000_000, 0.0),
        ("24-SM002", datetime(2024, 3, 1), "Out Of Territory LLC", "Fremont",
         "Alameda", "Advanced Manufacturing", "Battery Manufacturing", 5_000_000, 0.2),
    ])
    respx.get(AWARDS_URL).mock(return_value=httpx.Response(200, content=xlsx))

    with fast_client() as client:
        docs = list(CaeatfaAdapter().fetch(_cfg(cfg), client))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source == "caeatfa"
    assert doc.source_uid == "24-SM001"
    assert doc.default_signal_type == SignalType.abatement_application
    assert doc.published_at == datetime(2024, 7, 16)
    # Every field in raw_text is a literal transcription of the row -- nothing invented.
    assert "Territory Manufacturer, Inc." in doc.raw_text
    assert "Kern" in doc.raw_text
    assert "$10,000,000.00" in doc.raw_text
    assert "2024-07-16" in doc.raw_text
    assert "Out Of Territory" not in "".join(d.raw_text for d in docs)


@respx.mock
def test_caeatfa_drops_rows_with_no_board_date(cfg):
    xlsx = _workbook([
        ("24-SM003", None, "No Board Date Yet LLC", "Ontario",
         "San Bernardino", "Advanced Manufacturing", "Testing", 1_000_000, None),
    ])
    respx.get(AWARDS_URL).mock(return_value=httpx.Response(200, content=xlsx))

    with fast_client() as client:
        docs = list(CaeatfaAdapter().fetch(_cfg(cfg), client))

    assert docs == []


@respx.mock
def test_caeatfa_does_not_invent_a_dollar_figure_when_none_is_stated(cfg):
    xlsx = _workbook([
        ("24-SM004", datetime(2024, 1, 1), "Amount Not Stated Inc.", "Irvine",
         "Orange", "Advanced Manufacturing", "Testing", None, None),
    ])
    respx.get(AWARDS_URL).mock(return_value=httpx.Response(200, content=xlsx))

    with fast_client() as client:
        docs = list(CaeatfaAdapter().fetch(_cfg(cfg), client))

    assert len(docs) == 1
    assert "Qualified Property Amount Approved: not stated" in docs[0].raw_text
