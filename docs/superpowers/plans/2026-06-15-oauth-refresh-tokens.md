# OAuth Refresh Tokens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OAuth 2.1 rotating refresh tokens (long-lived/non-expiring, revocable, with reuse detection) and a configurable access-token TTL to the `app/oauth.py` shim, so connectors silently refresh instead of re-authorizing.

**Architecture:** All changes live in the `OAuthProvider` (stores + token/revoke endpoints) plus small `app/server.py` wiring (config + a second persisted store). The `authorization_code` grant now also returns a `refresh_token`; a new `grant_type=refresh_token` handler rotates the pair; revoke + discovery + persistence are extended. MCP bearer validation (`verify_access_token`) is unchanged; backward compatible.

**Tech Stack:** Python 3.12+, stdlib-only `app/oauth.py` (unit-tested via `tests/test_oauth.py` with no Mem0/Qdrant), pytest.

**Spec:** [`docs/superpowers/specs/2026-06-15-reliquary-oauth-refresh-tokens-design.md`](../specs/2026-06-15-reliquary-oauth-refresh-tokens-design.md)

---

## File Structure

**Modify:**
- `app/oauth.py` — `RefreshToken` dataclass; `family_id` on `AccessToken`; `__init__` gains `refresh_token_ttl` + `refresh_token_store`; `issue_token_pair`; `authorization_code` grant returns a refresh token; `_exchange_refresh_token` (rotation + reuse detection); `_revoke_family` + `revoke_token`; refresh persistence/prune/load; discovery advertises `refresh_token`.
- `app/server.py` — `ProxySettings` TTL fields + argparse + `build_settings`; second `JsonFileStore` (`oauth_refresh_tokens.json`); pass `access_token_ttl`/`refresh_token_ttl`/`refresh_token_store` to `OAuthProvider`; `handle_oauth_revoke` calls `revoke_token`.
- `tests/test_oauth.py` — extend.
- `README.md`, `docs/GUIDE.md` — document refresh tokens, new TTL envs, and the `MEM0_STATE_DIR` requirement.

**Detail level:** Phases 1–3 (the OAuth core + wiring) are full step-level TDD; Phase 4 is docs.

---

## Phase 1 — refresh-token model + issue on `authorization_code`

### Task 1.1: `RefreshToken` model + `AccessToken.family_id` + config

**Files:** Modify `app/oauth.py`; Test `tests/test_oauth.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_oauth.py`)

```python
def _provider(**kw):
    from oauth import OAuthProvider
    return OAuthProvider(master_token="master", mcp_resource_path="/claude/mcp", **kw)


def test_authorization_code_grant_returns_refresh_token():
    p = _provider()
    code = _issue_code(p)  # helper that runs the authorize+code path (see existing tests)
    resp, err = p.exchange_code({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": VERIFIER,
    })
    assert err is None
    assert resp["access_token"] and resp["refresh_token"]
    assert resp["token_type"] == "Bearer" and resp["expires_in"] > 0
```

> Reuse the existing `tests/test_oauth.py` helpers for the authorize→code flow (PKCE verifier/challenge, redirect_uri). If none exist as helpers, inline the same steps the current `exchange_code` happy-path test uses.

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/test_oauth.py -k refresh -q` → `KeyError: 'refresh_token'`.

- [ ] **Step 3: Implement model + config.** In `app/oauth.py`:

Add a module constant near the others:
```python
REFRESH_TOKEN_TTL = 0  # 0 = non-expiring
REFRESH_REUSE_GRACE = 24 * 3600  # keep a consumed refresh token this long to detect replay
```
Add `family_id` to `AccessToken`:
```python
@dataclass
class AccessToken:
    client_id: str
    scope: str
    expires_at: float
    resource: str | None = None
    family_id: str | None = None
```
Add the `RefreshToken` dataclass:
```python
@dataclass
class RefreshToken:
    client_id: str
    scope: str
    resource: str | None
    family_id: str
    created_at: float
    expires_at: float | None = None  # None = non-expiring
    consumed: bool = False
