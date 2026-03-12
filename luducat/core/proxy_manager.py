# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# proxy_manager.py

"""Authenticated access to luducat API proxy with route authorization.

Centralizes proxy signing with per-route, per-caller authorization.
Plugins access this through ``sdk.proxy`` shim — never directly.

Core modules (update_checker, news) continue using ``utils.signing``
directly since they're part of core.
"""

import logging
from typing import Dict, Optional, Set

from luducat.utils.signing import (
    IGDB_PROXY_DEFAULT,
    get_user_agent,
    sign_request,
)

logger = logging.getLogger(__name__)

# Route → set of authorized caller names.
# "_core" is for core modules (update checker, news, security).
# Plugin names must match plugin.json "name" field.
_ROUTE_WHITELIST: Dict[str, Set[str]] = {
    "/version": {"_core"},
    "/news": {"_core"},
    "/fingerprints": {"_core"},
    "/igdb/v4/": {"igdb"},
    "/epic/credentials": {"epic"},
}


class ProxyAccessDenied(Exception):
    """Raised when a caller is not authorized for a proxy route."""
    pass


class ProxyManager:
    """Authenticated access to luducat API proxy with route authorization."""

    def __init__(self, proxy_url: str = IGDB_PROXY_DEFAULT):
        self._proxy_url = proxy_url
        self._route_whitelist: Dict[str, Set[str]] = dict(_ROUTE_WHITELIST)

    @property
    def proxy_url(self) -> str:
        """Base URL of the luducat API proxy."""
        return self._proxy_url

    def _check_authorization(self, caller: str, endpoint: str) -> None:
        """Check if caller is authorized for the given endpoint.

        Args:
            caller: Plugin name or "_core"
            endpoint: Proxy endpoint path (e.g., "/igdb/v4/games")

        Raises:
            ProxyAccessDenied: If caller is not authorized
        """
        for route_prefix, allowed_callers in self._route_whitelist.items():
            if endpoint.startswith(route_prefix):
                if caller in allowed_callers:
                    return
                logger.warning(
                    "Proxy access denied: caller=%s endpoint=%s "
                    "(allowed: %s)",
                    caller, endpoint, allowed_callers,
                )
                raise ProxyAccessDenied(
                    f"Caller '{caller}' is not authorized for "
                    f"proxy route '{route_prefix}'"
                )

        # No matching route — deny by default
        logger.warning(
            "Proxy access denied: caller=%s endpoint=%s (no matching route)",
            caller, endpoint,
        )
        raise ProxyAccessDenied(
            f"No proxy route matches endpoint '{endpoint}'"
        )

    def sign(self, caller: str, endpoint: str, body: str) -> str:
        """Generate HMAC-TOTP signature for a proxy request.

        Args:
            caller: Plugin name or "_core"
            endpoint: IGDB API endpoint name (e.g., "games", "covers")
            body: Apicalypse query body

        Returns:
            Signature string for the X-Signature header

        Raises:
            ProxyAccessDenied: If caller is not authorized
        """
        # Build full path for authorization check
        full_path = f"/igdb/v4/{endpoint}"
        self._check_authorization(caller, full_path)
        return sign_request(endpoint, body)

    def get_user_agent(self) -> str:
        """Build User-Agent string for proxy requests."""
        return get_user_agent()

    def sign_route(self, caller: str, route: str, body: str) -> str:
        """Generate HMAC-TOTP signature for an arbitrary proxy route.

        Unlike ``sign()`` which hardcodes the ``/igdb/v4/`` prefix, this
        accepts a full route path (e.g., ``"epic/credentials"``).

        Args:
            caller: Plugin name or "_core"
            route: Full route path (e.g., "epic/credentials")
            body: Request body (empty string for GET)

        Returns:
            Signature string for the X-Signature header

        Raises:
            ProxyAccessDenied: If caller is not authorized
        """
        full_path = f"/{route}" if not route.startswith("/") else route
        self._check_authorization(caller, full_path)
        return sign_request(route, body)

    def build_route_headers(
        self, caller: str, route: str, body: str
    ) -> dict:
        """Build complete proxy request headers for an arbitrary route.

        Args:
            caller: Plugin name or "_core"
            route: Full route path (e.g., "epic/credentials")
            body: Request body (empty string for GET)

        Returns:
            Dict with User-Agent, X-Signature, Accept headers

        Raises:
            ProxyAccessDenied: If caller is not authorized
        """
        return {
            "User-Agent": self.get_user_agent(),
            "X-Signature": self.sign_route(caller, route, body),
            "Accept": "application/json",
        }

    def build_proxy_headers(
        self, caller: str, endpoint: str, body: str
    ) -> dict:
        """Build complete proxy request headers.

        Args:
            caller: Plugin name or "_core"
            endpoint: IGDB API endpoint name (e.g., "games")
            body: Apicalypse query body

        Returns:
            Dict with User-Agent, X-Signature, Accept headers

        Raises:
            ProxyAccessDenied: If caller is not authorized
        """
        return {
            "User-Agent": self.get_user_agent(),
            "X-Signature": self.sign(caller, endpoint, body),
            "Accept": "application/json",
        }


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    """Get or create the singleton ProxyManager."""
    global _instance
    if _instance is None:
        _instance = ProxyManager()
    return _instance


def reset_proxy_manager() -> None:
    """Reset singleton (for testing)."""
    global _instance
    _instance = None
