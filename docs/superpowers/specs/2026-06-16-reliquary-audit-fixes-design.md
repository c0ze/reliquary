# Reliquary audit fixes (v0.3.1) — design

**Date:** 2026-06-16
**Component:** `reliquary/` — `app/server.py` (tools + handlers) + config + docs
**Origin:** external audit (Codex) of the deployed v0.3.0. This spec covers only the findings that
are **ours to fix**; wrapper/client-side findings (Codex Apps error re-wrapping, tool-discovery
ranking, `fetch_image` "missing" inline bytes that are actually an MCP image block the wrapper
dropped) are out of scope.

Ships as **v0.3.1** (additive/bugfix; no breaking changes).

## 1. `propose_update` validates its target (bug)

**Problem:** [`handle_propose_update_tool`](app/server.py) only checks `target_id` is non-empty,
then files a `kind=correction` record. A nonexistent `target_id` creates an orphan correction.

**Fix:** after the empty check, resolve the target. It is valid if **either**:
- it's a known imported corpus record: `self.catalog and target_id in self.catalog.records_by_id`, **or**
- it resolves as a live memory: `await self.fetch_live_memory(target_id) is not None`.

If neither, return (no record written):
```json
{"error": "not_found", "id": "<target_id>",
 "suggested_action": "target_id must be an existing record id from reliquary_search"}
```
with `is_error=True`. Keep the existing missing/empty-target check.

## 2. Structured search filters (enhancement)

**Problem:** `reliquary_search` has no explicit `domain`/`hall`/`room`/`topic` params; routing is
query-text-based, so unrelated domains can rank first.

**Fix:** add optional `domain`, `hall`, `room`, `topic` params to the **Claude** `reliquary_search`
tool schema. When **any** are supplied, they act as a **hard metadata filter**:
- Build `explicit = {k: v for k in (domain,hall,room,topic) if provided}`.
- Bypass `catalog.build_routes(query)`; run a single search with `filters=explicit` merged with the
  resolved `user_id` (mirror how `route.filters` is passed to `search_memories`). This overrides the
  implicit query-text routing **and** the soft `context` bias.
- Filter synthesis-first results to the same `domain` when `domain` is supplied (synthesis pages
  carry a domain); otherwise leave synthesis behavior unchanged.
- Echo the applied filters in the response payload: `"filters": {...}` (and mention them in the text
  header). When no explicit filters are given, behavior is **exactly as today**.

The lean OpenAI `search` is left unchanged (carve-out; keep the deep-research shape minimal).

## 3. Absolute signed URLs (enhancement)

**Problem:** `_signed_blob_url` returns `"/blobs/{id}?…"` and `create_image_upload` returns
`"/uploads/{id}"` — relative paths a remote/wrapped client can't resolve.

**Fix:** add `ProxySettings.public_base_url` (CLI `--public-base-url`, env
`RELIQUARY_PUBLIC_BASE_URL`, default empty). Add a helper `self._absolute_url(path: str) -> str`:
strip a trailing `/` from `public_base_url` and prefix it when set; otherwise return `path`
unchanged. Apply to the blob URL ([server.py:3386](app/server.py)) and the upload URL
([server.py:2300](app/server.py)). Unset → relative as today (back-compat, zero behavior change).
Document in README + `.env.example`.

## 4a. `capabilities.write_authorized` (clarity)

Add an additive boolean `"write_authorized": bool(can_write)` to the `handle_capabilities_tool`
payload. `write_tools_when_authorized` keeps its meaning (tools you'd unlock by authorizing — empty
when already write-scoped); the new field makes the empty list self-explanatory. No shape break.

## 4b. `fetch_image` inline bytes in `structuredContent` (clarity)

`handle_fetch_image_tool` already returns an MCP image content block + signed URL + JSON. Also add
`"image_base64": encoded` to `structuredContent` **only when** `len(data) <= INLINE_IMAGE_MAX_BYTES`
(new module constant, `1_000_000`). Above the cap, omit it and append a note to the text that the
image is available via the signed URL. So wrappers that drop the image block still get bytes for
small images, without bloating responses for large ones.

## Testing (mirror existing patterns; `tests/`)

- **#1:** `propose_update` with an unknown `target_id` → `error=not_found`, no record created;
  with a valid live-memory target (add one, then propose) → succeeds.
- **#2:** searching with `domain=<X>` returns only `domain==X` hits (use the `FakeMemory` /
  conftest harness; extend `FakeMemory.search` to honor a `domain` metadata filter if needed);
  response echoes `filters`; no filters → unchanged behavior.
- **#3:** with `public_base_url` set, blob + upload URLs start with it; unset → start with `/`.
- **#4a:** capabilities shows `write_authorized: true` with a write token, `false` for read-only.
- **#4b:** `fetch_image` on a small blob includes `image_base64` in `structuredContent`; on a blob
  over the cap it omits it (and still returns the URL).
- Full suite + `python -m py_compile app/*.py` green.

## Out of scope
- Codex Apps wrapper error re-shaping (#2 in the audit), tool-discovery ranking (#4), and the
  `fetch_image` "no inline bytes" report (#7) — all client/wrapper-side; our direct surface is
  correct.
- Letting `commit_image_upload` accept base64 (duplicates `add_image`; the upload flow is for large
  out-of-band files by design).
