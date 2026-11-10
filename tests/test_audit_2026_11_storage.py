"""Regression cases from the November 2026 storage/state audit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest


PNG = b"\x89PNG\r\n\x1a\n" + b"storage-audit"


def run(coro):
    return asyncio.run(coro)


def claude(proxy):
    return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]


async def call(proxy, tool, args):
    return await proxy.call_mcp_tool(claude(proxy), tool, args, can_write=True)


def sidecars(proxy):
    return sorted(p.stem for p in Path(proxy.blobs.blob_dir).rglob("*.json"))


async def post_bytes(proxy, upload_id, data=PNG):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://t") as client:
        return await client.post(f"/uploads/{upload_id}", content=data,
                                 headers={"content-type": "image/png", "authorization": "Bearer claude-secret"})


# 1 -- delete_page must not wedge once the page's vector is already gone -----

def test_delete_page_succeeds_when_vector_already_absent(proxy):
    async def scenario():
        filed = await call(proxy, "reliquary_compile_page", {"slug": "p", "markdown": "body"})
        memory_id = filed["structuredContent"]["memory_id"]
        # Collection rebuilt/restored, or a crash after an earlier vector delete.
        del proxy.compiled_memory._store[memory_id]
        return await call(proxy, "reliquary_delete_page", {"slug": "p"})

    result = run(scenario())
    assert result["isError"] is False, result
    assert result["structuredContent"]["deleted"] is True
    assert proxy.pages.get("p") is None


def test_delete_page_still_aborts_when_vector_survives_a_failed_delete(proxy, monkeypatch):
    async def scenario():
        await call(proxy, "reliquary_compile_page", {"slug": "p", "markdown": "body"})

        def boom(memory_id):
            raise ConnectionError("qdrant down")

        monkeypatch.setattr(proxy.compiled_memory, "delete", boom)
        return await call(proxy, "reliquary_delete_page", {"slug": "p"})

    result = run(scenario())
    assert result["structuredContent"]["error"] == "index_delete_failed"
    assert proxy.pages.get("p") is not None


# 2 -- an indexing failure must not leave a committed, unindexed revision ---

def _fail_compiled_index(proxy, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(proxy.compiled_memory, "add", boom)
    monkeypatch.setattr(proxy.compiled_memory, "update", boom)


def test_compile_index_failure_does_not_create_a_new_page(proxy, monkeypatch):
    _fail_compiled_index(proxy, monkeypatch)
    result = run(call(proxy, "reliquary_compile_page", {"slug": "newpage", "markdown": "body", "domain": "d"}))
    assert result["isError"] is True
    assert proxy.pages.get("newpage") is None
    assert result["structuredContent"]["error"] == "index_failed"
    # The revision blob's reference was released, not leaked.
    assert sidecars(proxy) == []


def test_compile_index_failure_restores_the_existing_page(proxy, monkeypatch):
    first = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "old text"}))
    before = proxy.pages.get("pg")
    _fail_compiled_index(proxy, monkeypatch)
    second = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "new text"}))
    assert proxy.pages.get("pg") == before
    assert "old text" in proxy.pages.read_body("pg")[0]
    assert second["structuredContent"]["error"] == "index_failed"
    assert "old text" in proxy.compiled_memory._store[first["structuredContent"]["memory_id"]]["memory"]
    assert sidecars(proxy) == [before.current_blob]  # the new revision's bytes were released
    assert proxy.blobs.info(before.current_blob).ref_count == 1


def test_needs_review_and_lint_report_current_page_without_index(proxy, tmp_path):
    proxy.pages.put_revision("broken", "body", {"status": "current"})  # e.g. filed before this fix
    payload = json.loads(proxy.read_resource("reliquary://needs-review")["contents"][0]["text"])
    assert [item["slug"] for item in payload["unindexed"]] == ["broken"]

    import lint
    report = lint.build_report(compiled_dir=proxy.settings.compiled_dir, blob_dir=proxy.settings.blob_dir,
                               dataset_path=None, min_count=8)
    assert [item["slug"] for item in report["unindexed_pages"]] == ["broken"]


# 5 -- a re-file without derived_from/title keeps them ----------------------

def test_refile_without_provenance_keeps_it_and_still_goes_stale(proxy):
    async def scenario():
        added = await call(proxy, "reliquary_add_memory", {"text": "source fact"})
        source = added["structuredContent"]["ids"][0]
        await call(proxy, "reliquary_compile_page", {"slug": "syn", "title": "Syn Title", "markdown": "v1",
                                                     "derived_from": [source], "supersedes": ["older"]})
        refiled = await call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v2"})
        page = proxy.pages.get("syn")
        await call(proxy, "reliquary_update", {"id": source, "text": "source fact changed"})
        return source, refiled, page, proxy.pages.get("syn")

    source, refiled, page, final = run(scenario())
    assert page.derived_from == [source]
    assert page.supersedes == ["older"]
    assert page.title == "Syn Title"
    assert refiled["structuredContent"]["derived_from"] == [source]
    indexed = proxy.compiled_memory._store[refiled["structuredContent"]["memory_id"]]
    assert indexed["metadata"]["derived_from"] == [source]
    assert indexed["memory"].startswith("Syn Title")
    assert final.status == "stale"


def test_refile_with_explicit_empty_lists_clears_provenance(proxy):
    run(call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v1",
                                               "derived_from": ["m1"], "supersedes": ["older"]}))
    run(call(proxy, "reliquary_compile_page", {"slug": "syn", "markdown": "v2",
                                               "derived_from": [], "supersedes": []}))
    page = proxy.pages.get("syn")
    assert page.derived_from == [] and page.supersedes == []
    assert page.title == "syn"  # a new page with no title still defaults to its slug


# 4 -- over-long slugs: no leaked blob, no raw OSError ----------------------

def test_long_title_is_rejected_before_any_blob_is_written(proxy):
    result = run(call(proxy, "reliquary_compile_page", {"title": "word " * 60, "markdown": "x"}))
    assert result["structuredContent"]["error"] == "slug_too_long"
    assert sidecars(proxy) == []


def test_fetch_of_an_over_long_id_is_not_found(proxy):
    run(call(proxy, "reliquary_compile_page", {"slug": "aa-page", "markdown": "x"}))  # shard dir "aa" exists
    result = run(call(proxy, "reliquary_fetch", {"id": "a" * 300}))
    assert result["structuredContent"]["error"] == "not_found"


def test_registry_releases_the_blob_when_the_page_cannot_be_saved(tmp_path, monkeypatch):
    from blobs import BlobStore
    from compiled import MAX_SLUG_LENGTH, PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    with pytest.raises(ValueError):
        registry.put_revision("w" * (MAX_SLUG_LENGTH + 1), "body", {})

    def disk_full(info):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(registry, "_save", disk_full)
    with pytest.raises(OSError):
        registry.put_revision("ok", "body", {})
    assert list((tmp_path / "blobs").rglob("*.json")) == []


def test_existing_page_over_the_slug_cap_can_still_be_refiled(proxy, monkeypatch):
    import compiled

    run(call(proxy, "reliquary_compile_page", {"slug": "legacy-long-slug", "markdown": "v1"}))
    monkeypatch.setattr(compiled, "MAX_SLUG_LENGTH", 8)
    refiled = run(call(proxy, "reliquary_compile_page", {"slug": "legacy-long-slug", "markdown": "v2"}))
    assert refiled["isError"] is False
    fresh = run(call(proxy, "reliquary_compile_page", {"slug": "another-long-one", "markdown": "v1"}))
    assert fresh["structuredContent"]["error"] == "slug_too_long"


# 3 -- a new page slug must not shadow an existing record id ----------------

def test_new_page_cannot_shadow_a_catalog_record(proxy):
    from catalog import CorpusCatalog, CorpusRecord

    proxy.catalog = CorpusCatalog([CorpusRecord(import_record_id="nas-backup-plan", title="NAS backup plan",
                                                text="the imported record", metadata={"domain": "infra"},
                                                source_ref="")])
    result = run(call(proxy, "reliquary_compile_page", {"title": "NAS backup plan", "markdown": "synthesis"}))
    assert proxy.pages.get("nas-backup-plan") is None
    assert result["structuredContent"]["error"] == "slug_conflict"
    fetched = run(call(proxy, "reliquary_fetch", {"id": "nas-backup-plan"}))
    assert fetched["structuredContent"]["text"] == "the imported record"


def test_new_page_cannot_shadow_a_live_memory(proxy):
    added = run(call(proxy, "reliquary_add_memory", {"text": "live note"}))
    memory_id = added["structuredContent"]["ids"][0]
    result = run(call(proxy, "reliquary_compile_page", {"slug": memory_id, "markdown": "synthesis"}))
    assert proxy.pages.get(memory_id) is None
    assert result["structuredContent"]["error"] == "slug_conflict"


def test_existing_page_can_be_refiled_under_its_own_slug(proxy, fake_memory):
    run(call(proxy, "reliquary_compile_page", {"slug": "fake-1", "markdown": "v1"}))
    run(call(proxy, "reliquary_add_memory", {"text": "now a live memory has the same id"}))
    assert fake_memory.get("fake-1") is not None
    refiled = run(call(proxy, "reliquary_compile_page", {"slug": "fake-1", "markdown": "v2"}))
    assert refiled["isError"] is False


# 6 -- embedded on-disk Qdrant: the compiled Memory shares the raw client ----

EMBEDDER = {"provider": "openai", "config": {"model": "text-embedding-3-small"}}


def test_compiled_memory_shares_the_embedded_qdrant_client(tmp_path, monkeypatch):
    import sys
    import types

    from conftest import FakeMemory
    from server import ProxySettings, ReliquaryChatProxy

    built = []

    class StubMemory:
        @staticmethod
        def from_config(config):
            built.append(config)
            return FakeMemory()

    monkeypatch.setitem(sys.modules, "mem0", types.SimpleNamespace(Memory=StubMemory))
    raw = FakeMemory()
    raw.vector_store = types.SimpleNamespace(client=object(), is_local=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"vector_store": {"provider": "qdrant", "config": {
        "path": str(tmp_path / "qdrant"), "collection_name": "reliquary"}}, "embedder": EMBEDDER}))
    ReliquaryChatProxy(ProxySettings(config_path=str(cfg), blob_dir=str(tmp_path / "blobs"),
                                     compiled_dir=str(tmp_path / "compiled"), native_hybrid="off"),
                       memory=raw)
    [compiled_config] = built
    assert compiled_config["vector_store"]["config"]["client"] is raw.vector_store.client
    assert compiled_config["vector_store"]["config"]["collection_name"] == "reliquary_compiled"


def test_compiled_memory_keeps_its_own_client_for_a_qdrant_server(tmp_path, monkeypatch):
    import sys
    import types

    from conftest import FakeMemory
    from server import ProxySettings, ReliquaryChatProxy

    built = []
    monkeypatch.setitem(sys.modules, "mem0", types.SimpleNamespace(
        Memory=types.SimpleNamespace(from_config=lambda config: built.append(config) or FakeMemory())))
    raw = FakeMemory()
    raw.vector_store = types.SimpleNamespace(client=object(), is_local=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"vector_store": {"provider": "qdrant", "config": {"host": "qdrant", "port": 6333}}, "embedder": EMBEDDER}))
    ReliquaryChatProxy(ProxySettings(config_path=str(cfg), blob_dir=str(tmp_path / "blobs"),
                                     compiled_dir=str(tmp_path / "compiled"), native_hybrid="off"),
                       memory=raw)
    assert "client" not in built[0]["vector_store"]["config"]


_EMBEDDED_SCRIPT = r"""
import json, os, sys
d = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from server import ProxySettings, ReliquaryChatProxy
cfg = os.path.join(d, "config.json")
with open(cfg, "w") as fh:
    json.dump({
        "vector_store": {"provider": "qdrant", "config": {"path": os.path.join(d, "qdrant"), "on_disk": True,
                                                           "collection_name": "reliquary", "embedding_model_dims": 8}},
        "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small", "embedding_dims": 8}},
        "history_db_path": os.path.join(d, "history.db"),
    }, fh)
