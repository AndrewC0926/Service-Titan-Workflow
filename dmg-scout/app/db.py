from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from sqlalchemy import create_engine, event, text
from sqlmodel import Session, SQLModel

from app.config import CONFIG_PATH_ENV, DEFAULT_CONFIG, database_url

_engine = None

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def refuse_remote_migration_without_override(url: str) -> None:
    """Refuse to touch a non-local database unless this is a real Render
    deploy or the caller explicitly opts in. Called from alembic/env.py
    before every migration run.

    Manually running `alembic upgrade head` (or any other DDL) against
    production from a local machine, ahead of committing the migration
    that justifies it, is what caused the cron job to fail on 2026-08-13
    and again on 2026-08-17: the web and cron services autodeploy
    independently against one shared database, each running its own
    `alembic upgrade head` on startup, so stamping the database ahead of a
    commit desyncs whichever service hasn't redeployed yet -- its own
    alembic/versions/ has never heard of the revision the DB now claims.

    The safe path -- commit, push, let the deploy (which already runs
    `alembic upgrade head` itself) apply it -- costs nothing extra and is
    the default here on purpose. `RENDER=true` is set automatically by
    Render on every service, so a real deploy is never blocked; only a
    local shell pointed at a remote host is.
    """
    if os.environ.get("RENDER") == "true":
        return
    if os.environ.get("ALEMBIC_ALLOW_REMOTE") == "1":
        return
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return
    raise RuntimeError(
        f"\nRefusing to run Alembic against {host!r} from outside a Render deploy.\n\n"
        "Running `alembic upgrade head` by hand against production, ahead of committing\n"
        "the migration, is what caused the cron job to fail on 2026-08-13 and again on\n"
        "2026-08-17 -- the web and cron services autodeploy independently against one\n"
        "shared database, so stamping it ahead of a commit desyncs whichever service\n"
        "hasn't redeployed yet.\n\n"
        "The safe path: commit the migration, push it, and let Render's own deploy\n"
        "(which already runs `alembic upgrade head`) apply it.\n\n"
        "If you are certain you need to run this by hand right now: push the migration\n"
        "file FIRST so both services pick up the same revision on their next deploy,\n"
        "then set ALEMBIC_ALLOW_REMOTE=1 and re-run.\n"
    )


def _repo_root() -> Path:
    """`pip install .` (no -e, see Dockerfile) copies app/ into site-packages
    as a separate, permanent copy -- a path computed as Path(__file__).
    resolve().parent.parent from THIS file resolves inside site-packages for
    the installed `scout` console script, not the /srv/dmg-scout checkout
    that actually holds alembic.ini and alembic/versions/ (the exact bug
    the Dockerfile's SCOUT_CONFIG override already exists to fix for
    config.yaml -- see app.config.DEFAULT_CONFIG's own comment). Anchoring
    on SCOUT_CONFIG's already-correct, already-deployed directory avoids
    recomputing our own broken __file__-relative guess."""
    return Path(os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG)).resolve().parent


def _known_alembic_revisions() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    repo_root = _repo_root()
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    return {rev.revision for rev in script.walk_revisions()}


def check_migration_state() -> None:
    """Fail loudly, with the exact revision ID, if the database is stamped
    ahead of what THIS deployment's own alembic/versions/ contains.

    Without this, the same mismatch surfaces only when something happens to
    call `alembic upgrade head` next -- for the cron service, that's the
    next scheduled pipeline run, so the failure (and the lost day) shows up
    twenty seconds into a 6am job instead of at deploy time. Call this
    first, before `alembic upgrade head` runs at all, so a stale deploy
    fails immediately and by revision ID rather than mid-pipeline.
    """
    engine = get_engine()
    with engine.connect() as conn:
        try:
            db_revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            return  # no alembic_version table yet -- first deploy ever, nothing to check
    if db_revision is None:
        return
    known = _known_alembic_revisions()
    if db_revision not in known:
        raise SystemExit(
            f"\nFATAL: the database is stamped with alembic revision {db_revision!r}, which "
            f"does not exist in this deployment's alembic/versions/.\n\n"
            f"This means the OTHER service (web or cron) has already deployed a migration "
            f"this one doesn't have yet -- they autodeploy independently against one shared "
            f"database. Running `alembic upgrade head` here will fail the same way, just "
            f"later and with a less specific error.\n\n"
            f"Fix: redeploy this service so it picks up the missing migration file.\n"
        )


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        kwargs = {"pool_pre_ping": True} if url.startswith("postgresql") else {}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            # Tests/local dev only -- production always uses postgresql (see
            # database_url's own DEFAULT_CONFIG). A throwaway per-test sqlite
            # file has no durability requirement, so skipping fsync is free:
            # measured directly, a 73-table create_all() drops from 6-14s to
            # under 0.1s with these two pragmas, which was ~90% of the
            # pre-Block-4A test suite's 40-90 minute chunked runtime (Block
            # 4A Item 0).
            @event.listens_for(_engine, "connect")
            def _fast_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA synchronous=OFF")
                cursor.execute("PRAGMA journal_mode=MEMORY")
                cursor.close()
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
