import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory for tests

import pytest

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


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir():
    return FIXTURES
