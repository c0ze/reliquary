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
f=$(mktemp) && uv export --frozen --no-hashes --no-emit-project -o "$f" && uvx pip-audit -r "$f" --no-deps --disable-pip
docker build -t reliquary:local .
```

CI (`.github/workflows/tests.yml`) runs the same list against
`requirements.lock`; `docker-publish.yml` publishes only after it passes.

Tests use dependency-light in-memory fakes, including Mem0 1.x and 2.x API
contracts; `tests/test_audit_2026_11_packaging.py` binds every adapter call to
the installed `mem0.Memory` signatures. Integration checks must also exercise
the built image with the installed Mem0, Qdrant and embedder. Keep direct pins
in `pyproject.toml` and `requirements.txt` aligned; Docker installs
`requirements.lock`, while uv uses `uv.lock`. Regenerate `requirements.lock`
with the command in `requirements.txt`: it passes
`--python-platform x86_64-unknown-linux-gnu`, without which a run on an arm64
Mac drops greenlet. Python 3.12 is the supported and tested version.

Imports are incremental by default (matched by JSONL `id`). The importer stores
taxonomy as catalog slugs and keeps corpus dates as `source_updated_at` /
`source_created_at`, which the recency tiebreaker prefers over Mem0's own
timestamps; these derived fields are not part of `import_content_hash`.

## Deployment on Arda's machine

The checkout is `/home/arda/projects/utils/agents/reliquary`. The running stack
is configured separately in `/home/arda/reliquary-deploy/docker-compose.yml`.
The app publishes only `127.0.0.1:8787`, behind the Cloudflare tunnel for
`https://reliquary.arda.tr`. Rebuild and recreate only the app; preserve the
Qdrant, Ollama, corpus, blob, compiled and OAuth state mounts. The image's
`appuser` is uid 100 / gid 101 (pinned in the Dockerfile); the data mounts are
owned by 100:101.

The local image selected by
`/home/arda/reliquary-deploy/docker-compose.override.yml` is
`reliquary:openai-json-fix-20261003`. It includes the September audit fixes and
the OpenAI search/fetch JSON response correction. A plain
`docker compose up -d --no-deps app` from that directory includes the override.
No registry release is implied by a local deployment. See
[the audit record](docs/AUDIT-2026-09-06.md) and
[the JSON fix verification](docs/OPENAI-JSON-FIX-2026-10-03.md) for evidence
and rollback.

Token-store compatibility boundary: 0.5.2 rewrites `oauth_tokens.json` and
`oauth_refresh_tokens.json` in the state directory once, keying them by
`sha256:<hex>` of each token instead of the token. Older images (0.5.1,
`reliquary:local-audit-20260906`, `reliquary:openai-json-fix-20261003`) read
those keys as plaintext tokens: they reject every client's real token and would
accept the stored hash strings as bearers. Before the first 0.5.2 start, copy
both files aside. After it has started once, roll back only to an image that
reads hashed token stores, or put the pre-upgrade copies of both files back
together with the older image; tokens issued since the upgrade are then lost
and those connectors re-authorise. Never run an older image on the migrated
files.

Upload-marker compatibility boundary: a pending upload slot now marks the blob
reference it holds as an `upload:<upload_id>` owner in the blob's sidecar
(`<blob_dir>/<ab>/<sha256>.json`), and hands it to the caption memory on
commit. A slot's reference is released or handed over only through that marker
and only when its owner is certain; otherwise the bytes stay (a leak lint
reports). Upgrading: slots pending in the deployed build carry no marker and
are forgotten at the first start without touching any reference (their clients
get `unknown_upload` and upload again; their bytes stay as an orphan lint
reports). A build older than this one misreads the marker: it treats the upload
as already committed and never reclaims its bytes. Before starting an older
build, let pending uploads commit or expire (a slot lives at most the upload
TTL, 10 minutes, unless its commit outcome could not be read and it is kept for
a retry: check that `pending_uploads.json` is empty), or remove the `upload:`
entries from the sidecars' `owners` by hand without changing `ref_count`.
