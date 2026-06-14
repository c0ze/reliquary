# Memory Compilation Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a curated, versioned, domain-neutral compiled (synthesis) layer above Reliquary's raw corpus, so the agent can file synthesized answers back, retrieve synthesis-first, and keep pages maintained — without Reliquary ever generating prose or running an LLM.

**Architecture:** A new dependency-light `PageRegistry` (`app/compiled.py`) maps stable slugs to immutable revisions stored in the existing content-addressed `BlobStore`; the registry holds the mutable current-pointer + status. A second Mem0 `Memory` instance on its own Qdrant collection (`self.compiled_memory`) indexes the current revision of each page for recall. New MCP tools let the agent file/list/inspect pages; retrieval consults the compiled layer first and falls back to raw. Lazy triggers flag stale pages on ingest; a cron-friendly lint CLI proposes (never applies) refreshes.

**Tech Stack:** Python 3.12+, stdlib-only for `compiled.py`/`health.py` (matching `blobs.py`/`catalog.py`), Mem0 + Qdrant for vectors, pytest. ASGI server in `app/server.py`.

**Spec:** [`docs/superpowers/specs/2026-06-15-reliquary-memory-compilation-design.md`](../specs/2026-06-15-reliquary-memory-compilation-design.md)

---

## File Structure

**Create:**
- `app/compiled.py` — `PageInfo` dataclass + `PageRegistry` (slug→revision over `BlobStore`) + `slugify`.
- `app/health.py` — pure health-check functions (stale, coverage gaps, orphans, stale-by-source, supersession) shared by `mem0://needs-review` and the lint CLI.
- `app/lint.py` — `python -m app.lint` CLI wrapper over `health.py`.
- `tests/test_compiled.py`, `tests/test_health.py` — dependency-light unit tests.

**Modify:**
- `app/server.py` — `ProxySettings` fields; `__init__` builds `self.compiled_memory` + `self.pages`; new tool defs in `mcp_tools_for`; dispatch in `call_mcp_tool`; `handle_compile_page_tool` / `handle_list_pages_tool` / `handle_page_history_tool`; compiled-aware `handle_fetch_tool`; synthesis-first `search_memories`; gated `live_lexical_matches`; new resources in `mcp_resources`/`read_resource`; ingest fan-out hook in `handle_add_memory_tool`; argparse + `build_settings`.
- `app/ingest.py` — batched fan-out after bulk add.
- `tests/conftest.py` — inject a second `FakeMemory` as `compiled_memory`; default a `compiled_dir`.
- `docker-compose.yml`, `.env.example`, `docs/GUIDE.md` — config + docs.

**Detail level:** Phases 1–3 are full step-level TDD (the working core: store → wire → file-back). Phases 4–8 are task roadmaps with the novel code and test names; expand each to step-level when reached (per subagent-driven-development), so wiring is written against the settled APIs from phases 1–3.

---

## Phase 1 — Compiled-layer storage (`PageRegistry`) — #51

Standalone, no server wiring. Produces a tested module.

### Task 1.1: `slugify` + `PageInfo`

**Files:** Create `app/compiled.py`; Test `tests/test_compiled.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_compiled.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from blobs import BlobStore  # noqa: E402
from compiled import PageInfo, PageRegistry, slugify  # noqa: E402


def test_slugify_normalizes():
    assert slugify("Brigid: Goddess of the Forge!") == "brigid-goddess-of-the-forge"
    assert slugify("  Multiple   Spaces ") == "multiple-spaces"
    assert slugify("already-a-slug") == "already-a-slug"


def test_pageinfo_defaults():
    info = PageInfo(slug="x", current_blob="abc")
    assert info.status == "current"
    assert info.kind == "synthesis"
    assert info.derived_from == [] and info.history == []
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_compiled.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'compiled'`.

- [ ] **Step 3: Implement `app/compiled.py` header + `slugify` + `PageInfo`**

