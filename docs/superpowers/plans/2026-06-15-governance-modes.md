# Contextual Governance Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional, soft governance affordances (capabilities orientation, repo-context routing bias, a correction workflow for protected imports, actionable rejection messages, a provenance resource) that activate only when a caller supplies project context — leaving generic behavior unchanged.

**Architecture:** A dependency-light `app/context.py` resolves caller context (nested `context` arg + `X-Reliquary-*` header fallback) into a `CallerContext` or `None`. The server threads it into `handle_search_tool` for a soft, additive score bias; new opt-in tools/resources (`mem0_capabilities`, `propose_update`, `mem0://sources`) and systematic `suggested_action` errors hang off existing seams. When no context is supplied and the new tools aren't called, behavior is byte-identical to today.

**Tech Stack:** Python 3.12+, stdlib-only for `context.py` (mirrors `compiled.py`/`health.py`), Mem0 + Qdrant, pytest. ASGI server in `app/server.py`.

**Spec:** [`docs/superpowers/specs/2026-06-15-reliquary-governance-modes-design.md`](../specs/2026-06-15-reliquary-governance-modes-design.md)

---

## File Structure

**Create:**
- `app/context.py` — `CallerContext` + `resolve_context(arguments, headers)`.
- `tests/test_context.py` — dependency-light unit tests.
- `tests/test_governance.py` — integration tests (capabilities, propose_update, bias, sources, suggested_action).

**Modify:**
- `app/server.py` — capabilities tool + handler; `propose_update` tool + handler; `_scope_error` helper + `suggested_action` on protected/scope errors; context threading (`handle_mcp` → `call_mcp_tool` → `handle_search_tool`) + `_context_bonus`; `mem0://sources` in `mcp_resources`/`read_resource`; `_tool_category` for the new tools.
- `README.md`, `docs/GUIDE.md` — document tools/resource/headers/correction workflow.

**Detail level:** Phase 1 is full step-level TDD. Phases 2–6 give concrete per-task code, exact insertion anchors, and test assertions; expand any step to micro-steps at execution time.

---

## Phase 1 — `app/context.py` (#42 foundation)

### Task 1.1: `CallerContext` + `resolve_context`

**Files:** Create `app/context.py`; Test `tests/test_context.py`

- [ ] **Step 1: Write the failing test** (`tests/test_context.py`)

```python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from context import CallerContext, resolve_context  # noqa: E402


def test_args_context_yields_repo_slug():
    ctx = resolve_context({"context": {"repo": "c0ze/reliquary", "client": "codex"}}, {})
    assert ctx is not None
    assert ctx.repo == "c0ze/reliquary"
    assert ctx.repo_slug == "reliquary"
    assert ctx.client == "codex"


def test_git_root_basename_slug():
    ctx = resolve_context({"context": {"git_root": "/home/arda/projects/utils/"}}, {})
    assert ctx is not None and ctx.repo_slug == "utils"


def test_header_fallback_when_no_args_context():
    ctx = resolve_context({}, {"x-reliquary-repo": "c0ze/reliquary"})
    assert ctx is not None and ctx.repo_slug == "reliquary"


def test_args_take_precedence_over_headers():
    ctx = resolve_context({"context": {"repo": "owner/from-args"}}, {"x-reliquary-repo": "owner/from-header"})
    assert ctx.repo == "owner/from-args" and ctx.repo_slug == "from-args"


def test_no_signal_returns_none():
    assert resolve_context({}, {}) is None
    assert resolve_context({"context": {"client": "codex"}}, {}) is None  # client only, no repo


def test_malformed_context_is_ignored():
    assert resolve_context({"context": "not-a-dict"}, {}) is None
    assert resolve_context(None, None) is None
```

- [ ] **Step 2: Run, verify failure** — `python -m pytest tests/test_context.py -q` → `ModuleNotFoundError: No module named 'context'`.

- [ ] **Step 3: Implement `app/context.py`**

