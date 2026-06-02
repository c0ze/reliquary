# Reliquary

A personal **memory server for AIs** — [Mem0](https://github.com/mem0ai/mem0) +
[Qdrant](https://qdrant.tech) behind the **Model Context Protocol (MCP)**, so any
Claude or ChatGPT session can search and store your long-term memories.

This is the full guide (also published to the
[project wiki](https://github.com/c0ze/reliquary/wiki)). For the short version,
see the [README](../README.md).

## Contents

1. [What it does](#what-it-does)
2. [How it works](#how-it-works)
3. [What you need](#what-you-need)
4. [Setup](#setup)
5. [Configuration reference](#configuration-reference)
6. [Importing & ingesting data](#importing--ingesting-data)
7. [Example: ingesting an Obsidian vault](#example-ingesting-an-obsidian-vault)
8. [Authentication](#authentication)
9. [Embeddings & the tokenizer](#embeddings--the-tokenizer)
10. [Embedder alternatives (Ollama vs LM Studio vs external)](#embedder-alternatives)
11. [External access (Cloudflare / Tailscale / ngrok)](#external-access)
12. [Operations](#operations)
13. [Troubleshooting](#troubleshooting)
14. [Security notes](#security-notes)
15. [MCP transport notes](#mcp-transport-notes)

---

## What it does

Reliquary turns a pile of your own notes/exports into a **semantic memory** that
AI assistants can query and append to over MCP:

- **Semantic recall** — vector search over your corpus, returning the most
  relevant memories for a natural-language query.
- **Taxonomy routing** — optional `domain / hall / room / topic` metadata lets a
  query that mentions a known area (e.g. "infra", "pagan") get routed to a
  narrower pool *before* falling back to global search, which sharpens results.
- **Two MCP endpoints** — a full read+write one shaped for the **Claude.ai**
  connector, and a lean, deep-research-shaped read (optionally write) one for
  **ChatGPT**.
- **OAuth 2.1 shim** — PKCE + dynamic client registration + revocable,
  resource-scoped tokens, so the Claude.ai Custom Connector can authenticate.
- **No GPU, no chat LLM required** for retrieval — just an embedding model and a
  vector store. Runs on a small CPU box.

> Reliquary is the **serving engine**. Building the corpus from your notes is up
> to you — it ingests any JSONL of `{"id", "text", "metadata"}` records. See
> [Importing & ingesting data](#importing--ingesting-data).

---

## How it works

### Components

```text
                 ┌────────────────────────────────────────────┐
   Claude.ai ───▶│  reliquary app  (ASGI / uvicorn)           │
   ChatGPT   ───▶│   • /claude/mcp   (full read+write)        │
   curl      ───▶│   • /openai/mcp   (lean read, opt-in write)│
                 │   • OAuth 2.1 shim, /healthz, /status       │
                 │   • brand assets: /favicon.ico, /icon*.png  │
                 └───────────────┬───────────────┬────────────┘
                                 │ Mem0 client   │ embeddings
                                 ▼               ▼
                         ┌──────────────┐  ┌──────────────────┐
                         │   Qdrant     │  │  Embedder        │
                         │ (vector DB)  │  │  (Ollama/LMStudio│
                         │              │  │   /external API) │
                         └──────────────┘  └──────────────────┘
```

The Docker Compose stack runs three services: **qdrant** (vector store),
**embedder** (Ollama serving a small embedding model on CPU), and **app** (this
server — MCP + OAuth + the Mem0 client). No chat LLM is involved in retrieval.

### A search request, end to end

1. The MCP client calls the `search` / `mem0_search` tool with a query.
2. The app embeds the query via the embedder, and (if a dataset is loaded)
   checks the **taxonomy catalog** to see whether the query mentions a known
   domain/room/topic — if so it adds a filter to narrow the pool.
3. Mem0 runs the vector search against Qdrant and returns scored hits.
4. The app shapes the result for the endpoint: rich (Claude) or lean snippets
   with `id`/`title`/`url` (ChatGPT). `fetch(id)` returns the full document.

### Endpoints

| Path | Method | Auth | Purpose |
|------|--------|------|---------|
| `/claude/mcp` | POST | Bearer or OAuth | Full MCP: `mem0_status`, `mem0_search`, `mem0_fetch`, `mem0_add_memory` |
| `/openai/mcp` | POST | Bearer (or no-auth) | Lean MCP: `search`, `fetch`, + `add_memory` if writes enabled |
| `/healthz` | GET | none | Liveness check |
| `/status` | GET | Claude bearer | Config + taxonomy introspection |
| `/mem0/search?q=` | GET | Claude bearer | Raw debug search (returns memories directly) |
| `/.well-known/oauth-*`, `/oauth/*` | — | — | OAuth 2.1 discovery / authorize / token / revoke |
| `/favicon.ico`, `/icon[-<size>].png` | GET | none | Brand assets (also embedded in MCP `serverInfo`) |

---

## What you need

**Minimum (Docker path — recommended):**

- A Linux/macOS host with **Docker** + the Compose plugin. A small CPU box is
  fine; ~2 GB RAM free is comfortable for the embedder + Qdrant.
- ~1–2 GB disk for the embedding model + your vectors.
- No GPU, no OpenAI key, no chat LLM for retrieval-only use.

**For external access (so Claude.ai / ChatGPT can reach it):**

- A way to expose the port over **public HTTPS** — a Cloudflare Tunnel, Caddy,
  Traefik, or ngrok. (See [External access](#external-access). Note: a private
  Tailscale address is **not** reachable by ChatGPT's servers.)

**For a manual (non-Docker) install:**

- Python 3.12+, a running Qdrant, and an embedding backend (Ollama, LM Studio,
  or an external embeddings API).

---

## Setup

### Docker (recommended)

```bash
git clone https://github.com/c0ze/reliquary.git
cd reliquary

cp .env.example .env                 # set MEM0_CLAUDE_MCP_TOKEN etc.
cp config.example.yaml config.yaml   # points at the qdrant + embedder services

docker compose up -d                 # qdrant + embedder (+ model pull) + app
curl -s http://127.0.0.1:8787/healthz
```

Three services come up: **qdrant**, **embedder** (Ollama; a one-shot
`embedder-pull` fetches `nomic-embed-text` on first boot), and **app** on
`127.0.0.1:8787`. The port is bound to loopback on purpose — put a TLS
terminator in front of it (see [External access](#external-access)).

Generate strong tokens:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Manual (no Docker)

```bash
pip install -r requirements.lock          # or requirements.txt
# Run Qdrant somewhere (docker run -p 6333:6333 qdrant/qdrant), and an embedder.
python app/server.py --config config.yaml --host 127.0.0.1 --port 8787 --no-chat-upstream
```

`--no-chat-upstream` runs MCP-only (no OpenAI chat-completion passthrough), which
is what you want for a pure memory server.

---

## Configuration reference

Behaviour is driven by **environment variables** (`.env`) and a **Mem0 config
file** (`config.yaml`). Run `python app/server.py --help` for every flag.

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEM0_CLAUDE_MCP_TOKEN` | — | Bearer for `/claude/mcp` (required for write + OAuth) |
| `MEM0_OPENAI_MCP_TOKEN` | — | Bearer for `/openai/mcp` |
| `MEM0_OPENAI_ALLOW_NOAUTH` | `false` | `true` lets anyone reaching `/openai/mcp` read with no token |
| `MEM0_OPENAI_ALLOW_WRITE` | `false` | Expose `add_memory` on `/openai/mcp`. **Refuses to start** together with `ALLOW_NOAUTH=true` (no public write) |
| `MEM0_OAUTH_CLIENT_ID` | — | Pin the OAuth client id; only this id is accepted |
| `MEM0_OAUTH_ALLOW_REGISTRATION` | `true` | Allow `POST /oauth/register` (DCR). Disable after the connector registers once |
| `MEM0_OAUTH_VERBATIM_TOKEN` | `false` | Return the master bearer verbatim instead of a derived, revocable token |
| `MEM0_DATASET_PATH` | — | Curated JSONL (or dir) enabling taxonomy routing + `fetch` bootstrap docs |
| `MEM0_EMBEDDER_PROVIDER` / `_MODEL` / `_BASE_URL` / `_API_KEY` / `_DIMS` | — | Synthesize an embedder block without editing `config.yaml` |
| `MEM0_CLAUDE_MCP_PATH` / `MEM0_OPENAI_MCP_PATH` | `/claude/mcp` / `/openai/mcp` | Override endpoint paths |

### Key CLI flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--config` | `~/.mem0/config.yaml` | Mem0 config path |
| `--host` / `--port` | `127.0.0.1` / `8787` | Listen address |
| `--user-id` | `default` | Default Mem0 user_id for retrieval |
| `--memory-limit` | `5` | Max memories returned |
| `--memory-threshold` | none | Optional minimum similarity score |
| `--no-chat-upstream` | off | MCP-only (disable chat-completion passthrough) |
| `--memory-concurrent-reads` | auto | Force concurrent reads (auto: concurrent only for server-backed Qdrant) |
| `--allowed-origin` | — | Allowed `Origin` for MCP endpoints (repeatable) |

### `config.yaml` (Mem0)

```yaml
vector_store:
  provider: qdrant
  config:
    host: qdrant            # compose service name
    port: 6333
    embedding_model_dims: 768   # MUST match the embedder's output

embedder:
  # Ollama exposes an OpenAI-compatible /v1/embeddings, so mem0's "openai"
  # provider can point straight at it (no extra Python dependency).
  provider: openai
  config:
    model: nomic-embed-text
    openai_base_url: http://embedder:11434/v1
    api_key: ollama         # unused by Ollama, but the client requires a value
    embedding_dims: 768

llm:
  # mem0 instantiates an LLM client at startup even in MCP-only mode, so it
  # needs *a* valid llm section. It's never called for search/fetch/add, so a
  # dummy pointing at the embedder endpoint is enough.
  provider: openai
  config:
    model: nomic-embed-text
    openai_base_url: http://embedder:11434/v1
    api_key: ollama
```

---

## Importing & ingesting data

Reliquary ingests **JSONL** — one record per line:

```json
{"id": "stable-unique-id", "text": "the full text to embed and return", "metadata": {"title": "...", "domain": "infra", "room": "nas", "source_ref": "/path/or/url"}}
```

| Field | Required | Notes |
|-------|----------|-------|
| `id` | ✅ | Stable, unique. Re-ingesting the same id **updates** (de-duped), never duplicates |
| `text` | ✅ | What gets embedded *and* returned. Include a small provenance header if useful |
| `metadata` | ✅ (object) | `title` → result title; `source_url`/`source_ref` → document URL; `domain`/`hall`/`room`/`topic` → routing |

### Ingest CLI

```bash
python app/ingest.py corpus.jsonl --config config.yaml --user-id me

# multiple files / a directory of *.jsonl:
python app/ingest.py exports/ notes.jsonl --config config.yaml --user-id me

# preview without writing:
python app/ingest.py corpus.jsonl --config config.yaml --dry-run

# only first N records:
python app/ingest.py corpus.jsonl --config config.yaml --limit 100
```

| Flag | Purpose |
|------|---------|
| `dataset` (positional) | One or more JSONL files or directories of `*.jsonl` |
| `--user-id` | user_id attached to every record (match the server's `--user-id`) |
| `--limit N` | Ingest only the first N unique records |
| `--dry-run` | Show what would be imported, call nothing |
| `--infer` / `--no-infer` | Let Mem0 extract atomic facts with the LLM before writing (default **off**, so raw records are stored verbatim) |

**Run ingest with the same config and embedder as the server**, so the vectors
land in the same space. With the Docker stack:

```bash
docker compose run --rm app python app/ingest.py /data/corpus.jsonl \
  --config /config/config.yaml --user-id me
```

> ⚠️ **The embedder defines the vector space.** If you change the embedding model
> (or its dimensions), existing vectors become incomparable — **re-ingest** the
> whole corpus afterwards.

---

## Example: ingesting an Obsidian vault

[`examples/obsidian_to_jsonl.py`](https://github.com/c0ze/reliquary/blob/main/examples/obsidian_to_jsonl.py)
walks a vault, reads each note's frontmatter + body, and emits the JSONL format
above. It derives the taxonomy from your folder layout:
`<domain>/<hall>/…/<note>.md`.

```bash
# 1) Convert your vault to JSONL (skip private folders):
python examples/obsidian_to_jsonl.py ~/Documents/obsidian/myvault \
    -o corpus.jsonl \
    --exclude diary/private --exclude .trash

# 2) Ingest it:
python app/ingest.py corpus.jsonl --config config.yaml --user-id me
```

What it produces per note:

- **`id`** — `sha1(relative_path)[:16]`, stable across runs (edits update, never
  duplicate).
- **`text`** — a small header (`Vault`, `Path`, `Title`, frontmatter) followed by
  the note body, so the model sees provenance.
- **`metadata`** — `title` (frontmatter or filename), `source_ref` (path),
  `tags`, and `domain`/`hall`/`room`/`topic` from the folder hierarchy.

The folder→taxonomy mapping in `taxonomy_for()` is deliberately simple — **edit
it to match your vault.** For example, if your vault is organised as
`Projects/<project>/<note>.md` you might map the project name to `domain` and the
note to `topic`. The richer and more consistent your taxonomy, the better the
routing.

The same pattern works for any source (Gemini/ChatGPT exports, a blog, Joplin) —
write a small converter that emits `{"id","text","metadata"}` and ingest it.

---

## Authentication

There are **two** auth surfaces, one per connector.

### ChatGPT — API key (Bearer)

ChatGPT connectors use a static API key. Add an MCP server at
`https://your-host/openai/mcp` with **API key / Bearer** auth, using
`MEM0_OPENAI_MCP_TOKEN`. Keep `MEM0_OPENAI_ALLOW_NOAUTH=false` so the token is
required.

To let ChatGPT **write**, set `MEM0_OPENAI_ALLOW_WRITE=true` (exposes
`add_memory`). The server **refuses to start** if write is enabled together with
no-auth, to avoid an unauthenticated public write surface.

### Claude.ai — OAuth 2.1 (with a built-in shim)

Claude.ai Custom Connectors speak OAuth. Reliquary ships a small OAuth 2.1 shim:

- **PKCE** (S256), **public client** (`token_endpoint_auth_methods_supported:
  ["none"]` — there is **no client secret**).
- **Dynamic Client Registration** — Claude self-registers at `/oauth/register`.
- The discovery chain: an unauthenticated call to `/claude/mcp` returns
  `401` + `WWW-Authenticate` pointing at `/.well-known/oauth-protected-resource`,
  which lists the authorization server, whose metadata lives at
  `/.well-known/oauth-authorization-server`.

**Adding the connector:**

1. In Claude.ai → Settings → Connectors → Add custom connector, set the URL to
   `https://your-host/claude/mcp`. **Leave OAuth Client ID and Secret blank** —
   Claude registers itself via DCR.
2. Claude opens a browser authorize page (`/oauth/authorize`). **Paste your
   `MEM0_CLAUDE_MCP_TOKEN`** there and authorize. The connector receives a
   **derived, revocable** token (not the master).
3. Once it works, pin the client and lock down registration:
   ```bash
   MEM0_OAUTH_CLIENT_ID=<the client_id Claude registered>
   MEM0_OAUTH_ALLOW_REGISTRATION=false
   ```

> If a field *forces* a value, the pinned `MEM0_OAUTH_CLIENT_ID` works as the
> Client ID and any string works as the Secret (it's a public client; the secret
> isn't validated). The real credential is the token you paste on the authorize
> page.

OAuth tokens are derived, resource-scoped, 30-day, and held **in memory** — a
restart invalidates them and clients re-authorize. Revoke via `/oauth/revoke`.

---

## Embeddings & the tokenizer

The **embedder** turns text into vectors; Qdrant stores and searches them. Two
rules tie the pieces together:

1. **Dimensions must match.** The embedder's output dimensionality must equal
   Qdrant's `embedding_model_dims`. The default model, `nomic-embed-text`, is
   **768-dim**, so the config uses `768` in both `embedder.config.embedding_dims`
   and `vector_store.config.embedding_model_dims`. Mismatch → ingest/search
   errors or garbage results.
2. **The model defines the space.** Query and stored vectors must come from the
   **same** model. Changing the embedder means re-ingesting (see above).

**Why `nomic-embed-text`?** It's small, CPU-friendly, **multilingual**, and
768-dim — a good default if your notes mix languages. mem0 talks to it through
Ollama's **OpenAI-compatible `/v1/embeddings`** endpoint, so you use mem0's
`openai` embedder provider with no extra Python dependency.

> **Heads-up on TEI:** HuggingFace's Text-Embeddings-Inference server **fails to
> load** `nomic-embed-text-v1.5` (a duplicate `max_position_embeddings` field in
> its `config.json`). That's why the default stack uses Ollama, not TEI. If you
> want TEI, pick a model it can parse and update the dims.

There is no separate "tokenizer" to configure — tokenization is internal to
whichever embedding model you choose. You only ever set the **model** and its
**dimensions**.

---

## Embedder alternatives

mem0 supports several embedder providers. Reliquary's `ingest.py` can also
synthesize a provider from `MEM0_EMBEDDER_*` env vars. Common choices:

| Option | Provider in config | Pros | Cons / notes |
|--------|--------------------|------|--------------|
| **Ollama** (default) | `openai` → `http://host:11434/v1` | Zero extra deps, multilingual `nomic-embed-text`, CPU-fine, easy model pulls | Slightly slower than a tuned server |
| **LM Studio** | `lmstudio` (auto-detected on `:1234`/`api_key: lm-studio`) | Nice GUI, GGUF models, runs `nomic-embed-text-v1.5` | Desktop-oriented; you manage the server |
| **External API (OpenAI, etc.)** | `openai` with a real key | Fast, no local compute, high quality (`text-embedding-3-*`) | Sends your text to a third party; costs money; remember its dims (1536/3072) |
| **TEI (HF)** | `huggingface` / OpenAI-compat | Very fast on GPU | Can't parse some configs (see nomic note); GPU-oriented |

**Recommendation:** start with the **Ollama default**. Move to an external API
only if you want maximum quality and don't mind sending text out; move to LM
Studio if you already run it. Whatever you pick, **keep `embedding_model_dims` in
sync** and re-ingest after a change.

Swap models in the Docker stack by editing the `embedder` image/model in
`docker-compose.yml` **and** the model + dims in `config.yaml` together.

---

## External access

The app binds to `127.0.0.1:8787` by design. To let Claude.ai / ChatGPT reach
it, put a **public HTTPS** terminator in front. Options, roughly best-first for
this use case:

### Cloudflare Tunnel (recommended)

A persistent outbound tunnel — no open inbound ports, free, stable hostname,
TLS handled by Cloudflare.

```bash
cloudflared tunnel login
cloudflared tunnel create reliquary
# route a hostname to the local app:
cloudflared tunnel route dns reliquary mem0.example.com
# config.yml: ingress → service: http://127.0.0.1:8787
cloudflared tunnel run reliquary
```

Then point both connectors at `https://mem0.example.com/claude/mcp` and
`…/openai/mcp`. Narrow the tunnel to the needed paths if you like
(`/claude/*`, `/openai/*`, `/oauth/*`, `/.well-known/*`, `/healthz`).

### Caddy / Traefik (own domain + server)

If you already run a reverse proxy with a public IP, terminate TLS there and
`proxy_pass` to `127.0.0.1:8787`. Caddy gets you automatic Let's Encrypt in two
lines:

```caddyfile
mem0.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

### ngrok (quick / temporary)

```bash
ngrok http 8787
```

Great for testing; the free tier gives an ephemeral hostname that changes each
run (you'll re-add the connector). Fine to validate the flow, not ideal as a
permanent endpoint.

### Tailscale — works for Claude, **not** for ChatGPT

Tailscale (or Tailscale Funnel) is excellent for **private** access from your own
devices and the Claude desktop/mobile apps on your tailnet. But a plain tailnet
address (`100.x` / `*.ts.net` MagicDNS) is **CGNAT and not publicly routable**,
so **ChatGPT's servers cannot reach it** — that's a classic cause of a `424
Failed Dependency` in the ChatGPT connector. Use **Tailscale Funnel** (which does
expose a public `*.ts.net` HTTPS URL) or a Cloudflare Tunnel for ChatGPT.

| Method | Public to ChatGPT? | Stable host? | Inbound ports? | Best for |
|--------|--------------------|--------------|----------------|----------|
| Cloudflare Tunnel | ✅ | ✅ | none | Permanent, both connectors |
| Caddy/Traefik | ✅ | ✅ | 443 | You own a server + domain |
| ngrok | ✅ | ✗ (free) | none | Quick tests |
| Tailscale (tailnet) | ❌ | ✅ | none | Private/Claude-only |
| Tailscale Funnel | ✅ | ✅ | none | Public via tailnet |

---

## Operations

```bash
# Status / logs
docker compose ps
docker compose logs -f app
curl -s http://127.0.0.1:8787/healthz

# Authed status (config + taxonomy)
curl -s -H "Authorization: Bearer $MEM0_CLAUDE_MCP_TOKEN" http://127.0.0.1:8787/status

# Memory count (point count in Qdrant)
#   shown as approx_memory_count in /status and the mem0_status tool

# Upgrade the app image
docker compose pull app && docker compose up -d app

# Back up your vectors (named volume)
docker run --rm -v reliquary_qdrant_storage:/data -v "$PWD":/backup alpine \
    tar czf /backup/qdrant-backup.tgz -C /data .
```

- **Reads & concurrency:** an embedded on-disk Qdrant is **not** read-thread-safe,
  so reads are serialized; a Qdrant **server** allows concurrent reads (the app
  auto-detects and logs which). The Docker stack uses a server, so reads run
  concurrently.
- **Pinned deps:** the image installs from `requirements.lock`, so the runtime
  can't drift. Bump deliberately, regenerate the lock, re-test.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| **Search returns nothing, but ingest worked** | mem0 major-version API drift (2.0 moved the entity id into `filters={"user_id": …}` and renamed `limit`→`top_k`) | Use a build that targets your mem0 version; deps are pinned in `requirements.lock` to prevent this |
| **`PermissionError: '/nonexistent'` at startup** | Non-root container user had no writable `HOME`; mem0 writes `~/.mem0` | Fixed in the image (real `HOME`); if self-hosting, set `HOME` to a writable dir |
| **`OpenAIError: Missing credentials`** | mem0 instantiates an LLM client at init even MCP-only | Add the dummy `llm` section (see config) pointing at the embedder with any api_key |
| **TEI: `duplicate field max_position_embeddings`** | TEI can't parse `nomic-embed-text-v1.5`'s config | Use Ollama (default), or a TEI-parseable model |
| **ChatGPT 424 Failed Dependency** | Endpoint not publicly reachable (e.g. a tailnet `100.x` address) | Use a public HTTPS path (Cloudflare Tunnel / Tailscale Funnel) |
| **ChatGPT/Claude shows the connector but "no tools"** | Stale connector cache on the client; never ran `tools/list` | Refresh / remove + re-add the connector; reload the page |
| **Claude OAuth "stuck" after metadata fetch** | Cached auth flow on Claude's side | Reload the page / retry Connect; the server flow is stateless and re-runnable |
| **`GET /…/mcp → 405`** | Optional server-initiated SSE stream not offered | Harmless and spec-allowed; the client falls back to POST |

To watch a live connector handshake, tail the app logs and look for the
sequence: `/.well-known/*` → `/oauth/register` → `/oauth/authorize` →
`POST /claude/mcp 200`.

---

## Security notes

- **Default-closed.** `/openai/mcp` requires a bearer by default; the write tool
  is opt-in and **cannot** be combined with no-auth.
- **Use distinct tokens** for the Claude and OpenAI endpoints so one leak doesn't
  grant both (they have different capabilities).
- **OAuth tokens are derived & revocable** (resource-scoped, 30-day, in memory) —
  a restart invalidates them.
- **Publish only behind TLS**, and prefer narrowing your proxy to the
  `/claude/*`, `/openai/*`, `/oauth/*`, `/.well-known/*`, `/healthz` paths.
- **Don't commit secrets or your corpus.** `.env`, `config.yaml`, `*.jsonl`, and
  `qdrant_storage/` are gitignored.

---

## MCP transport notes

Reliquary implements MCP's current **Streamable HTTP** transport (single
endpoint, `POST`; advertised protocol version `2025-06-18`). A `POST` can return
JSON or an SSE (`text/event-stream`) body; the optional `GET`-as-SSE stream for
*server-initiated* messages is not offered (we return `405`, which the spec
permits).

The older two-endpoint **HTTP+SSE transport** (2024-11-05) is **deprecated** —
Streamable HTTP replaced it. SSE-the-format lives on *inside* Streamable HTTP, so
there's nothing to "migrate back" to. The brand icon is also advertised in the
MCP `serverInfo` (`title`, `websiteUrl`, and an embedded `icons` data-URI) so
connector UIs can display it.

---

*Built on [Mem0](https://github.com/mem0ai/mem0), [Qdrant](https://qdrant.tech),
and [Ollama](https://ollama.com). Licensed under MIT.*
