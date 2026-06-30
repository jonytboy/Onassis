"""Etsy Open API v3 — OAuth 2.0 (Authorization Code grant with PKCE).

Etsy's v3 API authenticates with OAuth 2.0 using PKCE. The "keystring" issued
for an Etsy app is both the OAuth ``client_id`` and the ``x-api-key`` sent on
every request; the access token (``Authorization: Bearer``) is obtained through
the flow below and expires after one hour, so it is refreshed automatically
using the long-lived refresh token.

Because the flow is PKCE-based, the shared secret (``client_secret``) is **not**
transmitted for the token/refresh requests — the ``code_verifier`` proves
possession instead. We still accept the secret (read from the environment) for
completeness and any future confidential-client need.

This module owns three things:

* :class:`TokenStore` — secure, file-backed token persistence (0600, atomic
  writes, git-ignored) plus the short-lived PKCE "pending authorisation" state.
* PKCE helpers — :func:`generate_code_verifier` / :func:`code_challenge`.
* :class:`EtsyOAuth` — build the authorisation URL, exchange the code for
  tokens, refresh expired tokens, and hand out a valid access token on demand.

No credentials are hardcoded; everything comes from config/env. The HTTP call
and clock are injectable, so the whole flow is tested offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from onassis.logger import get_logger

log = get_logger(__name__)

# Type of the injectable HTTP poster: (url, form_data) -> parsed JSON dict.
HttpPost = Callable[[str, dict[str, Any]], dict[str, Any]]


class EtsyAuthError(RuntimeError):
    """Raised when authorisation is missing, invalid, or cannot be refreshed."""


# --- PKCE ------------------------------------------------------------

def generate_code_verifier(n_bytes: int = 64) -> str:
    """A high-entropy, URL-safe PKCE code verifier (43-128 chars)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    """The S256 code challenge for a verifier: base64url(sha256(verifier))."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# --- Token storage ---------------------------------------------------

class TokenStore:
    """Secure, file-backed storage for the OAuth token bundle + pending state.

    Tokens are written atomically with ``0600`` permissions inside a ``0700``
    directory, so they are never world-readable and never half-written. The
    file is git-ignored; nothing here is committed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # The pending PKCE state lives next to the token file.
        self.pending_path = self.path.with_name(self.path.stem + "_pending.json")

    # -- token bundle --
    def load(self) -> dict[str, Any] | None:
        return self._read(self.path)

    def save(self, tokens: dict[str, Any]) -> None:
        self._write(self.path, tokens)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    # -- pending PKCE state (between login and callback) --
    def save_pending(self, state: str, code_verifier: str) -> None:
        self._write(self.pending_path, {"state": state, "code_verifier": code_verifier})

    def load_pending(self) -> dict[str, Any] | None:
        return self._read(self.pending_path)

    def clear_pending(self) -> None:
        self.pending_path.unlink(missing_ok=True)

    # -- helpers --
    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass  # best-effort on platforms without POSIX perms
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Create the temp file with 0600 from the start (no readable window).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


# --- The OAuth client ------------------------------------------------

