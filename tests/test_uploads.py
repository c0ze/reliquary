"""Tests for the MCP-discoverable raw binary upload flow."""

from __future__ import annotations

import asyncio
import re

import httpx

from server import PendingUpload


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def run(coro):
    return asyncio.run(coro)


def test_write_tools_advertise_upload_flow(proxy):
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]

    claude_names = {tool["name"] for tool in proxy.mcp_tools_for(claude, can_write=True)}
    openai_names = {tool["name"] for tool in proxy.mcp_tools_for(openai, can_write=True)}

    assert {"create_image_upload", "commit_image_upload"} <= claude_names
    assert {"create_image_upload", "commit_image_upload"} <= openai_names


def test_read_only_tools_do_not_advertise_upload_flow(proxy):
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    names = {tool["name"] for tool in proxy.mcp_tools_for(claude, can_write=False)}

    assert "create_image_upload" not in names
    assert "commit_image_upload" not in names


def test_create_image_upload_returns_one_time_http_instructions(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]

    result = run(proxy.call_mcp_tool(
        profile,
        "create_image_upload",
        {"mimetype": "image/png", "size": len(PNG), "filename": "test.png"},
        can_write=True,
    ))

    assert result["isError"] is False
    structured = result["structuredContent"]
    assert re.fullmatch(r"upl_[A-Za-z0-9_-]+", structured["upload_id"])
    assert structured["upload_url"] == f"/uploads/{structured['upload_id']}"
    assert structured["method"] == "POST"
    assert structured["headers"]["Content-Type"] == "image/png"
    assert structured["max_bytes"] == proxy.settings.blob_max_bytes
    assert structured["expires_at"] > 0


def test_upload_commit_fetch_delete_roundtrip(proxy):
    async def scenario():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(
            profile,
            "create_image_upload",
            {"mimetype": "image/png", "size": len(PNG)},
            can_write=True,
        )
        upload_url = create["structuredContent"]["upload_url"]
        upload_id = create["structuredContent"]["upload_id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            upload = await client.post(
                upload_url,
                content=PNG,
                headers={"content-type": "image/png", "authorization": "Bearer claude-secret"},
            )

        assert upload.status_code == 201
        assert upload.json()["upload_id"] == upload_id
        blob_id = upload.json()["blob_id"]

        commit = await proxy.call_mcp_tool(
            profile,
            "commit_image_upload",
            {"upload_id": upload_id, "caption": "uploaded raw PNG", "title": "Raw upload"},
            can_write=True,
        )
        assert commit["isError"] is False
        structured = commit["structuredContent"]
        assert structured["blob_id"] == blob_id
        assert structured["memory_id"].startswith("fake-")

        fetched = await proxy.handle_fetch_image_tool({"id": blob_id})
        assert fetched["isError"] is False
        assert fetched["structuredContent"]["size"] == len(PNG)

        deleted = await proxy.handle_delete_image_tool({"memory_id": structured["memory_id"]}, allow_user_id=True)
        assert deleted["isError"] is False
        assert deleted["structuredContent"]["blob_unlinked"] is True

    run(scenario())


def test_upload_rejects_unknown_and_expired_slots(proxy):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            unknown = await client.post("/uploads/upl_missing", content=PNG)
        assert unknown.status_code == 404

        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(profile, "create_image_upload", {}, can_write=True)
        upload_id = create["structuredContent"]["upload_id"]
        proxy.pending_uploads[upload_id].expires_at = 1.0

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            expired = await client.post(
                f"/uploads/{upload_id}", content=PNG, headers={"authorization": "Bearer claude-secret"}
            )
        assert expired.status_code == 410

    run(scenario())


def test_commit_rejects_upload_before_bytes_arrive(proxy):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    create = run(proxy.call_mcp_tool(profile, "create_image_upload", {}, can_write=True))
    upload_id = create["structuredContent"]["upload_id"]

    commit = run(proxy.call_mcp_tool(
        profile,
        "commit_image_upload",
        {"upload_id": upload_id, "caption": "not uploaded yet"},
        can_write=True,
    ))

    assert commit["isError"] is True
    assert commit["structuredContent"]["error"] == "upload_not_ready"


def test_upload_rejects_oversize_body(proxy):
    async def scenario():
        proxy.settings.blob_max_bytes = 4
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(profile, "create_image_upload", {}, can_write=True)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                create["structuredContent"]["upload_url"],
                content=b"too large",
                headers={"authorization": "Bearer claude-secret"},
            )

        assert response.status_code == 413

    run(scenario())


async def _post_bytes(proxy, upload_id, data=PNG, content_type="image/png", token="claude-secret"):
    headers = {"content-type": content_type}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy),
        base_url="http://testserver",
    ) as client:
        return await client.post(f"/uploads/{upload_id}", content=data, headers=headers)


def test_create_image_upload_requires_write_scope(proxy):
    """A read-only token must not be able to mint upload slots."""
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    result = run(proxy.call_mcp_tool(profile, "create_image_upload", {}, can_write=False))

    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "insufficient_scope"


def test_upload_slot_is_one_time_use(proxy):
    """A second POST to the same slot (before commit) is rejected with 409."""
    async def scenario():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(profile, "create_image_upload", {}, can_write=True)
        upload_id = create["structuredContent"]["upload_id"]

        first = await _post_bytes(proxy, upload_id)
        second = await _post_bytes(proxy, upload_id)

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["error"] == "upload_already_used"

    run(scenario())


