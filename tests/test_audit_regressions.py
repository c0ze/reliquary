"""Regression cases found during the September 2026 service audit."""

import asyncio
import base64
import hashlib
import socket

import httpx
import pytest

from oauth import OAuthProvider, REFRESH_REUSE_GRACE
from ingest import ingest_records, import_metadata
from urlfetch import validate_public_url
from persistence import JsonFileStore


PNG = b"\x89PNG\r\n\x1a\n" + b"audit"


def test_revision_history_keeps_old_timestamp_and_status(proxy, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("compiled.time.time", lambda: now[0])
    proxy.pages.put_revision("history", "first", {"status": "draft"})
    now[0] = 2000.0
    proxy.pages.put_revision("history", "second", {"status": "current"})
    history = proxy.pages.history_detailed("history")
    assert history[1]["ts"] == 1000.0
    assert history[1]["status"] == "draft"


def test_repeated_revision_bytes_keep_distinct_history_timestamps(proxy, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("compiled.time.time", lambda: now[0])
    for body in ("first", "second", "first", "third"):
        proxy.pages.put_revision("history", body, {})
        now[0] += 1000.0
    assert [row["ts"] for row in proxy.pages.history_detailed("history")] == [4000, 3000, 2000, 1000]


@pytest.mark.parametrize("operation", ["update", "delete", "delete_image"])
def test_source_mutations_mark_dependent_synthesis_stale(proxy, operation):
    async def scenario():
        if operation == "delete_image":
            result = await proxy.handle_add_image_tool({
                "image_base64": base64.b64encode(PNG).decode(), "caption": "source"})
            mid = result["structuredContent"]["memory_id"]
        else:
            result = await proxy.handle_add_memory_tool({"text": "source"})
            mid = result["structuredContent"]["ids"][0]
        proxy.pages.put_revision("dependent", "summary", {"derived_from": [mid]})
        if operation == "update":
            result = await proxy.handle_update_tool({"id": mid, "text": "corrected"})
        elif operation == "delete":
            result = await proxy.handle_delete_tool({"id": mid})
        else:
            result = await proxy.handle_delete_image_tool({"memory_id": mid})
        assert not result["isError"]
        assert proxy.pages.get("dependent").status == "stale"
    asyncio.run(scenario())


def test_parallel_upload_posts_store_only_one_blob(proxy):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_body():
            entered.set()
            await release.wait()
            yield PNG

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            headers = {"authorization": "Bearer claude-secret"}
            first = asyncio.create_task(client.post(slot["upload_url"], content=slow_body(), headers=headers))
            await entered.wait()
            try:
                second = await client.post(slot["upload_url"], content=PNG + b"second", headers=headers)
            finally:
                release.set()
            first_result = await first
        assert first_result.status_code == 201
        assert second.status_code == 409
        assert proxy.blobs.info(hashlib.sha256(PNG + b"second").hexdigest()) is None
    asyncio.run(scenario())


def test_parallel_upload_commits_create_one_caption(proxy, monkeypatch):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            assert (await client.post(slot["upload_url"], content=PNG,
                    headers={"authorization": "Bearer claude-secret"})).status_code == 201
        original = proxy.add_memory
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            # Expiration cleanup may run on another request while this write
            # awaits the backend. It must not remove the in-flight blob.
            proxy.pending_uploads[slot["upload_id"]].expires_at = 1
            proxy._cleanup_expired_uploads()
            return await original(*args, **kwargs)

        monkeypatch.setattr(proxy, "add_memory", delayed)
        args = {"upload_id": slot["upload_id"], "caption": "audit"}
        first = asyncio.create_task(proxy.handle_commit_image_upload_tool(args))
        await entered.wait()
        second = asyncio.create_task(proxy.handle_commit_image_upload_tool(args))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert sum(not r["isError"] for r in results) == 1
        assert len(proxy.memory._store) == 1
        info = proxy.blobs.info(hashlib.sha256(PNG).hexdigest())
        assert info.ref_count == len(info.owners) == 1
    asyncio.run(scenario())


def test_expired_post_reclaims_previously_uploaded_bytes(proxy):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            headers = {"authorization": "Bearer claude-secret"}
            result = await client.post(slot["upload_url"], content=PNG, headers=headers)
            blob_id = result.json()["blob_id"]
            proxy.pending_uploads[slot["upload_id"]].expires_at = 1
            assert (await client.post(slot["upload_url"], content=PNG, headers=headers)).status_code == 410
        assert proxy.blobs.info(blob_id) is None
    asyncio.run(scenario())


def test_disconnected_upload_does_not_store_partial_bytes(proxy):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
        messages = iter([
            {"type": "http.request", "body": PNG, "more_body": True},
            {"type": "http.disconnect"},
        ])
        async def receive():
            return next(messages)
        sent = []
        async def send(message):
            sent.append(message)
        await proxy.handle_upload_post(slot["upload_id"], {
            "headers": [(b"authorization", b"Bearer claude-secret")]}, receive, send)
        assert sent[0]["status"] == 400
        assert proxy.blobs.info(hashlib.sha256(PNG).hexdigest()) is None
    asyncio.run(scenario())


def test_old_refresh_token_has_replay_window_after_rotation(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("oauth.time.time", lambda: now[0])
    provider = OAuthProvider(master_token="master", mcp_resource_path="/claude/mcp")
    _, refresh = provider.issue_token_pair(client_id="c", scope="read")
    now[0] += REFRESH_REUSE_GRACE * 5
    result, error = provider.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert error is None
    _, error = provider.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert "reuse" in error[2]
    assert not provider.verify_access_token(result["access_token"])


def test_oauth_code_cannot_be_exchanged_by_different_client():
    provider = OAuthProvider(master_token="master", mcp_resource_path="/claude/mcp")
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = provider.issue_code({"client_id": "owner", "redirect_uri": "https://example.com/cb",
                                "code_challenge": challenge})
    result, error = provider.exchange_code({"grant_type": "authorization_code", "code": code,
        "client_id": "other", "redirect_uri": "https://example.com/cb", "code_verifier": verifier})
    assert result is None and error[1] == "invalid_grant"


def test_incremental_ingest_uses_mem0_v2_and_reads_beyond_default_page():
    records = [{"id": str(i), "text": f"record {i}", "metadata": {}} for i in range(1050)]

    class V2Memory:
        def get_all(self, *, filters=None, top_k=20, **kwargs):
            assert not kwargs, "Mem0 2.x rejects top-level user_id"
            assert filters == {"user_id": "owner"}
            return {"results": [{"id": f"mem-{r['id']}", "memory": r["text"],
                                 "metadata": import_metadata(r)} for r in records[:top_k]]}

        def add(self, *args, **kwargs):
            pytest.fail("unchanged records must not be duplicated")

    result = ingest_records(V2Memory(), records, user_id="owner", incremental=True)
    assert result["skipped"] == len(records)


@pytest.mark.parametrize("url", ["https://example.com:bad/x", "https://example.com:99999/x"])
def test_malformed_image_url_returns_validation_error(url):
    assert validate_public_url(url) is not None


def test_image_fetch_pins_validated_ip_and_preserves_tls_hostname(proxy, monkeypatch):
    resolutions = []
    def resolve(*args, **kwargs):
        resolutions.append(args[0])
        ip = "93.184.216.34" if len(resolutions) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(proxy, "client", client)
            monkeypatch.setattr(proxy, "image_client", client, raising=False)
            result = await proxy._fetch_image_from_url("https://example.com/picture.png")
        assert result == (PNG, "image/png")
        assert len(resolutions) == 1
        assert requests[0].url.host == "93.184.216.34"
        assert requests[0].headers["host"] == "example.com"
        assert requests[0].extensions["sni_hostname"] == "example.com"
    asyncio.run(scenario())


@pytest.mark.parametrize("synthesis_score,expected_first", [(0.4, "raw"), (0.88, "compiled")])
def test_synthesis_preference_respects_relevance(proxy, monkeypatch, synthesis_score, expected_first):
    async def raw(*args, **kwargs):
        return [{"id": "raw", "memory": "Reliquary MCP architecture", "score": 0.9}]
    async def synthesis(*args, **kwargs):
        return [{"id": "compiled", "text": "summary", "score": synthesis_score, "route": "synthesis"}]
    monkeypatch.setattr(proxy, "search_memories", raw)
    monkeypatch.setattr(proxy, "_synthesis_first_hits", synthesis)
    result = asyncio.run(proxy.handle_search_tool({"query": "Reliquary MCP architecture", "limit": 1}))
    assert result["structuredContent"]["results"][0]["id"] == expected_first


def test_concurrent_page_revisions_share_one_index_record(proxy, monkeypatch):
    async def scenario():
        original = proxy._index_compiled_page
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(proxy, "_index_compiled_page", delayed)
        first = asyncio.create_task(proxy.handle_compile_page_tool({"slug": "same", "markdown": "first"}))
        await entered.wait()
        second = asyncio.create_task(proxy.handle_compile_page_tool({"slug": "same", "markdown": "second"}))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert all(not r["isError"] for r in results)
        assert len(proxy.compiled_memory._store) == 1
        info = proxy.pages.get("same")
        indexed = proxy.compiled_memory._store[info.memory_id]
        assert indexed["metadata"]["blob_ref"] == info.current_blob
    asyncio.run(scenario())


def test_upload_size_mismatch_can_be_retried(proxy):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({"size": len(PNG)})["structuredContent"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            headers = {"authorization": "Bearer claude-secret"}
            short = await client.post(slot["upload_url"], content=PNG[:-1], headers=headers)
            assert short.status_code == 400 and short.json()["error"] == "size_mismatch"
            assert (await client.post(slot["upload_url"], content=PNG, headers=headers)).status_code == 201
    asyncio.run(scenario())


def test_upload_expiring_during_receive_does_not_store_bytes(proxy):
    async def scenario():
        slot = proxy.handle_create_image_upload_tool({})["structuredContent"]
        async def body():
            proxy.pending_uploads[slot["upload_id"]].expires_at = 1
            yield PNG
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://test") as client:
            result = await client.post(slot["upload_url"], content=body(),
                                       headers={"authorization": "Bearer claude-secret"})
        assert result.status_code == 410
        assert proxy.blobs.info(hashlib.sha256(PNG).hexdigest()) is None
    asyncio.run(scenario())


def test_image_redirect_is_checked_before_private_connection(proxy, monkeypatch):
    def resolve(host, port, **kwargs):
        ip = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://internal.example/image"})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(proxy, "image_client", client)
            result = await proxy._fetch_image_from_url("https://example.com/image")
        assert result["structuredContent"]["error"] == "unsafe_url"
        assert len(requests) == 1
    asyncio.run(scenario())


def test_rotation_replay_detection_survives_restart(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("oauth.time.time", lambda: now[0])
    settings = dict(master_token="master", mcp_resource_path="/claude/mcp",
                    token_store=JsonFileStore(str(tmp_path / "access.json")),
                    refresh_token_store=JsonFileStore(str(tmp_path / "refresh.json")))
    provider = OAuthProvider(**settings)
    _, refresh = provider.issue_token_pair(client_id="c", scope="read")
    now[0] += REFRESH_REUSE_GRACE * 5
    result, error = provider.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert error is None
    restored = OAuthProvider(**settings)
    _, error = restored.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert "reuse" in error[2]
    assert not restored.verify_access_token(result["access_token"])
