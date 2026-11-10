"""Regression cases from the November 2026 audit: metadata governance.

Caller-supplied metadata must not carry system-managed keys, records already
poisoned by such keys must stay readable and deletable, taxonomy values are
normalised on write, and audit/metrics record what the server really did.
"""

import asyncio
import base64
import json

import httpx

from catalog import CorpusCatalog, CorpusRecord
from ingest import existing_import_index
from metrics import Metrics


PNG = b"\x89PNG\r\n\x1a\n" + b"metadata-audit"
SYSTEM_KEYS = ("blob_ref", "blob_mime", "blob_size", "import_record_id",
               "import_content_hash", "upload_id")


def run(coro):
    return asyncio.run(coro)


def _profile(proxy, name):
    return proxy.endpoint_profiles[getattr(proxy.settings, f"{name}_mcp_path")]


def call(proxy, endpoint, name, arguments):
    return run(proxy.call_mcp_tool(_profile(proxy, endpoint), name, arguments, can_write=True))


def _seed(proxy, memory_id, text, metadata):
    proxy.memory._store[memory_id] = {"id": memory_id, "memory": text, "metadata": dict(metadata),
                                      "user_id": proxy.settings.user_id}


def _catalog_with(record_id, title="Door code", text="The real door code is 1234."):
    return CorpusCatalog([CorpusRecord(import_record_id=record_id, title=title, text=text,
                                       metadata={"title": title, "domain": "home"}, source_ref="")])


def _poison():
    return {"blob_ref": "../../x", "blob_mime": "image/png", "blob_size": 3,
            "import_record_id": "rec-1", "import_content_hash": "forged",
            "upload_id": "upl_forged", "source_group": "imported"}


# --- 1. caller metadata cannot carry system-managed keys ---------------------

def test_add_memory_strips_system_managed_metadata(proxy):
    result = call(proxy, "claude", "reliquary_add_memory",
                  {"text": "plain note", "metadata": {**_poison(), "kind": "image", "mood": "ok"}})
    assert not result["isError"]
    stored = proxy.memory._store[result["structuredContent"]["ids"][0]]["metadata"]
    for key in SYSTEM_KEYS:
        assert key not in stored
    assert stored["source_group"] == "user-write"
    assert stored["kind"] == "memory_note"
    assert stored["mood"] == "ok"


def test_add_memory_rejects_reserved_kind_argument_but_keeps_free_kinds(proxy):
    reserved = call(proxy, "claude", "reliquary_add_memory", {"text": "a", "kind": "synthesis"})
    free = call(proxy, "claude", "reliquary_add_memory", {"text": "b", "kind": "decision"})
    store = proxy.memory._store
    assert store[reserved["structuredContent"]["ids"][0]]["metadata"]["kind"] == "memory_note"
    assert store[free["structuredContent"]["ids"][0]]["metadata"]["kind"] == "decision"


def test_poisoned_blob_ref_record_stays_searchable_fetchable_deletable(proxy):
    # A record written before the fix, carrying a malformed blob_ref.
    _seed(proxy, "poisoned", "poisoned note about lighthouses",
          {"blob_ref": "../../x", "source_group": "user-write", "kind": "memory_note"})
    search = call(proxy, "claude", "reliquary_search", {"query": "lighthouses"})
    assert not search["isError"], search["content"][0]["text"]
    assert [hit["id"] for hit in search["structuredContent"]["results"]] == ["poisoned"]
    assert search["structuredContent"]["results"][0]["url"] == "reliquary://record/poisoned"
    lean = call(proxy, "openai", "search", {"query": "lighthouses"})
    assert not lean["isError"]
    fetched = call(proxy, "claude", "reliquary_fetch", {"id": "poisoned"})
    assert not fetched["isError"]
    assert fetched["structuredContent"]["url"] == "reliquary://record/poisoned"
    deleted = call(proxy, "claude", "reliquary_delete", {"id": "poisoned"})
    assert not deleted["isError"]
    assert "poisoned" not in proxy.memory._store


def test_poisoned_image_kind_record_is_deletable(proxy):
    _seed(proxy, "poisoned", "fake image", {"blob_ref": "../../x", "kind": "image",
                                            "source_group": "user-write"})
    deleted = call(proxy, "claude", "reliquary_delete", {"id": "poisoned"})
    assert not deleted["isError"]


