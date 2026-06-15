# Reliquary de-`mem0` rename — design

**Date:** 2026-06-16
**Component:** `reliquary/` — MCP tool surface, resource URIs, operator env vars, chat-proxy contract
**Issue:** none (maintainer-driven cleanup)
**Goal:** Purge the `mem0_` naming artifact from Reliquary's *own* public surface and replace it
with one consistent `reliquary` namespace. `mem0` is an implementation detail (the backend memory
library); it should not leak into the agent-facing tools, the resource URIs, the operator's env
vars, or the chat-proxy request contract. **Clean break — no aliases.**

## Problem

The tool/resource/config surface is a mix of `mem0_`-prefixed names, a few bare names, and
`MEM0_*` env vars — all inherited from an earlier version. Agents see `mem0_capabilities`,
`mem0_search`, etc.; operators configure `MEM0_STATE_DIR`. None of this should reference the
backend library. The names should say **Reliquary**.

## Decisions locked during brainstorming

- **Scope:** rename *all* Claude-endpoint tools (incl. the currently-unprefixed ones), the
  `mem0://` resource URIs, the `MEM0_*` env vars, **and** the chat-proxy request contract.
- **Clean break:** no functional `mem0_*`/`MEM0_*` aliases. The connector must reconnect to pick
  up new tool names — which it already must do for the v0.2.0 tools, so this rides along.
- **Approach A:** one PR + a startup migration guard (a loud warning when a legacy `MEM0_*` var is
  still present in the environment), rather than a deprecation shim (B) or a phased split (C).
- **Breaking change → ships as `v0.3.0`.**

## Rename rules

All renames are a literal namespace swap. Implementers should treat the tables below as the
authoritative mapping.

### Tools — Claude endpoint (17) → `reliquary_*`

| Old | New |
|---|---|
| `mem0_capabilities` | `reliquary_capabilities` |
| `mem0_status` | `reliquary_status` |
| `mem0_search` | `reliquary_search` |
| `mem0_fetch` | `reliquary_fetch` |
| `mem0_add_memory` | `reliquary_add_memory` |
| `mem0_update` | `reliquary_update` |
| `mem0_delete` | `reliquary_delete` |
| `mem0_compile_page` | `reliquary_compile_page` |
| `mem0_list_pages` | `reliquary_list_pages` |
| `mem0_page_history` | `reliquary_page_history` |
| `propose_update` | `reliquary_propose_update` |
| `list_domains` | `reliquary_list_domains` |
| `add_image` | `reliquary_add_image` |
| `fetch_image` | `reliquary_fetch_image` |
| `delete_image` | `reliquary_delete_image` |
| `create_image_upload` | `reliquary_create_image_upload` |
| `commit_image_upload` | `reliquary_commit_image_upload` |

### Resources (9) → `reliquary://`

`mem0://schema`, `mem0://recent`, `mem0://needs-review`, `mem0://sources`, `mem0://taxonomy`,
`mem0://domain/`, `mem0://domain/{domain}`, `mem0://domain/{domain}/index`,
`mem0://record/{record_id}` → the same paths under `reliquary://`.

### Env vars (~35) → `RELIQUARY_*`

Pure prefix swap `MEM0_ → RELIQUARY_` for every Reliquary-owned var read in code:

```text
MEM0_AUDIT_LOG  MEM0_BLOB_DIR  MEM0_BLOB_MAX_BYTES  MEM0_BLOB_SIGNING_KEY  MEM0_BLOB_URL_TTL
MEM0_CLAUDE_MCP_PATH  MEM0_CLAUDE_MCP_TOKEN  MEM0_COMPILED_COLLECTION  MEM0_COMPILED_DIR
MEM0_DATASET_PATH  MEM0_EMBEDDER_API_KEY  MEM0_EMBEDDER_BASE_URL  MEM0_EMBEDDER_DIMS
MEM0_EMBEDDER_MODEL  MEM0_EMBEDDER_PROVIDER  MEM0_IMAGE_URL_INGEST  MEM0_LINT_COVERAGE_MIN
MEM0_MEMORY_CONCURRENT_READS  MEM0_METRICS_PUBLIC  MEM0_OAUTH_ACCESS_TOKEN_TTL
MEM0_OAUTH_ALLOW_REGISTRATION  MEM0_OAUTH_CLIENT_ID  MEM0_OAUTH_REFRESH_TOKEN_TTL
MEM0_OAUTH_VERBATIM_TOKEN  MEM0_OPENAI_ALLOW_NOAUTH  MEM0_OPENAI_ALLOW_WRITE  MEM0_OPENAI_MCP_PATH
MEM0_OPENAI_MCP_TOKEN  MEM0_RATE_LIMIT_SEARCHES  MEM0_RATE_LIMIT_WRITES  MEM0_SCHEMA_PATH
MEM0_STATE_DIR  MEM0_STATIC_TOKENS  MEM0_WRITEBACK_PATH
```

**Special case:** the legacy `MEM0_MCP_TOKEN` env var and its `--mcp-token` CLI alias (a
back-compat alias for `--claude-mcp-token`) are **dropped entirely**, not renamed — they are exactly
the kind of cruft this change clears. `RELIQUARY_CLAUDE_MCP_TOKEN` / `--claude-mcp-token` is the one
true name.

