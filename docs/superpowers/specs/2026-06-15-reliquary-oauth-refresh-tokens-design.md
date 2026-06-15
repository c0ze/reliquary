# Reliquary OAuth refresh tokens — design

**Date:** 2026-06-15
**Component:** `reliquary/` (the OAuth 2.1 shim in `app/oauth.py`)
**Issue:** #56
**Goal:** Stop frequent connector sign-outs by adding OAuth 2.1 **refresh tokens**
(rotating, long-lived/non-expiring, revocable) plus a **configurable access-token
TTL**, all persisted — so a connector silently refreshes and effectively never
needs a manual re-authorization, without resorting to an unrevocable infinite
access token.

## Problem

Claude.ai connectors get signed out too often. Today (`app/oauth.py`):

- Access token TTL is **30 days** (`ACCESS_TOKEN_TTL`), and there are **no refresh
  tokens** — the token endpoint only accepts `grant_type=authorization_code`
  ([oauth.py:227](app/oauth.py#L227)) and discovery advertises only that grant
  ([oauth.py:108](app/oauth.py#L108)). At expiry the connector must do a full
  re-authorization.
- Derived tokens are opaque, held in memory, and **persisted only when
  `MEM0_STATE_DIR` is set** ([server.py:313](app/server.py#L313)). With no state
  dir, every restart (deploy) wipes them → sign-out. This is the likely dominant
  cause of "often".

A longer TTL or refresh token does **not** help across restarts unless tokens are
persisted, so persistence (`MEM0_STATE_DIR`) is foundational and called out below.

## Decisions locked during brainstorming

- **Rotating** refresh tokens (OAuth 2.1 best practice for public clients): each
  refresh mints a new access token AND a new refresh token, invalidating the old.
- Refresh tokens are **long-lived / non-expiring by default**, but **revocable**.
  (Chosen over a literally-infinite *access* token, which would be an unrevocable
  internet-facing bearer.)
- **Reuse detection:** a consumed (already-rotated) refresh token presented again
  revokes the entire rotation **family** (replay/theft defense).
- **Configurable access-token TTL**, default **5 days** (down from 30): short enough
  that a leaked access token expires fast, while refresh rotation renews it
  seamlessly so the user never notices.
- Persisted to `state_dir`; everything self-contained in the `OAuthProvider` + the
  token/revoke endpoints. `verify_access_token` (bearer validation on MCP requests)
  is unchanged.

## Architecture

All changes are additive and backward-compatible. Existing access tokens keep
working; the `verbatim` master-token mode is untouched; clients that never refresh
still work (they re-auth once at access-token expiry).

| # | Component (`app/oauth.py` unless noted) | Responsibility |
|---|------|----------------|
| 1 | `RefreshToken` dataclass + `_refresh_tokens` store | `{token, client_id, scope, resource, family_id, created_at, expires_at\|None, consumed}` (`consumed` carries the persisted replay state); in-memory dict persisted to `state_dir` (`oauth_refresh_tokens.json`) and loaded on startup — mirrors the access-token store. |
| 2 | `issue_token_pair(...)` | The `authorization_code` grant returns **both** an access token (TTL) and a refresh token, linked by a `family_id`. |
| 3 | `grant_type=refresh_token` handler | Validate the presented refresh token → mint a new access + new refresh token in the same family, invalidate the old. Reused/consumed token → revoke the family, `invalid_grant`. |
| 4 | discovery | `grant_types_supported` gains `"refresh_token"`. |
| 5 | `/oauth/revoke` | Revoke refresh tokens too; revoking a refresh token drops the family's access tokens (RFC 7009). |
| 6 | config (`ProxySettings` + flags/env) | `MEM0_OAUTH_ACCESS_TOKEN_TTL` (default 5 days) and `MEM0_OAUTH_REFRESH_TOKEN_TTL` (default `0` = non-expiring). |

**Boundary:** the change lives entirely in the `OAuthProvider`'s stores and the
token/revoke endpoints. MCP bearer validation is unchanged.

## Data flows

### `authorization_code` grant (extended)

After today's checks (code, PKCE, redirect_uri, pinned-client), issue an access
token (TTL) + a refresh token (new `family_id`). Response gains `refresh_token`:

```json
{ "access_token": "...", "token_type": "Bearer", "expires_in": <ttl>,
  "refresh_token": "...", "scope": "mcp" }
```

`verbatim` mode unchanged (returns the master token, no refresh).

### `refresh_token` grant (new)

1. `grant_type=refresh_token` + `refresh_token=…` (+ `client_id` if `fixed_client_id`
   is pinned → must match).
2. Look up the token: must exist, not expired (if a refresh TTL is set), and not
   already-rotated/revoked. Carry the original `scope` + bound `resource`.
3. **Rotate:** mint a new access token + new refresh token in the same `family_id`;
   invalidate the presented refresh token.
4. **Reuse detection:** a consumed/rotated-away refresh token presented again →
   revoke the entire `family_id` (all its access + refresh tokens) and return
   `invalid_grant`.
5. Return the same shape as the `authorization_code` response (new access + refresh).

### `/oauth/revoke` (extended)

Accept either token type. Revoking a refresh token also drops the family's access
tokens. Revoking an access token works as today. Persist.

## Configuration

| Field | Env / flag | Default |
|---|---|---|
| `oauth_access_token_ttl` | `MEM0_OAUTH_ACCESS_TOKEN_TTL` / `--oauth-access-token-ttl` | `432000` (5 days) |
| `oauth_refresh_token_ttl` | `MEM0_OAUTH_REFRESH_TOKEN_TTL` / `--oauth-refresh-token-ttl` | `0` (non-expiring) |

Both thread into the `OAuthProvider` constructor. **`MEM0_STATE_DIR` must be set**
for tokens to survive restarts — documented in README/GUIDE as the foundational
fix for frequent sign-outs.

## Error handling

| Condition | Result |
|---|---|
| unknown / expired / reused refresh token | `invalid_grant` (reused → also revoke the family) |
| pinned-client mismatch on refresh | `invalid_client` |
| any other `grant_type` | `unsupported_grant_type` (unchanged) |
| revoke of unknown token | `200` (per RFC 7009 — revocation is idempotent) |

## Testing (mirror `tests/test_oauth.py`)

- `authorization_code` grant now returns a `refresh_token`.
- `refresh_token` grant → new access **and** new refresh; old refresh invalidated.
- Rotation: returned refresh differs; the old one is rejected (`invalid_grant`).
- Reuse detection: presenting a consumed refresh token revokes the family (the
  current refresh then also fails).
- Access token expires per configured TTL; refresh non-expiring by default, and
  expires when a TTL is set.
- `/oauth/revoke` on a refresh token drops the family's access tokens; on an access
  token works as before.
- Discovery advertises `refresh_token`.
- Persistence: refresh tokens survive a simulated restart with a shared `state_dir`
  (mirror the existing restart test).
- Back-compat: a pre-existing access token still validates; `verbatim` mode
  unchanged; a client that never refreshes still works.
- `python -m py_compile app/*.py` + full suite green.

## Out of scope (YAGNI)

Scope-narrowing on refresh (keep the original scope) · other grant types
(device-code, client-credentials) · JWT / self-contained tokens (stay opaque +
store) · multi-user / per-user token policies · a rotation on/off switch (rotation
is always on) · DPoP / sender-constrained tokens.

## Note on root cause

The single most important operational fix is **setting `MEM0_STATE_DIR`** so tokens
persist across restarts; refresh tokens + configurable TTL are the durable UX layer
on top. The spec ships docs that state this explicitly.
