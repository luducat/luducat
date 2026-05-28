# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Store download handler registry — Layer 3 of the Luducat Downloader.

Routes URLs and product IDs to the correct store-specific download handler.
Handlers register themselves at import time. The registry dispatches to the
matching handler based on URL patterns (drop target) or store name (library).

Usage:
    from luducat.core.download_handlers import register, find_handler, get_handler

    # Drop target path — URL routing
    handler = find_handler("https://www.gog.com/game/baldurs_gate")
    if handler:
        target = handler.resolve_url(url)
        download_manager.submit(target)

    # Library path — direct store lookup
    handler = get_handler("gog")
    if handler:
        info = handler.get_available_downloads("1207658691")
        # ... show picker ...
        target = handler.resolve_downloads(info, selections)
        download_manager.submit(target)
"""

from __future__ import annotations

import logging
from typing import Optional

from luducat.core.download_handlers.base import (
    AbstractDownloadHandler,
    AuthStatus,
)

logger = logging.getLogger(__name__)

# Module-level registry — handler instances keyed by store_name
_handlers: dict[str, AbstractDownloadHandler] = {}


def register(handler: AbstractDownloadHandler) -> None:
    """Register a download handler.

    Args:
        handler: An instance of AbstractDownloadHandler. Its store_name
            is used as the registry key.

    Raises:
        ValueError: If a handler with the same store_name is already registered.
    """
    name = handler.store_name
    if name in _handlers:
        raise ValueError(
            f"Download handler already registered for store {name!r}"
        )
    _handlers[name] = handler
    logger.debug("Registered download handler: %s (%s)", name, handler.display_name)


def unregister(store_name: str) -> None:
    """Remove a handler from the registry.

    Silently ignores if no handler is registered for the given store.
    """
    removed = _handlers.pop(store_name, None)
    if removed:
        logger.debug("Unregistered download handler: %s", store_name)


def find_handler(url: str) -> Optional[AbstractDownloadHandler]:
    """Find the handler that accepts the given URL.

    Checks each registered handler's can_handle() method. Returns the
    first match, or None if no handler accepts the URL.
    """
    for handler in _handlers.values():
        if handler.can_handle(url):
            return handler
    return None


def get_handler(store_name: str) -> Optional[AbstractDownloadHandler]:
    """Look up a handler by store name.

    Args:
        store_name: The store identifier (e.g. 'gog', 'zoom').

    Returns:
        The handler instance, or None if not registered.
    """
    return _handlers.get(store_name)


def get_all_handlers() -> list[AbstractDownloadHandler]:
    """Return all registered handlers (insertion order)."""
    return list(_handlers.values())


def supported_stores() -> list[str]:
    """Return store names of all registered handlers."""
    return list(_handlers.keys())


def supported_domains() -> set[str]:
    """Extract unique domain patterns from all handlers.

    Useful for UI hints ("Supports: gog.com, zoom-platform.com, ...").
    Extracts domain-like strings from url_patterns — best-effort, not
    guaranteed to be perfect for complex patterns.
    """
    import re as _re

    domains: set[str] = set()
    # Match domain-like parts in regex pattern strings (dots may be escaped as \.)
    _domain_re = _re.compile(
        r"(?:https?://)?(?:\(\?:)?(?:www\\?\.)?([a-z0-9\\._-]+\\?\.[a-z]{2,})"
    )
    for handler in _handlers.values():
        for pattern in handler.url_patterns:
            m = _domain_re.search(pattern.pattern)
            if m:
                domain = m.group(1).replace("\\.", ".").replace("\\", "")
                domains.add(domain)
    return domains


def clear() -> None:
    """Remove all registered handlers (for test teardown)."""
    _handlers.clear()
