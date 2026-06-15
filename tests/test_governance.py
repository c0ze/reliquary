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
    result = run(proxy.call_mcp_tool(profile, "reliquary_capabilities", {}, can_write=True))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    for key in ("what", "endpoint", "tools", "rules", "taxonomy", "project_context"):
        assert key in sc
    assert "reliquary_capabilities" in sc["tools"]


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
    assert "reliquary_capabilities" in claude_names
    assert "capabilities" in openai_names


def test_capabilities_reflects_write_scope(proxy):
    # A read-only request must not advertise write tools as available; they belong
    # under write_tools_when_authorized instead (matches tools/list for that scope).
    profile = _profile(proxy, "claude")
    ro = run(proxy.call_mcp_tool(profile, "reliquary_capabilities", {}, can_write=False))["structuredContent"]
    assert "reliquary_add_memory" not in ro["tools"] and "reliquary_propose_update" not in ro["tools"]
    assert "reliquary_add_memory" in ro["write_tools_when_authorized"]
    assert "reliquary_propose_update" in ro["write_tools_when_authorized"]
    assert "reliquary_capabilities" in ro["tools"] and "reliquary_search" in ro["tools"]  # reads always available
    rw = run(proxy.call_mcp_tool(profile, "reliquary_capabilities", {}, can_write=True))["structuredContent"]
    assert "reliquary_add_memory" in rw["tools"] and rw["write_tools_when_authorized"] == []


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
    result = run(proxy.call_mcp_tool(profile, "reliquary_propose_update", {"target_id": "imp-1"}, can_write=False))
    assert result.get("isError") is True
    sc = result["structuredContent"]
    assert sc["error"] == "insufficient_scope" and "suggested_action" in sc


def test_protected_delete_suggests_propose_update(proxy):
    proxy.memory._store["imp-1"] = {"id": "imp-1", "memory": "imported", "metadata": {"source_group": "imported"}, "user_id": "my_lord"}
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "reliquary_delete", {"id": "imp-1"}, can_write=True))
    assert result.get("isError") is True
    sc = result["structuredContent"]
    assert sc["error"] == "protected_record" and "reliquary_propose_update" in sc["suggested_action"]


def test_protected_update_suggests_propose_update(proxy):
    proxy.memory._store["imp-1"] = {"id": "imp-1", "memory": "imported", "metadata": {"source_group": "imported"}, "user_id": "my_lord"}
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "reliquary_update", {"id": "imp-1", "text": "new text"}, can_write=True))
    assert result.get("isError") is True
    sc = result["structuredContent"]
    assert sc["error"] == "protected_record" and "reliquary_propose_update" in sc["suggested_action"]


def test_propose_update_openai_ignores_caller_user_id(proxy):
    # Lean endpoint (allow_user_id=False): a caller-supplied user_id must be ignored.
    run(proxy.handle_propose_update_tool({"target_id": "imp-1", "user_id": "intruder"}, allow_user_id=False))
    rec = next(r for r in proxy.memory._store.values() if r.get("metadata", {}).get("kind") == "correction")
    assert rec["user_id"] == proxy.settings.user_id  # server user, not "intruder"


# ---------------------------------------------------------------------------
# Phase 4: caller-context threading + soft project routing bias (#42)
# ---------------------------------------------------------------------------

def _seed_two(proxy):
    # Project record: has domain=dev and room=reliquary; id sorts LAST alphabetically.
    # Distinct memory text prevents deduplication by apply_retrieval_quality.
    proxy.memory._store["zzz-note"] = {"id": "zzz-note", "memory": "alpha feature for reliquary project",
        "metadata": {"domain": "dev", "room": "reliquary"}, "user_id": "my_lord"}
    # Non-project record: id sorts FIRST alphabetically without bias.
    proxy.memory._store["aaa-note"] = {"id": "aaa-note", "memory": "alpha changelog entry",
        "metadata": {"domain": "misc"}, "user_id": "my_lord"}


