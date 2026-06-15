from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import server  # noqa: E402
from server import Mem0ChatProxy, ProxySettings  # noqa: E402
from conftest import FakeMemory  # noqa: E402


class RecordingMemory:
    def __init__(self):
        self.added = []

    def search(self, query, *, user_id=None, limit=None, **kwargs):
        return {"results": []}

    def add(self, messages, *, user_id=None, metadata=None, **kwargs):
        self.added.append({"messages": messages, "user_id": user_id, "metadata": metadata})
        return {"results": [{"id": "mem-1", "event": "ADD"}]}


def test_append_source_writeback_creates_markdown_entry(tmp_path):
    path = tmp_path / "Agent Writeback.md"

    server.append_source_writeback(
        path,
        user_id="default",
        user_text="Remember the blue notebook.",
        assistant_text="Noted.",
        model="test-model",
    )

    text = path.read_text(encoding="utf-8")
    assert "## Agent turn" in text
    assert "- user_id: default" in text
    assert "- model: test-model" in text
    assert "### User" in text
    assert "Remember the blue notebook." in text
    assert "### Assistant" in text
    assert "Noted." in text


def test_writeback_turn_appends_source_note_when_configured(tmp_path, monkeypatch):
    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))
    output = tmp_path / "Writeback.md"
    memory = RecordingMemory()
    proxy = Mem0ChatProxy(
        ProxySettings(
            config_path=str(cfg),
            user_id="default",
            writeback=True,
            writeback_path=str(output),
            blob_dir=str(tmp_path / "blobs"),
            compiled_dir=str(tmp_path / "compiled"),
        ),
        memory=memory,
        compiled_memory=FakeMemory(),
    )

    asyncio.run(
        proxy.writeback_turn(
            user_id="default",
            user_text="Keep this.",
            assistant_text="Stored.",
            model="test-model",
        )
    )

    assert memory.added[0]["metadata"]["source_group"] == "user-write"
    assert output.read_text(encoding="utf-8").count("## Agent turn") == 1
    assert "Keep this." in output.read_text(encoding="utf-8")


def test_writeback_turn_swallows_source_note_errors(tmp_path, monkeypatch):
    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    def failing_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(server, "append_source_writeback", failing_append)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))
    proxy = Mem0ChatProxy(
        ProxySettings(
            config_path=str(cfg),
            user_id="default",
            writeback=True,
            writeback_path=str(tmp_path / "Writeback.md"),
            blob_dir=str(tmp_path / "blobs"),
            compiled_dir=str(tmp_path / "compiled"),
        ),
        memory=RecordingMemory(),
        compiled_memory=FakeMemory(),
    )

    asyncio.run(
        proxy.writeback_turn(
            user_id="default",
            user_text="Keep this.",
            assistant_text="Stored.",
            model="test-model",
        )
    )


# ---------------------------------------------------------------------------
# Chat-proxy contract: reliquary_* payload keys + x-reliquary-* headers + source tag
# ---------------------------------------------------------------------------

def _make_chat_proxy(tmp_path, monkeypatch):
    """Build a Mem0ChatProxy with writeback enabled and a fake upstream URL."""
    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))
    memory = RecordingMemory()
    proxy = Mem0ChatProxy(
        ProxySettings(
            config_path=str(cfg),
            user_id="default",
            upstream_base_url="http://fake-upstream",
            writeback=True,
            writeback_path=str(tmp_path / "Writeback.md"),
            blob_dir=str(tmp_path / "blobs"),
            compiled_dir=str(tmp_path / "compiled"),
        ),
        memory=memory,
        compiled_memory=FakeMemory(),
    )
    return proxy, memory


def _fake_upstream_response(content: dict) -> httpx.Response:
    """Return a minimal httpx.Response that looks like a successful upstream reply."""
    body = json.dumps(content).encode()
    return httpx.Response(
        status_code=200,
        headers={"content-type": "application/json"},
        content=body,
    )


def test_chat_proxy_uses_reliquary_payload_keys(tmp_path, monkeypatch):
    """POST /v1/chat/completions reads `reliquary_query` (not `mem0_query`) and
    emits x-reliquary-* response headers.  A stray `mem0_query` in the payload
    must be forwarded verbatim to the upstream (i.e. NOT consumed as a hint)."""

    async def scenario():
        proxy, _memory = _make_chat_proxy(tmp_path, monkeypatch)

        # Monkeypatch search_memories so no real Qdrant connection is needed.
        async def fake_search(query, *, user_id, limit, threshold, filters):
            return [{"id": "m1", "memory": "Blue notebook.", "score": 0.9}]

        monkeypatch.setattr(proxy, "search_memories", fake_search)

        # Fake the upstream LLM response so handle_chat_completions can complete.
        upstream_body = {
            "choices": [{"message": {"role": "assistant", "content": "OK."}, "finish_reason": "stop"}],
            "model": "gpt-mock",
        }
        proxy.client.request = AsyncMock(return_value=_fake_upstream_response(upstream_body))

        payload = {
            "model": "gpt-mock",
            "messages": [{"role": "user", "content": "What is the blue notebook?"}],
            "reliquary_query": "blue notebook",  # new key — must be consumed
            "mem0_query": "should be forwarded as-is",  # old key — must NOT be consumed
        }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # x-reliquary-hit-count must be present and reflect the one fake hit.
        assert resp.headers["x-reliquary-hit-count"] == "1"
        # x-reliquary-query must echo the query we supplied.
        assert resp.headers.get("x-reliquary-query") == "blue notebook"

        # Verify that the upstream received mem0_query forwarded intact (i.e. the
        # old key was NOT stripped by the proxy).
        call_content = json.loads(proxy.client.request.call_args.kwargs["content"])
        assert call_content.get("mem0_query") == "should be forwarded as-is"
        # And reliquary_query must have been consumed (not forwarded).
        assert "reliquary_query" not in call_content

    asyncio.run(scenario())


def test_chat_proxy_writeback_source_is_reliquary_chat_proxy(tmp_path, monkeypatch):
    """After a successful completion the writeback record's source tag must be
    'reliquary_chat_proxy' (not the old 'mem0_chat_proxy')."""

    async def scenario():
        proxy, memory = _make_chat_proxy(tmp_path, monkeypatch)

        async def fake_search(query, *, user_id, limit, threshold, filters):
            return []

        monkeypatch.setattr(proxy, "search_memories", fake_search)

        upstream_body = {
            "choices": [{"message": {"role": "assistant", "content": "Stored."}, "finish_reason": "stop"}],
            "model": "gpt-mock",
        }
        proxy.client.request = AsyncMock(return_value=_fake_upstream_response(upstream_body))

        payload = {
            "model": "gpt-mock",
            "messages": [{"role": "user", "content": "Keep this thought."}],
        }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                content=json.dumps(payload).encode(),
                headers={"content-type": "application/json"},
            )

        assert resp.status_code == 200
        # Writeback must have fired; inspect the source tag.
        assert len(memory.added) == 1
        assert memory.added[0]["metadata"]["source"] == "reliquary_chat_proxy"

    asyncio.run(scenario())