```python
"""Slug-keyed page registry for Reliquary's compiled (synthesis) layer.

Dependency-light: stdlib + a BlobStore for revision bytes. No Mem0/Qdrant/server
imports, so it is unit-testable in isolation like blobs.py / catalog.py.

A *page* is the mutable unit of the compiled layer: a stable slug whose content is
a sequence of immutable revisions. Each revision's bytes (markdown + YAML
frontmatter) live in the content-addressed BlobStore; the registry holds the
mutable pointer to the current revision plus frontmatter and status. Flagging a
page ``stale`` is a registry write and does NOT mint a new revision.

Layout under ``registry_dir`` (sharded by the first two chars of the slug):

    <registry_dir>/<sl>/<slug>.json     PageInfo as JSON
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blobs import BlobStore

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REGISTRY_LOCK = threading.Lock()

VALID_STATUSES = ("current", "stale", "draft", "archived")


def slugify(value: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics to single hyphens, trim."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class PageInfo:
    slug: str
    current_blob: str
    title: str = ""
    domain: str | None = None
    hall: str | None = None
    room: str | None = None
    topic: str | None = None
    derived_from: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    status: str = "current"
    kind: str = "synthesis"
    created_at: float = 0.0
    updated_at: float = 0.0
    history: list[str] = field(default_factory=list)
    memory_id: str | None = None
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_compiled.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/compiled.py tests/test_compiled.py
git commit -m "feat(#51): PageInfo + slugify for the compiled layer"
```

### Task 1.2: `PageRegistry.put_revision` / `get` / `read_body`

**Files:** Modify `app/compiled.py`; `tests/test_compiled.py`

- [ ] **Step 1: Write the failing test**

```python
def _registry(tmp_path):
    blobs = BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0)
    return PageRegistry(registry_dir=str(tmp_path / "reg"), blobs=blobs)


def test_create_get_read_roundtrip(tmp_path):
    reg = _registry(tmp_path)
    info = reg.put_revision("brigid", "# Brigid\n\nForge goddess.",
                            {"title": "Brigid", "domain": "pagan", "derived_from": ["r1", "r2"]})
    assert info.slug == "brigid"
    assert info.title == "Brigid" and info.domain == "pagan"
    assert info.derived_from == ["r1", "r2"]
    assert info.created_at > 0 and info.updated_at == info.created_at
    got = reg.get("brigid")
    assert got is not None and got.current_blob == info.current_blob
    body, blob_id = reg.read_body("brigid")
    assert "Forge goddess." in body and blob_id == info.current_blob
    assert "---\ntitle: Brigid" in body  # frontmatter prepended


def test_get_unknown_returns_none(tmp_path):
    reg = _registry(tmp_path)
    assert reg.get("missing") is None
    assert reg.read_body("missing") is None
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_compiled.py -q`
Expected: FAIL — `AttributeError: ... 'PageRegistry'` / cannot import `PageRegistry`.

- [ ] **Step 3: Implement the registry core**

Append to `app/compiled.py`:

```python
def _emit_frontmatter(info: "PageInfo") -> str:
    """Minimal one-way YAML frontmatter for Obsidian/serving. The registry's JSON
    sidecar is the source of truth; this is never parsed back."""
    def scalar(v: object) -> str:
        return "" if v is None else str(v)

    lines = ["---"]
    for key in ("slug", "title", "domain", "hall", "room", "topic", "status", "kind"):
        val = getattr(info, key)
        if val:
            lines.append(f"{key}: {scalar(val)}")
    for key in ("derived_from", "supersedes"):
        vals = getattr(info, key)
        if vals:
            lines.append(f"{key}: [{', '.join(scalar(v) for v in vals)}]")
    lines.append(f"updated_at: {info.updated_at}")
    lines.append("---")
    return "\n".join(lines)


def assemble_markdown(info: "PageInfo", body: str) -> str:
    return f"{_emit_frontmatter(info)}\n\n{body.strip()}\n"


class PageRegistry:
    def __init__(self, registry_dir: str, blobs: "BlobStore") -> None:
        self.registry_dir = registry_dir
        self.blobs = blobs
        os.makedirs(self.registry_dir, exist_ok=True)

    # --- paths ---
    def _shard_dir(self, slug: str) -> str:
        return os.path.join(self.registry_dir, slug[:2])

    def _path(self, slug: str) -> str:
        return os.path.join(self._shard_dir(slug), f"{slug}.json")

    # --- read ---
    def get(self, slug: str) -> "PageInfo | None":
        try:
            with open(self._path(slug), "r", encoding="utf-8") as fh:
                return PageInfo(**json.load(fh))
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def read_body(self, slug: str) -> "tuple[str, str] | None":
        info = self.get(slug)
        if info is None:
            return None
        result = self.blobs.get(info.current_blob)
        if result is None:
            return None
        data, _mime = result
        return data.decode("utf-8"), info.current_blob

    # --- write ---
    def _save(self, info: "PageInfo") -> None:
        os.makedirs(self._shard_dir(info.slug), exist_ok=True)
        path = self._path(info.slug)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh)
        os.replace(tmp, path)

    def put_revision(self, slug: str, body: str, frontmatter: dict) -> "PageInfo":
        slug = slugify(slug)
        if not slug:
            raise ValueError("empty slug")
        now = time.time()
        with _REGISTRY_LOCK:
            existing = self.get(slug)
            info = existing or PageInfo(slug=slug, current_blob="", created_at=now)
            # apply frontmatter fields
            for key in ("title", "domain", "hall", "room", "topic", "status", "kind"):
                if frontmatter.get(key) is not None:
                    setattr(info, key, frontmatter[key])
            for key in ("derived_from", "supersedes"):
                if frontmatter.get(key) is not None:
                    setattr(info, key, [str(v) for v in frontmatter[key]])
            info.updated_at = now
            if not info.created_at:
                info.created_at = now
            # store revision bytes (frontmatter + body) in the BlobStore
            blob_info = self.blobs.put(assemble_markdown(info, body).encode("utf-8"),
                                       mimetype="text/markdown")
            if existing and existing.current_blob and existing.current_blob != blob_info.id:
                info.history.append(existing.current_blob)
            info.current_blob = blob_info.id
            self._save(info)
            return info
```

