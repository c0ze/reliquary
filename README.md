# Reliquary

A personal **memory server for AIs** — [Mem0](https://github.com/mem0ai/mem0) +
[Qdrant](https://qdrant.tech) behind the **Model Context Protocol (MCP)**, so any
Claude or ChatGPT session can search and store your long-term memories.

- **Semantic recall** over your own corpus (vector search), with optional
  domain/hall/room/topic **routing** for sharper retrieval.
- **Two MCP endpoints:** a full read+write one for the Claude.ai connector, and
  a lean, deep-research-shaped read (optionally write) one for ChatGPT.
- **OAuth 2.1 shim** (PKCE, dynamic client registration, revocable
  resource-scoped tokens) so the Claude.ai Custom Connector can authenticate.
- **No GPU, no chat LLM** required for retrieval — just an embedding model and a
  vector store. Runs on a small CPU box.

> Reliquary is the serving engine. Building a corpus from your own notes/exports
> is left to you — it ingests any JSONL of `{"id", "text", "metadata"}` records.

📖 **Full guide:** [`docs/GUIDE.md`](docs/GUIDE.md) (also on the
[project wiki](https://github.com/c0ze/reliquary/wiki)) covers architecture,
ingestion (with an Obsidian example), embedder choices (Ollama / LM Studio /
external APIs), auth, and external-access options (Cloudflare Tunnel / Tailscale
/ ngrok).

## Quick start (Docker)

```bash
cp .env.example .env                  # set MEM0_CLAUDE_MCP_TOKEN etc.
cp config.example.yaml config.yaml    # point at the qdrant + embedder services
docker compose up -d
curl -s http://127.0.0.1:8787/healthz
```

Three services come up: **qdrant** (vector store), **embedder** (Ollama serving
the multilingual 768-dim `nomic-embed-text` on CPU; a one-shot `embedder-pull`
fetches the model on first boot), and **app** (this server: MCP + OAuth + the
Mem0 client). Publish `:8787` only behind a TLS terminator — a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
Caddy, or Traefik.

> **Embedder:** the compose default is Ollama + `nomic-embed-text` (mem0 talks to
> it via Ollama's OpenAI-compatible `/v1/embeddings`). To use a different model
> or an external embeddings API, edit the `embedder` block in `config.yaml` and
> keep `embedding_model_dims` in sync with the model. Changing the embedder
> changes the vector space — re-ingest your corpus afterwards.

## MCP endpoints & tools

| Endpoint | For | Auth | Tools |
|----------|-----|------|-------|
| `POST /claude/mcp` | Claude.ai Custom Connector | Bearer or OAuth | `mem0_status`, `mem0_search`, `mem0_fetch`, `mem0_add_memory` |
| `POST /openai/mcp` | ChatGPT / OpenAI-compatible | Bearer (or no-auth) | `search`, `fetch` (lean snippet shape); `add_memory` if `MEM0_OPENAI_ALLOW_WRITE=true` |

Also: `GET /healthz` (minimal, public), `GET /status` and `GET /mem0/search?q=...`
(both **require the Claude bearer** — they return config/taxonomy and raw
memories respectively), and the OAuth 2.1 discovery/authorize/token/revoke routes
under `/.well-known/*` and `/oauth/*`.

### Connecting Claude.ai

Add a Custom Connector pointing at `https://your-host/claude/mcp`. Claude runs
the OAuth flow; you authorize once by pasting your `MEM0_CLAUDE_MCP_TOKEN` into
the `/oauth/authorize` page, and the connector receives a derived, revocable
token (not the master). After it registers, pin `MEM0_OAUTH_CLIENT_ID` and set
`MEM0_OAUTH_ALLOW_REGISTRATION=false`.

### Connecting ChatGPT

Add an MCP server at `https://your-host/openai/mcp` with **API key** auth
(`Bearer` scheme) using `MEM0_OPENAI_MCP_TOKEN`. Keep
`MEM0_OPENAI_ALLOW_NOAUTH=false` so the token is required.

## Configuration

Behaviour is driven by env vars (see [.env.example](.env.example)) and a Mem0
config file (see [config.example.yaml](config.example.yaml)). Highlights:

| Variable | Purpose |
|----------|---------|
| `MEM0_CLAUDE_MCP_TOKEN` | bearer for `/claude/mcp` (required for write + OAuth) |
| `MEM0_OPENAI_MCP_TOKEN` | bearer for `/openai/mcp` |
| `MEM0_OPENAI_ALLOW_NOAUTH` | `true` to allow unauthenticated `/openai/mcp` (default `false`) |
| `MEM0_OPENAI_ALLOW_WRITE` | expose `add_memory` on `/openai/mcp` (default `false`). **Refuses to start** with `ALLOW_NOAUTH=true`, to avoid public write. |
| `MEM0_OAUTH_CLIENT_ID` / `MEM0_OAUTH_ALLOW_REGISTRATION` | lock the OAuth shim to one known client |
| `MEM0_DATASET_PATH` | curated JSONL enabling taxonomy routing + `fetch` bootstrap docs |

Run `python app/server.py --help` for the full flag list.

## Security notes

- **Default-closed.** `/openai/mcp` requires a bearer by default; the write tool
  is opt-in and cannot be combined with no-auth.
- **OAuth tokens are derived & revocable** (resource-scoped, 30-day expiry),
  held in memory — a restart invalidates them and clients re-authorize.
- **Embedded vs server Qdrant.** Reads run concurrently only against a Qdrant
  *server*; an embedded on-disk store is auto-detected and serialized (it is not
  read-thread-safe).
- Publish only behind TLS, and prefer narrowing your reverse proxy to the
  `/claude/*`, `/openai/*`, `/oauth/*`, `/.well-known/*`, `/healthz` paths.

## Loading data

```bash
# JSONL: one {"id": "...", "text": "...", "metadata": {...}} per line.
python app/ingest.py path/to/corpus.jsonl --config config.yaml --user-id default
```

`metadata.title` is used as the result title; `domain`/`hall`/`room`/`topic`
enable routing; any `source_url`/`source_ref` becomes the document URL.

Building the JSONL from your own notes is up to you.
[`examples/obsidian_to_jsonl.py`](examples/obsidian_to_jsonl.py) is a runnable
starting point that walks an Obsidian vault and derives the taxonomy from your
folder layout — see [`docs/GUIDE.md`](docs/GUIDE.md) for a full walkthrough.

## Development

```bash
pip install -r requirements.txt
python -m pytest            # or: python tests/test_helpers.py
python -m py_compile app/*.py
```

The `helpers` / `catalog` / `runtime` / `oauth` modules are dependency-light and
unit-tested without the Mem0/Qdrant stack.

## Built on

[Mem0](https://github.com/mem0ai/mem0) (memory layer), [Qdrant](https://qdrant.tech)
(vectors), and [Ollama](https://ollama.com) (embeddings). Licensed under
[MIT](LICENSE).
