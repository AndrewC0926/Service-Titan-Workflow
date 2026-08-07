"""End-to-end tests for the MCP connector: DCR, the password-gated OAuth
flow, and that a valid token actually reaches a tool call while an invalid
one is rejected.

Uses TestClient as a context manager (`with TestClient(app) as client:`)
unlike test_web.py's `client` fixture — the MCP endpoint needs the ASGI
lifespan to run (it starts the StreamableHTTP session manager's task group;
see app/mcp_server.py), which only fires inside the context-manager form.
"""
import base64
import hashlib
import secrets

import pytest
from starlette.testclient import TestClient

from app.mcp_auth import revoke_all_tokens
from app.models import McpAccessToken, McpOAuthClient
from app.web.main import app

REDIRECT_URI = "https://claude.ai/api/mcp/callback"


@pytest.fixture()
def mcp_client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    with TestClient(app) as c:
        yield c


def _pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(client) -> str:
    r = client.post("/register", json={
        "redirect_uris": [REDIRECT_URI], "client_name": "Claude",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize(client, client_id: str, challenge: str, state: str = "xyz") -> str:
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }, follow_redirects=False)
    assert r.status_code in (302, 307)
    return r.headers["location"]


def _login_and_get_code(client, login_url: str, password: str) -> str:
    state = login_url.split("state=")[1]
    r = client.post("/login/callback", data={"state": state, "password": password},
                    follow_redirects=False)
    assert r.status_code == 302, r.text
    return r.headers["location"].split("code=")[1].split("&")[0]


def _get_token(client) -> str:
    """Full flow from registration through a working access token."""
    client_id = _register(client)
    verifier, challenge = _pkce()
    login_url = _authorize(client, client_id, challenge)
    code = _login_and_get_code(client, login_url, "testpw")
    r = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_dynamic_client_registration(mcp_client):
    client_id = _register(mcp_client)
    assert client_id


def test_registered_client_persists_across_requests(mcp_client, db_session):
    """DCR is DB-backed (not the SDK demo's in-memory dict) — Claude only
    registers once and expects the client to still exist on the next call."""
    client_id = _register(mcp_client)
    row = db_session.get(McpOAuthClient, client_id)
    assert row is not None


def test_wrong_password_is_rejected_and_retry_still_works(mcp_client):
    client_id = _register(mcp_client)
    verifier, challenge = _pkce()
    login_url = _authorize(mcp_client, client_id, challenge)
    state = login_url.split("state=")[1]

    r = mcp_client.post("/login/callback", data={"state": state, "password": "wrong"})
    assert r.status_code == 401
    assert "Wrong password" in r.text

    # the SAME state still works — a mistyped password on a phone must not
    # burn the whole login attempt
    code = _login_and_get_code(mcp_client, login_url, "testpw")
    r = mcp_client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    })
    assert r.status_code == 200


def test_auth_code_is_single_use(mcp_client):
    client_id = _register(mcp_client)
    verifier, challenge = _pkce()
    login_url = _authorize(mcp_client, client_id, challenge)
    code = _login_and_get_code(mcp_client, login_url, "testpw")
    token_params = {
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }
    r1 = mcp_client.post("/token", data=token_params)
    assert r1.status_code == 200
    r2 = mcp_client.post("/token", data=token_params)
    assert r2.status_code == 401


def test_valid_token_reaches_mcp_endpoint(mcp_client):
    token = _get_token(mcp_client)
    r = mcp_client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "initialize", "id": 1,
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                  "clientInfo": {"name": "test", "version": "1"}},
    }, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert '"protocolVersion"' in r.text


def test_no_token_is_rejected(mcp_client):
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                        headers=MCP_HEADERS)
    assert r.status_code == 401


def test_garbage_token_is_rejected(mcp_client):
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                        headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_board_summary_tool_reports_real_project_count(mcp_client, db_session):
    """One real tool call through the full OAuth + transport stack, not just a
    direct function call — tests/test_mcp_tools.py covers each tool's query
    logic in isolation; this proves the wiring between them actually works."""
    from app.manual import add_manual_signal
    from app.pipeline.resolve import run_resolve

    add_manual_signal(db_session, "prequal_invite", "test", project_name="Test DC",
                      county="Los Angeles", state="CA", mw_it=50, stage="entitlement")
    from app.config import load_config
    run_resolve(db_session, load_config(), use_llm=False)

    token = _get_token(mcp_client)
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    r = mcp_client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "initialize", "id": 1,
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                  "clientInfo": {"name": "test", "version": "1"}},
    }, headers=headers)
    headers["mcp-session-id"] = r.headers["mcp-session-id"]
    mcp_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers=headers)

    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "id": 2,
                                     "params": {"name": "board_summary", "arguments": {}}},
                        headers=headers)
    assert r.status_code == 200
    assert "1 active projects" in r.text


def test_revoke_all_tokens_locks_out_existing_sessions(mcp_client, db_session):
    token = _get_token(mcp_client)
    revoked = revoke_all_tokens()
    assert revoked == 1
    assert db_session.get(McpAccessToken, token) is None
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
                        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_dashboard_basic_auth_still_works_alongside_mcp_middleware(mcp_client):
    """The MCP auth middleware is global (added via app.add_middleware, not
    scoped to /mcp), so it must not interfere with the dashboard's own
    HTTP-Basic auth on unrelated routes."""
    auth = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}
    r = mcp_client.get("/", headers=auth)
    assert r.status_code == 200
