# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# dialogs.py

"""SDK dialog helpers — mixed shim + re-export.

Provides plugins with access to dialog utilities without importing
from ``luducat.ui.*`` or ``luducat.utils.*`` directly.

Usage in plugins::

    from luducat.plugins.sdk.dialogs import (
        set_status_property,
        get_browser_login_config_class,
    )
    set_status_property(label, "success", bold=True)

``set_status_property`` is a self-contained re-implementation (no core
import needed).  ``BrowserLoginConfig`` is injected via registry since
it depends on Qt/UI infrastructure.
"""

from __future__ import annotations

from typing import Any, Optional, Type

from . import _registry


# ── Self-contained: set_status_property ──────────────────────────────


def set_status_property(widget: Any, status: str, bold: bool = False) -> None:
    """Set status property on widget and refresh QSS styling.

    Uses dynamic properties so QSS selectors like
    ``QLabel[status="success"]`` can style the widget.

    Args:
        widget: Any QWidget
        status: Status value (``"success"``, ``"error"``, ``"warning"``, or ``""``)
        bold: If True, also sets fontWeight="bold" property
    """
    widget.setProperty("status", status)
    if bold:
        widget.setProperty("fontWeight", "bold")
    elif widget.property("fontWeight"):
        widget.setProperty("fontWeight", "")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# ── Shim: BrowserLoginConfig ────────────────────────────────────────


def get_browser_login_config_class() -> Optional[Type]:
    """Return the BrowserLoginConfig dataclass, if registered.

    Returns:
        The ``BrowserLoginConfig`` class from
        ``luducat.ui.dialogs.oauth_dialog``, or ``None`` if not
        yet registered.
    """
    return _registry._browser_login_config_class


def get_login_status(domain: str, required_cookie: str) -> tuple:
    """Check login status for a cookie-based service.

    Delegates to the registered implementation (from oauth_dialog).

    Args:
        domain: Cookie domain to check (e.g. ".gog.com")
        required_cookie: Cookie name indicating successful auth

    Returns:
        Tuple of (is_logged_in: bool, browser_name: str | None)
    """
    if _registry._get_login_status is None:
        return False, None
    return _registry._get_login_status(domain, required_cookie)


# ── Shim: reset_plugin_data ───────────────────────────────────────


def reset_plugin_data(
    parent_widget,
    plugin_name: str,
    display_name: str,
    plugin_types,
    config,
    status_label,
    store_data_reset_signal,
    get_game_service_fn,
    get_plugin_instance_fn,
    collect_image_urls_fn=None,
) -> None:
    """Reset all data for a plugin using the shared reset path.

    Delegates to the registered ``reset_plugin_data`` implementation
    from ``luducat.ui.dialogs.plugin_config``.

    Raises:
        RuntimeError: If the implementation has not been registered
    """
    fn = _registry._reset_plugin_data
    if fn is None:
        raise RuntimeError(
            "reset_plugin_data not registered — SDK not fully initialized"
        )
    fn(
        parent_widget=parent_widget,
        plugin_name=plugin_name,
        display_name=display_name,
        plugin_types=plugin_types,
        config=config,
        status_label=status_label,
        store_data_reset_signal=store_data_reset_signal,
        get_game_service_fn=get_game_service_fn,
        get_plugin_instance_fn=get_plugin_instance_fn,
        collect_image_urls_fn=collect_image_urls_fn,
    )
