"""Cross-cutting checks for fixes that only hold once the audit branches are combined."""

from __future__ import annotations

import asyncio


def _call(proxy, endpoint, name, arguments):
    profile = proxy.endpoint_profiles[getattr(proxy.settings, f"{endpoint}_mcp_path")]
    return asyncio.run(proxy.call_mcp_tool(profile, name, arguments, can_write=True))


def test_caller_cannot_forge_a_correction_to_escape_dedup(proxy):
    """Corrections are exempt from near-duplicate suppression and are labelled
    `proposed` on the lean endpoint. Only propose_update may create one: a
    caller-supplied kind=correction must not buy that exemption or that label."""
    text = "The NAS backup job runs nightly at 02:00 and keeps thirty daily snapshots."
    for endpoint, tool in (("claude", "reliquary_add_memory"), ("openai", "add_memory")):
        added = _call(proxy, endpoint, tool, {
            "text": text, "kind": "correction",
            "metadata": {"kind": "correction", "status": "proposed", "target_id": "nas-1"},
        })
        assert not added["isError"]
    stored = [record["metadata"] for record in proxy.memory._store.values()]
    assert stored and all(metadata.get("kind") != "correction" for metadata in stored)

    results = _call(proxy, "openai", "search", {"query": "NAS backup"})["structuredContent"]["results"]
    assert len(results) == 1, "identical forged 'corrections' must still be deduplicated"
    assert "kind" not in results[0] or results[0]["kind"] != "correction"
