"""Block 4C Item 1 (Master Plan v3.6 section 35): "Nightly database
backups with a tested restore."

Render's own continuous point-in-time recovery (confirmed on, see
docs/RUNBOOK.md "Backups") already covers "recover from an accidental
drop." What it does not give: a dump a human can actually read the
restore procedure for, or a repeatable drill that PROVES the dump is
real and not silently empty/corrupt. This module is that second layer,
not a replacement for the first.

Storage: a Render Disk mounted on dmg-scout-web (BACKUP_DIR, default
/var/backups/scout) -- object storage was considered and rejected for
now because no S3/R2/B2 credentials exist anywhere in this project
(confirmed: grepped .env and every Render service's env vars, all
empty). Cron jobs cannot have a Disk at all (Render's own docs, "You
can't add a disk to a cron job service") -- only web/private/background-
worker services can, which is why the actual pg_dump runs inside the web
service (via POST /internal/backup, app/web/main.py) rather than in the
cron container that triggers it nightly (scripts/run-pipeline.sh).
scripts/backup.sh (S3-based, written earlier) is superseded by this and
left in place only as the object-storage path for a future move off
Render Disk -- see that script's own header.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)

RETENTION_DAYS = 14
DUMP_PREFIX = "scout-"
DUMP_SUFFIX = ".dump"
SCRATCH_DB_NAME = "scout_restore_drill"


def _dump_path(out_dir: Path, stamp: str) -> Path:
    return out_dir / f"{DUMP_PREFIX}{stamp}{DUMP_SUFFIX}"


def _stamp_of(path: Path) -> datetime | None:
    name = path.name
    if not (name.startswith(DUMP_PREFIX) and name.endswith(DUMP_SUFFIX)):
        return None
    try:
        return datetime.strptime(name[len(DUMP_PREFIX):-len(DUMP_SUFFIX)], "%Y-%m-%d")
    except ValueError:
        return None


def list_dumps(out_dir: Path) -> list[Path]:
    """Every scout-YYYY-MM-DD.dump in out_dir, oldest first. Anything that
    doesn't match the exact naming convention is ignored, never deleted --
    prune_old_dumps only ever removes files this same function recognizes."""
    if not out_dir.exists():
        return []
    dated = [(p, _stamp_of(p)) for p in out_dir.iterdir() if p.is_file()]
    return [p for p, stamp in sorted(dated, key=lambda t: (t[1] is None, t[1])) if stamp is not None]


def latest_dump(out_dir: Path) -> Path | None:
    dumps = list_dumps(out_dir)
    return dumps[-1] if dumps else None


def prune_old_dumps(out_dir: Path, *, retention_days: int = RETENTION_DAYS, now: datetime | None = None) -> list[str]:
    """Delete dumps older than retention_days (default 14, per this item).
    Keeps at least one dump no matter how old, so a source that's been down
    for 3 weeks doesn't prune its way to zero backups -- a stale backup is
    still strictly better than none for a restore drill or a real recovery."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=retention_days)
    dumps = list_dumps(out_dir)
    pruned = []
    for path in dumps[:-1]:  # never prune the newest
        stamp = _stamp_of(path)
        if stamp is not None and stamp < cutoff:
            path.unlink()
            pruned.append(path.name)
    return pruned


def run_backup(database_url: str, *, out_dir: Path, stamp: str | None = None) -> dict:
    """pg_dump --format=custom into out_dir/scout-{stamp}.dump (today's
    UTC date by default), then prune anything past RETENTION_DAYS.
    Returns {"file": str, "size_bytes": int, "pruned": [str, ...]}.
    Raises subprocess.CalledProcessError if pg_dump itself fails -- never
    swallowed, since a silently-failed backup is worse than a loud one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.utcnow().strftime("%Y-%m-%d")
    out_path = _dump_path(out_dir, stamp)
    subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(out_path), database_url],
        check=True, capture_output=True, text=True,
    )
    pruned = prune_old_dumps(out_dir)
    log.info("backup written: %s (%d bytes), pruned %d", out_path, out_path.stat().st_size, len(pruned))
    return {"file": str(out_path), "size_bytes": out_path.stat().st_size, "pruned": pruned}


def _server_url(database_url: str, dbname: str) -> str:
    """Same host/user/password/port, a different database name -- for
    connecting to the Postgres server's own maintenance database (or the
    fresh scratch database) rather than the app's real one."""
    parts = urlparse(database_url)
    return urlunparse(parts._replace(path=f"/{dbname}"))


# Tables asserted after a restore -- named directly in this item ("asserts
# row counts on projects, signals, opportunities, metric_snapshot"), by
# their real table names (see app/models.py __tablename__).
DRILL_TABLES = ["projects", "signals", "opportunities", "metric_snapshots"]


def restore_drill(database_url: str, *, dump_path: Path, scratch_db_name: str = SCRATCH_DB_NAME) -> dict:
    """Restore dump_path into a fresh scratch database on the SAME Postgres
    server database_url points at (never a second server -- the drill is
    "can we actually read this dump back," not "do we have a second
    Postgres instance"), assert each of DRILL_TABLES has at least one row,
    then drop the scratch database again so a repeated drill never
    accumulates state.

    Reuses app.db.refuse_remote_migration_without_override's exact safety
    check before touching anything: this runs CREATE DATABASE/DROP DATABASE
    against whatever server database_url names, and a local shell pointed
    at production by mistake is exactly the accident that function already
    exists to catch for Alembic.

    Returns {"tables": {name: count}, "ok": bool, "dump_file": str}. Never
    leaves the scratch database behind, even on failure (drop runs in a
    finally)."""
    from sqlalchemy import create_engine, text

    from app.db import refuse_remote_migration_without_override

    refuse_remote_migration_without_override(database_url)

    maint_url = _server_url(database_url, "postgres")
    scratch_url = _server_url(database_url, scratch_db_name)

    maint_engine = create_engine(maint_url, isolation_level="AUTOCOMMIT")
    with maint_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{scratch_db_name}"'))
    maint_engine.dispose()

    try:
        subprocess.run(
            ["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", scratch_url, str(dump_path)],
            check=True, capture_output=True, text=True,
        )
        counts: dict[str, int] = {}
        scratch_engine = create_engine(scratch_url)
        with scratch_engine.connect() as conn:
            for table in DRILL_TABLES:
                counts[table] = conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        scratch_engine.dispose()
        ok = all(n > 0 for n in counts.values())
        return {"tables": counts, "ok": ok, "dump_file": str(dump_path)}
    finally:
        maint_engine = create_engine(maint_url, isolation_level="AUTOCOMMIT")
        with maint_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db_name}" WITH (FORCE)'))
        maint_engine.dispose()
