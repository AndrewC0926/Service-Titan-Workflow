from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.demo.run import run_demo
from committee.journal.store import Journal


def test_offline_demo_end_to_end(tmp_path: Path) -> None:
    lines: list[str] = []
    out = run_demo(tmp_path / "demo", dt.date(2026, 10, 2), reviews=2, say=lines.append)
    assert out["journal_ok"] is True and out["reviews"] == 2 and out.get("orders", 0) >= 1
    j = Journal(tmp_path / "demo" / "var" / "journal.sqlite")
    agent_flags = {
        f for e in j.entries("agent_output") for f in (e.payload["output"] or {}).get("flags", [])
    }
    assert "SIMULATED" in agent_flags  # simulated research is always labelled
    assert j.latest("fill") is not None and j.latest("approval") is not None
    assert any("gate check" in s for s in lines)
    with pytest.raises(FileExistsError):
        run_demo(tmp_path / "demo", dt.date(2026, 10, 2), say=lines.append)