def test_context_bias_floats_project_memory_up(proxy):
    from context import resolve_context
    _seed_two(proxy)
    ctx = resolve_context({"context": {"repo": "c0ze/reliquary"}}, {})
    result = run(proxy.handle_search_tool({"query": "alpha"}, context=ctx))
    assert result["structuredContent"]["results"][0]["id"] == "zzz-note"


def test_no_context_leaves_order_unchanged(proxy):
    _seed_two(proxy)
    result = run(proxy.handle_search_tool({"query": "alpha"}))  # no context
    ids = [r["id"] for r in result["structuredContent"]["results"]]
    # equal scores => deterministic tie-break by id asc; project record is NOT lifted
    assert ids[0] == "aaa-note"  # "aaa-note" < "zzz-note" alphabetically; bias absent
    assert "zzz-note" in ids


def test_context_threaded_through_call_mcp_tool(proxy):
    from context import resolve_context
    _seed_two(proxy)
    profile = _profile(proxy, "claude")
    ctx = resolve_context({"context": {"repo": "c0ze/reliquary"}}, {})
    result = run(proxy.call_mcp_tool(profile, "reliquary_search", {"query": "alpha"}, can_write=False, context=ctx))
    assert result["structuredContent"]["results"][0]["id"] == "zzz-note"


def test_context_bias_prefers_this_repo_over_other_dev(proxy):
    # Tiered bias: this repo's memory (room match) must outrank generic dev memory
    # from another repo, even when the other record's id sorts first.
    from context import resolve_context
    proxy.memory._store["zzz-mine"] = {"id": "zzz-mine", "memory": "alpha note for my repo",
        "metadata": {"domain": "dev", "room": "reliquary"}, "user_id": "my_lord"}
    proxy.memory._store["aaa-other"] = {"id": "aaa-other", "memory": "alpha note other dev project",
        "metadata": {"domain": "dev", "room": "somethingelse"}, "user_id": "my_lord"}
    ctx = resolve_context({"context": {"repo": "c0ze/reliquary"}}, {})
    result = run(proxy.handle_search_tool({"query": "alpha"}, context=ctx))
    assert result["structuredContent"]["results"][0]["id"] == "zzz-mine"


# ---------------------------------------------------------------------------
# Phase 5: reliquary://sources provenance registry (#46)
# ---------------------------------------------------------------------------

def test_sources_resource_groups_by_source(proxy):
    import json
    from types import SimpleNamespace
    proxy.catalog = SimpleNamespace(
        routeable_domains=[],
        records_by_id={
            "r1": SimpleNamespace(import_record_id="r1", metadata={"source_group": "imported", "source": "vault", "source_ref": "a.md"}),
            "r2": SimpleNamespace(import_record_id="r2", metadata={"source_group": "imported", "source": "vault", "source_ref": "b.md"}),
            "u1": SimpleNamespace(import_record_id="u1", metadata={"source_group": "user-write"}),
        },
    )
    res = proxy.read_resource("reliquary://sources")
    payload = json.loads(res["contents"][0]["text"])
    assert payload["total"] == 3
    vault = next(s for s in payload["sources"] if s["source"] == "vault")
    assert vault["count"] == 2 and vault["source_group"] == "imported"
    assert set(vault["sample_refs"]) == {"a.md", "b.md"}
    user = next(s for s in payload["sources"] if s["source_group"] == "user-write")
    assert user["count"] == 1


def test_sources_resource_listed_with_catalog(proxy):
    from types import SimpleNamespace
    proxy.catalog = SimpleNamespace(routeable_domains=[], records_by_id={})
    uris = {r["uri"] for r in proxy.mcp_resources()}
    assert "reliquary://sources" in uris


def test_sources_absent_without_catalog(proxy):
    proxy.catalog = None
    uris = {r["uri"] for r in proxy.mcp_resources()}
    assert "reliquary://sources" not in uris
    assert proxy.read_resource("reliquary://sources") is None
