"""Tests for query-time retrieval quality heuristics."""

from __future__ import annotations

import asyncio

from retrieval_quality import apply_retrieval_quality


def run(coro):
    return asyncio.run(coro)


def test_quality_pass_deduplicates_near_identical_cross_source_hits():
    hits = [
        {
            "id": "obsidian-old",
            "memory": "Alice prefers jasmine tea with a little honey.",
            "metadata": {"source": "obsidian", "updated_at": "2024-01-01T00:00:00Z"},
            "score": 0.91,
        },
        {
            "id": "chat-new",
            "memory": "Alice prefers jasmine tea with a little honey!",
            "metadata": {"source": "chat", "updated_at": "2026-05-01T00:00:00Z"},
            "score": 0.91,
        },
        {
            "id": "distinct",
            "memory": "Alice also keeps sencha in the kitchen cabinet.",
            "metadata": {"source": "summary", "updated_at": "2026-04-01T00:00:00Z"},
            "score": 0.88,
        },
    ]

    result = apply_retrieval_quality(
        "Alice jasmine tea",
        hits,
        limit=3,
        now=1_780_000_000,
    )

    assert [hit["id"] for hit in result] == ["chat-new", "distinct"]


def test_quality_pass_uses_recency_as_a_tiebreaker():
    hits = [
        {
            "id": "old",
            "memory": "The deploy token lives in the shared password manager.",
            "metadata": {"updated_at": "2024-01-01T00:00:00Z"},
            "score": 0.8,
        },
        {
            "id": "fresh",
            "memory": "The deploy token lives in the shared password manager.",
            "metadata": {"updated_at": "2026-05-15T00:00:00Z"},
            "score": 0.8,
        },
    ]

    result = apply_retrieval_quality("deploy token", hits, limit=2, now=1_780_000_000)

    assert [hit["id"] for hit in result] == ["fresh"]


def test_quality_pass_reranks_lexically_stronger_hits_without_overriding_large_score_gap():
    hits = [
        {"id": "broad", "memory": "Bob likes being outside.", "metadata": {}, "score": 0.84},
        {
            "id": "specific",
            "memory": "Bob schedules hiking trips near Kamakura.",
            "metadata": {},
            "score": 0.8,
        },
        {
            "id": "much-better-vector",
            "memory": "Bob recently bought trail shoes.",
            "metadata": {},
            "score": 0.95,
        },
    ]

    result = apply_retrieval_quality("Bob hiking Kamakura", hits, limit=3, now=1_780_000_000)

    assert [hit["id"] for hit in result] == ["much-better-vector", "specific", "broad"]


def test_search_memories_overfetches_before_quality_limit(proxy, monkeypatch):
    captured_limits = []
    hits = [
        {
            "id": "dup-old",
            "memory": "Dana wants the Kyoto notes summarized before Friday.",
            "metadata": {"updated_at": "2024-01-01T00:00:00Z"},
            "score": 0.91,
        },
        {
            "id": "dup-new",
            "memory": "Dana wants the Kyoto notes summarized before Friday!",
            "metadata": {"updated_at": "2026-05-01T00:00:00Z"},
            "score": 0.91,
        },
        {
            "id": "distinct",
            "memory": "Dana also asked for the hotel shortlist.",
            "metadata": {"updated_at": "2026-05-01T00:00:00Z"},
            "score": 0.86,
        },
    ]

    def fake_search(query, **kwargs):
        captured_limits.append(kwargs.get("limit") or kwargs.get("top_k"))
        return {"results": hits[: captured_limits[-1]]}

    monkeypatch.setattr(proxy.memory, "search", fake_search)

    result = run(
        proxy.search_memories(
            "Dana Kyoto notes",
            user_id="my_lord",
            limit=2,
            threshold=None,
            filters=None,
        )
    )

    assert captured_limits[0] > 2
    assert [hit["id"] for hit in result] == ["dup-new", "distinct"]
