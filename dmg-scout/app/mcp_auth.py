"""Single-user OAuth 2.1 authorization server for the MCP connector.

Why this exists at all: claude.ai's custom-connector UI has no field for a
static bearer token or API key — only OAuth (Authorization URL, Token URL,
Client ID/Secret, discovered via Dynamic Client Registration), and every
connection requires PKCE with S256. That is a platform constraint, not a
choice made here; there is no simpler path for the claude.ai / mobile use
case, even though a plain bearer token would work fine for other MCP clients
(Claude Code, scripts).

Structurally this is the MCP Python SDK's own official single-user demo
(examples/servers/simple-auth/mcp_simple_auth/simple_auth_provider.py),
adapted two ways for a service that actually has to stay working:

1. DB-backed, not in-memory. The demo keeps clients/codes/tokens in plain
   dicts, which lives exactly as long as one process — the first Render
   restart (a deploy, a crash, a host migration) would silently log Claude
   out. Everything here goes through the three mcp_* tables in app/models.py.
2. The "login" is the existing dashboard password (DASHBOARD_PASSWORD), not a
   separate demo username/password. One user, one secret, nothing new to
   provision or remember. Registration (DCR) itself is left open to any
   client — the security boundary is the password check in
   handle_login_callback, not client registration, which is exactly the
   demo's own model.

No refresh tokens (matches the demo — load_refresh_token/exchange_
refresh_token are unimplemented on purpose). A single-user tool with a
password-gated login doesn't get meaningfully safer from a 1-hour access
token over a long-lived one; the token TTL exists to bound a leak, not to
bound normal use, and implementing rotation would add real complexity for a
threat model that doesn't ask for it. ACCESS_TOKEN_TTL_SECONDS is a knob if
that judgment ever needs revisiting.
"""
from __future__ import annotations

import os
import secrets
import time

from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from sqlmodel import select
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from app.config import load_config
from app.db import session_scope
from app.models import McpAccessToken, McpAuthCode, McpOAuthClient

AUTH_CODE_TTL_SECONDS = 300  # RFC-typical, and Claude exchanges it within seconds
ACCESS_TOKEN_TTL_SECONDS = 90 * 24 * 3600  # 90 days — see module docstring


def _dashboard_password() -> str:
    cfg = load_config()
    return os.environ.get(cfg.get("dashboard.basic_auth_password_env", "DASHBOARD_PASSWORD"), "")


