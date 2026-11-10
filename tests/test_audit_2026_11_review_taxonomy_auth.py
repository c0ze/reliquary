"""Regression cases from the external review of the November 2026 audit branch:
OAuth scope in verbatim mode, MCP session ownership, taxonomy filters and
legacy taxonomy spellings, duplicate imports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from oauth import OAuthProvider, scope_is_write  # noqa: E402


def _call(app, method, path, *, headers=None, body=b""):
    hdrs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": hdrs}
    parts = [body]

    async def receive():
        if parts:
            return {"type": "http.request", "body": parts.pop(0), "more_body": False}
        return {"type": "http.disconnect"}

    out = {"status": None, "headers": {}, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out


def _rpc(method, params=None, rid=1):
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def _exchange(provider, *, scope):
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = provider.issue_code({
        "client_id": "client-1", "redirect_uri": "https://claude.ai/cb", "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": scope,
    })
    return provider.exchange_code({
        "grant_type": "authorization_code", "code": code, "redirect_uri": "https://claude.ai/cb",
        "client_id": "client-1", "code_verifier": verifier,
    })


# --- 2. verbatim OAuth mode must not upgrade a read grant to the master -------


def test_verbatim_mode_read_grant_gets_a_derived_read_token():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", issue_verbatim_token=True)
    response, error = _exchange(provider, scope="read")
    assert error is None
    assert response["access_token"] != "MASTER"
    assert response["scope"] == "read"
    assert provider.access_token_scope(response["access_token"]) == "read"
    assert not scope_is_write(provider.access_token_scope(response["access_token"]))


def test_verbatim_mode_write_grant_still_gets_the_master():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", issue_verbatim_token=True)
    for scope in ("write", "mcp", "read write"):
        response, error = _exchange(provider, scope=scope)
        assert error is None and response["access_token"] == "MASTER", scope


def test_verbatim_mode_read_grant_cannot_write_through_the_server(make_proxy):
    p = make_proxy(oauth_verbatim_token=True)
    response, error = _exchange(p.oauth, scope="read")
    assert error is None
    headers = {"authorization": f"Bearer {response['access_token']}"}
    resp = _call(p, "POST", "/claude/mcp", headers=headers, body=_rpc("tools/list"))
    names = {tool["name"] for tool in json.loads(resp["body"])["result"]["tools"]}
    assert "reliquary_search" in names
    assert "reliquary_delete" not in names and "reliquary_add_memory" not in names


# --- 8. MCP sessions belong to the credential and endpoint that made them -----

CLAUDE = {"authorization": "Bearer claude-secret"}
READER = {"authorization": "Bearer read-tok"}


def _initialize(p, headers, path="/claude/mcp"):
    resp = _call(p, "POST", path, headers=headers, body=_rpc("initialize", {"protocolVersion": "2025-06-18"}))
    assert resp["status"] == 200
    return resp["headers"]["mcp-session-id"]


def _ping(p, headers, session_id, path="/claude/mcp"):
    return _call(p, "POST", path, headers={**headers, "mcp-session-id": session_id}, body=_rpc("ping"))["status"]


def test_another_credential_cannot_use_a_session(make_proxy):
    p = make_proxy(static_tokens=(("ro", "read", "read-tok"),))
    session = _initialize(p, CLAUDE)
    assert _ping(p, READER, session) == 404
    assert _ping(p, CLAUDE, session) == 200


def test_another_credential_cannot_delete_a_session(make_proxy):
    p = make_proxy(static_tokens=(("ro", "read", "read-tok"),))
    session = _initialize(p, CLAUDE)
    resp = _call(p, "DELETE", "/claude/mcp", headers={**READER, "mcp-session-id": session})
    assert resp["status"] == 204  # answered like an unknown session
    assert _ping(p, CLAUDE, session) == 200
    assert _call(p, "DELETE", "/claude/mcp", headers={**CLAUDE, "mcp-session-id": session})["status"] == 204
    assert _ping(p, CLAUDE, session) == 404


def test_a_session_is_bound_to_its_endpoint(make_proxy):
    p = make_proxy(static_tokens=(("rw", "write", "both-tok"),))
    both = {"authorization": "Bearer both-tok"}
    session = _initialize(p, both, "/claude/mcp")
    assert _ping(p, both, session, "/openai/mcp") == 404
    _call(p, "DELETE", "/openai/mcp", headers={**both, "mcp-session-id": session})
    assert _ping(p, both, session, "/claude/mcp") == 200


def test_ownerless_persisted_session_adopts_its_first_credential(make_proxy, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "mcp_sessions.json").write_text(json.dumps({"legacy": "claude"}))
    tokens = (("ro", "read", "read-tok"), ("rw", "write", "both-tok"))
    p = make_proxy(state_dir=str(state), static_tokens=tokens)
    both = {"authorization": "Bearer both-tok"}
    assert _ping(p, both, "legacy", "/openai/mcp") == 404  # the wrong endpoint does not adopt it
    assert _ping(p, CLAUDE, "legacy") == 200
    assert _ping(p, READER, "legacy") == 404
    assert json.loads((state / "mcp_sessions.json").read_text())["legacy"] == ["claude", "master:claude"]
    restarted = make_proxy(state_dir=str(state), static_tokens=tokens)
    assert _ping(restarted, READER, "legacy") == 404
    assert _ping(restarted, CLAUDE, "legacy") == 200


# --- 5. explicit search filters accept the spelling writes accept --------------


def _tool(p, name, arguments, endpoint="claude"):
    profile = p.endpoint_profiles[getattr(p.settings, f"{endpoint}_mcp_path")]
    return asyncio.run(p.call_mcp_tool(profile, name, arguments, can_write=True))


def _search_ids(p, **arguments):
    result = _tool(p, "reliquary_search", {"query": "notes", "limit": 20, **arguments})
    assert not result.get("isError"), result
    return {item["id"] for item in result["structuredContent"]["results"]}


def test_explicit_filter_round_trips_a_normalised_write(proxy):
    added = _tool(proxy, "reliquary_add_memory", {"text": "Rack wiring plan", "domain": "Work Notes"})
    new_id = added["structuredContent"]["ids"][0]
    assert proxy.memory.get(new_id)["metadata"]["domain"] == "work-notes"
    assert new_id in _search_ids(proxy, domain="Work Notes")
    assert new_id in _search_ids(proxy, domain="work-notes")


def test_explicit_filter_still_finds_records_stored_under_the_raw_spelling(proxy):
    new_id = _tool(proxy, "reliquary_add_memory", {"text": "New note", "domain": "Work Notes"})[
        "structuredContent"]["ids"][0]
    legacy_id = proxy.memory.add("Legacy note", user_id="my_lord",
                                 metadata={"domain": "Work Notes", "source_group": "user-write"})["results"][0]["id"]
    other_id = proxy.memory.add("Elsewhere", user_id="my_lord",
                                metadata={"domain": "Home", "source_group": "user-write"})["results"][0]["id"]
    found = _search_ids(proxy, domain="Work Notes")
    assert {new_id, legacy_id} <= found
    assert other_id not in found  # still a hard restriction
    # The pool is the same on every page: paging through finds each record once.
    first = _tool(proxy, "reliquary_search", {"query": "notes", "limit": 1, "domain": "Work Notes"})
    second = _tool(proxy, "reliquary_search", {"query": "notes", "limit": 1, "domain": "Work Notes",
                                               "cursor": first["structuredContent"]["nextCursor"]})
    paged = [first["structuredContent"]["results"][0]["id"], second["structuredContent"]["results"][0]["id"]]
    assert sorted(paged) == sorted([new_id, legacy_id])
    assert second["structuredContent"]["nextCursor"] is None


def test_explicit_filter_reaches_new_and_legacy_compiled_pages(proxy):
    _tool(proxy, "reliquary_compile_page", {"slug": "new-page", "title": "New page", "markdown": "notes body",
                                            "domain": "Work Notes"})
    assert proxy.pages.get("new-page").domain == "work-notes"
    # A page filed by an older version keeps the raw spelling in the registry and index.
    info = proxy.pages.put_revision("old-page", "notes body", {"title": "Old page", "domain": "Work Notes"})
    proxy.compiled_memory.add("notes body", user_id="my_lord", metadata={
        "kind": "synthesis", "source_group": "compiled", "slug": "old-page", "domain": "Work Notes",
        "blob_ref": info.current_blob})
    found = _search_ids(proxy, domain="Work Notes")
    assert {"new-page", "old-page"} <= found


def test_list_pages_domain_filter_accepts_either_spelling(proxy):
    _tool(proxy, "reliquary_compile_page", {"slug": "new-page", "title": "New", "markdown": "b", "domain": "Work Notes"})
    proxy.pages.put_revision("old-page", "b", {"title": "Old", "domain": "Work Notes"})
    proxy.pages.put_revision("elsewhere", "b", {"title": "Else", "domain": "home"})
    for domain in ("Work Notes", " Work Notes "):
        pages = _tool(proxy, "reliquary_list_pages", {"domain": domain})["structuredContent"]["pages"]
        assert {page["slug"] for page in pages} == {"new-page", "old-page"}
    pages = _tool(proxy, "reliquary_list_pages", {"domain": "work-notes"})["structuredContent"]["pages"]
    assert {page["slug"] for page in pages} == {"new-page"}


# --- 6. staleness matches legacy page taxonomy spellings ----------------------


def test_taxonomy_match_keys_join_every_historical_spelling():
    from metadata_policy import taxonomy_match_keys

    def same(a, b):
        return bool(taxonomy_match_keys(a) & taxonomy_match_keys(b))

    assert same("Work Notes", "work-notes")
    for a in ("Günlük", "günlük", "g-nl-k"):
        for b in ("Günlük", "günlük", "g-nl-k"):
            assert same(a, b), (a, b)
    assert same("Straße", "strasse")
    assert same("日記", "日記")
    assert not same("日記", "旅行")
    assert not same("work-notes", "home-lab")
    assert taxonomy_match_keys(None) == frozenset() == taxonomy_match_keys("  ")


def test_registry_matches_legacy_page_spellings(proxy):
    pages = proxy.pages
    pages.put_revision("raw", "b", {"domain": "Work Notes", "topic": "Home Lab"})
    pages.put_revision("old-slug", "b", {"domain": "g-nl-k", "topic": "home-lab"})
    pages.put_revision("other", "b", {"domain": "work-notes", "topic": "garden"})
    pages.put_revision("sourced", "b", {"domain": "work-notes", "topic": "home-lab", "derived_from": ["x"]})
    assert {p.slug for p in pages.pages_deriving_from(domain="work-notes", topic="home-lab")} == {"raw"}
    assert {p.slug for p in pages.pages_deriving_from(domain="günlük", topic="Home Lab")} == {"old-slug"}


def test_new_write_marks_a_legacy_raw_page_stale(proxy):
    proxy.pages.put_revision("legacy", "b", {"domain": "Work Notes", "topic": "Home Lab"})
    _tool(proxy, "reliquary_add_memory", {"text": "A new note", "domain": "Work Notes", "topic": "Home Lab"})
    assert proxy.pages.get("legacy").status == "stale"


def test_updating_a_legacy_memory_marks_a_new_page_stale(proxy):
    legacy_id = proxy.memory.add("Old note", user_id="my_lord", metadata={
        "domain": "Günlük", "topic": "Home Lab", "source_group": "user-write"})["results"][0]["id"]
    _tool(proxy, "reliquary_compile_page", {"slug": "fresh", "title": "Fresh", "markdown": "b",
                                            "domain": "Günlük", "topic": "Home Lab"})
    assert proxy.pages.get("fresh").domain == "günlük" and proxy.pages.get("fresh").status == "current"
    result = _tool(proxy, "reliquary_update", {"id": legacy_id, "text": "Old note, edited"})
    assert not result.get("isError"), result
    assert proxy.pages.get("fresh").status == "stale"


def test_ingest_fan_out_matches_legacy_page_spellings(tmp_path):
    from blobs import BlobStore
    from compiled import PageRegistry
    from ingest import ingest_records
    from conftest import FakeMemory

    registry = PageRegistry(registry_dir=str(tmp_path / "compiled"),
                            blobs=BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0))
    registry.put_revision("old-slug", "b", {"domain": "g-nl-k", "topic": "home-lab"})
    registry.put_revision("raw", "b", {"domain": "Work Notes", "topic": "Home Lab"})
    registry.put_revision("unrelated", "b", {"domain": "garden", "topic": "home-lab"})
    ingest_records(FakeMemory(), [
        {"id": "r1", "text": "t1", "metadata": {"domain": "Günlük", "topic": "Home Lab"}},
        {"id": "r2", "text": "t2", "metadata": {"domain": "work notes", "topic": "home lab"}},
    ], user_id="u", page_registry=registry)
    assert registry.get("old-slug").status == "stale"
    assert registry.get("raw").status == "stale"
    assert registry.get("unrelated").status == "current"
