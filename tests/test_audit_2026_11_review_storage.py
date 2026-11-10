"""Regression cases from the review of the merged November 2026 audit branches
(storage findings)."""

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

# Captured at import, before the fixtures swap in their inline stand-in, so the
# thread tests below can put real worker threads back.
REAL_TO_THREAD = asyncio.to_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"review-storage"


def run(coro):
    return asyncio.run(coro)


def run_threaded(monkeypatch, scenario, *, timeout=10.0):
    """Run ``scenario()`` with real worker threads on an explicitly owned
    executor that is shut down before the loop closes (no reliance on the
    default executor's shutdown)."""
    monkeypatch.setattr(asyncio, "to_thread", REAL_TO_THREAD)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="review-storage")

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


async def commit_and_crash(proxy, slot, caption="cat"):
    """Commit, but crash before the slot removal is persisted."""
    real_drop = proxy._drop_upload
    proxy._drop_upload = lambda upload_id: None
    try:
        return (await call(proxy, "reliquary_commit_image_upload",
                           {"upload_id": slot, "caption": caption}))["structuredContent"]
    finally:
        proxy._drop_upload = real_drop


# 1 -- an upload slot consumes only its own blob reference -------------------

def test_expired_committed_slot_does_not_eat_another_images_reference(make_proxy, tmp_path):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    first = make_proxy(memory=memory, state_dir=state)

    async def before_crash():
        slot = await new_upload(first)
        return slot, await commit_and_crash(first, slot)

    slot, committed = run(before_crash())
    restarted = make_proxy(memory=memory, state_dir=state)
    restarted.pending_uploads[slot].expires_at = 1  # it expires after the restart

    async def after_restart():
        added = await call(restarted, "reliquary_add_image",
                           {"caption": "same bytes", "image_base64": base64.b64encode(PNG).decode()})
        await call(restarted, "reliquary_delete_image", {"memory_id": committed["memory_id"]})
        return added["structuredContent"], await call(restarted, "reliquary_fetch_image",
                                                       {"id": committed["blob_id"]})

    added, fetched = run(after_restart())
    assert "error" not in fetched["structuredContent"], fetched["structuredContent"]
    info = restarted.blobs.info(committed["blob_id"])
    assert info.owners == [added["memory_id"]] and info.ref_count == 1


def test_replay_guard_is_per_slot_when_slots_share_bytes(make_proxy, tmp_path):
    from conftest import FakeMemory

    memory = FakeMemory()
    state = str(tmp_path / "state")
    first = make_proxy(memory=memory, state_dir=state)

    async def before_crash():
        slot_a = await new_upload(first)
        slot_b = await new_upload(first)
        return slot_a, slot_b, await commit_and_crash(first, slot_a)

    slot_a, slot_b, committed = run(before_crash())
    restarted = make_proxy(memory=memory, state_dir=state)

    async def after_restart():
        retry = await call(restarted, "reliquary_commit_image_upload", {"upload_id": slot_a, "caption": "retry"})
        other = await call(restarted, "reliquary_commit_image_upload", {"upload_id": slot_b, "caption": "dog"})
        return retry["structuredContent"], other["structuredContent"]

    retry, other = run(after_restart())
    assert retry.get("error") == "already_committed"
    assert "error" not in other, other
    info = restarted.blobs.info(committed["blob_id"])
    assert sorted(info.owners) == sorted([committed["memory_id"], other["memory_id"]])
    assert info.ref_count == 2


def _legacy_slot(upload_id, blob_id, *, expires_at=1e12):
    # pending_uploads.json as the deployed version writes it (no owner marker).
    return {"id": upload_id, "created_at": 1.0, "expires_at": expires_at, "expected_mimetype": "image/png",
            "expected_size": None, "filename": None, "blob_id": blob_id, "mimetype": "image/png",
            "size": len(PNG), "profile": "claude"}


def test_legacy_slots_are_reconciled_against_caption_upload_ids(make_proxy, tmp_path):
    from blobs import BlobStore
    from conftest import FakeMemory

    memory = FakeMemory()
    store = BlobStore(str(tmp_path / "blobs"), b"test-signing-key")
    blob = store.put(PNG).id   # slot X's reference, already handed to its caption
    store.put(PNG)             # slot Y's reference, never committed
    caption = memory.add("cat", user_id="my_lord",
                         metadata={"kind": "image", "blob_ref": blob, "upload_id": "upl_X"})["results"][0]["id"]
    store.register_owner(blob, caption)
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({
        "upl_X": _legacy_slot("upl_X", blob), "upl_Y": _legacy_slot("upl_Y", blob)}))

    proxy = make_proxy(memory=memory, state_dir=str(state))
    assert sorted(proxy.blobs.info(blob).owners) == sorted([caption, "upload:upl_Y"])
    retry = run(call(proxy, "reliquary_commit_image_upload", {"upload_id": "upl_X", "caption": "again"}))
    assert retry["structuredContent"]["error"] == "already_committed"
    proxy.pending_uploads["upl_Y"].expires_at = 1
    proxy._cleanup_expired_uploads()
    info = proxy.blobs.info(blob)
    assert info.owners == [caption] and info.ref_count == 1


def test_legacy_slot_that_cannot_be_reconciled_never_releases_bytes(make_proxy, tmp_path, monkeypatch):
    from blobs import BlobStore
    from conftest import FakeMemory

    memory = FakeMemory()
    store = BlobStore(str(tmp_path / "blobs"), b"test-signing-key")
    blob = store.put(PNG).id
    store.put(PNG)
    caption = memory.add("cat", user_id="my_lord", metadata={"upload_id": "upl_X"})["results"][0]["id"]
    store.register_owner(blob, caption)
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({"upl_X": _legacy_slot("upl_X", blob)}))

    def down(memory_id):
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(memory, "get", down)
    proxy = make_proxy(memory=memory, state_dir=str(state))
    proxy.pending_uploads["upl_X"].expires_at = 1
    proxy._cleanup_expired_uploads()
    assert proxy.blobs.info(blob).ref_count == 2  # leaked (lint shows it), never unlinked


def test_lint_counts_a_slot_marker_only_while_its_slot_is_pending(tmp_path):
    import lint
    from blobs import BlobStore

    store = BlobStore(str(tmp_path / "blobs"), b"k")
    blob = store.put(PNG, owner="upload:upl_gone").id
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text("{}")
    report = lint.build_report(compiled_dir=str(tmp_path / "compiled"), blob_dir=str(tmp_path / "blobs"),
                               dataset_path=None, min_count=8, state_dir=str(state))
    assert [item["id"] for item in report["orphan_blobs"]] == [blob]
    (state / "pending_uploads.json").write_text(json.dumps({"upl_gone": _legacy_slot("upl_gone", blob)}))
    report = lint.build_report(compiled_dir=str(tmp_path / "compiled"), blob_dir=str(tmp_path / "blobs"),
                               dataset_path=None, min_count=8, state_dir=str(state))
    assert report["orphan_blobs"] == []
