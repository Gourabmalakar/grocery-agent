"""
OAuth 2.1 for MCP servers: discovery, dynamic client registration, the
authorization-code flow with PKCE, refresh, and an at-rest token store.

THE POINT OF THIS MODULE IS THE SCOPE, NOT THE PLUMBING.

Zepto's MCP publishes three scopes at
https://mcp.zepto.co.in/.well-known/oauth-protected-resource:

    tools:read   tools:write   dev.ucp.shopping.cart:manage

and its own README says the integration "is not a sandbox or demo
environment. Any order placed through the Zepto MCP will be processed as a
real Zepto order." So a token carrying tools:write is a token that can spend
Gourab's money, and this repo exists to make that structurally impossible
rather than merely unlikely.

Therefore: we request tools:read and nothing else, and `store()` REFUSES a
grant that comes back with anything more (see _assert_read_only). That is a
stronger guarantee than an application-level allowlist, because it is
enforced by the merchant's own authorization server rather than by our code
remembering to check. The allowlist in the adapter stays anyway — two
independent mechanisms, because the whole architecture here is built on not
trusting any single one.

AT REST. The token is written to VAULT_DIR/<name>.json at chmod 0600,
matching how grocery/mandates/signing.py already stores the mandate signing
key. It is deliberately NOT encrypted under the passphrase: that would tie
every price lookup to a live 30-minute console window and break unattended
worker runs, to protect against an attacker who — having filesystem read
access — would already hold the mandate signing key. The read-only scope is
what makes this acceptable: the worst a stolen token can do is read prices.

grocery/killswitch.py's BURN tier already deletes VAULT_DIR/*, so revocation
works with no change there. Zepto-side revocation is manual and belongs in
the BURN checklist.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import requests

from grocery import config

_TIMEOUT_SECONDS = 30
_REFRESH_SKEW_SECONDS = 120  # refresh a little early rather than racing expiry


class OAuthError(RuntimeError):
    pass


class ScopeTooBroad(OAuthError):
    """The authorization server granted more than we asked for. We refuse to
    persist such a token rather than hold write authority 'just in case'."""


# ---- discovery ---------------------------------------------------------

def discover_resource(resource_url: str) -> dict[str, Any]:
    url = resource_url.rstrip("/") + "/.well-known/oauth-protected-resource"
    return _get_json(url)


def discover_auth_server(issuer: str) -> dict[str, Any]:
    base = issuer.rstrip("/")
    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            return _get_json(base + suffix)
        except OAuthError:
            continue
    raise OAuthError(f"no authorization-server metadata found at {issuer}")


def _get_json(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise OAuthError(f"could not reach {url}: {exc}") from exc
    if response.status_code != 200:
        raise OAuthError(f"HTTP {response.status_code} from {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise OAuthError(f"non-JSON metadata at {url}") from exc


# ---- dynamic client registration (RFC 7591) ----------------------------

def register_client(registration_endpoint: str, *, redirect_uri: str, client_name: str) -> dict[str, Any]:
    """Registers a public client. token_endpoint_auth_method is "none"
    because this is a native app with no way to keep a client secret — which
    is exactly why PKCE is mandatory below."""
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    try:
        response = requests.post(registration_endpoint, json=body, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise OAuthError(f"registration failed: {exc}") from exc
    if response.status_code not in (200, 201):
        raise OAuthError(f"registration rejected (HTTP {response.status_code}): {response.text[:300]}")
    return response.json()


# ---- PKCE authorization-code flow --------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


@dataclass
class PendingAuth:
    authorize_url: str
    verifier: str
    state: str
    redirect_uri: str
    client_id: str
    token_endpoint: str
    scope: str


def begin_authorization(
    *,
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    resource: str | None = None,
) -> PendingAuth:
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        # RFC 8707 — binds the token to this MCP server rather than letting
        # it be replayed against another resource on the same AS.
        params["resource"] = resource
    return PendingAuth(
        authorize_url=f"{authorization_endpoint}?{urlencode(params)}",
        verifier=verifier,
        state=state,
        redirect_uri=redirect_uri,
        client_id=client_id,
        token_endpoint=token_endpoint,
        scope=scope,
    )


def exchange_code(pending: PendingAuth, code: str, *, resource: str | None = None) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": pending.client_id,
        "code_verifier": pending.verifier,
    }
    if resource:
        data["resource"] = resource
    return _token_request(pending.token_endpoint, data)


def refresh_token(token_endpoint: str, *, refresh_tok: str, client_id: str,
                  scope: str | None = None, resource: str | None = None) -> dict[str, Any]:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }
    if scope:
        data["scope"] = scope
    if resource:
        data["resource"] = resource
    return _token_request(token_endpoint, data)


def _token_request(token_endpoint: str, data: dict[str, str]) -> dict[str, Any]:
    try:
        response = requests.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise OAuthError(f"token request failed: {exc}") from exc
    if response.status_code != 200:
        raise OAuthError(f"token endpoint returned HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


# ---- token store -------------------------------------------------------

@dataclass
class StoredToken:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    client_id: str = ""
    token_endpoint: str = ""
    resource: str = ""
    obtained_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False  # no expiry advertised; let a 401 tell us instead
        return time.time() >= (self.expires_at - _REFRESH_SKEW_SECONDS)

    def granted_scopes(self) -> set[str]:
        return {s for s in self.scope.replace(",", " ").split() if s}

    def redacted(self) -> dict[str, Any]:
        """Safe to log or render. Never put access_token in an audit entry,
        a decision record, or a conversation turn."""
        return {
            "scope": self.scope,
            "expires_at": self.expires_at,
            "obtained_at": self.obtained_at,
            "resource": self.resource,
            "has_refresh_token": bool(self.refresh_token),
        }


def _path(name: str):
    return config.VAULT_DIR / f"{name}.json"


def _assert_read_only(scopes: set[str], *, allowed: set[str]) -> None:
    extra = scopes - allowed
    if extra:
        raise ScopeTooBroad(
            f"authorization server granted {sorted(extra)} beyond the requested "
            f"{sorted(allowed)}. Refusing to store a token that can write. "
            "Revoke it in the merchant's app and re-connect."
        )


def store(name: str, token: StoredToken, *, allowed_scopes: set[str]) -> None:
    # A grant with NO scope string is not evidence of a narrow grant — it is
    # an absence of evidence, and this repo's rule is that an unsourced fact
    # is worse than a missing one. Refuse it.
    granted = token.granted_scopes()
    if not granted:
        raise ScopeTooBroad(
            "authorization server returned no scope on the grant, so we cannot "
            "prove it is read-only. Refusing to store it."
        )
    _assert_read_only(granted, allowed=allowed_scopes)
    path = _path(name)
    path.write_text(json.dumps(token.__dict__, indent=2, sort_keys=True))
    path.chmod(0o600)


def load(name: str) -> StoredToken | None:
    path = _path(name)
    if not path.exists():
        return None
    try:
        return StoredToken(**json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def forget(name: str) -> None:
    _path(name).unlink(missing_ok=True)