> Note: `assemble_markdown` is called once to compute bytes; because the blob id is `sha256(content)`, an identical re-file yields the same id and `BlobStore.put` just increments ref_count — a no-op revision (tested in 1.3). `updated_at` is embedded in the frontmatter, so a genuine no-content-change re-file at a later second changes the bytes (new `updated_at`) and therefore mints a revision; tests pin behavior with a frozen clock where needed.

- [ ] **Step 4: Run, verify pass**

Run: `python -m pytest tests/test_compiled.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/compiled.py tests/test_compiled.py
git commit -m "feat(#51): PageRegistry put_revision/get/read_body over BlobStore"
```

### Task 1.3: revisions + history + identical-refile dedup

**Files:** `tests/test_compiled.py` (registry already supports this; this task locks behavior)

- [ ] **Step 1: Write the failing test**

```python
def test_update_creates_revision_and_history(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    import compiled
    t = [1000.0]
    monkeypatch.setattr(compiled.time, "time", lambda: t[0])
    v1 = reg.put_revision("brigid", "v1 body", {"title": "Brigid"})
    t[0] = 2000.0
    v2 = reg.put_revision("brigid", "v2 body", {"title": "Brigid"})
    assert v2.current_blob != v1.current_blob
    assert v1.current_blob in v2.history
    body, _ = reg.read_body("brigid")
    assert "v2 body" in body and "v1 body" not in body


def test_identical_refile_is_noop_revision(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    import compiled
    monkeypatch.setattr(compiled.time, "time", lambda: 1000.0)  # frozen clock
    a = reg.put_revision("p", "same", {"title": "P"})
    b = reg.put_revision("p", "same", {"title": "P"})
    assert a.current_blob == b.current_blob
    assert b.history == []  # no new revision recorded
```

- [ ] **Step 2: Run** — Expected: PASS (logic already implemented in 1.2; this pins it). If `test_identical_refile` fails, ensure `history.append` only runs when `existing.current_blob != blob_info.id`.

- [ ] **Step 3:** (no impl change expected) — if a test fails, fix `put_revision` accordingly.

- [ ] **Step 4: Commit**

```bash
git add tests/test_compiled.py
git commit -m "test(#51): pin revisioning + identical-refile dedup"
```

### Task 1.4: `list` / `history` / `set_status` / `set_memory_id` / `pages_deriving_from`

**Files:** Modify `app/compiled.py`; `tests/test_compiled.py`

- [ ] **Step 1: Write the failing test**

```python
def test_list_history_status_provenance(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("brigid", "b", {"domain": "pagan", "topic": "deities", "derived_from": ["r1"]})
    reg.put_revision("morrigan", "m", {"domain": "pagan", "topic": "deities", "derived_from": ["r2"]})
    reg.put_revision("docker", "d", {"domain": "infra", "topic": "containers", "derived_from": ["r3"]})

    assert {p.slug for p in reg.list(domain="pagan")} == {"brigid", "morrigan"}

    reg.set_status("brigid", "stale")
    assert reg.get("brigid").status == "stale"
    assert {p.slug for p in reg.list(status="stale")} == {"brigid"}
    # status flag did NOT mint a revision
    assert reg.get("brigid").history == []

    reg.set_memory_id("brigid", "mem-99")
    assert reg.get("brigid").memory_id == "mem-99"

    # provenance: by id and by domain+topic
    by_id = {p.slug for p in reg.pages_deriving_from(ids=["r2"])}
    assert by_id == {"morrigan"}
    by_tax = {p.slug for p in reg.pages_deriving_from(domain="pagan", topic="deities")}
    assert by_tax == {"brigid", "morrigan"}
```

