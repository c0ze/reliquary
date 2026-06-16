# Reliquary audit fixes (v0.3.1) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD each task; keep the suite green; commit per task.

**Goal:** Fix the audit findings that are ours — `propose_update` target validation, structured search filters, absolute signed URLs, and two clarity tweaks (`capabilities.write_authorized`, `fetch_image` inline base64). Additive/bugfix; ships as v0.3.1.

**Spec:** `docs/superpowers/specs/2026-06-16-reliquary-audit-fixes-design.md` (authoritative; read the matching section per task).

**Tech:** Python 3.12+, pytest. Single file mostly: `app/server.py` (+ config + `.env.example`/README + tests). Test harness: `proxy` fixture in `tests/conftest.py`; `proxy.call_mcp_tool(profile, name, args, can_write=)`; `FakeMemory` backend.

---

### Task 1 — `propose_update` target validation (spec §1)
**Files:** `app/server.py` (`handle_propose_update_tool`), `tests/test_governance.py`.
- [ ] Failing test: `reliquary_propose_update` (Claude) with `target_id="does-not-exist"` → `structuredContent.error == "not_found"`, `isError`, and **no** new record (assert the store count is unchanged / the returned id is absent). Second test: add a memory, then `propose_update` with that id → succeeds (`status=proposed`).
- [ ] Run → fails.
- [ ] Implement: after the empty-check, `valid = (self.catalog and target_id in self.catalog.records_by_id) or (await self.fetch_live_memory(target_id) is not None)`; if not `valid`, return the `not_found` error result (see spec) before writing. 
- [ ] Run → passes; full suite green.
- [ ] Commit: `fix: validate propose_update target_id (reject nonexistent targets)`

### Task 2 — structured search filters (spec §2)
**Files:** `app/server.py` (`mcp_tools_for` Claude `reliquary_search` schema + `handle_search_tool`), `tests/conftest.py` (extend `FakeMemory.search` to honor a `domain`/metadata filter), `tests/test_mcp_surface.py` (or `test_tools.py`).
- [ ] Failing test: seed memories in two domains; `reliquary_search(query, domain="dev")` returns only `domain=="dev"` hits and `structuredContent.filters == {"domain":"dev"}`; a no-filter search still returns across domains (unchanged).
- [ ] Run → fails.
- [ ] Implement: add `domain`/`hall`/`room`/`topic` to the `reliquary_search` input schema (Claude only). In `handle_search_tool`, build `explicit = {k:v for k,v in (("domain",…),("hall",…),("room",…),("topic",…)) if v}`. When `explicit`: skip `build_routes`; do one `search_memories(query, …, filters={**explicit})` (merge user_id the same way routes do); filter `_synthesis_first_hits` to `domain` when given; include `"filters": explicit` in the structured payload + text header. When empty: current behavior. (Leave lean OpenAI `search` unchanged.)
- [ ] Run → passes; full suite green.
- [ ] Commit: `feat: explicit domain/hall/room/topic filters on reliquary_search`

### Task 3 — absolute signed URLs (spec §3)
**Files:** `app/server.py` (`ProxySettings`, argparse, `_absolute_url` helper, `_signed_blob_url`, upload URL), `tests/conftest.py` (allow passing `public_base_url`), `tests/test_uploads.py` or `test_tools.py`, `.env.example`, `README.md`.
- [ ] Failing test: with `public_base_url="https://r.example.com"`, a `reliquary_fetch_image`/`create_image_upload` URL starts with `https://r.example.com/`; unset → starts with `/blobs/` or `/uploads/`.
- [ ] Run → fails.
- [ ] Implement: add `public_base_url: str = ""` to `ProxySettings`; argparse `--public-base-url` default `os.getenv("RELIQUARY_PUBLIC_BASE_URL", "")`; helper `_absolute_url(path)` → `f"{self.settings.public_base_url.rstrip('/')}{path}"` when set else `path`; wrap the blob + upload URL returns. Document the env var in `.env.example` + README.
- [ ] Run → passes; full suite green.
- [ ] Commit: `feat: optional RELIQUARY_PUBLIC_BASE_URL for absolute blob/upload URLs`

### Task 4 — capabilities + fetch_image clarity (spec §4a, §4b)
**Files:** `app/server.py` (`handle_capabilities_tool`, `handle_fetch_image_tool`, new `INLINE_IMAGE_MAX_BYTES`), `tests/test_compiled_server.py`/`test_tools.py`, `tests/test_scopes.py`.
- [ ] Failing tests: capabilities with write scope → `structuredContent.write_authorized is True`; read-only → `False`. `reliquary_fetch_image` on a small blob → `structuredContent.image_base64` present and decodes to the bytes; (optional) a blob over the cap → key absent.
- [ ] Run → fails.
- [ ] Implement: add `"write_authorized": bool(can_write)` to the capabilities payload. Add module const `INLINE_IMAGE_MAX_BYTES = 1_000_000`; in `handle_fetch_image_tool`, add `image_base64` to the structured dict when `len(data) <= INLINE_IMAGE_MAX_BYTES` (else append a "available via URL" note to the text).
- [ ] Run → passes; full suite green.
- [ ] Commit: `feat: capabilities.write_authorized + fetch_image inline base64 (size-capped)`

### Task 5 — docs + final verification
**Files:** `README.md`, `docs/GUIDE.md`.
- [ ] Document: search filters, `RELIQUARY_PUBLIC_BASE_URL`, and the `fetch_image`/capabilities behavior. Note `propose_update` now validates its target.
- [ ] `python -m pytest -q` + `python -m py_compile app/*.py` green.
- [ ] Commit: `docs: search filters, public base URL, capabilities/fetch_image notes`

## Self-review
Each spec section (§1–§4b) maps to a task; tests assert behavior (filter restricts, validation rejects, URL prefixing, both clarity fields). Out-of-scope (wrapper/client) items are not addressed by design.
