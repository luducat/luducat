# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# proxy.py

"""SDK proxy shim — proxy signing without importing from core/utils.

Usage in plugins::

    from luducat.plugins.sdk.proxy import get_proxy_url, build_proxy_headers

    url = get_proxy_url()
    headers = build_proxy_headers("igdb", "games", query)
    resp = self.http.post(f"{url}/igdb/v4/games", headers=headers, data=query)

The caller name (first argument) must match the plugin's ``plugin.json``
name field.  The ProxyManager validates authorization per route.
"""

from __future__ import annotations

from . import _registry


def get_proxy_url() -> str:
    """Return the base URL of the luducat API proxy."""
    mgr = _registry._proxy_manager
    if mgr is None:
        raise RuntimeError("ProxyManager not registered — SDK not initialized")
    return mgr.proxy_url


def get_proxy_user_agent() -> str:
    """Return the User-Agent string for proxy requests."""
    mgr = _registry._proxy_manager
    if mgr is None:
        raise RuntimeError("ProxyManager not registered — SDK not initialized")
    return mgr.get_user_agent()


def build_route_headers(caller: str, route: str, body: str) -> dict:
    """Build complete proxy request headers for an arbitrary route.

    Args:
        caller: Plugin name (must match plugin.json "name" field)
        route: Full route path (e.g., "epic/credentials")
        body: Request body (empty string for GET)

    Returns:
        Dict with User-Agent, X-Signature, Accept headers

    Raises:
        ProxyAccessDenied: If caller is not authorized for the route
        RuntimeError: If ProxyManager is not registered
    """
    mgr = _registry._proxy_manager
    if mgr is None:
        raise RuntimeError("ProxyManager not registered — SDK not initialized")
    return mgr.build_route_headers(caller, route, body)


def build_proxy_headers(caller: str, endpoint: str, body: str) -> dict:
    """Build complete proxy request headers.

    Args:
        caller: Plugin name (must match plugin.json "name" field)
        endpoint: IGDB API endpoint name (e.g., "games", "covers")
        body: Apicalypse query body

    Returns:
        Dict with User-Agent, X-Signature, Accept headers

    Raises:
        ProxyAccessDenied: If caller is not authorized for the route
        RuntimeError: If ProxyManager is not registered
    """
    mgr = _registry._proxy_manager
    if mgr is None:
        raise RuntimeError("ProxyManager not registered — SDK not initialized")
    return mgr.build_proxy_headers(caller, endpoint, body)
