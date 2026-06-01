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

from oauth import OAuthProvider  # noqa: E402


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