```python
"""Caller-context resolution for Reliquary's optional, soft governance layer.

Dependency-light: stdlib + catalog.slugify_value. No Mem0/server imports.

When a coding agent supplies project context (a nested ``context`` arg, or the
X-Reliquary-* headers), Reliquary can softly bias retrieval toward that repo's
memory. Absent context => generic behavior, unchanged. ``cwd``/``git_root`` are
opaque informational strings: Reliquary never touches the filesystem at those paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from catalog import slugify_value

_REPO_HEADER = "x-reliquary-repo"
_GIT_ROOT_HEADER = "x-reliquary-git-root"
_CWD_HEADER = "x-reliquary-cwd"
_CLIENT_HEADER = "x-reliquary-client"


@dataclass
class CallerContext:
    client: str | None = None
    cwd: str | None = None
    git_root: str | None = None
    repo: str | None = None
    repo_slug: str | None = None


def _derive_slug(repo: str | None, git_root: str | None) -> str | None:
    if repo:
        slug = slugify_value(repo.rstrip("/").split("/")[-1])
        if slug:
            return slug
    if git_root:
        slug = slugify_value(os.path.basename(git_root.rstrip("/")))
        if slug:
            return slug
    return None


def resolve_context(arguments: dict | None, headers: dict | None) -> "CallerContext | None":
    arguments = arguments if isinstance(arguments, dict) else {}
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    raw = arguments.get("context")
    if not isinstance(raw, dict):
        raw = {}

    def pick(key: str, header: str) -> str | None:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        hval = headers.get(header)
        if isinstance(hval, str) and hval.strip():
            return hval.strip()
        return None

    repo = pick("repo", _REPO_HEADER)
    git_root = pick("git_root", _GIT_ROOT_HEADER)
    repo_slug = _derive_slug(repo, git_root)
    if repo_slug is None:
        return None  # no usable project signal => generic behavior
    return CallerContext(
        client=pick("client", _CLIENT_HEADER),
        cwd=pick("cwd", _CWD_HEADER),
        git_root=git_root,
        repo=repo,
        repo_slug=repo_slug,
    )
```

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_context.py -q` (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/context.py tests/test_context.py
git commit -m "feat(#42): CallerContext + resolve_context (args + header fallback)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — capabilities tool (#41)

**Files:** Modify `app/server.py`; Test `tests/test_governance.py`

- **Task 2.1: handler.** Add to `Mem0ChatProxy`:

```python
    def handle_capabilities_tool(self, profile: "EndpointProfile", context=None) -> dict[str, Any]:
        is_openai = profile.name == "openai"
        payload = {
            "what": "Reliquary: domain-neutral semantic memory over Mem0 + Qdrant, served over MCP.",
            "endpoint": profile.name,
            "tools": [t["name"] for t in self.mcp_tools_for(profile, can_write=True)],
            "rules": {
                "imported_records": "read-only (protected); user-written records are mutable",
                "corrections": "propose changes to imported records with propose_update (never mutates the import)",
                "write_scope": "write tools require a write-scoped token" + (
                    "; this endpoint also requires MEM0_OPENAI_ALLOW_WRITE" if is_openai else ""),
                "user_id": "not accepted on the OpenAI endpoint" if is_openai else "optional override accepted",
            },
            "images": "store/fetch binary blobs with add_image / fetch_image (+ upload flow)",
            "taxonomy": {
                "fields": ["domain", "hall", "room", "topic"],
                "routeable_domains": self.catalog.routeable_domains if self.catalog else [],
            },
            "compiled_layer": self.pages is not None,
            "project_context": {
                "active": context is not None,
                "repo": getattr(context, "repo", None),
                "how": "pass a `context` object ({client,cwd,git_root,repo}) in tool args, or X-Reliquary-Repo header",
            },
            "when_to_write": "store durable facts/decisions the user will want recalled later; don't store transient chatter",
        }
        return self.mcp_tool_result(
            text="Reliquary capabilities (orientation). See structuredContent for details.",
            structured=payload,
        )
