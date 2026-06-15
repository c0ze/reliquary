import asyncio
import json
import types


def run(coro):
    return asyncio.run(coro)


def _stub_catalog():
    cat = types.SimpleNamespace()
    cat.routeable_domains = ["pagan", "7thshadow"]
    cat.domains_by_room = {"studio": {"pagan"}, "lore": {"7thshadow"}}
    cat.domains_by_topic = {"mixing": {"pagan"}}
    cat.records_by_id = {"a": 1, "b": 2}
    return cat


def test_resources_list_and_read(proxy):
    proxy.catalog = _stub_catalog()
    uris = [resource["uri"] for resource in proxy.mcp_resources()]
    assert "mem0://taxonomy" in uris
    assert "mem0://domain/pagan" in uris

    tax = proxy.read_resource("mem0://taxonomy")
    payload = json.loads(tax["contents"][0]["text"])
    assert payload["domains"] == ["pagan", "7thshadow"]

    dom = proxy.read_resource("mem0://domain/pagan")
    assert json.loads(dom["contents"][0]["text"])["rooms"] == ["studio"]
    assert proxy.read_resource("mem0://domain/nope") is None
    assert proxy.read_resource("bogus://x") is None


def test_resources_with_no_catalog(proxy):
    assert proxy.catalog is None
    uris = [resource["uri"] for resource in proxy.mcp_resources()]
    # mem0://taxonomy and mem0://schema are always present; compiled resources are
    # present because the default proxy fixture enables the compiled layer.
    assert "mem0://taxonomy" in uris
    assert "mem0://schema" in uris
    assert json.loads(proxy.read_resource("mem0://taxonomy")["contents"][0]["text"])["records"] == 0


def test_prompts(proxy):
    names = {prompt["name"] for prompt in proxy.mcp_prompts()}
    assert names == {"recall", "summarise_results"}

    got = proxy.get_prompt("recall", {"query": "the dragon logo"})
    assert "dragon logo" in got["messages"][0]["content"]["text"]
    assert proxy.get_prompt("unknown", {}) is None


def test_list_domains_tool(proxy):
    proxy.catalog = _stub_catalog()
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]

    res = run(proxy.call_mcp_tool(claude, "list_domains", {}, can_write=False))
    openai_res = run(proxy.call_mcp_tool(openai, "list_domains", {}, can_write=False))

    assert res["structuredContent"]["domains"] == ["pagan", "7thshadow"]
    assert res["isError"] is False
    assert openai_res["structuredContent"]["domains"] == ["pagan", "7thshadow"]
    assert openai_res["isError"] is False


def test_search_pagination(proxy, monkeypatch):
    hits = [
        {
            "id": f"fake-{index}",
            "memory": f"memory number {index}",
            "metadata": {"source_group": "user-write"},
            "score": 1.0,
        }
        for index in range(5)
    ]

    async def fake_search_memories(query, *, user_id, limit, threshold, filters):
        assert query == "memory"
        assert user_id == "my_lord"
        return hits[:limit]

    monkeypatch.setattr(proxy, "search_memories", fake_search_memories)
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]

    page1 = run(proxy.call_mcp_tool(claude, "mem0_search", {"query": "memory", "limit": 2}, can_write=False))
    s1 = page1["structuredContent"]
    assert s1.get("nextCursor") == "2"

    page2 = run(
        proxy.call_mcp_tool(
            claude,
            "mem0_search",
            {"query": "memory", "limit": 2, "cursor": "2"},
            can_write=False,
        )
    )

    def ids(structured):
        return {result.get("id") for result in structured.get("results", [])}

    assert ids(s1)
    assert ids(page2["structuredContent"])
    assert ids(s1).isdisjoint(ids(page2["structuredContent"]))