- [ ] **Step 2: Run, verify failure** — `AttributeError: 'PageRegistry' object has no attribute 'list'`.

- [ ] **Step 3: Implement**

Append to `PageRegistry`:

```python
    def _iter_pages(self):
        for shard in os.listdir(self.registry_dir):
            shard_path = os.path.join(self.registry_dir, shard)
            if not os.path.isdir(shard_path):
                continue
            for name in os.listdir(shard_path):
                if name.endswith(".json"):
                    info = self.get(name[:-5])
                    if info is not None:
                        yield info

    def list(self, *, domain: str | None = None, status: str | None = None) -> "list[PageInfo]":
        out = []
        for info in self._iter_pages():
            if domain is not None and info.domain != domain:
                continue
            if status is not None and info.status != status:
                continue
            out.append(info)
        return sorted(out, key=lambda p: p.updated_at, reverse=True)

    def history(self, slug: str) -> "list[str]":
        info = self.get(slug)
        return list(info.history) if info else []

    def set_status(self, slug: str, status: str) -> "PageInfo | None":
        with _REGISTRY_LOCK:
            info = self.get(slug)
            if info is None:
                return None
            info.status = status
            info.updated_at = time.time()
            self._save(info)
            return info

    def set_memory_id(self, slug: str, memory_id: str) -> None:
        with _REGISTRY_LOCK:
            info = self.get(slug)
            if info is None:
                return
            info.memory_id = memory_id
            self._save(info)

    def pages_deriving_from(self, *, ids=(), domain: str | None = None,
                            topic: str | None = None) -> "list[PageInfo]":
        idset = set(ids)
        out = []
        for info in self._iter_pages():
            if idset and idset.intersection(info.derived_from):
                out.append(info)
            elif domain and topic and info.domain == domain and info.topic == topic:
                out.append(info)
        return out
```

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_compiled.py -q`

- [ ] **Step 5: Commit**

```bash
git add app/compiled.py tests/test_compiled.py
git commit -m "feat(#51): list/history/status/provenance on PageRegistry"
```

---

## Phase 2 — Compiled `Memory` + config wiring

### Task 2.1: `ProxySettings` fields + argparse + `build_settings`

**Files:** Modify `app/server.py` (`ProxySettings` ~line 207; argparse ~line 3518; `build_settings`)

- [ ] **Step 1:** Add fields to `ProxySettings` after the `blob_*` fields:

```python
    compiled_collection: str = "reliquary_compiled"
    compiled_dir: str = "/data/compiled"
    schema_path: str | None = None
    lint_coverage_min: int = 8
```

- [ ] **Step 2:** Add argparse options (after `--image-url-ingest`, before `def main`):

```python
    parser.add_argument("--compiled-collection", default=os.getenv("MEM0_COMPILED_COLLECTION", "reliquary_compiled"),
                        help="Qdrant collection for the compiled synthesis layer. Empty disables the layer.")
    parser.add_argument("--compiled-dir", default=os.getenv("MEM0_COMPILED_DIR", "/data/compiled"),
                        help="Host directory for the page registry + vault export.")
    parser.add_argument("--schema-path", default=os.getenv("MEM0_SCHEMA_PATH"),
                        help="Path to the editable memory constitution (mem0://schema). Unset uses a built-in default.")
    parser.add_argument("--lint-coverage-min", type=int, default=int(os.getenv("MEM0_LINT_COVERAGE_MIN", "8")),
                        help="Min raw records in a domain/topic with no synthesis before lint flags a coverage gap.")
```

- [ ] **Step 3:** In `build_settings(args)`, map the four new fields (mirroring the `blob_*` mappings): `compiled_collection=args.compiled_collection, compiled_dir=args.compiled_dir, schema_path=args.schema_path, lint_coverage_min=args.lint_coverage_min`.

- [ ] **Step 4: Verify compile** — `python -m py_compile app/server.py` → no error.

- [ ] **Step 5: Commit** — `git commit -am "feat(#47): compiled-layer settings + flags"`

### Task 2.2: build `self.compiled_memory` + `self.pages` in `__init__`

**Files:** Modify `app/server.py` `Mem0ChatProxy.__init__`; `tests/conftest.py`

- [ ] **Step 1: Write the failing test** — `tests/test_compiled_server.py`:

```python
def test_proxy_builds_compiled_layer(proxy):
    assert proxy.pages is not None
    assert proxy.compiled_memory is not None


