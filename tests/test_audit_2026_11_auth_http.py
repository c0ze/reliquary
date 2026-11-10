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
