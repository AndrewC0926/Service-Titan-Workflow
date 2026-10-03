from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from committee.config.secrets import Secrets
from committee.data.cli import _guard
from committee.data.cli import app as data_cli
from committee.data.http import FetchError
from committee.data.ingest import lake_for
from committee.data.lake import Lake
from committee.data.quality import DQReport, run_quality
from committee.journal.store import Journal

T = dt.datetime(2024, 1, 2, 21, 30, tzinfo=dt.UTC)  # Tuesday
OLD_INGEST = "20240102T220000-aaaaaaaa"


def bar(sid: str, d: dt.date, close: float, low: float | None = None) -> dict[str, Any]:
    kt = dt.datetime.combine(d, dt.time(21, 30), tzinfo=dt.UTC)
    return {
        "security_id": sid, "date": d, "open": close, "high": close, "low": close if low is None else low,
        "close": close, "adj_close": close, "volume": 1.0, "event_time": kt, "known_time": kt,
    }  # fmt: skip


def check(rep: DQReport, name: str) -> Any:
    return next(c for c in rep.checks if c.name == name)


def test_freshness_fails_after_one_business_day(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.write("prices_daily", [bar("S1", T.date(), 10.0)], source="prices", ingest_id=OLD_INGEST)
    assert check(run_quality(lake, asof="2024-01-03"), "freshness").status == "pass"
    rep = run_quality(lake, asof="2024-01-05")  # Friday: 3 business days later
    fr = check(rep, "freshness")
    assert fr.status == "fail" and not rep.ok
    assert any(i.startswith("prices_daily/prices: last ingest") for i in fr.items)
    assert any("latest bar 2024-01-02" in i for i in fr.items)
    stat = rep.tables[0]
    assert (stat.table, stat.rows, stat.stale_bdays) == ("prices_daily", 1, 3)


def test_journaled_run_without_new_rows_keeps_source_fresh(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    lake.write(
        "macro_series",
        [{"series_id": "DFF", "obs_date": T.date(), "value": 5.33, "vintage_date": T.date(),
          "event_time": T, "known_time": T}],
        source="fred", ingest_id=OLD_INGEST,
    )  # fmt: skip
    run_at = dt.datetime(2024, 1, 9, 23, tzinfo=dt.UTC)  # a later run that found nothing new
    with Journal(tmp_path / "j.sqlite", clock=lambda: run_at) as j:
        j.append("ingest_summary", {"source": "fred", "status": "ok"})
        assert run_quality(lake, asof="2024-01-10", journal=j).ok
        assert not run_quality(lake, asof="2024-01-12", journal=j).ok
        reports = list(j.entries("dq_report"))
    assert len(reports) == 2 and reports[1].payload["failures"] == ["freshness"]


def test_price_sanity_flags_bad_prices_and_unmatched_moves(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    d = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(4)]
    rows = [
        bar("S1", d[0], 100.0), bar("S1", d[1], 25.0), bar("S1", d[2], 25.5),  # split-matched
        bar("S2", d[0], 50.0), bar("S2", d[1], 80.0),  # +60%, no action -> review
        bar("S3", d[0], 10.0, low=0.0),  # zero low
    ]  # fmt: skip
    lake.write("prices_daily", rows, source="prices", ingest_id=OLD_INGEST)
    lake.write(
        "corporate_actions",
        [{"security_id": "S1", "ex_date": d[1], "kind": "split", "ratio": 4.0, "amount": None,
          "event_time": T, "known_time": T}],
        source="prices", ingest_id=OLD_INGEST,
    )  # fmt: skip
    seen: list[DQReport] = []
    rep = run_quality(lake, asof="2024-01-05", notify=seen.append)
    ps = check(rep, "price_sanity")
    assert ps.status == "fail"
    assert len(ps.items) == 2
    assert any(i.startswith("S2 2024-01-03: move +60.0%") for i in ps.items)
    assert any(i.startswith("S3 2024-01-02: non-positive") for i in ps.items)
    assert seen == [rep]  # email hook called on failure


def _filing(acc: str, form: str, n_txns: int | None) -> dict[str, Any]:
    return {
        "accession": acc, "cik": 1, "security_id": "CIK0000000001", "form": form,
        "period": None, "accepted_at": T, "url": "u", "items": None, "sections_hash": None,
        "n_txns": n_txns, "event_time": T, "known_time": T,
    }  # fmt: skip


def test_form4_completeness(tmp_path: Path) -> None:
    import pandas as pd

    lake = Lake(tmp_path)
    df = pd.DataFrame(
        [
            _filing("A-parsed", "4", 1),
            _filing("A-holdings", "4", 0),
            _filing("A-dead", "4/A", None),
            _filing("A-missing", "4", None),
            _filing("A-8k", "8-K", None),
        ]
    )
    df["n_txns"] = df["n_txns"].astype("Int64")
    lake.write("filings", df, source="sec_edgar", ingest_id=OLD_INGEST)
    lake.write(
        "insider_txns",
        [{"accession": "A-parsed", "line": 0, "event_time": T, "known_time": T}],
        source="sec_edgar", ingest_id=OLD_INGEST,
    )  # fmt: skip
    lake.write(
        "dead_letter",
        [{"entity": "form4", "ref": "A-dead", "error": "x", "event_time": T, "known_time": T}],
        source="sec_edgar", ingest_id=OLD_INGEST,
    )  # fmt: skip
    c = check(run_quality(lake, asof="2024-01-03"), "form4_completeness")
    assert c.status == "fail" and c.items == ["A-missing"]


def test_data_check_cli_exit_codes(ctx, project: Path) -> None:  # type: ignore[no-untyped-def]
    runner = CliRunner()
    empty = runner.invoke(data_cli, ["data", "check", "--root", str(project)])
    assert empty.exit_code == 0 and "lake is empty" in empty.output
    lake_for(ctx).write(
        "prices_daily", [bar("S1", T.date(), 0.0)], source="prices", ingest_id=OLD_INGEST
    )
    bad = runner.invoke(data_cli, ["data", "check", "--root", str(project), "--asof", "2024-01-03"])
    assert bad.exit_code == 1
    assert "[FAIL] price_sanity" in bad.output and "Traceback" not in bad.output


def test_cli_missing_secret_and_network_errors_are_clean(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(Secrets, "get", lambda self, name: None)  # nothing in keychain/.env
    out = CliRunner().invoke(
        data_cli, ["ingest", "edgar", "--tickers", "AAPL", "--root", str(project)]
    )
    assert out.exit_code == 2, out.output
    assert "SEC_USER_AGENT is not set" in out.output and "Traceback" not in out.output
    with pytest.raises(typer.Exit) as ei, _guard():
        raise FetchError("https://data.sec.gov/x", None, "ConnectError")
    assert ei.value.exit_code == 2
    assert "network unreachable" in capsys.readouterr().err