```
Extend `__init__` params + state:
```python
        access_token_ttl: float = ACCESS_TOKEN_TTL,
        refresh_token_ttl: float = REFRESH_TOKEN_TTL,
        token_store=None,
        refresh_token_store=None,
    ):
        ...
        self.access_token_ttl = access_token_ttl
        self.refresh_token_ttl = refresh_token_ttl
        self._codes: dict[str, AuthorizationCode] = {}
        self._token_store = token_store
        self._refresh_token_store = refresh_token_store
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        if self._token_store is not None:
            self._load_access_tokens()
        if self._refresh_token_store is not None:
            self._load_refresh_tokens()
```
Thread `family_id` through `issue_access_token`:
```python
    def issue_access_token(self, *, client_id: str, scope: str, resource: str | None = None, family_id: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self._access_tokens[token] = AccessToken(
            client_id=client_id, scope=scope,
            expires_at=time.time() + self.access_token_ttl,
            resource=(resource or "").strip() or None, family_id=family_id,
        )
        self._persist_access_tokens()
        return token
```
Update `_load_access_tokens` to read the new optional field: add `family_id=entry.get("family_id")` to the `AccessToken(...)` construction.

- [ ] **Step 4: Implement `issue_token_pair` + use it in the grant.** Add:
```python
    def _issue_refresh_token(self, *, client_id, scope, resource, family_id):
        token = secrets.token_urlsafe(32)
        expires_at = (time.time() + self.refresh_token_ttl) if self.refresh_token_ttl else None
        self._refresh_tokens[token] = RefreshToken(
            client_id=client_id, scope=scope, resource=(resource or "").strip() or None,
            family_id=family_id, created_at=time.time(), expires_at=expires_at,
        )
        self._persist_refresh_tokens()
        return token

    def issue_token_pair(self, *, client_id, scope, resource=None, family_id=None):
        family_id = family_id or secrets.token_urlsafe(12)
        access = self.issue_access_token(client_id=client_id, scope=scope, resource=resource, family_id=family_id)
        refresh = self._issue_refresh_token(client_id=client_id, scope=scope, resource=resource, family_id=family_id)
        return access, refresh
```
In `exchange_code`, replace the `authorization_code` success block (the `issue_access_token(...)` call + the returned dict) with:
```python
        access_token, refresh_token = self.issue_token_pair(
            client_id=entry.client_id, scope=scope, resource=entry.resource
        )
        return (
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": int(self.access_token_ttl),
                "refresh_token": refresh_token,
                "scope": scope,
            },
            None,
        )
