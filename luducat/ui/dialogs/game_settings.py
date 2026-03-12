# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_settings.py

"""Game Settings Dialog

Per-game configuration for platform selection, launch arguments,
and installation management.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, Signal

from ...core.constants import APP_NAME
from ...core.plugin_manager import PluginManager
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from luducat.core.config import Config
    from luducat.core.database import Game
    from luducat.core.game_manager import GameManager
    from luducat.core.runtime_manager import RuntimeManager

logger = logging.getLogger(__name__)


class GameSettingsDialog(QDialog):
    """Dialog for configuring per-game settings

    Provides UI for:
    - Platform selection (DOSBox, ScummVM, external launcher, etc.)
    - Custom launch arguments
    - Installation path management
    - Game verification

    Usage:
        dialog = GameSettingsDialog(game, config, game_manager, runtime_manager)
        if dialog.exec() == QDialog.Accepted:
            # Settings saved automatically
            pass
    """

    # Emitted when settings are saved
    settings_saved = Signal(str)  # game_id

    def __init__(
        self,
        game: "Game",
        config: "Config",
        game_manager: Optional["GameManager"] = None,
        runtime_manager: Optional["RuntimeManager"] = None,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.game = game
        self.config = config
        self.game_manager = game_manager
        self.runtime_manager = runtime_manager

        game_title = getattr(game, 'title', _('Unknown Game'))
        self.setWindowTitle(_("Settings - {}").format(game_title))
        self.setMinimumSize(500, 400)

        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        """Build the dialog UI"""
        layout = QVBoxLayout(self)

        # Tab widget for organizing settings
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Platform tab
        platform_tab = self._create_platform_tab()
        self.tabs.addTab(platform_tab, _("Platform"))

        # Launch tab
        launch_tab = self._create_launch_tab()
        self.tabs.addTab(launch_tab, _("Launch Options"))

        # Installation tab
        install_tab = self._create_install_tab()
        self.tabs.addTab(install_tab, _("Installation"))

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._save_settings)
        layout.addWidget(buttons)

    def _create_platform_tab(self) -> QWidget:
        """Create the platform selection tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Platform selection group
        platform_group = QGroupBox(_("Platform Environment"))
        form = QFormLayout(platform_group)

        # Platform dropdown
        self.platform_combo = QComboBox()
        self.platform_combo.setToolTip(_("Select how to run this game"))
        self._populate_platforms()
        form.addRow(_("Platform:"), self.platform_combo)

        # Platform info
        self.platform_info = QLabel()
        self.platform_info.setWordWrap(True)
        self.platform_info.setObjectName("platformInfo")
        form.addRow("", self.platform_info)

        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)

        layout.addWidget(platform_group)

        # Compatible platforms info
        compat_group = QGroupBox(_("Compatible Platforms"))
        compat_layout = QVBoxLayout(compat_group)

        self.compat_label = QLabel()
        self.compat_label.setWordWrap(True)
        compat_layout.addWidget(self.compat_label)

        self._update_compatible_platforms()
        layout.addWidget(compat_group)

        layout.addStretch()
        return widget

    def _create_launch_tab(self) -> QWidget:
        """Create the launch options tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Arguments group
        args_group = QGroupBox(_("Launch Arguments"))
        args_layout = QVBoxLayout(args_group)

        args_info = QLabel(
            _("Additional command-line arguments passed when launching. "
              "These are appended after any platform-specific arguments.")
        )
        args_info.setWordWrap(True)
        args_info.setObjectName("hintLabel")
        args_layout.addWidget(args_info)

        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText(_("e.g., -windowed -skipintro"))
        args_layout.addWidget(self.args_edit)

        layout.addWidget(args_group)

        # Environment variables group
        env_group = QGroupBox(_("Environment Variables"))
        env_layout = QVBoxLayout(env_group)

        env_info = QLabel(
            _("Set custom environment variables for this game. "
              "One per line, format: NAME=value")
        )
        env_info.setWordWrap(True)
        env_info.setObjectName("hintLabel")
        env_layout.addWidget(env_info)

        self.env_edit = QTextEdit()
        self.env_edit.setPlaceholderText("DXVK_HUD=1\nWINEDEBUG=-all")
        self.env_edit.setMaximumHeight(100)
        env_layout.addWidget(self.env_edit)

        layout.addWidget(env_group)

        layout.addStretch()
        return widget

    def _create_install_tab(self) -> QWidget:
        """Create the installation management tab"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Installation status group
        status_group = QGroupBox(_("Installation Status"))
        status_layout = QFormLayout(status_group)

        self.status_label = QLabel()
        status_layout.addRow(_("Status:"), self.status_label)

        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        status_layout.addRow(_("Path:"), self.path_label)

        self.version_label = QLabel()
        status_layout.addRow(_("Version:"), self.version_label)

        self.size_label = QLabel()
        status_layout.addRow(_("Size:"), self.size_label)

        layout.addWidget(status_group)

        # Actions group
        actions_group = QGroupBox(_("Actions"))
        actions_layout = QHBoxLayout(actions_group)

        self.verify_btn = QPushButton(_("Verify Files"))
        self.verify_btn.clicked.connect(self._verify_installation)
        self.verify_btn.setEnabled(False)
        actions_layout.addWidget(self.verify_btn)

        self.repair_btn = QPushButton(_("Repair"))
        self.repair_btn.clicked.connect(self._repair_installation)
        self.repair_btn.setEnabled(False)
        actions_layout.addWidget(self.repair_btn)

        self.uninstall_btn = QPushButton(_("Uninstall"))
        self.uninstall_btn.clicked.connect(self._uninstall_game)
        self.uninstall_btn.setEnabled(False)
        actions_layout.addWidget(self.uninstall_btn)

        actions_layout.addStretch()
        layout.addWidget(actions_group)

        # Update installation info
        self._update_install_info()

        layout.addStretch()
        return widget

    def _populate_platforms(self) -> None:
        """Populate platform dropdown with available options"""
        self.platform_combo.clear()
        self.platform_combo.addItem(_("Auto-select"), "auto")

        if not self.runtime_manager:
            return

        # Get all available platforms
        platforms = self.runtime_manager.get_available_platforms()
        for platform in platforms:
            display = f"{platform.name}"
            if platform.version:
                display += f" ({platform.version})"
            if platform.is_default:
                display += " [{}]".format(_("Default"))

            self.platform_combo.addItem(display, platform.platform_id)

    def _on_platform_changed(self, index: int) -> None:
        """Update info when platform selection changes"""
        platform_id = self.platform_combo.currentData()
        if not platform_id or platform_id == "auto" or not self.runtime_manager:
            self.platform_info.setText(_("Automatically selects the best available platform."))
            return

        platform = self.runtime_manager.get_platform(platform_id)
        if platform:
            info_parts = []
            if platform.executable_path:
                info_parts.append(_("Executable: {}").format(platform.executable_path))
            if platform.is_managed:
                info_parts.append(_("Managed by {}").format(APP_NAME))
            else:
                info_parts.append(_("System-installed"))
            self.platform_info.setText("\n".join(info_parts))
        else:
            self.platform_info.setText("")

    def _update_compatible_platforms(self) -> None:
        """Update compatible platforms info"""
        if not self.runtime_manager:
            self.compat_label.setText(_("Platform manager not available"))
            return

        compatible = self.runtime_manager.get_compatible_platforms(self.game)
        if compatible:
            names = [r.name for r in compatible]
            self.compat_label.setText(_("This game can run with: {}").format(', '.join(names)))
        else:
            store_name = getattr(self.game, 'store_name', 'unknown')
            self.compat_label.setText(
                _("Uses external launcher for {} games").format(PluginManager.get_store_display_name(store_name))
            )

    def _update_install_info(self) -> None:
        """Update installation status information"""
        if not self.game_manager:
            self.status_label.setText(_("Not available"))
            self.path_label.setText("-")
            self.version_label.setText("-")
            self.size_label.setText("-")
            return

        game_id = str(self.game.id) if hasattr(self.game, 'id') else None
        if not game_id:
            self.status_label.setText(_("Unknown"))
            return

        status = self.game_manager.get_installation_status(self.game)
        info = self.game_manager.get_installation_info(game_id)

        # Update status label with color via QSS properties
        from luducat.core.game_manager import InstallationStatus
        from luducat.utils.style_helpers import set_status_property
        status_map = {
            InstallationStatus.NOT_INSTALLED: (_("Not Installed"), ""),
            InstallationStatus.INSTALLED: (_("Installed"), "installed"),
            InstallationStatus.UPDATE_AVAILABLE: (_("Update Available"), "detected"),
            InstallationStatus.INSTALLING: (_("Installing..."), "available"),
            InstallationStatus.VERIFYING: (_("Verifying..."), "available"),
            InstallationStatus.CORRUPT: (_("Corrupt"), "error"),
        }
        text, status_key = status_map.get(status, (_("Unknown"), ""))
        self.status_label.setText(text)
        self.status_label.setObjectName("gameSettingsStatus")
        set_status_property(self.status_label, status_key)

        if info:
            self.path_label.setText(str(info.install_path) if info.install_path else "-")
            self.version_label.setText(info.version or "-")
            if info.size_bytes:
                size_gb = info.size_bytes / (1024 ** 3)
                self.size_label.setText(_("{:.2f} GB").format(size_gb))
            else:
                self.size_label.setText("-")

            # Enable action buttons for installed games
            is_installed = status == InstallationStatus.INSTALLED
            self.verify_btn.setEnabled(is_installed)
            self.repair_btn.setEnabled(is_installed)
            self.uninstall_btn.setEnabled(is_installed)
        else:
            self.path_label.setText("-")
            self.version_label.setText("-")
            self.size_label.setText("-")

    def _load_settings(self) -> None:
        """Load existing game settings"""
        game_id = str(self.game.id) if hasattr(self.game, 'id') else None
        if not game_id:
            return

        # Load from game manager if available
        if self.game_manager:
            info = self.game_manager.get_installation_info(game_id)
            if info and info.settings:
                # Platform selection
                platform_id = info.settings.get("platform_id", "auto")
                index = self.platform_combo.findData(platform_id)
                if index >= 0:
                    self.platform_combo.setCurrentIndex(index)

                # Launch arguments
                args = info.settings.get("launch_args", "")
                self.args_edit.setText(args)

                # Environment variables
                env_vars = info.settings.get("environment", {})
                env_text = "\n".join(f"{k}={v}" for k, v in env_vars.items())
                self.env_edit.setPlainText(env_text)
                return

        # Fallback: load from config
        settings = self.config.get(f"game_settings.{game_id}", {})
        if settings:
            platform_id = settings.get("platform_id", "auto")
            index = self.platform_combo.findData(platform_id)
            if index >= 0:
                self.platform_combo.setCurrentIndex(index)

            self.args_edit.setText(settings.get("launch_args", ""))

            env_vars = settings.get("environment", {})
            env_text = "\n".join(f"{k}={v}" for k, v in env_vars.items())
            self.env_edit.setPlainText(env_text)

    def _save_settings(self) -> None:
        """Save current settings"""
        game_id = str(self.game.id) if hasattr(self.game, 'id') else None
        if not game_id:
            return

        # Build settings dict
        settings = {
            "platform_id": self.platform_combo.currentData() or "auto",
            "launch_args": self.args_edit.text().strip(),
            "environment": self._parse_env_vars(),
        }

        # Save via game manager if available
        if self.game_manager:
            self.game_manager.save_game_settings(game_id, settings)
        else:
            # Fallback to config
            self.config.set(f"game_settings.{game_id}", settings)
            self.config.save()

        # Assign platform if specified
        if settings["platform_id"] != "auto" and self.runtime_manager:
            self.runtime_manager.assign_platform(game_id, settings["platform_id"])

        logger.info(f"Saved settings for game {game_id}")
        self.settings_saved.emit(game_id)

    def _save_and_close(self) -> None:
        """Save settings and close dialog"""
        self._save_settings()
        self.accept()

    def _parse_env_vars(self) -> Dict[str, str]:
        """Parse environment variables from text edit"""
        env = {}
        text = self.env_edit.toPlainText().strip()
        for line in text.split("\n"):
            line = line.strip()
            if "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
        return env

    def _verify_installation(self) -> None:
        """Verify game installation"""
        if not self.game_manager:
            return

        QMessageBox.information(
            self,
            _("Verification"),
            _("File verification will be implemented in a future update.")
        )

    def _repair_installation(self) -> None:
        """Repair game installation"""
        if not self.game_manager:
            return

        QMessageBox.information(
            self,
            _("Repair"),
            _("Installation repair will be implemented in a future update.")
        )

    def _uninstall_game(self) -> None:
        """Uninstall the game"""
        if not self.game_manager:
            return

        game_title = getattr(self.game, 'title', _('this game'))
        result = QMessageBox.question(
            self,
            _("Confirm Uninstall"),
            _("Are you sure you want to uninstall {}?\n\n"
              "This will remove the installed game files.").format(game_title),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if result == QMessageBox.StandardButton.Yes:
            QMessageBox.information(
                self,
                _("Uninstall"),
                _("Uninstall will be implemented in a future update.")
            )
