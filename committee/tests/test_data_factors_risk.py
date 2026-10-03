from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

import pytest

from committee.data.factors import DATASETS, ingest_factors, parse_french_csv
from committee.data.http import FixtureFetcher
from committee.data.lake import Lake, RawZone
from committee.data.pit import PIT
from committee.data.risk_indexes import (
    URLS,
    decode_csv,
    ingest_risk_indexes,
    parse_epu,
    parse_gpr_daily,
    parse_gpr_monthly,
)

D = Path(__file__).parent / "fixtures" / "data"
NOW = dt.datetime(2024, 9, 20, 12, tzinfo=dt.UTC)


def zipped(path: Path, text: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(path.name, text if text is not None else path.read_text())
    return buf.getvalue()


def test_parse_french_daily_and_monthly() -> None:
    daily = parse_french_csv((D / "french/F-F_Research_Data_5_Factors_2x3_daily.CSV").read_text())
    assert (dt.date(2024, 8, 26), "mkt_rf", -0.0036) in daily
    assert (dt.date(2024, 8, 26), "rf", 0.00022) in daily
    assert not any(d == dt.date(2024, 8, 29) and f == "cma" for d, f, _ in daily)  # -99.99
    assert len(daily) == 4 * 6 - 1
    monthly = parse_french_csv((D / "french/F-F_Research_Data_5_Factors_2x3.csv").read_text())
    assert {d for d, _, _ in monthly} == {dt.date(2024, 6, 30), dt.date(2024, 7, 31)}  # no annual
    assert (dt.date(2024, 7, 31), "smb", 0.0683) in monthly
    mom = parse_french_csv((D / "french/F-F_Momentum_Factor_daily.CSV").read_text())
    assert mom[0] == (dt.date(2024, 8, 26), "mom", -0.0111)


def test_ingest_factors_known_at_download_and_revisions(tmp_path: Path) -> None:
    lake, raw = Lake(tmp_path / "lake"), RawZone(tmp_path / "raw")
    ds = {"ff5_daily": DATASETS["ff5_daily"], "mom_daily": DATASETS["mom_daily"]}
    f5 = D / "french/F-F_Research_Data_5_Factors_2x3_daily.CSV"
    mom = D / "french/F-F_Momentum_Factor_daily.CSV"
    ff = FixtureFetcher({DATASETS["ff5_daily"]: zipped(f5), DATASETS["mom_daily"]: zipped(mom)})
    s = ingest_factors(lake, raw, ff, datasets=ds, now=NOW)
    assert s["counts"] == {"ff5_daily": 23, "mom_daily": 4}
    pit = PIT(lake)
    assert pit.latest("factor_returns", NOW - dt.timedelta(seconds=1)).empty
    assert len(pit.latest("factor_returns", NOW)) == 27
    pit.close()
    revised = mom.read_text().replace("20240827,   0.52", "20240827,   0.55")
    later = NOW + dt.timedelta(days=30)
    ff2 = FixtureFetcher(
        {DATASETS["ff5_daily"]: zipped(f5), DATASETS["mom_daily"]: zipped(mom, revised)}
    )
    s2 = ingest_factors(lake, raw, ff2, datasets=ds, now=later)
    assert s2["rows_written"] == {"factor_returns": 1}
    pit = PIT(lake)
    q = "factor = 'mom' AND date = DATE '2024-08-27'"
    assert pit.latest("factor_returns", NOW, where=q)["value"].tolist() == [0.0052]
    assert pit.latest("factor_returns", later, where=q)["value"].tolist() == [0.0055]
    pit.close()


def test_parse_risk_indexes() -> None:
    gpr = parse_gpr_monthly((D / "risk/data_gpr_export.csv").read_text())
    assert gpr[0] == (dt.date(2024, 6, 30), 110.53) and len(gpr) == 3
    daily = parse_gpr_daily((D / "risk/data_gpr_daily_recent.csv").read_text())
    assert daily == [(dt.date(2024, 8, 26), 98.72), (dt.date(2024, 8, 27), 140.10)]
    epu = parse_epu((D / "risk/US_Policy_Uncertainty_Data.csv").read_text())
    assert epu[-1] == (dt.date(2024, 8, 31), 142.70) and len(epu) == 3  # footer skipped
    with pytest.raises(ValueError, match="Excel engine"):
        decode_csv(b"\xd0\xcf\x11\xe0" + b"\0" * 100)


def test_ingest_risk_indexes_release_date_and_revision(tmp_path: Path) -> None:
    lake, raw = Lake(tmp_path / "lake"), RawZone(tmp_path / "raw")
    gpr_csv = D / "risk/data_gpr_export.csv"
    routes: dict[str, Path | bytes] = {
        URLS["gpr"]: gpr_csv,
        URLS["gpr_daily"]: D / "risk/data_gpr_daily_recent.csv",
        URLS["epu"]: D / "risk/US_Policy_Uncertainty_Data.csv",
    }
    s = ingest_risk_indexes(lake, raw, FixtureFetcher(routes), now=NOW)
    assert s["counts"] == {"gpr": 3, "gpr_daily": 2, "epu": 3}
    pit = PIT(lake)
    aug = pit.latest("risk_indexes", NOW, where="index_id = 'gpr' AND obs_date = DATE '2024-08-31'")
    # release = month end + 10 days
    assert aug["known_time"][0].to_pydatetime() == dt.datetime(
        2024, 9, 10, 23, 59, 59, tzinfo=dt.UTC
    )
    epu_aug = pit.latest(
        "risk_indexes", NOW, where="index_id = 'epu' AND obs_date = DATE '2024-08-31'"
    )
    assert epu_aug["known_time"][0].to_pydatetime() == NOW  # 30-day lag capped at download time
    pit.close()
    later = NOW + dt.timedelta(days=5)
    routes[URLS["gpr"]] = gpr_csv.read_bytes().replace(b"121.70", b"125.00")
    s2 = ingest_risk_indexes(lake, raw, FixtureFetcher(routes), now=later)
    assert s2["rows_written"] == {"risk_indexes": 1}
    pit = PIT(lake)
    q = "index_id = 'gpr' AND obs_date = DATE '2024-08-31'"
    assert pit.latest("risk_indexes", NOW, where=q)["value"].tolist() == [121.70]
    assert pit.latest("risk_indexes", later, where=q)["value"].tolist() == [125.00]
    pit.close()


def test_decode_xlsx_spreadsheet() -> None:
    import io

    import openpyxl

    from committee.data.risk_indexes import decode_csv

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["month", "GPR"])
    ws.append(["2026-08-01", 101.5])
    buf = io.BytesIO()
    wb.save(buf)
    text = decode_csv(buf.getvalue())
    assert "GPR" in text and "101.5" in text
