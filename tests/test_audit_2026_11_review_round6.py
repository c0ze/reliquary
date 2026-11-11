"""Round 6 of the November 2026 audit: write-ahead saves of the upload flow
must succeed before the step that relies on them (the model-based cases live
in test_audit_2026_11_review_round5.py)."""

from __future__ import annotations

import asyncio
import json

PNG = b"\x89PNG\r\n\x1a\n" + b"review-round6"


def _claude(proxy):
    return proxy.endpoint_profiles[proxy.settings.claude_mcp_path]


def _fail_next_save(proxy):
    store = proxy.pending_upload_store
    real_save = store.save
    state = {"armed": True}

    def save(data):
        if state["armed"]:
            state["armed"] = False
            raise OSError(28, "No space left on device")
        return real_save(data)

    store.save = save


async def _post(proxy, upload_id):
    sent = []

    async def receive():
        return {"type": "http.request", "body": PNG, "more_body": False}

    async def send(message):
        sent.append(message)

    await proxy.handle_upload_post(upload_id, {"type": "http", "headers": [
        (b"authorization", b"Bearer claude-secret"), (b"content-type", b"image/png")]}, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _slot_with_bytes(proxy):
    upload_id = proxy.handle_create_image_upload_tool({}, profile=_claude(proxy))["structuredContent"]["upload_id"]
    assert asyncio.run(_post(proxy, upload_id)) == 201
    return upload_id


def test_commit_writes_no_caption_when_its_attempt_cannot_be_recorded(make_proxy, tmp_path, fake_memory):
    state = tmp_path / "state"
    proxy = make_proxy(memory=fake_memory, state_dir=str(state))
    slot = _slot_with_bytes(proxy)
    _fail_next_save(proxy)
    args = {"upload_id": slot, "caption": "cat"}
    failed = asyncio.run(proxy.call_mcp_tool(_claude(proxy), "reliquary_commit_image_upload", args, can_write=True))
    assert failed["structuredContent"]["error"] == "state_write_failed"
    assert fake_memory._store == {}                                  # nothing was written
    assert proxy.pending_uploads[slot].commit_users == []            # memory matches disk
    assert json.loads((state / "pending_uploads.json").read_text())[slot]["commit_users"] == []
    # Storage recovers: the retry records its user before writing the caption.
    retry = asyncio.run(proxy.call_mcp_tool(_claude(proxy), "reliquary_commit_image_upload", args, can_write=True))
    assert retry["isError"] is False


def test_retry_persists_its_user_even_when_memory_already_lists_it(make_proxy, tmp_path, fake_memory, monkeypatch):
    state = tmp_path / "state"
    proxy = make_proxy(memory=fake_memory, state_dir=str(state))
    slot = _slot_with_bytes(proxy)
    proxy.pending_uploads[slot].commit_users = ["my_lord"]  # in memory only, as after a failed save
    seen = []
    real_add = fake_memory.add

    def add(*args, **kwargs):
        seen.append(json.loads((state / "pending_uploads.json").read_text())[slot]["commit_users"])
        return real_add(*args, **kwargs)

    monkeypatch.setattr(fake_memory, "add", add)
    asyncio.run(proxy.call_mcp_tool(_claude(proxy), "reliquary_commit_image_upload",
                                    {"upload_id": slot, "caption": "cat"}, can_write=True))
    assert seen == [["my_lord"]]  # on disk before the caption write


def test_upload_post_undoes_its_blob_when_the_slot_cannot_be_saved(make_proxy, tmp_path):
    state = tmp_path / "state"
    proxy = make_proxy(state_dir=str(state))
    upload_id = proxy.handle_create_image_upload_tool({}, profile=_claude(proxy))["structuredContent"]["upload_id"]
    _fail_next_save(proxy)
    assert asyncio.run(_post(proxy, upload_id)) == 503
    assert proxy.pending_uploads[upload_id].blob_id is None
    assert list((tmp_path / "blobs").rglob("*.json")) == []          # the reference was released
    assert asyncio.run(_post(proxy, upload_id)) == 201                # and the client can retry


def test_slot_creation_fails_cleanly_when_it_cannot_be_saved(make_proxy, tmp_path):
    proxy = make_proxy(state_dir=str(tmp_path / "state"))
    _fail_next_save(proxy)
    result = proxy.handle_create_image_upload_tool({}, profile=_claude(proxy))
    assert result["structuredContent"]["error"] == "state_write_failed"
    assert proxy.pending_uploads == {}
