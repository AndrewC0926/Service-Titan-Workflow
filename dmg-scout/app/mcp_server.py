"""MCP server: query Scout's board conversationally from claude.ai (desktop, web,
and — since connectors added via claude.ai work on mobile — the phone).

Mounted on the SAME FastAPI app as the dashboard (app/web/main.py), same
database, same deploy. No second service, no sync problem — this is a thin
query layer over data the pipeline already produces, not a new system. See
docs/CHARTER.md's invariants: they apply here exactly as everywhere else —
null over inference, every number keeps its basis and confidence, nothing
here writes to the pipeline's own tables (log_outreach is the one write tool,
and it only ever touches Outreach, the same table the dashboard's own
outreach form writes to).

A single trivial tool shipped first and was confirmed working from both
claude.ai web and mobile before any of the other seven were written — there
is an open claude.ai bug where a custom connector shows Connected with tools
listed in Settings, but the tools never reach the model in conversation
(web-specific, reported June-July 2026, still open as of this build). Worth
knowing if a future connector added the same way seems to hang the same
way: check that first, not last.

Mounting note: FastMCP's own FastAPI integration example
(app.mount("/mcp", mcp.http_app(path="/mcp"))) is correct for a server with
no auth, but doubles the path when auth is configured — the OAuth well-known
discovery routes (RFC 8414/9728) must live at the domain root, and app.mount
prefixes EVERYTHING nested inside it, discovery routes included. Verified
empirically (not just from docs) before writing this: build http_app with
the FINAL absolute path baked in, then flatten its routes directly onto the
parent app rather than nesting via app.mount(). See mounted_routes() below.
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

from app.config import load_config
from app.mcp_auth import ScoutOAuthProvider

MCP_PATH = "/mcp"


def _base_url() -> str:
    cfg = load_config()
    explicit = cfg.get("mcp.base_url") or os.environ.get("MCP_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    render_url = os.environ.get("RENDER_EXTERNAL_URL")  # Render sets this automatically
    if render_url:
        return render_url.rstrip("/")
    return "http://localhost:8000"  # local dev only; never reachable from claude.ai


oauth_provider = ScoutOAuthProvider(base_url=_base_url())

mcp = FastMCP(
    name="DMG Scout",
    instructions=(
        "Query DMG's live board of early-signal data center and industrial "
        "mechanical projects, plus the accounts and firms modules. Every "
        "tonnage or dollar figure carries its own confidence/basis — never "
        "restate one without it. Numbers this system could not determine are "
        "null, not guessed; say so plainly rather than filling a gap."
    ),
    auth=oauth_provider,
)


import app.mcp_tools  # noqa: E402,F401 — registers the eight tools via @mcp.tool; see that module


@mcp.custom_route("/login", methods=["GET"])
async def login_page(request):
    from starlette.exceptions import HTTPException

    state = request.query_params.get("state")
    if not state:
        raise HTTPException(400, "Missing state parameter")
    return await oauth_provider.get_login_page(state)


@mcp.custom_route("/login/callback", methods=["POST"])
async def login_callback(request):
    return await oauth_provider.handle_login_callback(request)


mcp_app = mcp.http_app(path=MCP_PATH)


def mounted_routes() -> list:
    """Every route the MCP server needs, at its FINAL absolute path — hand
    these to the parent FastAPI app's own route list (app.router.routes.extend
    (...)), do NOT wrap this app in another app.mount(); see module docstring."""
    return list(mcp_app.routes)


def mounted_middleware() -> list:
    """The auth provider's ASGI middleware (validates the Authorization: Bearer
    header on /mcp and populates request.state/scope["user"] for
    RequireAuthMiddleware to check). Flattening routes the way mounted_routes()
    does bypasses mcp_app's OWN middleware stack entirely — this exists to add
    it back on the parent app via app.add_middleware(mw.cls, *mw.args,
    **mw.kwargs) for each entry. Harmless on every other route: it only acts
    when an Authorization: Bearer header is present, which none of the
    dashboard's HTTP-Basic-protected routes ever send."""
    return list(oauth_provider.get_middleware())