def test_compiled_layer_disabled_when_no_collection(make_proxy):
    p = make_proxy(compiled_collection="")
    assert p.pages is None
    assert p.compiled_memory is None
```

- [ ] **Step 2:** Update `tests/conftest.py` so the proxy fixtures inject a second fake + a compiled dir. Add a `compiled_memory` param to `Mem0ChatProxy` (Step 3). In `proxy` fixture add `compiled_dir=str(tmp_path / "compiled")` to `ProxySettings(...)` and pass `compiled_memory=FakeMemory()` to `Mem0ChatProxy(...)`. In `make_proxy._make`, add `compiled_dir=str(tmp_path / "compiled")` to `opts` and `compiled_memory=FakeMemory()` to the constructor (allow override).

- [ ] **Step 3:** Modify `Mem0ChatProxy.__init__` signature and body:

```python
    def __init__(self, settings: ProxySettings, *, memory: Any = None, compiled_memory: Any = None) -> None:
```

After `self.blobs = BlobStore(...)` (and `self._load_pending_uploads()`), add:

```python
        self.pages = None
        self.compiled_memory = None
        if settings.compiled_collection:
            from compiled import PageRegistry
            self.pages = PageRegistry(registry_dir=settings.compiled_dir, blobs=self.blobs)
            if compiled_memory is not None:
                self.compiled_memory = compiled_memory
            else:
                import copy
                from mem0 import Memory
                compiled_config = copy.deepcopy(self.config)
                compiled_config.setdefault("vector_store", {}).setdefault("config", {})
                compiled_config["vector_store"]["config"]["collection_name"] = settings.compiled_collection
                self.compiled_memory = Memory.from_config(compiled_config)
```

- [ ] **Step 4: Run** — `python -m pytest tests/test_compiled_server.py -q` → PASS. Run full suite: `python -m pytest -q` → green (conftest change must not break existing tests).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(#47): construct compiled_memory + page registry"`

### Task 2.3: docker-compose + .env + docs

- [ ] Add `${COMPILED_HOST_DIR:-./data/compiled}:/data/compiled` to the `app` service `volumes` in `docker-compose.yml`.
- [ ] Document `MEM0_COMPILED_COLLECTION`, `COMPILED_HOST_DIR`, `MEM0_SCHEMA_PATH`, `MEM0_LINT_COVERAGE_MIN` in `.env.example`.
- [ ] Add a "Compilation layer" section to `docs/GUIDE.md` (what it is, on-by-default, disable with empty collection).
- [ ] Commit: `git commit -am "docs(#47): compiled-layer compose + env + guide"`

---

## Phase 3 — File answers back (#48)

### Task 3.1: `_index_compiled_page` helper

**Files:** Modify `app/server.py`; `tests/test_compiled_server.py`

- [ ] **Step 1: Test** — files a page, asserts a record lands in `compiled_memory` with `kind=synthesis`, `source_group=compiled`, `slug`, `blob_ref`.
- [ ] **Step 2: Run** → fail (`handle_compile_page_tool` missing).
- [ ] **Step 3: Implement** the indexing helper (add to `Mem0ChatProxy`):

```python
    async def _index_compiled_page(self, info, body, *, user_id, metadata):
        text = f"{info.title}\n\n{body}".strip() if info.title else body
        async with self.memory_lock.write():
            if info.memory_id:
                try:
                    await asyncio.to_thread(self.compiled_memory.update, info.memory_id, text, metadata=metadata)
                    return info.memory_id
                except Exception:
                    LOG.exception("Compiled re-index failed for slug=%s; adding fresh", info.slug)
            result = await asyncio.to_thread(self.compiled_memory.add, text, user_id=user_id, metadata=metadata, infer=False)
        ids = added_memory_ids(result)
        return ids[0] if ids else None
```

- [ ] **Step 4/5:** Run → pass; commit `feat(#48): index compiled pages into the compiled collection`.

### Task 3.2: `handle_compile_page_tool`

**Files:** Modify `app/server.py`; `tests/test_compiled_server.py`

- [ ] **Step 1: Test** the full round-trip: call handler with `markdown`, `title`, `derived_from`; assert structured `{slug, revision, memory_id, url, status}`, page exists in `proxy.pages`, body retrievable, `url` is a signed `/blobs/...`. Add error tests: empty markdown → `missing_markdown`; no slug/title/topic → `missing_slug`; compiled disabled (`make_proxy(compiled_collection="")`) → `compiled_disabled`.
- [ ] **Step 2: Run** → fail.
- [ ] **Step 3: Implement** (mirrors `handle_add_memory_tool` + `handle_add_image_tool`):

