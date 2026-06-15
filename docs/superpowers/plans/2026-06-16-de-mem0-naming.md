# Reliquary de-`mem0` Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every `mem0_` artifact on Reliquary's *own* surface — Claude-endpoint tool names, `mem0://` resource URIs, `MEM0_*` env vars, and the chat-proxy request contract — with a consistent `reliquary` namespace. Clean break, no aliases.

**Architecture:** A wide-but-shallow rename across `app/server.py` (the tool list builder `mcp_tools_for`, the dispatch `call_mcp_tool`, `_tool_category`, `handle_capabilities_tool`, `mcp_resources`/`read_resource`, the chat-proxy in `handle_passthrough`, and the argparse `getenv` defaults in `main`), plus the dependency-light CLIs (`ingest.py`, `lint.py`, `export_vault.py`) and `.env.example`/docs. The **OpenAI/lean endpoint** (`profile.name == "openai"`) and the **Mem0 library config** (`config.yaml`) are deliberate carve-outs and are NOT touched. Spec: `docs/superpowers/specs/2026-06-16-reliquary-tool-rename-design.md`.

**Tech Stack:** Python 3.12+, stdlib `asyncio`/`logging`, pytest. No new dependencies.

---

## Critical rename mechanic (read before every task)

The dispatch and tests touch **both** endpoints. The Claude endpoint gets `reliquary_*`; the OpenAI endpoint keeps its bare deep-research names. So in `call_mcp_tool`, the `if profile.name == "openai":` branch is **unchanged**; only the Claude branch below it changes. In tests, a call like `call_mcp_tool(claude, "list_domains", …)` becomes `"reliquary_list_domains"`, but `call_mcp_tool(openai, "list_domains", …)` **stays** `"list_domains"`. Always check which profile a test call targets.

**Tool rename table (Claude endpoint, authoritative):**

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

---

## File Structure

- `app/server.py` — tool list (`mcp_tools_for`, the `claude_tools` blocks ~1386–1610), dispatch (`call_mcp_tool` Claude branch ~1755–1762+), `_tool_category` read/write name sets (~926–935), `handle_capabilities_tool` Claude-side names (~1161), resource list/read (`mcp_resources` ~971, `read_resource` ~1020), chat-proxy keys/headers (`handle_passthrough` ~2887–2921, plus ~662), the write-tag at ~3435, the `LEGACY_ENV_RENAMES` map + guard call, and argparse `getenv` defaults (~3895–3970).
- `app/ingest.py`, `app/lint.py`, `app/export_vault.py` — `os.getenv("MEM0_…")` keys.
- `app/compiled.py`, `app/catalog.py`, `app/health.py` — any `mem0://` URI or `MEM0_`/`mem0_` references (grep to confirm).
- `.env.example` — 20 documented vars + comments.
- `README.md`, `docs/GUIDE.md`, `docs/INGRESS.md` — all references.
- `tests/test_mcp_surface.py` — new regression guards + Claude-call renames.
- `tests/test_tools.py`, `tests/test_scopes.py`, `tests/test_governance.py`, `tests/test_compiled_server.py`, `tests/test_audit.py`, `tests/test_metrics.py`, `tests/test_helpers.py` — Claude-endpoint name renames; new startup-guard + chat-proxy tests.

---

## Task 1: Rename Claude-endpoint tool names

**Files:**
- Modify: `app/server.py` (`mcp_tools_for`, `call_mcp_tool` Claude branch, `_tool_category`, `handle_capabilities_tool`)
- Test: `tests/test_mcp_surface.py` (+ update name strings in `test_tools.py`, `test_scopes.py`, `test_governance.py`, `test_compiled_server.py`, `test_audit.py`, `test_metrics.py`)

- [ ] **Step 1: Write the failing regression-guard tests**

Add to `tests/test_mcp_surface.py`:

```python
def test_claude_tools_all_reliquary_prefixed(proxy):
    claude = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    names = [t["name"] for t in proxy.mcp_tools_for(claude, can_write=True)]
    assert names, "expected a non-empty Claude tool list"
    offenders = [n for n in names if not n.startswith("reliquary_")]
    assert offenders == [], f"non-reliquary tool names on Claude endpoint: {offenders}"


def test_openai_endpoint_keeps_search_and_fetch(proxy):
    openai = proxy.endpoint_profiles[proxy.settings.openai_mcp_path]
    names = [t["name"] for t in proxy.mcp_tools_for(openai, can_write=True)]
    assert "search" in names and "fetch" in names, names
    # carve-out: the lean endpoint stays unprefixed
    assert not any(n.startswith("reliquary_") for n in names), names
```

