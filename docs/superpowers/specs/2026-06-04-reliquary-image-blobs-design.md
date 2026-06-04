# Reliquary image blobs — design

**Date:** 2026-06-04
**Component:** `reliquary/` (the MCP memory server)
**Goal:** Let agents save generated binary files (mostly images) and fetch them
back later — discoverable both by semantic caption search and by a stable id.

## Problem

Reliquary wraps Mem0 + Qdrant, a text/embedding store. Agents reach it only
through the MCP tool surface (`mem0_add_memory`, `mem0_search`, `mem0_fetch`,
`mem0_delete`) on `/claude/mcp` and `/openai/mcp`. There is no way to persist
binary content. Agents that generate images cannot stash them for later reuse.

## Design summary

Binary blobs live on a host-mounted filesystem directory, addressed by content
hash. Each blob is paired with a normal Mem0 text memory (its caption) so the
existing semantic search and fetch tools find it. Three new MCP tools and one
authed HTTP route expose the store. Mem0/Qdrant stays text-only.

Decisions locked during brainstorming:

- **Retrieval:** both — semantic (caption embedded in Mem0) *and* by stable id.
- **Transport:** base64 over MCP as the primary path; a signed `GET` URL as the
  escape hatch for large downloads.
- **Storage:** local filesystem, **host bind-mount** so the user can browse and
  back up the files outside the container.
- **Guardrails in scope:** signed/expiring URLs and delete support. One generous
  size cap (30 MB, configurable, disable-able). **No MIME allowlist** — arbitrary
  binary is accepted; images are simply the common case. No new write-gate env
  var beyond reusing the existing `add_memory`/`delete` gating.

## Components

### `app/blobs.py` (new, dependency-light)

A self-contained `BlobStore` that depends only on a configured directory and a
signing key. No Mem0/Qdrant imports, so it is unit-testable in isolation like
`helpers`/`catalog`.

```
class BlobInfo:        # id (sha256 hex), mimetype, size, ext, ref_count, created
class BlobStore:
    __init__(self, blob_dir: str, signing_key: bytes, max_bytes: int)
    put(self, data: bytes, *, mimetype: str | None = None) -> BlobInfo
    get(self, blob_id: str) -> tuple[bytes, str] | None     # (data, mimetype)
    info(self, blob_id: str) -> BlobInfo | None
    incref(self, blob_id: str) -> int
    delete(self, blob_id: str) -> bool                      # decref; unlink at 0
    sign(self, blob_id: str, ttl_seconds: int) -> tuple[int, str]   # (exp, sig)
    verify(self, blob_id: str, exp: int, sig: str) -> bool
```

Layout under `blob_dir` (sharded by first 2 hex chars of the id to avoid huge
flat dirs):

```
blob_dir/<ab>/<sha256>.<ext>        # the raw bytes, with a human-friendly ext
blob_dir/<ab>/<sha256>.json         # sidecar: {mimetype, size, ext, ref_count, created}
```

- **id = sha256 hex** of the content → natural dedup and a stable id.
- **ext** is derived from the sniffed mimetype (`.png`, `.jpg`, `.webp`,
  `.gif`, …; `.bin` fallback) purely so the files open in a viewer when the
  user browses the directory. The id never includes the extension.
- **mimetype** is sniffed from magic bytes; a caller-supplied `mimetype` is used
  only as a fallback when sniffing is inconclusive. Stored, never enforced.
- **ref_count** in the sidecar: `put` of an already-present hash increments it;
  `delete` decrements and unlinks bytes + sidecar only at zero. This makes
  delete safe when two captions point at the same identical image.
- **Signing:** HMAC-SHA256 over `f"{blob_id}|{exp}"` keyed by `signing_key`,
  hex-encoded. `verify` checks `secrets.compare_digest` and `exp > now`.
- **Size cap:** `put` raises `BlobTooLarge` when `max_bytes > 0 and
  len(data) > max_bytes`.

