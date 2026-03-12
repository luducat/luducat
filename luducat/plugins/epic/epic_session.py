# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# epic_session.py

# Portions adapted from Legendary (https://github.com/derrod/legendary)
# Copyright (c) Rodney and Legendary contributors
# Licensed under GPLv3+
"""Epic Games OAuth session manager.

Handles credential vending (proxy or BYOK), OAuth token exchange, refresh,
and revocation. All auth traffic goes directly to Epic — only the initial
client_id/client_secret fetch touches the proxy.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from luducat.plugins.sdk.json import json

logger = logging.getLogger(__name__)

# Epic OAuth endpoints
_OAUTH_HOST = "https://account-public-service-prod03.ol.epicgames.com"
_OAUTH_TOKEN_URL = f"{_OAUTH_HOST}/account/api/oauth/token"
_OAUTH_VERIFY_URL = f"{_OAUTH_HOST}/account/api/oauth/verify"

# 5-minute buffer before token expiry
_EXPIRY_BUFFER = 300


class EpicSession:
    """OAuth session manager for Epic Games Store.

    Credential flow:
        1. Check keyring for client_id/client_secret (BYOK or previously cached)
        2. If missing: fetch from proxy (signed request), cache in keyring
        3. User authenticates via browser → authorization code
        4. Exchange code for tokens at Epic's OAuth endpoint (direct)
        5. Refresh tokens before expiry (direct)

    Args:
        get_credential: Callback to read from keyring (key → value|None)
        set_credential: Callback to write to keyring (key, value)
        delete_credential: Callback to delete from keyring (key)
        get_setting: Callback to read plugin setting (key, default → value)
        http_client: PluginHttpClient for HTTP requests
    """

    def __init__(
        self,
        get_credential: Callable[[str], Optional[str]],
        set_credential: Callable[[str, str], None],
        delete_credential: Callable[[str], None],
        get_setting: Callable[..., Any],
        http_client: Any,
    ):
        self._get_credential = get_credential
        self._set_credential = set_credential
        self._delete_credential = delete_credential
        self._get_setting = get_setting
        self._http = http_client

        # In-memory session cache (loaded from keyring on first access)
        self._session: Optional[Dict[str, Any]] = None
        self._session_loaded = False

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def has_session(self) -> bool:
        """Whether an OAuth session exists (may be expired)."""
        session = self._load_session()
        return session is not None and bool(session.get("access_token"))

    @property
    def access_token(self) -> Optional[str]:
        """Current access token, or None if not authenticated."""
        session = self._load_session()
        return session.get("access_token") if session else None

    @property
    def account_id(self) -> Optional[str]:
        """Authenticated account ID."""
        session = self._load_session()
        return session.get("account_id") if session else None

    @property
    def display_name(self) -> Optional[str]:
        """Authenticated display name."""
        session = self._load_session()
        return session.get("display_name") if session else None

    # ── Public Methods ──────────────────────────────────────────────────

    def ensure_valid(self) -> str:
        """Ensure we have a valid (non-expired) access token.

        Auto-refreshes if token is within the expiry buffer.

        Returns:
            Valid access token

        Raises:
            RuntimeError: If not authenticated or refresh fails
        """
        session = self._load_session()
        if not session or not session.get("access_token"):
            raise RuntimeError("Not authenticated — no Epic session")

        expires_at = session.get("expires_at", 0)
        if time.time() >= expires_at - _EXPIRY_BUFFER:
            refresh_token = session.get("refresh_token")
            if not refresh_token:
                raise RuntimeError(
                    "Epic session expired and no refresh token available"
                )
            logger.debug("Epic access token near expiry, refreshing")
            self.refresh(refresh_token)
            session = self._load_session()

        return session["access_token"]

    def exchange_authorization_code(self, code: str) -> Dict[str, Any]:
        """Exchange an authorization code for OAuth tokens.

        Args:
            code: Authorization code from Epic login redirect

        Returns:
            Session dict with access_token, refresh_token, account_id, etc.

        Raises:
            RuntimeError: If exchange fails
        """
        client_id, client_secret = self._ensure_credentials()

        logger.info("Exchanging Epic authorization code for tokens")
        response = self._http.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

        if response.status_code != 200:
            error_msg = self._parse_error(response)
            raise RuntimeError(f"Epic auth code exchange failed: {error_msg}")

        data = response.json()
        self._store_session(data)
        logger.info(
            "Epic authentication successful: %s",
            data.get("displayName", "unknown"),
        )
        return self._load_session()

    def refresh(self, refresh_token: Optional[str] = None) -> Dict[str, Any]:
        """Refresh the OAuth session using a refresh token.

        Args:
            refresh_token: Override refresh token (uses stored if None)

        Returns:
            Updated session dict

        Raises:
            RuntimeError: If refresh fails
        """
        if refresh_token is None:
            session = self._load_session()
            refresh_token = session.get("refresh_token") if session else None
        if not refresh_token:
            raise RuntimeError("No refresh token available")

        client_id, client_secret = self._ensure_credentials()

        logger.debug("Refreshing Epic OAuth token")
        response = self._http.post(
            _OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

        if response.status_code != 200:
            error_msg = self._parse_error(response)
            logger.warning("Epic token refresh failed: %s", error_msg)
            # Clear invalid session
            self.clear()
            raise RuntimeError(f"Epic token refresh failed: {error_msg}")

        data = response.json()
        self._store_session(data)
        logger.debug("Epic token refreshed successfully")
        return self._load_session()

    def revoke(self) -> None:
        """Revoke the current session (logout).

        Calls Epic's session kill endpoint, then clears local state.
        """
        session = self._load_session()
        if not session or not session.get("access_token"):
            self.clear()
            return

        access_token = session["access_token"]
        try:
            self._http.delete(
                f"{_OAUTH_HOST}/account/api/oauth/sessions/kill/{access_token}",
                headers={
                    "Authorization": f"bearer {access_token}",
                },
                timeout=10,
            )
            logger.info("Epic session revoked")
        except Exception as e:
            logger.warning("Failed to revoke Epic session: %s", e)

        self.clear()

    def clear(self) -> None:
        """Delete session from keyring and clear in-memory state."""
        try:
            self._delete_credential("oauth_session")
        except Exception:
            pass
        self._session = None
        self._session_loaded = False
        logger.debug("Epic session cleared")

    def fetch_credentials(self) -> tuple:
        """Fetch client_id/secret from proxy (one-time, cached).

        Returns:
            Tuple of (client_id, client_secret)

        Raises:
            RuntimeError: If proxy request fails
        """
        from luducat.plugins.sdk.proxy import build_route_headers, get_proxy_url

        url = f"{get_proxy_url()}/epic/credentials"
        headers = build_route_headers("epic", "epic/credentials", "")

        logger.debug("Fetching Epic credentials from proxy")
        response = self._http.get(url, headers=headers, timeout=15)

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to fetch Epic credentials from proxy "
                f"(HTTP {response.status_code})"
            )

        data = response.json()
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")

        if not client_id or not client_secret:
            raise RuntimeError(
                "Proxy returned incomplete Epic credentials"
            )

        # Cache in keyring for future use
        self._set_credential("epic_client_id", client_id)
        self._set_credential("epic_client_secret", client_secret)
        logger.info("Epic credentials fetched from proxy and cached")

        return client_id, client_secret

    # ── Private Methods ─────────────────────────────────────────────────

    def _ensure_credentials(self) -> tuple:
        """Get client_id and client_secret, fetching from proxy if needed.

        Priority: BYOK settings → keyring cache → proxy fetch

        Returns:
            Tuple of (client_id, client_secret)
        """
        # 1. Check BYOK settings
        client_id = (
            self._get_credential("epic_client_id")
            or self._get_setting("client_id")
        )
        client_secret = (
            self._get_credential("epic_client_secret")
            or self._get_setting("client_secret")
        )

        if client_id and client_secret:
            return client_id, client_secret

        # 2. Fetch from proxy (caches in keyring)
        return self.fetch_credentials()

    def _load_session(self) -> Optional[Dict[str, Any]]:
        """Load session from keyring (cached in memory after first load)."""
        if self._session_loaded:
            return self._session

        raw = self._get_credential("oauth_session")
        if raw:
            try:
                self._session = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Corrupt Epic session in keyring, clearing")
                self._session = None
                try:
                    self._delete_credential("oauth_session")
                except Exception:
                    pass
        else:
            self._session = None

        self._session_loaded = True
        return self._session

    def _store_session(self, oauth_response: Dict[str, Any]) -> None:
        """Store OAuth response as session in keyring.

        Args:
            oauth_response: Raw response from Epic's /oauth/token endpoint
        """
        expires_in = oauth_response.get("expires_in", 3600)
        session = {
            "access_token": oauth_response.get("access_token"),
            "refresh_token": oauth_response.get("refresh_token"),
            "account_id": oauth_response.get("account_id"),
            "display_name": oauth_response.get("displayName"),
            "expires_at": time.time() + expires_in,
        }

        session_json = json.dumps(session)
        self._set_credential("oauth_session", session_json)

        # Update in-memory cache
        self._session = session
        self._session_loaded = True

    def _parse_error(self, response) -> str:
        """Extract error message from Epic API error response."""
        try:
            data = response.json()
            error_code = data.get("errorCode", "")
            error_msg = data.get("errorMessage", "")

            # Corrective action required (CAPTCHA, 2FA)
            if error_code == "errors.com.epicgames.oauth.corrective_action_required":
                continuation = data.get("continuation", "")
                return (
                    f"Epic requires additional verification. "
                    f"Please complete the action at: {continuation}"
                )

            return error_msg or f"HTTP {response.status_code}"
        except Exception:
            return f"HTTP {response.status_code}"
