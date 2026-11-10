"""Regression tests for the 2026-11 retrieval audit findings."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import server
from catalog import CorpusCatalog, CorpusRecord


class ScoredMemory:
    """In-memory fake with Mem0 2.x call shapes: the entity id travels inside
    ``filters``, ``top_k`` caps results, scores come from ``scorer`` and
    metadata keys in ``filters`` match the stored metadata (as the Qdrant
    payload filter does). ``threshold`` is ignored unless honour_threshold."""

    def __init__(self, scorer=None, *, honour_threshold=False):
        self._store: dict[str, dict] = {}
        self._n = 0
        self.scorer = scorer or (lambda query, record: 0.5)
        self.honour_threshold = honour_threshold
        self.search_calls: list[dict] = []
        self.get_all_calls: list[dict] = []

    def _match(self, record, filters):
        filters = dict(filters or {})
        user_id = filters.pop("user_id", None)
        if user_id is not None and record["user_id"] != user_id:
            return False
        metadata = record.get("metadata") or {}
        return all(metadata.get(key) == value for key, value in filters.items())

    def search(self, query, *, top_k=20, filters=None, threshold=None):
        self.search_calls.append({"top_k": top_k, "filters": filters, "threshold": threshold})
        hits = []
        for record in self._store.values():
            if not self._match(record, filters):
                continue
            score = self.scorer(query, record)
            if self.honour_threshold and threshold is not None and score < threshold:
                continue
            hits.append(dict(record, score=score))
        hits.sort(key=lambda hit: -hit["score"])
        return {"results": hits[:top_k]}

    def get(self, memory_id):
        record = self._store.get(memory_id)
        return dict(record) if record else None

    def get_all(self, *, filters=None, top_k=20):
        self.get_all_calls.append({"filters": filters, "top_k": top_k})
        records = [dict(r) for r in self._store.values() if self._match(r, filters)]
        return {"results": records[:top_k]}

    def add(self, text, *, user_id=None, metadata=None, infer=False, created_at=None):
        self._n += 1
        memory_id = f"00000000-0000-4000-8000-{self._n:012d}"
        self._store[memory_id] = {
            "id": memory_id, "memory": text, "metadata": dict(metadata or {}), "user_id": user_id,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        return {"results": [{"id": memory_id, "event": "ADD"}]}

    def delete(self, memory_id):
        if memory_id not in self._store:
            raise ValueError(f"Memory with id {memory_id} not found")
        del self._store[memory_id]

    def update(self, memory_id, data, metadata=None):
        record = self._store[memory_id]
        record["memory"] = data
        if metadata is not None:
            record["metadata"] = dict(metadata)
        return {"message": "ok"}


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


# ---------------------------------------------------------------------------
# 5. A proposed correction and its imported original must not suppress each other
# ---------------------------------------------------------------------------

NAS_BODY = ("The NAS backup job runs every night at 02:00 and copies the photo share "
            "to the external disk in the garage, then verifies checksums and mails a report "
            "to the admin account when any file differs from the previous snapshot.")


def test_dedup_never_drops_a_correction_or_its_target():
    from retrieval_quality import apply_retrieval_quality

    corrected = NAS_BODY.replace("02:00", "04:00")
    hits = [
        {"id": "correction", "memory": corrected, "score": 0.81,
         "metadata": {"kind": "correction", "target_id": "nas-1", "status": "proposed"}},
        {"id": "nas-1", "memory": NAS_BODY, "score": 0.80, "metadata": {"source_group": "imported"}},
        # An ordinary near-copy of the original is still a duplicate of it.
        {"id": "copy", "memory": NAS_BODY + " ", "score": 0.79, "metadata": {}},
    ]
    assert _ids(apply_retrieval_quality("NAS backup", hits, limit=10, now=1_780_000_000)) == [
        "correction", "nas-1",
    ]
    # Whichever ranks first, both survive.
    hits[0]["score"], hits[1]["score"] = 0.70, 0.90
    assert set(_ids(apply_retrieval_quality("NAS backup", hits, limit=10, now=1_780_000_000))) == {
        "correction", "nas-1",
    }


def test_lean_search_shows_correction_beside_its_original_with_status(proxy):
    metadata = {"title": "NAS backup", "import_record_id": "nas-1", "source_group": "imported"}
    proxy.catalog = CorpusCatalog([CorpusRecord(
        import_record_id="nas-1", title="NAS backup", text=NAS_BODY,
        metadata={"title": "NAS backup"}, source_ref="",
    )])
    proxy.memory.add(NAS_BODY, user_id=proxy.settings.user_id, metadata=metadata)
    filed = call(proxy, "propose_update", {
        "target_id": "nas-1", "reason": "time is wrong",
        "replacement_text": NAS_BODY.replace("02:00", "04:00"),
    })
    assert filed["isError"] is False
    correction_id = filed["structuredContent"]["id"]

    result = call(proxy, "search", {"query": "NAS backup job", "limit": 5})
    assert result["isError"] is False
    assert schema_errors(output_schemas(proxy)["search"], result["structuredContent"]) == []
    items = {item["id"]: item for item in result["structuredContent"]["results"]}
    assert {"nas-1", correction_id} <= set(items)
    assert items[correction_id]["kind"] == "correction"
    assert items[correction_id]["status"] == "proposed"
    assert items[correction_id]["target_id"] == "nas-1"
    assert "kind" not in items["nas-1"] and "status" not in items["nas-1"]


# ---------------------------------------------------------------------------
# 6. Cursor pagination must partition one stable ranking
# ---------------------------------------------------------------------------

def _paginate(proxy, query, *, limit, pages, endpoint="openai", **extra):
    tool = "search" if endpoint == "openai" else "reliquary_search"
    seen, cursor = [], None
    for _ in range(pages):
        arguments = {"query": query, "limit": limit, **extra}
        if cursor:
            arguments["cursor"] = cursor
        structured = call(proxy, tool, arguments, endpoint=endpoint)["structuredContent"]
        seen += [item["id"] for item in structured["results"]]
        cursor = structured["nextCursor"]
        if not cursor:
            break
    return seen


def test_pagination_pages_partition_one_ranking(make_proxy):
    old = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat()
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet",
             "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo", "sierra", "tango"]
    boosted = {8, 20, 22, 30, 40}
    scores: dict[str, float] = {}
    memory = ScoredMemory(lambda query, record: scores[record["id"]])
    for i in range(60):
        unique = " ".join(words[(i * 7 + j * 3) % 20] + str(i) for j in range(8))
        added = memory.add(
            f"record{i} {unique}" + (" zeppelin hangar" if i in boosted else ""),
            user_id="my_lord", created_at=None if i in boosted else old,
            metadata={"title": f"r{i}", "source_group": "imported", "import_record_id": f"r{i}"},
        )
        scores[added["results"][0]["id"]] = 0.90 - i * 0.004
    proxy = make_proxy(memory=memory)

    by_five = _paginate(proxy, "zeppelin hangar", limit=5, pages=6)
    assert len(by_five) == 30
    assert len(set(by_five)) == 30, "a record repeated across pages"
    # The same ranking, sliced differently, gives the same sequence.
    assert _paginate(proxy, "zeppelin hangar", limit=15, pages=2) == by_five
    # The vector pool is the same size on every page.
    assert len({c["top_k"] for c in memory.search_calls}) == 1
    # The quality key (lexical + recency bonus) orders the pages, not just the cut:
    # the fresh records that match the query lead.
    assert by_five[:4] == ["r8", "r20", "r22", "r30"]


# ---------------------------------------------------------------------------
# 8. threshold applies to compiled pages too
# ---------------------------------------------------------------------------

def test_threshold_excludes_low_scoring_synthesis(make_proxy):
    compiled = ScoredMemory(lambda query, record: 0.15)
    proxy = make_proxy(memory=ScoredMemory(lambda query, record: 0.95), compiled_memory=compiled)
    call(proxy, "reliquary_add_memory", {"text": "raw strong match"}, endpoint="claude")
    assert not call(proxy, "reliquary_compile_page", {"slug": "weak-page", "markdown": "barely related"},
                    endpoint="claude")["isError"]

    results = call(proxy, "reliquary_search", {"query": "x", "threshold": 0.9},
                   endpoint="claude")["structuredContent"]["results"]
    assert [item["route"] for item in results] == ["global"]
    assert compiled.search_calls[-1]["threshold"] == 0.9

    results = call(proxy, "reliquary_search", {"query": "x"}, endpoint="claude")["structuredContent"]["results"]
    assert "weak-page" in [item["id"] for item in results]


# ---------------------------------------------------------------------------
# 9. Explicit taxonomy filters apply to syntheses before the limit cut
# ---------------------------------------------------------------------------

def _compile_ranked_pages(proxy, scores):
    for slug, (score, domain) in scores.items():
        result = call(proxy, "reliquary_compile_page",
                      {"slug": slug, "markdown": f"page {slug}", "domain": domain}, endpoint="claude")
        assert not result["isError"]


def test_explicit_domain_keeps_matching_synthesis_behind_higher_scoring_pages(make_proxy):
    scores = {f"other-{i}": (0.9 - i * 0.01, "life") for i in range(12)}
    scores["dev-page"] = (0.7, "dev")
    compiled = ScoredMemory(lambda query, record: scores[record["metadata"]["slug"]][0])
    proxy = make_proxy(memory=ScoredMemory(), compiled_memory=compiled)
    _compile_ranked_pages(proxy, scores)

    results = call(proxy, "reliquary_search", {"query": "x", "domain": "dev", "limit": 1},
                   endpoint="claude")["structuredContent"]["results"]
    assert [item["id"] for item in results] == ["dev-page"]
    # The filter runs before the cut inside the compiled pre-pass itself.
    hits = asyncio.run(proxy._synthesis_first_hits("x", user_id="my_lord", limit=1, filters={"domain": "dev"}))
    assert [hit["id"] for hit in hits] == ["dev-page"]


# ---------------------------------------------------------------------------
# 10. nextCursor never points past the cursor cap
# ---------------------------------------------------------------------------

def test_next_cursor_is_null_when_the_next_offset_would_be_rejected(make_proxy, monkeypatch):
    monkeypatch.setattr(server, "SEARCH_RANKING_DEPTH", 500, raising=False)
    memory = ScoredMemory(lambda query, record: 0.5)
    for i in range(450):
        memory.add(f"r{i} t{i} u{i} v{i} w{i}", user_id="my_lord",
                   metadata={"source_group": "imported", "import_record_id": f"r{i}"})
    proxy = make_proxy(memory=memory)

    structured = call(proxy, "search", {"query": "zzz", "limit": 20, "cursor": "380"})["structuredContent"]
    assert structured["nextCursor"] == "400"
    follow = call(proxy, "search", {"query": "zzz", "limit": 20, "cursor": "400"})
    assert follow["isError"] is False
    assert len(follow["structuredContent"]["results"]) == 20
    # 420 would be rejected as invalid_cursor, so no cursor is offered.
    assert follow["structuredContent"]["nextCursor"] is None
    structured = call(proxy, "search", {"query": "zzz", "limit": 20, "cursor": "395"})["structuredContent"]
    assert structured["nextCursor"] is None


# ---------------------------------------------------------------------------
# 4. The exact-identifier lexical fallback must reach every user-write
# ---------------------------------------------------------------------------

TOKEN = "ARDA-RELIQUARY-IMAGE-20260605-01"


def _store_with_imports(memory, count):
    for i in range(count):
        memory.add(f"imported filler record number {i}", user_id="my_lord",
                   metadata={"source_group": "imported", "import_record_id": f"imp-{i}"})


def test_lexical_fallback_finds_user_write_behind_5000_imports(make_proxy):
    memory = ScoredMemory(lambda query, record: 0.9 if record["metadata"].get("source_group") == "imported" else 0.05)
    _store_with_imports(memory, 5500)
    proxy = make_proxy(memory=memory)
    call(proxy, "reliquary_add_memory", {"text": f"token {TOKEN} smoke marker"}, endpoint="claude")

    results = call(proxy, "reliquary_search", {"query": TOKEN, "limit": 5},
                   endpoint="claude")["structuredContent"]["results"]
    assert any(TOKEN in item["text"] for item in results)
    assert memory.get_all_calls[-1]["filters"] == {"user_id": "my_lord", "source_group": "user-write"}


class UnfilteredListMemory(ScoredMemory):
    """A store whose listing ignores metadata filters (only the entity id)."""

    def get_all(self, *, filters=None, top_k=20):
        self.get_all_calls.append({"filters": filters, "top_k": top_k})
        user_id = (filters or {}).get("user_id")
        records = [dict(r) for r in self._store.values() if r["user_id"] == user_id]
        return {"results": records[:top_k]}


def test_lexical_fallback_grows_the_scan_when_a_page_comes_back_full(make_proxy):
    memory = UnfilteredListMemory(lambda query, record: 0.05)
    _store_with_imports(memory, 5500)
    proxy = make_proxy(memory=memory)
    call(proxy, "reliquary_add_memory", {"text": f"token {TOKEN} smoke marker"}, endpoint="claude")

    hits = asyncio.run(proxy.live_lexical_matches(TOKEN, user_id="my_lord", filters=None, limit=5))
    assert [TOKEN in hit["memory"] for hit in hits] == [True]
    assert [c["top_k"] for c in memory.get_all_calls] == [5000, 20000]


def test_get_all_kwargs_pass_source_group_on_mem0_1x_and_2x_signatures():
    def get_all_1x(*, user_id=None, agent_id=None, run_id=None, filters=None, limit=100):
        return {}

    def get_all_2x(*, filters=None, top_k=20, **kwargs):
        return {}

    meta = {"source_group": "user-write"}
    assert server.ReliquaryChatProxy._get_all_kwargs(get_all_1x, user_id="u", top_k=7, metadata_filters=meta) == {
        "filters": {"user_id": "u", "source_group": "user-write"}, "user_id": "u", "limit": 7,
    }
    assert server.ReliquaryChatProxy._get_all_kwargs(get_all_2x, user_id="u", top_k=7, metadata_filters=meta) == {
        "filters": {"user_id": "u", "source_group": "user-write"}, "top_k": 7,
    }
