from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from server import Mem0ChatProxy, ProxySettings, append_source_writeback  # noqa: E402


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

    append_source_writeback(
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
        ),
        memory=memory,
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
