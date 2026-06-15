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


def test_propose_update_stores_correction(proxy):
    result = run(proxy.handle_propose_update_tool({"target_id": "imp-1", "reason": "wrong date", "replacement_text": "Correct: 1999"}))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    assert sc["target_id"] == "imp-1" and sc["status"] == "proposed"
    rec = next(r for r in proxy.memory._store.values() if r.get("metadata", {}).get("kind") == "correction")
    assert rec["metadata"]["target_id"] == "imp-1"
    assert rec["metadata"]["status"] == "proposed"
    assert rec["metadata"]["source_group"] == "user-write"


def test_propose_update_missing_target(proxy):
    result = run(proxy.handle_propose_update_tool({"reason": "x"}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "missing_target"


def test_propose_update_write_gated(proxy):
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "propose_update", {"target_id": "imp-1"}, can_write=False))
    assert result.get("isError") is True
    sc = result["structuredContent"]
    assert sc["error"] == "insufficient_scope" and "suggested_action" in sc


def test_protected_delete_suggests_propose_update(proxy):
    proxy.memory._store["imp-1"] = {"id": "imp-1", "memory": "imported", "metadata": {"source_group": "imported"}, "user_id": "my_lord"}
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "mem0_delete", {"id": "imp-1"}, can_write=True))
    assert result.get("isError") is True
    sc = result["structuredContent"]
    assert sc["error"] == "protected_record" and "propose_update" in sc["suggested_action"]


def test_propose_update_openai_ignores_caller_user_id(proxy):
    # Lean endpoint (allow_user_id=False): a caller-supplied user_id must be ignored.
    run(proxy.handle_propose_update_tool({"target_id": "imp-1", "user_id": "intruder"}, allow_user_id=False))
    rec = next(r for r in proxy.memory._store.values() if r.get("metadata", {}).get("kind") == "correction")
    assert rec["user_id"] == proxy.settings.user_id  # server user, not "intruder"