class ScoutOAuthProvider(OAuthProvider):
    """DB-backed single-user OAuth provider. See module docstring.

    `_pending` maps an in-flight login's `state` to the params needed to
    resume it after the password form posts back. This is the ONE piece of
    genuinely ephemeral, in-memory state — it lives only between showing the
    login form and the human submitting it (seconds), unlike clients/codes/
    tokens, which persist for real (a DCR registration, a live connector)
    and go to the DB. A restart mid-login just means retrying /authorize,
    which claude.ai does automatically.
    """

    def __init__(self, base_url: str):
        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["scout"], default_scopes=["scout"],
            ),
        )
        self.login_path = "/login"
        self._pending: dict[str, dict] = {}

    # -- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with session_scope() as session:
            row = session.get(McpOAuthClient, client_id)
            return OAuthClientInformationFull.model_validate(row.data) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("no client_id provided")
        with session_scope() as session:
            existing = session.get(McpOAuthClient, client_info.client_id)
            if existing:
                existing.data = client_info.model_dump(mode="json")
            else:
                session.add(McpOAuthClient(client_id=client_info.client_id,
                                           data=client_info.model_dump(mode="json")))

    # -- authorization: redirect to our own password form -------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = params.state or secrets.token_hex(16)
        self._pending[state] = {
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "client_id": client.client_id,
            "resource": params.resource,
            "scopes": params.scopes or ["scout"],
        }
        return f"{self.login_path}?state={state}"

    async def get_login_page(self, state: str) -> HTMLResponse:
        if state not in self._pending:
            raise HTTPException(400, "Unknown or expired login attempt — go back to Claude and try again.")
        html = f"""<!DOCTYPE html>
<html><head><title>DMG Scout — Connect Claude</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; max-width: 420px; margin: 80px auto; padding: 0 20px; }}
input {{ width: 100%; padding: 10px; margin: 10px 0 20px; box-sizing: border-box;
         border: 1px solid #ccc; border-radius: 4px; font-size: 16px; }}
button {{ background: #2a6df4; color: white; padding: 10px 20px; border: none;
          border-radius: 4px; font-size: 16px; cursor: pointer; width: 100%; }}
h2 {{ margin-bottom: 4px; }}
p {{ color: #666; font-size: 14px; }}
</style></head>
<body>
<h2>Connect Claude to DMG Scout</h2>
<p>Same password as the dashboard.</p>
<form method="post" action="/login/callback">
  <input type="hidden" name="state" value="{state}">
  <input type="password" name="password" placeholder="Dashboard password" autofocus required>
  <button type="submit">Authorize</button>
</form>
</body></html>"""
        return HTMLResponse(content=html)

    async def handle_login_callback(self, request: Request) -> Response:
        form = await request.form()
        password = form.get("password")
        state = form.get("state")
        if not isinstance(password, str) or not isinstance(state, str):
            raise HTTPException(400, "Missing password or state")

        pending = self._pending.get(state)
        if not pending:
            raise HTTPException(400, "Unknown or expired login attempt — go back to Claude and try again.")

        real_password = _dashboard_password()
        if not real_password or not secrets.compare_digest(password, real_password):
            # Re-show the form rather than a bare 401 — a wrong password on a
            # phone keyboard is common, and losing the whole flow over one
            # typo is a bad way to spend the one time this matters.
            html = await self.get_login_page(state)
            return HTMLResponse(
                content=html.body.decode().replace("<h2>", "<p style='color:#c00'>Wrong password.</p><h2>"),
                status_code=401,
            )

        new_code = f"scout_{secrets.token_hex(24)}"
        auth_code = AuthorizationCode(
            code=new_code,
            client_id=pending["client_id"],
            redirect_uri=AnyHttpUrl(pending["redirect_uri"]),
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            scopes=pending["scopes"],
            code_challenge=pending["code_challenge"],
            resource=pending["resource"],
            subject="andrew",
        )
        with session_scope() as session:
            session.add(McpAuthCode(code=new_code, data=auth_code.model_dump(mode="json"),
                                    expires_at=auth_code.expires_at))
        del self._pending[state]
        return RedirectResponse(
            url=construct_redirect_uri(pending["redirect_uri"], code=new_code, state=state),
            status_code=302,
        )

    # -- token exchange ------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with session_scope() as session:
            row = session.get(McpAuthCode, authorization_code)
            if row is None:
                return None
            if row.expires_at < time.time():
                session.delete(row)
                return None
            return AuthorizationCode.model_validate(row.data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        token = f"scout_{secrets.token_hex(32)}"
        access = AccessToken(
            token=token, client_id=client.client_id, scopes=authorization_code.scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
            resource=authorization_code.resource, subject=authorization_code.subject,
        )
        with session_scope() as session:
            row = session.get(McpAuthCode, authorization_code.code)
            if row is None:
                raise ValueError("authorization code already used or expired")
            session.delete(row)
            session.add(McpAccessToken(token=token, data=access.model_dump(mode="json"),
                                       expires_at=access.expires_at))
        return OAuthToken(access_token=token, token_type="Bearer",
                          expires_in=ACCESS_TOKEN_TTL_SECONDS,
                          scope=" ".join(authorization_code.scopes))

    async def load_access_token(self, token: str) -> AccessToken | None:
        with session_scope() as session:
            row = session.get(McpAccessToken, token)
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at < time.time():
                session.delete(row)
                return None
            return AccessToken.model_validate(row.data)

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        return None  # not supported — see module docstring

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("refresh tokens not supported — re-authorize instead")

    async def revoke_token(self, token: str, token_type_hint: str | None = None) -> None:
        with session_scope() as session:
            row = session.get(McpAccessToken, token)
            if row is not None:
                session.delete(row)


def revoke_all_tokens() -> int:
    """Admin escape hatch: kill every issued token (a phone gets lost, etc).
    Registered clients survive — the next /authorize just needs the password
    again, no need to redo Claude's DCR registration."""
    with session_scope() as session:
        rows = session.exec(select(McpAccessToken)).all()
        for row in rows:
            session.delete(row)
        return len(rows)