class EtsyOAuth:
    """Drives the Etsy v3 OAuth 2.0 PKCE flow and serves valid access tokens."""

    def __init__(
        self,
        *,
        client_id: str | None,
        redirect_uri: str | None,
        scopes: list[str],
        store: TokenStore,
        client_secret: str | None = None,
        connect_url: str = "https://www.etsy.com/oauth/connect",
        token_url: str = "https://api.etsy.com/v3/public/oauth/token",
        refresh_leeway: int = 120,
        http_post: HttpPost | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or []
        self.store = store
        self.connect_url = connect_url
        self.token_url = token_url
        self.refresh_leeway = int(refresh_leeway)
        self._http_post = http_post or self._default_post
        self._clock = clock

    # -- configuration --
    @property
    def is_configured(self) -> bool:
        """True when we have enough to *start* the flow (client id + redirect)."""
        return bool(self.client_id and self.redirect_uri)

    @property
    def is_authorised(self) -> bool:
        """True when a usable token bundle (with a refresh token) is stored."""
        tokens = self.store.load()
        return bool(tokens and tokens.get("refresh_token"))

    # -- step 1: authorisation URL --
    def create_authorization_url(self) -> dict[str, str]:
        """Build the consent URL and persist the PKCE state for the callback.

        Returns ``{url, state, code_verifier}``. Send the user to ``url``; Etsy
        redirects back to the registered redirect URI with ``?code=...&state=...``.
        """
        if not self.is_configured:
            raise EtsyAuthError(
                "Etsy OAuth not configured. Set ETSY_CLIENT_ID and ETSY_REDIRECT_URI."
            )
        verifier = generate_code_verifier()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        self.store.save_pending(state, verifier)
        url = f"{self.connect_url}?{urlencode(params)}"
        log.info("Generated Etsy authorisation URL (state=%s).", state)
        return {"url": url, "state": state, "code_verifier": verifier}

    # -- step 2: exchange the code for tokens --
    def exchange_code(
        self, code: str, *, state: str | None = None, code_verifier: str | None = None
    ) -> dict[str, Any]:
        """Swap an authorisation code for tokens and store them.

        ``code_verifier`` defaults to the one saved by
        :meth:`create_authorization_url`; ``state`` (if given) is checked
        against the saved value to defend against CSRF.
        """
        pending = self.store.load_pending() or {}
        verifier = code_verifier or pending.get("code_verifier")
        if not verifier:
            raise EtsyAuthError(
                "No PKCE code_verifier available. Start with create_authorization_url()."
            )
        if state is not None and pending.get("state") and state != pending["state"]:
            raise EtsyAuthError("OAuth state mismatch — possible CSRF; aborting.")

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code": code,
            "code_verifier": verifier,
        }
        tokens = self._request_tokens(payload)
        self.store.clear_pending()
        log.info("Etsy authorisation complete; tokens stored.")
        return tokens

    # -- step 3: refresh --
    def refresh(self) -> dict[str, Any]:
        """Use the stored refresh token to obtain a fresh access token."""
        tokens = self.store.load()
        if not tokens or not tokens.get("refresh_token"):
            raise EtsyAuthError("No refresh token stored — re-authorise Etsy.")
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": tokens["refresh_token"],
        }
        new_tokens = self._request_tokens(payload, fallback=tokens)
        log.info("Refreshed Etsy access token.")
        return new_tokens

    # -- the thing everyone calls --
    def valid_access_token(self) -> str:
        """Return a non-expired access token, refreshing transparently."""
        tokens = self.store.load()
        if not tokens:
            raise EtsyAuthError("Etsy is not authorised yet — run the OAuth flow.")
        if self._is_expired(tokens):
            tokens = self.refresh()
        token = tokens.get("access_token")
        if not token:
            raise EtsyAuthError("Stored Etsy token bundle has no access_token.")
        return token

    def status(self) -> dict[str, Any]:
        """A non-secret snapshot of the auth state (safe to expose/log)."""
        tokens = self.store.load() or {}
        expires_at = tokens.get("expires_at")
        return {
            "configured": self.is_configured,
            "authorised": self.is_authorised,
            "has_access_token": bool(tokens.get("access_token")),
            "expires_at": expires_at,
            "expires_in": (round(expires_at - self._clock()) if expires_at else None),
            "expired": self._is_expired(tokens) if tokens else None,
            "scopes": self.scopes,
        }

    # -- internals --
    def _is_expired(self, tokens: dict[str, Any]) -> bool:
        expires_at = tokens.get("expires_at")
        if not expires_at:
            return True
        return self._clock() >= (float(expires_at) - self.refresh_leeway)

    def _request_tokens(
        self, payload: dict[str, Any], fallback: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = self._http_post(self.token_url, payload)
        now = self._clock()
        # Etsy rotates the refresh token on refresh, but be defensive if it omits it.
        refresh_token = data.get("refresh_token") or (fallback or {}).get("refresh_token")
        bundle = {
            "access_token": data["access_token"],
            "refresh_token": refresh_token,
            "token_type": data.get("token_type", "Bearer"),
            "expires_in": data.get("expires_in"),
            "expires_at": now + float(data.get("expires_in", 3600)),
            "scope": data.get("scope") or (fallback or {}).get("scope"),
            "obtained_at": now,
        }
        self.store.save(bundle)
        return bundle

    def _default_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.post(url, data=payload, timeout=30.0)
        if resp.status_code >= 400:
            raise EtsyAuthError(
                f"Etsy token endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()


def build_etsy_oauth(config: Any, http_post: HttpPost | None = None) -> EtsyOAuth:
    """Construct an :class:`EtsyOAuth` from an ONASSIS config (no hardcoding)."""
    e = config.etsy or {}
    from onassis.config import ROOT_DIR

    store_path = Path(e.get("token_store", "data/etsy_tokens.json"))
    if not store_path.is_absolute():
        store_path = ROOT_DIR / store_path
    return EtsyOAuth(
        client_id=e.get("client_id") or e.get("api_key"),
        client_secret=e.get("client_secret"),
        redirect_uri=e.get("redirect_uri"),
        scopes=e.get("scopes") or [],
        store=TokenStore(store_path),
        connect_url=e.get("oauth_connect_url", "https://www.etsy.com/oauth/connect"),
        token_url=e.get("oauth_token_url", "https://api.etsy.com/v3/public/oauth/token"),
        refresh_leeway=int(e.get("token_refresh_leeway", 120) or 120),
        http_post=http_post,
    )
