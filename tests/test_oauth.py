"""Unit tests for the OAuth shim (pure stdlib, no proxy runtime needed).

Run with: python -m pytest mem0_import/tests/test_oauth.py
(or: python mem0_import/tests/test_oauth.py)
"""

from __future__ import annotations

import base64
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import json
import tempfile
import os

from oauth import OAuthProvider, scope_is_write  # noqa: E402
from persistence import JsonFileStore  # noqa: E402


def _pkce_pair() -> tuple[str, str]:
    verifier = "a" * 64
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _exchange(provider: OAuthProvider, *, redirect_uri="https://claude.ai/cb", client_id="client-1", resource=None):
    verifier, challenge = _pkce_pair()
    code_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "mcp",
    }
    if resource is not None:
        code_params["resource"] = resource
    code = provider.issue_code(code_params)
    return provider.exchange_code(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }
    )


def test_derived_token_is_not_the_master():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    response, error = _exchange(provider)
    assert error is None
    assert response["access_token"] != "MASTER"
    assert response["token_type"] == "Bearer"
    assert response["expires_in"] == int(provider.access_token_ttl)
    assert provider.verify_access_token(response["access_token"]) is True
    # the master is not implicitly a registered access token in the store
    assert provider.verify_access_token("MASTER") is False


def test_verbatim_mode_returns_master():
    provider = OAuthProvider(
        master_token="MASTER", mcp_resource_path="/claude/mcp", issue_verbatim_token=True
    )
    response, error = _exchange(provider)
    assert error is None
    assert response["access_token"] == "MASTER"
    assert "expires_in" not in response


def test_resource_scoped_token_only_valid_for_its_resource():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    response, _ = _exchange(provider, resource="https://host/openai/mcp")
    token = response["access_token"]
    assert provider.verify_access_token(token, resource="https://host/openai/mcp") is True
    assert provider.verify_access_token(token, resource="https://host/claude/mcp") is False
    # a resource-bound token must NOT validate when no resource is supplied
    assert provider.verify_access_token(token) is False


def test_valid_redirect_uri_rules():
    ok = OAuthProvider.valid_redirect_uri
    assert ok("https://chatgpt.com/callback") is True
    assert ok("http://localhost:1234/cb") is True
    assert ok("https://host/cb#frag") is False  # fragment not allowed
    assert ok("http://evil.example/cb") is False  # plain http only for loopback
    assert ok("not-a-uri") is False


def test_resourceless_token_accepted_for_any_resource():
    # back-compat: a client that doesn't send a resource indicator (e.g. Claude)
    # gets a token usable on any MCP resource.
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    response, _ = _exchange(provider)  # no resource
    token = response["access_token"]
    assert provider.verify_access_token(token, resource="https://host/claude/mcp") is True
    assert provider.verify_access_token(token, resource="https://host/openai/mcp") is True


def test_revocation():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    response, _ = _exchange(provider)
    token = response["access_token"]
    assert provider.verify_access_token(token) is True
    assert provider.revoke_access_token(token) is True
    assert provider.verify_access_token(token) is False
    assert provider.revoke_access_token(token) is False  # already gone


def test_expiry():
    provider = OAuthProvider(
        master_token="MASTER", mcp_resource_path="/claude/mcp", access_token_ttl=-1
    )
    response, _ = _exchange(provider)
    assert provider.verify_access_token(response["access_token"]) is False


def test_pkce_mismatch_rejected():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    _, challenge = _pkce_pair()
    code = provider.issue_code(
        {
            "client_id": "c",
            "redirect_uri": "https://claude.ai/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response, error = provider.exchange_code(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://claude.ai/cb",
            "client_id": "c",
            "code_verifier": "wrong-verifier",
        }
    )
    assert response is None
    assert error[0] == 400 and error[1] == "invalid_grant"


