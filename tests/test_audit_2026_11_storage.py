"""Regression cases from the November 2026 storage/state audit."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"storage-audit"


def run(coro):
    return asyncio.run(coro)


def claude(proxy):
    return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]


async def call(proxy, tool, args):
    return await proxy.call_mcp_tool(claude(proxy), tool, args, can_write=True)


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
