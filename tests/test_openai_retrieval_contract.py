"""OpenAI search/fetch must mirror their structured result as JSON text."""

import asyncio
import json

import httpx
import pytest

from catalog import CorpusCatalog, CorpusRecord


@pytest.fixture(params=["raw", "imported", "compiled"])
def document(proxy, request):
    title = 'Café "protocol"'
    body = '# Memory\n\nA quoted "fact", a backslash \\ and Unicode: 日本語.'
    metadata = {"title": title, "source_url": "https://example.org/memory"}
    if request.param == "compiled":
        result = asyncio.run(proxy.handle_compile_page_tool({
            "slug": "protocol-page", "title": title, "markdown": body,
        }))
        assert not result["isError"]
        record_id = result["structuredContent"]["slug"]
    else:
        if request.param == "imported":
            metadata["import_record_id"] = "imported-protocol"
            proxy.catalog = CorpusCatalog([CorpusRecord(
                import_record_id="imported-protocol", title=title, text=body,
                metadata=metadata, source_ref=metadata["source_url"],
            )])
        added = proxy.memory.add(body, user_id=proxy.settings.user_id, metadata=metadata)
        record_id = metadata.get("import_record_id") or added["results"][0]["id"]
    return {"id": record_id, "title": title, "text": body}


def call(proxy, name, arguments, *, endpoint="openai"):
    profile = proxy.endpoint_profiles[getattr(proxy.settings, f"{endpoint}_mcp_path")]
    return asyncio.run(proxy.call_mcp_tool(profile, name, arguments))


def assert_json_mirror(result):
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def assert_json_error(result):
    # Error payloads do not match the advertised outputSchema, so they travel
    # as JSON text only, without structuredContent (validating clients throw).
    assert result["isError"] is True
    assert "structuredContent" not in result
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_openai_retrieval_mirrors_json_for_every_document_source(proxy, document, tool):
    arguments = {"query": "protocol"} if tool == "search" else {"id": document["id"]}
    result = call(proxy, tool, arguments)
    assert result["isError"] is False
    assert_json_mirror(result)
    payload = result["structuredContent"]
    item = payload["results"][0] if tool == "search" else payload
    assert item["id"] == document["id"]
    assert item["title"] == document["title"]
    # Compiled pages also include their existing frontmatter and final newline.
    assert document["text"] in item["snippet" if tool == "search" else "text"]
    assert isinstance(item["url"], str) and item["url"]


def test_openai_empty_search_mirrors_json(proxy):
    result = call(proxy, "search", {"query": "missing"})
    assert result["isError"] is False
    assert_json_mirror(result)
    assert result["structuredContent"] == {
        "query": "missing", "results": [], "nextCursor": None,
    }


@pytest.mark.parametrize("tool,arguments,error", [
    ("search", {"query": ""}, "missing_query"),
    ("fetch", {"id": ""}, "missing_id"),
    ("fetch", {"id": "not-present"}, "not_found"),
])
def test_openai_retrieval_errors_preserve_error_flag_and_return_json_text(proxy, tool, arguments, error):
    result = call(proxy, tool, arguments)
    assert assert_json_error(result)["error"] == error


def test_openai_unexpected_retrieval_error_returns_json_text(proxy, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(proxy, "handle_search_tool", fail)
    result = call(proxy, "search", {"query": "protocol"})
    assert assert_json_error(result) == {"error": "synthetic failure"}


@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_claude_retrieval_keeps_native_text_format(proxy, document, tool):
    arguments = {"query": "protocol"} if tool == "search" else {"id": document["id"]}
    result = call(proxy, f"reliquary_{tool}", arguments, endpoint="claude")
    native = asyncio.run(getattr(proxy, f"handle_{tool}_tool")(arguments))
    assert result["isError"] is False
    assert result["content"] == native["content"]
    assert document["text"] in result["content"][0]["text"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(result["content"][0]["text"])


def test_openai_other_tools_keep_native_text_format(proxy):
    result = call(proxy, "capabilities", {})
    assert result["content"][0]["text"].startswith("Reliquary capabilities")


def test_openai_retrieval_advertises_output_schemas(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    tools = {t["name"]: t for t in proxy.mcp_tools_for(profile)}
    search = tools["search"]["outputSchema"]
    assert search["type"] == "object"
    assert "results" in search["required"]
    results = search["properties"]["results"]
    assert results["type"] == "array"
    assert set(results["items"]["required"]) == {"id", "title", "url"}
    for name in ("id", "title", "url"):
        assert results["items"]["properties"][name]["type"] == "string"
    fetch = tools["fetch"]["outputSchema"]
    assert fetch["type"] == "object"
    assert set(fetch["required"]) == {"id", "title", "text", "url"}
    for name in ("id", "title", "text", "url"):
        assert fetch["properties"][name]["type"] == "string"
    assert fetch["properties"]["metadata"]["type"] == "object"
    assert "outputSchema" not in tools["capabilities"]
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    assert all("outputSchema" not in t for t in proxy.mcp_tools_for(claude))


@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_openai_retrieval_json_survives_http_transport(proxy, document, tool):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="https://example.org",
            headers={"authorization": "Bearer openai-secret"},
        ) as client:
            arguments = {"query": "protocol"} if tool == "search" else {"id": document["id"]}
            response = await client.post("/openai/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            })
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["isError"] is False
            assert_json_mirror(result)

    asyncio.run(request())


@pytest.mark.parametrize("endpoint", ["openai", "claude"])
@pytest.mark.parametrize("tool", ["search", "fetch"])
def test_rate_limited_retrieval_keeps_endpoint_response_format(proxy, monkeypatch, endpoint, tool):
    monkeypatch.setattr(proxy.search_limiter, "allow", lambda key: False)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy), base_url="https://example.org",
            headers={"authorization": f"Bearer {endpoint}-secret"},
        ) as client:
            name = tool if endpoint == "openai" else f"reliquary_{tool}"
            response = await client.post(f"/{endpoint}/mcp", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            })
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["isError"] is True
            if endpoint == "openai":
                assert assert_json_error(result) == {"error": "rate_limited", "category": "read"}
            else:
                assert result["structuredContent"] == {"error": "rate_limited", "category": "read"}
                assert result["content"][0]["text"] == "Rate limit exceeded; slow down and retry shortly."

    asyncio.run(request())
