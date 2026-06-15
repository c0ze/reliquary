# Ingress narrowing

Reliquary exposes several routes. Not all of them should be reachable from the
public internet. This document lists what to allow and provides copy-paste
reverse-proxy snippets for Caddy and nginx.

## Public allowlist

Only these paths should be reachable from untrusted networks:

| Path prefix | Purpose |
|---|---|
| `/claude/*` | Claude MCP endpoint (bearer-protected) |
| `/openai/*` | ChatGPT / OpenAI MCP endpoint (bearer or no-auth) |
| `/uploads/*` | Raw binary upload slots for `create_image_upload`. Requires the same write bearer as the MCP endpoint that minted the slot (anonymous uploads → `401` before any bytes are read); the one-time `upl_` id is also unguessable and short-lived. |
| `/oauth/*` | OAuth 2.1 token exchange |
| `/.well-known/*` | OAuth discovery metadata |
| `/healthz` | Liveness probe (unauthenticated, read-only) |
| `/favicon.ico` | Browser favicon |
| `/icon*.png` | App icon assets |

## Block everything else

These paths are **not** on the public allowlist even though some are
bearer-protected. Scrape or access them only from within a trusted network
(e.g. via Tailscale, an internal VLAN, or a local loopback):

| Path | Reason |
|---|---|
| `/status` | Leaks config details (user IDs, upstream URLs, catalog domains) |
| `/mem0/search` | Raw vector-store debug search |
| `/metrics` | Prometheus endpoint — scrape internally, not from the internet |
| `/blobs/*` | Signed blob downloads — bearer/signature protected but no public need |
| `/v1/*` | Upstream LLM passthrough — internal only |

## Caddy

```caddy
reliquary.example.com {
    @allowed path \
        /claude/* \
        /openai/* \
        /uploads/* \
        /oauth/* \
        /.well-known/* \
        /healthz \
        /favicon.ico \
        /icon*.png

    handle @allowed {
        reverse_proxy localhost:8787
    }

    handle {
        respond 404
    }
}
```

## nginx

```nginx
server {
    listen 443 ssl;
    server_name reliquary.example.com;

    # Allowed public paths
    location ~ ^/(claude|openai|uploads|oauth)/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location ~ ^/\.well-known/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
    }

    location = /healthz {
        proxy_pass http://127.0.0.1:8787;
    }

    location = /favicon.ico {
        proxy_pass http://127.0.0.1:8787;
    }

    location ~ ^/icon.*\.png$ {
        proxy_pass http://127.0.0.1:8787;
    }

    # Everything else — including /status, /metrics, /blobs, /v1
    location / {
        return 404;
    }
}
```

## Hardening checklist

- [ ] Reverse proxy blocks all paths not on the allowlist above.
- [ ] `RELIQUARY_CLAUDE_MCP_TOKEN` is set to a strong random secret.
- [ ] `RELIQUARY_BLOB_SIGNING_KEY` is set so blob URLs survive restarts.
- [ ] `RELIQUARY_OAUTH_ALLOW_REGISTRATION` is set to `false` after the client has
      registered once.
- [ ] `/metrics` is only scraped from an internal network
      (Prometheus scrape job points at the internal address, not the public one).
- [ ] `RELIQUARY_OPENAI_ALLOW_NOAUTH` is `false` unless the OpenAI endpoint is on a
      trusted network with no untrusted callers.

## Environment variables for operational features

See the main README for full documentation. Summary:

| Variable | Default | Effect |
|---|---|---|
| `RELIQUARY_AUDIT_LOG` | unset | Path for the append-only JSONL write-audit log. Disabled when unset. |
| `RELIQUARY_RATE_LIMIT_WRITES` | `0` | Max write tool calls per token per minute. `0` = unlimited. |
| `RELIQUARY_RATE_LIMIT_SEARCHES` | `0` | Max search/fetch calls per token per minute. `0` = unlimited. |
| `RELIQUARY_METRICS_PUBLIC` | `false` | Expose `/metrics` without auth. Default: requires Claude bearer. |
