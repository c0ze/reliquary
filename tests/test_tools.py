"""Integration tests: MCP tool handlers against the in-memory FakeMemory backend."""

from __future__ import annotations

import asyncio
import base64


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. CRUD text flow
# --------------------------------------------------------------------------- #

def test_add_memory_returns_id(proxy):
    result = run(proxy.handle_add_memory_tool({"text": "Alice likes tea"}))
    assert not result["isError"]
    ids = result["structuredContent"]["ids"]
    assert len(ids) == 1
    assert ids[0].startswith("fake-")


def test_search_finds_added_memory(proxy):
    run(proxy.handle_add_memory_tool({"text": "Bob enjoys hiking"}))
    result = run(proxy.handle_search_tool({"query": "Bob"}))
    assert not result["isError"]
    sc = result["structuredContent"]
    assert sc["results"]
    texts = [r["text"] for r in sc["results"]]
    assert any("Bob" in t for t in texts)


def test_fetch_returns_text(proxy):
    add_result = run(proxy.handle_add_memory_tool({"text": "Carol plays chess"}))
    mid = add_result["structuredContent"]["ids"][0]
    fetch_result = run(proxy.handle_fetch_tool({"id": mid}))
    assert not fetch_result["isError"]
    sc = fetch_result["structuredContent"]
    assert sc["text"] == "Carol plays chess"


def test_update_changes_text(proxy):
    add_result = run(proxy.handle_add_memory_tool({"text": "Dave reads novels"}))
    mid = add_result["structuredContent"]["ids"][0]
    update_result = run(proxy.handle_update_tool({"id": mid, "text": "Dave reads comics"}, allow_user_id=True))
    assert not update_result["isError"]
    fetch_result = run(proxy.handle_fetch_tool({"id": mid}))
    assert not fetch_result["isError"]
    assert fetch_result["structuredContent"]["text"] == "Dave reads comics"


def test_delete_removes_memory(proxy):
    add_result = run(proxy.handle_add_memory_tool({"text": "Eve writes code"}))
    mid = add_result["structuredContent"]["ids"][0]
    del_result = run(proxy.handle_delete_tool({"id": mid}, allow_user_id=True))
    assert not del_result["isError"]
    assert del_result["structuredContent"]["deleted"] is True
    # Fetch should now return not_found
    fetch_result = run(proxy.handle_fetch_tool({"id": mid}))
    assert fetch_result["isError"]
    assert fetch_result["structuredContent"]["error"] == "not_found"


# --------------------------------------------------------------------------- #
# 2. Corpus protection
# --------------------------------------------------------------------------- #

def test_delete_refuses_protected_record(proxy, fake_memory):
    # Seed a record that looks like an imported corpus entry (source_group != "user-write")
    fake_memory._store["corpus-1"] = {
        "id": "corpus-1",
        "memory": "Corpus record content",
        "metadata": {"source_group": "imported", "kind": "article"},
        "user_id": "my_lord",
    }
    result = run(proxy.handle_delete_tool({"id": "corpus-1"}, allow_user_id=True))
    assert result["isError"]
    assert result["structuredContent"]["error"] == "protected_record"


def test_update_refuses_protected_record(proxy, fake_memory):
    fake_memory._store["corpus-2"] = {
        "id": "corpus-2",
        "memory": "Another corpus record",
        "metadata": {"source_group": "imported", "kind": "article"},
        "user_id": "my_lord",
    }
    result = run(proxy.handle_update_tool({"id": "corpus-2", "text": "changed"}, allow_user_id=True))
    assert result["isError"]
    assert result["structuredContent"]["error"] == "protected_record"


# --------------------------------------------------------------------------- #
# 3. Image flow
# --------------------------------------------------------------------------- #

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def test_add_image_returns_ids(proxy):
    result = run(proxy.handle_add_image_tool({
        "caption": "A test PNG image",
        "image_base64": _PNG_B64,
    }))
    assert not result["isError"], result
    sc = result["structuredContent"]
    assert "blob_id" in sc
    assert "memory_id" in sc
    assert "url" in sc


def test_fetch_image_returns_image_block(proxy):
    add_result = run(proxy.handle_add_image_tool({
        "caption": "Fetchable image",
        "image_base64": _PNG_B64,
    }))
    assert not add_result["isError"]
    blob_id = add_result["structuredContent"]["blob_id"]
    fetch_result = run(proxy.handle_fetch_image_tool({"id": blob_id}))
    assert not fetch_result["isError"]
    content_types = [block["type"] for block in fetch_result["content"]]
    assert "image" in content_types