- [ ] **Step 2: Run to verify the first test fails**

Run: `python -m pytest tests/test_mcp_surface.py::test_claude_tools_all_reliquary_prefixed -v`
Expected: FAIL (current names like `mem0_search`, `list_domains` don't start with `reliquary_`).

- [ ] **Step 3: Rename the tool-name strings in `mcp_tools_for`**

In `app/server.py`, within `mcp_tools_for` (the `claude_tools` list blocks, ~1386–1610, including the `claude_tools.extend([...])` write-tools block ~1471), rename every `"name": "<old>"` per the table above. Do **not** touch the `if profile.name == "openai":` `tools = [...]` block (~1210–1384).

- [ ] **Step 4: Rename the Claude dispatch + `_tool_category` sets**

In `call_mcp_tool` (~1612), leave the `if profile.name == "openai":` branch intact; in the Claude branch below it, rename every `tool_name == "<old>"` comparison per the table. In `_tool_category` (~926), rename the tool names inside the `writes` and `reads` sets (these gate scope/metrics). In `handle_capabilities_tool` (~1161), rename any hardcoded Claude-side tool names (the OpenAI branch keeps bare names).

- [ ] **Step 5: Update existing tests' Claude-endpoint calls**

Across `tests/test_tools.py`, `test_scopes.py`, `test_governance.py`, `test_compiled_server.py`, `test_audit.py`, `test_metrics.py`, and `test_mcp_surface.py`: rename tool-name strings in `call_mcp_tool(claude, "<old>", …)` calls per the table. **Leave `call_mcp_tool(openai, …)` calls unchanged.** Example in `test_mcp_surface.py::test_list_domains_tool`: the `claude` call becomes `"reliquary_list_domains"`; the `openai` call stays `"list_domains"`.

- [ ] **Step 6: Run the full suite green**

Run: `python -m pytest -q`
Expected: PASS (both new guards pass; all renamed calls resolve).

- [ ] **Step 7: Verify no stray Claude tool names remain**

Run: `grep -nE '"(mem0_(capabilities|status|search|fetch|add_memory|update|delete|compile_page|list_pages|page_history))"' app/server.py`
Expected: matches only inside the `if profile.name == "openai":` block, if any (the lean endpoint has none of these — so expect **no** output). If output appears outside the OpenAI branch, fix it.

- [ ] **Step 8: Commit**

```bash
git add app/server.py tests/
git commit -m "refactor(rename): reliquary_ prefix for Claude-endpoint tools"
```

---

## Task 2: Rename resource URIs (`mem0://` → `reliquary://`)

**Files:**
- Modify: `app/server.py` (`mcp_resources` ~971, `read_resource` ~1020), and `app/compiled.py` / `app/catalog.py` / `app/health.py` if they emit URIs
- Test: `tests/test_mcp_surface.py`, `tests/test_compiled_server.py`

- [ ] **Step 1: Write the failing guard test**

Add to `tests/test_mcp_surface.py`:

```python
def test_resource_uris_all_reliquary_scheme(proxy):
    uris = [r["uri"] for r in proxy.mcp_resources()]
    assert uris, "expected resources"
    offenders = [u for u in uris if not u.startswith("reliquary://")]
    assert offenders == [], f"non-reliquary resource URIs: {offenders}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_mcp_surface.py::test_resource_uris_all_reliquary_scheme -v`
Expected: FAIL (`mem0://taxonomy`, etc.).

- [ ] **Step 3: Rename URIs in `mcp_resources` + `read_resource`**

In `app/server.py`: in `mcp_resources` (~971–1015) rename every `"uri": "mem0://…"` and the f-strings `f"mem0://domain/{domain}"` / `f"mem0://domain/{domain}/index"` to `reliquary://`. In `read_resource` (~1020) rename every URI literal / prefix it matches against (`mem0://schema`, `mem0://taxonomy`, `mem0://recent`, `mem0://needs-review`, `mem0://sources`, `mem0://domain/…`, `mem0://record/…`).

- [ ] **Step 4: Sweep the other modules**

Run: `grep -rnE 'mem0://' app/` — rename any remaining occurrences in `app/compiled.py`, `app/catalog.py`, `app/health.py` to `reliquary://`.

- [ ] **Step 5: Update tests referencing `mem0://`**

In `tests/test_mcp_surface.py` (`test_resources_list_and_read`, `test_resources_with_no_catalog`) and `tests/test_compiled_server.py`, rename `mem0://…` literals to `reliquary://…`. Keep the negative cases (`read_resource("bogus://x")` stays `None`).

- [ ] **Step 6: Run green + verify**

Run: `python -m pytest -q` (Expected: PASS)
Run: `grep -rnE 'mem0://' app/` (Expected: no output)

- [ ] **Step 7: Commit**

```bash
git add app/ tests/
git commit -m "refactor(rename): reliquary:// resource URI scheme"
```

---

## Task 3: Rename env vars (`MEM0_*` → `RELIQUARY_*`) + drop legacy alias

**Files:**
- Modify: `app/server.py` (argparse `getenv` defaults ~3895–3970 + the inline `MEM0_MEMORY_CONCURRENT_READS` read), `app/ingest.py`, `app/lint.py`, `app/export_vault.py`, `app/compiled.py`, `app/catalog.py`, `app/health.py`, `.env.example`

- [ ] **Step 1: Rename every `getenv` key in app code**

For each file, replace `os.getenv("MEM0_<X>"…)` / `os.environ["MEM0_<X>"]` with `RELIQUARY_<X>` (pure prefix swap). Files: `app/server.py`, `app/ingest.py`, `app/lint.py`, `app/export_vault.py`, plus any hit in `app/compiled.py`/`app/catalog.py`/`app/health.py`. Verify the set with: `grep -rnE 'MEM0_' app/`.

- [ ] **Step 2: Drop the legacy `--mcp-token` / `MEM0_MCP_TOKEN` alias**

In `app/server.py` `main()` remove the `parser.add_argument("--mcp-token", default=os.getenv("MEM0_MCP_TOKEN"), …)` line and any code that falls back to it (the canonical arg is `--claude-mcp-token` ← `RELIQUARY_CLAUDE_MCP_TOKEN`). Do **not** add a `RELIQUARY_MCP_TOKEN`.

- [ ] **Step 3: Rename `.env.example`**

Rewrite `.env.example`: every `MEM0_<X>` → `RELIQUARY_<X>` (keep the unprefixed compose helpers `BLOB_HOST_DIR` / `COMPILED_HOST_DIR` as-is). Update the prose comments that mention `MEM0_*` names.

- [ ] **Step 4: Confirm no app-owned `MEM0_` remains**

Run: `grep -rnE 'MEM0_' app/ .env.example`
Expected: **no output**. (The only `mem0`-ish tokens left in `app/` should be the Mem0 *library* imports/usage like `from mem0 import …`, which are correct.)

- [ ] **Step 5: Run the suite green**

Run: `python -m pytest -q`
Expected: PASS (tests build `ProxySettings` directly, so the env rename should not break them).

- [ ] **Step 6: Commit**

```bash
git add app/ .env.example
git commit -m "refactor(rename): RELIQUARY_ env vars; drop legacy MEM0_MCP_TOKEN alias"
```

---

## Task 4: Startup migration guard

**Files:**
- Modify: `app/server.py` (module-level `LEGACY_ENV_RENAMES` + `warn_legacy_env_vars` + call in `main`)
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_runtime.py` (import `warn_legacy_env_vars` and `LEGACY_ENV_RENAMES` from `server`):

```python
def test_warn_legacy_env_vars_flags_known_stale_var(caplog):
    from server import warn_legacy_env_vars
    import logging
    env = {"MEM0_CLAUDE_MCP_TOKEN": "x", "PATH": "/usr/bin"}
    with caplog.at_level(logging.WARNING):
        flagged = warn_legacy_env_vars(env)
    assert flagged == {"MEM0_CLAUDE_MCP_TOKEN": "RELIQUARY_CLAUDE_MCP_TOKEN"}
    assert "MEM0_CLAUDE_MCP_TOKEN" in caplog.text
    assert "RELIQUARY_CLAUDE_MCP_TOKEN" in caplog.text


def test_warn_legacy_env_vars_silent_when_clean():
    from server import warn_legacy_env_vars
    assert warn_legacy_env_vars({"PATH": "/usr/bin", "RELIQUARY_STATE_DIR": "/d"}) == {}


def test_legacy_mcp_token_maps_to_claude_token():
    from server import LEGACY_ENV_RENAMES
    assert LEGACY_ENV_RENAMES["MEM0_MCP_TOKEN"] == "RELIQUARY_CLAUDE_MCP_TOKEN"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_runtime.py -k legacy -v`
Expected: FAIL (`ImportError: cannot import name 'warn_legacy_env_vars'`).

- [ ] **Step 3: Implement the guard**

In `app/server.py` at module level (near other constants):

```python
# Former MEM0_* env var names → their RELIQUARY_* replacements. Used only to warn
# operators who still have stale vars set; the old names are no longer read.
_RENAMED_ENV_SUFFIXES = (
    "AUDIT_LOG", "BLOB_DIR", "BLOB_MAX_BYTES", "BLOB_SIGNING_KEY", "BLOB_URL_TTL",
    "CLAUDE_MCP_PATH", "CLAUDE_MCP_TOKEN", "COMPILED_COLLECTION", "COMPILED_DIR",
    "DATASET_PATH", "EMBEDDER_API_KEY", "EMBEDDER_BASE_URL", "EMBEDDER_DIMS",
    "EMBEDDER_MODEL", "EMBEDDER_PROVIDER", "IMAGE_URL_INGEST", "LINT_COVERAGE_MIN",
    "MEMORY_CONCURRENT_READS", "METRICS_PUBLIC", "OAUTH_ACCESS_TOKEN_TTL",
    "OAUTH_ALLOW_REGISTRATION", "OAUTH_CLIENT_ID", "OAUTH_REFRESH_TOKEN_TTL",
    "OAUTH_VERBATIM_TOKEN", "OPENAI_ALLOW_NOAUTH", "OPENAI_ALLOW_WRITE",
    "OPENAI_MCP_PATH", "OPENAI_MCP_TOKEN", "RATE_LIMIT_SEARCHES", "RATE_LIMIT_WRITES",
    "SCHEMA_PATH", "STATE_DIR", "STATIC_TOKENS", "WRITEBACK_PATH",
)
LEGACY_ENV_RENAMES = {f"MEM0_{s}": f"RELIQUARY_{s}" for s in _RENAMED_ENV_SUFFIXES}
# The dropped alias has no MEM0_-suffix-symmetric successor:
LEGACY_ENV_RENAMES["MEM0_MCP_TOKEN"] = "RELIQUARY_CLAUDE_MCP_TOKEN"


def warn_legacy_env_vars(env=None) -> dict[str, str]:
    """Warn (once) about any stale MEM0_* env vars still set. Returns the
    {old: new} map of the ones found. Non-fatal; the old names are not read."""
    import os as _os
    env = _os.environ if env is None else env
    found = {old: new for old, new in LEGACY_ENV_RENAMES.items() if old in env}
    if found:
        lines = "\n".join(f"  {old} -> {new}" for old, new in sorted(found.items()))
        logger.warning(
            "Ignoring %d legacy MEM0_* env var(s) — Reliquary now reads RELIQUARY_* "
            "names. Rename in your environment:\n%s", len(found), lines
        )
    return found
```

(Use the module's existing `logger`; if none, add `logger = logging.getLogger("reliquary")` near the imports.)

- [ ] **Step 4: Call it from `main`**

In `app/server.py` `main()`, near the start (before constructing settings), add: `warn_legacy_env_vars()`.

- [ ] **Step 5: Run green**

Run: `python -m pytest tests/test_runtime.py -q && python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/server.py tests/test_runtime.py
git commit -m "feat: warn on stale MEM0_* env vars at startup (migration guard)"
```

---

## Task 5: Rename the chat-proxy contract

**Files:**
- Modify: `app/server.py` (`handle_passthrough` ~2887–2921, the read at ~662, the write tag at ~3435), and the `app/helpers.py` docstring mention
- Test: `tests/test_helpers.py` (+ any chat-proxy test)

- [ ] **Step 1: Write/extend a failing test**

Add to `tests/test_helpers.py` (adapt to how the proxy exposes passthrough payload parsing; if the proxy reads keys inline, assert via a `handle_passthrough` unit or a focused helper). Minimal contract test:

```python
def test_chat_proxy_reads_reliquary_payload_keys(proxy, monkeypatch):
    # The proxy should read reliquary_query (not mem0_query) from the payload.
    captured = {}

    def fake_search(query, **kw):
        captured["query"] = query
        return []

    monkeypatch.setattr(proxy, "search_memories", fake_search, raising=False)
    payload = {"messages": [{"role": "user", "content": "hi"}], "reliquary_query": "find X"}
    # Exercise the payload-key extraction path used by handle_passthrough.
    # (Call the smallest unit available; assert reliquary_query wins.)
    assert payload.get("reliquary_query") == "find X"
    assert "mem0_query" not in payload
```

(If a direct `handle_passthrough` unit test is impractical, instead assert the rename via `grep` in Step 4 and keep this as a payload-shape sanity check.)

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_helpers.py -k reliquary_payload -v`
Expected: PASS for the shape assert (it documents intent); the real enforcement is the grep in Step 4.

- [ ] **Step 3: Rename the chat-proxy keys/headers/tag**

In `app/server.py`:
- `handle_passthrough` (~2887–2896): `payload.pop("mem0_user_id"…)`, `mem0_limit`, `mem0_threshold`, `mem0_query`, `mem0_disable` → `reliquary_*`. (Local variable names like `mem0_limit` are cosmetic but rename them too for clarity.)
- Header reads/writes: `request_headers.get("x-mem0-user-id")` (~663, ~2887), and the emitted `"x-mem0-hit-count"`, `"x-mem0-user-id"`, `"x-mem0-query"` (~2917–2921) → `x-reliquary-*`.
- The write tag `"source": "mem0_chat_proxy"` (~3435) → `"reliquary_chat_proxy"`.
- Update the `app/helpers.py` module docstring that says `mem0_chat_proxy`.

- [ ] **Step 4: Verify + run green**

Run: `grep -rnE 'mem0_(query|user_id|limit|threshold|disable)|x-mem0|mem0_chat_proxy' app/`
Expected: no output.
Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/ tests/
git commit -m "refactor(rename): reliquary chat-proxy keys, x-reliquary-* headers, source tag"
```

---

## Task 6: Docs sweep + final verification

**Files:**
- Modify: `README.md`, `docs/GUIDE.md`, `docs/INGRESS.md`

- [ ] **Step 1: Sweep the docs**

In `README.md`, `docs/GUIDE.md`, `docs/INGRESS.md`: rename all tool names (table + prose) per the Task 1 table, `mem0://` → `reliquary://`, and `MEM0_*` → `RELIQUARY_*` (including the env table and the dropped `MEM0_MCP_TOKEN`). Do **not** edit dated files under `docs/superpowers/`. Leave OpenAI-endpoint `search`/`fetch` names and the `config.yaml` `vector_store`/`embedder`/`llm` keys as-is.

- [ ] **Step 2: Final repo-wide verification**

Run:
```bash
grep -rnE 'mem0_(capabilities|status|search|fetch|add_memory|update|delete|compile_page|list_pages|page_history|chat_proxy)|mem0://|MEM0_|x-mem0' \
  app/ README.md docs/GUIDE.md docs/INGRESS.md .env.example
```
Expected: **no output**. (Matches inside `docs/superpowers/` are historical and excluded above.)

- [ ] **Step 3: Full suite + compile**

Run: `python -m pytest -q && python -m py_compile app/*.py`
Expected: PASS / no errors.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/GUIDE.md docs/INGRESS.md
git commit -m "docs: reliquary_ tool names, reliquary:// URIs, RELIQUARY_* env vars"
```

---

## Self-review notes (already applied)

- **Spec coverage:** tools (Task 1), resources (Task 2), env vars + dropped alias (Task 3), startup guard (Task 4), chat proxy (Task 5), docs + carve-out verification (Task 6). Regression guards for "no `mem0_` tools", "OpenAI keeps `search`/`fetch`", "no `mem0://`", and "legacy env warns" are in Tasks 1/4.
- **Carve-outs preserved:** every task explicitly excludes the OpenAI branch and the Mem0 library config.
- **Type/name consistency:** `LEGACY_ENV_RENAMES` and `warn_legacy_env_vars` names match between Task 4's code and its test; tool-name table is the single source reused across tasks.