def test_commit_rejects_slot_created_on_another_endpoint(proxy):
    """A slot minted on /claude/mcp cannot be finalized via /openai/mcp."""
    async def scenario():
        claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]

        create = await proxy.call_mcp_tool(
            claude, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]
        await _post_bytes(proxy, upload_id)

        commit = await proxy.call_mcp_tool(
            openai, "commit_image_upload", {"upload_id": upload_id, "caption": "x"}, can_write=True
        )

        assert commit["isError"] is True
        assert commit["structuredContent"]["error"] == "forbidden_endpoint"

    run(scenario())


def test_commit_image_upload_schema_exposes_taxonomy_and_metadata(proxy):
    """A strict client must be able to discover domain/hall/room/metadata on commit."""
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    tools = {t["name"]: t for t in proxy.mcp_tools_for(profile, can_write=True)}
    props = tools["commit_image_upload"]["inputSchema"]["properties"]

    for key in ("domain", "hall", "room", "metadata"):
        assert key in props, f"commit_image_upload schema is missing {key!r}"


def test_commit_applies_taxonomy_and_merges_metadata(proxy):
    """commit_image_upload should route by taxonomy and merge a metadata object,
    matching add_image."""
    async def scenario():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(
            profile, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]
        await _post_bytes(proxy, upload_id)

        commit = await proxy.call_mcp_tool(
            profile,
            "commit_image_upload",
            {
                "upload_id": upload_id,
                "caption": "tagged upload",
                "domain": "dev",
                "hall": "images",
                "room": "screenshots",
                "metadata": {"custom_key": "custom_value"},
            },
            can_write=True,
        )

        assert commit["isError"] is False
        memory_id = commit["structuredContent"]["memory_id"]
        stored = proxy.memory._store[memory_id]["metadata"]
        assert stored["domain"] == "dev"
        assert stored["hall"] == "images"
        assert stored["room"] == "screenshots"
        assert stored["custom_key"] == "custom_value"

    run(scenario())


def test_upload_rejects_missing_bearer(proxy):
    """An anonymous POST to a valid slot is rejected with 401 and stores nothing."""
    async def scenario():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(
            profile, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]

        resp = await _post_bytes(proxy, upload_id, token=None)

        assert resp.status_code == 401
        assert proxy.pending_uploads[upload_id].blob_id is None

    run(scenario())


def test_upload_rejects_bearer_for_a_different_endpoint(proxy):
    """A slot minted on /claude/mcp cannot be uploaded with the /openai/mcp bearer."""
    async def scenario():
        claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(
            claude, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]

        resp = await _post_bytes(proxy, upload_id, token="openai-secret")

        assert resp.status_code == 401
        assert proxy.pending_uploads[upload_id].blob_id is None

    run(scenario())


def test_cleanup_preserves_committed_blob(proxy):
    """A stale pending slot left behind after a successful commit (e.g. a crash
    before the slot was dropped) must NOT delete the committed memory's blob when
    it is later reaped — only truly orphaned (uncommitted) blobs are reclaimed."""
    async def scenario():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        create = await proxy.call_mcp_tool(
            profile, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]
        up = await _post_bytes(proxy, upload_id)
        blob_id = up.json()["blob_id"]

        commit = await proxy.call_mcp_tool(
            profile, "commit_image_upload", {"upload_id": upload_id, "caption": "kept"}, can_write=True
        )
        assert commit["isError"] is False

        # Simulate a crash that left the (already committed) slot behind, now expired.
        proxy.pending_uploads[upload_id] = PendingUpload(
            id=upload_id, created_at=0.0, expires_at=1.0, blob_id=blob_id, profile="claude"
        )
        proxy._cleanup_expired_uploads()

        # The committed memory still owns the blob, so its bytes must survive.
        assert proxy.blobs.get(blob_id) is not None

    run(scenario())


def test_orphaned_upload_blob_reaped_after_restart(make_proxy, tmp_path):
    """Bytes uploaded but never committed must not orphan a blob across a restart:
    the pending slot is persisted, and an expired slot is reaped on startup."""
    async def scenario():
        blob_dir = str(tmp_path / "shared-blobs")
        state_dir = str(tmp_path / "state")

        p1 = make_proxy(blob_dir=blob_dir, state_dir=state_dir)
        profile = p1.endpoint_profiles[p1.settings.claude_mcp_path]
        create = await p1.call_mcp_tool(
            profile, "create_image_upload", {"mimetype": "image/png"}, can_write=True
        )
        upload_id = create["structuredContent"]["upload_id"]
        up = await _post_bytes(p1, upload_id)
        blob_id = up.json()["blob_id"]

        # Bytes are on disk but the slot was never committed (no owner) -> orphan-to-be.
        assert p1.blobs.get(blob_id) is not None

        # Expire the slot and persist that state, then simulate a process restart.
        p1.pending_uploads[upload_id].expires_at = 1.0
        p1._save_pending_uploads()

        p2 = make_proxy(blob_dir=blob_dir, state_dir=state_dir)
        assert p2.blobs.get(blob_id) is None

    run(scenario())