def test_search_finds_image_caption(proxy):
    run(proxy.handle_add_image_tool({
        "caption": "Searchable sunset photo",
        "image_base64": _PNG_B64,
    }))
    result = run(proxy.handle_search_tool({"query": "sunset photo"}))
    assert not result["isError"]
    texts = [r["text"] for r in result["structuredContent"]["results"]]
    assert any("sunset" in t.lower() for t in texts)


def test_delete_image_succeeds(proxy):
    add_result = run(proxy.handle_add_image_tool({
        "caption": "Deletable image",
        "image_base64": _PNG_B64,
    }))
    assert not add_result["isError"]
    sc = add_result["structuredContent"]
    memory_id = sc["memory_id"]
    blob_id = sc["blob_id"]
    del_result = run(proxy.handle_delete_image_tool({"memory_id": memory_id}, allow_user_id=True))
    assert not del_result["isError"]
    assert del_result["structuredContent"]["deleted"] is True
    # fetch_image should now be not_found
    fetch_result = run(proxy.handle_fetch_image_tool({"id": blob_id}))
    assert fetch_result["isError"]
    assert fetch_result["structuredContent"]["error"] == "not_found"


# --------------------------------------------------------------------------- #
# 4. Update cannot forge image metadata
# --------------------------------------------------------------------------- #

def test_update_cannot_forge_blob_ref(proxy):
    add_result = run(proxy.handle_add_image_tool({
        "caption": "Original image caption",
        "image_base64": _PNG_B64,
    }))
    assert not add_result["isError"]
    sc = add_result["structuredContent"]
    memory_id = sc["memory_id"]
    original_blob_ref = sc["blob_id"]

    # Try to overwrite system-managed keys via update
    update_result = run(proxy.handle_update_tool(
        {"id": memory_id, "text": "Updated caption", "metadata": {"blob_ref": "deadbeef", "kind": "note"}},
        allow_user_id=True,
    ))
    assert not update_result["isError"]

    # The stored record must still have the original blob_ref and kind == "image"
    rec = proxy.memory.get(memory_id)
    assert rec is not None
    metadata = rec["metadata"]
    assert metadata.get("blob_ref") == original_blob_ref, "blob_ref must not be overwritten"
    assert metadata.get("kind") == "image", "kind must not be overwritten"


# --------------------------------------------------------------------------- #
# 5. Hardening wiring: metrics, audit, rate limiters, tool category
# --------------------------------------------------------------------------- #

def test_proxy_has_metrics_audit_limiters(proxy):
    from metrics import Metrics
    from audit import AuditLog
    from ratelimit import RateLimiter
    assert isinstance(proxy.metrics, Metrics)
    assert isinstance(proxy.audit, AuditLog)
    assert isinstance(proxy.write_limiter, RateLimiter)
    assert isinstance(proxy.search_limiter, RateLimiter)


def test_tool_category_writes():
    from server import Mem0ChatProxy
    # Claude endpoint (reliquary_*) names
    for tool in ("reliquary_add_memory", "reliquary_delete", "reliquary_update",
                 "reliquary_add_image", "reliquary_delete_image",
                 "reliquary_create_image_upload", "reliquary_commit_image_upload",
                 "reliquary_compile_page", "reliquary_propose_update"):
        assert Mem0ChatProxy._tool_category(tool) == "write", f"expected write for {tool}"
    # OpenAI lean endpoint (bare) names — carve-out preserved
    for tool in ("add_memory", "delete", "update", "add_image", "delete_image",
                 "create_image_upload", "commit_image_upload", "propose_update"):
        assert Mem0ChatProxy._tool_category(tool) == "write", f"expected write for {tool}"


def test_tool_category_reads():
    from server import Mem0ChatProxy
    # Claude endpoint (reliquary_*) names
    for tool in ("reliquary_search", "reliquary_fetch", "reliquary_fetch_image",
                 "reliquary_list_domains", "reliquary_list_pages", "reliquary_page_history",
                 "reliquary_capabilities"):
        assert Mem0ChatProxy._tool_category(tool) == "read", f"expected read for {tool}"
    # OpenAI lean endpoint (bare) names — carve-out preserved
    for tool in ("search", "fetch", "fetch_image", "list_domains", "capabilities"):
        assert Mem0ChatProxy._tool_category(tool) == "read", f"expected read for {tool}"