proxy = ReliquaryChatProxy(ProxySettings(config_path=cfg, blob_dir=os.path.join(d, "blobs"),
                                         compiled_dir=os.path.join(d, "compiled"), native_hybrid="off"))
assert proxy.compiled_memory.vector_store.client is proxy.memory.vector_store.client
print("ok")
"""


def test_server_starts_on_embedded_qdrant_with_the_real_mem0(tmp_path):
    import os
    import subprocess
    import sys

    pytest.importorskip("mem0")
    app_dir = str(Path(__file__).resolve().parents[1] / "app")
    env = dict(os.environ, MEM0_TELEMETRY="False", MEM0_DIR=str(tmp_path / "mem0home"),
               OPENAI_API_KEY="sk-not-used", HOME=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", _EMBEDDED_SCRIPT, str(tmp_path), app_dir],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("ok")


# 7 -- every atomic replace is fsynced, then its directory -------------------

def _record_durability(monkeypatch):
    import os
    import stat

    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append((kind, None))
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return events


def _assert_durable(events, *suffixes):
    replaces = [i for i, (kind, _dst) in enumerate(events) if kind == "replace"]
    assert replaces, events
    for i in replaces:
        assert events[i - 1][0] == "file", events  # data fsynced before the rename
    assert events[-1][0] == "dir", events          # the rename itself made durable
    written = [dst for kind, dst in events if kind == "replace"]
    for suffix in suffixes:
        assert any(dst.endswith(suffix) for dst in written), (suffix, written)


def test_blob_put_fsyncs_bytes_and_sidecar(tmp_path, monkeypatch):
    from blobs import BlobStore

    store = BlobStore(str(tmp_path / "blobs"), b"k")
    events = _record_durability(monkeypatch)
    store.put(PNG)
    _assert_durable(events, ".png", ".json")


def test_page_registry_save_is_fsynced(tmp_path, monkeypatch):
    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    events = _record_durability(monkeypatch)
    registry.put_revision("page", "body", {})
    _assert_durable(events, "page.json")


def test_json_file_store_save_is_fsynced(tmp_path, monkeypatch):
    from persistence import JsonFileStore

    events = _record_durability(monkeypatch)
    JsonFileStore(str(tmp_path / "state" / "oauth_tokens.json")).save({"a": 1})
    _assert_durable(events, "oauth_tokens.json")


# 15 -- registry writes from two processes cannot collide --------------------

def test_registry_writes_use_unique_temp_names(tmp_path, monkeypatch):
    import os

    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    registry.put_revision("page", "body", {})
    sources = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (sources.append(str(src)), real_replace(src, dst))[1])
    registry.set_status("page", "stale")
    registry.set_status("page", "current")
    assert len(sources) == len(set(sources)) == 2


def test_registry_mutations_hold_a_cross_process_lock(tmp_path):
    import os

    fcntl = pytest.importorskip("fcntl")
    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    registry.put_revision("page", "body", {})
    other = os.open(str(tmp_path / "compiled" / ".lock"), os.O_RDWR)
    try:
        with registry._locked():
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released afterwards
    finally:
        os.close(other)
    assert [p.slug for p in registry.list()] == ["page"]


# 8 -- an identical re-file heals a lost sidecar instead of unlinking bytes --

def test_identical_refile_restores_a_page_whose_sidecar_was_lost(proxy):
    args = {"slug": "heal", "title": "Heal", "markdown": "body"}
    filed = run(call(proxy, "reliquary_compile_page", dict(args)))
    blob = filed["structuredContent"]["revision"]
    Path(proxy.blobs._sidecar_path(blob)).write_text("")  # zero-length after a power loss
    assert run(call(proxy, "reliquary_fetch", {"id": "heal"}))["structuredContent"]["error"] == "blob_missing"

    refiled = run(call(proxy, "reliquary_compile_page", dict(args)))
    assert refiled["isError"] is False
    fetched = run(call(proxy, "reliquary_fetch", {"id": "heal"}))
    assert "error" not in fetched["structuredContent"]
    assert proxy.blobs.info(blob).ref_count == 1


def test_identical_refile_still_releases_its_extra_reference(proxy):
    args = {"slug": "same", "markdown": "body"}
    blob = run(call(proxy, "reliquary_compile_page", dict(args)))["structuredContent"]["revision"]
    run(call(proxy, "reliquary_compile_page", dict(args)))
    assert proxy.blobs.info(blob).ref_count == 1


# 9 -- an upload slot that outlived its commit cannot be committed again -----

def _commit_then_crash_before_slot_drop(make_proxy, state, memory):
    first = make_proxy(memory=memory, state_dir=state)

    async def scenario():
        slot = (await call(first, "reliquary_create_image_upload", {}))["structuredContent"]["upload_id"]
        assert (await post_bytes(first, slot)).status_code == 201
        first._drop_upload = lambda upload_id: None  # crash before the drop is persisted
        committed = await call(first, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"})
        return slot, committed["structuredContent"]

    return run(scenario())


def test_retried_commit_after_a_crash_does_not_double_own_the_blob(make_proxy, tmp_path):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    slot, committed = _commit_then_crash_before_slot_drop(make_proxy, state, memory)
    restarted = make_proxy(memory=memory, state_dir=state)
    retry = run(call(restarted, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat (retry)"}))
    info = restarted.blobs.info(committed["blob_id"])
    assert info.owners == [committed["memory_id"]] and info.ref_count == 1
    assert retry["structuredContent"]["error"] == "already_committed"
    assert slot not in restarted.pending_uploads
    assert len(memory._store) == 1


def test_failed_retry_after_a_crash_keeps_the_committed_bytes(make_proxy, tmp_path, monkeypatch):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    slot, committed = _commit_then_crash_before_slot_drop(make_proxy, state, memory)
    restarted = make_proxy(memory=memory, state_dir=state)

    def down(*args, **kwargs):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(memory, "add", down)
    run(call(restarted, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "retry"}))
    assert restarted.blobs.get(committed["blob_id"]) is not None


# 11 -- a type-invalid pending_uploads.json neither crashes nor leaks --------

def test_startup_skips_type_invalid_upload_slots(make_proxy, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({
        "upl_null": {"id": "upl_null", "created_at": 1.0, "expires_at": None},
        "upl_str": {"id": "upl_str", "created_at": "yesterday", "expires_at": 1e12},
        "upl_path": {"id": "upl_path", "created_at": 1.0, "expires_at": 1e12, "blob_id": "../../etc/passwd"},
    }))
    proxy = make_proxy(state_dir=str(state))
    assert proxy.pending_uploads == {}
    assert json.loads((state / "pending_uploads.json").read_text()) == {}


def test_startup_releases_the_blob_of_an_unusable_slot(make_proxy, tmp_path):
    from blobs import BlobStore

    state = tmp_path / "state"
    state.mkdir()
    blob = BlobStore(str(tmp_path / "blobs"), b"test-signing-key").put(PNG)  # the slot's reference
    (state / "pending_uploads.json").write_text(json.dumps({
        "upl_bad": {"id": "upl_bad", "created_at": 1.0, "expires_at": None, "blob_id": blob.id}}))
    proxy = make_proxy(state_dir=str(state))
    assert proxy.blobs.info(blob.id) is None


def test_startup_keeps_a_slot_with_an_unknown_field(make_proxy, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({
        "upl_new": {"id": "upl_new", "created_at": 1.0, "expires_at": 1e12, "added_later": True}}))
    proxy = make_proxy(state_dir=str(state))
    assert proxy.pending_uploads["upl_new"].expires_at == 1e12


# 12 -- image writes mark dependent pages stale, like add_memory ------------

def test_add_image_marks_same_topic_page_stale(proxy):
    import base64

    run(call(proxy, "reliquary_compile_page", {"slug": "topicpage", "markdown": "x", "domain": "d", "topic": "t"}))
    run(call(proxy, "reliquary_add_image", {"caption": "new evidence", "domain": "d", "topic": "t",
                                            "image_base64": base64.b64encode(PNG).decode()}))
    assert proxy.pages.get("topicpage").status == "stale"


def test_commit_image_upload_marks_same_topic_page_stale(proxy):
    async def scenario():
        await call(proxy, "reliquary_compile_page", {"slug": "topicpage", "markdown": "x", "domain": "d", "topic": "t"})
        slot = (await call(proxy, "reliquary_create_image_upload", {}))["structuredContent"]["upload_id"]
        assert (await post_bytes(proxy, slot)).status_code == 201
        committed = await call(proxy, "reliquary_commit_image_upload",
                               {"upload_id": slot, "caption": "new evidence", "domain": "d", "topic": "t"})
        assert committed["isError"] is False

    run(scenario())
    assert proxy.pages.get("topicpage").status == "stale"


# 10 -- abandoned uploads are reaped on writes; lint reports orphan blobs ----

def test_write_tools_reap_expired_upload_slots(proxy):
    async def scenario():
        slot = (await call(proxy, "reliquary_create_image_upload", {}))["structuredContent"]["upload_id"]
        blob = (await post_bytes(proxy, slot)).json()["blob_id"]
        proxy.pending_uploads[slot].expires_at = 1  # long past expiry
        await call(proxy, "reliquary_add_memory", {"text": "ordinary write"})
        return slot, blob

    slot, blob = run(scenario())
    assert slot not in proxy.pending_uploads
    assert proxy.blobs.info(blob) is None


def test_lint_reports_orphan_blobs_but_not_referenced_ones(make_proxy, tmp_path):
    import base64

    import lint

    state = tmp_path / "state"
    proxy = make_proxy(state_dir=str(state))

    async def scenario():
        await call(proxy, "reliquary_compile_page", {"slug": "page", "markdown": "a page"})
        await call(proxy, "reliquary_add_image", {"caption": "owned", "image_base64": base64.b64encode(PNG).decode()})
        slot = (await call(proxy, "reliquary_create_image_upload", {}))["structuredContent"]["upload_id"]
        pending = (await post_bytes(proxy, slot, PNG + b"pending")).json()["blob_id"]
        return pending

    pending = run(scenario())
    leaked = proxy.blobs.put(PNG + b"leaked").id  # a reference nobody holds
    report = lint.build_report(compiled_dir=proxy.settings.compiled_dir, blob_dir=proxy.settings.blob_dir,
                               dataset_path=None, min_count=8, state_dir=str(state))
    assert [item["id"] for item in report["orphan_blobs"]] == [leaked]
    without_state = lint.build_report(compiled_dir=proxy.settings.compiled_dir, blob_dir=proxy.settings.blob_dir,
                                      dataset_path=None, min_count=8)
    assert {item["id"] for item in without_state["orphan_blobs"]} == {leaked, pending}


# 13 -- the retrieval-stats log is bounded and aggregated off the event loop --

def test_retrieval_stats_log_rotates_and_aggregates_both_files(tmp_path):
    from retrieval_stats import RetrievalStatsLog, aggregate

    path = tmp_path / "stats.jsonl"
    log = RetrievalStatsLog(str(path), clock=lambda: 1.0, max_bytes=2000)
    for i in range(200):
        log.record("search", [{"id": f"rec-{i}", "domain": "dev", "topic": "t"}])
    assert path.stat().st_size <= 2000 + 200
    assert (tmp_path / "stats.jsonl.1").stat().st_size <= 2000 + 200
    agg = aggregate(str(path))
    total_lines = sum(1 for _ in open(path)) + sum(1 for _ in open(f"{path}.1"))
    assert agg["events"] == total_lines < 200
    assert "rec-199" in agg["by_id"]


def test_resources_read_runs_off_the_event_loop(proxy, monkeypatch):
    offloaded = []
    inline = asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await inline(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://t") as client:
            return await client.post("/claude/mcp", headers={"authorization": "Bearer claude-secret"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "reliquary://needs-review"}})

    response = run(scenario())
    assert response.status_code == 200, response.text
    assert "unindexed" in response.json()["result"]["contents"][0]["text"]
    assert "read_resource" in offloaded


# 14 -- cancelling a waiting writer wakes the readers it was blocking --------

def test_cancelled_waiting_writer_wakes_blocked_readers():
    from runtime import AsyncRWLock

    async def scenario():
        lock = AsyncRWLock()
        release_first = asyncio.Event()
        second_in = asyncio.Event()

        async def first_reader():
            async with lock.read():
                await release_first.wait()

        async def writer():
            async with lock.write():
                pass

        async def second_reader():
            async with lock.read():
                second_in.set()

        reader = asyncio.create_task(first_reader())
        await asyncio.sleep(0.01)
        waiting_writer = asyncio.create_task(writer())
        await asyncio.sleep(0.01)
        blocked = asyncio.create_task(second_reader())
        await asyncio.sleep(0.01)
        assert not second_in.is_set()
        waiting_writer.cancel()
        await asyncio.wait_for(second_in.wait(), timeout=1)
        release_first.set()
        await asyncio.gather(reader, blocked)
        with pytest.raises(asyncio.CancelledError):
            await waiting_writer

    run(scenario())


# 16 -- vault export prunes the files it wrote earlier, and only those -------

def test_export_vault_prunes_its_own_stale_files_only(tmp_path):
    import export_vault
    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    registry.put_revision("moved", "body", {"domain": "pagan"})
    registry.put_revision("gone", "body", {"domain": "infra"})
    registry.put_revision("kept", "body", {"domain": "pagan"})
    out = tmp_path / "vault"
    assert export_vault.export_vault(registry, str(out)) == 3
    (out / "pagan" / "my-own-note.md").write_text("written by the user")
    (out / "notes").mkdir()
    (out / "notes" / "gone.md").write_text("same name, not exported")

    registry.put_revision("moved", "body", {"domain": "music"})
    registry.delete("gone")
    assert export_vault.export_vault(registry, str(out)) == 2

    assert (out / "music" / "moved.md").exists()
    assert not (out / "pagan" / "moved.md").exists()
    assert not (out / "infra").exists()
    assert (out / "pagan" / "kept.md").exists()
    assert (out / "pagan" / "my-own-note.md").read_text() == "written by the user"
    assert (out / "notes" / "gone.md").exists()


def test_export_vault_ignores_hostile_manifest_entries(tmp_path):
    import export_vault
    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    out = tmp_path / "vault"
    out.mkdir()
    victim = tmp_path / "victim.md"
    victim.write_text("outside the vault")
    (out / export_vault.MANIFEST_NAME).write_text(json.dumps({"files": ["../victim.md", "x/../../victim.md"]}))
    export_vault.export_vault(registry, str(out))
    assert victim.exists()
