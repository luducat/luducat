# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# network.py

"""SDK network facade for plugin HTTP access.

Provides ``PluginHttpClient`` — the ONLY sanctioned way for plugins
to make HTTP requests. Delegates to ``NetworkManager`` in core via
the SDK registry.

Usage in plugins::

    # Injected by PluginManager — access via self.http
    response = self.http.get("https://api.example.com/data")
    data = response.json()

All requests go through the centralized rate limiter, domain
allowlist, and online/offline toggle.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from . import _registry

# Re-export types so plugins don't need bare `import requests`
Response = requests.Response
RequestException = requests.RequestException
RequestTimeout = requests.exceptions.Timeout
ConnectionError = requests.exceptions.ConnectionError
HTTPError = requests.exceptions.HTTPError

logger = logging.getLogger(__name__)


def is_online() -> bool:
    """Check if network is available.

    Delegates to ``NetworkMonitor`` via registry. Returns True if
    the monitor is not registered (optimistic default).
    """
    monitor = _registry._network_monitor
    if monitor is None:
        return True
    return monitor.is_online


class PluginHttpClient:
    """HTTP client facade for a single plugin.

    Each plugin gets its own ``PluginHttpClient`` injected by
    ``PluginManager``. All requests are routed through
    ``NetworkManager`` for rate limiting, domain checking, and
    statistics.

    Args:
        plugin_name: Plugin identifier (for logging and domain checks)
    """

    def __init__(self, plugin_name: str):
        self._plugin_name = plugin_name

    @property
    def _manager(self):
        """Lazy access to the NetworkManager via registry."""
        mgr = _registry._network_manager
        if mgr is None:
            raise RuntimeError(
                "NetworkManager not initialized — "
                "PluginManager has not registered it yet"
            )
        return mgr

    # ── Synchronous API ──────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> requests.Response:
        """HTTP GET request.

        Args:
            url: Request URL
            **kwargs: Passed to ``requests.Session.request()``
                      (params, headers, timeout, etc.)

        Returns:
            ``requests.Response``

        Raises:
            DomainBlockedError: URL domain not in plugin's allowlist
            OfflineError: Application is in offline mode
        """
        return self._manager.execute_request(
            self._plugin_name, "GET", url, **kwargs
        )

    def post(self, url: str, **kwargs) -> requests.Response:
        """HTTP POST request.

        Args:
            url: Request URL
            **kwargs: Passed to ``requests.Session.request()``
                      (data, json, headers, timeout, etc.)

        Returns:
            ``requests.Response``
        """
        return self._manager.execute_request(
            self._plugin_name, "POST", url, **kwargs
        )

    def put(self, url: str, **kwargs) -> requests.Response:
        """HTTP PUT request."""
        return self._manager.execute_request(
            self._plugin_name, "PUT", url, **kwargs
        )

    def delete(self, url: str, **kwargs) -> requests.Response:
        """HTTP DELETE request."""
        return self._manager.execute_request(
            self._plugin_name, "DELETE", url, **kwargs
        )

    def head(self, url: str, **kwargs) -> requests.Response:
        """HTTP HEAD request."""
        return self._manager.execute_request(
            self._plugin_name, "HEAD", url, **kwargs
        )

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Generic HTTP request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            url: Request URL
            **kwargs: Passed to ``requests.Session.request()``
        """
        return self._manager.execute_request(
            self._plugin_name, method, url, **kwargs
        )

    # ── Session access ───────────────────────────────────────────────

    @property
    def session(self) -> requests.Session:
        """Raw ``requests.Session`` for advanced use.

        Bypasses automatic rate limiting and domain checking.
        Use with caution — prefer the ``get()``/``post()`` methods.
        """
        return self._manager.get_plugin_session(self._plugin_name)

    # ── Statistics ───────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get request statistics for this plugin.

        Returns:
            Dict mapping domain -> {count, bytes, last_request}
        """
        return self._manager.get_plugin_stats(self._plugin_name)

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        """Release resources. Called by PluginManager on shutdown."""
        # Session cleanup is handled by NetworkManager.close()
        pass

    def __repr__(self) -> str:
        return f"PluginHttpClient({self._plugin_name!r})"
