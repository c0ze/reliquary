"""Regression cases from the November 2026 storage/state audit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"storage-audit"


def run(coro):
    return asyncio.run(coro)


def claude(proxy):
    return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]


async def call(proxy, tool, args):
    return await proxy.call_mcp_tool(claude(proxy), tool, args, can_write=True)


def sidecars(proxy):
    return sorted(p.stem for p in Path(proxy.blobs.blob_dir).rglob("*.json"))


async def post_bytes(proxy, upload_id, data=PNG):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://t") as client:
        return await client.post(f"/uploads/{upload_id}", content=data,
                                 headers={"content-type": "image/png", "authorization": "Bearer claude-secret"})


# 1 -- delete_page must not wedge once the page's vector is already gone -----

def test_delete_page_succeeds_when_vector_already_absent(proxy):
    async def scenario():
        filed = await call(proxy, "reliquary_compile_page", {"slug": "p", "markdown": "body"})
        memory_id = filed["structuredContent"]["memory_id"]
        # Collection rebuilt/restored, or a crash after an earlier vector delete.
        del proxy.compiled_memory._store[memory_id]
        return await call(proxy, "reliquary_delete_page", {"slug": "p"})

    result = run(scenario())
    assert result["isError"] is False, result
    assert result["structuredContent"]["deleted"] is True
    assert proxy.pages.get("p") is None


def test_delete_page_still_aborts_when_vector_survives_a_failed_delete(proxy, monkeypatch):
    async def scenario():
        await call(proxy, "reliquary_compile_page", {"slug": "p", "markdown": "body"})

        def boom(memory_id):
            raise ConnectionError("qdrant down")

        monkeypatch.setattr(proxy.compiled_memory, "delete", boom)
        return await call(proxy, "reliquary_delete_page", {"slug": "p"})

    result = run(scenario())
    assert result["structuredContent"]["error"] == "index_delete_failed"
    assert proxy.pages.get("p") is not None


# 2 -- an indexing failure must not leave a committed, unindexed revision ---

def _fail_compiled_index(proxy, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(proxy.compiled_memory, "add", boom)
    monkeypatch.setattr(proxy.compiled_memory, "update", boom)


def test_compile_index_failure_does_not_create_a_new_page(proxy, monkeypatch):
    _fail_compiled_index(proxy, monkeypatch)
    result = run(call(proxy, "reliquary_compile_page", {"slug": "newpage", "markdown": "body", "domain": "d"}))
    assert result["isError"] is True
    assert proxy.pages.get("newpage") is None
    assert result["structuredContent"]["error"] == "index_failed"
    # The revision blob's reference was released, not leaked.
    assert sidecars(proxy) == []


def test_compile_index_failure_restores_the_existing_page(proxy, monkeypatch):
    first = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "old text"}))
    before = proxy.pages.get("pg")
    _fail_compiled_index(proxy, monkeypatch)
    second = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "new text"}))
    assert proxy.pages.get("pg") == before
    assert "old text" in proxy.pages.read_body("pg")[0]
    assert second["structuredContent"]["error"] == "index_failed"
    assert "old text" in proxy.compiled_memory._store[first["structuredContent"]["memory_id"]]["memory"]
    assert sidecars(proxy) == [before.current_blob]  # the new revision's bytes were released
    assert proxy.blobs.info(before.current_blob).ref_count == 1


def test_needs_review_and_lint_report_current_page_without_index(proxy, tmp_path):
    proxy.pages.put_revision("broken", "body", {"status": "current"})  # e.g. filed before this fix
    payload = json.loads(proxy.read_resource("reliquary://needs-review")["contents"][0]["text"])
    assert [item["slug"] for item in payload["unindexed"]] == ["broken"]

    import lint
    report = lint.build_report(compiled_dir=proxy.settings.compiled_dir, blob_dir=proxy.settings.blob_dir,
                               dataset_path=None, min_count=8)
    assert [item["slug"] for item in report["unindexed_pages"]] == ["broken"]


# 5 -- a re-file without derived_from/title keeps them ----------------------

def test_refile_without_provenance_keeps_it_and_still_goes_stale(proxy):
    async def scenario():
        added = await call(proxy, "reliquary_add_memory", {"text": "source fact"})
        source = added["structuredContent"]["ids"][0]
        await call(proxy, "reliquary_compile_page", {"slug": "syn", "title": "Syn Title", "markdown": "v1",
                                                     "derived_from": [source], "supersedes": ["older"]})
        refiled = await call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v2"})
        page = proxy.pages.get("syn")
        await call(proxy, "reliquary_update", {"id": source, "text": "source fact changed"})
        return source, refiled, page, proxy.pages.get("syn")

    source, refiled, page, final = run(scenario())
    assert page.derived_from == [source]
    assert page.supersedes == ["older"]
    assert page.title == "Syn Title"
    assert refiled["structuredContent"]["derived_from"] == [source]
    indexed = proxy.compiled_memory._store[refiled["structuredContent"]["memory_id"]]
    assert indexed["metadata"]["derived_from"] == [source]
    assert indexed["memory"].startswith("Syn Title")
    assert final.status == "stale"


def test_refile_with_explicit_empty_lists_clears_provenance(proxy):
    run(call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v1",
                                               "derived_from": ["m1"], "supersedes": ["older"]}))
    run(call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v2",
                                               "derived_from": [], "supersedes": []}))
    page = proxy.pages.get("syn")
    assert page.derived_from == [] and page.supersedes == []
    assert page.title == "syn"  # a new page with no title still defaults to its slug