### `ProxySettings` additions (`app/server.py`)

| Field | Env | Flag | Default |
|-------|-----|------|---------|
| `blob_dir` | `MEM0_BLOB_DIR` | `--blob-dir` | `/data/blobs` |
| `blob_signing_key` | `MEM0_BLOB_SIGNING_KEY` | `--blob-signing-key` | random per-process if unset (like OAuth tokens) |
| `blob_max_bytes` | `MEM0_BLOB_MAX_BYTES` | `--blob-max-bytes` | `31457280` (30 MB); `0` disables |
| `blob_url_ttl` | `MEM0_BLOB_URL_TTL` | `--blob-url-ttl` | `3600` (seconds) |

A `BlobStore` is constructed once at startup from these settings and held on the
proxy instance (`self.blobs`).

### `mcp_tool_result` extension

Add an optional `image: tuple[str, str] | None = None` (base64, mimetype)
parameter. When present, the returned `content` list gains an MCP image block
`{"type": "image", "data": <base64>, "mimeType": <mimetype>}` alongside the text
block. Existing callers are unaffected (default `None`).

### New MCP tools

Registered in `mcp_tools_for` and dispatched in `call_mcp_tool`.

- **`add_image`** — write tool. Gated exactly like `add_memory`: exposed on the
  Claude endpoint; on the OpenAI endpoint only when `openai_allow_write`.
  Args: `caption` (string, required), `image_base64` (string, required),
  `mimetype` (string, optional fallback), and the same optional
  `metadata`/`title`/`user_id`/`domain`/`hall`/`room`/`topic` keys
  `add_memory` accepts.
- **`fetch_image`** — read tool, available on both endpoints (mirrors `fetch`).
  Args: `id` (string, required) — the blob id.
- **`delete_image`** — write tool, gated like `delete`. Args: `memory_id`
  (required — the id `add_image` returned); `user_id` honoured only on the
  Claude endpoint. The blob id is read from that memory's `blob_ref` metadata,
  so no fuzzy lookup is needed.

### New HTTP route: `GET /blobs/{id}`

Added to the `__call__` router before the MCP/passthrough fallthrough.
Authorizes via **either** a valid signed query (`?exp=…&sig=…`) **or** the Claude
bearer (`_require_claude_auth`). On success streams the bytes with the stored
`Content-Type`. 403 on bad/expired signature and no bearer; 404 on unknown id.

## Data flow

### add_image
1. Validate `caption` non-empty, decode `image_base64` (→ `invalid_image` on
   failure), enforce size cap (→ `too_large`).
2. `info = self.blobs.put(data, mimetype=arg_mimetype)`.
3. Build metadata and create the caption memory by reusing the
   `handle_add_memory_tool` path with `text=caption` and metadata:
   `kind="image"`, `blob_ref=info.id`, `blob_mime=info.mimetype`,
   `blob_size=info.size`, plus the standard `source="mcp"`,
   `source_group="user-write"`.
4. Build a signed url: `exp, sig = self.blobs.sign(info.id, blob_url_ttl)` →
   `"/blobs/{id}?exp={exp}&sig={sig}"`.
5. Return `mcp_tool_result(text="Stored image … (blob_id=…, memory_id=…)",
   structured={"blob_id", "memory_id", "url", "mimetype", "size", "user_id"})`.

### Discovery via existing search/fetch
- `_enrich_hit` and `_document_url`: when a hit's metadata carries `blob_ref`,
  set the result `url` to a freshly signed `/blobs/{id}` instead of the
  `mem0://record/…` placeholder, and surface `blob_ref`/`blob_mime` in the
  emitted metadata. So `mem0_search("the dragon logo")` returns the caption hit
  with a working image URL; `mem0_fetch(memory_id)` returns the caption document
  plus that URL.

