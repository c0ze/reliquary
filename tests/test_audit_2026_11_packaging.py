"""Regression tests for the 2026-11 audit: startup env parsing, Docker and
compose files, CI workflows, the Mem0 2.x API contract, and HTTP auth gates
that no earlier test pinned."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import server  # noqa: E402
from ingest import existing_import_index, ingest_records  # noqa: E402

INT_ENV_VARS = {
    "RELIQUARY_OAUTH_ACCESS_TOKEN_TTL": ("oauth_access_token_ttl", 432000),
    "RELIQUARY_OAUTH_REFRESH_TOKEN_TTL": ("oauth_refresh_token_ttl", 0),
    "RELIQUARY_BLOB_MAX_BYTES": ("blob_max_bytes", 30 * 1024 * 1024),
    "RELIQUARY_BLOB_URL_TTL": ("blob_url_ttl", 3600),
    "RELIQUARY_RATE_LIMIT_WRITES": ("rate_limit_writes", 0),
    "RELIQUARY_RATE_LIMIT_SEARCHES": ("rate_limit_searches", 0),
    "RELIQUARY_LINT_COVERAGE_MIN": ("lint_coverage_min", 8),
    "RELIQUARY_LINT_COLD_MIN_EVENTS": ("lint_cold_min_events", 200),
}


def _parse_server_args(monkeypatch, env: dict[str, str], argv: list[str] | None = None):
    for name in list(INT_ENV_VARS) + ["RELIQUARY_USER_ID"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["server.py", *(argv or [])])
    return server.parse_args()


# --- Finding 11: integer env vars


@pytest.mark.parametrize("name", sorted(INT_ENV_VARS))
def test_blank_integer_env_var_means_default(monkeypatch, name):
    args = _parse_server_args(monkeypatch, {name: ""})
    attr, default = INT_ENV_VARS[name]
    assert getattr(args, attr) == default
    args = _parse_server_args(monkeypatch, {name: "  "})
    assert getattr(args, attr) == default


def test_bad_integer_env_var_names_the_variable(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _parse_server_args(monkeypatch, {"RELIQUARY_OAUTH_ACCESS_TOKEN_TTL": "432000   # 5 days"})
    assert str(excinfo.value) == (
        "RELIQUARY_OAUTH_ACCESS_TOKEN_TTL must be an integer, got '432000   # 5 days'"
    )


def _env_file_values(path: Path) -> dict[str, str]:
    """Parse like `docker run --env-file` / systemd: everything after '=' is the value."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value
    return values


def test_env_example_works_as_a_literal_env_file(monkeypatch):
    values = _env_file_values(ROOT / ".env.example")
    for name, value in values.items():
        assert "#" not in value, f"{name}: keep comments on their own line"
    args = _parse_server_args(monkeypatch, {k: v for k, v in values.items() if k.startswith("RELIQUARY_")})
    assert args.oauth_access_token_ttl == 432000
    assert args.oauth_refresh_token_ttl == 0
    assert args.image_url_ingest is True  # a blank value would silently mean false
    assert args.metrics_public is False
    assert values["MEM0_TELEMETRY"] == "false"


def test_env_example_lists_every_variable_the_code_reads():
    code = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").glob("*.py"))
    read = set(re.findall(r'(?:getenv|environ\.get)\(\s*"(RELIQUARY_[A-Z_]+)"', code))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(name for name in read if name not in example)
    assert not missing, f"undocumented in .env.example: {missing}"


# --- Finding 2: RELIQUARY_USER_ID


def test_server_user_id_defaults_from_environment(monkeypatch):
    assert _parse_server_args(monkeypatch, {}).user_id == "default"
    assert _parse_server_args(monkeypatch, {"RELIQUARY_USER_ID": "household"}).user_id == "household"
    assert _parse_server_args(monkeypatch, {"RELIQUARY_USER_ID": ""}).user_id == "default"
    assert _parse_server_args(monkeypatch, {"RELIQUARY_USER_ID": "household"},
                              ["--user-id", "cli"]).user_id == "cli"


# --- Finding 2: image, compose and ignore files


