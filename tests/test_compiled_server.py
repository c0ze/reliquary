from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Phase 2 — layer construction (from earlier phases)
# ---------------------------------------------------------------------------


def test_proxy_builds_compiled_layer(proxy):
    assert proxy.pages is not None
    assert proxy.compiled_memory is not None


def test_compiled_layer_disabled_when_no_collection(make_proxy):
    p = make_proxy(compiled_collection="")
    assert p.pages is None
    assert p.compiled_memory is None


# ---------------------------------------------------------------------------
# Task 3.1 + 3.2 — handle_compile_page_tool and _index_compiled_page
# ---------------------------------------------------------------------------


def test_compile_page_round_trip(proxy):
    """Round-trip: structured has expected keys, page exists, body reads back."""
    result = run(proxy.handle_compile_page_tool({
        "markdown": "# Hello\n\nThis is a synthesis.",
        "slug": "hello-world",
        "title": "Hello World",
        "status": "current",
    }))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    assert sc["slug"] == "hello-world"
    assert "revision" in sc
    assert sc["memory_id"] is not None
    assert sc["url"].startswith("/blobs/")
    assert sc["status"] == "current"
    # Page must exist in registry
    assert proxy.pages.get("hello-world") is not None
    # Body must round-trip
    body_result = proxy.pages.read_body("hello-world")
    assert body_result is not None
    body, _ = body_result
    assert "Hello" in body


def test_compile_page_indexes_into_compiled_memory(proxy):
    """After filing, compiled_memory holds a record with kind=synthesis metadata."""
    run(proxy.handle_compile_page_tool({
        "markdown": "Some synthesis content",
        "slug": "my-synthesis",
        "title": "My Synthesis",
        "derived_from": ["mem-id-1", "mem-id-2"],
    }))
    # FakeMemory stores by id; look for any record matching slug
    store = proxy.compiled_memory._store
    found = [r for r in store.values()
             if r.get("metadata", {}).get("slug") == "my-synthesis"]
    assert found, "No compiled_memory record found for slug=my-synthesis"
    meta = found[0]["metadata"]
    assert meta["kind"] == "synthesis"
    assert meta["source_group"] == "compiled"
    assert "blob_ref" in meta
    assert meta["slug"] == "my-synthesis"


def test_compile_page_error_empty_markdown(proxy):
    result = run(proxy.handle_compile_page_tool({
        "markdown": "",
        "slug": "test-slug",
    }))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "missing_markdown"


def test_compile_page_error_missing_slug(proxy):
    result = run(proxy.handle_compile_page_tool({
        "markdown": "Some content",
    }))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "missing_slug"


def test_compile_page_error_disabled_layer(make_proxy):
    proxy = make_proxy(compiled_collection="")
    result = run(proxy.handle_compile_page_tool({
        "markdown": "content",
        "slug": "test",
    }))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "compiled_disabled"


def test_compile_page_slug_derived_from_title(proxy):
    """Slug derived from title when slug param is omitted."""
    result = run(proxy.handle_compile_page_tool({
        "markdown": "Content here",
        "title": "My Page Title",
    }))
    assert result.get("isError") is not True
    # slugify("My Page Title") → "my-page-title"
    assert result["structuredContent"]["slug"] == "my-page-title"


def test_compile_page_slug_derived_from_topic(proxy):
    """Slug derived from topic when slug+title are omitted."""
    result = run(proxy.handle_compile_page_tool({
        "markdown": "Content here",
        "topic": "Audio Mixing",
    }))
    assert result.get("isError") is not True
    assert result["structuredContent"]["slug"] == "audio-mixing"


def test_compile_page_revisioning(proxy):
    """Re-filing the same slug adds a revision."""
    run(proxy.handle_compile_page_tool({
        "markdown": "Version 1",
        "slug": "versioned-page",
        "title": "V1",
    }))
    run(proxy.handle_compile_page_tool({
        "markdown": "Version 2",
        "slug": "versioned-page",
        "title": "V2",
    }))
    info = proxy.pages.get("versioned-page")
    assert info is not None
    assert len(info.history) == 1  # one old blob in history


def test_compile_page_derived_from_in_result(proxy):
    """derived_from is echoed in the structured result."""
    result = run(proxy.handle_compile_page_tool({
        "markdown": "Derived content",
        "slug": "derived-page",
        "derived_from": ["id-a", "id-b"],
    }))
    assert result["structuredContent"]["derived_from"] == ["id-a", "id-b"]


# ---------------------------------------------------------------------------
# Task 3.3 — handle_list_pages_tool + handle_page_history_tool
# ---------------------------------------------------------------------------