def test_tool_category_other():
    from server import Mem0ChatProxy
    assert Mem0ChatProxy._tool_category("reliquary_status") == "other"
    assert Mem0ChatProxy._tool_category("unknown_tool") == "other"


# --------------------------------------------------------------------------- #
# 6. add_image source_url ingest
# --------------------------------------------------------------------------- #

_PNG_BYTES_URL = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_add_image_source_url_happy_path(proxy, monkeypatch):
    """source_url path: fetch stub returns PNG bytes; result has blob_id + memory_id."""
    async def fake_fetch(url):
        return (_PNG_BYTES_URL, "image/png")

    monkeypatch.setattr(proxy, "_fetch_image_from_url", fake_fetch)
    result = run(proxy.handle_add_image_tool({
        "caption": "URL-ingested image",
        "source_url": "https://example.com/cat.png",
    }))
    assert not result["isError"], result
    sc = result["structuredContent"]
    assert "blob_id" in sc
    assert "memory_id" in sc
    assert "url" in sc

    # Caption should be searchable
    search_result = run(proxy.handle_search_tool({"query": "URL-ingested image"}))
    assert not search_result["isError"]
    texts = [r["text"] for r in search_result["structuredContent"]["results"]]
    assert any("URL-ingested" in t for t in texts)


def test_add_image_both_sources_rejected(proxy):
    """Providing both image_base64 and source_url returns ambiguous_source error."""
    result = run(proxy.handle_add_image_tool({
        "caption": "ambiguous",
        "image_base64": _PNG_B64,
        "source_url": "https://example.com/x.png",
    }))
    assert result["isError"]
    assert result["structuredContent"]["error"] == "ambiguous_source"


def test_add_image_no_source_rejected(proxy):
    """Providing neither image_base64 nor source_url returns missing_image error."""
    result = run(proxy.handle_add_image_tool({"caption": "no source"}))
    assert result["isError"]
    assert result["structuredContent"]["error"] == "missing_image"


def test_add_image_url_ingest_disabled(proxy):
    """When image_url_ingest is False, source_url returns url_ingest_disabled error."""
    proxy.settings.image_url_ingest = False
    try:
        result = run(proxy.handle_add_image_tool({
            "caption": "disabled",
            "source_url": "https://example.com/x.png",
        }))
        assert result["isError"]
        assert result["structuredContent"]["error"] == "url_ingest_disabled"
    finally:
        proxy.settings.image_url_ingest = True


def test_exact_marker_ranks_first_across_routes(proxy, fake_memory):
    """An exact-token match from the global route must outrank domain-routed
    vector hits, even though domain routes are processed first (issue #33)."""
    import types

    from server import _GlobalRoute

    # Noise first so insertion order alone would NOT put the marker on top.
    for i in range(4):
        fake_memory.add(f"unrelated dev note about images {i}", user_id="my_lord",
                        metadata={"source_group": "imported", "domain": "dev"})
    marker = "RELIQUARY-EXACT-MARKER-ABC-20260605"
    fake_memory.add(f"{marker} persistent handoff note", user_id="my_lord",
                    metadata={"source_group": "user-write"})
    for i in range(4):
        fake_memory.add(f"more dev notes {i}", user_id="my_lord",
                        metadata={"source_group": "imported", "domain": "dev"})

    # Route the query to a domain first, then global (mirrors production routing).
    cat = types.SimpleNamespace()
    cat.routeable_domains = ["dev"]
    cat.records_by_id = {}
    cat.build_routes = lambda q: [
        types.SimpleNamespace(description="domain=dev", filters={"domain": "dev"}),
        _GlobalRoute(),
    ]
    proxy.catalog = cat

    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    res = run(proxy.call_mcp_tool(claude, "reliquary_search", {"query": marker, "limit": 5}, can_write=False))
    results = res["structuredContent"]["results"]
    assert results, "expected search results"
    top = results[0]
    assert marker in (top.get("text") or ""), f"exact marker not ranked #1: {top.get('text')!r}"
    assert top.get("score") and top["score"] >= 2.0


# ---------------------------------------------------------------------------
# Task 4a: capabilities.write_authorized
# ---------------------------------------------------------------------------