def test_authorization_code_single_use():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    verifier, challenge = _pkce_pair()
    code = provider.issue_code(
        {
            "client_id": "c",
            "redirect_uri": "https://claude.ai/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://claude.ai/cb",
        "client_id": "c",
        "code_verifier": verifier,
    }
    first, error = provider.exchange_code(dict(form))
    assert error is None and first is not None
    second, error2 = provider.exchange_code(dict(form))
    assert second is None and error2[1] == "invalid_grant"


def test_metadata_advertises_revocation_endpoint():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    meta = provider.authorization_server_metadata({"host": "mem0.example", "x-forwarded-proto": "https"})
    assert meta["revocation_endpoint"] == "https://mem0.example/oauth/revoke"


def test_token_survives_restart(tmp_path):
    """A token issued by one OAuthProvider instance is valid after a fresh instance loads the same store."""
    store_path = str(tmp_path / "tokens.json")
    store = JsonFileStore(store_path)

    provider1 = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", token_store=store)
    response, error = _exchange(provider1)
    assert error is None
    token = response["access_token"]
    assert provider1.verify_access_token(token) is True

    # Simulate a restart: brand-new instance backed by the same file
    provider2 = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", token_store=store)
    assert provider2.verify_access_token(token) is True


def test_revoke_removes_from_persisted_store(tmp_path):
    store_path = str(tmp_path / "tokens.json")
    store = JsonFileStore(store_path)

    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", token_store=store)
    response, _ = _exchange(provider)
    token = response["access_token"]
    assert provider.verify_access_token(token) is True

    provider.revoke_access_token(token)

    # Reload from file: token must be absent
    provider2 = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", token_store=store)
    assert provider2.verify_access_token(token) is False


def test_expired_token_dropped_on_load(tmp_path):
    store_path = str(tmp_path / "tokens.json")
    # Write an already-expired token directly into the JSON file
    expired_data = {
        "stale-token-xyz": {
            "client_id": "c1",
            "scope": "mcp",
            "expires_at": 1.0,  # epoch second in the past
            "resource": None,
        }
    }
    with open(store_path, "w") as fh:
        json.dump(expired_data, fh)

    store = JsonFileStore(store_path)
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", token_store=store)
    assert provider.verify_access_token("stale-token-xyz") is False


def test_scope_is_write():
    # Explicit read scope strings → False (not write)
    assert scope_is_write("read") is False
    assert scope_is_write("readonly") is False
    assert scope_is_write("read_only") is False
    assert scope_is_write("search") is False
    # Everything else → True (grants write; legacy 'mcp' = write)
    assert scope_is_write("write") is True
    assert scope_is_write("mcp") is True
    assert scope_is_write("") is True
    assert scope_is_write(None) is True
    # Case-insensitive
    assert scope_is_write("READ") is False
    assert scope_is_write("Read") is False


def test_access_token_scope_returns_scope():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    # Issue a token with scope 'read'
    token_read = provider.issue_access_token(client_id="c1", scope="read")
    assert provider.access_token_scope(token_read) == "read"
    # Issue a token with scope 'write'
    token_write = provider.issue_access_token(client_id="c1", scope="write")
    assert provider.access_token_scope(token_write) == "write"


def test_access_token_scope_expired_returns_none():
    provider = OAuthProvider(
        master_token="MASTER", mcp_resource_path="/claude/mcp", access_token_ttl=-1
    )
    token = provider.issue_access_token(client_id="c1", scope="read")
    assert provider.access_token_scope(token) is None


def test_access_token_scope_unknown_returns_none():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    assert provider.access_token_scope("not-a-real-token") is None
    assert provider.access_token_scope(None) is None


def test_access_token_scope_resource_bound():
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    token = provider.issue_access_token(client_id="c1", scope="read", resource="https://host/claude/mcp")
    # Correct resource → returns scope
    assert provider.access_token_scope(token, resource="https://host/claude/mcp") == "read"
    # Wrong resource → None
    assert provider.access_token_scope(token, resource="https://host/openai/mcp") is None
    # No resource supplied → None (bound token)
    assert provider.access_token_scope(token) is None


def test_authorization_code_grant_returns_refresh_token():
    """exchange_code on the authorization_code grant must include a refresh_token."""
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    response, error = _exchange(provider)
    assert error is None
    assert response["access_token"] != "MASTER"
    assert response["token_type"] == "Bearer"
    assert "expires_in" in response
    assert "refresh_token" in response
    assert response["refresh_token"]  # non-empty string


def test_metadata_advertises_refresh_token_grant():
    """authorization_server_metadata must list 'refresh_token' in grant_types_supported."""
    provider = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    meta = provider.authorization_server_metadata({"host": "mem0.example", "x-forwarded-proto": "https"})
    assert "refresh_token" in meta["grant_types_supported"]


def test_refresh_rotates_and_invalidates_old():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    _, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    resp, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is None and resp["access_token"] and resp["refresh_token"] != refresh
    again, err2 = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err2 is not None and err2[1] == "invalid_grant"  # rotated token rejected


def test_refresh_reuse_revokes_family():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    _, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    resp, _ = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    new_access, new_refresh = resp["access_token"], resp["refresh_token"]
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})  # replay consumed token
    assert err[1] == "invalid_grant"
    # family revoked: the rotated-forward tokens are dead too
    _, err2 = p.exchange_code({"grant_type": "refresh_token", "refresh_token": new_refresh})
    assert err2[1] == "invalid_grant"
    assert p.verify_access_token(new_access) is False


