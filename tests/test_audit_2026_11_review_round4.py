"""Regression cases from the fourth review round of the November 2026 audit."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

# Captured at import, before the fixtures swap in their inline stand-in.
REAL_TO_THREAD = asyncio.to_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"review-round4"


def run(coro):
    return asyncio.run(coro)


def run_threaded(monkeypatch, scenario, *, timeout=15.0):
    """Run ``scenario()`` with real worker threads on an executor that is shut
    down explicitly before the loop closes."""
    monkeypatch.setattr(asyncio, "to_thread", REAL_TO_THREAD)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="review-round4")

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


def _slot(upload_id, blob_id, *, expires_at):
    return {"id": upload_id, "created_at": 1.0, "expires_at": expires_at, "expected_mimetype": "image/png",
            "expected_size": None, "filename": None, "blob_id": blob_id, "mimetype": "image/png",
            "size": len(PNG), "profile": "claude"}


class Gate:
    """A memory lookup that blocks (in its worker thread) until released."""

    def __init__(self, real, *, block_calls=1):
        self.real = real
        self.block_calls = block_calls
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.block_calls:
            self.entered.set()
            self.release.wait(5)
        return self.real(*args, **kwargs)


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


# 1 -- a commit retry reconciles every marked slot ---------------------------

def test_commit_retry_on_a_marked_slot_without_commit_record_finds_the_landed_caption(proxy, fake_memory):
    slot = run(new_upload(proxy))
    blob = proxy.pending_uploads[slot].blob_id
    # An attempt recorded its user, wrote the caption and stopped before the hand-over.
    proxy.pending_uploads[slot].commit_users = ["my_lord"]
    caption = fake_memory.add("cat", user_id="my_lord",
                              metadata={"kind": "image", "blob_ref": blob, "upload_id": slot})["results"][0]["id"]
    retry = run(call(proxy, "reliquary_commit_image_upload", {"upload_id": slot, "caption": "cat again"}))
    assert retry["structuredContent"]["error"] == "already_committed"
    assert list(fake_memory._store) == [caption]
    info = proxy.blobs.info(blob)
    assert info.owners == [caption] and info.ref_count == 1


# 2 -- a cancelled settling pass keeps its claim until its worker is done ----

def test_cancelled_reaper_cannot_release_a_reference_twice(make_proxy, tmp_path, monkeypatch):
    memory, state, blob, other = _expired_marked_slot_sharing_bytes(make_proxy, tmp_path)
    proxy = make_proxy(memory=memory, state_dir=str(state))
    gate = Gate(memory.get_all)
    memory.get_all = gate

    async def scenario():
        first = asyncio.create_task(proxy._reap_expired_uploads())
        await _until(gate.entered.is_set)
        first.cancel()
        task = getattr(proxy, "_reap_task", None)
        if task is not None:
            task.cancel()  # as at shutdown
        await asyncio.sleep(0.05)
        second = asyncio.create_task(proxy._reap_expired_uploads())
        await asyncio.sleep(0.1)
        gate.release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await proxy._reap_expired_uploads()

    run_threaded(monkeypatch, scenario)
    info = proxy.blobs.info(blob)
    assert info is not None and info.owners == [other] and info.ref_count == 1


# 3 -- settling never holds up startup or unrelated writes -------------------

def _expired_marked_slot_sharing_bytes(make_proxy, tmp_path):
    """An abandoned (expired, never committed) upload whose bytes another
    caption also owns: ref_count 2, owners [caption, upload:<slot>]."""
    from blobs import BlobStore
    from conftest import FakeMemory

    memory = FakeMemory()
    store = BlobStore(str(tmp_path / "blobs"), b"test-signing-key")
    blob = store.put(PNG).id
    other = memory.add("other", user_id="my_lord", metadata={"blob_ref": blob})["results"][0]["id"]
    store.register_owner(blob, other)
    store.put(PNG, owner="upload:upl_X")
    state = tmp_path / "state"
    state.mkdir()
    # One commit attempt was recorded (so settling must look for a caption).
    (state / "pending_uploads.json").write_text(json.dumps(
        {"upl_X": {**_slot("upl_X", blob, expires_at=1), "commit_users": ["my_lord"]}}))
    return memory, state, blob, other


def test_startup_does_not_wait_for_a_hung_settling_lookup(make_proxy, tmp_path):
    memory, state, blob, other = _expired_marked_slot_sharing_bytes(make_proxy, tmp_path)
    gate = Gate(memory.get_all)
    memory.get_all = gate
    started = time.monotonic()
    try:
        proxy = make_proxy(memory=memory, state_dir=str(state))
    finally:
        gate.release.set()
    assert time.monotonic() - started < 1.0
    assert "upl_X" in proxy.pending_uploads  # kept until settled


def test_hung_settling_lookup_neither_blocks_writes_nor_starts_a_second_pass(make_proxy, tmp_path, monkeypatch):
    memory, state, blob, other = _expired_marked_slot_sharing_bytes(make_proxy, tmp_path)
    proxy = make_proxy(memory=memory, state_dir=str(state))
    gate = Gate(memory.get_all)
    memory.get_all = gate

    async def scenario():
        started = time.monotonic()
        await call(proxy, "reliquary_add_memory", {"text": "a write while the backend lookup hangs"})
        await _until(gate.entered.is_set)
        await call(proxy, "reliquary_add_memory", {"text": "another write"})
        write_time = time.monotonic() - started
        calls_while_hung = gate.calls
        gate.release.set()
        await proxy._reap_expired_uploads()
        return write_time, calls_while_hung

    write_time, calls_while_hung = run_threaded(monkeypatch, scenario)
    assert write_time < 1.0
    assert calls_while_hung == 1  # no second settling pass while the first runs
    assert "upl_X" not in proxy.pending_uploads
    info = proxy.blobs.info(blob)
    assert info is not None and info.owners == [other] and info.ref_count == 1  # released exactly once


def test_lifespan_startup_starts_the_first_settling_pass(make_proxy, tmp_path):
    memory, state, blob, other = _expired_marked_slot_sharing_bytes(make_proxy, tmp_path)
    proxy = make_proxy(memory=memory, state_dir=str(state))
    assert "upl_X" in proxy.pending_uploads  # construction decided nothing

    async def scenario():
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message["type"])
            if message["type"] == "lifespan.startup.complete":
                await asyncio.wait({proxy._reap_task})

        await proxy.handle_lifespan(receive, send)
        return sent

    assert run(scenario()) == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert "upl_X" not in proxy.pending_uploads
    assert proxy.blobs.info(blob).owners == [other]