```python
    async def handle_compile_page_tool(self, arguments: dict[str, Any], *, allow_user_id: bool = False) -> dict[str, Any]:
        if self.pages is None or self.compiled_memory is None:
            return self.mcp_tool_result(text="The compiled layer is not configured.",
                                        structured={"error": "compiled_disabled"}, is_error=True)
        from compiled import slugify
        markdown = str(arguments.get("markdown") or "").strip()
        if not markdown:
            return self.mcp_tool_result(text="A non-empty `markdown` is required.",
                                        structured={"error": "missing_markdown"}, is_error=True)
        title = str(arguments.get("title") or "").strip()
        slug = slugify(str(arguments.get("slug") or "") or title or str(arguments.get("topic") or ""))
        if not slug:
            return self.mcp_tool_result(text="Provide a `slug`, `title`, or `topic` to name the page.",
                                        structured={"error": "missing_slug"}, is_error=True)
        derived_from = [str(x) for x in (arguments.get("derived_from") or []) if str(x).strip()]
        supersedes = [str(x) for x in (arguments.get("supersedes") or []) if str(x).strip()]
        status = str(arguments.get("status") or "current")
        frontmatter: dict[str, Any] = {"title": title or slug, "status": status,
                                       "derived_from": derived_from, "supersedes": supersedes}
        for key in ("domain", "hall", "room", "topic"):
            value = arguments.get(key)
            if value is not None and str(value).strip():
                frontmatter[key] = str(value).strip()
        try:
            info = await asyncio.to_thread(self.pages.put_revision, slug, markdown, frontmatter)
        except BlobTooLarge as exc:
            return self.mcp_tool_result(text=str(exc), structured={"error": "too_large", "max_bytes": exc.max_bytes}, is_error=True)
        user_id = str(arguments.get("user_id") or self.settings.user_id) if allow_user_id else self.settings.user_id
        metadata = {"kind": "synthesis", "source_group": "compiled", "slug": slug,
                    "blob_ref": info.current_blob, "derived_from": derived_from}
        for key in ("domain", "hall", "room", "topic"):
            if getattr(info, key):
                metadata[key] = getattr(info, key)
        memory_id = await self._index_compiled_page(info, markdown, user_id=user_id, metadata=metadata)
        if memory_id:
            self.pages.set_memory_id(slug, memory_id)
        url = self._signed_blob_url(info.current_blob)
        return self.mcp_tool_result(
            text=f"Filed synthesis page slug={slug} (revision {info.current_blob[:12]}, status={status}).",
            structured={"slug": slug, "revision": info.current_blob, "memory_id": memory_id,
                        "url": url, "derived_from": derived_from, "status": status})
```

- [ ] **Step 4/5:** Run → pass; commit `feat(#48): mem0_compile_page handler`.

### Task 3.3: `handle_list_pages_tool` + `handle_page_history_tool`

- [ ] **Step 1: Test** list (by domain/status) and history return expected structured shapes; `compiled_disabled` when off.
- [ ] **Step 3: Implement** thin handlers over `self.pages.list(...)` and `self.pages.history(slug)`, each guarded by the `self.pages is None` check, returning `mcp_tool_result` with a `pages`/`revisions` array.
- [ ] **Step 5:** Commit `feat(#48): list_pages + page_history handlers`.

### Task 3.4: register + dispatch the tools

**Files:** Modify `app/server.py` `mcp_tools_for` (add to `claude_tools` write block, and the `openai` `can_write` block; reads `mem0_list_pages`/`mem0_page_history` on both) and `call_mcp_tool`.

- [ ] **Step 1: Test** (`tests/test_compiled_server.py` driving `call_mcp_tool`): on the claude profile with `can_write=True`, `mem0_compile_page` then `mem0_list_pages` then `mem0_page_history` succeed; without `can_write`, `mem0_compile_page` → `insufficient_scope`.
- [ ] **Step 3: Implement** — tool defs mirroring `mem0_add_memory` (compile_page: properties `markdown` req, `slug`, `title`, `derived_from` array, `supersedes` array, `domain/hall/room/topic`, `status`, `user_id`; list_pages: `domain`,`status`; page_history: `slug` req). Dispatch entries mirroring the `mem0_add_memory`/`mem0_fetch` cases (compile_page write-gated with the `insufficient_scope` guard, `allow_user_id=True` on claude / `False` on openai-lean; list/history read, no gate).
- [ ] **Step 5:** Commit `feat(#48): register + dispatch compile_page/list_pages/page_history`.

