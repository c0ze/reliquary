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
