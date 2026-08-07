"""CEC IEPR forward-MW layer: import, county derivation, re-import replacement,
and that the churn discount stays a config value rather than baked into the
import. See app/pipeline/iepr.py."""
import openpyxl
import pytest

from app.models import IeprForwardLoad
from app.pipeline.iepr import county_forward_mw, import_iepr_workbook

HEADERS = ["Demand Forecasting Key", "Source", "Status", "CEC Grouping",
           "Project ID or Name", "Address", "WECC BUS ID", "Circuit", "Substation",
           "System", "Region", "Voltage", "City", "Requested Energization Year",
           "Requested Peak MW ", "MOS if applicable"]


def _make_workbook(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IEPR25 Request"
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def _row(status="Active Inqury", grouping=3, region="Orange", voltage=12,
        city="Irvine", year=2027, mw=5.0):
    return [None, None, status, grouping, None, None, None, None, None,
            None, region, voltage, city, year, mw, None]


@pytest.fixture()
def iepr_xlsx(tmp_path):
    return _make_workbook(tmp_path / "iepr.xlsx", [
        _row(city="Irvine", mw=5.0),                          # Orange
        _row(city="Vernon", mw=100.0),                         # Los Angeles
        _row(city="Redondo", mw=10.0),                         # alias -> Redondo Beach -> Los Angeles
        _row(city="Monterrey Park", mw=3.0),                   # alias -> Monterey Park -> Los Angeles
        _row(status="Canceled", city="Irvine", mw=999.0),       # excluded from totals
        _row(city="Open", mw=50.0),                            # placeholder, no county
        _row(city=None, mw=20.0),                              # no city at all
    ])


def test_import_derives_county_and_stores_named_utility_free_rows(db_session, iepr_xlsx):
    stats = import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                                 source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    assert stats["rows_in_sheet"] == 7
    assert stats["rows_stored"] == 7
    assert stats["unmapped_cities"] == {"Open": 1}
    rows = db_session.exec(
        __import__("sqlmodel").select(IeprForwardLoad)).all()
    vernon = next(r for r in rows if r.city == "Vernon")
    assert vernon.county == "Los Angeles"
    assert vernon.source_url.startswith("https://efiling.energy.ca.gov/")
    # The layer is redacted at the source and must carry no lead-identifying
    # field — the model itself has none, but assert the aliasing didn't leak
    # anything project-shaped in either direction.
    redondo = next(r for r in rows if r.city == "Redondo")
    assert redondo.county == "Los Angeles"
    monterrey = next(r for r in rows if r.city == "Monterrey Park")
    assert monterrey.county == "Los Angeles"
    no_city = next(r for r in rows if r.city is None)
    assert no_city.county is None


def test_reimport_same_docket_replaces_not_appends(db_session, iepr_xlsx):
    import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    rows = db_session.exec(__import__("sqlmodel").select(IeprForwardLoad)).all()
    assert len(rows) == 7, "re-running the same docket_tn must replace, not double, the rows"


def test_different_docket_tn_does_not_clobber_prior_utility_snapshot(db_session, iepr_xlsx):
    import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    import_iepr_workbook(db_session, iepr_xlsx, utility="PGE", docket_tn="999999",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=999999")
    rows = db_session.exec(__import__("sqlmodel").select(IeprForwardLoad)).all()
    assert len(rows) == 14
    assert {r.utility for r in rows} == {"SCE", "PGE"}


def test_county_forward_mw_excludes_canceled_and_unmapped_cities(db_session, iepr_xlsx):
    import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    totals = county_forward_mw(db_session)
    # Irvine: 5.0 kept, the 999.0 Canceled Irvine row excluded.
    assert totals["Orange"] == 5.0
    # Vernon 100 + Redondo(alias) 10 + Monterrey Park(alias) 3 = 113, all Los Angeles.
    assert totals["Los Angeles"] == 113.0
    # "Open" and the no-city row never resolved a county, so they contribute
    # to no county total at all rather than being silently misattributed.
    assert sum(totals.values()) == 118.0


def test_missing_expected_column_raises_rather_than_silently_misaligning(db_session, tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Status", "City"])  # missing Region, Voltage, Requested Peak MW, etc.
    ws.append(["Active Inqury", "Irvine"])
    path = tmp_path / "broken.xlsx"
    wb.save(path)
    with pytest.raises(ValueError, match="missing expected column"):
        import_iepr_workbook(db_session, path, utility="SCE", docket_tn="1",
                             source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=1")


def test_churn_discount_is_config_not_baked_into_import(cfg, db_session, iepr_xlsx):
    """The ~44% churn is a scoring-time discount applied by Phase 4, never
    baked into county_forward_mw's return value — see the module docstring."""
    import_iepr_workbook(db_session, iepr_xlsx, utility="SCE", docket_tn="266008",
                         source_url="https://efiling.energy.ca.gov/GetDocument.aspx?tn=266008")
    totals = county_forward_mw(db_session)
    assert totals["Orange"] == 5.0, "raw filed MW, undiscounted — discount is applied by the caller"
    assert cfg.get("iepr.churn_discount") == 0.44
