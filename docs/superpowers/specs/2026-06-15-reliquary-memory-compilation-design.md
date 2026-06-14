# Reliquary memory compilation layer — design

**Date:** 2026-06-15
**Component:** `reliquary/` (the MCP memory server)
**Epic:** #47 — turn retrieval into maintained synthesis. Slices #48–#53.
**Folds in:** #43 (memory lint / health checks) and #30 (gate the live lexical fallback).
**Goal:** Add a curated, versioned, domain-neutral **compiled layer** of synthesized
notes above the raw corpus, so memory *compounds* over time while raw memories stay
immutable sources. Shipped as one phased PR.

## Problem

Reliquary is a retrieval engine: it RAGs the raw corpus on every query and the
answer vanishes into chat history. Karpathy's [llm-wiki gist][gist] argues memory
should compound — repeated retrieval and conversation become *maintained synthesis*
that improves over time, while raw memories stay immutable sources. This epic makes
Reliquary a **compilation engine** too, without changing its identity (no GPU, no
chat LLM, runs on a small CPU box).

[gist]: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Decisions locked during brainstorming

- **Two physically distinct layers.** Raw (append-only, high-volume — what exists
  today) and compiled (low-volume, mutable, versioned, human-curated). They never
  co-mingle in a result set.
- **Synthesis boundary — Reliquary stays LLM-free.** The *calling agent*
  (Claude/ChatGPT) authors the synthesis prose and files it back; Reliquary only
  does bookkeeping: store + version, index, record provenance, flag staleness, serve.
  Reliquary never generates prose.
