from __future__ import annotations

from pathlib import Path

import pytest

APP = str(Path(__file__).resolve().parents[1] / "src" / "committee" / "ui" / "app.py")


@pytest.mark.parametrize("page", ["Today", "Briefing", "Portfolio", "Scorecards", "Journal", "Costs", "Settings"])
def test_every_page_renders(project: Path, monkeypatch: pytest.MonkeyPatch, page: str) -> None:
    from streamlit.testing.v1 import AppTest

    from committee.journal.store import Journal

    monkeypatch.setenv("COMMITTEE_ROOT", str(project))
    j = Journal(project / "var" / "journal.sqlite")
    j.append("briefing", {"symbol": "ABC", "recommendation": "BUY", "cooling_off_hours": 0,
                          "legs": [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 2}]})
    j.close()
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_approve_from_dashboard(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    from committee.journal.store import Journal

    monkeypatch.setenv("COMMITTEE_ROOT", str(project))
    j = Journal(project / "var" / "journal.sqlite")
    j.append("briefing", {"symbol": "ABC", "recommendation": "BUY", "cooling_off_hours": 0,
                          "legs": [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 2}]})
    j.close()
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.sidebar.radio[0].set_value("Briefing").run()
    at.text_input[0].input("Cheap, insiders buying, sized by the engine.").run()
    at.button[0].click().run()
    assert not at.exception
    assert any("Approved" in s.value for s in at.success)
    j = Journal(project / "var" / "journal.sqlite")
    assert j.latest("approval") is not None
