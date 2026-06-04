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
    for tool in ("mem0_add_memory", "add_memory", "mem0_delete", "delete",
                 "mem0_update", "update", "add_image", "delete_image"):
        assert Mem0ChatProxy._tool_category(tool) == "write", f"expected write for {tool}"


def test_tool_category_reads():
    from server import Mem0ChatProxy
    for tool in ("mem0_search", "search", "mem0_fetch", "fetch", "fetch_image", "list_domains"):
        assert Mem0ChatProxy._tool_category(tool) == "read", f"expected read for {tool}"


def test_tool_category_other():
    from server import Mem0ChatProxy
    assert Mem0ChatProxy._tool_category("mem0_status") == "other"
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