- **Isolation = separate Qdrant collection** (the #51 target), reached via a second
  Mem0 `Memory` instance. A `kind=synthesis` metadata tag rides along as a label,
  **not** as the isolation mechanism. Filter-only-on-one-collection is the rejected
  trap.
- **`BlobStore` is the revision substrate, not the page model.** Content-addressing
  (`id = sha256(content)`) is right for immutable revision history but wrong for
  editable pages. A new slug-keyed registry sits on top: stable slug → current
  revision blob id + mutable frontmatter/status.
- **Direct-to-compiled.** Because the storage slice (#51) lands in this same PR,
  #48 files synthesis *directly* into the compiled collection — the issue's
  "MVP in the shared collection, migrate later" detour and its migration code are
  dropped entirely.
- **Triggers are lazy + proposed, never eager rewrite.** No internal scheduler:
  "scheduled" is a stateless cron-friendly lint CLI invoked externally.
- **Compiled layer on by default, safely.** An empty compiled collection makes the
  synthesis-first pass a no-op, so query results are identical to today until pages
  exist. Disable with `MEM0_COMPILED_COLLECTION=` (empty).

## Architecture

```
                 ┌─────────────────────────────────────────────┐
   agent  ──MCP──▶  Mem0ChatProxy (app/server.py)               │
                 │    • raw pass  → self.memory   (raw coll.)    │
                 │    • compiled  → self.compiled_memory (coll.) │
                 │    • registry  → PageRegistry (app/compiled)  │
                 │    • health    → app/health.py (pure)         │
                 └───────┬───────────────────────┬──────────────┘
                         │                        │
                   BlobStore (revisions)    Qdrant (2 collections)
                   app/blobs.py             raw + reliquary_compiled
```

- **Raw layer:** `self.memory` (existing `Memory.from_config`), the raw Qdrant
  collection. Unchanged in storage; retrieval gains a synthesis-first pre-pass.
- **Compiled layer:** `self.compiled_memory` (a second `Memory.from_config` with a
  cloned config that only overrides `vector_store.config.collection_name`, same
  embedder) for semantic recall of pages, plus `PageRegistry` for the canonical
  slug→revision mapping, frontmatter, status, history, and enumeration.
- **Revision bytes** (markdown + YAML frontmatter) live in the existing `BlobStore`
  (free dedup, atomic writes, refcount, signed URLs).

## Components

### `app/compiled.py` (new, dependency-light)

Self-contained like `blobs.py`/`catalog.py` — no Mem0/Qdrant/server imports, so it
is unit-testable in isolation. Wraps a `BlobStore` for revision bytes and owns a
registry index for the mutable page model.

```
@dataclass
class PageInfo:
    slug: str
    current_blob: str            # sha256 of the current revision's bytes
    title: str
    domain / hall / room / topic: str | None
    derived_from: list[str]      # raw memory ids this synthesis cites
    supersedes: list[str]        # slugs this page supersedes (advisory)
    status: str                  # current | stale | draft | archived
    kind: str = "synthesis"
    created_at / updated_at: float
    history: list[str]           # prior revision blob ids, newest last
    memory_id: str | None        # compiled-collection record id (set by server)

class PageRegistry:
    __init__(self, registry_dir: str, blobs: BlobStore)
    put_revision(slug, markdown, frontmatter) -> PageInfo   # create or new revision
    get(slug) -> PageInfo | None
    read_body(slug) -> tuple[str, str] | None               # (markdown, blob_id)
    list(*, domain=None, status=None) -> list[PageInfo]
    history(slug) -> list[str]
    set_status(slug, status) -> PageInfo | None              # registry-only, no new revision
    set_memory_id(slug, memory_id) -> None
    pages_deriving_from(*, ids=(), domain=None, topic=None) -> list[PageInfo]
```

- **Storage:** one JSON sidecar per page under `registry_dir`, sharded by the first
  two chars of the slug (mirrors the blob layout). The JSON holds `PageInfo`; the
  body bytes live in `BlobStore`. The **blob is immutable content; the registry
  holds the mutable pointer + status** — so flagging `stale` is a registry write
  that does **not** mint a revision.
- **Revisioning:** `put_revision` assembles `frontmatter + body`, `blobs.put(...)`
  (identical re-file dedups → no-op revision), repoints `current_blob`, appends the
  prior id to `history`.
- **Reverse lookup:** `pages_deriving_from` scans the low-volume registry — a page
  matches if it shares a `derived_from` id (primary) or shares `domain`+`topic`
  (secondary).

### `self.compiled_memory` (`app/server.py`)

Built once at startup when `MEM0_COMPILED_COLLECTION` is non-empty, from a deep-copy
of `self.config` with `vector_store.config.collection_name` overridden. Injectable
via a `compiled_memory=` constructor param (mirrors the existing `memory=` test
seam). Holds one record per page (current revision) for recall; metadata
`{kind: synthesis, source_group: compiled, slug, blob_ref, derived_from, domain…}`.

### New MCP tools (`mcp_tools_for` / `call_mcp_tool` + handlers)

| Tool | Type | Endpoints | Args |
|------|------|-----------|------|
| `mem0_compile_page` | write (gated like `add_memory`) | Claude; OpenAI iff `openai_allow_write` | `markdown` (req), `slug`, `title`, `derived_from[]`, `domain/hall/room/topic`, `status` (=`current`), `supersedes[]`, `user_id` (Claude only) |
| `mem0_list_pages` | read | both | `domain`, `status`, pagination |
| `mem0_page_history` | read | both | `slug` |

- **On-demand refresh = re-invoke `mem0_compile_page`** with the same slug (new
  revision, `status→current`). No separate tool.
- **Page fetch reuses `mem0_fetch`**, made compiled-aware: a slug or compiled
  record id resolves to the page (frontmatter + body + freshly signed `/blobs`
  URL); otherwise falls through to raw fetch.
- **No `delete_page` in v1** — use `status=archived` (hard deletion + orphaned-
  revision GC is out of scope).

### New MCP resources (extend `mcp_resources()` / `read_resource()`)

| URI | Content |
|-----|---------|
| `mem0://schema` | The constitution doc (from `MEM0_SCHEMA_PATH`, else a built-in default). Declares taxonomy, `kind`s, slug/naming conventions, compile-vs-raw rules. `text/markdown`. |
| `mem0://domain/<d>/index` | Domain overview: syntheses in that domain (registry) + recent additions. Added **alongside** the existing `mem0://domain/<d>` resource (non-breaking). |
| `mem0://recent` | Recently added/updated records (raw + compiled). |
| `mem0://needs-review` | Pages `status=stale` + coverage-gap clusters (from `health.py`). |

All read-only, generated on read from catalog + registry.

### Schema / constitution (#49)

An editable markdown doc, the agent's read-first rulebook, surfaced at
`mem0://schema`. Soft guidance, never a gate. Declares the taxonomy
(`domain/hall/room/topic` meanings), the `kind`s (`raw`/`synthesis`/…) and when to
use each, slug/naming conventions, and what to compile vs. leave raw. Source:
`MEM0_SCHEMA_PATH` if set, else a bundled default. This is the shared anchor that
the #40 governance epic will later extend (governance rules are out of scope here).

### `app/health.py` (new, dependency-light)

Pure functions over a `PageRegistry` + catalog snapshot, consumed by **both**
`mem0://needs-review` and the lint CLI (one source of truth):

- **stale pages** → propose refresh
- **coverage gaps** — a domain/topic with ≥ `MEM0_LINT_COVERAGE_MIN` raw records but
  no `current` synthesis → propose compiling one
- **orphans** — raw records lacking taxonomy (advisory)
- **stale-by-source** — a page whose `derived_from` sources updated after the page's
  revision → propose refresh
- **supersession** — a page marked `supersedes` another still `current` → flag

### Lint CLI (`python -m app.lint`) — #43 + #53 "scheduled"

Stateless, idempotent, cron-friendly. Calls `health.py`, prints a report (`--json`
for machine output). **Proposes, never rewrites.** Exit 0 always; `--strict` returns
non-zero when issues exist (cron/CI alerting). **No internal scheduler/daemon** —
external cron invokes it, matching Reliquary's deployment model (already fronted by
Caddy/Cloudflare).

## Data flows

### `mem0_compile_page` (core write)

1. Gate (write auth + endpoint); validate `markdown` non-empty.
2. Resolve slug: explicit `slug` → else `slugify(title|topic)` → else `missing_slug`.
   An existing slug ⇒ *update* (new revision), not an error.
3. Assemble payload = YAML frontmatter (`slug, title, domain/hall/room/topic,
   derived_from, supersedes, status, kind=synthesis, created_at, updated_at`) + body.
4. `info = blobs.put(payload)` → revision blob id (identical re-file ⇒ no-op revision).
5. `registry.put_revision(slug, …)` → repoint current; append prior to history.
6. Index current revision into `compiled_memory` (text = title + body projection)
   with the metadata above; persist the returned record id via `set_memory_id`.
7. Sign a `/blobs` URL for the revision.
8. Return `{slug, revision, memory_id, url, derived_from, status}`.

### Synthesis-first search (#50)

1. **Compiled pass:** `compiled_memory.search(query)` (user-scoped). Skipped (no-op)
   when the compiled layer is disabled/empty.
2. If a relevant **current** (non-`stale`) synthesis clears a confidence bar → lead
   with it; run the **raw pass** and attach top raw hits as `evidence` (citing
   `derived_from` ∪ raw hits).
3. Else (missing / `stale` / low-confidence) → raw pass only (today's path, incl.
   the now-gated lexical fallback); emit a coverage-gap signal for `needs-review`.
4. `apply_retrieval_quality` runs **per layer** — a synthesis is never deduped
   against its own sources (different collections; the point of physical separation).
5. Query-intent is a simple heuristic to start (broad/overview → synthesis-first;
   specific-fact identifier → straight to raw); refine later.

### Ingest-time staleness fan-out (#53)

Triggered on every raw add, **both** write paths — bulk `app/ingest.py` and live
`handle_add_memory_tool` (+ writeback). After the raw memory is stored:

1. Find candidate pages via `registry.pages_deriving_from(ids=[new_id],
   domain=…, topic=…)`.
2. `registry.set_status(slug, "stale")` for each (queue — **never auto-rewrite**).

**Cost guards (critical):** wrapped `try/except`, never blocks/fails the raw write.
Bulk ingest does **not** scan per-record (O(records×pages)); it accumulates touched
ids + domain/topics and runs **one** fan-out pass after the batch. Cheap because the
compiled layer is deliberately low-volume.

### `mem0_fetch` (page) / `mem0_list_pages` / `mem0_page_history`

Registry/compiled lookups; fetch returns frontmatter + body + signed URL, raw
fallback otherwise.

### #30 — gate the live lexical fallback (fold-in)

In `search_memories` (`app/server.py:2628`), the currently-unconditional
`live_lexical_matches` runs **only when** either:

- **(a) vector hits are thin** — fewer than `limit`, or top score below a threshold; or
- **(b) the query looks like an exact identifier** — contains an all-caps/hyphenated
  token (e.g. `ARDA-RELIQUARY-IMAGE-20260605-01`).

Combines #30 options 1+2 → preserves #28/#29 exact-token recall (regression tests
stay green) while skipping the 5000-record `get_all` scroll on healthy searches. The
fallback lives in the **raw pass** only.