def test_dockerfile_pins_the_deployed_user_ids_and_creates_data_dirs():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "addgroup --system --gid 101 appuser" in dockerfile
    assert "adduser --system --uid 100 --ingroup appuser" in dockerfile
    assert "mkdir -p /home/appuser /data/blobs /data/compiled /data/state" in dockerfile
    assert "chown -R appuser:appuser /srv /home/appuser /data" in dockerfile
    assert re.search(r"^\s+MEM0_TELEMETRY=false\s*$", dockerfile, re.M)
    assert dockerfile.index("USER appuser") > dockerfile.index("chown -R appuser")


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_mounts_corpus_read_only_and_disables_telemetry():
    app = _compose()["services"]["app"]
    assert "${CORPUS_HOST_DIR:-./corpus}:/corpus:ro" in app["volumes"]
    assert app["environment"]["MEM0_TELEMETRY"] == "${MEM0_TELEMETRY:-false}"


def test_compose_healthchecks_and_offline_model_pull():
    services = _compose()["services"]
    app_check = services["app"]["healthcheck"]["test"]
    assert app_check[:3] == ["CMD", "python", "-c"]
    assert "/healthz" in app_check[3] and "ProxyHandler({})" in app_check[3]
    compile(app_check[3], "<healthcheck>", "exec")
    qdrant_check = services["qdrant"]["healthcheck"]["test"]
    assert qdrant_check[:3] == ["CMD", "bash", "-c"] and "/readyz" in qdrant_check[3]
    entry = services["embedder-pull"]["entrypoint"]
    assert entry[:2] == ["/bin/sh", "-c"]
    assert entry[2] == "ollama show nomic-embed-text >/dev/null 2>&1 || ollama pull nomic-embed-text"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash for /dev/tcp")
def test_qdrant_healthcheck_command_reads_the_status_line(tmp_path):
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/readyz" else 503)
            self.end_headers()
            self.wfile.write(b"all shards are ready")

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        command = _compose()["services"]["qdrant"]["healthcheck"]["test"][3].replace("6333", str(port))
        assert subprocess.run(["bash", "-c", command], timeout=10).returncode == 0
        failing = command.replace("/readyz", "/not-ready")
        assert subprocess.run(["bash", "-c", failing], timeout=10).returncode != 0
    finally:
        httpd.shutdown()


def test_dockerignore_patterns_reach_nested_directories():
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for pattern in ("**/__pycache__/", "**/*.py[cod]", ".env.*", ".worktrees/"):
        assert pattern in lines
    assert "__pycache__/" not in lines and "*.py[cod]" not in lines


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_gitignore_hides_env_variants_but_not_the_example():
    def ignored(path):
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0
    assert ignored(".env") and ignored(".env.local") and ignored(".env.production")
    assert not ignored(".env.example")


# --- Finding 3: CI


def _workflow(name):
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the `on` key as True
    return data


def _steps(job):
    return [step.get("run", "") for step in job.get("steps", [])]


def test_tests_workflow_checks_what_ships():
    workflow = _workflow("tests.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert "workflow_call" in workflow["on"]
    runs = "\n".join(_steps(workflow["jobs"]["test"]))
    for command in ("-r requirements.lock", "python -m pytest -q", "python -m compileall -q app",
                    "uv lock --check", "uvx pip-audit -r requirements.lock --no-deps --disable-pip",
                    "uv export --frozen", "docker build"):
        assert command in runs


def test_publishing_is_gated_on_tests_and_release_is_isolated():
    workflow = _workflow("docker-publish.yml")
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["test"]["uses"] == "./.github/workflows/tests.yml"
    assert jobs["publish"]["needs"] == "test"
    assert jobs["publish"]["permissions"] == {"contents": "read", "packages": "write"}
    assert jobs["release"]["needs"] == "publish"
    assert jobs["release"]["permissions"] == {"contents": "write"}
    assert [s["uses"].split("@")[0] for s in jobs["release"]["steps"]] == ["softprops/action-gh-release"]
    assert not any("softprops" in str(s.get("uses")) for s in jobs["publish"]["steps"])
    tag_check = next(s for s in jobs["publish"]["steps"] if "pyproject" in s.get("name", ""))
    assert jobs["publish"]["steps"].index(tag_check) < next(
        i for i, s in enumerate(jobs["publish"]["steps"]) if "build-push-action" in str(s.get("uses")))


def test_tag_version_check_rejects_a_mismatched_tag(tmp_path):
    step = next(s for s in _workflow("docker-publish.yml")["jobs"]["publish"]["steps"]
                if "pyproject" in s.get("name", ""))
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)

    def run(tag):
        env = {"GITHUB_REF_NAME": tag, "PATH": "/usr/bin:/bin:/usr/local/bin"}
        env["PATH"] = str(Path(sys.executable).parent) + ":" + env["PATH"]
        script = step["run"].replace("python3 ", f"{sys.executable} ")
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)

    assert run(f"v{version}").returncode == 0
    assert run("v9.9.9").returncode != 0