def _file_page(proxy, slug, markdown="Content", domain=None, status="current"):
    args = {"markdown": markdown, "slug": slug}
    if domain:
        args["domain"] = domain
    if status != "current":
        args["status"] = status
    return run(proxy.handle_compile_page_tool(args))


def test_list_pages_empty(proxy):
    result = run(proxy.handle_list_pages_tool({}))
    assert result.get("isError") is not True
    assert result["structuredContent"]["pages"] == []


def test_list_pages_returns_all(proxy):
    _file_page(proxy, "page-one")
    _file_page(proxy, "page-two")
    result = run(proxy.handle_list_pages_tool({}))
    slugs = {p["slug"] for p in result["structuredContent"]["pages"]}
    assert "page-one" in slugs
    assert "page-two" in slugs


def test_list_pages_filter_by_domain(proxy):
    _file_page(proxy, "pagan-page", domain="pagan")
    _file_page(proxy, "other-page", domain="other")
    result = run(proxy.handle_list_pages_tool({"domain": "pagan"}))
    slugs = {p["slug"] for p in result["structuredContent"]["pages"]}
    assert "pagan-page" in slugs
    assert "other-page" not in slugs


def test_list_pages_filter_by_status(proxy):
    _file_page(proxy, "current-page", status="current")
    _file_page(proxy, "stale-page", status="stale")
    result = run(proxy.handle_list_pages_tool({"status": "current"}))
    slugs = {p["slug"] for p in result["structuredContent"]["pages"]}
    assert "current-page" in slugs
    assert "stale-page" not in slugs


def test_list_pages_summary_shape(proxy):
    _file_page(proxy, "shape-page")
    result = run(proxy.handle_list_pages_tool({}))
    page = result["structuredContent"]["pages"][0]
    for key in ("slug", "title", "domain", "status", "updated_at", "revision"):
        assert key in page, f"Missing key {key!r} in page summary"