## Configuration / runtime

New `ProxySettings` fields (env + flag), following the blob pattern:

| Field | Env | Flag | Default |
|-------|-----|------|---------|
| compiled collection | `MEM0_COMPILED_COLLECTION` | `--compiled-collection` | `reliquary_compiled` (empty ⇒ layer disabled) |
| registry dir | `MEM0_COMPILED_DIR` | `--compiled-dir` | `/data/compiled` (host bind-mount) |
| schema path | `MEM0_SCHEMA_PATH` | `--schema-path` | unset ⇒ built-in default |
| coverage-gap threshold | `MEM0_LINT_COVERAGE_MIN` | `--lint-coverage-min` | `8` |

The compiled `Memory` reuses the same embedder/llm config — only the collection name
changes. Page-revision URLs reuse the existing blob signing key + TTL.
`docker-compose.yml` `app` service gains
`${COMPILED_HOST_DIR:-./data/compiled}:/data/compiled` (same qdrant service, second
collection — no new container). `.env.example` documents the vars; `docs/GUIDE.md`
gets a compilation-layer section.

## Error handling

| Condition | MCP result |
|-----------|-----------|
| empty markdown | `missing_markdown`, is_error |
| no slug and no title/topic | `missing_slug`, is_error |
| compiled layer not configured | new tools/resources hidden; `compiled_disabled` if forced |
| unknown slug (history/fetch) | `not_found`, is_error (404 on blob route) |
| wrong endpoint / no write scope | existing gating message |
| revision over blob cap | `too_large`, is_error |

