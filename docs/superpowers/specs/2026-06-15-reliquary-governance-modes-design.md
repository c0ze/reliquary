# Reliquary contextual governance modes — design

**Date:** 2026-06-15
**Component:** `reliquary/` (the MCP memory server)
**Epic:** #40 — contextual governance modes. Slices #41, #42, #44, #45, #46.
(#43 memory-lint already shipped in the #47 compilation epic.)
**Goal:** Add **optional, soft** governance affordances that activate only when a
caller supplies project/repo context — without changing Reliquary's domain-neutral
identity or any generic (non-repo) behavior.

## Thesis

A comparison with [Scrinium](https://github.com/ozgurcd/scrinium) (a governed,
repo-local markdown wiki for coding agents) surfaced strong governance ideas. The
takeaway is **not** "wiki as storage" but "agents need rails around shared memory."
Scrinium's *identity* is project governance; Reliquary's is generic, domain-neutral
memory. So Reliquary adopts governance as an **optional context adapter**, never as
its identity: when the caller is operating inside a repo, expose project-governance
affordances; everywhere else (web agents, non-repo CLI, home-dir sessions), act like
plain personal memory.

## Decisions locked during brainstorming

- **Context model — caller-declared + header fallback.** Context comes from a nested
  `context` object in the tool args; if absent, an optional HTTP header
  (`X-Reliquary-Repo` / `X-Reliquary-Git-Root` / `X-Reliquary-Client`) is read.
  Absent entirely → generic behavior, byte-identical to today.
- **Soft by default.** No `begin_session`, no hard write-gating. The routing bias is
  additive (a score bonus) and excludes nothing.
- **Additive.** The domain-neutral core (`search_memories`, add/update/delete,
  taxonomy routing) is untouched; governance rides on context-presence and new,
  opt-in tools. When no context is supplied and the new tools aren't called,
  behavior is unchanged.
- **One branch / one PR** for the epic (#41/#42/#44/#45/#46).
- **Approach A — thin context-adapter** (chosen over a stateful "mode" object, which
  would bake governance into identity, and over deferring the #42 routing centerpiece).

## Architecture

When no caller context is supplied and the new tools aren't called, behavior is
byte-identical to today. New modules follow the dependency-light `compiled.py` /
`health.py` pattern.

| # | Component | Responsibility | Slice |
|---|-----------|----------------|-------|
| 1 | `app/context.py` *(new)* | `CallerContext` + `resolve_context(arguments, headers)` → normalized context or `None` | #42 |
| 2 | context threading + soft bias | `handle_mcp` resolves context once, threads it to `handle_search_tool`; a score bonus lifts project-matching hits | #42 |
| 3 | `mem0_capabilities` / `capabilities` | concise agent orientation tool, both endpoints | #41 |
| 4 | `propose_update` | correction workflow for protected imports (user-write `kind=correction`) | #44 |
| 5 | `mem0://sources` resource | provenance registry generated from the loaded catalog | #46 |
| 6 | systematic `suggested_action` | concrete next-action on protected/scope error results | #45 |

**Boundary:** governance is a context adapter, not a mode baked into identity. No
filesystem access from caller context; no session state.

## Components & data flows

### `app/context.py` (dependency-light: stdlib only; no Mem0/server imports)

```
@dataclass
class CallerContext:
    client: str | None
    cwd: str | None
    git_root: str | None
    repo: str | None
    repo_slug: str | None        # derived, for routing

def resolve_context(arguments: dict, headers: dict) -> CallerContext | None
```

- **Args first:** a nested `context` object in the tool args
  (`{"query": …, "context": {"client","cwd","git_root","repo"}}`). Nested keeps the
  arg namespace clean under `additionalProperties: False`.
- **Header fallback:** when args carry no context, read `X-Reliquary-Repo` /
  `X-Reliquary-Git-Root` / `X-Reliquary-Client`.
- **Normalize:** `repo_slug = slugify(repo.split("/")[-1])` else
  `slugify(basename(git_root))` (reuse `catalog.slugify_value`). Returns `None` when
  there is no repo/git_root signal.
- **Security:** `cwd`/`git_root` are opaque informational strings used only to derive
  a slug — Reliquary never touches the filesystem at those paths (the lesson from
  #47's `PageRegistry.get`). `resolve_context` never raises; malformed → `None`/partial.

### Context threading + soft routing bias (#42)

`handle_mcp` resolves `CallerContext` once from request headers + the call's
`arguments` and passes it through `call_mcp_tool(..., context=ctx)` →
`handle_search_tool(..., context=ctx)` (and to `capabilities`, to report "project
context active"). Other handlers ignore it.

The bias rides in the **raw-results sort key** (no score mutation, no-op when context
is absent, so synthesis-first ordering from #47 and displayed scores are preserved):

```python
raw_results = sorted(
    by_key.values(),
    key=lambda item: (-(self._numeric_score(item.get("score")) + self._context_bonus(item, context)),
                      str(item.get("id") or "")),
)
# _context_bonus(item, context) -> CONTEXT_MATCH_BONUS (module constant, e.g. 0.1)
# when context and context.repo_slug and the hit metadata domain=="dev" or
# room==repo_slug; else 0.0
```

Project memory floats up; nothing is filtered (soft).

### `mem0_capabilities` / `capabilities` tool (#41) — read, both endpoints

`handle_capabilities_tool(profile, context=None)` builds a concise orientation dict
from current settings + catalog: what Reliquary is (domain-neutral semantic memory
over Mem0/Qdrant); tools per endpoint; read/write + scope rules; imported-record
protection (imports read-only, user-write mutable); image/blob support; taxonomy
(`domain/hall/room/topic` + routeable domains); when-to-write guidance; whether the
compiled layer + project context are active. Orientation, not a data dump. Named
`mem0_capabilities` on Claude, `capabilities` on the lean OpenAI endpoint.

### `propose_update` tool (#44) — write, gated like `add_memory`

Args: `target_id` (required), `reason`, `replacement_text`, `source`, `user_id`
(Claude only). Flow: gate (write) → validate `target_id` (`missing_target` if empty)
→ optionally resolve the target (catalog/live) to capture its title → store a
**user-write** memory: `text = replacement_text or reason or "Proposed correction
for {target_id}"`, metadata `{kind: correction, target_id, status: proposed, source,
source_group: user-write}` → return `{id, target_id, status: "proposed"}`. The import
stays immutable; the correction is a normal searchable/fetchable record carrying
`target_id`. Claude + OpenAI-iff-`openai_allow_write` (mirrors `add_memory`).

### `mem0://sources` resource (#46) — read-only

Generated on read from `catalog.records_by_id`: group records by `source_group`
(imported vs user-write) and a derived source key (corpus / `source_url` host /
`source_ref` prefix), with counts, private/public status, and sample `source_ref`s.
Manifest-independent (`mem0_import` is an optional input, not the authority). Empty /
minimal when no catalog is loaded. Listed in `mcp_resources` when a catalog exists.

### Systematic `suggested_action` (#45)

Error results gain a concrete next step (consistent `suggested_action` field):

| Error | `suggested_action` |
|-------|--------------------|
| `protected_record` (update/delete) | "Imported records are read-only — file a correction with `propose_update` (target_id=…)." |
| `insufficient_scope` | "This token/endpoint is read-only; reconnect with a write-scoped token." |
| image not owned (`delete_image`) | "Only the memory that owns this blob can delete it." |
| `user_id` not accepted (OpenAI lean) | "This endpoint does not accept a caller-supplied `user_id`." |

## Configuration

**No new env vars** — governance is soft and needs no configuration. Context header
names (`X-Reliquary-Repo`, `X-Reliquary-Git-Root`, `X-Reliquary-Client`) and
`CONTEXT_MATCH_BONUS` are fixed module constants. README + GUIDE document the new
tools/resource, the context headers, and the correction workflow.

## Error handling

| Condition | Result |
|-----------|--------|
| `propose_update` empty `target_id` | `missing_target`, is_error |
| write tool without write scope | `insufficient_scope` + `suggested_action` |
| update/delete on imported record | `protected_record` + `suggested_action` (→ `propose_update`) |
| malformed caller context | ignored (treated as no context); never fatal |

## Testing

Dependency-light first, mirroring `tests/test_compiled.py` / `tests/test_health.py`:

1. **`tests/test_context.py`** (no Mem0): nested `context` arg → `CallerContext` with
   derived `repo_slug`; header fallback when args absent; **args take precedence**;
   no repo/git_root → `None`; slug derivation (`c0ze/reliquary` → `reliquary`;
   `git_root` basename); malformed → `None`, never raises.
2. **Integration** (`proxy` + `FakeMemory`):
   - capabilities: `mem0_capabilities` (Claude) + `capabilities` (OpenAI) return the
     orientation dict with expected keys; reflects `openai_allow_write` + catalog.
   - propose_update: stores a `kind=correction` user-write record with
     `target_id`/`status=proposed`; `missing_target` on empty; write-gated; OpenAI
     only when write enabled.
   - context bias: a `domain=dev`/`room=<repo_slug>` record outranks an
     equally-scored non-project record *when context is supplied*; **no context →
     ordering unchanged** (regression).
   - `mem0://sources`: groups by `source_group` with counts; empty without a catalog.
   - #45: `protected_record` (update + delete) and `insufficient_scope` results carry
     the documented `suggested_action`.
3. `python -m py_compile app/*.py` + full suite green.

## Phasing (one branch, reviewable commits)

1. `app/context.py` + `tests/test_context.py` (standalone)
2. `mem0_capabilities` / `capabilities` (#41)
3. `propose_update` (#44) + `suggested_action` systematization (#45) — paired
4. context threading + soft routing bias (#42)
5. `mem0://sources` resource (#46)
6. docs (README/GUIDE) + final

## Out of scope (YAGNI)

Hard session enforcement / `begin_session` (the epic rejects it) · project tools
(`project_status`/`project_log`/`project_decision`/`project_lint`; deferred by #42) ·
auto-suggesting project-memory updates after changes · inferred context (we chose
caller-declared + header) · mutating/versioning imported records (corrections are
separate user-write records) · a correction *approval/merge* workflow (proposals
only; applying is manual/future) · per-repo memory isolation/namespacing (soft bias
only, no separate collection).
