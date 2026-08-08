"""HCAI county-level aggregate import (Phase 6g). See app/pipeline/hcai.py."""
import pytest

from app.models import HcaiCountyActivity
from app.pipeline.hcai import import_hcai_snapshot

HEADER = "County,Data Generation Date,OSHPD Project Status,Total Costs of OSHPD Projects,Number of OSHPD Projects,Collection of Counties\n"


def _csv(*rows: str) -> str:
    return HEADER + "\n".join(rows)


def test_keeps_only_the_most_recent_snapshot(db_session):
    csv_text = _csv(
        '01 - Alameda,10/14/2013,In Review,"$50,890,315.00 ",44,Bay Area Counties',
        '01 - Alameda,7/23/2026,In Review,"$504,283,304.38 ",34,Bay Area Counties',
        '01 - Alameda,7/23/2026,In Construction,"$236,187,916.21 ",98,Bay Area Counties',
    )
    stats = import_hcai_snapshot(db_session, csv_text, source_url="https://x", is_path=False)
    assert stats["snapshot_date"] == "2026-07-23"
    assert stats["rows_stored"] == 2

    from sqlmodel import select
    rows = db_session.exec(select(HcaiCountyActivity)).all()
    assert len(rows) == 2
    assert all(r.county == "Alameda" for r in rows)


def test_county_prefix_stripped(db_session):
    csv_text = _csv('19 - Los Angeles,1/1/2026,In Review,"$1,000.00 ",1,Southern California Counties')
    import_hcai_snapshot(db_session, csv_text, source_url="https://x", is_path=False)
    from sqlmodel import select
    row = db_session.exec(select(HcaiCountyActivity)).one()
    assert row.county == "Los Angeles"
    assert row.total_cost == 1000.0
    assert row.project_count == 1


def test_reimport_replaces_not_appends(db_session):
    csv_text = _csv('01 - Alameda,1/1/2026,In Review,"$1,000.00 ",1,Bay Area Counties')
    import_hcai_snapshot(db_session, csv_text, source_url="https://x", is_path=False)
    csv_text2 = _csv('01 - Alameda,2/1/2026,In Review,"$2,000.00 ",2,Bay Area Counties')
    import_hcai_snapshot(db_session, csv_text2, source_url="https://x", is_path=False)

    from sqlmodel import select
    rows = db_session.exec(select(HcaiCountyActivity)).all()
    assert len(rows) == 1
    assert rows[0].total_cost == 2000.0
    assert rows[0].snapshot_date.isoformat().startswith("2026-02-01")


def test_zero_dollar_and_zero_count_parsed_correctly(db_session):
    """A real row for a tiny county: '0.00 ' and '0', not blank."""
    csv_text = _csv('02 - Alpine,1/1/2026,In Review,0.00 ,0,')
    import_hcai_snapshot(db_session, csv_text, source_url="https://x", is_path=False)
    from sqlmodel import select
    row = db_session.exec(select(HcaiCountyActivity)).one()
    assert row.total_cost == 0.0
    assert row.project_count == 0


def test_no_parseable_dates_raises(db_session):
    csv_text = HEADER + "01 - Alameda,not-a-date,In Review,$0,0,\n"
    with pytest.raises(ValueError, match="no parseable"):
        import_hcai_snapshot(db_session, csv_text, source_url="https://x", is_path=False)


def test_empty_csv_raises(db_session):
    with pytest.raises(ValueError, match="no data rows"):
        import_hcai_snapshot(db_session, HEADER, source_url="https://x", is_path=False)
