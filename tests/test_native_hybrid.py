"""Tests for native mem0 BM25 hybrid detection and the lexical-fallback gate (Task A).

Covers:
- _should_run_lexical short-circuits to False when native hybrid is active, regardless
  of hit count / exact-id query.
- native_hybrid="off" preserves the pre-existing fallback behavior.
- The FakeMemory backend has no vector_store attribute, so auto-detection defaults to
  dense-only (no bm25 slot found) -> _native_hybrid is False by default.
- /status and reliquary_status both surface the search_mode key.
"""

from __future__ import annotations

import asyncio
import types

from conftest import FakeMemory


def run(coro):
    return asyncio.run(coro)


# --------------------------- _native_hybrid default detection ---------------------------


def test_fake_memory_has_no_vector_store_so_native_hybrid_defaults_false(proxy):
    # FakeMemory exposes no `vector_store` attribute, so auto-detection finds no bm25
    # slot and _native_hybrid should default to False.
    assert proxy._native_hybrid is False


# --------------------------- _should_run_lexical gating ---------------------------


def test_should_run_lexical_skips_natural_language_when_native_hybrid_active(proxy):
    proxy._native_hybrid = True
    # Natural-language (non exact-id) query: hybrid re-ranks it well, so skip the broad
    # get_all scan — regardless of whether the dense hits are thin or full.
    assert proxy._should_run_lexical("some query", hits=[], limit=5) is False
    assert proxy._should_run_lexical("some query", hits=[{}, {}, {}, {}, {}], limit=5) is False


def test_should_run_lexical_still_runs_for_exact_id_when_native_hybrid_active(proxy):
    proxy._native_hybrid = True
    # mem0's dense-anchored hybrid can drop a low-dense-rank exact token in a user-write,
    # so the fallback must STILL run for exact-identifier queries even under hybrid.
    assert proxy._should_run_lexical("get item abc-def-123", hits=[], limit=5) is True
    assert proxy._should_run_lexical("get item abc-def-123", hits=[{}, {}, {}, {}, {}], limit=5) is True


def test_should_run_lexical_unchanged_when_native_hybrid_inactive(proxy):
    proxy._native_hybrid = False
    # thin results -> still runs fallback
    assert proxy._should_run_lexical("some query", hits=[], limit=5) is True
    # full results + no exact id -> does not run fallback
    assert proxy._should_run_lexical("plain query", hits=[{}, {}, {}, {}, {}], limit=5) is False
    # full results + exact id token -> still runs fallback
    assert proxy._should_run_lexical("find abc-123-def", hits=[{}, {}, {}, {}, {}], limit=5) is True


def test_native_hybrid_off_preserves_fallback_behavior(make_proxy):
    proxy = make_proxy(native_hybrid="off")
    assert proxy._native_hybrid is False
    assert proxy._should_run_lexical("some query", hits=[], limit=5) is True
    assert proxy._should_run_lexical("plain query", hits=[{}, {}, {}, {}, {}], limit=5) is False


def test_native_hybrid_on_forces_hybrid_and_skips_fallback(make_proxy):
    proxy = make_proxy(native_hybrid="on")
    assert proxy._native_hybrid is True
    assert proxy._should_run_lexical("some query", hits=[], limit=5) is False


def test_native_hybrid_auto_true_when_backend_reports_bm25_slot_and_fastembed(make_proxy, monkeypatch):
    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(_has_bm25_slot=True)

    # native_hybrid_active() imports importlib.util locally inside __init__, so patching
    # the real stdlib module's find_spec is what actually takes effect.
    import importlib.util as real_importlib_util
    monkeypatch.setattr(real_importlib_util, "find_spec", lambda name: object() if name == "fastembed" else None)

    proxy = make_proxy(memory=memory, native_hybrid="auto")
    assert proxy._native_hybrid is True


def test_native_hybrid_auto_false_when_fastembed_missing_despite_slot(make_proxy, monkeypatch):
    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(_has_bm25_slot=True)

    import importlib.util as real_importlib_util
    monkeypatch.setattr(real_importlib_util, "find_spec", lambda name: None)

    proxy = make_proxy(memory=memory, native_hybrid="auto")
    assert proxy._native_hybrid is False


# --------------------------- ops visibility ---------------------------


def test_status_endpoint_includes_search_mode_dense(proxy):
    sent = []

    async def fake_send(message):
        sent.append(message)

    run(proxy.handle_status(fake_send))
    body = sent[1]["body"]
    import json
    payload = json.loads(body)
    assert payload["search_mode"] == "dense+lexical-fallback"


def test_status_endpoint_includes_search_mode_hybrid(proxy):
    proxy._native_hybrid = True
    sent = []

    async def fake_send(message):
        sent.append(message)

    run(proxy.handle_status(fake_send))
    body = sent[1]["body"]
    import json
    payload = json.loads(body)
    assert payload["search_mode"] == "hybrid"


def test_reliquary_status_tool_includes_search_mode_dense(proxy):
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(claude, "reliquary_status", {}, can_write=False))
    assert result["structuredContent"]["search_mode"] == "dense+lexical-fallback"


def test_reliquary_status_tool_includes_search_mode_hybrid(proxy):
    proxy._native_hybrid = True
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(claude, "reliquary_status", {}, can_write=False))
    assert result["structuredContent"]["search_mode"] == "hybrid"
