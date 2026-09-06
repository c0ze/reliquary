# Reliquary

Personal semantic memory served over MCP. Python ASGI application using Mem0
and Qdrant, with a filesystem blob store and an optional compiled-page layer.
See [README.md](README.md) for setup and [docs/GUIDE.md](docs/GUIDE.md) for usage.

## Architecture and constraints

- `app/server.py`: ASGI routes, MCP tools, authorization, retrieval, uploads,
  compiled-page indexing, and optional OpenAI-compatible chat proxy.
- `app/oauth.py`: PKCE authorization codes, access tokens, rotating refresh
  tokens and persisted replay-detection state. Static bearer auth also works.
- `app/catalog.py`: imported JSONL catalog and taxonomy routing.
- `app/ingest.py`: import CLI and Mem0 configuration loading. Mem0 2.x requires
  entity IDs in `filters` for reads; its listing API defaults to only 20 items.
- `app/blobs.py` / `app/compiled.py`: content-addressed bytes, reference counts,
  authoritative ownership, revision registry and synthesis status.
- `app/urlfetch.py`: validates every DNS answer; image requests connect to the
  validated IP with the original HTTP Host and TLS hostname. URL ingest uses
  direct egress, independently of proxy environment variables.
- `app/runtime.py`: memory readers/writer lock and persisted MCP sessions.

Imported records are protected. Only user-write records may be updated or
deleted; corrections to imports are separate proposed records. The lean OpenAI
endpoint ignores caller user IDs on writes. The compiled registry is global,
keyed by slug, and uses the configured default user.

Run one application process per state/blob/compiled directory. Locks are
process-local. A compiled-page write serializes its registry and vector-index
operations; upload slots are claimed across awaits. Updating/deleting a source
marks dependent current pages stale. Search grants current syntheses a small
relevance bonus, rather than unconditional priority over raw results.

Keep `RELIQUARY_STATE_DIR` persistent so restarts preserve connectors, and keep
`RELIQUARY_BLOB_SIGNING_KEY` stable. Set `RELIQUARY_PUBLIC_BASE_URL` for usable
remote upload and blob links. Never commit secrets or corpus data.

## Validation

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app
git diff --check
uv lock --check
uvx pip-audit -r requirements.lock --no-deps --disable-pip
docker build -t reliquary:local .
```

Tests use dependency-light in-memory fakes, including Mem0 1.x and 2.x API
contracts. Integration checks must also exercise the built image with the
installed Mem0, Qdrant and embedder. Keep direct pins in `pyproject.toml` and
`requirements.txt` aligned; Docker installs `requirements.lock`, while uv uses
`uv.lock`.

## Deployment on Arda's machine

The checkout is `/home/arda/projects/utils/agents/reliquary`. The running stack
is configured separately in `/home/arda/reliquary-deploy/docker-compose.yml`.
The app publishes only `127.0.0.1:8787`, behind the Cloudflare tunnel for
`https://reliquary.arda.tr`. Rebuild and recreate only the app; preserve the
Qdrant, Ollama, corpus, blob, compiled and OAuth state mounts.

The September 2026 audit uses a local image selected by
`/home/arda/reliquary-deploy/docker-compose.override.yml`. A plain
`docker compose up -d --no-deps app` from that directory includes the override.
No registry release is implied by a local deployment. See
[the audit record](docs/AUDIT-2026-09-06.md) for fixes, evidence and rollback.
