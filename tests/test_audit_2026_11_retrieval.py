"""Regression tests for the 2026-11 retrieval audit findings."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from catalog import CorpusCatalog, CorpusRecord


def call(proxy, name, arguments, *, endpoint="openai", can_write=True):
    profile = proxy.endpoint_profiles[getattr(proxy.settings, f"{endpoint}_mcp_path")]
    return asyncio.run(proxy.call_mcp_tool(profile, name, arguments, can_write=can_write))


def output_schemas(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    return {t["name"]: t.get("outputSchema") for t in proxy.mcp_tools_for(profile)}


_PY_TYPES = {
    "object": dict, "array": list, "string": str, "null": type(None),
    "number": (int, float), "integer": int, "boolean": bool,
}


def schema_errors(schema, value, path="$"):
    """The JSON Schema subset the advertised output schemas use."""
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else ([declared] if declared else [])
    if types and not any(isinstance(value, _PY_TYPES[t]) and not (t in ("number", "integer") and isinstance(value, bool))
                         for t in types):
        return [f"{path}: expected {declared}, got {type(value).__name__}"]
    errors = []
    if isinstance(value, dict):
        errors += [f"{path}: missing {key!r}" for key in schema.get("required", []) if key not in value]
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors += schema_errors(sub, value[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{path}[{index}]")
    return errors


# ---------------------------------------------------------------------------
# 1. OpenAI search/fetch error results vs the advertised outputSchema
# ---------------------------------------------------------------------------

@pytest.fixture(params=["raw", "imported", "compiled"])
def document(proxy, request):
    title = "Schema probe"
    body = "A document that the schema check retrieves: 日本語 and Türkçe."
    metadata = {"title": title}
    if request.param == "compiled":
        result = asyncio.run(proxy.handle_compile_page_tool({
            "slug": "schema-probe", "title": title, "markdown": body,
        }))
        assert not result["isError"]
        return result["structuredContent"]["slug"]
    if request.param == "imported":
        metadata["import_record_id"] = "imported-schema-probe"
        proxy.catalog = CorpusCatalog([CorpusRecord(
            import_record_id="imported-schema-probe", title=title, text=body,
            metadata=dict(metadata), source_ref="",
        )])
    added = proxy.memory.add(body, user_id=proxy.settings.user_id, metadata=metadata)
    return metadata.get("import_record_id") or added["results"][0]["id"]


@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_openai_success_payloads_match_advertised_output_schema(proxy, document, tool):
    schemas = output_schemas(proxy)
    arguments = {"query": "schema probe"} if tool == "search" else {"id": document}
    result = call(proxy, tool, arguments)
    assert result["isError"] is False
    assert schema_errors(schemas[tool], result["structuredContent"]) == []
    if tool == "search":
        assert document in [item["id"] for item in result["structuredContent"]["results"]]


@pytest.mark.parametrize("tool,arguments,error", [
    ("search", {"query": ""}, "missing_query"),
    ("search", {"query": "x", "cursor": "999"}, "invalid_cursor"),
    ("fetch", {"id": ""}, "missing_id"),
    ("fetch", {"id": "nope"}, "not_found"),
])
def test_openai_error_results_carry_no_schema_violating_structured_content(proxy, tool, arguments, error):
    result = call(proxy, tool, arguments)
    assert result["isError"] is True
    # A validating client checks any structuredContent against outputSchema;
    # error payloads cannot satisfy it, so they must not be sent there.
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["error"] == error


def test_openai_rate_limited_retrieval_has_no_structured_content_over_http(proxy, monkeypatch):
    monkeypatch.setattr(proxy.search_limiter, "allow", lambda key: False)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="https://example.org",
            headers={"authorization": "Bearer openai-secret"},
        ) as client:
            response = await client.post("/openai/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "fetch", "arguments": {"id": "x"}},
            })
            return response.json()["result"]

    result = asyncio.run(request())
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["error"] == "rate_limited"


def test_claude_error_results_keep_structured_content(proxy):
    result = call(proxy, "reliquary_fetch", {"id": "nope"}, endpoint="claude")
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "not_found"


# ---------------------------------------------------------------------------
# 2. Unexpected tool exceptions must not echo raw exception text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint,tool", [("claude", "reliquary_search"), ("openai", "search")])
def test_tool_exception_hides_detail_and_logs_it_under_an_error_id(proxy, monkeypatch, caplog, endpoint, tool):
    secret = "https://admin:sk-live-123@qdrant.internal:6333"

    async def fail(*args, **kwargs):
        raise ConnectionError(f"cannot reach {secret}")

    monkeypatch.setattr(proxy, "handle_search_tool", fail)
    with caplog.at_level(logging.ERROR, logger="reliquary"):
        result = call(proxy, tool, {"query": "anything"}, endpoint=endpoint)
    assert result["isError"] is True
    serialized = json.dumps(result)
    assert secret not in serialized and "sk-live" not in serialized
    payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert payload["error"] == "internal_error"
    error_id = payload["error_id"]
    assert error_id in result["content"][0]["text"]
    logged = "\n".join(record.getMessage() + (record.exc_text or "") for record in caplog.records)
    assert error_id in logged
    assert any(record.exc_info and secret in str(record.exc_info[1]) for record in caplog.records)


def test_deliberate_validation_errors_keep_their_messages(proxy):
    result = call(proxy, "reliquary_update", {"id": "x", "text": "t", "metadata": "nope"}, endpoint="claude")
    assert result["structuredContent"] == {"error": "invalid_metadata"}
    assert result["content"][0]["text"] == "`metadata` must be an object."


# ---------------------------------------------------------------------------
# 3. Near-duplicate filter must not collapse distinct non-Latin memories
# ---------------------------------------------------------------------------

def _ids(hits):
    return [hit["id"] for hit in hits]


def test_dedup_keeps_distinct_japanese_notes_sharing_one_latin_word():
    from retrieval_quality import apply_retrieval_quality

    hits = [
        {"id": "a", "memory": "東京で iPhone を買った。色は黒。", "score": 0.80},
        {"id": "b", "memory": "iPhone の画面が割れたので修理に出した。", "score": 0.79},
    ]
    assert _ids(apply_retrieval_quality("iPhone", hits, limit=10, now=1_780_000_000)) == ["a", "b"]


def test_dedup_keeps_distinct_notes_whose_only_ascii_token_is_a_year():
    from retrieval_quality import apply_retrieval_quality

    hits = [
        {"id": "c", "memory": "母の誕生日は 2025 年の春に祝った。", "score": 0.70},
        {"id": "d", "memory": "2025 年の確定申告は三月に提出済み。", "score": 0.69},
    ]
    assert _ids(apply_retrieval_quality("2025", hits, limit=10, now=1_780_000_000)) == ["c", "d"]


def test_dedup_still_collapses_japanese_near_duplicates():
    from retrieval_quality import apply_retrieval_quality

    hits = [
        {"id": "first", "memory": "東京駅の近くの喫茶店で毎週土曜日に友達と会って話をする。", "score": 0.80},
        {"id": "again", "memory": "東京駅の近くの喫茶店で毎週土曜日に友達と会って話をする！", "score": 0.79},
        {"id": "exact", "memory": "東京駅の近くの喫茶店で  毎週土曜日に友達と会って話をする。", "score": 0.78},
    ]
    assert _ids(apply_retrieval_quality("喫茶店", hits, limit=10, now=1_780_000_000)) == ["first"]


def test_dedup_tokenises_turkish_letters_as_word_characters():
    from retrieval_quality import _dedup_tokens, apply_retrieval_quality

    # Letters stay whole words (the old [a-z0-9]+ split "şık" into "k");
    # dotted and dotless i fold together because Turkish casing is ambiguous.
    assert _dedup_tokens("İstanbul'da şık bir ılık gün; DAĞ yolu") == {
        "istanbul", "da", "şik", "bir", "ilik", "gün", "dağ", "yolu",
    }
    assert _dedup_tokens("ŞIK ılık") == _dedup_tokens("şık ILIK")
    # "şık" (chic) and "sık" (frequent) stay different words.
    hits = [
        {"id": "chic", "memory": "Ayşe şık bir ceket aldı, rengi kırmızı ve çok güzel.", "score": 0.80},
        {"id": "often", "memory": "Ayşe sık sık ceket alır, rengi önemli değil ama güzel.", "score": 0.79},
        {"id": "dup", "memory": "AYŞE ŞIK BİR CEKET ALDI, RENGİ KIRMIZI VE ÇOK GÜZEL!", "score": 0.78},
    ]
    assert _ids(apply_retrieval_quality("ceket", hits, limit=10, now=1_780_000_000)) == ["chic", "often"]