def test_actions_are_pinned_to_full_commit_shas():
    for name in ("tests.yml", "docker-publish.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if not match or match.group(1).startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", match.group(1)), line
            assert re.search(r"# v\d+\.\d+\.\d+$", line), f"name the version in a comment: {line}"


# --- Finding 3: Mem0 2.x API contract


class StrictMem0V2:
    """Shaped like mem0.Memory 2.x: entity ids only inside `filters`, `top_k`
    instead of `limit`, get_all defaulting to 20 rows. Records every call."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, tuple, dict]] = []
        self._next = 0

    @staticmethod
    def _reject(method, kwargs):
        bad = sorted(set(kwargs) & {"user_id", "agent_id", "run_id", "limit"})
        if bad:
            raise ValueError(f"{method}() got unsupported top-level parameters {bad}; use filters / top_k")
        if kwargs:
            raise TypeError(f"{method}() got unexpected keyword arguments {sorted(kwargs)}")

    @staticmethod
    def _require_entity(filters):
        if not any(key in (filters or {}) for key in ("user_id", "agent_id", "run_id")):
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id")

    def _match(self, row, filters):
        for key, value in filters.items():
            if key == "user_id":
                if row["user_id"] != value:
                    return False
            elif key == "AND":
                if not all(self._match(row, part) for part in value):
                    return False
            elif row["metadata"].get(key) != value:
                return False
        return True

    def search(self, query, *, top_k=20, filters=None, threshold=0.1, rerank=False, **kwargs):
        self.calls.append(("search", (query,), dict(top_k=top_k, filters=filters, threshold=threshold, **kwargs)))
        self._reject("search", kwargs)
        self._require_entity(filters)
        hits = [dict(row, score=0.9) for row in self.rows.values() if self._match(row, filters)]
        return {"results": hits[:top_k]}

    def get_all(self, *, filters=None, top_k=20, **kwargs):
        self.calls.append(("get_all", (), dict(filters=filters, top_k=top_k, **kwargs)))
        self._reject("get_all", kwargs)
        self._require_entity(filters)
        return {"results": [row for row in self.rows.values() if self._match(row, filters)][:top_k]}

    def get(self, memory_id):
        self.calls.append(("get", (memory_id,), {}))
        return self.rows.get(memory_id)

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None, metadata=None, infer=True,
            memory_type=None, prompt=None):
        self.calls.append(("add", (messages,), dict(user_id=user_id, metadata=metadata, infer=infer)))
        self._next += 1
        memory_id = f"v2-{self._next}"
        text = messages if isinstance(messages, str) else " ".join(m["content"] for m in messages)
        self.rows[memory_id] = {"id": memory_id, "memory": text, "user_id": user_id,
                                "metadata": dict(metadata or {})}
        return {"results": [{"id": memory_id, "memory": text, "event": "ADD"}]}

    def update(self, memory_id, data, metadata=None):
        self.calls.append(("update", (memory_id, data, metadata), {}))
        self.rows[memory_id].update(memory=data, metadata=dict(metadata or {}))
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        self.calls.append(("delete", (memory_id,), {}))
        self.rows.pop(memory_id, None)
        return {"message": "Memory deleted successfully!"}


def run(coro):
    return asyncio.run(coro)


def test_server_search_takes_the_mem0_2x_branch(make_proxy):
    memory = StrictMem0V2()
    proxy = make_proxy(memory=memory, compiled_memory=StrictMem0V2())
    assert proxy._search_user_id_param is False and proxy._search_limit_param == "top_k"
    memory.add("the nas backup runs nightly", user_id="my_lord", metadata={"domain": "infra"}, infer=False)
    memory.add("someone else's note", user_id="other", metadata={"domain": "infra"}, infer=False)

    result = run(proxy.handle_search_tool({"query": "nas backup", "domain": "infra"}))
    assert not result["isError"], result
    assert [hit["text"] for hit in result["structuredContent"]["results"]] == ["the nas backup runs nightly"]
    searches = [kwargs for name, _, kwargs in memory.calls if name == "search"]
    assert searches
    for kwargs in searches:
        assert "user_id" not in kwargs and "limit" not in kwargs
        assert kwargs["filters"]["user_id"] == "my_lord"
        assert kwargs["top_k"] >= 1
    assert any(kwargs["filters"].get("domain") == "infra" for kwargs in searches)


def _exercise_adapter_paths(proxy, memory):
    """Drive every server and ingest path that talks to Mem0."""
    added = run(proxy.handle_add_memory_tool({"text": "ARDA-NAS-BACKUP-0001 runs nightly"}))
    memory_id = added["structuredContent"]["ids"][0]
    run(proxy.handle_search_tool({"query": "ARDA-NAS-BACKUP-0001"}))  # exact id: lexical get_all too
    run(proxy.handle_search_tool({"query": "backup", "domain": "infra", "threshold": 0.2}))
    run(proxy.handle_fetch_tool({"id": memory_id}))
    run(proxy.handle_update_tool({"id": memory_id, "text": "the backup runs hourly"}, allow_user_id=True))
    run(proxy.handle_compile_page_tool({"slug": "nas", "title": "NAS", "markdown": "# NAS\nbackups",
                                        "domain": "infra", "topic": "nas"}))
    run(proxy.handle_compile_page_tool({"slug": "nas", "title": "NAS", "markdown": "# NAS\nhourly",
                                        "domain": "infra", "topic": "nas"}))
    run(proxy.handle_delete_tool({"id": memory_id}, allow_user_id=True))
    records = [{"id": "r1", "text": "imported", "metadata": {"domain": "infra"}}]
    ingest_records(memory, records, user_id="my_lord")
    ingest_records(memory, [{"id": "r1", "text": "imported v2", "metadata": {}}], user_id="my_lord")
    existing_import_index(memory, user_id="my_lord")


def test_adapter_calls_bind_to_the_installed_mem0_signatures(make_proxy):
    mem0 = pytest.importorskip("mem0")
    memory, compiled = StrictMem0V2(), StrictMem0V2()
    proxy = make_proxy(memory=memory, compiled_memory=compiled)
    _exercise_adapter_paths(proxy, memory)
    calls = memory.calls + compiled.calls
    seen = {name for name, _, _ in calls}
    assert {"search", "get_all", "add", "update", "delete", "get"} <= seen

    for name, args, kwargs in calls:
        real = inspect.signature(getattr(mem0.Memory, name))
        real.bind(None, *args, **kwargs)  # raises TypeError on a mismatch
        named = {p.name for p in real.parameters.values() if p.kind is not inspect.Parameter.VAR_KEYWORD}
        swallowed = set(kwargs) - named
        # Mem0 2.x hides **kwargs behind search/get_all: anything not a named
        # parameter is either rejected (user_id) or silently ignored (limit).
        assert not swallowed, f"{name}() would not honour {sorted(swallowed)}"


def test_strict_fake_matches_the_installed_mem0_signatures():
    mem0 = pytest.importorskip("mem0")
    for name in ("search", "get_all", "add", "update", "delete", "get"):
        real = inspect.signature(getattr(mem0.Memory, name))
        fake = inspect.signature(getattr(StrictMem0V2, name))
        assert [(p.name, p.kind, p.default) for p in real.parameters.values()] == [
            (p.name, p.kind, p.default) for p in fake.parameters.values()
        ], name


# --- Finding 13: HTTP auth gates through the ASGI callable


def _asgi(app, method, path, *, headers=None, body=b""):
    scope = {
        "type": "http", "http_version": "1.1", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": b"", "scheme": "http", "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 5000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], payload


BEARER = {"authorization": "Bearer claude-secret"}


def test_status_requires_the_claude_bearer(proxy):
    assert _asgi(proxy, "GET", "/status")[0] == 401
    assert _asgi(proxy, "GET", "/status", headers={"authorization": "Bearer openai-secret"})[0] == 401
    status, body = _asgi(proxy, "GET", "/status", headers=BEARER)
    assert status == 200 and json.loads(body)["default_user_id"] == "my_lord"


def test_metrics_require_auth_unless_made_public(make_proxy):
    private = make_proxy()
    assert _asgi(private, "GET", "/metrics")[0] == 401
    assert _asgi(private, "GET", "/metrics", headers=BEARER)[0] == 200
    public = make_proxy(metrics_public=True)
    assert _asgi(public, "GET", "/metrics")[0] == 200


def test_dynamic_registration_disabled_returns_403(make_proxy):
    payload = json.dumps({"client_name": "x", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}).encode()
    closed = make_proxy(oauth_allow_registration=False)
    status, body = _asgi(closed, "POST", "/oauth/register", headers={"content-type": "application/json"},
                         body=payload)
    assert status == 403 and json.loads(body)["error"] == "registration_not_supported"
    open_ = make_proxy()
    assert _asgi(open_, "POST", "/oauth/register", headers={"content-type": "application/json"},
                 body=payload)[0] == 201