### fetch_image
1. `result = self.blobs.get(id)`; `not_found` if `None`.
2. `data, mimetype = result`; base64-encode `data`.
3. Return `mcp_tool_result(text="…", structured={"blob_id", "url"(freshly
   signed), "mimetype", "size"}, image=(b64, mimetype))`. The inline image is
   the primary payload; the signed `url` is the large-file escape hatch.

### delete_image
1. Resolve the caption memory by `memory_id` via `fetch_live_memory` and apply
   `handle_delete_tool`'s guards: only `source_group="user-write"` memories
   owned by the effective user are deletable. Read `blob_ref` from its metadata
   (→ `not_an_image` if absent).
2. Delete the caption memory, then `self.blobs.delete(blob_ref)` (decref; unlink
   at zero).
3. Return a result reporting `{deleted: true, blob_id, memory_id,
   blob_unlinked: bool}`.

### GET /blobs/{id}
1. Parse `exp`/`sig` from the query string.
2. Authorized if `self.blobs.verify(id, exp, sig)` **or**
   `_require_claude_auth(headers)`.
3. `self.blobs.get(id)` → 404 if missing; else 200 with `Content-Type` =
   stored mimetype and the raw bytes.

## Deployment / runtime config

The blob directory must be reachable from the host for browsing and backup, so
it is a **bind mount**, not a named Docker volume.

`docker-compose.yml` `app` service gains:

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ${BLOB_HOST_DIR:-./data/blobs}:/data/blobs
```

`.env.example` documents the new vars:

```
# Where saved blobs (images, etc.) live on the host — browse/back up this dir.
BLOB_HOST_DIR=./data/blobs
# Max upload size in bytes (0 disables the cap). Default 30 MB.
MEM0_BLOB_MAX_BYTES=31457280
# HMAC key for signed blob URLs. Leave blank for a random per-process key
# (signed URLs then invalidate on restart); set a fixed value for stable URLs.
MEM0_BLOB_SIGNING_KEY=
# Signed blob URL lifetime in seconds.
MEM0_BLOB_URL_TTL=3600
```

`MEM0_BLOB_DIR` stays at the in-container default `/data/blobs`; users remap the
host side via `BLOB_HOST_DIR`.

## Error handling

| Condition | MCP result | HTTP route |
|-----------|-----------|------------|
| empty caption | `missing_caption`, `is_error` | — |
| undecodable base64 | `invalid_image`, `is_error` | — |
| over size cap | `too_large` (with `max_bytes`), `is_error` | — |
| unknown blob id | `not_found`, `is_error` | 404 |
| bad/expired signature, no bearer | — | 403 |
| not a user-written memory | `protected_record`/`not_found`, `is_error` | — |
| memory_id has no `blob_ref` | `not_an_image`, `is_error` | — |
| tool on wrong endpoint | "not available on … endpoint" | — |

All MCP errors use the existing `mcp_tool_result(..., is_error=True)` shape.

## Testing

`tests/test_blobs.py`, dependency-light (no Mem0/Qdrant), following
`tests/test_helpers.py`:

- put → get round-trip returns identical bytes + sniffed mimetype.
- sha256 dedup: putting identical bytes twice yields one file, `ref_count == 2`.
- sidecar contents (mimetype, size, ext, ref_count, created) are correct.
- extension derivation per common image type, `.bin` fallback for unknown.
- `delete` decrements ref_count; bytes + sidecar persist until count hits 0,
  then are unlinked.
- `sign`/`verify` round-trip; tampered sig rejected; expired `exp` rejected.
- size cap: `put` of oversized data raises `BlobTooLarge`; `max_bytes=0`
  disables the check.

`python -m py_compile app/*.py` and the existing pytest suite must still pass.

## Out of scope (YAGNI)

- MIME allowlisting / content moderation.
- Object-store (S3/MinIO) backend.
- Thumbnailing, transforms, or EXIF stripping.
- Multi-node / shared storage.
- A separate write-gate env var for images beyond reusing `openai_allow_write`.
