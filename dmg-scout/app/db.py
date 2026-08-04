from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.config import database_url

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        kwargs = {"pool_pre_ping": True} if url.startswith("postgresql") else {}
        _engine = create_engine(url, **kwargs)
    return _engine


def init_db() -> None:
    """Create tables directly (dev/test). Production uses alembic upgrade head."""
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session
