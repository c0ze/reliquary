"""Tests for native mem0 BM25 hybrid detection and the lexical-fallback gate (Task A).

Covers:
- _should_run_lexical short-circuits to False when native hybrid is active, regardless
  of hit count / exact-id query.
- native_hybrid="off" preserves the pre-existing fallback behavior.
- The FakeMemory backend has no vector_store attribute, so auto-detection defaults to
  dense-only (no bm25 slot found) -> _native_hybrid is False by default.
- /status and reliquary_status both surface the search_mode key.
- auto-mode detection probes mem0's own _get_bm25_encoder() rather than trusting mere
  fastembed importability, so a broken/incompatible fastembed install (encoder resolves
  to None or raises) is correctly reported as dense+lexical-fallback, not hybrid.
"""

from __future__ import annotations

import asyncio
import types

from conftest import FakeMemory

from server import _bm25_encoder_usable


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


def test_native_hybrid_auto_true_when_encoder_resolves(make_proxy):
    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(
        _has_bm25_slot=True, _get_bm25_encoder=lambda: object()
    )

    proxy = make_proxy(memory=memory, native_hybrid="auto")
    assert proxy._native_hybrid is True


def test_native_hybrid_auto_false_when_encoder_returns_none_despite_slot(make_proxy):
    # THE regression this fixes: a broken/incompatible fastembed install is importable
    # (find_spec would say yes) but mem0's own _get_bm25_encoder() resolves to None,
    # meaning keyword_search will silently no-op. auto mode must NOT report hybrid.
    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(
        _has_bm25_slot=True, _get_bm25_encoder=lambda: None
    )

    proxy = make_proxy(memory=memory, native_hybrid="auto")
    assert proxy._native_hybrid is False

    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(claude, "reliquary_status", {}, can_write=False))
    assert result["structuredContent"]["search_mode"] == "dense+lexical-fallback"


def test_native_hybrid_auto_false_when_encoder_getter_raises(make_proxy):
    def _boom():
        raise RuntimeError("native dep load failure")

    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(_has_bm25_slot=True, _get_bm25_encoder=_boom)

    proxy = make_proxy(memory=memory, native_hybrid="auto")
    assert proxy._native_hybrid is False


def test_native_hybrid_on_forces_hybrid_without_probing_encoder(make_proxy):
    # 'on' asserts hybrid without paying for the (model-loading) probe; a
    # _get_bm25_encoder that would raise/fail if invoked must not prevent hybrid,
    # and must not even be called.
    def _boom():
        raise AssertionError("_get_bm25_encoder should not be called in 'on' mode")

    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(_has_bm25_slot=True, _get_bm25_encoder=_boom)

    proxy = make_proxy(memory=memory, native_hybrid="on")
    assert proxy._native_hybrid is True


def test_native_hybrid_off_does_not_probe_encoder(make_proxy):
    def _boom():
        raise AssertionError("_get_bm25_encoder should not be called in 'off' mode")

    memory = FakeMemory()
    memory.vector_store = types.SimpleNamespace(_has_bm25_slot=True, _get_bm25_encoder=_boom)

    proxy = make_proxy(memory=memory, native_hybrid="off")
    assert proxy._native_hybrid is False


# --------------------------- _bm25_encoder_usable ---------------------------


def test_bm25_encoder_usable_false_when_no_getter():
    vector_store = types.SimpleNamespace()
    assert _bm25_encoder_usable(vector_store) is False


def test_bm25_encoder_usable_true_when_getter_returns_object():
    vector_store = types.SimpleNamespace(_get_bm25_encoder=lambda: object())
    assert _bm25_encoder_usable(vector_store) is True


def test_bm25_encoder_usable_false_when_getter_returns_none():
    vector_store = types.SimpleNamespace(_get_bm25_encoder=lambda: None)
    assert _bm25_encoder_usable(vector_store) is False


def test_bm25_encoder_usable_false_when_getter_raises():
    def _boom():
        raise RuntimeError("boom")

    vector_store = types.SimpleNamespace(_get_bm25_encoder=_boom)
    assert _bm25_encoder_usable(vector_store) is False


def test_bm25_encoder_usable_false_for_none_vector_store():
    assert _bm25_encoder_usable(None) is False


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
