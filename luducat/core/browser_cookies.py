# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# browser_cookies.py

"""Centralized browser cookie management for luducat

Provides a unified interface for extracting cookies from user's browsers.
Supports configurable browser selection instead of auto-detection.

Used by store plugins for authentication (Steam, GOG, etc.)
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Browser definitions: (display_name, config_key, function_name)
SUPPORTED_BROWSERS: List[Tuple[str, str, str]] = [
    ("Vivaldi", "vivaldi", "vivaldi"),
    ("Firefox", "firefox", "firefox"),
    ("Chrome", "chrome", "chrome"),
    ("Chromium", "chromium", "chromium"),
    ("Brave", "brave", "brave"),
    ("Edge", "edge", "edge"),
    ("Opera", "opera", "opera"),
    ("Opera GX", "opera_gx", "opera_gx"),
    ("LibreWolf", "librewolf", "librewolf"),
    ("Safari", "safari", "safari"),
]


class BrowserCookieManager:
    """Centralized manager for browser cookie extraction

    Provides a unified API for plugins to request cookies from browsers.
    Supports user-configured browser preference or automatic detection.

    Usage:
        manager = BrowserCookieManager(config)
        cookies, browser = manager.get_cookies_for_domain(".gog.com", "gog-al")
    """

    def __init__(self, config: Any = None):
        """Initialize the cookie manager

        Args:
            config: Config object with get() method. If None, uses "auto" mode.
        """
        self._config = config
        self._browser_cookie3 = None
        self._available_browsers: List[Tuple[str, str, Callable]] = []
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        """Lazy-load browser_cookie3 and discover available browsers

        Returns:
            True if browser_cookie3 is available
        """
        if self._initialized:
            return self._browser_cookie3 is not None

        self._initialized = True

        try:
            import browser_cookie3
            self._browser_cookie3 = browser_cookie3
        except ImportError:
            logger.error("browser_cookie3 not installed. Install with: pip install browser-cookie3")
            return False

        # Build list of available browser functions
        for display_name, config_key, func_name in SUPPORTED_BROWSERS:
            if hasattr(browser_cookie3, func_name):
                func = getattr(browser_cookie3, func_name)
                self._available_browsers.append((display_name, config_key, func))

        return True

    def get_preferred_browser(self) -> str:
        """Get user's preferred browser from config

        Returns:
            Browser config key (e.g., "firefox") or "auto" for auto-detection
        """
        if self._config is None:
            return "auto"
        return self._config.get("sync.preferred_browser", "auto")

    def get_available_browsers(self) -> List[Tuple[str, str]]:
        """Get list of available browsers

        Returns:
            List of (display_name, config_key) tuples
        """
        self._ensure_initialized()
        return [(name, key) for name, key, _ in self._available_browsers]

    def get_cookies_for_domain(
        self,
        domain: str,
        required_cookie: Optional[str] = None
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Get cookies for a domain from browser(s)

        Uses the user's preferred browser if configured, otherwise tries
        all available browsers in order.

        Args:
            domain: Cookie domain to search (e.g., ".gog.com", "steampowered.com")
            required_cookie: If specified, only return if this cookie exists

        Returns:
            Tuple of (cookies dict, browser name) or ({}, None) if not found
        """
        # Consent gate: user must have granted local data access permission
        if self._config is not None:
            if not self._config.get("privacy.local_data_access_consent", False):
                logger.debug(
                    f"Skipping cookie access for {domain}: "
                    "privacy.local_data_access_consent not granted"
                )
                return {}, None

        if not self._ensure_initialized():
            return {}, None

        preferred = self.get_preferred_browser()

        if preferred != "auto":
            # Try only the preferred browser
            for display_name, config_key, func in self._available_browsers:
                if config_key == preferred:
                    result = self._try_browser(func, display_name, domain, required_cookie)
                    if result[0]:  # Found cookies
                        return result
                    logger.warning(f"Preferred browser {display_name} has no cookies for {domain}")
                    return {}, None

            logger.warning(f"Preferred browser '{preferred}' not available, falling back to auto")

        # Auto-detect: try all browsers
        tried = []
        for display_name, config_key, func in self._available_browsers:
            tried.append(display_name)
            result = self._try_browser(func, display_name, domain, required_cookie)
            if result[0]:  # Found cookies
                return result

        logger.debug("No cookies for %s (tried %s)", domain, ", ".join(tried))
        return {}, None

    def _try_browser(
        self,
        browser_func: Callable,
        browser_name: str,
        domain: str,
        required_cookie: Optional[str]
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Try to get cookies from a specific browser

        Args:
            browser_func: browser_cookie3 function to call
            browser_name: Display name for logging
            domain: Cookie domain
            required_cookie: Required cookie name (if any)

        Returns:
            Tuple of (cookies dict, browser name) or ({}, None)
        """
        try:
            cookie_jar = browser_func(domain_name=domain)
            cookies = {}

            for cookie in cookie_jar:
                cookies[cookie.name] = cookie.value

            if not cookies:
                return {}, None

            # Check for required cookie
            if required_cookie:
                if required_cookie in cookies:
                    logger.info(f"Found {required_cookie} in {browser_name}")
                    return cookies, browser_name
                else:
                    return {}, None

            logger.debug(f"Found {len(cookies)} cookies for {domain} from {browser_name}")
            return cookies, browser_name

        except Exception:
            return {}, None

    def get_cookie_jar_for_domain(
        self,
        domain: str,
        required_cookies: Optional[List[str]] = None
    ) -> Tuple[Any, Optional[str]]:
        """Get raw cookie jar for a domain (preserves cookie attributes)

        Some use cases need the full cookie objects with all attributes
        (domain, path, secure, httponly, etc.) rather than just name/value.

        Args:
            domain: Cookie domain to search
            required_cookies: List of cookie names that must be present

        Returns:
            Tuple of (cookie_jar, browser_name) or (None, None)
        """
        if not self._ensure_initialized():
            return None, None

        preferred = self.get_preferred_browser()
        browsers_to_try = []

        if preferred != "auto":
            # Find preferred browser
            for display_name, config_key, func in self._available_browsers:
                if config_key == preferred:
                    browsers_to_try = [(display_name, func)]
                    break
            if not browsers_to_try:
                logger.warning(f"Preferred browser '{preferred}' not available, falling back to auto")
                browsers_to_try = [(name, func) for name, _, func in self._available_browsers]
        else:
            browsers_to_try = [(name, func) for name, _, func in self._available_browsers]

        for browser_name, browser_func in browsers_to_try:
            try:
                cookie_jar = browser_func(domain_name=domain)

                # Check if it has cookies
                cookie_names = [c.name for c in cookie_jar]
                if not cookie_names:
                    continue

                # Check for required cookies
                if required_cookies:
                    if all(rc in cookie_names for rc in required_cookies):
                        logger.info(f"Found required cookies in {browser_name}")
                        return cookie_jar, browser_name
                else:
                    logger.debug(f"Found {len(cookie_names)} cookies from {browser_name}")
                    return cookie_jar, browser_name

            except Exception:
                continue

        return None, None

    def check_login(self, domain: str, required_cookie: str) -> Tuple[bool, Optional[str]]:
        """Check if user is logged in to a domain

        Convenience method for login status checks.

        Args:
            domain: Cookie domain to check
            required_cookie: Cookie name that indicates successful auth

        Returns:
            Tuple of (is_logged_in, browser_name)
        """
        cookies, browser = self.get_cookies_for_domain(domain, required_cookie)
        return bool(cookies), browser


# Module-level singleton for simple access
_default_manager: Optional[BrowserCookieManager] = None


def get_browser_cookie_manager(config: Any = None) -> BrowserCookieManager:
    """Get the browser cookie manager instance

    Creates a singleton instance if config is provided, or returns
    existing instance. For plugin use, pass the config on first call.

    Args:
        config: Config object (required on first call)

    Returns:
        BrowserCookieManager instance
    """
    global _default_manager

    if _default_manager is None:
        _default_manager = BrowserCookieManager(config)
    elif config is not None and _default_manager._config is None:
        _default_manager._config = config

    return _default_manager
