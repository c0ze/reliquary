"""Regression cases from the third review round of the November 2026 audit."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

# Captured at import, before the fixtures swap in their inline stand-in.
REAL_TO_THREAD = asyncio.to_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"review-round3"


class Crash(BaseException):
    """Stands in for the process dying at a given point."""


def run(coro):
    return asyncio.run(coro)


def run_threaded(monkeypatch, scenario, *, timeout=10.0):
    """Run ``scenario()`` with real worker threads on an executor that is shut
    down explicitly before the loop closes."""
    monkeypatch.setattr(asyncio, "to_thread", REAL_TO_THREAD)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="review-round3")

    async def main():
        asyncio.get_running_loop().set_default_executor(executor)
        try:
            return await asyncio.wait_for(scenario(), timeout)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    return asyncio.run(main())


def claude(proxy):
    return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]


async def call(proxy, tool, args):
    return await proxy.call_mcp_tool(claude(proxy), tool, args, can_write=True)


async def post_bytes(proxy, upload_id, data=PNG):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://t") as client:
        return await client.post(f"/uploads/{upload_id}", content=data,
                                 headers={"content-type": "image/png", "authorization": "Bearer claude-secret"})


async def new_upload(proxy, data=PNG):
    slot = (await call(proxy, "reliquary_create_image_upload", {}))["structuredContent"]["upload_id"]
    assert (await post_bytes(proxy, slot, data)).status_code == 201
    return slot


def _backend_down(*args, **kwargs):
    raise ConnectionError("qdrant down")


# 1a -- every marked slot is reconciled by upload_id before release ---------

def test_marked_slot_of_unknown_commit_history_is_forgotten_and_keeps_its_bytes(make_proxy, tmp_path):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = tmp_path / "state"
    first = make_proxy(memory=memory, state_dir=str(state))
    slot = run(new_upload(first))
    blob = first.pending_uploads[slot].blob_id
    # A build that marked slots but did not record its commit attempts wrote
    # the caption and stopped before handing the reference over.
    memory.add("cat", user_id="my_lord", metadata={"kind": "image", "blob_ref": blob, "upload_id": slot})
    saved = json.loads((state / "pending_uploads.json").read_text())
    saved[slot].pop("commit_users", None)
    saved[slot]["expires_at"] = 1
    (state / "pending_uploads.json").write_text(json.dumps(saved))

    restarted = make_proxy(memory=memory, state_dir=str(state))
    run(restarted._reap_expired_uploads())
    # Round 5: its history is unknown, so nothing is guessed: the record is
    # forgotten and the marker keeps the bytes (lint reports the leak).
    assert slot not in restarted.pending_uploads
    info = restarted.blobs.info(blob)
    assert info is not None and info.owners == [f"upload:{slot}"]


def test_expiry_at_runtime_reconciles_a_marked_slot_before_releasing(make_proxy, tmp_path):
    from conftest import FakeMemory

    memory = FakeMemory()
    proxy = make_proxy(memory=memory, state_dir=str(tmp_path / "state"))
    slot = run(new_upload(proxy))
    blob = proxy.pending_uploads[slot].blob_id
    caption = memory.add("cat", user_id="my_lord",
                         metadata={"kind": "image", "blob_ref": blob, "upload_id": slot})["results"][0]["id"]
    proxy.pending_uploads[slot].commit_users = ["my_lord"]  # the attempt was recorded before the write
    proxy.pending_uploads[slot].expires_at = 1
    run(call(proxy, "reliquary_add_memory", {"text": "an ordinary write reaps expired slots"}))
    assert slot not in proxy.pending_uploads
    assert proxy.blobs.info(blob).owners == [caption]


def test_abandoned_upload_is_still_released_at_expiry(proxy):
    slot = run(new_upload(proxy))
    blob = proxy.pending_uploads[slot].blob_id
    proxy.pending_uploads[slot].expires_at = 1
    run(call(proxy, "reliquary_add_memory", {"text": "an ordinary write reaps expired slots"}))
    assert slot not in proxy.pending_uploads
    assert proxy.blobs.info(blob) is None


# 1b -- a caption add that landed before Mem0 raised keeps its bytes ---------

def test_caption_add_that_landed_before_raising_keeps_the_blob(proxy, fake_memory, monkeypatch):
    slot = run(new_upload(proxy))
    blob = proxy.pending_uploads[slot].blob_id
    real_add = fake_memory.add

    def add_lands_then_fails(*args, **kwargs):
        real_add(*args, **kwargs)  # the vector is inserted, then the history write fails
        raise RuntimeError("history database is locked")

    monkeypatch.setattr(fake_memory, "add", add_lands_then_fails)
    result = run(call(proxy, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"}))
    [caption] = list(fake_memory._store)
    info = proxy.blobs.info(blob)
    assert info is not None and info.owners == [caption] and info.ref_count == 1
    assert slot not in proxy.pending_uploads
    assert result["structuredContent"].get("memory_id") == caption


def test_caption_add_that_never_landed_still_releases_the_blob(proxy, fake_memory, monkeypatch):
    slot = run(new_upload(proxy))
    blob = proxy.pending_uploads[slot].blob_id
    monkeypatch.setattr(fake_memory, "add", _backend_down)
    monkeypatch.setattr(fake_memory, "get_all", lambda *a, **k: {"results": []})
    result = run(call(proxy, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"}))
    assert result["isError"] is True
    assert proxy.blobs.info(blob) is None and slot not in proxy.pending_uploads


# 2 -- every matched page advances its invalidation counter ------------------

def test_invalidating_an_already_stale_page_still_advances_its_counter(proxy, monkeypatch):
    run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "v1", "derived_from": ["x"]}))
    marked = []

    def source_changes_then_index_fails(*args, **kwargs):
        if not marked:
            marked.append(True)
            proxy.pages.mark_stale_deriving_from(ids=["x"])  # the server's fan-out for a change to x
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(proxy.compiled_memory, "update", source_changes_then_index_fails)
    monkeypatch.setattr(proxy.compiled_memory, "add", source_changes_then_index_fails)
    result = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "v2", "status": "stale"}))
    assert result["isError"] is True
    assert "v1" in proxy.pages.read_body("pg")[0]
    assert proxy.pages.get("pg").status == "stale"


# 3 -- the image delete transaction is drained against cancellation ---------

def test_cancelled_image_delete_still_releases_the_blob(proxy, fake_memory, monkeypatch):
    added = run(call(proxy, "reliquary_add_image",
                     {"caption": "cat", "image_base64": base64.b64encode(PNG).decode()}))["structuredContent"]
    real_delete = fake_memory.delete
    started, release = threading.Event(), threading.Event()

    def slow_delete(memory_id):
        started.set()
        release.wait(5)
        return real_delete(memory_id)

    monkeypatch.setattr(fake_memory, "delete", slow_delete)

    async def scenario():
        task = asyncio.create_task(proxy.handle_delete_image_tool({"memory_id": added["memory_id"]}))
        deadline = time.monotonic() + 5
        while not started.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_threaded(monkeypatch, scenario)
    assert fake_memory.get(added["memory_id"]) is None
    assert proxy.blobs.info(added["blob_id"]) is None


# 4 -- ingest matches and marks under one registry lock ---------------------

def test_ingest_fan_out_does_not_stale_a_revision_filed_after_the_match(tmp_path, monkeypatch):
    from blobs import BlobStore
    from compiled import PageRegistry
    from ingest import _fan_out_bulk

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    registry.put_revision("p", "v1", {"derived_from": ["x"], "status": "current"})
    real_get = registry.get
    refiler = []

    def get_then_compile_lands(slug):
        info = real_get(slug)
        if slug == "p" and not refiler:
            # A compile replaces p (now deriving only from y) right after it was read.
            thread = threading.Thread(target=registry.put_revision,
                                      args=("p", "v2", {"derived_from": ["y"], "status": "current"}))
            refiler.append(thread)
            thread.start()
            thread.join(0.3)
        return info

    monkeypatch.setattr(registry, "get", get_then_compile_lands)
    _fan_out_bulk(registry, {"x"}, set())
    refiler[0].join(5)
    page = real_get("p")
    assert page.derived_from == ["y"]
    assert page.status == "current"


# 5 -- an unresolved interrupted commit survives expiry and restarts ---------

def _crash_between_caption_and_transfer(make_proxy, state, memory, monkeypatch):
    first = make_proxy(memory=memory, state_dir=state)

    def crash(*args, **kwargs):
        raise Crash()

    async def scenario():
        slot = await new_upload(first)
        monkeypatch.setattr(first.blobs, "transfer_owner", crash)
        with pytest.raises(Crash):
            await call(first, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"})
        return slot

    slot = run(scenario())
    monkeypatch.undo()
    [caption] = [mid for mid, rec in memory._store.items() if rec["metadata"].get("upload_id") == slot]
    return slot, caption


def test_unresolved_commit_record_survives_expiry_and_restart_until_reconciled(make_proxy, tmp_path, monkeypatch):
    import lint
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    slot, caption = _crash_between_caption_and_transfer(make_proxy, state, memory, monkeypatch)
    real_get_all = memory.get_all
    memory.get_all = _backend_down
    restarted = make_proxy(memory=memory, state_dir=state)  # cannot reconcile: backend down
    blob = restarted.pending_uploads[slot].blob_id
    restarted.pending_uploads[slot].expires_at = 1
    restarted._cleanup_expired_uploads()
    run(restarted._reap_expired_uploads())
    assert slot in restarted.pending_uploads  # kept: the commit outcome is unknown
    report = lint.build_report(compiled_dir=restarted.settings.compiled_dir, blob_dir=restarted.settings.blob_dir,
                               dataset_path=None, min_count=8, state_dir=state)
    assert report["orphan_blobs"] == []

    again = make_proxy(memory=memory, state_dir=state)  # still down at the next restart
    assert slot in again.pending_uploads
    memory.get_all = real_get_all                      # the backend recovers
    again._reap_retry_at = 0.0
    run(call(again, "reliquary_add_memory", {"text": "a later write retries the reconciliation"}))
    assert slot not in again.pending_uploads
    assert again.blobs.info(blob).owners == [caption]


def test_malformed_slot_is_forgotten_and_keeps_its_bytes(make_proxy, tmp_path, monkeypatch):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = tmp_path / "state"
    slot, caption = _crash_between_caption_and_transfer(make_proxy, str(state), memory, monkeypatch)
    saved = json.loads((state / "pending_uploads.json").read_text())
    saved[slot]["expires_at"] = None  # malformed
    (state / "pending_uploads.json").write_text(json.dumps(saved))
    blob = saved[slot]["blob_id"]
    restarted = make_proxy(memory=memory, state_dir=str(state))
    run(restarted._reap_expired_uploads())
    # Round 5: no guess about a record that cannot be read; the bytes stay.
    assert slot not in restarted.pending_uploads
    assert restarted.blobs.info(blob).owners == [f"upload:{slot}"]
    fetched = run(call(restarted, "reliquary_fetch_image", {"id": blob}))
    assert "error" not in fetched["structuredContent"]


def test_reaping_backs_off_while_the_memory_backend_is_down(proxy, fake_memory, monkeypatch):
    slot = run(new_upload(proxy))
    proxy.pending_uploads[slot].commit_users = ["my_lord"]  # an attempt may have written a caption
    proxy.pending_uploads[slot].expires_at = 1
    calls = []

    def down(*args, **kwargs):
        calls.append(1)
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(fake_memory, "get_all", down)
    run(proxy._reap_expired_uploads())
    run(proxy._reap_expired_uploads())  # within the back-off: no second lookup
    assert len(calls) == 1 and slot in proxy.pending_uploads