def test_list_pages_disabled_layer(make_proxy):
    proxy = make_proxy(compiled_collection="")
    result = run(proxy.handle_list_pages_tool({}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "compiled_disabled"


def test_page_history_single_revision(proxy):
    _file_page(proxy, "history-page")
    result = run(proxy.handle_page_history_tool({"slug": "history-page"}))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    assert sc["slug"] == "history-page"
    assert sc["current"] is not None
    assert len(sc["revisions"]) == 1


def test_page_history_multiple_revisions(proxy):
    _file_page(proxy, "multi-rev", markdown="V1")
    _file_page(proxy, "multi-rev", markdown="V2")
    result = run(proxy.handle_page_history_tool({"slug": "multi-rev"}))
    sc = result["structuredContent"]
    assert len(sc["revisions"]) == 2
    assert sc["revisions"][0] == sc["current"]  # newest-first ordering


def test_page_history_not_found(proxy):
    result = run(proxy.handle_page_history_tool({"slug": "no-such-slug"}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "not_found"


def test_page_history_missing_slug(proxy):
    result = run(proxy.handle_page_history_tool({}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "missing_slug"


def test_page_history_disabled_layer(make_proxy):
    proxy = make_proxy(compiled_collection="")
    result = run(proxy.handle_page_history_tool({"slug": "any"}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "compiled_disabled"


# ---------------------------------------------------------------------------
# Task 3.4 — call_mcp_tool dispatch + tool list
# ---------------------------------------------------------------------------


def test_compile_page_via_call_mcp_tool_with_write(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(
        profile, "mem0_compile_page",
        {"markdown": "Hello synthesis", "slug": "dispatch-test"},
        can_write=True,
    ))
    assert result.get("isError") is not True
    assert result["structuredContent"]["slug"] == "dispatch-test"


def test_compile_page_via_call_mcp_tool_without_write(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(
        profile, "mem0_compile_page",
        {"markdown": "Hello synthesis", "slug": "dispatch-test"},
        can_write=False,
    ))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "insufficient_scope"


def test_list_pages_via_call_mcp_tool(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(
        profile, "mem0_list_pages", {}, can_write=False,
    ))
    assert result.get("isError") is not True
    assert "pages" in result["structuredContent"]


def test_page_history_via_call_mcp_tool(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    # File a page first
    run(proxy.call_mcp_tool(
        profile, "mem0_compile_page",
        {"markdown": "Content", "slug": "mcp-history-test"},
        can_write=True,
    ))
    result = run(proxy.call_mcp_tool(
        profile, "mem0_page_history",
        {"slug": "mcp-history-test"},
        can_write=False,
    ))
    assert result.get("isError") is not True
    assert result["structuredContent"]["slug"] == "mcp-history-test"


def test_mcp_tools_for_claude_read_includes_list_and_history(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = proxy.mcp_tools_for(profile, can_write=False)
    names = {t["name"] for t in tools}
    assert "mem0_list_pages" in names
    assert "mem0_page_history" in names
    # compile_page must NOT appear in read-only list
    assert "mem0_compile_page" not in names


def test_mcp_tools_for_claude_write_includes_compile_page(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = proxy.mcp_tools_for(profile, can_write=True)
    names = {t["name"] for t in tools}
    assert "mem0_compile_page" in names
    assert "mem0_list_pages" in names
    assert "mem0_page_history" in names


def test_new_tools_not_on_openai_endpoint(proxy):
    """New compiled tools must NOT appear on the OpenAI endpoint."""
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    for can_write in (False, True):
        tools = proxy.mcp_tools_for(profile, can_write=can_write)
        names = {t["name"] for t in tools}
        assert "mem0_compile_page" not in names
        assert "mem0_list_pages" not in names
        assert "mem0_page_history" not in names


# ---------------------------------------------------------------------------
# Task 3.5 — compiled-aware mem0_fetch
# ---------------------------------------------------------------------------


def test_mem0_fetch_compiled_page(proxy):
    """mem0_fetch resolves a compiled page by slug."""
    run(proxy.handle_compile_page_tool({
        "markdown": "Synthesis body here",
        "slug": "fetch-me",
        "title": "Fetch Me",
    }))
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(
        profile, "mem0_fetch", {"id": "fetch-me"}, can_write=False,
    ))
    assert result.get("isError") is not True
    sc = result["structuredContent"]
    assert sc["id"] == "fetch-me"
    assert sc["kind"] == "synthesis"
    assert "Synthesis body here" in sc["text"]
    assert sc["url"].startswith("/blobs/")


def test_mem0_fetch_unknown_id_still_returns_not_found(proxy):
    """Unknown ids still get the usual not_found response."""
    result = run(proxy.handle_fetch_tool({"id": "totally-unknown-xyz"}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "not_found"


def test_mem0_fetch_page_with_missing_blob_errors(proxy):
    """A registered page whose blob bytes are gone surfaces an error, not an empty body."""
    _file_page(proxy, "ghost")
    info = proxy.pages.get("ghost")
    proxy.blobs.delete(info.current_blob)  # simulate disk inconsistency
    result = run(proxy.handle_fetch_tool({"id": "ghost"}))
    assert result.get("isError") is True
    assert result["structuredContent"]["error"] == "blob_missing"


# ---------------------------------------------------------------------------
# Phase 4 (#50) — synthesis-first retrieval
# ---------------------------------------------------------------------------


def _claude_search(proxy, query, limit=5):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    return run(proxy.call_mcp_tool(profile, "mem0_search", {"query": query, "limit": limit}, can_write=False))


def test_current_synthesis_leads_results(proxy):
    # raw memory + a current synthesis page both match
    run(proxy.handle_add_memory_tool({"text": "Brigid is a forge goddess"}))
    _file_page(proxy, "brigid", markdown="Brigid: forge, poetry, healing.")
    sc = _claude_search(proxy, "brigid")["structuredContent"]
    assert sc["results"], "expected results"
    first = sc["results"][0]
    assert first["id"] == "brigid"
    assert (first.get("metadata") or {}).get("kind") == "synthesis"


def test_stale_synthesis_does_not_lead(proxy):
    run(proxy.handle_add_memory_tool({"text": "Brigid is a forge goddess"}))
    _file_page(proxy, "brigid", markdown="Brigid notes")
    proxy.pages.set_status("brigid", "stale")
    sc = _claude_search(proxy, "brigid")["structuredContent"]
    kinds = [(r.get("metadata") or {}).get("kind") for r in sc["results"]]
    assert "synthesis" not in kinds  # stale page must not lead


def test_search_unaffected_when_compiled_disabled(make_proxy):
    proxy = make_proxy(compiled_collection="")
    run(proxy.handle_add_memory_tool({"text": "plain raw memory about brigid"}))
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    sc = run(proxy.call_mcp_tool(profile, "mem0_search", {"query": "brigid"}, can_write=False))["structuredContent"]
    kinds = [(r.get("metadata") or {}).get("kind") for r in sc["results"]]
    assert "synthesis" not in kinds  # no compiled layer => no synthesis hits, no crash