```
(Leave the `issue_verbatim_token` early-return untouched — it has no refresh token.)

- [ ] **Step 5: Add refresh persistence/load.** Mirror the access-token methods:
```python
    def _persist_refresh_tokens(self) -> None:
        if self._refresh_token_store is not None:
            self._refresh_token_store.save({t: asdict(rt) for t, rt in self._refresh_tokens.items()})

    def _load_refresh_tokens(self) -> None:
        now = time.time()
        for token, entry in (self._refresh_token_store.load() if self._refresh_token_store else {}).items():
            try:
                rt = RefreshToken(
                    client_id=entry["client_id"], scope=entry["scope"], resource=entry.get("resource"),
                    family_id=entry["family_id"], created_at=float(entry["created_at"]),
                    expires_at=(float(entry["expires_at"]) if entry.get("expires_at") is not None else None),
                    consumed=bool(entry.get("consumed", False)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if rt.expires_at is None or rt.expires_at >= now:
                self._refresh_tokens[token] = rt
```

- [ ] **Step 6: Advertise in discovery.** In `authorization_server_metadata`, change `"grant_types_supported": ["authorization_code"]` → `["authorization_code", "refresh_token"]`.

- [ ] **Step 7: Run, verify pass** — `python -m pytest tests/test_oauth.py -q`.

- [ ] **Step 8: Commit** — `git commit -am "feat(#56): issue refresh tokens on the authorization_code grant" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Phase 2 — `refresh_token` grant: rotation + reuse detection

**Files:** Modify `app/oauth.py`; Test `tests/test_oauth.py`

- [ ] **Step 1: Write failing tests**
```python
def test_refresh_rotates_and_invalidates_old():
    p = _provider()
    _, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    resp, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is None and resp["access_token"] and resp["refresh_token"] != refresh
    # old refresh token no longer works (rotation)
    _, err2 = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err2 is not None and err2[1] == "invalid_grant"


def test_refresh_reuse_revokes_family():
    p = _provider()
    access, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    resp, _ = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    new_refresh = resp["refresh_token"]
    new_access = resp["access_token"]
    # replay the consumed token => family revoked
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err[1] == "invalid_grant"
    # the rotated-forward refresh AND access tokens are now dead too
    _, err2 = p.exchange_code({"grant_type": "refresh_token", "refresh_token": new_refresh})
    assert err2[1] == "invalid_grant"
    assert p.verify_access_token(new_access) is False


def test_unknown_refresh_token_invalid_grant():
    p = _provider()
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": "nope"})
    assert err[1] == "invalid_grant"
```

- [ ] **Step 2: Run, verify fail** (the refresh grant returns `unsupported_grant_type` today).

- [ ] **Step 3: Implement.** In `exchange_code`, at the very top (before the `grant_type != "authorization_code"` check):
```python
        if form.get("grant_type") == "refresh_token":
            return self._exchange_refresh_token(form)
```
Add the handler + family revocation + prune:
```python
    def _exchange_refresh_token(self, form):
        presented = (form.get("refresh_token") or "").strip()
        if not presented:
            return None, (400, "invalid_request", "refresh_token is required")
        if self.fixed_client_id is not None and not self.verify_client_id(form.get("client_id") or None):
            return None, (400, "invalid_client", "client_id does not match the configured OAuth client")
        self._prune_refresh_tokens()
        entry = self._refresh_tokens.get(presented)
        if entry is None or (entry.expires_at is not None and entry.expires_at < time.time()):
            return None, (400, "invalid_grant", "invalid or expired refresh token")
        if entry.consumed:
            self._revoke_family(entry.family_id)  # replay of a rotated token => theft
            return None, (400, "invalid_grant", "refresh token reuse detected; session revoked")
        entry.consumed = True
        access_token, refresh_token = self.issue_token_pair(
            client_id=entry.client_id, scope=entry.scope, resource=entry.resource, family_id=entry.family_id,
        )
        self._persist_refresh_tokens()
        return (
            {"access_token": access_token, "token_type": "Bearer",
             "expires_in": int(self.access_token_ttl), "refresh_token": refresh_token, "scope": entry.scope},
            None,
        )

    def _revoke_family(self, family_id: str) -> None:
        self._refresh_tokens = {t: e for t, e in self._refresh_tokens.items() if e.family_id != family_id}
        self._access_tokens = {t: e for t, e in self._access_tokens.items() if e.family_id != family_id}
        self._persist_refresh_tokens()
        self._persist_access_tokens()

    def _prune_refresh_tokens(self) -> None:
        now = time.time()
        drop = [
            t for t, e in self._refresh_tokens.items()
            if (e.expires_at is not None and e.expires_at < now)
            or (e.consumed and e.created_at + REFRESH_REUSE_GRACE < now)
        ]
        for t in drop:
            self._refresh_tokens.pop(t, None)
        if drop:
            self._persist_refresh_tokens()
```

- [ ] **Step 4: Run, verify pass** — `python -m pytest tests/test_oauth.py -q`.

- [ ] **Step 5: Commit** — `git commit -am "feat(#56): refresh_token grant with rotation + reuse detection" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Phase 3 — extend revoke + server wiring

### Task 3.1: `revoke_token` (access or refresh + family)

- [ ] **Step 1: Test**
```python
def test_revoke_refresh_kills_family_access():
    p = _provider()
    access, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    assert p.verify_access_token(access) is True
    p.revoke_token(refresh)
    assert p.verify_access_token(access) is False  # RFC 7009: family access dropped
```
- [ ] **Step 2: Run → fail** (`revoke_token` undefined).
- [ ] **Step 3: Implement** in `app/oauth.py`:
```python
    def revoke_token(self, token: str | None) -> bool:
        key = (token or "").strip()
        if not key:
            return False
        rt = self._refresh_tokens.get(key)
        if rt is not None:
            self._revoke_family(rt.family_id)  # drops the family's refresh + access tokens
            return True
        return self.revoke_access_token(key)
```
- [ ] **Step 4:** In `app/server.py` `handle_oauth_revoke`, change `self.oauth.revoke_access_token(form.get("token"))` → `self.oauth.revoke_token(form.get("token"))`.
- [ ] **Step 5: Run + commit** — `feat(#56): revoke refresh tokens (drops the rotation family)`.

### Task 3.2: server config + wiring

- [ ] **Step 1:** Add to `ProxySettings` (near `oauth_verbatim_token`):
```python
    oauth_access_token_ttl: int = 432000   # 5 days
    oauth_refresh_token_ttl: int = 0       # 0 = non-expiring
```
- [ ] **Step 2:** Add argparse (near `--oauth-verbatim-token`):
```python
    parser.add_argument("--oauth-access-token-ttl", type=int, default=int(os.getenv("MEM0_OAUTH_ACCESS_TOKEN_TTL", "432000")),
                        help="Access-token lifetime in seconds (default 5 days). Refresh rotation renews it.")
    parser.add_argument("--oauth-refresh-token-ttl", type=int, default=int(os.getenv("MEM0_OAUTH_REFRESH_TOKEN_TTL", "0")),
                        help="Refresh-token lifetime in seconds (0 = non-expiring).")
```
- [ ] **Step 3:** In `build_settings`, map both (`oauth_access_token_ttl=args.oauth_access_token_ttl`, `oauth_refresh_token_ttl=args.oauth_refresh_token_ttl`).
- [ ] **Step 4:** In `Mem0ChatProxy.__init__`, add the second store (where `token_store` is built from `state_dir`):
```python
            refresh_token_store = JsonFileStore(os.path.join(settings.state_dir, "oauth_refresh_tokens.json"))
```
(declare `refresh_token_store = None` alongside `token_store = None` before the `if settings.state_dir:` block). Then pass the new args to `OAuthProvider(...)`:
```python
            access_token_ttl=settings.oauth_access_token_ttl,
            refresh_token_ttl=settings.oauth_refresh_token_ttl,
            token_store=token_store,
            refresh_token_store=refresh_token_store,
```
- [ ] **Step 5: Test** (`tests/test_oauth.py` or a server test): refresh tokens persist across a simulated restart with a shared `state_dir` — mirror the existing access-token restart/persistence test (construct a provider with a tmp `JsonFileStore` for both stores, issue a pair, build a NEW provider over the same stores, confirm the refresh token still rotates). And a config test: `build_settings` defaults give `oauth_access_token_ttl == 432000`.
- [ ] **Step 6:** `python -m pytest -q` (full suite green) + `python -m py_compile app/*.py`. Commit — `feat(#56): wire configurable OAuth TTLs + refresh-token persistence`.

---

## Phase 4 — docs + final

- [ ] **Task 4.1 README:** in the OAuth/security section, note that connectors now get a **refresh token** (silent renewal; 5-day access tokens by default), the new `MEM0_OAUTH_ACCESS_TOKEN_TTL` / `MEM0_OAUTH_REFRESH_TOKEN_TTL` envs, and — prominently — that **`MEM0_STATE_DIR` must be set** for tokens to survive restarts (the real fix for frequent sign-outs). Update the "OAuth tokens are derived & revocable (… 30-day expiry)" line to reflect 5-day access + non-expiring refresh.
- [ ] **Task 4.2 GUIDE / `.env.example`:** document the two new env vars + the state-dir requirement.
- [ ] **Task 4.3 final:** `python -m py_compile app/*.py`; `python -m pytest -q`; commit `docs(#56): document refresh tokens + state-dir requirement`.

---

## Self-review (against spec)

**Spec coverage:** rotating refresh tokens → Phase 1 (issue) + Phase 2 (rotate); non-expiring-but-revocable → `refresh_token_ttl=0` default + `revoke_token`/`_revoke_family`; reuse detection (family revocation) → Phase 2 `consumed` + `_revoke_family`; configurable access TTL (5-day default) → Phase 3 config; persistence → Phase 1/3 stores + `MEM0_STATE_DIR`; discovery → Phase 1 Step 6; revoke → Phase 3.1; back-compat (existing access tokens, verbatim) → `family_id` optional + verbatim path untouched; docs → Phase 4.

**Type consistency:** `RefreshToken`/`AccessToken.family_id`/`issue_token_pair`/`_exchange_refresh_token`/`_revoke_family`/`revoke_token`/`_persist_refresh_tokens`/`_load_refresh_tokens` are used consistently; `exchange_code` returns the same `(dict, error)` tuple shape throughout; settings names match server wiring.

**Placeholder scan:** complete code in every step of Phases 1–3; Phase 4 is docs prose. No vague TODOs.