def test_unknown_refresh_token_invalid_grant():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": "nope"})
    assert err[1] == "invalid_grant"


def test_refresh_rejects_wrong_client_when_pinned():
    # With fixed_client_id set, a refresh carrying a mismatched client_id is rejected.
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", fixed_client_id="right")
    _, refresh = p.issue_token_pair(client_id="right", scope="mcp")
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "wrong"})
    assert err is not None and err[1] == "invalid_client"
    resp, ok_err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "right"})
    assert ok_err is None and resp["refresh_token"] != refresh  # correct client still rotates


def test_refresh_accepts_any_client_when_not_pinned():
    # No fixed_client_id => the client_id on a refresh request is not enforced.
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    _, refresh = p.issue_token_pair(client_id="right", scope="mcp")
    resp, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "anything"})
    assert err is None and resp["refresh_token"] != refresh


def test_refresh_expired_invalid_grant():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp", refresh_token_ttl=1)
    _, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    p._refresh_tokens[refresh].expires_at = time.time() - 1  # backdate past expiry
    _, err = p.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is not None and err[1] == "invalid_grant"


def test_revoke_refresh_kills_family_access():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    access, refresh = p.issue_token_pair(client_id="c", scope="mcp")
    assert p.verify_access_token(access) is True
    assert p.revoke_token(refresh) is True
    assert p.verify_access_token(access) is False  # RFC 7009: family access dropped


def test_revoke_access_token_still_works():
    p = OAuthProvider(master_token="MASTER", mcp_resource_path="/claude/mcp")
    access, _ = p.issue_token_pair(client_id="c", scope="mcp")
    assert p.revoke_token(access) is True
    assert p.verify_access_token(access) is False


def test_refresh_token_survives_restart(tmp_path):
    """A refresh token issued by one OAuthProvider survives a simulated restart
    and can still be used to rotate on the reloaded instance."""
    rt_store_path = str(tmp_path / "refresh_tokens.json")
    at_store_path = str(tmp_path / "access_tokens.json")
    rt_store = JsonFileStore(rt_store_path)
    at_store = JsonFileStore(at_store_path)

    p1 = OAuthProvider(
        master_token="MASTER", mcp_resource_path="/claude/mcp",
        token_store=at_store, refresh_token_store=rt_store,
    )
    _access, refresh = p1.issue_token_pair(client_id="c", scope="mcp")

    # Simulate a restart: fresh instance backed by the SAME store files
    p2 = OAuthProvider(
        master_token="MASTER", mcp_resource_path="/claude/mcp",
        token_store=at_store, refresh_token_store=rt_store,
    )
    resp, err = p2.exchange_code({"grant_type": "refresh_token", "refresh_token": refresh})
    assert err is None, f"expected rotation to succeed after restart but got {err}"
    assert resp["refresh_token"] != refresh  # token rotated as expected
    assert resp["access_token"]


def test_proxy_settings_access_token_ttl_default():
    """ProxySettings.oauth_access_token_ttl must default to 432000 (5 days)."""
    from server import ProxySettings
    assert ProxySettings().oauth_access_token_ttl == 432000
    assert ProxySettings().oauth_refresh_token_ttl == 0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