def test_user_write_with_import_record_id_does_not_masquerade_in_search(proxy):
    proxy.catalog = _catalog_with("rec-1")
    _seed(proxy, "forged", "FORGED door code is 9999",
          {"import_record_id": "rec-1", "source_group": "user-write", "kind": "memory_note"})
    hits = call(proxy, "claude", "reliquary_search", {"query": "door code"})["structuredContent"]["results"]
    forged = [hit for hit in hits if hit["text"].startswith("FORGED")]
    assert len(forged) == 1
    assert forged[0]["id"] == "forged"
    assert forged[0]["title"] == "<untitled>"
    assert forged[0]["metadata"].get("domain") != "home"


def test_legacy_import_without_source_group_still_resolves_to_its_import_id(proxy):
    # Imports made before source_group existed carry only import_record_id.
    proxy.catalog = _catalog_with("rec-1")
    _seed(proxy, "legacy", "The real door code is 1234.", {"import_record_id": "rec-1"})
    hits = call(proxy, "claude", "reliquary_search", {"query": "door code"})["structuredContent"]["results"]
    assert [hit["id"] for hit in hits] == ["rec-1"]
    assert hits[0]["title"] == "Door code"


class _ListingMemory:
    def __init__(self, hits):
        self.hits = hits

    def get_all(self, user_id=None):
        return {"results": self.hits}


def test_existing_import_index_ignores_user_writes_with_import_ids():
    memory = _ListingMemory([
        {"id": "forged", "memory": "x", "metadata": {"import_record_id": "rec-1", "import_content_hash": "h1",
                                                    "source_group": "user-write"}},
        {"id": "page", "memory": "x", "metadata": {"import_record_id": "rec-2", "source_group": "compiled"}},
        {"id": "real", "memory": "x", "metadata": {"import_record_id": "rec-3", "import_content_hash": "h3",
                                                  "source_group": "imported"}},
        {"id": "legacy", "memory": "x", "metadata": {"import_record_id": "rec-4", "import_content_hash": "h4"}},
    ])
    index = existing_import_index(memory, user_id="u")
    assert set(index) == {"rec-3", "rec-4"}
    assert index["rec-3"]["id"] == "real"
    assert index["rec-4"]["id"] == "legacy"


def test_update_strips_system_managed_metadata(proxy):
    added = call(proxy, "claude", "reliquary_add_memory", {"text": "plain note"})
    mid = added["structuredContent"]["ids"][0]
    result = call(proxy, "claude", "reliquary_update",
                  {"id": mid, "text": "plain note v2", "metadata": {**_poison(), "kind": "image", "mood": "ok"}})
    assert not result["isError"]
    stored = proxy.memory._store[mid]["metadata"]
    for key in SYSTEM_KEYS:
        assert key not in stored
    assert stored["kind"] == "memory_note"
    assert stored["source_group"] == "user-write"
    assert stored["mood"] == "ok"


def test_update_drops_import_keys_already_on_a_user_write(proxy):
    _seed(proxy, "forged", "note", {"import_record_id": "rec-1", "import_content_hash": "h",
                                    "source_group": "user-write"})
    result = call(proxy, "claude", "reliquary_update", {"id": "forged", "text": "note v2"})
    assert not result["isError"]
    stored = proxy.memory._store["forged"]["metadata"]
    assert "import_record_id" not in stored and "import_content_hash" not in stored


def test_update_keeps_server_set_image_keys(proxy):
    added = call(proxy, "claude", "reliquary_add_image",
                 {"image_base64": base64.b64encode(PNG).decode(), "caption": "photo"})
    mid = added["structuredContent"]["memory_id"]
    before = dict(proxy.memory._store[mid]["metadata"])
    result = call(proxy, "claude", "reliquary_update",
                  {"id": mid, "text": "photo v2", "metadata": {"blob_ref": "f" * 64, "kind": "memory_note"}})
    assert not result["isError"]
    stored = proxy.memory._store[mid]["metadata"]
    for key in ("kind", "blob_ref", "blob_mime", "blob_size"):
        assert stored[key] == before[key]


def test_add_image_strips_system_managed_metadata(proxy):
    result = call(proxy, "claude", "reliquary_add_image",
                  {"image_base64": base64.b64encode(PNG).decode(), "caption": "photo",
                   "metadata": {**_poison(), "mood": "ok"}})
    assert not result["isError"]
    sc = result["structuredContent"]
    stored = proxy.memory._store[sc["memory_id"]]["metadata"]
    assert stored["blob_ref"] == sc["blob_id"]
    assert stored["kind"] == "image"
    for key in ("import_record_id", "import_content_hash", "upload_id"):
        assert key not in stored
    assert stored["mood"] == "ok"