def _profile(proxy, name):
    if name == "claude":
        return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    return proxy.endpoint_profiles[proxy.settings.openai_mcp_path]


def test_capabilities_write_authorized_true_with_write_scope(proxy):
    """capabilities must include write_authorized=True when can_write=True."""
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "reliquary_capabilities", {}, can_write=True))
    assert not result.get("isError")
    assert result["structuredContent"]["write_authorized"] is True


def test_capabilities_write_authorized_false_with_read_only_scope(proxy):
    """capabilities must include write_authorized=False when can_write=False."""
    profile = _profile(proxy, "claude")
    result = run(proxy.call_mcp_tool(profile, "reliquary_capabilities", {}, can_write=False))
    assert not result.get("isError")
    assert result["structuredContent"]["write_authorized"] is False


# ---------------------------------------------------------------------------
# Task 4b: fetch_image inline base64 in structuredContent
# ---------------------------------------------------------------------------

def test_fetch_image_small_blob_includes_image_base64(proxy):
    """For a small blob, fetch_image structuredContent must include image_base64."""
    add_result = run(proxy.handle_add_image_tool({
        "caption": "Small inline image",
        "image_base64": _PNG_B64,
    }))
    assert not add_result["isError"], add_result
    blob_id = add_result["structuredContent"]["blob_id"]
    fetch_result = run(proxy.handle_fetch_image_tool({"id": blob_id}))
    assert not fetch_result["isError"], fetch_result
    sc = fetch_result["structuredContent"]
    assert "image_base64" in sc, "expected image_base64 in structuredContent for small blob"
    # Must decode back to the original bytes.
    decoded = base64.b64decode(sc["image_base64"])
    assert decoded == _PNG_BYTES, "image_base64 decodes to wrong bytes"


def test_fetch_image_large_blob_omits_image_base64(proxy, monkeypatch):
    """For a blob over INLINE_IMAGE_MAX_BYTES, image_base64 must be absent."""
    from server import INLINE_IMAGE_MAX_BYTES
    large_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (INLINE_IMAGE_MAX_BYTES + 1)
    large_b64 = base64.b64encode(large_data).decode("ascii")

    # Patch blob_max_bytes so the store accepts the large blob (auto-restored by monkeypatch).
    monkeypatch.setattr(proxy.settings, "blob_max_bytes", 0)  # 0 = disable cap
    add_result = run(proxy.handle_add_image_tool({
        "caption": "Large image",
        "image_base64": large_b64,
    }))
    assert not add_result["isError"], add_result
    blob_id = add_result["structuredContent"]["blob_id"]
    fetch_result = run(proxy.handle_fetch_image_tool({"id": blob_id}))
    assert not fetch_result["isError"], fetch_result
    sc = fetch_result["structuredContent"]
    assert "image_base64" not in sc, "image_base64 must not appear for large blobs"
    # URL must still be present.
    assert "url" in sc


# ---------------------------------------------------------------------------
# search: per-hit body cap (max_chars) + exact-match ranking
# ---------------------------------------------------------------------------


def test_search_max_chars_caps_preview(proxy):
    run(proxy.handle_add_memory_tool({"text": "x" * 5000}))
    sc = run(proxy.handle_search_tool({"query": "x", "max_chars": 200}))["structuredContent"]
    r = sc["results"][0]
    assert r["truncated"] is True
    assert len(r["text"]) <= 200           # preview_body caps at limit *including* the ellipsis
    assert r["char_count"] == 5000         # full length still reported


def test_exact_title_match_outranks_tied_neighbours(proxy, fake_memory):
    # FakeMemory scores every hit 1.0, so ordering is decided purely by the
    # exact-match bonus — exactly the "exact hit buried under ties" report.
    for i in range(4):
        fake_memory.add(f"some neighbour note {i}", user_id="my_lord",
                        metadata={"title": f"Neighbour {i}", "source_group": "user-write"})
    fake_memory.add("the canonical record", user_id="my_lord",
                    metadata={"title": "Vigil Today", "source_group": "user-write"})
    sc = run(proxy.handle_search_tool({"query": "Vigil Today"}))["structuredContent"]
    titles = [r["title"] for r in sc["results"]]
    # The exact-title hit must lead, and the same-score non-exact neighbours must
    # still be present (below it) — proving the bonus reordered ties, not filtered.
    assert titles[0] == "Vigil Today"
    assert any(t.startswith("Neighbour") for t in titles[1:])
