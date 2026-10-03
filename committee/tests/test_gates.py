from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.broker.live_gate import live_allowed
from committee.journal.store import Journal
from committee.ops.gates import evaluate, run_gate_check, sign

NOW = dt.datetime(2027, 3, 1, tzinfo=dt.UTC)


def _ready_journal(path: Path) -> Journal:
    t = [NOW - dt.timedelta(days=120)]
    j = Journal(path, clock=lambda: t[0])
    for i in range(120):
        t[0] = NOW - dt.timedelta(days=120 - i)
        if i % 4 == 0:
            j.append("briefing", {"symbol": f"S{i}"})
            j.append(
                "order",
                {
                    "mode": "paper",
                    "status": "filled",
                    "broker_order_id": f"o{i}",
                    "approval_hash": "x",
                },
            )
            j.append("fill", {"broker_order_id": f"o{i}", "qty": 1})
        j.append("anchor", {"anchored_seq": i})
    t[0] = NOW - dt.timedelta(days=1)
    j.append("note", {"kind": "ci_green", "commit": "abc"})
    j.append("note", {"kind": "ips_signed", "doc": "IPS-2027.pdf"})
    j.append("note", {"kind": "adviser_review", "adviser": "A. Adviser", "cpa": "C. Pa"})
    return j


def test_fresh_system_fails_every_gate(tmp_path: Path) -> None:
    gates = evaluate(Journal(tmp_path / "j.sqlite"), tmp_path, NOW)
    assert [g.ok for g in gates] == [False, False, False]


def test_ready_system_passes_and_sign_flow(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "REVIEW.md").write_text(
        "| id | severity | status |\n|---|---|---|\n| R1 | high | closed |\n"
    )
    j = _ready_journal(tmp_path / "j.sqlite")
    gate_file = tmp_path / "var" / "LIVE_GATE.signed.json"
    gates, report = run_gate_check(j, tmp_path, gate_file, NOW)
    assert all(g.ok for g in gates), [
        (c.text, c.evidence) for g in gates for c in g.criteria if not c.ok
    ]
    assert "G1 Operational burn-in (paper): PASS" in report.read_text()
    assert gate_file.exists()
    assert not live_allowed(True, gate_file, j)[0]  # unsigned
    with pytest.raises(ValueError):
        sign(j, gate_file, "me", "ok")
    sign(j, gate_file, "me", "I have read the gate report and accept the live core scope.")
    assert live_allowed(True, gate_file, j)[0]
    assert not live_allowed(False, gate_file, j)[0]  # env flag still required


def test_open_finding_or_p1_blocks_and_removes_stale_gate_file(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "REVIEW.md").write_text("| R1 | high | open |\n")
    j = _ready_journal(tmp_path / "j.sqlite")
    gate_file = tmp_path / "gate.json"
    gate_file.write_text("{}")
    gates, _ = run_gate_check(j, tmp_path, gate_file, NOW)
    assert not gates[0].ok and not gate_file.exists()
    with pytest.raises(FileNotFoundError):
        sign(j, gate_file, "me", "I have read the gate report and accept it fully.")


def test_unreconciled_order_fails_g1(tmp_path: Path) -> None:
    j = _ready_journal(tmp_path / "j.sqlite")
    j.append("order", {"mode": "paper", "status": "filled", "broker_order_id": "ghost"})
    g1 = evaluate(j, tmp_path, NOW)[1]
    assert not next(c for c in g1.criteria if "reconciliation" in c.text).ok
