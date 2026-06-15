from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode, urlparse


AUTHORIZATION_CODE_TTL = 600
ACCESS_TOKEN_TTL = 30 * 24 * 3600  # 30 days
REFRESH_TOKEN_TTL = 0  # 0 = non-expiring
REFRESH_REUSE_GRACE = 24 * 3600  # keep a consumed refresh token this long to detect replay

READ_SCOPES = {"read", "readonly", "read_only", "search"}


def scope_is_write(scope: str | None) -> bool:
    """Everything except an explicit read scope grants write (legacy 'mcp' = write)."""
    return str(scope or "").strip().lower() not in READ_SCOPES


@dataclass
class AuthorizationCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    expires_at: float
    scope: str | None
    resource: str | None


@dataclass
class AccessToken:
    client_id: str
    scope: str
    expires_at: float
    resource: str | None = None
    family_id: str | None = None


@dataclass
class RefreshToken:
    client_id: str
    scope: str
    resource: str | None
    family_id: str
    created_at: float
    expires_at: float | None = None  # None = non-expiring
    consumed: bool = False


class OAuthProvider:
    """Minimal OAuth 2.1 shim.

    By default it mints a derived, revocable access token per authorization and
    keeps it in an in-memory store, so individual clients can be expired or
    revoked without touching the master bearer. Set ``issue_verbatim_token`` to
    fall back to the old behavior (return the master bearer as the access token)
    for the simple single-user case.

    Optional hardening:
    - ``fixed_client_id``: if set, only this client_id is ever valid. DCR returns
      it verbatim (or refuses, depending on ``allow_registration``).
    - ``allow_registration``: when ``False``, /register returns 403 so a caller
      must already know ``fixed_client_id`` out-of-band. Use after the legitimate
      client (Claude.ai) has registered once and its stored client_id matches.
    """

    def __init__(
        self,
        *,
        master_token: str,
        mcp_resource_path: str,
        fixed_client_id: str | None = None,
        allow_registration: bool = True,
        issue_verbatim_token: bool = False,
        access_token_ttl: float = ACCESS_TOKEN_TTL,
        refresh_token_ttl: float = REFRESH_TOKEN_TTL,
        token_store=None,
        refresh_token_store=None,
    ):
        self.master_token = master_token
        self.mcp_resource_path = mcp_resource_path
        self.fixed_client_id = (fixed_client_id or "").strip() or None
        self.allow_registration = allow_registration
        self.issue_verbatim_token = issue_verbatim_token
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

    def base_url(self, headers: dict[str, str]) -> str:
        host = headers.get("host") or "localhost"
        forwarded_proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        scheme = forwarded_proto or "http"
        return f"{scheme}://{host}"

    def protected_resource_metadata(self, headers: dict[str, str], resource_path: str | None = None) -> dict[str, Any]:
        base = self.base_url(headers)
        path = resource_path or self.mcp_resource_path
        return {
            "resource": f"{base}{path}",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["read", "write"],
        }

    def authorization_server_metadata(self, headers: dict[str, str]) -> dict[str, Any]:
        base = self.base_url(headers)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["read", "write"],
        }

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.allow_registration:
            raise RegistrationDisabledError("Dynamic client registration is disabled")

        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ValueError("redirect_uris is required")
        for uri in redirect_uris:
            if not isinstance(uri, str) or not uri or not self.valid_redirect_uri(uri):
                raise ValueError(f"invalid redirect_uri: {uri!r}")

        client_id = self.fixed_client_id or f"mcp-{secrets.token_urlsafe(8)}"
        response = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "grant_types": payload.get("grant_types") or ["authorization_code"],
            "response_types": payload.get("response_types") or ["code"],
            "token_endpoint_auth_method": "none",
        }
        name = payload.get("client_name")
        if isinstance(name, str) and name:
            response["client_name"] = name
        return response

    @staticmethod
    def valid_redirect_uri(uri: str) -> bool:
        try:
            parsed = urlparse(uri)
        except ValueError:
            return False
        # No fragments in a redirect URI (RFC 6749 §3.1.2).
        if parsed.fragment:
            return False
        if parsed.scheme == "https":
            return bool(parsed.hostname)
        # Plain http only for loopback (native/dev clients).
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    def authorize_form_html(self, params: dict[str, str], error: str | None = None) -> str:
        hidden = "".join(
            f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
            for name, value in params.items()
        )
        error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
        redirect_host = html.escape(self._host_from_uri(params.get("redirect_uri", "")))
        return (
            "<!doctype html>\n"
            '<html lang="en"><head><meta charset="utf-8">'
            "<title>mem0 authorization</title>"
            "<style>"
            "body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}"
            ".error{color:#b00;font-weight:600}"
            ".host{font-family:ui-monospace,monospace;background:#f3f3f3;padding:.1rem .3rem;border-radius:.2rem}"
            "input[type=password]{width:100%;padding:.6rem;font-family:ui-monospace,monospace;box-sizing:border-box;border:1px solid #ccc;border-radius:.3rem}"
            "button{padding:.6rem 1.2rem;margin-top:.75rem;border:0;border-radius:.3rem;background:#222;color:#fff;cursor:pointer}"
            "label{display:block;margin-top:1rem}"
            "</style></head><body>"
            "<h1>mem0</h1>"
            f'<p>Authorize <span class="host">{redirect_host}</span> to access your memories.</p>'
            f"{error_html}"
            '<form method="post" action="/oauth/authorize">'
            f"{hidden}"
            '<label>Bearer token'
            '<input type="password" name="bearer_token" autocomplete="off" required autofocus>'
            "</label>"
            '<button type="submit">Authorize</button>'
            "</form></body></html>\n"
        )

    @staticmethod
    def _host_from_uri(uri: str) -> str:
        if not uri:
            return "<unknown>"
        try:
            parsed = urlparse(uri)
        except ValueError:
            return "<unknown>"
        return parsed.netloc or "<unknown>"

    def verify_bearer(self, candidate: str) -> bool:
        return secrets.compare_digest(candidate.strip(), self.master_token)

    def verify_client_id(self, candidate: str | None) -> bool:
        if self.fixed_client_id is None:
            return bool(candidate)
        if not candidate:
            return False
        return secrets.compare_digest(candidate, self.fixed_client_id)

    def issue_code(self, params: dict[str, str]) -> str:
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            client_id=params.get("client_id", ""),
            redirect_uri=params["redirect_uri"],
            code_challenge=params.get("code_challenge", ""),
            code_challenge_method=params.get("code_challenge_method", "S256"),
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
            scope=params.get("scope"),
            resource=params.get("resource"),
        )
        return code

    @staticmethod
    def build_redirect(redirect_uri: str, query: dict[str, str]) -> str:
        separator = "&" if "?" in redirect_uri else "?"
        return redirect_uri + separator + urlencode(query)

    def exchange_code(
        self, form: dict[str, str]
    ) -> tuple[dict[str, Any] | None, tuple[int, str, str] | None]:
        self._prune()
        grant_type = form.get("grant_type")
        if grant_type != "authorization_code":
            return None, (400, "unsupported_grant_type", f"grant_type {grant_type!r} is not supported")
        code = form.get("code") or ""
        entry = self._codes.pop(code, None)
        if entry is None:
            return None, (400, "invalid_grant", "authorization code is invalid or already used")
        if entry.expires_at < time.time():
            return None, (400, "invalid_grant", "authorization code expired")
        redirect_uri = form.get("redirect_uri") or ""
        if redirect_uri != entry.redirect_uri:
            return None, (400, "invalid_grant", "redirect_uri does not match authorization request")
        if self.fixed_client_id is not None:
            if not self.verify_client_id(form.get("client_id") or entry.client_id or None):
                return None, (400, "invalid_client", "client_id does not match the configured OAuth client")
        code_verifier = form.get("code_verifier") or ""
        if not self._verify_pkce(code_verifier, entry.code_challenge, entry.code_challenge_method):
            return None, (400, "invalid_grant", "PKCE verification failed")

        scope = entry.scope or "mcp"
        if self.issue_verbatim_token:
            return (
                {"access_token": self.master_token, "token_type": "Bearer", "scope": scope},
                None,
            )
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

    def issue_access_token(self, *, client_id: str, scope: str, resource: str | None = None, family_id: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self._access_tokens[token] = AccessToken(
            client_id=client_id,
            scope=scope,
            expires_at=time.time() + self.access_token_ttl,
            resource=(resource or "").strip() or None,
            family_id=family_id,
        )
        self._persist_access_tokens()
        return token

    def verify_access_token(self, candidate: str | None, *, resource: str | None = None) -> bool:
        """Validate a derived token. When the token was bound to a specific
        resource (RFC 8707 indicator), it is only accepted for that resource;
        tokens issued without a resource indicator are accepted for any MCP
        resource (back-compat with clients that don't send `resource`)."""
        if not candidate:
            return False
        key = candidate.strip()
        self._prune_access_tokens()
        entry = self._access_tokens.get(key)
        if entry is None:
            return False
        if entry.expires_at < time.time():  # guards the prune/check race window
            self._access_tokens.pop(key, None)
            self._persist_access_tokens()
            return False
        # A token bound to a resource is only valid for that resource; the caller
        # must present a matching one (tokens issued without a resource indicator
        # stay usable for any MCP resource).
        if entry.resource:
            if not resource or entry.resource != resource:
                return False
        return True

    def access_token_scope(self, candidate: str | None, *, resource: str | None = None) -> str | None:
        """Return the scope of a valid access token, or None if invalid/expired/unknown."""
        if not candidate:
            return None
        key = candidate.strip()
        self._prune_access_tokens()
        entry = self._access_tokens.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.time():
            self._access_tokens.pop(key, None)
            self._persist_access_tokens()
            return None
        if entry.resource:
            if not resource or entry.resource != resource:
                return None
        return entry.scope or "mcp"

    def revoke_access_token(self, token: str | None) -> bool:
        removed = self._access_tokens.pop((token or "").strip(), None) is not None
        if removed:
            self._persist_access_tokens()
        return removed

    def _prune(self) -> None:
        now = time.time()
        expired = [code for code, entry in self._codes.items() if entry.expires_at < now]
        for code in expired:
            self._codes.pop(code, None)

    def _prune_access_tokens(self) -> None:
        now = time.time()
        expired = [token for token, entry in self._access_tokens.items() if entry.expires_at < now]
        for token in expired:
            self._access_tokens.pop(token, None)
        # verify_access_token() prunes on every check; only touch disk when
        # something actually expired, so the common path stays write-free.
        if expired:
            self._persist_access_tokens()

    def _load_access_tokens(self) -> None:
        now = time.time()
        for token, entry in (self._token_store.load() if self._token_store else {}).items():
            try:
                at = AccessToken(
                    client_id=entry["client_id"],
                    scope=entry["scope"],
                    expires_at=float(entry["expires_at"]),
                    resource=entry.get("resource"),
                    family_id=entry.get("family_id"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if at.expires_at >= now:
                self._access_tokens[token] = at

    def _persist_access_tokens(self) -> None:
        if self._token_store is not None:
            self._token_store.save({token: asdict(at) for token, at in self._access_tokens.items()})

    def issue_token_pair(self, *, client_id: str, scope: str, resource: str | None = None, family_id: str | None = None) -> tuple[str, str]:
        family_id = family_id or secrets.token_urlsafe(12)
        access = self.issue_access_token(client_id=client_id, scope=scope, resource=resource, family_id=family_id)
        refresh = self._issue_refresh_token(client_id=client_id, scope=scope, resource=resource, family_id=family_id)
        return access, refresh

    def _issue_refresh_token(self, *, client_id: str, scope: str, resource: str | None, family_id: str) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = (time.time() + self.refresh_token_ttl) if self.refresh_token_ttl else None
        self._refresh_tokens[token] = RefreshToken(
            client_id=client_id,
            scope=scope,
            resource=(resource or "").strip() or None,
            family_id=family_id,
            created_at=time.time(),
            expires_at=expires_at,
        )
        self._persist_refresh_tokens()
        return token

    def _persist_refresh_tokens(self) -> None:
        if self._refresh_token_store is not None:
            self._refresh_token_store.save({t: asdict(rt) for t, rt in self._refresh_tokens.items()})

    def _load_refresh_tokens(self) -> None:
        now = time.time()
        for token, entry in (self._refresh_token_store.load() if self._refresh_token_store else {}).items():
            try:
                rt = RefreshToken(
                    client_id=entry["client_id"],
                    scope=entry["scope"],
                    resource=entry.get("resource"),
                    family_id=entry["family_id"],
                    created_at=float(entry["created_at"]),
                    expires_at=(float(entry["expires_at"]) if entry.get("expires_at") is not None else None),
                    consumed=bool(entry.get("consumed", False)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if rt.expires_at is None or rt.expires_at >= now:
                self._refresh_tokens[token] = rt

    @staticmethod
    def _verify_pkce(verifier: str, challenge: str, method: str) -> bool:
        if not challenge or not verifier:
            return False
        method = (method or "S256").upper()
        if method == "S256":
            try:
                encoded = verifier.encode("ascii")
            except UnicodeEncodeError:
                return False  # untrusted input; treat as PKCE failure, not a 500
            digest = hashlib.sha256(encoded).digest()
            computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            return secrets.compare_digest(computed, challenge)
        if method == "PLAIN":
            return secrets.compare_digest(verifier, challenge)
        return False


class RegistrationDisabledError(RuntimeError):
    pass
