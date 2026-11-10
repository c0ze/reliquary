"""Regression cases from the external review of the November 2026 audit branch:
OAuth scope in verbatim mode, MCP session ownership, taxonomy filters and
legacy taxonomy spellings, duplicate imports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oauth import OAuthProvider, scope_is_write  # noqa: E402


def _call(app, method, path, *, headers=None, body=b""):
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": hdrs}
    parts = [body]

    async def receive():
        if parts:
            return {"type": "http.request", "body": parts.pop(0), "more_body": False}
        return {"type": "http.disconnect"}

    out = {"status": None, "headers": {}, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out


def _rpc(method, params=None, rid=1):
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def _exchange(provider, *, scope):
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = provider.issue_code({
        "client_id": "client-1", "redirect_uri": "https://claude.ai/cb", "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": scope,
    })
    return provider.exchange_code({
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/cb",
        "client_id": "client-1", "code_verifier": verifier,
    })


# --- 2. verbatim OAuth mode must not upgrade a read grant to the master -------


def test_verbatim_mode_read_grant_gets_a_derived_read_token():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", issue_verbatim_token=True)
    response, error = _exchange(provider, scope="read")
    assert error is None
    assert response["access_token"] != "MASTER"
    assert response["scope"] == "read"
    assert provider.access_token_scope(response["access_token"]) == "read"
    assert not scope_is_write(provider.access_token_scope(response["access_token"]))


def test_verbatim_mode_write_grant_still_gets_the_master():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", issue_verbatim_token=True)
    for scope in ("write", "mcp", "read write"):
        response, error = _exchange(provider, scope=scope)
        assert error is None and response["access_token"] == "MASTER", scope


def test_verbatim_mode_read_grant_cannot_write_through_the_server(make_proxy):
    p = make_proxy(oauth_verbatim_token=True)
    response, error = _exchange(p.oauth, scope="read")
    assert error is None
    headers = {"authorization": f"Bearer {response['access_token']}"}
    resp = _call(p, "POST", "/claude/mcp", headers=headers, body=_rpc("tools/list"))
    names = {tool["name"] for tool in json.loads(resp["body"])["result"]["tools"]}
    assert "reliquary_search" in names
    assert "reliquary_delete" not in names and "reliquary_add_memory" not in names


# --- 8. MCP sessions belong to the credential and endpoint that made them -----

CLAUDE = {"authorization": "Bearer claude-secret"}
READER = {"authorization": "Bearer read-tok"}


def _initialize(p, headers, path="/claude/mcp"):
    resp = _call(p, "POST", path, headers=headers, body=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert resp["status"] == 200
    return resp["headers"]["mcp-session-id"]


def _ping(p, headers, session_id, path="/claude/mcp"):
    return _call(p, "POST", path, headers={**headers, "mcp-session-id": session_id}, body=_rpc("ping"))["status"]


def test_another_credential_cannot_use_a_session(make_proxy):
    p = make_proxy(static_tokens=(("ro", "read", "read-tok"),))
    session = _initialize(p, CLAUDE)
    assert _ping(p, READER, session) == 404
    assert _ping(p, CLAUDE, session) == 200


def test_another_credential_cannot_delete_a_session(make_proxy):
    p = make_proxy(static_tokens=(("ro", "read", "read-tok"),))
    session = _initialize(p, CLAUDE)
    resp = _call(p, "DELETE", "/claude/mcp", headers={**READER, "mcp-session-id": session})
    assert resp["status"] == 204  # answered like an unknown session
    assert _ping(p, CLAUDE, session) == 200
    assert _call(p, "DELETE", "/claude/mcp", headers={**CLAUDE, "mcp-session-id": session})["status"] == 204
    assert _ping(p, CLAUDE, session) == 404


def test_a_session_is_bound_to_its_endpoint(make_proxy):
    p = make_proxy(static_tokens=(("rw", "write", "both-tok"),))
    both = {"authorization": "Bearer both-tok"}
    session = _initialize(p, both, "/claude/mcp")
    assert _ping(p, both, session, "/openai/mcp") == 404
    _call(p, "DELETE", "/openai/mcp", headers={**both, "mcp-session-id": session})
    assert _ping(p, both, session, "/claude/mcp") == 200


def test_ownerless_persisted_session_adopts_its_first_credential(make_proxy, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp_sessions.json").write_text(json.dumps({"legacy": "claude"}))
    tokens = (("ro", "read", "read-tok"), ("rw", "write", "both-tok"))
    p = make_proxy(state_dir=str(state), static_tokens=tokens)
    both = {"authorization": "Bearer both-tok"}
    assert _ping(p, both, "legacy", "/openai/mcp") == 404  # the wrong endpoint does not adopt it
    assert _ping(p, CLAUDE, "legacy") == 200
    assert _ping(p, READER, "legacy") == 404
    assert json.loads((state / "mcp_sessions.json").read_text())["legacy"] == ["claude", "master:claude"]
    restarted = make_proxy(state_dir=str(state), static_tokens=tokens)
    assert _ping(restarted, READER, "legacy") == 404
    assert _ping(restarted, CLAUDE, "legacy") == 200
