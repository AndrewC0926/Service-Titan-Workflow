"""Operational plumbing: dead man's switch ping, doctor checks, JSON logging."""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import timedelta

import httpx
from sqlmodel import select

from app.config import anthropic_api_key, load_config
from app.db import get_engine, session_scope
from app.models import SourceRun, utcnow

log = logging.getLogger(__name__)

HEALTHCHECK_ENV = "HEALTHCHECK_URL"  # e.g. https://hc-ping.com/<uuid>


def ping_healthcheck(success: bool = True) -> bool:
    """Dead man's switch: healthchecks.io alerts if this ping stops arriving.
    Separate from source-failure alerting — this catches nothing running at all."""
    url = os.environ.get(HEALTHCHECK_ENV)
    if not url:
        log.warning("%s not set — dead man's switch is DISARMED", HEALTHCHECK_ENV)
        return False
    target = url if success else url.rstrip("/") + "/fail"
    try:
        httpx.get(target, timeout=10)
        return True
    except httpx.HTTPError as exc:
        log.error("healthcheck ping failed: %s", exc)
        return False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": utcnow().isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging() -> None:
    """LOG_FORMAT=json switches to structured logs (default on Render via render.yaml)."""
    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT") == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def doctor() -> list[tuple[str, bool, str]]:
    """Health checks for `scout doctor`: (check, ok, detail)."""
    checks: list[tuple[str, bool, str]] = []

    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        checks.append(("database", True, str(get_engine().url).split("@")[-1]))
    except Exception as exc:  # noqa: BLE001
        checks.append(("database", False, f"{type(exc).__name__}: {exc}"))
        return checks  # everything else needs the DB

    key = anthropic_api_key()
    if not key:
        checks.append(("anthropic_api_key", False, "ANTHROPIC_API_KEY not set — triage/extract will fail"))
    else:
        try:
            import anthropic
            anthropic.Anthropic(api_key=key).models.list(limit=1)
            checks.append(("anthropic_api_key", True, "key valid"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("anthropic_api_key", False, f"key rejected: {exc}"))

    total, used, free = shutil.disk_usage("/")
    free_gb = free / 1e9
    checks.append(("disk", free_gb > 1.0, f"{free_gb:.1f} GB free"))

    cfg = load_config()
    stale_cutoff = utcnow() - timedelta(hours=36)
    with session_scope() as session:
        for name in [n for n in cfg.data.get("sources", {}) if cfg.source_enabled(n)]:
            if name == "manual":
                continue
            last_ok = session.exec(
                select(SourceRun).where(SourceRun.source == name, SourceRun.ok == True)  # noqa: E712
                .order_by(SourceRun.started_at.desc())).first()
            if last_ok is None:
                checks.append((f"source:{name}", False, "no successful run recorded"))
            else:
                fresh = last_ok.started_at >= stale_cutoff
                checks.append((f"source:{name}", fresh,
                               f"last success {last_ok.started_at:%Y-%m-%d %H:%M}Z"))

    # A fragmenting board is a silent failure: nothing errors, the row count just
    # grows and every rate computed over it is wrong. See app/duplicates.py.
    from app.duplicates import find_duplicates
    with session_scope() as session:
        dup = find_duplicates(session)
    checks.append(("project_duplicates", dup["n_groups"] == 0,
                   "no duplicate project rows" if dup["n_groups"] == 0 else
                   f"{dup['n_groups']} suspect groups, {dup['n_excess_rows']} excess rows "
                   f"of {dup['n_projects']} — run `scout duplicates`"))

    armed = bool(os.environ.get(HEALTHCHECK_ENV))
    checks.append(("dead_mans_switch", armed,
                   "HEALTHCHECK_URL set" if armed else "HEALTHCHECK_URL not set — cron death would be silent"))

    from app.spend import budget_status
    st = budget_status()
    checks.append(("llm_budget", not st["exhausted"],
                   f"today ${st['today_usd']:.2f} / ${st['daily_budget_usd']:.2f}, month ${st['month_usd']:.2f}"))
    return checks
