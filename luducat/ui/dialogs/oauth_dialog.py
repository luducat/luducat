# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# oauth_dialog.py

"""Browser Cookie Login Dialog

Generic dialog for authenticating with stores by reading cookies
from the user's browser after they log in normally.

Uses browser_cookie3 to read cookies from all supported browsers.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


@dataclass
class BrowserLoginConfig:
    """Configuration for browser cookie-based login

    Plugins provide this config to specify how to authenticate
    using cookies from the user's browser.
    """
    name: str                    # Display name (e.g., "GOG")
    login_url: str               # URL to open for login
    cookie_domain: str           # Domain to search cookies (e.g., ".gog.com")
    required_cookie: str         # Cookie name that indicates auth (e.g., "gog-al")
    additional_cookies: List[str] = field(default_factory=list)


def get_cookies_for_domain(domain: str, required_cookie: str = None) -> tuple:
    """Get all cookies for a domain from user's preferred browser

    Delegates to centralized BrowserCookieManager which respects user's
    browser preference from settings.

    Args:
        domain: Cookie domain to search for (e.g., ".gog.com")
        required_cookie: If specified, only return if this cookie exists

    Returns:
        Tuple of (cookies dict, browser name) or (empty dict, None)
    """
    try:
        from ...core.browser_cookies import get_browser_cookie_manager

        manager = get_browser_cookie_manager()
        return manager.get_cookies_for_domain(domain, required_cookie)
    except Exception as e:
        logger.error(f"Error reading browser cookies: {e}")
        return {}, None


def check_login_cookies(domain: str, required_cookie: str) -> Optional[Dict[str, str]]:
    """Check if login cookies exist for a domain

    Args:
        domain: Cookie domain to check
        required_cookie: Cookie name that indicates successful auth

    Returns:
        Dict of cookies if logged in, None otherwise
    """
    cookies, _ = get_cookies_for_domain(domain, required_cookie)
    if cookies and required_cookie in cookies:
        return cookies
    return None


def get_login_status(domain: str, required_cookie: str) -> tuple:
    """Get login status with browser info

    Args:
        domain: Cookie domain to check
        required_cookie: Cookie name that indicates successful auth

    Returns:
        Tuple of (is_logged_in: bool, browser_name: str or None)
    """
    try:
        from ...core.browser_cookies import get_browser_cookie_manager

        manager = get_browser_cookie_manager()
        return manager.check_login(domain, required_cookie)
    except Exception as e:
        logger.error(f"Error checking login status: {e}")
        return False, None


class BrowserLoginDialog(QDialog):
    """Generic browser cookie login dialog

    Reads authentication cookies from the user's browser
    after they log into a store normally.
    """

    auth_success = Signal(dict)  # Emits cookies dict
    auth_failed = Signal(str)    # Emits error message

    def __init__(self, config: BrowserLoginConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self._cookies: Optional[Dict[str, str]] = None
        self._browser: Optional[str] = None

        self.setWindowTitle(_("Login to {name}").format(name=config.name))
        self.setMinimumWidth(450)
        self.setModal(True)

        self._setup_ui()
        self._check_existing_login()

    def _setup_ui(self) -> None:
        """Set up dialog UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel(_("<h3>Login to {name}</h3>").format(name=self.config.name))
        layout.addWidget(title)

        # Instructions
        instructions = QLabel(
            _("<b>Step 1:</b> Click 'Open {name}' and log in with your browser.<br><br>"
              "<b>Step 2:</b> After logging in, click 'Check Login' to import your session.").format(
                name=self.config.name)
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)

        # Open browser button
        btn_open_layout = QHBoxLayout()
        self.btn_open = QPushButton(_("Open {name}").format(name=self.config.name))
        self.btn_open.clicked.connect(self._open_login_page)
        btn_open_layout.addWidget(self.btn_open)
        btn_open_layout.addStretch()
        layout.addLayout(btn_open_layout)

        # Status
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch()

        # Bottom buttons
        bottom_layout = QHBoxLayout()

        self.btn_check = QPushButton(_("Check Login"))
        self.btn_check.clicked.connect(self._check_login)
        bottom_layout.addWidget(self.btn_check)

        bottom_layout.addStretch()

        self.btn_cancel = QPushButton(_("Cancel"))
        self.btn_cancel.clicked.connect(self._on_cancel)
        bottom_layout.addWidget(self.btn_cancel)

        layout.addLayout(bottom_layout)

    def _check_existing_login(self) -> None:
        """Check if user is already logged in"""
        is_logged_in, browser = get_login_status(
            self.config.cookie_domain,
            self.config.required_cookie
        )
        if is_logged_in and browser:
            self.status_label.setText(
                _("Found existing {name} login in {browser}.\n"
                  "Click 'Check Login' to use it, or log in again if needed.").format(
                    name=self.config.name, browser=browser)
            )
            from luducat.utils.style_helpers import set_status_property
            set_status_property(self.status_label, "success")

    def _open_login_page(self) -> None:
        """Open login page in preferred browser"""
        from ...utils.browser import open_url
        open_url(self.config.login_url)
        self.status_label.setText(
            _("Browser opened. Please log in to {name},\n"
              "then click 'Check Login'.").format(name=self.config.name)
        )
        from luducat.utils.style_helpers import set_status_property
        set_status_property(self.status_label, "")

    def _check_login(self) -> None:
        """Check for login cookies"""
        cookies, browser = get_cookies_for_domain(
            self.config.cookie_domain,
            self.config.required_cookie
        )

        if cookies and self.config.required_cookie in cookies:
            self._cookies = cookies
            self._browser = browser
            from luducat.utils.style_helpers import set_status_property
            self.status_label.setText(_("Login successful! (from {browser})").format(browser=browser))
            set_status_property(self.status_label, "success", bold=True)

            # Emit success with cookies
            self.auth_success.emit(cookies)
            self.accept()
        else:
            from luducat.utils.style_helpers import set_status_property
            self.status_label.setText(
                _("Could not find {name} login.\n"
                  "Make sure you're logged in on {name},\n"
                  "then try again.").format(name=self.config.name)
            )
            set_status_property(self.status_label, "error")

    def _on_cancel(self) -> None:
        """Handle cancel button"""
        logger.info(f"{self.config.name} login cancelled by user")
        self.auth_failed.emit(_("Login cancelled"))
        self.reject()

    def get_cookies(self) -> Optional[Dict[str, str]]:
        """Get captured cookies"""
        return self._cookies

    def get_browser(self) -> Optional[str]:
        """Get browser name that provided the cookies"""
        return self._browser


# Backwards compatibility alias
OAuthDialog = BrowserLoginDialog
