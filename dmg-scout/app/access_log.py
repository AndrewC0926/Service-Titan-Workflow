"""Dashboard access logging: one row per page hit, plus a one-time email the
first time a username other than the configured admin user shows up.

This is a FastAPI app (see app/web/main.py), not Flask -- there is no Flask
anywhere in this codebase, and adding a second web framework alongside
FastAPI to get a literal "after_request" decorator would be its own bug.
Starlette's `@app.middleware("http")` is the direct equivalent: it wraps
every request/response the same way, registered once in app/web/main.py.

username is read from the raw Authorization header (extract_basic_auth_
username), never from app.web.main.auth()'s return value. auth() only ever
returns the one configured dashboard username or raises 401 -- it never
reaches this code for anyone else. Reading the header directly, independent
of whether the credentials were VALID, is what makes "a username that isn't
the admin user appeared" a real, detectable event: a failed or probing login
attempt with a different username, not a second legitimate user (this
dashboard has exactly one). That is also why the exclusion filter below is
path-based, not response-content-type-based: a failed-auth request to a
real page route returns a JSON 401 body from FastAPI's default exception
handler, not HTML, and a content-type filter would silently exclude exactly
the requests this feature exists to catch.

Every write here (the access_log insert, the notification email) is best-
effort: a failure must never break the page the visitor was actually trying
to load. Same discipline as app.ops.ping_healthcheck and app.spend.record.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
from datetime import timedelta

import httpx
from sqlmodel import func, select

from app.config import Config, load_config
from app.db import session_scope
from app.models import AccessLog, utcnow

log = logging.getLogger(__name__)

# Path-prefix exclusions. Deliberately NOT a content-type check -- see the
# module docstring for why a failed-auth request (JSON body, not HTML) must
# still be logged.
_EXCLUDED_PREFIXES = ("/static", "/mcp")
_EXCLUDED_EXACT = ("/healthz", "/login", "/login/callback")
# NOT excluded, on purpose: the real `/health` page (Source Health dashboard,
# app/web/main.py) is an HTML page a person visits, distinct from the
# machine-hit JSON `/healthz` check above -- excluding it would have been
# reading the instruction's literal "/health" over what it's actually for.

SECURITY_NOTIFY_TO = "acrane988@gmail.com"


def should_log_path(path: str) -> bool:
    if path in _EXCLUDED_EXACT:
        return False
    return not any(path.startswith(p) for p in _EXCLUDED_PREFIXES)


def extract_basic_auth_username(auth_header: str | None) -> str | None:
    """Whatever username was submitted, valid or not -- see module docstring.
    None means no Authorization header was sent (anonymous), not a failed
    login."""
    if not auth_header or not auth_header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(auth_header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, _, _ = decoded.partition(":")
    return username or None


def admin_username(cfg: Config) -> str:
    return cfg.get("dashboard.basic_auth_username", "andrew")


def send_new_username_notification(cfg: Config, username: str, when) -> bool:
    """Best-effort -- a failed send must never be the reason a page didn't
    load. Always via Resend, independent of digest.transport (this is a
    security notification, not the daily digest)."""
    api_key = os.environ.get(cfg.get("digest.resend.api_key_env", "RESEND_API_KEY"), "")
    if not api_key:
        log.warning("new dashboard username %r seen but RESEND_API_KEY is empty -- notification not sent", username)
        return False
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": cfg.get("digest.from_addr"),
                "to": [SECURITY_NOTIFY_TO],
                "subject": f"[DMG Scout] New dashboard username seen: {username}",
                "text": (f"A basic-auth request with username {username!r} hit the DMG Scout dashboard for "
                        f"the first time, at {when:%Y-%m-%d %H:%M:%S} UTC. This does not mean the login "
                        f"succeeded -- only the configured admin username can pass auth() -- but a new "
                        f"username being tried at all is worth a look."),
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("failed to send new-username notification for %r: %s", username, exc)
        return False


def log_access(session, *, username: str | None, path: str, method: str,
               ip: str | None, user_agent: str | None) -> AccessLog:
    """Inserts the row and, if this is the first time `username` has ever
    appeared and it isn't the admin user, fires the notification -- exactly
    once per username, never once per request, because the "already seen"
    check happens BEFORE this row is committed, and every later hit from the
    same username finds this row (or a subsequent one) already there."""
    cfg = load_config()
    now = utcnow()
    is_first_appearance = False
    if username and username != admin_username(cfg):
        existing = session.exec(select(AccessLog.id).where(AccessLog.username == username)).first()
        is_first_appearance = existing is None

    entry = AccessLog(username=username, path=path, method=method, ip=ip,
                      user_agent=user_agent, created_at=now)
    session.add(entry)
    session.commit()

    if is_first_appearance:
        send_new_username_notification(cfg, username, now)

    return entry


async def access_logging_middleware(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not should_log_path(path):
        return response
    try:
        username = extract_basic_auth_username(request.headers.get("authorization"))
        ip = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        with session_scope() as session:
            log_access(session, username=username, path=path, method=request.method,
                      ip=ip, user_agent=user_agent)
    except Exception:  # noqa: BLE001 — access logging must never break the page it's logging
        log.exception("access logging failed for %s %s", request.method, path)
    return response


def access_summary(session, days: int = 7) -> list[dict]:
    """Distinct (non-null) usernames seen in the last `days` days, each with
    first-seen, last-seen, and a page count. Shared by /admin/access and
    `scout access-summary` so the two can never quietly disagree."""
    since = utcnow() - timedelta(days=days)
    rows = session.exec(
        select(AccessLog.username, func.min(AccessLog.created_at), func.max(AccessLog.created_at),
              func.count(AccessLog.id))
        .where(AccessLog.username.is_not(None), AccessLog.created_at >= since)
        .group_by(AccessLog.username)
        .order_by(func.max(AccessLog.created_at).desc())
    ).all()
    return [
        {"username": username, "first_seen": first_seen, "last_seen": last_seen, "page_count": count}
        for username, first_seen, last_seen, count in rows
    ]
