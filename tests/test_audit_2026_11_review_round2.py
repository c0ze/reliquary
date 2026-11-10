"""Regression cases from the second review round of the November 2026 audit."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

# Captured at import, before the fixtures swap in their inline stand-in.
REAL_TO_THREAD = asyncio.to_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"review-round2"


class Crash(BaseException):
    """Stands in for the process dying at a given point."""


def run(coro):
    return asyncio.run(coro)


def run_threaded(monkeypatch, scenario, *, timeout=10.0):
    """Run ``scenario()`` with real worker threads on an executor that is shut
    down explicitly before the loop closes."""
    monkeypatch.setattr(asyncio, "to_thread", REAL_TO_THREAD)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="review-round2")

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


def _legacy_slot(upload_id, blob_id, *, expires_at=1e12):
    return {"id": upload_id, "created_at": 1.0, "expires_at": expires_at, "expected_mimetype": "image/png",
            "expected_size": None, "filename": None, "blob_id": blob_id, "mimetype": "image/png",
            "size": len(PNG), "profile": "claude"}


# a -- marked slots are reconciled before their reference is released -------

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


def test_restart_hands_a_marked_slot_reference_to_its_existing_caption(make_proxy, tmp_path, monkeypatch):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    slot, caption = _crash_between_caption_and_transfer(make_proxy, state, memory, monkeypatch)
    restarted = make_proxy(memory=memory, state_dir=state)
    blob = restarted.pending_uploads[slot].blob_id
    assert restarted.blobs.info(blob).owners == [caption]
    restarted.pending_uploads[slot].expires_at = 1
    restarted._cleanup_expired_uploads()
    info = restarted.blobs.info(blob)
    assert info is not None and info.owners == [caption] and info.ref_count == 1
    fetched = run(call(restarted, "reliquary_fetch_image", {"id": blob}))
    assert "error" not in fetched["structuredContent"]


def test_retried_commit_after_that_crash_reports_already_committed(make_proxy, tmp_path, monkeypatch):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    slot, caption = _crash_between_caption_and_transfer(make_proxy, state, memory, monkeypatch)
    restarted = make_proxy(memory=memory, state_dir=state)
    retry = run(call(restarted, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "again"}))
    assert retry["structuredContent"]["error"] == "already_committed"
    assert len(memory._store) == 1


def test_crash_before_the_caption_landed_leaves_an_ordinary_pending_slot(make_proxy, tmp_path, monkeypatch):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    first = make_proxy(memory=memory, state_dir=state)

    def crash(*args, **kwargs):
        raise Crash()

    async def scenario():
        slot = await new_upload(first)
        monkeypatch.setattr(memory, "add", crash)
        with pytest.raises(Crash):
            await call(first, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"})
        return slot

    slot = run(scenario())
    monkeypatch.undo()
    restarted = make_proxy(memory=memory, state_dir=state)
    blob = restarted.pending_uploads[slot].blob_id
    committed = run(call(restarted, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"}))
    assert "error" not in committed["structuredContent"], committed
    info = restarted.blobs.info(blob)
    assert info.owners == [committed["structuredContent"]["memory_id"]] and info.ref_count == 1


def test_malformed_marked_slot_releases_only_its_own_reference(make_proxy, tmp_path):
    from blobs import BlobStore

    store = BlobStore(str(tmp_path / "blobs"), b"test-signing-key")
    blob = store.put(PNG, owner="upload:upl_bad").id  # the malformed slot's marked reference
    store.put(PNG)                                     # a valid legacy slot's unmarked reference
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({
        "upl_bad": {**_legacy_slot("upl_bad", blob), "expires_at": None},
        "upl_good": _legacy_slot("upl_good", blob)}))
    proxy = make_proxy(state_dir=str(state))
    info = proxy.blobs.info(blob)
    assert info is not None and info.ref_count == 1 and info.owners == ["upload:upl_good"]


# b -- must-do steps run before the awaited staleness fan-out ---------------

def _cancel_fan_out(proxy, monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()  # the request is cancelled while fan-out waits

    monkeypatch.setattr(proxy, "_fan_out_staleness", cancelled)


def test_delete_image_releases_the_blob_before_fan_out(proxy, monkeypatch):
    added = run(call(proxy, "reliquary_add_image",
                     {"caption": "cat", "image_base64": base64.b64encode(PNG).decode()}))["structuredContent"]
    _cancel_fan_out(proxy, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        run(call(proxy, "reliquary_delete_image", {"memory_id": added["memory_id"]}))
    assert proxy.memory.get(added["memory_id"]) is None
    assert proxy.blobs.info(added["blob_id"]) is None  # no blob owned by a deleted memory


def test_commit_drops_its_slot_before_fan_out(proxy, monkeypatch):
    slot = run(new_upload(proxy))
    _cancel_fan_out(proxy, monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        run(call(proxy, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat"}))
    assert slot not in proxy.pending_uploads


# c -- dependency match and stale mark happen together ----------------------

def test_fan_out_does_not_stale_a_revision_filed_after_the_match(proxy, monkeypatch):
    run(call(proxy, "reliquary_compile_page", {"slug": "p", "markdown": "v1", "derived_from": ["x"]}))
    real_match = proxy.pages.pages_deriving_from
    refiler = []

    def match_then_compile_lands(**kwargs):
        matches = real_match(**kwargs)
        if not refiler:
            # A compile replaces p (now deriving only from y) right after the match.
            thread = threading.Thread(target=proxy.pages.put_revision,
                                      args=("p", "v2", {"derived_from": ["y"], "status": "current"}))
            refiler.append(thread)
            thread.start()
            thread.join(0.3)
        return matches

    monkeypatch.setattr(proxy.pages, "pages_deriving_from", match_then_compile_lands)
    run(proxy._fan_out_staleness(["x"], {}))
    refiler[0].join(5)
    page = proxy.pages.get("p")
    assert page.derived_from == ["y"]
    assert page.status == "current"


# d -- rollback keeps an invalidation even when the attempt was filed stale --

def test_rollback_of_a_stale_filed_revision_keeps_a_concurrent_invalidation(proxy, monkeypatch):
    run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "v1"}))

    marked = []

    def fails_after_a_source_change(*args, **kwargs):
        if not marked:
            marked.append(True)
            proxy.pages.set_status("pg", "stale")  # a live write invalidates the page meanwhile
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(proxy.compiled_memory, "update", fails_after_a_source_change)
    monkeypatch.setattr(proxy.compiled_memory, "add", fails_after_a_source_change)
    result = run(call(proxy, "reliquary_compile_page", {"slug": "pg", "markdown": "v2", "status": "stale"}))
    assert result["isError"] is True
    assert "v1" in proxy.pages.read_body("pg")[0]
    assert proxy.pages.get("pg").status == "stale"


def test_page_json_without_the_invalidation_counter_loads(tmp_path):
    from blobs import BlobStore
    from compiled import PageRegistry

    registry = PageRegistry(str(tmp_path / "compiled"), BlobStore(str(tmp_path / "blobs"), b"k"))
    registry.put_revision("pg", "v1", {})
    path = Path(registry._path("pg"))
    data = json.loads(path.read_text())
    data.pop("invalidations")
    path.write_text(json.dumps(data))
    assert registry.get("pg").invalidations == 0
    assert registry.set_status("pg", "stale").invalidations == 1
