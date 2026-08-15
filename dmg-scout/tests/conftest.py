import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory for tests

import pytest
from sqlmodel import SQLModel

from app.config import load_config


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    """Fresh file-backed SQLite DB per test (in-memory doesn't share across connections)."""
    import app.db as db_mod

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    db_mod.init_db()
    with db_mod.session_scope() as session:
        yield session


def call_via_closing_session(fn, *args, **kwargs):
    """Call fn(session, *args, **kwargs) inside a real app.db.session_scope()
    that commits and closes BEFORE this returns -- the same shape CLI
    commands, MCP tools, and FastAPI routes actually use in production, and
    the one thing the db_session fixture above structurally cannot exercise
    (it holds its own session open for an entire test, so nothing read from
    it can ever raise DetachedInstanceError, no matter what a function
    returns).

    Use this whenever a test needs to prove a function's return value
    survives the session that produced it: seed data with db_session as
    usual (commit it so a second, independent session can see it), then
    call the function under test through this helper instead, and read
    every field of the result you expect a real caller to read. If any
    SQLModel ORM attribute access on the result raises, the function is
    handing back a live ORM object instead of already-extracted plain
    values -- the exact bug class that broke `scout fetch`, `scout doctor`,
    and app.pipeline_health.memory_pressure_status in production before
    each was fixed to extract scalars while its own session was still
    open. See tests/test_pipeline_health.py's
    test_check_and_alert_staleness_survives_a_closed_session for the
    pattern in use."""
    from app.db import session_scope
    with session_scope() as session:
        return fn(session, *args, **kwargs)


def assert_no_orm_objects(value, *, _path="result"):
    """Recursively asserts nothing in `value` (walking dicts/lists/tuples/
    sets) is a live SQLModel table instance.

    Structural companion to call_via_closing_session() above: that helper
    proves a function's return value doesn't raise when read after its
    session closes, but only for the specific attributes a test happens to
    touch -- a live ORM object stashed under a key nobody reads in THIS
    test would pass silently and still be the exact bug (see
    memory_pressure_status's docstring in app/pipeline_health.py: it used
    to return {"run": run} instead of {"run_id": run.id}, which broke `scout
    check-freshness` specifically because that caller never touched `.run`
    at all until formatting the CLI's own output line, well after its
    session had closed). Call this on any dict/list a session-scoped
    function returns, regardless of which fields the rest of the test
    exercises, to catch a live ORM object leaking in anywhere -- not just
    the one place a previous bug happened to surface."""
    if isinstance(value, SQLModel):
        pytest.fail(f"{_path} is a live SQLModel instance ({type(value).__name__}) -- extract plain "
                   f"values (scalars, dicts) before returning; see call_via_closing_session's docstring "
                   f"above for why a caller reading this after its session closes will raise "
                   f"DetachedInstanceError")
    if isinstance(value, dict):
        for k, v in value.items():
            assert_no_orm_objects(v, _path=f"{_path}[{k!r}]")
    elif isinstance(value, (list, tuple, set)):
        for i, v in enumerate(value):
            assert_no_orm_objects(v, _path=f"{_path}[{i}]")


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir():
    return FIXTURES
