from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from committee.cli import app
from committee.commands import command_exists
from committee.journal.store import Journal
from committee.ops.scheduler import JOBS

R = CliRunner()


@pytest.mark.parametrize("job", JOBS, ids=lambda j: j.id)
def test_every_scheduled_job_is_a_real_command(job) -> None:  # type: ignore[no-untyped-def]
    assert command_exists(app, job.command), job.command


def test_command_exists_rejects_unknown() -> None:
    assert not command_exists(app, "nope")
    assert not command_exists(app, "review")  # a group, not a command


def run(project: Path, *args: str, input: str | None = None) -> str:
    res = R.invoke(app, [*args, "--root", str(project)], input=input)
    assert res.exit_code == 0, res.output
    return res.output


def test_decision_flow_through_cli(project: Path) -> None:
    j = Journal(project / "var" / "journal.sqlite")
    b = j.append(
        "briefing",
        {
            "symbol": "ABC",
            "recommendation": "BUY",
            "cooling_off_hours": 0,
            "legs": [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 1.5}],
        },
    )
    j.close()
    assert "ABC" in run(project, "briefings", "list")
    out = run(
        project,
        "approve",
        b.hash,
        "--reason",
        "Cheap and insiders are buying.",
        "--pct",
        "1.0",
        "--account-value",
        "100000",
    )
    approval = out.split("approval hash ")[1].split()[0]
    res = R.invoke(
        app, ["orders", "place", approval, "--account-value", "100000", "--root", str(project)]
    )
    assert (
        res.exit_code == 1 and "no valid last close" in res.output
    )  # no price data in an empty lake
    assert "kill switch" in run(project, "kill-switch", "--reason", "drill").lower()
    res = R.invoke(
        app, ["orders", "place", approval, "--account-value", "100000", "--root", str(project)]
    )
    assert "kill_switch" in res.output
    run(project, "kill-switch", "--release", "--reason", "drill finished, write-up in the journal")


def test_ops_and_misc_commands(project: Path) -> None:
    (project / ".env").write_text("BACKUP_PASSPHRASE=correct-horse-battery\n")
    run(project, "journal", "anchor")
    assert "backup written" in run(project, "ops", "backup")
    assert "restore test OK" in run(project, "ops", "restore-test")
    assert "journal_verify" in run(project, "ops", "jobs")
    out = run(project, "digest", "send", "--print")
    assert "No trading from the digest" in out
    assert "G0" in run(project, "gate", "check")
    assert (project / "docs" / "LIVE_GATE.md").exists()
    assert "expired 0" in run(project, "briefings", "expire")
    assert "allocator: FREEZE" in run(project, "eval", "quarterly")
    assert "resolved_now" in run(project, "eval", "monthly")
    assert "0 new triggers" in run(project, "review", "triggers")
    res = R.invoke(app, ["review", "batch", "--root", str(project)])
    assert res.exit_code == 1 and "no screen" in res.output


def test_core_import_and_drift_needs_prices(project: Path, tmp_path: Path) -> None:
    csv = tmp_path / "ira.csv"
    csv.write_text("account,symbol,qty\nira,AVUV,10\nira,CASH,500\n")
    assert "imported" in run(project, "core", "import", str(csv))
    res = R.invoke(app, ["core", "drift", "--root", str(project)])
    assert res.exit_code == 1 and "no price" in res.output


def test_review_run_reports_missing_data(project: Path) -> None:
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n")
    res = R.invoke(
        app,
        [
            "review",
            "run",
            "ABC",
            "--asof",
            dt.date(2026, 9, 27).isoformat(),
            "--root",
            str(project),
        ],
    )
    assert res.exit_code == 1 and "security master" in res.output
