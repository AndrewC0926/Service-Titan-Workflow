from __future__ import annotations

import datetime as dt
from pathlib import Path

from committee.journal.store import Journal
from committee.orchestration.triggers import find_triggers, journal_new
from committee.ui.digest import build_digest

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def test_triggers(tmp_path: Path) -> None:
    clock = [T0]
    j = Journal(tmp_path / "j.sqlite", clock=lambda: clock[0])
    b = j.append(
        "briefing",
        {
            "review_id": "r1",
            "symbol": "ABC",
            "entry_price": 100.0,
            "recommendation": "BUY",
            "legs": [],
            "falsifiers": [
                {"observable": "gross margin", "threshold": "< 40%", "check_by": "2026-12-31"}
            ],
        },
    )
    j.append("approval", {"briefing_hash": b.hash})
    today = dt.date(2026, 10, 15)
    assert find_triggers(j, today, lambda s: 95.0, lambda s, d: []) == []
    trig = find_triggers(j, dt.date(2027, 1, 5), lambda s: 79.0, lambda s, d: ["2.02,9.01", "4.02"])
    keys = {t.key.split(":")[0] for t in trig}
    assert keys == {"falsifier", "8k", "drop20", "quarterly"}
    clock[0] = dt.datetime(2027, 1, 5, 12, tzinfo=dt.UTC)
    assert len(journal_new(j, trig)) == 4
    assert journal_new(j, trig) == []  # journaled once
    d = build_digest(j, clock[0])
    assert len(d.triggers) == 4 and any("never auto-sell" in t for t in d.triggers)
