from __future__ import annotations

import datetime as dt
from pathlib import Path

from committee.evaluation.resolve import add_months, resolve_due
from committee.journal.store import Journal


def test_add_months() -> None:
    assert add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert add_months(dt.date(2026, 11, 15), 3) == dt.date(2027, 2, 15)


def test_resolve_only_when_due(tmp_path: Path) -> None:
    t = [dt.datetime(2026, 1, 5, tzinfo=dt.UTC)]
    j = Journal(tmp_path / "j.sqlite", clock=lambda: t[0])
    j.append("state_transition", {"review_id": "r1", "symbol": "ABC", "to": "SCREENED"})
    j.append(
        "forecast",
        {
            "thesis_id": "r1",
            "agent": "chair",
            "event": "beats_benchmark",
            "horizon_months": 3,
            "probability": 0.6,
            "cohort": "c",
        },
    )
    j.append(
        "forecast",
        {
            "thesis_id": "r1",
            "agent": "chair",
            "event": "doubles",
            "horizon_months": 36,
            "probability": 0.1,
            "cohort": "c",
        },
    )
    paths = {"ABC": [100.0, 120.0], "SPY": [100.0, 105.0]}
    assert resolve_due(j, dt.date(2026, 3, 1), lambda s, a, b: paths[s]) == 0
    assert resolve_due(j, dt.date(2026, 4, 6), lambda s, a, b: paths[s]) == 1
    r = j.latest("forecast_resolution")
    assert r is not None and r.payload["outcome"] == 1 and r.payload["agent"] == "chair"
    assert resolve_due(j, dt.date(2026, 4, 6), lambda s, a, b: paths[s]) == 0  # once only