```

- **Task 2.2: register + dispatch.** In `mcp_tools_for`, add a read-tool def to BOTH lists (name `capabilities` in the `openai` list, `mem0_capabilities` in `claude_tools`):

```python
{
    "name": "mem0_capabilities",   # "capabilities" in the openai list
    "title": "Capabilities",
    "description": "Orient yourself: what Reliquary is, the tools available, read/write and protection rules, taxonomy, and how to supply project context. Call this first.",
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
},
```
In `call_mcp_tool`, dispatch in both branches (read, no gate): openai `if tool_name == "capabilities": return self.handle_capabilities_tool(profile, context=context)`; claude `if tool_name == "mem0_capabilities": return self.handle_capabilities_tool(profile, context=context)`. (`context` param added to `call_mcp_tool` in Phase 4; until then pass nothing — to keep phases independently green, add `context=None` to `call_mcp_tool`'s signature now.)

- **Task 2.3: `_tool_category`.** Add `"mem0_capabilities"` and `"capabilities"` to the `reads` set so rate-limiting/audit classify them correctly.

- **Tests** (`tests/test_governance.py`): `mem0_capabilities` on the Claude profile and `capabilities` on the OpenAI profile each return `isError` falsey + a structured dict containing `what`, `tools`, `rules`, `taxonomy`; the OpenAI payload's `rules.user_id` mentions "not accepted". Update any tool-list-count assertions in `tests/test_mcp_surface.py`.

- **Commit:** `feat(#41): mem0_capabilities / capabilities orientation tool`

---

## Phase 3 — `propose_update` (#44) + `suggested_action` (#45)

**Files:** Modify `app/server.py`; Test `tests/test_governance.py`

- **Task 3.1: `_scope_error` helper + systematize `insufficient_scope`.** Add:

```python
    def _scope_error(self, tool_name: str) -> dict[str, Any]:
        return self.mcp_tool_result(
            text=f"Tool {tool_name} requires write access (read-only token or endpoint).",
            structured={"error": "insufficient_scope",
                        "suggested_action": "Reconnect with a write-scoped token; this token or endpoint is read-only."},
            is_error=True,
        )
```
Replace every inline `insufficient_scope` result in `call_mcp_tool` (both endpoint branches) with `return self._scope_error(tool_name)`. (Mechanical; preserves behavior, adds `suggested_action`.)

- **Task 3.2: `suggested_action` on protected-record errors.** In `handle_delete_tool` (the `protected_record` result ~`server.py:2629`) and `handle_update_tool` (the `protected_record` result), add to the structured dict:
```python
"suggested_action": "Imported records are read-only — file a correction with propose_update (target_id=<id>).",
```
Also add to `handle_delete_image_tool`'s not-owned result: `"suggested_action": "Only the memory that owns this blob can delete it."`.

- **Task 3.3: `propose_update` handler.**

```python
    async def handle_propose_update_tool(self, arguments: dict[str, Any], *, allow_user_id: bool = False) -> dict[str, Any]:
        target_id = str(arguments.get("target_id") or "").strip()
        if not target_id:
            return self.mcp_tool_result(
                text="A non-empty `target_id` is required.",
                structured={"error": "missing_target",
                            "suggested_action": "Pass target_id = the id of the record you want to correct."},
                is_error=True)
        user_id = str(arguments.get("user_id") or self.settings.user_id) if allow_user_id else self.settings.user_id
        reason = str(arguments.get("reason") or "").strip()
        replacement = str(arguments.get("replacement_text") or "").strip()
        text = replacement or reason or f"Proposed correction for {target_id}"
        metadata: dict[str, Any] = {
            "kind": "correction", "target_id": target_id, "status": "proposed",
            "source": str(arguments.get("source") or "mcp"), "source_group": "user-write",
        }
        if reason:
            metadata["reason"] = reason
        result = await self.add_memory(text, user_id=user_id, metadata=metadata, infer=False)
        new_ids = added_memory_ids(result)
        return self.mcp_tool_result(
            text=f"Filed correction for {target_id} (status=proposed).",
            structured={"id": new_ids[0] if new_ids else None, "target_id": target_id, "status": "proposed"})
```

- **Task 3.4: register + dispatch `propose_update`** (write tool; Claude + OpenAI-iff-write, mirroring `mem0_add_memory`/`add_memory`). Tool def:
```python
{
    "name": "propose_update",
    "title": "Propose Correction",
    "description": "File a correction for a protected/imported record without mutating it. Stores a linked user-write record (kind=correction, status=proposed).",
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "inputSchema": {"type": "object", "properties": {
        "target_id": {"type": "string", "description": "Id of the record to correct."},
        "reason": {"type": "string"}, "replacement_text": {"type": "string"}, "source": {"type": "string"},
        "user_id": {"type": "string"},
    }, "required": ["target_id"], "additionalProperties": False},
},
```
(Drop `user_id` from the OpenAI variant's schema, matching the lean endpoint.) Dispatch (gated): `if not can_write: return self._scope_error(tool_name)` then `return await self.handle_propose_update_tool(arguments, allow_user_id=<True on claude / False on openai>)`. Add `"propose_update"` to `_tool_category` `writes`.

- **Tests** (`tests/test_governance.py`): propose_update stores a `kind=correction`/`status=proposed`/`target_id` user-write record (inspect `proxy.memory._store`); `missing_target` on empty; `insufficient_scope` (with `suggested_action`) when `can_write=False`; deleting an imported record returns `protected_record` + a `suggested_action` mentioning `propose_update` (seed an imported record in `fake_memory` with `source_group != "user-write"`).

- **Commit(s):** `feat(#45): actionable suggested_action on scope/protected errors` then `feat(#44): propose_update correction workflow`.

---

## Phase 4 — context threading + soft routing bias (#42)

**Files:** Modify `app/server.py`; Test `tests/test_governance.py`

- **Task 4.1: thread context.** Add `context=None` params to `call_mcp_tool` and `handle_search_tool`. In `handle_mcp`'s `tools/call` block (`server.py:888`), resolve and pass it:
```python
                from context import resolve_context
                ctx = resolve_context(tool_arguments, headers)
                result = await self.call_mcp_tool(profile, tool_name, tool_arguments, can_write=can_write, context=ctx)
```
In `call_mcp_tool`, pass `context` to `handle_search_tool(...)` (Claude `mem0_search` + OpenAI `search`) and to `handle_capabilities_tool(...)`.

- **Task 4.2: bias mechanics.** Add a module constant `CONTEXT_MATCH_BONUS = 0.1` (near `LIVE_LEXICAL_SCAN_LIMIT`) and a method:
```python
    def _context_bonus(self, hit: dict[str, Any], context) -> float:
        if context is None or not getattr(context, "repo_slug", None):
            return 0.0
        md = hit.get("metadata") or {}
        if md.get("domain") == "dev" or md.get("room") == context.repo_slug:
            return CONTEXT_MATCH_BONUS
        return 0.0
```
In `handle_search_tool`, fold the bonus into the existing `raw_results` sort key:
```python
        raw_results = sorted(
            by_key.values(),
            key=lambda item: (-(self._numeric_score(item.get("score")) + self._context_bonus(item, context)),
                              str(item.get("id") or "")),
        )
```

- **Task 4.3: schema.** Add an optional `context` object to the `mem0_search` (and OpenAI `search`) `inputSchema` properties:
```python
"context": {"type": "object", "description": "Optional caller context (client, cwd, git_root, repo) for project-aware routing."},
```

- **Tests** (`tests/test_governance.py`): seed two equally-scored records (one `metadata.domain=dev` or `room=<repo_slug>`, one not) in `fake_memory`; a `mem0_search` with `{"context": {"repo": "c0ze/reliquary"}}` ranks the project record first; the same search WITHOUT context preserves the original order (regression). Also a `resolve_context`-via-`handle_mcp` smoke test is optional (unit coverage in Phase 1 suffices).

- **Commit:** `feat(#42): caller-context threading + soft project routing bias`

---

## Phase 5 — `mem0://sources` provenance resource (#46)

**Files:** Modify `app/server.py`; Test `tests/test_governance.py`

- **Task 5.1: resource.** In `mcp_resources`, when `self.catalog` is present, append `{"uri": "mem0://sources", "name": "Source registry", "description": "Provenance of imported corpora and user writes.", "mimeType": "application/json"}`. In `read_resource`, handle it:
```python
        if uri == "mem0://sources" and self.catalog:
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            for rec in self.catalog.records_by_id.values():
                md = rec.metadata or {}
                group = str(md.get("source_group") or "imported")
                src = str(md.get("source") or md.get("source_url") or md.get("source_ref") or "(unknown)")
                key = (group, src)
                entry = groups.setdefault(key, {"source_group": group, "source": src, "count": 0,
                                                 "private": bool(md.get("private")), "sample_refs": []})
                entry["count"] += 1
                if md.get("source_ref") and len(entry["sample_refs"]) < 3:
                    entry["sample_refs"].append(str(md.get("source_ref")))
            payload = {"sources": sorted(groups.values(), key=lambda e: (-e["count"], e["source"])),
                       "total": len(self.catalog.records_by_id)}
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(payload)}]}
```

- **Tests** (`tests/test_governance.py`): with a stub catalog exposing `records_by_id` (records carrying `source_group`/`source_ref`), `read_resource("mem0://sources")` returns grouped sources with counts; `mcp_resources()` lists `mem0://sources` when a catalog is set and omits it otherwise.

- **Commit:** `feat(#46): mem0://sources provenance registry resource`

---

## Phase 6 — docs + final

- **Task 6.1: README.** Add `mem0_capabilities`, `propose_update` to the Claude tools row (and `capabilities` to the OpenAI row); add a short "Governance / project context" paragraph (context args + `X-Reliquary-*` headers, the correction workflow, `mem0://sources`).
- **Task 6.2: GUIDE.** Add a "Project context & governance" subsection.
- **Task 6.3: final.** `python -m py_compile app/*.py`; `python -m pytest -q` (full suite green); commit `docs(#40): document governance tools, context headers, correction workflow`.

---

## Self-review (against spec)

**Spec coverage:** #41 capabilities → Phase 2; #42 context model + routing bias → Phase 1 + Phase 4; #44 propose_update → Phase 3; #45 suggested_action → Phase 3; #46 sources → Phase 5; docs/acceptance → Phase 6. Soft-by-default honored (additive bonus, no gating). Caller-declared + header fallback implemented in `resolve_context`. Non-repo behavior unchanged (bonus is 0.0, new tools opt-in).

**Type consistency:** `CallerContext`/`resolve_context` signatures match across phases; `context` param added to `call_mcp_tool`/`handle_search_tool`/`handle_capabilities_tool` consistently; `_scope_error(tool_name)` and `_context_bonus(hit, context)` used as defined; `mcp_tool_result(*, text, structured, is_error)` matches the real signature.

**Placeholder scan:** Phase 1 has complete code; Phases 2–6 give concrete handler/tool/dispatch code + exact anchors and test assertions — no vague TODOs.
