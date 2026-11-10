"""Regression cases from the November 2026 auth/HTTP audit."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import urlencode

import pytest


def _call(app, method, path, *, headers=None, body=b"", chunks=None, query=b""):
    """Drive the ASGI app directly and record how much of the body it consumed."""
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "query_string": query, "headers": hdrs}
    parts = list(chunks) if chunks is not None else [body]
    consumed = {"bytes": 0}

    async def receive():
        if parts:
            chunk = parts.pop(0)
            consumed["bytes"] += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(parts)}
        return {"type": "http.disconnect"}

    out = {"status": None, "headers": {}, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    out["consumed"] = consumed["bytes"]
    return out


CLAUDE = {"authorization": "Bearer claude-secret"}


def _rpc(method, params=None, rid=1):
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def _pkce():
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# --- 1. request body caps ---------------------------------------------------


@pytest.mark.parametrize("path", ["/oauth/register", "/oauth/authorize", "/oauth/token", "/oauth/revoke"])
def test_oauth_body_is_capped_while_streaming(proxy, path):
    chunks = [b"x" * 16384] * 64  # 1 MiB, no Content-Length
    resp = _call(proxy, "POST", path, chunks=chunks)
    assert resp["status"] == 413
    assert resp["consumed"] <= 128 * 1024


def test_oauth_oversized_content_length_rejected_before_reading(proxy):
    resp = _call(proxy, "POST", "/oauth/register", headers={"content-length": str(48 * 1024 * 1024)},
                 chunks=[b"x" * 1024] * 4)
    assert resp["status"] == 413
    assert resp["consumed"] == 0


def test_oauth_register_still_accepts_normal_body(proxy):
    body = json.dumps({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude"}).encode()
    resp = _call(proxy, "POST", "/oauth/register", body=body, headers={"content-length": str(len(body))})
    assert resp["status"] == 201


def test_mcp_body_cap_fits_inline_image_at_blob_limit(make_proxy):
    p = make_proxy(blob_max_bytes=3000)
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"a" * (3000 - 8)).decode()
    body = _rpc("tools/call", {"name": "reliquary_add_image", "arguments": {"image_base64": image, "caption": "c"}})
    resp = _call(p, "POST", "/claude/mcp", body=body, headers=CLAUDE)
    assert resp["status"] == 200
    assert json.loads(resp["body"])["result"]["isError"] is False
    # Far beyond the inline limit: rejected while streaming, not fully buffered.
    huge = [b"x" * 65536] * 64  # 4 MiB against a ~1 MiB cap
    resp = _call(p, "POST", "/claude/mcp", chunks=huge, headers=CLAUDE)
    assert resp["status"] == 413
    assert resp["consumed"] < 4 * 1024 * 1024


def test_upload_path_keeps_its_own_limit(proxy):
    # The raw upload route is not capped by the JSON body limit.
    slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
    data = b"\x89PNG\r\n\x1a\n" + b"p" * (2 * 1024 * 1024)
    resp = _call(proxy, "POST", slot["upload_url"], body=data,
                 headers={**CLAUDE, "content-type": "image/png", "content-length": str(len(data))})
    assert resp["status"] in (200, 201), resp["body"]


# --- 2. consent page host spoofing ------------------------------------------


@pytest.mark.parametrize("uri", [
    "https://claude.ai@evil.example/cb",
    "https://user:pass@evil.example/cb",
    "https://@evil.example/cb",
    "http://evil.example\\@127.0.0.1/cb",
    "https://evil.example\\.claude.ai/cb",
    "https://claude.ai/cb\r\nSet-Cookie: x=1",
    "https://claude.ai/c\tb",
    "https://claude.ai/cb\x00",
    "https://claude.ai:99999/cb",
])
def test_redirect_uri_rejects_userinfo_backslash_and_controls(uri):
    from oauth import OAuthProvider
    assert OAuthProvider.valid_redirect_uri(uri) is False


@pytest.mark.parametrize("uri", [
    "https://claude.ai/api/mcp/auth_callback",
    "http://localhost:6274/oauth/callback",
    "http://127.0.0.1/cb?x=1",
    "http://[::1]:8080/cb",
])
def test_redirect_uri_still_accepts_legitimate_callbacks(uri):
    from oauth import OAuthProvider
    assert OAuthProvider.valid_redirect_uri(uri) is True


def test_consent_page_shows_hostname_client_and_scope(proxy):
    _verifier, challenge = _pkce()
    params = {
        "response_type": "code", "client_id": "mcp-<b>x</b>", "redirect_uri": "https://claude.ai/api/cb",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read",
    }
    resp = _call(proxy, "GET", "/oauth/authorize", query=urlencode(params).encode())
    page = resp["body"].decode()
    assert resp["status"] == 200
    assert '<span class="host">claude.ai</span>' in page
    assert "mcp-&lt;b&gt;x&lt;/b&gt;" in page and "mcp-<b>" not in page
    assert "read-only" in page
    params["scope"] = "write"
    page = _call(proxy, "GET", "/oauth/authorize", query=urlencode(params).encode())["body"].decode()
    assert "read and write" in page


def test_consent_page_never_shows_userinfo(proxy):
    from oauth import OAuthProvider
    page = OAuthProvider(master_token="m", mcp_resource_path="/claude/mcp").authorize_form_html(
        {"redirect_uri": "https://claude.ai@evil.example/cb"}, error="bad")
    assert '<span class="host">claude.ai' not in page
    assert '<span class="host">evil.example</span>' in page


# --- 3. scope parsing -------------------------------------------------------


@pytest.mark.parametrize("scope", ["read offline_access", "openid read", "READ  search", "mcp read", "read"])
def test_oauth_scope_with_read_word_and_no_write_is_read(scope):
    from oauth import scope_is_write
    assert scope_is_write(scope) is False


@pytest.mark.parametrize("scope", ["write", "read write", "openid write offline_access", "mcp", "", None,
                                   "  ", "mcp offline_access", "openid", "claudeai", "client-specific"])
def test_oauth_write_legacy_and_unknown_only_scopes_keep_write(scope):
    # Existing grants with an unknown-only scope were write; they must not lose it on deploy.
    from oauth import scope_is_write
    assert scope_is_write(scope) is True


def test_oauth_token_with_unknown_only_scope_is_write(proxy):
    access, _ = proxy.oauth.issue_token_pair(client_id="c", scope="claudeai")
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert proxy.resolve_scope(profile, {"authorization": f"Bearer {access}"}) == "write"


def test_consent_page_shows_unknown_scope_gets_write(proxy):
    _verifier, challenge = _pkce()
    params = {"response_type": "code", "client_id": "c", "redirect_uri": "https://claude.ai/api/cb",
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": "claudeai"}
    page = _call(proxy, "GET", "/oauth/authorize", query=urlencode(params).encode())["body"].decode()
    assert "<strong>read and write</strong>" in page
    assert '<span class="host">claudeai</span>' in page
    params["scope"] = "read offline_access"
    page = _call(proxy, "GET", "/oauth/authorize", query=urlencode(params).encode())["body"].decode()
    assert "<strong>read-only</strong>" in page


def test_oauth_token_with_multiword_read_scope_is_read(proxy):
    access, _ = proxy.oauth.issue_token_pair(client_id="c", scope="read offline_access")
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert proxy.resolve_scope(profile, {"authorization": f"Bearer {access}"}) == "read"


def test_static_token_unknown_scope_is_read_and_warns(caplog):
    import logging
    from server import parse_static_tokens
    with caplog.at_level(logging.WARNING):
        parsed = parse_static_tokens(
            "a:ro:t1;b:read-only:t2;c:reader:t3;g:rw:t7;h:openid:t8;d:write:t4;e:mcp:t5;f:read:t6")
    assert dict((label, scope) for label, scope, _ in parsed) == {
        "a": "read", "b": "read", "c": "read", "g": "read", "h": "read",
        "d": "write", "e": "write", "f": "read"}
    warned = [r.getMessage() for r in caplog.records]
    for label, raw in (("a", "ro"), ("b", "read-only"), ("c", "reader"), ("g", "rw"), ("h", "openid")):
        assert any(f"'{label}'" in m and f"'{raw}'" in m for m in warned), (label, warned)
    assert not any("'d'" in m or "'e'" in m or "'f'" in m for m in warned)
    assert not any(f"t{i}" in m for m in warned for i in range(1, 9))  # never log the token itself


# --- 4. non-ASCII credentials -------------------------------------------------


def _call_raw_headers(app, method, path, raw_headers, body=b""):
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": raw_headers}
    sent = {"status": None}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]

    asyncio.run(app(scope, receive, send))
    return sent["status"]


@pytest.mark.parametrize("raw", ["café".encode("utf-8"), "café".encode("latin-1"), "東京".encode("utf-8")])
def test_non_ascii_bearer_is_401_not_500(make_proxy, raw):
    p = make_proxy(static_tokens=(("ro", "read", "static-tok"),))
    status = _call_raw_headers(p, "POST", "/claude/mcp", [(b"authorization", b"Bearer " + raw)], _rpc("ping"))
    assert status == 401


def test_non_ascii_oauth_inputs_fail_cleanly():
    from oauth import OAuthProvider
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", fixed_client_id="pinned")
    assert p.verify_bearer("café") is False
    assert p.verify_client_id("café") is False
    assert p._verify_pkce("v" * 50, "défi", "S256") is False
    assert p._verify_pkce("défi", "défi", "plain") is True


def test_non_ascii_bearer_on_authorize_form_is_401(proxy):
    _verifier, challenge = _pkce()
    form = {"response_type": "code", "client_id": "c", "redirect_uri": "https://claude.ai/cb",
            "code_challenge": challenge, "code_challenge_method": "S256", "bearer_token": "café"}
    resp = _call(proxy, "POST", "/oauth/authorize", body=urlencode(form).encode())
    assert resp["status"] == 401


# --- 5. malformed MCP bodies --------------------------------------------------


@pytest.mark.parametrize("body", [b"\xff\xfe{not utf-8", b'{"jsonrpc": "2.0", "id": 1, "method": "\xc3"}',
                                  b"[" * 100000 + b"]" * 100000])
def test_malformed_mcp_body_is_parse_error(proxy, body):
    resp = _call(proxy, "POST", "/claude/mcp", body=body, headers=CLAUDE)
    assert resp["status"] == 400
    assert json.loads(resp["body"])["error"]["code"] == -32700


@pytest.mark.parametrize("path,headers", [("/oauth/register", {}), ("/v1/chat/completions", CLAUDE),
                                          ("/reliquary/search", CLAUDE)])
def test_malformed_json_bodies_elsewhere_are_400(proxy, path, headers):
    for body in (b'{"a": "\xc3"}', b"[" * 20000 + b"]" * 20000):
        resp = _call(proxy, "POST", path, body=body, headers=headers)
        assert resp["status"] == 400, (path, body[:10])


# --- 6. protocol version negotiation -----------------------------------------


@pytest.mark.parametrize("requested,expected", [
    ("2024-11-05", "2024-11-05"), ("2025-03-26", "2025-03-26"), ("2025-06-18", "2025-06-18"),
    ("2099-01-01", "2025-06-18"), (None, "2025-06-18"), ({"x": 1}, "2025-06-18"),
])
def test_initialize_negotiates_protocol_version(proxy, requested, expected):
    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    if requested is not None:
        params["protocolVersion"] = requested
    resp = _call(proxy, "POST", "/claude/mcp", body=_rpc("initialize", params), headers=CLAUDE)
    assert resp["status"] == 200
    assert json.loads(resp["body"])["result"]["protocolVersion"] == expected
    assert resp["headers"]["mcp-protocol-version"] == expected


# --- 7. /v1 passthrough stays under the upstream prefix ------------------------


def _relay_proxy(make_proxy):
    from unittest.mock import AsyncMock
    import httpx
    p = make_proxy(upstream_base_url="http://ollama:11434/v1")
    p.client.request = AsyncMock(return_value=httpx.Response(200, json={"ok": True}))
    return p


@pytest.mark.parametrize("path", ["/v1/../api/delete", "/v1/%2e%2e/api/delete", "/v1/%252e%252E/api/delete",
                                  "/v1/models/..", "/v1//api/delete", "/v1/..%2fapi", "/v1/..\\api"])
def test_passthrough_rejects_prefix_escape(make_proxy, path):
    p = _relay_proxy(make_proxy)
    resp = _call(p, "DELETE", path, headers=CLAUDE)
    assert resp["status"] == 400
    p.client.request.assert_not_called()


def test_passthrough_forwards_only_allowlisted_headers(make_proxy):
    p = _relay_proxy(make_proxy)
    resp = _call(p, "POST", "/v1/models", body=b"{}", headers={
        "x-reliquary-token": "claude-secret", "authorization": "Bearer upstream-key",
        "content-type": "application/json", "cookie": "session=1", "x-forwarded-for": "10.0.0.1",
        "cf-connecting-ip": "1.2.3.4", "cf-access-jwt-assertion": "jwt", "openai-organization": "org"})
    assert resp["status"] == 200
    call = p.client.request.call_args
    assert call.args[1] == "http://ollama:11434/v1/models"
    sent = {k.lower(): v for k, v in call.kwargs["headers"].items()}
    assert sent == {"authorization": "Bearer upstream-key", "content-type": "application/json",
                    "openai-organization": "org"}


# --- 8. chat proxy headers with non-Latin-1 text -----------------------------


def _chat(p, payload):
    return _call(p, "POST", "/v1/chat/completions", body=json.dumps(payload).encode(),
                 headers={**CLAUDE, "content-type": "application/json"})


@pytest.mark.parametrize("text", ["merhaba şişe", "こんにちは", "100% done\r\nx-evil: 1"])
def test_chat_proxy_non_latin1_query_is_header_safe(make_proxy, text):
    from urllib.parse import unquote
    p = _relay_proxy(make_proxy)
    resp = _chat(p, {"model": "m", "messages": [{"role": "user", "content": text}]})
    assert resp["status"] == 200
    header = resp["headers"]["x-reliquary-query"]
    assert header.isascii() and "\r" not in header and "\n" not in header
    assert unquote(header) == " ".join(text.split())  # trim_text collapses whitespace


def test_chat_proxy_keeps_plain_ascii_query_header(make_proxy):
    p = _relay_proxy(make_proxy)
    resp = _chat(p, {"model": "m", "messages": [{"role": "user", "content": "blue notebook"}]})
    assert resp["headers"]["x-reliquary-query"] == "blue notebook"


def test_chat_proxy_user_id_is_validated(make_proxy):
    p = _relay_proxy(make_proxy)
    msgs = [{"role": "user", "content": "hi"}]
    resp = _chat(p, {"model": "m", "messages": msgs, "reliquary_user_id": 42})
    assert resp["status"] == 200 and resp["headers"]["x-reliquary-user-id"] == "42"
    resp = _chat(p, {"model": "m", "messages": msgs, "reliquary_user_id": "kullanıcı"})
    assert resp["status"] == 200 and resp["headers"]["x-reliquary-user-id"] == "kullan%C4%B1c%C4%B1"
    p.client.request.reset_mock()
    for bad in ({"a": 1}, ["x"], True, 1.5):
        resp = _chat(p, {"model": "m", "messages": msgs, "reliquary_user_id": bad})
        assert resp["status"] == 400, bad
    p.client.request.assert_not_called()


# --- 9. MCP sessions are bounded per credential --------------------------------


def _initialize(p, token):
    resp = _call(p, "POST", "/claude/mcp", body=_rpc("initialize", {"protocolVersion": "2025-06-18"}),
                 headers={"authorization": f"Bearer {token}"})
    assert resp["status"] == 200
    return resp["headers"]["mcp-session-id"]


def test_read_token_cannot_evict_other_sessions(make_proxy):
    import server
    p = make_proxy(static_tokens=(("ro", "read", "read-tok"),))
    owner_session = _initialize(p, "claude-secret")
    for _ in range(server.MCP_MAX_SESSIONS + 1):
        _initialize(p, "read-tok")
    assert owner_session in p.mcp_sessions
    resp = _call(p, "POST", "/claude/mcp", body=_rpc("ping"),
                 headers={**CLAUDE, "mcp-session-id": owner_session})
    assert resp["status"] == 200


def test_session_store_evicts_callers_own_sessions_first():
    from runtime import MCPSessionStore
    now = [0.0]
    store = MCPSessionStore(max_size=4, ttl=1000, clock=lambda: now[0], max_per_owner=4)
    store.add("victim", "claude", owner="alice")
    for i in range(10):
        now[0] += 1
        store.add(f"m{i}", "claude", owner="mallory")
    assert "victim" in store
    assert len(store) == 4
    assert "m9" in store and "m0" not in store


def test_session_store_per_owner_cap():
    from runtime import MCPSessionStore
    store = MCPSessionStore(max_size=100, ttl=1000, clock=lambda: 0.0, max_per_owner=3)
    for i in range(5):
        store.add(f"s{i}", "claude", owner="bob")
    assert [sid for sid in ("s0", "s1", "s2", "s3", "s4") if sid in store] == ["s2", "s3", "s4"]


def test_session_store_loads_old_format_and_round_trips_owner(tmp_path):
    from persistence import JsonFileStore
    from runtime import MCPSessionStore
    path = tmp_path / "mcp_sessions.json"
    path.write_text(json.dumps({"old-sess": "claude", "junk": 5}))
    file_store = JsonFileStore(str(path))
    store = MCPSessionStore(max_size=8, ttl=1000, clock=lambda: 0.0, session_store=file_store)
    assert store.touch("old-sess") == "claude"
    assert store.touch("junk") is None
    store.add("new-sess", "openai", owner="static:ro")
    reloaded = MCPSessionStore(max_size=8, ttl=1000, clock=lambda: 0.0, session_store=file_store)
    assert reloaded.touch("old-sess") == "claude"
    assert reloaded.touch("new-sess") == "openai"
    assert json.loads(path.read_text())["old-sess"] == "claude"  # owner-less entries keep the old shape


# --- 10. rate limit follows the credential, not the token string -------------


def _search(p, token):
    body = _rpc("tools/call", {"name": "reliquary_search", "arguments": {"query": "x"}})
    resp = _call(p, "POST", "/claude/mcp", body=body, headers={"authorization": f"Bearer {token}"})
    return json.loads(resp["body"])["result"]


def test_rate_limit_survives_token_refresh(make_proxy):
    p = make_proxy(rate_limit_searches=2)
    access, refresh = p.oauth.issue_token_pair(client_id="c", scope="read")
    assert not _search(p, access).get("isError")
    assert not _search(p, access).get("isError")
    assert _search(p, access)["structuredContent"]["error"] == "rate_limited"
    resp, err = p.oauth.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is None
    # A refreshed token is the same grant: still limited.
    assert _search(p, resp["access_token"])["structuredContent"]["error"] == "rate_limited"
    # A different grant has its own budget.
    other, _ = p.oauth.issue_token_pair(client_id="c", scope="read")
    assert not _search(p, other).get("isError")


def test_rate_limit_keys_static_tokens_by_label(make_proxy):
    p = make_proxy(static_tokens=(("ci", "read", "tok-a"),))
    headers = {"authorization": "Bearer tok-a"}
    assert p._rate_key(headers) == "static:ci"
    assert p._rate_key({"authorization": "Bearer claude-secret"}) == "master:claude"
    assert p._rate_key({}) == "anon"


def test_rate_limits_default_off(proxy):
    assert proxy.write_limiter.limit == 0 and proxy.search_limiter.limit == 0


# --- 11. audit log permissions ----------------------------------------------


def test_audit_log_is_created_owner_only(tmp_path):
    import os
    import stat
    from audit import AuditLog
    path = tmp_path / "logs" / "audit.jsonl"
    old = os.umask(0o022)
    try:
        log = AuditLog(str(path))
        log.record(action="add", ids={"id": "1"})
        log.record(action="delete", ids={"id": "1"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [json.loads(line)["action"] for line in path.read_text().splitlines()] == ["add", "delete"]


# --- 12. tokens are hashed at rest ------------------------------------------


def _stores(tmp_path):
    from persistence import JsonFileStore
    return dict(token_store=JsonFileStore(str(tmp_path / "oauth_tokens.json")),
                refresh_token_store=JsonFileStore(str(tmp_path / "oauth_refresh_tokens.json")))


def _state_text(tmp_path):
    return (tmp_path / "oauth_tokens.json").read_text() + (tmp_path / "oauth_refresh_tokens.json").read_text()


def test_new_tokens_are_not_stored_in_plaintext(tmp_path):
    from oauth import OAuthProvider
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", **_stores(tmp_path))
    access, refresh = p.issue_token_pair(client_id="c", scope="write")
    text = _state_text(tmp_path)
    assert access not in text and refresh not in text
    restored = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", **_stores(tmp_path))
    assert restored.access_token_scope(access) == "write"
    resp, err = restored.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is None


def test_plaintext_state_files_migrate_and_keep_connectors_signed_in(tmp_path):
    import time
    from oauth import OAuthProvider
    old_access, old_refresh, consumed_refresh = "A" * 43, "R" * 43, "C" * 43
    now = time.time()
    (tmp_path / "oauth_tokens.json").write_text(json.dumps({old_access: {
        "client_id": "claude", "scope": "mcp", "expires_at": now + 3600,
        "resource": "https://h/claude/mcp", "family_id": "fam1"}}))
    (tmp_path / "oauth_refresh_tokens.json").write_text(json.dumps({
        old_refresh: {"client_id": "claude", "scope": "mcp", "resource": "https://h/claude/mcp",
                      "family_id": "fam1", "created_at": now, "expires_at": None,
                      "consumed": False, "consumed_at": None},
        consumed_refresh: {"client_id": "other", "scope": "mcp", "resource": None, "family_id": "fam2",
                           "created_at": now - 10, "expires_at": None, "consumed": True, "consumed_at": now - 5},
    }))
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", **_stores(tmp_path))
    # Rewritten on load: no plaintext token survives on disk.
    text = _state_text(tmp_path)
    for token in (old_access, old_refresh, consumed_refresh):
        assert token not in text
    # The connector is still signed in: its access token works for its resource...
    assert p.access_token_scope(old_access, resource="https://h/claude/mcp") == "mcp"
    assert p.access_token_identity(old_access) == "family:fam1"
    # ...and its refresh token still rotates.
    resp, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": old_refresh, "client_id": "claude"})
    assert err is None
    assert resp["access_token"] not in _state_text(tmp_path)
    # Replay detection survives the migration: the rotated-away token kills its family.
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": old_refresh, "client_id": "claude"})
    assert "reuse" in err[2]
    assert p.access_token_scope(resp["access_token"]) is None
    # A token already consumed before the migration is still recognised as a replay.
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": consumed_refresh})
    assert "reuse" in err[2]


def test_hashed_store_key_is_not_a_credential(tmp_path):
    from oauth import OAuthProvider
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", **_stores(tmp_path))
    access, refresh = p.issue_token_pair(client_id="c", scope="write")
    stored_access = next(iter(json.loads((tmp_path / "oauth_tokens.json").read_text())))
    stored_refresh = next(iter(json.loads((tmp_path / "oauth_refresh_tokens.json").read_text())))
    assert p.access_token_scope(stored_access) is None
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": stored_refresh})
    assert err[1] == "invalid_grant"
    assert p.revoke_token(stored_refresh) is False
    assert p.access_token_scope(access) == "write"


def test_server_keeps_old_state_dir_connector_signed_in(make_proxy, tmp_path):
    import time
    state = tmp_path / "state"
    state.mkdir()
    token = "L" * 43
    (state / "oauth_tokens.json").write_text(json.dumps({token: {
        "client_id": "claude", "scope": "mcp", "expires_at": time.time() + 3600,
        "resource": None, "family_id": "fam"}}))
    p = make_proxy(state_dir=str(state))
    assert token not in (state / "oauth_tokens.json").read_text()
    resp = _call(p, "POST", "/claude/mcp", body=_rpc("initialize", {}), headers={"authorization": f"Bearer {token}"})
    assert resp["status"] == 200
    assert token not in (state / "mcp_sessions.json").read_text()
