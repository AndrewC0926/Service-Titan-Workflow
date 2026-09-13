"""Block 4B Item 1: snapshot tests for the design system's application to
Today, Pipeline, Signals, Deadlines, Notes (Reports is snapshotted in
tests/test_reports.py, once that page exists).

Snapshots are taken against each page's EMPTY state (no signals, no
opportunities, no notes seeded) rather than populated data -- the empty
state is the one rendering that is fully deterministic without having to
freeze every default_factory=utcnow() timestamp on every seeded row, and
it directly exercises the thing Item 1 asks for: "an empty-state macro
that writes a sentence," not a blank table. Populated-data rendering for
each of these pages is already covered by tests/test_web.py and
tests/test_pipeline_and_signals_pages.py -- this file is deliberately not
a second copy of that coverage.

Run with UPDATE_SNAPSHOTS=1 to (re)write the golden files after a
deliberate, reviewed markup change.
"""
import base64
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.web.main import app

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}

# Volatile substrings normalized before comparison: ISO-ish timestamps and
# freshness "X.Xd ago"/"X days" text that would otherwise make every
# snapshot fail on wall-clock drift even with identical markup.
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?")
_FRESHNESS = re.compile(r"\b\d+(\.\d+)?\s*(d|days?|h|hours?)\b")


def _normalize(html: str) -> str:
    html = _ISO_DATETIME.sub("<TIMESTAMP>", html)
    html = _FRESHNESS.sub("<FRESHNESS>", html)
    return html


def _assert_matches_snapshot(name: str, html: str):
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.html"
    normalized = _normalize(html)
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        path.write_text(normalized)
        return
    expected = path.read_text()
    assert normalized == expected, (
        f"snapshot mismatch for {name} -- rerun with UPDATE_SNAPSHOTS=1 after confirming "
        f"the markup change was deliberate"
    )


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("name,path", [
    ("today_empty", "/"),
    ("pipeline_empty", "/pipeline"),
    ("signals_empty", "/signals"),
    ("deadlines_empty", "/deadlines"),
    ("notes_empty", "/notes"),
    ("reports_empty", "/reports"),
])
def test_page_empty_state_snapshot(client, name, path):
    resp = client.get(path, headers=AUTH)
    assert resp.status_code == 200
    _assert_matches_snapshot(name, resp.text)
