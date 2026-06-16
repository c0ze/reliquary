"""Tests for read/write scope resolution and named static tokens (issue #14)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest  # noqa: E402

from server import Mem0ChatProxy, ProxySettings  # noqa: E402
from conftest import FakeMemory  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _headers(token: str | None = None) -> dict[str, str]:
    if token is None:
        return {}
    return {"authorization": f"Bearer {token}"}


def _proxy_with_static_tokens(tmp_path, fake_memory, static_tokens):
    """Build a proxy with extra static tokens; reuses same config shape as conftest."""
    cfg = tmp_path / "config_scopes.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))
    settings = ProxySettings(
        config_path=str(cfg),
        user_id="my_lord",
        claude_token="claude-secret",
        openai_token="openai-secret",
        blob_dir=str(tmp_path / "blobs_scopes"),
        blob_signing_key="test-signing-key-scopes",
        static_tokens=static_tokens,
        compiled_dir=str(tmp_path / "compiled"),
    )
    return Mem0ChatProxy(settings, memory=fake_memory, compiled_memory=FakeMemory())


# ---------------------------------------------------------------------------
# resolve_scope tests
# ---------------------------------------------------------------------------

def test_resolve_scope_master_token_gives_write(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = proxy.resolve_scope(profile, _headers("claude-secret"))
    assert result == "write"


def test_resolve_scope_static_read_token(tmp_path, fake_memory):
    p = _proxy_with_static_tokens(
        tmp_path, fake_memory,
        static_tokens=(("ro", "read", "read-token-abc"),)
    )
    profile = p.endpoint_profiles[p.settings.claude_mcp_path]
    assert p.resolve_scope(profile, _headers("read-token-abc")) == "read"


def test_resolve_scope_static_write_token(tmp_path, fake_memory):
    p = _proxy_with_static_tokens(
        tmp_path, fake_memory,
        static_tokens=(("rw", "write", "write-token-def"),)
    )
    profile = p.endpoint_profiles[p.settings.claude_mcp_path]
    assert p.resolve_scope(profile, _headers("write-token-def")) == "write"


def test_resolve_scope_unknown_bearer_is_none(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert proxy.resolve_scope(profile, _headers("not-a-real-token")) is None


def test_resolve_scope_no_auth_openai_is_none(proxy):
    # No anonymous access on any endpoint: a tokenless request to OpenAI (which
    # used to allow opt-in no-auth reads) now resolves to None, same as Claude.
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    assert proxy.resolve_scope(profile, {}) is None


def test_resolve_scope_no_auth_claude_is_none(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert proxy.resolve_scope(profile, {}) is None


def test_openai_endpoint_is_write_capable_like_claude(proxy):
    # Both endpoints are symmetric: allow_write is True, and the endpoint's own
    # master token grants write scope.
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    assert claude.allow_write is True and openai.allow_write is True
    assert proxy.resolve_scope(openai, _headers("openai-secret")) == "write"


# ---------------------------------------------------------------------------
# mcp_tools_for with can_write flag
# ---------------------------------------------------------------------------

def test_mcp_tools_for_read_only_excludes_write_tools(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = proxy.mcp_tools_for(profile, can_write=False)
    names = {t["name"] for t in tools}
    assert "reliquary_add_memory" not in names
    assert "reliquary_add_image" not in names
    assert "reliquary_delete" not in names
    assert "reliquary_update" not in names
    assert "reliquary_delete_image" not in names
    # Read tools must still be present
    assert "reliquary_search" in names
    assert "reliquary_fetch" in names


def test_mcp_tools_for_write_includes_write_tools(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = proxy.mcp_tools_for(profile, can_write=True)
    names = {t["name"] for t in tools}
    assert "reliquary_add_memory" in names
    assert "reliquary_add_image" in names
    assert "reliquary_delete" in names
    assert "reliquary_update" in names


def test_mcp_tools_for_openai_read_only_excludes_write_tools(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    tools = proxy.mcp_tools_for(profile, can_write=False)
    names = {t["name"] for t in tools}
    assert "add_memory" not in names
    assert "add_image" not in names
    assert "delete" not in names
    assert "update" not in names
    assert "search" in names
    assert "fetch" in names


# ---------------------------------------------------------------------------
# call_mcp_tool with can_write=False returns insufficient_scope
# ---------------------------------------------------------------------------

def test_call_mcp_tool_write_tool_with_read_scope_returns_error(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]

    async def _run():
        return await proxy.call_mcp_tool(
            profile, "reliquary_add_memory", {"text": "hello"}, can_write=False
        )

    result = asyncio.run(_run())
    assert result.get("isError") is True
    content = result.get("content", [])
    structured = result.get("structuredContent", {})
    # The structured error must be insufficient_scope
    assert structured.get("error") == "insufficient_scope"


def test_call_mcp_tool_read_tool_with_read_scope_still_works(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]

    async def _run():
        return await proxy.call_mcp_tool(
            profile, "reliquary_search", {"query": "anything"}, can_write=False
        )

    result = asyncio.run(_run())
    assert result.get("isError") is not True


def test_call_mcp_tool_write_tool_with_write_scope_works(proxy, fake_memory):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]

    async def _run():
        return await proxy.call_mcp_tool(
            profile, "reliquary_add_memory", {"text": "test memory"}, can_write=True
        )

    result = asyncio.run(_run())
    assert result.get("isError") is not True


def test_call_mcp_tool_openai_write_with_read_scope_returns_error(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]

    async def _run():
        return await proxy.call_mcp_tool(
            profile, "add_memory", {"text": "hello"}, can_write=False
        )

    result = asyncio.run(_run())
    assert result.get("isError") is True
    structured = result.get("structuredContent", {})
    assert structured.get("error") == "insufficient_scope"


# ---------------------------------------------------------------------------
# is_allowed_token still works (backward compat)
# ---------------------------------------------------------------------------

def test_is_allowed_token_delegates_to_resolve_scope(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert proxy.is_allowed_token(profile, _headers("claude-secret")) is True
    assert proxy.is_allowed_token(profile, _headers("wrong")) is False
