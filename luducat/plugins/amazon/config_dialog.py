# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config_dialog.py

"""Amazon Games plugin configuration dialog.

Auth flow: Amazon forces a full password/OTP/approval sign-in for
device registration (cookies never suffice), so the dialog opens the
sign-in URL in the user's browser and the user pastes the address the
browser lands on afterwards. The single-use authorization code in that
URL completes the registration; the refresh token renews silently from
then on.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from luducat.plugins.sdk.dialogs import set_status_property

logger = logging.getLogger(__name__)


class AmazonConfigDialog(QDialog):
    """Configuration dialog for the Amazon Games plugin.

    Matches the generic store dialog layout:
    1. Header (icon + title + description)
    2. Separator
    3. Status row + Refresh
    4. Logout row
    5. Bottom row ([Reset...] ... [OK] [Cancel])
    """

    # Emitted when connection status changes (plugin_name, is_authenticated)
    connection_status_changed = Signal(str, bool)
    # Emitted when store data is reset (plugin_name)
    store_data_reset = Signal(str)

    def __init__(
        self,
        config: Any,
        plugin_manager: Any,
        parent: Optional[QDialog] = None,
    ):
        super().__init__(parent)

        self.config = config
        self.plugin_manager = plugin_manager
        self.plugin_name = "amazon"

        self._store = None

        self.setWindowTitle(_("Configure Amazon Games"))
        self.setMinimumWidth(620)
        self.setMinimumHeight(400)

        self._setup_ui()
        self._refresh_status(force=True)
        self.adjustSize()

    def _setup_ui(self) -> None:
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        # === Header ===
        from luducat.plugins.sdk.ui import load_tinted_icon

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)

        icon_name = None
        discovered = self.plugin_manager.get_discovered_plugins()
        meta = discovered.get(self.plugin_name)
        if meta and meta.icon and meta.plugin_dir:
            icon_path = meta.plugin_dir / meta.icon
            if icon_path.exists():
                icon_name = str(icon_path)
        if not icon_name:
            icon_name = "plug-store.svg"

        icon = load_tinted_icon(icon_name, size=32)
        icon_label = QLabel()
        icon_label.setPixmap(icon.pixmap(32, 32))
        icon_label.setFixedSize(32, 32)
        header_layout.addWidget(icon_label)

        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        version = meta.version if meta else ""
        title = QLabel(f"<b>Amazon Games</b>  v{version}")
        base_size = QApplication.instance().font().pointSize()
        title_font = title.font()
        title_font.setPointSize(base_size + 3)
        title.setFont(title_font)
        header_text.addWidget(title)

        desc = QLabel(_("Amazon Games library integration"))
        desc.setWordWrap(True)
        desc.setObjectName("dialogDescription")
        header_text.addWidget(desc)

        header_layout.addLayout(header_text, 1)
        layout.addLayout(header_layout)

        # === Separator ===
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # === Status row: [status_label ............ Refresh] ===
        status_row = QHBoxLayout()

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)

        self.btn_test = QPushButton(_("Refresh"))
        self.btn_test.clicked.connect(self._on_test_connection)
        status_row.addWidget(self.btn_test)

        layout.addLayout(status_row)

        # === Logout — right-aligned below ===
        auth_row = QHBoxLayout()
        auth_row.addStretch()
        self.btn_logout = QPushButton(_("Logout"))
        self.btn_logout.clicked.connect(self._on_logout)
        auth_row.addWidget(self.btn_logout)
        layout.addLayout(auth_row)

        layout.addStretch()

        # === Bottom Row: [Reset...] ... stretch ... [OK] [Cancel] ===
        bottom_layout = QHBoxLayout()

        self.btn_reset_data = QPushButton(_("Reset..."))
        self.btn_reset_data.setToolTip(
            _("Remove all Amazon game entries and clear the catalog database.\n"
              "User data (favorites, tags) for multi-store games is preserved.")
        )
        self.btn_reset_data.clicked.connect(self._on_reset_data)
        bottom_layout.addWidget(self.btn_reset_data)

        bottom_layout.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        bottom_layout.addWidget(button_box)

        layout.addLayout(bottom_layout)

    def _get_plugin_instance(self):
        """Get or load the Amazon plugin instance."""
        if self._store is not None:
            return self._store

        loaded = self.plugin_manager._loaded.get(self.plugin_name)
        if not loaded or not loaded.instance:
            try:
                self.plugin_manager.load_plugin(self.plugin_name)
                loaded = self.plugin_manager._loaded.get(self.plugin_name)
            except Exception as e:
                logger.error("Failed to load Amazon plugin: %s", e)
                return None

        if loaded and loaded.instance:
            self._store = loaded.instance

        return self._store

    def _refresh_status(self, force: bool = False) -> None:
        """Refresh status display based on current state."""
        store = self._get_plugin_instance()
        if not store:
            self.status_label.setText(_("Plugin not loaded"))
            set_status_property(self.status_label, "")
            self.btn_logout.setEnabled(False)
            self.btn_test.setEnabled(False)
            return

        is_auth, status_msg = store.get_auth_status()

        if is_auth:
            self.status_label.setText(status_msg)
            set_status_property(self.status_label, "success")
        else:
            self.status_label.setText(
                _("Not connected — click Refresh to authenticate")
            )
            set_status_property(self.status_label, "")

        self.btn_logout.setEnabled(is_auth)
        self.btn_test.setEnabled(True)

    def _on_test_connection(self) -> None:
        """Handle Refresh button click."""
        store = self._get_plugin_instance()
        if not store:
            QMessageBox.critical(self, _("Error"), _("Plugin not loaded"))
            return

        self.status_label.setText(_("Testing connection..."))
        set_status_property(self.status_label, "")
        QApplication.processEvents()

        self._refresh_status(force=True)

        if store.is_authenticated():
            self.connection_status_changed.emit(self.plugin_name, True)
        else:
            self._start_auth_flow()

    def _start_auth_flow(self) -> None:
        """Start the Amazon sign-in flow (browser + paste-back)."""
        instructions = (
            "<b>" + _("Amazon Games Authentication") + "</b><br><br>"
            + _("To connect your Amazon account:") + "<br><br>"
            "<b>1.</b> " + _("Click OK to open the Amazon sign-in page") + "<br><br>"
            "<b>2.</b> " + _("Sign in with your Amazon account. Amazon asks for "
                             "your password and possibly a one-time code — this "
                             "full sign-in is required once to register this "
                             "computer.") + "<br><br>"
            "<b>3.</b> " + _("After the final approval step the browser lands on "
                             "the Amazon home page. Copy the COMPLETE address "
                             "from the browser's address bar right away.") + "<br><br>"
            "<b>4.</b> " + _("Paste that address in the next dialog.") + "<br><br>"
            + _("The address contains a one-time code — if the browser "
                "navigates away, restart the sign-in.")
        )

        reply = QMessageBox.question(
            self,
            _("Amazon Sign-in"),
            instructions,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return

        store = self._get_plugin_instance()
        if not store:
            return

        signin_url = store.build_signin_url()
        from luducat.plugins.sdk.ui import open_url
        open_url(signin_url)

        redirect_url, ok = QInputDialog.getText(
            self,
            _("Paste the Address"),
            _("Paste the full address the browser shows after you "
              "completed the Amazon sign-in:"),
            QLineEdit.EchoMode.Normal,
            "",
        )

        if not ok or not redirect_url.strip():
            self.status_label.setText(_("Authentication cancelled"))
            set_status_property(self.status_label, "warning")
            return

        self.status_label.setText(_("Registering this computer with Amazon..."))
        set_status_property(self.status_label, "")
        QApplication.processEvents()

        success, message = store.authenticate_with_redirect_url(
            redirect_url.strip()
        )

        if success:
            self.status_label.setText(message)
            set_status_property(self.status_label, "success")
            self._refresh_status(force=True)
            self.connection_status_changed.emit(self.plugin_name, True)
        else:
            self.status_label.setText(
                _("Authentication failed: {message}").format(message=message)
            )
            set_status_property(self.status_label, "error")

    def _on_logout(self) -> None:
        """Handle Logout button click."""
        store = self._get_plugin_instance()
        if not store:
            return

        reply = QMessageBox.question(
            self,
            _("Confirm Logout"),
            _("Are you sure you want to logout from Amazon Games?<br><br>"
              "This deregisters the computer from your Amazon account. "
              "Signing in again requires the full Amazon sign-in."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.status_label.setText(_("Logging out..."))
        set_status_property(self.status_label, "")
        QApplication.processEvents()

        success, message = store.logout()

        if success:
            self.status_label.setText(_("Logged out successfully"))
            set_status_property(self.status_label, "")
        else:
            self.status_label.setText(
                _("Logout failed: {message}").format(message=message)
            )
            set_status_property(self.status_label, "error")

        self._refresh_status(force=True)

    def _get_game_service(self):
        """Get game_service from parent window chain."""
        parent = self.parent()
        while parent:
            if hasattr(parent, 'game_service'):
                return parent.game_service
            parent = parent.parent()
        return None

    def _on_reset_data(self) -> None:
        """Reset all Amazon store data using the shared reset path."""
        from luducat.plugins.sdk.dialogs import reset_plugin_data
        reset_plugin_data(
            parent_widget=self,
            plugin_name=self.plugin_name,
            display_name="Amazon Games",
            plugin_types=["store"],
            config=self.config,
            status_label=self.status_label,
            store_data_reset_signal=self.store_data_reset,
            get_game_service_fn=self._get_game_service,
            get_plugin_instance_fn=self._get_plugin_instance,
        )
