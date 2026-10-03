from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from committee.engines.scenario.cli import app
from committee.engines.scenario.holdings_io import parse_themes, read_holdings_csv

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "engines"
runner = CliRunner()


def test_run_demo_portfolio() -> None:
    r = runner.invoke(app, ["run", "--root", str(ROOT)])
    assert r.exit_code == 0, r.output
    assert "demo portfolio" in r.output
    assert "Taiwan supply freeze" in r.output
    assert "risk-budget modifier" in r.output


def test_run_json_with_holdings_csv() -> None:
    r = runner.invoke(
        app,
        [
            "run",
            "--root",
            str(ROOT),
            "--holdings",
            str(FIX / "holdings.csv"),
            "--json",
            "--total-value",
            "1000000",
        ],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert len(data["losses"]) == 8
    assert data["total_value"] == 1_000_000
    assert 0.5 <= data["modifier"] <= 1.0


def test_run_table_with_holdings_csv() -> None:
    r = runner.invoke(app, ["run", "--root", str(ROOT), "--holdings", str(FIX / "holdings.csv")])
    assert r.exit_code == 0, r.output
    assert str(FIX / "holdings.csv") in r.output


def test_run_errors(tmp_path: Path) -> None:
    r = runner.invoke(
        app, ["run", "--root", str(ROOT), "--holdings", str(FIX / "holdings_bad.csv")]
    )
    assert r.exit_code == 2
    r = runner.invoke(app, ["run", "--root", str(ROOT), "--holdings", str(tmp_path / "none.csv")])
    assert r.exit_code == 2
    r = runner.invoke(
        app,
        [
            "run",
            "--root",
            str(ROOT),
            "--holdings",
            str(FIX / "holdings.csv"),
            "--total-value",
            "10",
        ],
    )
    assert r.exit_code == 2
    (tmp_path / "config").mkdir()
    r = runner.invoke(app, ["run", "--root", str(tmp_path)])
    assert r.exit_code == 2


def test_run_breach_output(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    cfg = tmp_path / "config" / "scenarios.yaml"
    cfg.write_text(cfg.read_text().replace("loss_vol_multiple: 2.0", "loss_vol_multiple: 0.5"))
    r = runner.invoke(app, ["run", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "BREACH" in r.output
    assert "cuts the budget" in r.output


def test_read_holdings_csv() -> None:
    hs = read_holdings_csv(FIX / "holdings.csv")
    semi = next(h for h in hs if h.symbol == "SEMI")
    assert semi.themes == {"ai_capex_chain": 0.8, "taiwan_supply_chain": 0.5}
    assert semi.opened_on is not None and semi.annual_vol == 0.45
    vti = next(h for h in hs if h.symbol == "VTI")
    assert vti.bucket is None and vti.themes == {}


def test_parse_themes() -> None:
    assert parse_themes("") == {}
    assert parse_themes("a:0.5; b:1") == {"a": 0.5, "b": 1.0}
    with pytest.raises(ValueError):
        parse_themes("a")
