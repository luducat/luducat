# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# cookies.py

"""SDK browser cookie access — shim delegating to core via registry.

Provides plugins with browser cookie access without importing
``luducat.core.browser_cookies`` directly.

Usage in plugins::

    from luducat.plugins.sdk.cookies import get_browser_cookie_manager
    manager = get_browser_cookie_manager()
    cookies, error = manager.get_cookies_for_domain("gog.com", "gog-al")

The implementation is injected by ``PluginManager`` at startup via
``_registry.register_cookies()``.
"""

from __future__ import annotations

from typing import Any

from . import _registry
from .config import SdkNotInitializedError


def get_browser_cookie_manager() -> Any:
    """Return the shared BrowserCookieManager instance.

    Equivalent to ``luducat.core.browser_cookies.get_browser_cookie_manager()``.

    Returns:
        BrowserCookieManager instance with ``get_cookies_for_domain()``,
        ``check_login()``, ``get_preferred_browser()`` methods.
    """
    if _registry._get_browser_cookies is None:
        raise SdkNotInitializedError(
            "SDK cookies not initialized — PluginManager has not injected "
            "browser cookie functions yet"
        )
    return _registry._get_browser_cookies()
