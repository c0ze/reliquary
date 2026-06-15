from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def run(coro):
    return asyncio.run(coro)


def _profile(proxy, name):
    if name == "claude":
        return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    return proxy.endpoint_profiles[proxy.settings.openai_mcp_path]


def test_capabilities_claude(proxy):
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "mem0_capabilities", {}, can_write=True))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    for key in ("what", "endpoint", "tools", "rules", "taxonomy", "project_context"):
        assert key in sc
    assert "mem0_capabilities" in sc["tools"]


def test_capabilities_openai(proxy):
    profile = _profile(proxy, "openai")
    result = run(proxy.call_mcp_tool(profile, "capabilities", {}, can_write=True))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    assert sc["endpoint"] == "openai"
    assert "not accepted" in sc["rules"]["user_id"]


def test_capabilities_listed_on_both_endpoints(proxy):
    claude = _profile(proxy, "claude")
    openai = _profile(proxy, "openai")
    claude_names = {t["name"] for t in proxy.mcp_tools_for(claude, can_write=True)}
    openai_names = {t["name"] for t in proxy.mcp_tools_for(openai, can_write=True)}
    assert "mem0_capabilities" in claude_names
    assert "capabilities" in openai_names
