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
    assert "reliquary://taxonomy" in uris
    assert "reliquary://domain/pagan" in uris

    tax = proxy.read_resource("reliquary://taxonomy")
    payload = json.loads(tax["contents"][0]["text"])
    assert payload["domains"] == ["pagan", "7thshadow"]

    dom = proxy.read_resource("reliquary://domain/pagan")
    assert json.loads(dom["contents"][0]["text"])["rooms"] == ["studio"]
    assert proxy.read_resource("reliquary://domain/nope") is None
    assert proxy.read_resource("bogus://x") is None


def test_resources_with_no_catalog(proxy):
    assert proxy.catalog is None
    uris = [resource["uri"] for resource in proxy.mcp_resources()]
    # reliquary://taxonomy and reliquary://schema are always present; compiled resources are
    # present because the default proxy fixture enables the compiled layer.
    assert "reliquary://taxonomy" in uris
    assert "reliquary://schema" in uris
    assert json.loads(proxy.read_resource("reliquary://taxonomy")["contents"][0]["text"])["records"] == 0


def test_resource_uris_all_reliquary_scheme(proxy):
    uris = [r["uri"] for r in proxy.mcp_resources()]
    assert uris, "expected resources"
    offenders = [u for u in uris if not u.startswith("reliquary://")]
    assert offenders == [], f"non-reliquary resource URIs: {offenders}"


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

    res = run(proxy.call_mcp_tool(claude, "reliquary_list_domains", {}, can_write=False))
    openai_res = run(proxy.call_mcp_tool(openai, "list_domains", {}, can_write=False))

    assert res["structuredContent"]["domains"] == ["pagan", "7thshadow"]
    assert res["isError"] is False
    assert openai_res["structuredContent"]["domains"] == ["pagan", "7thshadow"]
    assert openai_res["isError"] is False


def test_claude_tools_all_reliquary_prefixed(proxy):
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    names = [t["name"] for t in proxy.mcp_tools_for(claude, can_write=True)]
    assert names, "expected a non-empty Claude tool list"
    offenders = [n for n in names if not n.startswith("reliquary_")]
    assert offenders == [], f"non-reliquary tool names on Claude endpoint: {offenders}"


def test_openai_endpoint_keeps_search_and_fetch(proxy):
    openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    names = [t["name"] for t in proxy.mcp_tools_for(openai, can_write=True)]
    assert "search" in names and "fetch" in names, names
    assert not any(n.startswith("reliquary_") for n in names), names


def test_search_explicit_domain_filter(proxy):
    """Searching with domain= returns only domain-matching hits and echoes filters."""
    proxy.memory._store["dev-1"] = {
        "id": "dev-1", "memory": "alpha dev note", "metadata": {"domain": "dev"}, "user_id": "my_lord",
    }
    proxy.memory._store["misc-1"] = {
        "id": "misc-1", "memory": "alpha misc note", "metadata": {"domain": "misc"}, "user_id": "my_lord",
    }
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(claude, "reliquary_search", {"query": "alpha", "domain": "dev"}, can_write=False))
    assert not result.get("isError"), result
    sc = result["structuredContent"]
    ids = {r["id"] for r in sc["results"]}
    assert "dev-1" in ids
    assert "misc-1" not in ids
    assert sc.get("filters") == {"domain": "dev"}


def test_search_no_filter_unchanged_behavior(proxy):
    """Without explicit filters, search returns all matching records across domains."""
    proxy.memory._store["dev-1"] = {
        "id": "dev-1", "memory": "alpha dev note", "metadata": {"domain": "dev"}, "user_id": "my_lord",
    }
    proxy.memory._store["misc-1"] = {
        "id": "misc-1", "memory": "alpha misc note", "metadata": {"domain": "misc"}, "user_id": "my_lord",
    }
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(claude, "reliquary_search", {"query": "alpha"}, can_write=False))
    assert not result.get("isError"), result
    sc = result["structuredContent"]
    ids = {r["id"] for r in sc["results"]}
    assert "dev-1" in ids and "misc-1" in ids
    # No filters echoed when none supplied.
    assert "filters" not in sc


def test_search_explicit_filters_in_schema(proxy):
    """The Claude reliquary_search tool schema must advertise domain/hall/room/topic params."""
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = {t["name"]: t for t in proxy.mcp_tools_for(claude, can_write=False)}
    search_schema = tools["reliquary_search"]["inputSchema"]["properties"]
    for field in ("domain", "hall", "room", "topic"):
        assert field in search_schema, f"missing {field!r} in reliquary_search schema"


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

    page1 = run(proxy.call_mcp_tool(claude, "reliquary_search", {"query": "memory", "limit": 2}, can_write=False))
    s1 = page1["structuredContent"]
    assert s1.get("nextCursor") == "2"

    page2 = run(
        proxy.call_mcp_tool(
            claude,
            "reliquary_search",
            {"query": "memory", "limit": 2, "cursor": "2"},
            can_write=False,
        )
    )

    def ids(structured):
        return {result.get("id") for result in structured.get("results", [])}

    assert ids(s1)
    assert ids(page2["structuredContent"])
    assert ids(s1).isdisjoint(ids(page2["structuredContent"]))
