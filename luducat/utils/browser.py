# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# browser.py

"""Centralized URL opening utility.

Opens web URLs in the user's preferred browser from Settings → Advanced →
Preferred Browser (config key: ``sync.preferred_browser``).

Non-web URLs (steam://, heroic://, file://, mailto:) always go through
QDesktopServices so the OS can route them to the correct handler.

Usage::

    from luducat.utils.browser import open_url
    open_url("https://example.com")
"""

import logging
import shutil
import subprocess
from typing import Any, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

logger = logging.getLogger(__name__)

# Config key → candidate executable names (first found wins)
_BROWSER_EXECUTABLES = {
    "firefox": ["firefox"],
    "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
    "chromium": ["chromium", "chromium-browser"],
    "brave": ["brave-browser", "brave"],
    "edge": ["microsoft-edge", "microsoft-edge-stable"],
    "opera": ["opera"],
    "opera_gx": ["opera"],
    "librewolf": ["librewolf"],
    "safari": ["open", "-a", "Safari"],  # macOS only
    "vivaldi": ["vivaldi", "vivaldi-stable"],
}

_config: Optional[Any] = None


def init_browser_opener(config: Any) -> None:
    """Inject the Config object.  Called once during startup in ``main.py``."""
    global _config
    _config = config


def open_url(url) -> bool:
    """Open *url* in the user's preferred browser.

    Falls back to the system default (``QDesktopServices``) when the
    preference is ``"auto"`` or the configured browser is not found.

    Accepts a ``str``, ``QUrl``, or anything with a sensible ``str()``
    representation.

    Returns ``True`` on success.
    """
    if isinstance(url, QUrl):
        url_str = url.toString()
    else:
        url_str = str(url)

    # Non-HTTP schemes → always system handler
    if not url_str.startswith(("http://", "https://")):
        return QDesktopServices.openUrl(QUrl(url_str))

    preferred = "auto"
    if _config is not None:
        preferred = _config.get("sync.preferred_browser", "auto")

    if preferred == "auto":
        return QDesktopServices.openUrl(QUrl(url_str))

    # Try the configured browser executable
    candidates = _BROWSER_EXECUTABLES.get(preferred, [])

    # Safari is special (uses 'open -a Safari')
    if preferred == "safari":
        safari_path = shutil.which("open")
        if safari_path:
            try:
                subprocess.Popen([safari_path, "-a", "Safari", url_str])
                return True
            except OSError as exc:
                logger.debug("Safari launch failed: %s", exc)
    else:
        for exe_name in candidates:
            exe_path = shutil.which(exe_name)
            if exe_path:
                try:
                    subprocess.Popen([exe_path, url_str])
                    return True
                except OSError as exc:
                    logger.debug("%s launch failed: %s", exe_name, exc)

    # Fallback
    logger.warning(
        "Preferred browser '%s' not found, falling back to system default",
        preferred,
    )
    return QDesktopServices.openUrl(QUrl(url_str))
