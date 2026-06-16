"""Shared test fixtures: an in-memory fake mem0 backend + a constructed proxy."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest  # noqa: E402

from server import Mem0ChatProxy, ProxySettings  # noqa: E402


class FakeMemory:
    """Minimal in-memory stand-in for mem0's Memory, matching the shapes the
    server's handlers expect (results lists, get/add/update/delete)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._counter = 0

    def search(self, query, *, user_id=None, limit=None, top_k=None, filters=None, threshold=None, **kw):
        uid = user_id or (filters or {}).get("user_id")
        # Build metadata filter: any of domain/hall/room/topic present in filters
        # (excluding user_id which is a top-level param, not a metadata field).
        _META_FILTER_KEYS = {"domain", "hall", "room", "topic"}
        meta_filter = {k: v for k, v in (filters or {}).items() if k in _META_FILTER_KEYS}
        hits = []
        for rec in self._store.values():
            if uid is not None and rec["user_id"] != uid:
                continue
            if meta_filter:
                rec_meta = rec.get("metadata") or {}
                if not all(rec_meta.get(k) == v for k, v in meta_filter.items()):
                    continue
            hits.append(dict(rec, score=1.0))
        cap = limit or top_k
        if cap:
            hits = hits[:cap]
        return {"results": hits}

    def get(self, memory_id):
        return self._store.get(memory_id)

    def get_all(self, user_id=None, **kw):
        recs = [r for r in self._store.values() if user_id is None or r["user_id"] == user_id]
        return {"results": recs}

    def add(self, text, *, user_id=None, metadata=None, infer=False, **kw):
        self._counter += 1
        mid = f"fake-{self._counter}"
        self._store[mid] = {"id": mid, "memory": text, "metadata": dict(metadata or {}), "user_id": user_id}
        return {"results": [{"id": mid, "event": "ADD"}]}

    def delete(self, memory_id):
        self._store.pop(memory_id, None)

    def update(self, memory_id, data, metadata=None):
        rec = self._store.get(memory_id)
        if rec is None:
            raise KeyError(memory_id)
        rec["memory"] = data
        if metadata is not None:
            rec["metadata"] = dict(metadata)
        return {"id": memory_id}


@pytest.fixture
def fake_memory():
    return FakeMemory()


@pytest.fixture
def proxy(tmp_path, fake_memory, monkeypatch):
    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    # The fake backend is pure in-memory test code; running it inline avoids
    # threadpool shutdown hangs under Python 3.14 in the sandboxed test runner.
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))
    settings = ProxySettings(
        config_path=str(cfg),
        user_id="my_lord",
        claude_token="claude-secret",
        openai_token="openai-secret",
        blob_dir=str(tmp_path / "blobs"),
        blob_signing_key="test-signing-key",
        compiled_dir=str(tmp_path / "compiled"),
    )
    return Mem0ChatProxy(settings, memory=fake_memory, compiled_memory=FakeMemory())


@pytest.fixture
def make_proxy(tmp_path, monkeypatch):
    """Factory for building proxies with custom settings (e.g. a shared
    state_dir/blob_dir across two instances to simulate a restart)."""

    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
    }))

    def _make(*, memory=None, **overrides):
        opts = dict(
            config_path=str(cfg),
            user_id="my_lord",
            claude_token="claude-secret",
            openai_token="openai-secret",
            blob_dir=str(tmp_path / "blobs"),
            blob_signing_key="test-signing-key",
            compiled_dir=str(tmp_path / "compiled"),
        )
        compiled_memory = overrides.pop("compiled_memory", None) or FakeMemory()
        opts.update(overrides)
        return Mem0ChatProxy(ProxySettings(**opts), memory=memory or FakeMemory(),
                             compiled_memory=compiled_memory)

    return _make