### Chat-completions proxy → `reliquary`

- Request payload keys: `mem0_query`, `mem0_user_id`, `mem0_limit`, `mem0_threshold`,
  `mem0_disable` → `reliquary_query`, `reliquary_user_id`, `reliquary_limit`,
  `reliquary_threshold`, `reliquary_disable`.
- Request/response headers: `x-mem0-*` → `x-reliquary-*` (e.g. `x-mem0-user-id`, `x-mem0-query`).
- Stored write tag: `source: "mem0_chat_proxy"` → `"reliquary_chat_proxy"`.

## Carve-outs (deliberately untouched)

| Surface | Why it stays |
|---|---|
| OpenAI endpoint tools `search` / `fetch` (+ the lean endpoint's unprefixed names) | OpenAI deep-research MCP contract **requires** those exact tool names. Renaming breaks ChatGPT. |
| `config.yaml` `vector_store` / `embedder` / `llm` blocks | The **Mem0 library's** own config schema, not Reliquary's. |
| Compose helpers `BLOB_HOST_DIR`, `COMPILED_HOST_DIR` | Unprefixed host-path bind-mount helpers, not `mem0_` artifacts. |
| Qdrant collection names + existing stored memory metadata | **No data migration.** Only *new* writes use new names; the corpus, collections, and old records are untouched. No re-ingest. |
| `user_id` (`my_lord`) and other data values | Data, not surface. |
| Dated specs/plans under `docs/superpowers/` | Historical records — left as written. |

The default value string `reliquary_compiled` (the compiled-collection default) is already correct.

## Startup migration guard

In `app/server.py` `main()` (before serving), check `os.environ` against the **known** former var
names (a static `LEGACY_ENV_RENAMES` map: each old `MEM0_*` → its `RELIQUARY_*` replacement, plus
`MEM0_MCP_TOKEN → RELIQUARY_CLAUDE_MCP_TOKEN`). For any still present, emit **one** prominent
`logging.warning` listing each stale var and its replacement, and stating the `MEM0_*` vars are no
longer read. **Non-fatal**, and scoped to the known set only (so unrelated `MEM0_*` vars from other
tools never trigger spurious warnings). This is the safety net for a missed `.env` rename: a
forgotten token var warns loudly instead of silently running unauthenticated.

## Files touched

- `app/server.py` — tool registration + dispatch, resource handlers/URIs, chat-proxy keys/headers,
  argparse `getenv` defaults, the startup guard, and `reliquary_capabilities` output (which lists
  tool names).
- `app/ingest.py`, `app/lint.py`, `app/export_vault.py`, `app/compiled.py`, `app/catalog.py`,
  `app/health.py` — `getenv` keys and any resource/tool-name references.
- `.env.example` — rename the 20 documented vars and their comments.
- `README.md`, `docs/GUIDE.md`, `docs/INGRESS.md` — every reference (tool table, env table, prose).
- `docker-compose.yml` / `Dockerfile` — verified: they pass no `MEM0_*` env directly (env comes
  from `.env`); confirm during implementation and leave the unprefixed compose helpers as-is.
- `tests/` — 10 files, ~95 references (heaviest: `test_compiled_server.py`, `test_governance.py`,
  `test_scopes.py`, `test_mcp_surface.py`).

## Testing

- Update every existing reference to the new names; full suite + `python -m py_compile app/*.py`
  green.
- **Regression guards (new):**
  1. No tool name and no resource URI on the **Claude endpoint** begins with `mem0` (scan the
     registered tool/resource lists).
  2. The **OpenAI endpoint still** exposes `search` and `fetch` (carve-out is intact).
  3. Setting a legacy `MEM0_*` env var produces the startup warning **and** the value is not read
     (e.g. `MEM0_CLAUDE_MCP_TOKEN` does not authenticate).
  4. The chat proxy reads `reliquary_*` payload keys / `x-reliquary-*` headers and ignores the old
     `mem0_*` ones.

## Out of scope (YAGNI)

- Renaming the Mem0 **library** config (carve-out above).
- Renaming Qdrant collections or migrating existing stored metadata.
- Renaming the compose host-path helpers (`BLOB_HOST_DIR` / `COMPILED_HOST_DIR`).
- Any deprecation shim or dual-name alias period (explicitly rejected — clean break).

## Release & migration

Ships as **`v0.3.0`** (breaking). Tagging `v0.3.0` builds + pushes to Docker Hub + GHCR and creates
a GitHub Release via the existing pipeline. After merge:

1. Update the stored Reliquary release note + add a `v0.3.0` migration note: the `MEM0_* →
   RELIQUARY_*` env rename table, "rename your `.env`, redeploy, reconnect the connector."
2. README/GUIDE already reflect the new names (changed in this PR).

The operator migration is: rename env vars in `.env` (per the table / the startup warning),
`docker compose pull && up -d`, then reconnect the Claude.ai connector once so it re-reads
`tools/list`. No data migration.