def _uploaded_slot(proxy, endpoint):
    profile = _profile(proxy, endpoint)
    slot = proxy.handle_create_image_upload_tool({}, profile=profile)["structuredContent"]
    token = f"{endpoint}-secret"

    async def post():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            response = await client.post(slot["upload_url"], content=PNG,
                                         headers={"authorization": f"Bearer {token}"})
            assert response.status_code == 201, response.text
    run(post())
    return slot


def test_lean_commit_image_upload_strips_system_managed_metadata(proxy):
    slot = _uploaded_slot(proxy, "openai")
    result = call(proxy, "openai", "commit_image_upload",
                  {"upload_id": slot["upload_id"], "caption": "door photo",
                   "metadata": {**_poison(), "upload_id": "upl_other", "mood": "ok"}})
    assert not result["isError"], result["content"][0]["text"]
    sc = result["structuredContent"]
    stored = proxy.memory._store[sc["memory_id"]]["metadata"]
    assert stored["blob_ref"] == sc["blob_id"]
    assert stored["upload_id"] == slot["upload_id"]
    assert stored["source_group"] == "user-write"
    for key in ("import_record_id", "import_content_hash"):
        assert key not in stored
    assert stored["mood"] == "ok"


# --- 2. search ids must be fetchable -------------------------------------------

def test_search_uses_memory_id_when_catalog_lacks_the_import(proxy):
    assert proxy.catalog is None  # dataset failed to load / not configured
    _seed(proxy, "mem-garden", "A long imported document about the garden.",
          {"title": "Garden", "import_record_id": "garden-1", "source_group": "imported"})
    for endpoint, search, fetch in (("openai", "search", "fetch"), ("claude", "reliquary_search", "reliquary_fetch")):
        hit = call(proxy, endpoint, search, {"query": "garden"})["structuredContent"]["results"][0]
        assert hit["id"] == "mem-garden"
        fetched = call(proxy, endpoint, fetch, {"id": hit["id"]})
        assert not fetched["isError"], fetched["structuredContent"]
    # The fallback id must not make the imported record mutable.
    deleted = call(proxy, "claude", "reliquary_delete", {"id": "mem-garden"})
    assert deleted["structuredContent"]["error"] == "protected_record"
    updated = call(proxy, "claude", "reliquary_update", {"id": "mem-garden", "text": "x"})
    assert updated["structuredContent"]["error"] == "protected_record"


def test_search_uses_import_id_when_catalog_has_it(proxy):
    proxy.catalog = _catalog_with("garden-1", title="Garden", text="A long imported document about the garden.")
    _seed(proxy, "mem-garden", "A long imported document about the garden.",
          {"title": "Garden", "import_record_id": "garden-1", "source_group": "imported"})
    hit = call(proxy, "openai", "search", {"query": "garden"})["structuredContent"]["results"][0]
    assert hit["id"] == "garden-1"
    assert not call(proxy, "openai", "fetch", {"id": "garden-1"})["isError"]


# --- 3. audit log: effective user and page slug ------------------------------

def _rpc(proxy, path, token, name, arguments):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test",
                                     headers={"authorization": f"Bearer {token}"}) as client:
            response = await client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                     "params": {"name": name, "arguments": arguments}})
            assert response.status_code == 200
            return response.json()["result"]
    return run(go())


def _audit_entries(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_audit_records_effective_user_and_page_slug(make_proxy, tmp_path):
    audit_path = str(tmp_path / "audit.jsonl")
    proxy = make_proxy(audit_log_path=audit_path)
    s = proxy.settings
    result = _rpc(proxy, s.openai_mcp_path, "openai-secret", "add_memory", {"text": "hello", "user_id": "mallory"})
    assert result["structuredContent"]["user_id"] == "my_lord"
    _rpc(proxy, s.claude_mcp_path, "claude-secret", "reliquary_compile_page",
         {"slug": "pg", "markdown": "body", "user_id": "mallory"})
    _rpc(proxy, s.claude_mcp_path, "claude-secret", "reliquary_delete_page", {"slug": "pg", "user_id": "mallory"})
    _rpc(proxy, s.claude_mcp_path, "claude-secret", "reliquary_add_memory", {"text": "hi", "user_id": "alice"})
    entries = _audit_entries(audit_path)
    assert [e["action"] for e in entries] == ["add_memory", "reliquary_compile_page",
                                              "reliquary_delete_page", "reliquary_add_memory"]
    assert [e["user_id"] for e in entries] == ["my_lord", "my_lord", "my_lord", "alice"]
    assert entries[1]["ids"].get("slug") == "pg"
    assert entries[2]["ids"].get("slug") == "pg"
