"""Integration tests: retrieval-stats wiring (search/fetch event recording +
needs-review resource surfacing cold_records/hot_topics)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from retrieval_stats import aggregate  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _file_page(proxy, slug, markdown="Content", domain=None, status="current"):
    args = {"markdown": markdown, "slug": slug}
    if domain:
        args["domain"] = domain
    if status != "current":
        args["status"] = status
    return run(proxy.handle_compile_page_tool(args))


# --------------------------------------------------------------------------- #
# search recording
# --------------------------------------------------------------------------- #


def test_search_records_raw_hit_ids(make_proxy, tmp_path):
    stats_path = tmp_path / "stats.jsonl"
    proxy = make_proxy(retrieval_stats_path=str(stats_path))
    add_result = run(proxy.handle_add_memory_tool({"text": "Bob enjoys hiking"}))
    mid = add_result["structuredContent"]["ids"][0]

    result = run(proxy.handle_search_tool({"query": "Bob"}))
    assert not result["isError"]

    assert stats_path.exists()
    agg = aggregate(str(stats_path))
    assert mid in agg["by_id"]
    assert agg["events"] >= 1


def test_search_does_not_record_synthesis_results(make_proxy, tmp_path):
    stats_path = tmp_path / "stats.jsonl"
    proxy = make_proxy(retrieval_stats_path=str(stats_path))
    # Both a synthesis page AND a raw memory surface for the same query, so the
    # filter must be exact: it records the raw id but drops the synthesis slug.
    # (A filter that dropped everything would still pass without the raw hit.)
    _file_page(proxy, "hiking-guide", markdown="Hiking guide content", domain="outdoors")
    add_result = run(proxy.handle_add_memory_tool({"text": "Hiking guide content for beginners"}))
    raw_id = add_result["structuredContent"]["ids"][0]

    result = run(proxy.handle_search_tool({"query": "Hiking guide content"}))
    assert not result["isError"]
    # Sanity: the search surfaced BOTH the synthesis page and the raw memory.
    ids = [r["id"] for r in result["structuredContent"]["results"]]
    assert "hiking-guide" in ids
    assert raw_id in ids

    # With a raw hit present the stats file must exist.
    assert stats_path.exists()
    agg = aggregate(str(stats_path))
    assert raw_id in agg["by_id"]
    assert "hiking-guide" not in agg["by_id"]


# --------------------------------------------------------------------------- #
# fetch recording
# --------------------------------------------------------------------------- #


def test_fetch_records_event_for_resolvable_id(make_proxy, tmp_path):
    stats_path = tmp_path / "stats.jsonl"
    proxy = make_proxy(retrieval_stats_path=str(stats_path))
    add_result = run(proxy.handle_add_memory_tool({"text": "Carol plays chess"}))
    mid = add_result["structuredContent"]["ids"][0]

    fetch_result = run(proxy.handle_fetch_tool({"id": mid}))
    assert not fetch_result["isError"]

    agg = aggregate(str(stats_path))
    assert mid in agg["by_id"]
    assert agg["by_id"][mid]["count"] == 1


def test_fetch_does_not_record_on_not_found(make_proxy, tmp_path):
    stats_path = tmp_path / "stats.jsonl"
    proxy = make_proxy(retrieval_stats_path=str(stats_path))
    fetch_result = run(proxy.handle_fetch_tool({"id": "does-not-exist"}))
    assert fetch_result["isError"]
    assert not stats_path.exists()


# --------------------------------------------------------------------------- #
# disabled (no path configured)
# --------------------------------------------------------------------------- #


def test_search_and_fetch_work_with_stats_disabled(make_proxy, tmp_path):
    proxy = make_proxy()  # retrieval_stats_path defaults to None
    add_result = run(proxy.handle_add_memory_tool({"text": "Dave reads novels"}))
    mid = add_result["structuredContent"]["ids"][0]

    search_result = run(proxy.handle_search_tool({"query": "Dave"}))
    assert not search_result["isError"]
    fetch_result = run(proxy.handle_fetch_tool({"id": mid}))
    assert not fetch_result["isError"]

    # No stats file should have been created anywhere under tmp_path.
    assert not list(tmp_path.rglob("*.jsonl"))


# --------------------------------------------------------------------------- #
# needs-review resource surfacing
# --------------------------------------------------------------------------- #


def test_needs_review_includes_cold_records_and_hot_topics_keys(make_proxy):
    proxy = make_proxy()  # stats disabled
    _file_page(proxy, "fresh")
    res = proxy.read_resource("reliquary://needs-review")
    payload = json.loads(res["contents"][0]["text"])
    assert isinstance(payload["cold_records"], list)
    assert isinstance(payload["hot_topics"], list)
    # Disabled stats -> aggregate() returns the empty shape -> both proposal
    # types are suppressed.
    assert payload["cold_records"] == []
    assert payload["hot_topics"] == []


def test_needs_review_cold_records_empty_below_event_floor(make_proxy, tmp_path):
    stats_path = tmp_path / "stats.jsonl"
    proxy = make_proxy(retrieval_stats_path=str(stats_path))
    _file_page(proxy, "fresh")
    add_result = run(proxy.handle_add_memory_tool({"text": "Eve writes code"}))
    mid = add_result["structuredContent"]["ids"][0]
    run(proxy.handle_fetch_tool({"id": mid}))  # records 1 event, far below the 200-event floor

    res = proxy.read_resource("reliquary://needs-review")
    payload = json.loads(res["contents"][0]["text"])
    assert payload["cold_records"] == []


def _write_dataset(path, records):
    # records: list of (id, domain) tuples -> one raw catalog record per line.
    with open(path, "w", encoding="utf-8") as fh:
        for rid, domain in records:
            fh.write(json.dumps({"id": rid, "text": "t",
                                 "metadata": {"domain": domain, "title": "x"}}) + "\n")


def _write_stats_jsonl(path, events):
    # events: list of (id, domain, topic) tuples; one JSONL line per event.
    with open(path, "w", encoding="utf-8") as fh:
        for i, (item_id, domain, topic) in enumerate(events):
            entry = {"ts": float(i), "event": "search", "id": item_id, "domain": domain, "topic": topic}
            fh.write(json.dumps(entry) + "\n")


def test_needs_review_honors_lint_cold_min_events_setting(make_proxy, tmp_path):
    # RELIQUARY_LINT_COLD_MIN_EVENTS must reach the live needs-review resource
    # (not just the offline lint CLI) via settings.lint_cold_min_events.
    dataset = tmp_path / "corpus.jsonl"
    _write_dataset(dataset, [("r1", "pagan"), ("r2", "pagan")])

    stats_path = tmp_path / "stats.jsonl"
    # Only 3 events -- far below the default 200-event floor -- but r2 is never
    # retrieved, so with a low configured floor it should surface as cold.
    _write_stats_jsonl(stats_path, [("r1", "pagan", "rituals")] * 3)

    proxy = make_proxy(dataset_path=str(dataset), retrieval_stats_path=str(stats_path),
                        lint_cold_min_events=1)
    assert proxy.settings.lint_cold_min_events == 1

    res = proxy.read_resource("reliquary://needs-review")
    payload = json.loads(res["contents"][0]["text"])
    cold_ids = {item["id"] for item in payload["cold_records"]}
    assert cold_ids == {"r2"}

    # Same sparse stats but with the default 200-event floor: cold_records must
    # stay empty, proving the low floor above genuinely came from the setting.
    default_proxy = make_proxy(dataset_path=str(dataset), retrieval_stats_path=str(stats_path))
    res_default = default_proxy.read_resource("reliquary://needs-review")
    payload_default = json.loads(res_default["contents"][0]["text"])
    assert payload_default["cold_records"] == []