### Task 3.5: compiled-aware `mem0_fetch`

**Files:** Modify `app/server.py` `handle_fetch_tool`; `tests/test_compiled_server.py`

- [ ] **Step 1: Test** `mem0_fetch(slug)` returns the page body + signed URL; an unknown id still returns `not_found`; a raw id still resolves via the existing path.
- [ ] **Step 3: Implement** — at the top of `handle_fetch_tool`, before the catalog/live lookups, add a compiled lookup:

```python
        if self.pages is not None:
            page = self.pages.get(record_id)
            if page is not None:
                body, blob_id = self.pages.read_body(record_id) or ("", page.current_blob)
                return self.mcp_tool_result(
                    text=body,
                    structured={"id": page.slug, "title": page.title, "text": body,
                                "url": self._signed_blob_url(blob_id), "kind": "synthesis",
                                "status": page.status, "derived_from": page.derived_from,
                                "metadata": {"kind": "synthesis", "slug": page.slug, "status": page.status}})
```

- [ ] **Step 4:** Run full suite `python -m pytest -q` → green.
- [ ] **Step 5:** Commit `feat(#48): resolve compiled pages through mem0_fetch`.

**Phase 3 acceptance:** an agent can file a synthesis (`mem0_compile_page`), it is stored as a versioned revision, indexed into the compiled collection, listed, fetched with a working URL, and its history inspected.

---

## Phase 4 — Synthesis-first retrieval (#50) — roadmap

**Files:** `app/server.py` `search_memories` (~line 2592) + `handle_search_tool`; `tests/test_compiled_server.py`.