All MCP errors use the existing `mcp_tool_result(..., is_error=True)` shape.

## Testing

Dependency-light first, mirroring `tests/test_blobs.py` / `tests/test_helpers.py`:

1. **`tests/test_compiled.py`** (no Mem0): registry over a temp `BlobStore` —
   create/get; update → new revision + history; identical-refile dedup no-op;
   list-by-domain/status; `pages_deriving_from` (id + taxonomy); `set_status`
   updates registry **without** a new revision.
2. **`tests/test_health.py`** (pure): stale pages, coverage gaps (threshold),
   orphans, stale-by-source, supersession — from synthetic fixtures.
3. **Integration** (injectable `memory=` + fake `compiled_memory`): `compile_page`
   round-trip (file → index → fetch + URL); synthesis-first leads with raw evidence
   vs. stale/missing → raw + gap-flag; ingest fan-out flags a dependent page stale;
   the four new resources return expected shapes; **#30 regression** — exact-token
   recall still works **and** the fallback is skipped on healthy vector hits.
4. **Lint CLI**: `python -m app.lint --json` against a fixture — proposals + exit
   codes (0 / `--strict` nonzero).
5. `python -m py_compile app/*.py` + the full pytest suite stay green.

## Phasing (single branch, reviewable commits)

One PR, built and committed in order so review can go commit-by-commit:

1. **Storage (#51):** `app/compiled.py` (`PageRegistry` + `PageInfo`) over `BlobStore`
   + `tests/test_compiled.py`. No server wiring yet.
2. **Compiled `Memory` + config:** second `Memory` instance, `ProxySettings` fields,
   compose/`.env`/docs. Default-on, empty = no-op.
3. **File-back (#48):** `mem0_compile_page`, compiled-aware `mem0_fetch`,
   `mem0_list_pages`, `mem0_page_history` + integration tests.
4. **Synthesis-first retrieval (#50):** two-pass `search_memories` + evidence shape.
5. **Schema + indexes (#49, #52):** `mem0://schema`, `domain/<d>/index`, `recent`,
   `needs-review`.
6. **Triggers + lint (#53, #43):** ingest fan-out (both paths, batched),
   `app/health.py`, `python -m app.lint`, `tests/test_health.py`.
7. **Fold-in #30:** gate the lexical fallback + skip-path regression test.
8. **Vault export (#51):** thin one-way `vault/<domain>/<slug>.md` CLI.

## Out of scope (YAGNI)

Reliquary-side prose generation (agent authors; locked) · two-way vault sync (export
is one-way) · hard page deletion + orphaned-revision GC (use `status=archived`) ·
cross-user/shared pages (per-`user_id`) · auto-rewrite / on-ingest recompilation
(flag-only; rewrite is human-confirmed) · internal scheduler (cron is external) ·
sophisticated query-intent classification (#50 = simple heuristic) · rich server-side
diffs (revisions exposed; diffing left to clients/Obsidian) · the #40 governance
modes + capabilities tool #41 (sibling epic; schema #49 is only the shared anchor).
