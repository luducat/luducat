# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# _registry.py

"""Internal registry for SDK shim implementations.

Core populates these at startup via ``PluginManager``.
Plugins MUST NOT import this module directly.  SDK shim modules
(``sdk.config``, ``sdk.cookies``, etc.) read from here.
"""

from __future__ import annotations

from typing import Any, Callable


# ── Registered implementations (populated by core at startup) ────────

# config shim: get_data_dir, get_cache_dir, get_config_value, set_config_value
_get_data_dir: Callable[[], Any] | None = None
_get_cache_dir: Callable[[], Any] | None = None
_get_config_value: Callable[[str, Any], Any] | None = None
_set_config_value: Callable[[str, Any], None] | None = None

# browser cookies shim
_get_browser_cookies: Callable[..., Any] | None = None

# dialog helpers
_browser_login_config_class: type | None = None
_set_status_property: Callable[..., None] | None = None
_get_login_status: Callable[..., Any] | None = None

# network manager (Phase 3)
_network_manager: Any | None = None

# proxy manager (authenticated proxy access)
_proxy_manager: Any | None = None

# dialog helpers (reset_plugin_data)
_reset_plugin_data: Callable[..., None] | None = None

# UI helpers (icon tinting)
_load_tinted_icon: Callable[..., Any] | None = None

# network monitor (online status)
_network_monitor: Any | None = None

# URL opener (browser-aware)
_open_url: Callable[..., None] | None = None


# ── Registration API (called by PluginManager) ───────────────────────

def register_config(
    get_data_dir: Callable,
    get_cache_dir: Callable,
    get_config_value: Callable,
    set_config_value: Callable | None = None,
) -> None:
    """Register config access functions."""
    global _get_data_dir, _get_cache_dir, _get_config_value, _set_config_value
    _get_data_dir = get_data_dir
    _get_cache_dir = get_cache_dir
    _get_config_value = get_config_value
    _set_config_value = set_config_value


def register_cookies(get_browser_cookies: Callable) -> None:
    """Register browser cookie access function."""
    global _get_browser_cookies
    _get_browser_cookies = get_browser_cookies


def register_dialogs(
    browser_login_config_class: type | None = None,
    set_status_property: Callable | None = None,
    get_login_status: Callable | None = None,
) -> None:
    """Register dialog helper implementations."""
    global _browser_login_config_class, _set_status_property, _get_login_status
    if browser_login_config_class is not None:
        _browser_login_config_class = browser_login_config_class
    if set_status_property is not None:
        _set_status_property = set_status_property
    if get_login_status is not None:
        _get_login_status = get_login_status


def register_network_manager(manager: Any) -> None:
    """Register the central NetworkManager instance (Phase 3)."""
    global _network_manager
    _network_manager = manager


def register_proxy_manager(manager: Any) -> None:
    """Register the ProxyManager for authenticated proxy access."""
    global _proxy_manager
    _proxy_manager = manager


def register_reset_plugin_data(fn: Callable) -> None:
    """Register the reset_plugin_data function from UI dialogs."""
    global _reset_plugin_data
    _reset_plugin_data = fn


def register_load_tinted_icon(fn: Callable) -> None:
    """Register the load_tinted_icon function from utils.icons."""
    global _load_tinted_icon
    _load_tinted_icon = fn


def register_network_monitor(monitor: Any) -> None:
    """Register the NetworkMonitor for online status checks."""
    global _network_monitor
    _network_monitor = monitor


def register_open_url(fn: Callable) -> None:
    """Register the open_url function from utils.browser."""
    global _open_url
    _open_url = fn