- **Task 4.1** — add a compiled pre-pass: query `self.compiled_memory.search` (user-scoped) when `self.compiled_memory` is not None; skip entirely when None (→ today's behavior, the empty-layer no-op).
- **Task 4.2** — merge policy: if a compiled hit is `status=current` and clears a confidence bar (`score >= COMPILED_LEAD_THRESHOLD`, a module constant; start at e.g. `0.0`/lead-if-present and tune), lead with it and attach the raw hits as `evidence`; else run raw only.
- **Task 4.3** — gap signal: when no current synthesis leads, record a lightweight coverage-gap marker the `needs-review` resource can read (derive from registry in Phase 5 rather than persisting state — keep stateless).
- **Tests:** current synthesis leads with raw evidence; `status=stale` synthesis does NOT lead (falls back to raw); empty compiled layer ⇒ identical to pre-change results (regression).
- **Acceptance:** queries consult compiled first; a current synthesis leads with raw as evidence; stale/missing falls back to raw.

---

## Phase 5 — Schema + wiki indexes (#49, #52) — roadmap

**Files:** `app/server.py` `mcp_resources` (~886) + `read_resource` (~903); a bundled default constitution string; `tests/test_compiled_server.py`.

- **Task 5.1 (#49)** — `mem0://schema`: list it in `mcp_resources`; in `read_resource`, return the file at `settings.schema_path` if set else a built-in default markdown constitution (taxonomy, kinds, slug conventions, compile-vs-raw). `text/markdown`.
- **Task 5.2 (#52)** — `mem0://domain/<d>/index`: domain overview = `self.pages.list(domain=d)` summaries + recent. Add alongside the existing `mem0://domain/<d>` (do not break it).
- **Task 5.3 (#52)** — `mem0://recent`: recent pages (registry, by `updated_at`) + recent raw (catalog if present).
- **Task 5.4 (#52)** — `mem0://needs-review`: `self.pages.list(status="stale")` + coverage gaps from `health.py` (Phase 6).
- **Tests:** each resource lists + reads with the expected JSON/markdown shape; resources absent when the compiled layer is disabled.
- **Acceptance:** schema + `domain/*/index` + `recent` + `needs-review` are MCP resources generated on read.

---

## Phase 6 — Triggers + lint (#53, #43) — roadmap

**Files:** Create `app/health.py`, `app/lint.py`, `tests/test_health.py`; modify `app/server.py` `handle_add_memory_tool`, `app/ingest.py`.

- **Task 6.1 (#43)** — `app/health.py` pure functions over a registry snapshot + a catalog/records iterable: `stale_pages(registry)`, `coverage_gaps(value_counts, pages, min_count)`, `orphans(records)`, `stale_by_source(registry, source_updated_at)`, `supersession(registry)`. Each returns a list of proposal dicts `{kind, target, reason}`. Full TDD with synthetic fixtures (no Mem0).
- **Task 6.2 (#53)** — live fan-out: in `handle_add_memory_tool`, after a successful add, call a new `self._fan_out_staleness(new_ids, metadata)` that uses `self.pages.pages_deriving_from(ids=new_ids, domain=…, topic=…)` and `set_status(slug, "stale")`. Wrap in `try/except`; never fail the write. No-op when `self.pages is None`.
- **Task 6.3 (#53)** — bulk fan-out: in `app/ingest.py`, accumulate `(ids, domain, topic)` during the run and call one fan-out pass at the end (avoid O(records×pages)). Guarded + non-fatal.
- **Task 6.4 (#43/#53)** — `app/lint.py`: `python -m app.lint [--json] [--strict]` builds a registry + catalog from config/settings, runs `health.py`, prints proposals; exit 0 always unless `--strict` and issues exist. No internal scheduler.
- **Tests:** `test_health.py` covers each check; a server test asserts adding a raw memory that shares `derived_from`/taxonomy flags a dependent page `stale`; a lint test asserts proposals + exit codes.
- **Acceptance:** new raw memory flags dependent syntheses `stale`; on-demand refresh = re-`compile_page`; cron/CLI lint proposes (never applies); no daemon.

---

## Phase 7 — Gate the live lexical fallback (#30) — roadmap

**Files:** `app/server.py` `search_memories` (~2628); `tests/test_compiled_server.py` or `tests/test_tools.py`.

- **Task 7.1** — add `_should_run_lexical(query, hits, limit)`: True iff `len(hits) < limit` OR top score `< LEXICAL_FALLBACK_MIN_SCORE` (module constant) OR `query` contains an exact-identifier token (regex `[A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,}` or an ALL-CAPS+digits run). Gate the `live_lexical_matches` call on it.
- **Tests:** exact-token recall (the #28/#29 case) still returns the live hit (regression stays green); a healthy multi-hit vector query does NOT invoke `live_lexical_matches` (assert via a spy/monkeypatch counting calls) — the new acceptance test.
- **Acceptance:** exact-token recall preserved; healthy searches skip the 5000-record `get_all` scroll; skip-path test added.

---

## Phase 8 — Vault export (#51) — roadmap

**Files:** Create `app/export_vault.py` (or add `--export-vault` to a CLI); `tests/test_compiled.py` (export is pure registry+fs).

- **Task 8.1** — `export_vault(registry, out_dir)`: for each page, write `out_dir/<domain or _>/<slug>.md` from `registry.read_body(slug)`. One-way.
- **Task 8.2** — thin CLI wrapper `python -m app.export_vault --compiled-dir … --out …`.
- **Tests:** export writes one markdown file per page under the domain folder; idempotent re-run overwrites.
- **Acceptance:** vault export produces a browsable Obsidian folder; one-way.

---

## Final integration

- [ ] `python -m py_compile app/*.py`
- [ ] `python -m pytest -q` (full suite green)
- [ ] Update `README.md` MCP tools table with `mem0_compile_page` / `mem0_list_pages` / `mem0_page_history` and the new resources.
- [ ] Open one PR for the branch `epic/47-memory-compilation`; let CodeRabbit review; merge when green.

---

## Self-review (against spec)

**Spec coverage:** #51 storage → Phase 1 + 2; #48 file-back → Phase 3; #50 retrieval inversion → Phase 4; #49 schema + #52 indexes → Phase 5; #53 triggers + #43 lint → Phase 6; #30 gating → Phase 7; vault export → Phase 8. LLM-free synthesis boundary honored (agent supplies `markdown`; Reliquary never generates). Separate-collection isolation via `compiled_memory` (not a filter). Direct-to-compiled (no MVP migration path). On-by-default with empty-collection disable. All spec sections map to a phase.

**Type consistency:** `PageInfo`/`PageRegistry` method names (`put_revision`, `get`, `read_body`, `list`, `history`, `set_status`, `set_memory_id`, `pages_deriving_from`) are used identically in Phases 3–8. `slugify` imported from `compiled`. `mcp_tool_result(*, text, structured, is_error)` matches the real keyword-only signature. `compiled_memory`/`pages` attribute names consistent throughout.

**Placeholder scan:** Phases 1–3 contain complete code in every step. Phases 4–8 are explicitly roadmap-level (real signatures, test names, acceptance) to be expanded at execution time against the settled Phase 1–3 APIs — not vague TODOs.
