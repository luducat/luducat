# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# settings.py

"""Settings dialog for luducat

Tabbed dialog containing:
- General: Auto-sync, default view, confirmations
- Appearance: UI zoom, theme override, grid density
- Metadata: Per-field metadata priority editor
- Tags: Tag sync settings
- Launching: Centralized launcher/emulator binary config and default store runners
- Plugins: Enable/disable, configure plugins
- Backup: Backup/restore
- Advanced: Paths, cache
- Privacy: Data access consent
"""

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from luducat.core.json_compat import json
from PySide6.QtCore import Qt, Signal, Slot, QMetaObject, QUrl, Q_ARG
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QWidget,
    QLabel,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QSlider,
    QPushButton,
    QGroupBox,
    QFormLayout,
    QGridLayout,
    QDialogButtonBox,
    QMessageBox,
    QFileDialog,
    QLineEdit,
    QFrame,
    QScrollArea,
    QStackedWidget,
    QInputDialog,
    QMenu,
)
from PySide6.QtGui import QDesktopServices

from ...utils.icons import load_tinted_icon
from ...core.config import Config
from ...core.constants import (
    APP_NAME,
    APP_VERSION,
    VIEW_MODE_LIST,
    VIEW_MODE_COVER,
    VIEW_MODE_SCREENSHOT,
    SORT_MODE_NAME,
    SORT_MODE_RECENT,
    SORT_MODE_ADDED,
    GRID_DENSITY_MIN,
    GRID_DENSITY_MAX,
    DEFAULT_GRID_DENSITY,
    DEFAULT_IMAGE_FADE_MS,
    IMAGE_FADE_MIN_MS,
    IMAGE_FADE_MAX_MS,
)
from ...core.plugin_manager import PluginManager

logger = logging.getLogger(__name__)


class BackupProgressDialog(QDialog):
    """Progress dialog with progress bar for backup/restore operations."""

    def __init__(self, title: str, total_items: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
        )
        self._total = total_items
        self._deferred_work = None
        self._backup_folder = None
        self._setup_ui()
        self.setMinimumWidth(520)

    def _setup_ui(self) -> None:
        from PySide6.QtWidgets import QProgressBar, QSizePolicy

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Current item label
        self.item_label = QLabel(_("Preparing..."))
        self.item_label.setObjectName("backupProgressStatus")
        self.item_label.setWordWrap(True)
        layout.addWidget(self.item_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, self._total)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_bar)

        # File path label (shows which ZIP is being written)
        self.path_label = QLabel("")
        self.path_label.setObjectName("hintLabel")
        self.path_label.setWordWrap(True)
        self.path_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self.path_label)

        # Result label (hidden until finish)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setVisible(False)
        layout.addWidget(self.result_label)

        # Button row (hidden until finish)
        from PySide6.QtWidgets import QHBoxLayout
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_open_folder = QPushButton(_("Open Folder"))
        self.btn_open_folder.clicked.connect(self._open_backup_folder)
        self.btn_open_folder.setVisible(False)
        btn_layout.addWidget(self.btn_open_folder)

        self.btn_close = QPushButton(_("Close"))
        self.btn_close.clicked.connect(self.accept)
        self.btn_close.setVisible(False)
        btn_layout.addWidget(self.btn_close)

        layout.addLayout(btn_layout)

    def set_item(self, text: str) -> None:
        """Update the current item label."""
        self.item_label.setText(text)
        QApplication.processEvents()

    def set_path(self, path: str) -> None:
        """Update the file path label."""
        import platform
        if platform.system() != "Windows":
            from pathlib import Path
            home = str(Path.home())
            if path.startswith(home):
                path = "~" + path[len(home):]
        self.path_label.setText(path)
        self.adjustSize()
        QApplication.processEvents()

    def set_progress(self, current: int) -> None:
        """Set progress bar value."""
        self.progress_bar.setValue(current)
        QApplication.processEvents()

    def finish(self, success: bool, message: str,
               show_close: bool = True) -> None:
        """Mark operation as finished."""
        self.progress_bar.setValue(self._total)
        if success:
            self.result_label.setText(message)
            self.result_label.setObjectName("backupProgressSuccess")
        else:
            self.result_label.setText(message)
            self.result_label.setObjectName("backupProgressError")
        self.result_label.style().unpolish(self.result_label)
        self.result_label.style().polish(self.result_label)
        self.result_label.setVisible(True)
        self.item_label.setVisible(False)
        self.path_label.setVisible(False)
        self.btn_open_folder.setVisible(
            show_close and success and self._backup_folder is not None
        )
        self.btn_close.setVisible(show_close)
        self.adjustSize()
        QApplication.processEvents()

    def set_backup_folder(self, folder_path: str) -> None:
        """Set the backup folder path for the Open Folder button."""
        self._backup_folder = folder_path

    def _open_backup_folder(self) -> None:
        """Open the backup folder in the system file manager."""
        if self._backup_folder:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._backup_folder))

    def start_work(self, func) -> None:
        """Register work to run once dialog is fully shown."""
        self._deferred_work = func

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._deferred_work:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._deferred_work)
            self._deferred_work = None


class GeneralSettingsTab(QWidget):
    """General settings tab"""

    def __init__(
        self, config: Config, plugin_manager=None,
        update_info=None, parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config
        self._plugin_manager = plugin_manager
        self._update_info = update_info
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Startup group
        startup_group = QGroupBox(_("Startup"))
        startup_layout = QVBoxLayout(startup_group)

        self.chk_auto_sync = QCheckBox(_("Auto-sync on startup"))
        self.chk_auto_sync.setToolTip(
            _("Automatically check all your stores for new games when luducat starts")
        )
        startup_layout.addWidget(self.chk_auto_sync)

        # Update check row: checkbox left, button right
        update_check_row = QHBoxLayout()

        self.chk_check_updates = QCheckBox(_("Check for updates on startup and after sync"))
        self.chk_check_updates.setToolTip(
            _("Check for new luducat versions on startup and after each sync.\n"
              "Makes a brief, anonymous connection to the update server.")
        )
        update_check_row.addWidget(self.chk_check_updates)
        update_check_row.addStretch()

        self.btn_check_now = QPushButton(_("Check for Update\u2026"))
        self.btn_check_now.setToolTip(
            _("Check whether a newer version of luducat is available right now"))
        self.btn_check_now.clicked.connect(self._on_check_now)
        update_check_row.addWidget(self.btn_check_now)

        startup_layout.addLayout(update_check_row)

        # Language selection row
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel(_("Language:")))
        self.cmb_language = QComboBox()
        self.cmb_language.setToolTip(_("Display language for the user interface"))
        self.cmb_language.addItem(_("System Default"), "")
        from ...core.i18n import get_available_languages
        for code, name in get_available_languages().items():
            self.cmb_language.addItem(name, code)
        lang_row.addWidget(self.cmb_language, 1)
        self._lang_restart_hint = QLabel(_("Requires restart"))
        self._lang_restart_hint.setObjectName("hintLabel")
        self._lang_restart_hint.setVisible(False)
        lang_row.addWidget(self._lang_restart_hint)
        startup_layout.addLayout(lang_row)
        self._initial_language = self.config.get("app.language", "")
        self.cmb_language.currentIndexChanged.connect(self._on_language_changed)

        layout.addWidget(startup_group)

        # Default view group — two-column grid
        view_group = QGroupBox(_("Default View"))
        view_grid = QGridLayout(view_group)

        # Row 0: View mode | Sort by
        view_grid.addWidget(QLabel(_("View mode:")), 0, 0)
        self.cmb_default_view = QComboBox()
        self.cmb_default_view.setToolTip(_("Which view to show when luducat starts"))
        self.cmb_default_view.addItem(_("List"), VIEW_MODE_LIST)
        self.cmb_default_view.addItem(_("Cover Grid"), VIEW_MODE_COVER)
        self.cmb_default_view.addItem(_("Screenshot Grid"), VIEW_MODE_SCREENSHOT)
        view_grid.addWidget(self.cmb_default_view, 0, 1)

        view_grid.addWidget(QLabel(_("Sort by:")), 0, 2)
        self.cmb_default_sort = QComboBox()
        self.cmb_default_sort.setToolTip(_("How your games are sorted when luducat starts"))
        self.cmb_default_sort.addItem(_("Name"), SORT_MODE_NAME)
        self.cmb_default_sort.addItem(_("Recently Played"), SORT_MODE_RECENT)
        self.cmb_default_sort.addItem(_("Date Added"), SORT_MODE_ADDED)
        view_grid.addWidget(self.cmb_default_sort, 0, 3)

        # Row 1: Recently played | Default store
        view_grid.addWidget(QLabel(_("Recently played:")), 1, 0)
        self.cmb_recent_days = QComboBox()
        self.cmb_recent_days.setToolTip(_("How far back the 'Recently Played' filter looks"))
        self.cmb_recent_days.addItem(_("7 days"), 7)
        self.cmb_recent_days.addItem(_("14 days"), 14)
        self.cmb_recent_days.addItem(_("30 days"), 30)
        self.cmb_recent_days.addItem(_("60 days"), 60)
        self.cmb_recent_days.addItem(_("90 days"), 90)
        view_grid.addWidget(self.cmb_recent_days, 1, 1)

        view_grid.addWidget(QLabel(_("Default store:")), 1, 2)
        self.cmb_default_store = QComboBox()
        self.cmb_default_store.setToolTip(
            _("Which store to launch from when a game is owned in multiple stores")
        )
        self._populate_stores()
        view_grid.addWidget(self.cmb_default_store, 1, 3)

        # Row 2: Hint spanning both columns
        store_note = QLabel(
            _("Default store is used for the Play button and context menu "
              "when a game is available in multiple stores.")
        )
        store_note.setObjectName("hintLabel")
        store_note.setWordWrap(True)
        store_note.setMinimumHeight(36)
        view_grid.addWidget(store_note, 2, 0, 1, 4)

        # Give combo columns equal stretch
        view_grid.setColumnStretch(1, 1)
        view_grid.setColumnStretch(3, 1)

        layout.addWidget(view_group)

        # Behavior group
        behavior_group = QGroupBox(_("Behavior"))
        behavior_layout = QVBoxLayout(behavior_group)

        self.chk_confirm_launch = QCheckBox(_("Confirm before launching games"))
        self.chk_confirm_launch.setToolTip(
            _("Ask for confirmation before opening a game in its store launcher")
        )
        behavior_layout.addWidget(self.chk_confirm_launch)

        layout.addWidget(behavior_group)

        layout.addStretch()

        # Update indicator (shown when an update is known, below the groups)
        self._update_link = QLabel()
        self._update_link.setObjectName("updateLink")
        self._update_link.setTextFormat(Qt.TextFormat.RichText)
        self._update_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_link.setVisible(False)
        self._update_link.linkActivated.connect(self._on_update_link_clicked)
        layout.addWidget(self._update_link, 0, Qt.AlignmentFlag.AlignRight)

        if self._update_info:
            self._show_update_indicator(self._update_info.version, self._update_info.changelog)

    def _populate_stores(self) -> None:
        """Populate default store dropdown from enabled store plugins."""
        from ...plugins.base import PluginType
        from ...core.plugin_manager import PluginManager as PM
        if not self._plugin_manager:
            # Fallback: enumerate from class-level registry
            for name in sorted(PM.get_store_plugin_names()):
                self.cmb_default_store.addItem(PM.get_store_display_name(name), name)
            return
        store_plugins = self._plugin_manager.get_plugins_by_type(PluginType.STORE)
        for name in sorted(store_plugins.keys()):
            self.cmb_default_store.addItem(PM.get_store_display_name(name), name)

    def _on_language_changed(self) -> None:
        """Show restart hint when language selection differs from current."""
        selected = self.cmb_language.currentData()
        changed = selected != self._initial_language
        self._lang_restart_hint.setVisible(changed)

    def _load_settings(self) -> None:
        self.chk_auto_sync.setChecked(
            self.config.get("sync.auto_sync_on_startup", False)
        )
        self.chk_check_updates.setChecked(
            self.config.get("app.check_for_updates", False)
        )

        # Language
        saved_lang = self.config.get("app.language", "")
        idx = self.cmb_language.findData(saved_lang)
        if idx >= 0:
            self.cmb_language.setCurrentIndex(idx)

        view_mode = self.config.get("ui.view_mode", VIEW_MODE_LIST)
        idx = self.cmb_default_view.findData(view_mode)
        if idx >= 0:
            self.cmb_default_view.setCurrentIndex(idx)

        sort_mode = self.config.get("ui.sort_mode", SORT_MODE_NAME)
        idx = self.cmb_default_sort.findData(sort_mode)
        if idx >= 0:
            self.cmb_default_sort.setCurrentIndex(idx)

        self.chk_confirm_launch.setChecked(
            self.config.get("ui.confirm_launch", False)
        )

        from ...core.constants import DEFAULT_RECENT_PLAYED_DAYS
        recent_days = self.config.get("ui.recently_played_days", DEFAULT_RECENT_PLAYED_DAYS)
        idx = self.cmb_recent_days.findData(recent_days)
        if idx >= 0:
            self.cmb_recent_days.setCurrentIndex(idx)

        default_store = self.config.get("ui.default_store", "")
        idx = self.cmb_default_store.findData(default_store)
        if idx >= 0:
            self.cmb_default_store.setCurrentIndex(idx)

    def save_settings(self) -> None:
        self.config.set("sync.auto_sync_on_startup", self.chk_auto_sync.isChecked())
        self.config.set("app.check_for_updates", self.chk_check_updates.isChecked())
        self.config.set("app.language", self.cmb_language.currentData())
        self.config.set("ui.view_mode", self.cmb_default_view.currentData())
        self.config.set("ui.sort_mode", self.cmb_default_sort.currentData())
        self.config.set("ui.confirm_launch", self.chk_confirm_launch.isChecked())
        self.config.set("ui.recently_played_days", self.cmb_recent_days.currentData())
        self.config.set("ui.default_store", self.cmb_default_store.currentData())

    def reset_to_defaults(self) -> None:
        """Reset general settings to defaults."""
        self.chk_auto_sync.setChecked(False)
        self.chk_check_updates.setChecked(False)
        idx = self.cmb_language.findData("")
        if idx >= 0:
            self.cmb_language.setCurrentIndex(idx)
        idx = self.cmb_default_view.findData(VIEW_MODE_LIST)
        if idx >= 0:
            self.cmb_default_view.setCurrentIndex(idx)
        idx = self.cmb_default_sort.findData(SORT_MODE_NAME)
        if idx >= 0:
            self.cmb_default_sort.setCurrentIndex(idx)
        self.chk_confirm_launch.setChecked(False)
        from ...core.constants import DEFAULT_RECENT_PLAYED_DAYS
        idx = self.cmb_recent_days.findData(DEFAULT_RECENT_PLAYED_DAYS)
        if idx >= 0:
            self.cmb_recent_days.setCurrentIndex(idx)
        # Reset to first available store (populated from discovered plugins)
        if self.cmb_default_store.count() > 0:
            self.cmb_default_store.setCurrentIndex(0)

    def _on_check_now(self) -> None:
        """Manual update check triggered by user."""
        try:
            from ...core.network_monitor import get_network_monitor
            if not get_network_monitor().is_online:
                QMessageBox.information(
                    self,
                    _("Offline Mode"),
                    _("Cannot check for updates while in offline mode.\n\n"
                      "Switch to online mode using the status bar indicator, "
                      "then try again."),
                )
                return
        except RuntimeError:
            pass

        import threading
        from ...core.update_checker import check_for_update, OfflineError

        self.btn_check_now.setEnabled(False)
        self.btn_check_now.setText(_("Checking\u2026"))

        def _worker():
            try:
                result = check_for_update(self.config, force=True)
            except OfflineError:
                result = "__offline__"
            except Exception:
                result = None
            version_str = ""
            if result == "__offline__":
                version_str = "__offline__"
            elif result is not None:
                self._last_check_result = result
                version_str = result.version
            QMetaObject.invokeMethod(
                self, "_on_check_result",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, version_str),
            )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    @Slot(str)
    def _on_check_result(self, new_version: str) -> None:
        """Handle update check result on main thread."""
        self.btn_check_now.setEnabled(True)
        self.btn_check_now.setText(_("Check for Update\u2026"))

        if new_version == "__offline__":
            QMessageBox.information(
                self,
                _("Offline Mode"),
                _("The network became unavailable during the check.\n\n"
                  "Switch to online mode using the status bar indicator, "
                  "then try again."),
            )
        elif new_version:
            changelog = None
            if hasattr(self, "_last_check_result") and self._last_check_result:
                changelog = self._last_check_result.changelog

                # Propagate UpdateInfo to MainWindow
                settings_dialog = self.window()
                if settings_dialog and settings_dialog.parent():
                    main_window = settings_dialog.parent()
                    if hasattr(main_window, "_pending_update"):
                        main_window._pending_update = self._last_check_result

            self._show_update_indicator(new_version, changelog)
            # Close Settings and trigger the full update dialog in MainWindow
            dialog = self.window()
            if hasattr(dialog, "show_update_requested"):
                dialog.show_update_requested.emit()
                dialog.reject()
        else:
            QMessageBox.information(
                self,
                _("Up to Date"),
                _("You are running the latest version ({version}).").format(version=APP_VERSION),
            )

    def _show_update_indicator(self, version: str, changelog=None) -> None:
        """Show the 'Update available' link below Check Now."""
        self._update_link.setText(
            _("<a href='#update'>Update available: {version}</a>").format(version=version)
        )
        tip = _("Click to update to version {version}").format(version=version)
        if changelog:
            from ...core.news import format_summary_text
            tip = (
                _("Version {version}").format(version=version)
                + "\n\n" + format_summary_text(changelog)
            )
        self._update_link.setToolTip(tip)
        self._update_link.setVisible(True)

    def _on_update_link_clicked(self, _link: str) -> None:
        """Close Settings and request the update dialog from MainWindow."""
        dialog = self.window()
        if hasattr(dialog, "show_update_requested"):
            dialog.show_update_requested.emit()
            dialog.reject()


class AppearanceSettingsTab(QWidget):
    """Appearance settings tab"""

    # Signal emitted when settings require immediate application
    settings_changed = Signal()

    def __init__(self, config: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Theme group
        theme_group = QGroupBox(_("Theme"))
        theme_layout = QVBoxLayout(theme_group)

        theme_row = QFormLayout()
        self.cmb_theme = QComboBox()
        self.cmb_theme.setToolTip(
            _("Choose the look and feel of the application.\n"
              "You can add custom themes by placing .luducat-theme files in the themes folder.")
        )
        self._populate_themes()
        theme_row.addRow(_("Theme:"), self.cmb_theme)
        theme_layout.addLayout(theme_row)

        # Note about custom themes
        from ...core.config import get_config_dir
        themes_path = get_config_dir() / "themes"
        theme_note = QLabel(
            _("Custom themes: Place .luducat-theme or "
              ".qss files in {path}").format(path=themes_path)
        )
        theme_note.setObjectName("hintLabel")
        theme_note.setWordWrap(True)
        theme_layout.addWidget(theme_note)

        layout.addWidget(theme_group)

        # UI Scale group
        scale_group = QGroupBox(_("UI Scale"))
        scale_layout = QVBoxLayout(scale_group)

        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel(_("Zoom:")))

        self.slider_zoom = QSlider(Qt.Orientation.Horizontal)
        self.slider_zoom.setToolTip(
            _("Make everything bigger or smaller. "
              "Needs a restart to apply")
        )
        self.slider_zoom.setMinimum(50)
        self.slider_zoom.setMaximum(400)
        self.slider_zoom.setValue(100)
        self.slider_zoom.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider_zoom.setTickInterval(25)
        scale_row.addWidget(self.slider_zoom)

        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setMinimumWidth(50)
        scale_row.addWidget(self.lbl_zoom)

        self.slider_zoom.valueChanged.connect(
            lambda v: self.lbl_zoom.setText(f"{v}%")
        )

        scale_layout.addLayout(scale_row)

        # Note about zoom requiring restart
        zoom_note = QLabel(_("Note: Zoom changes require restart to take effect."))
        zoom_note.setObjectName("hintLabel")
        scale_layout.addWidget(zoom_note)

        layout.addWidget(scale_group)

        # Images group
        fade_group = QGroupBox(_("Images"))
        fade_layout = QVBoxLayout(fade_group)

        fade_row = QHBoxLayout()
        fade_row.addWidget(QLabel(_("Image fade duration:")))

        self.slider_fade = QSlider(Qt.Orientation.Horizontal)
        self.slider_fade.setToolTip(
            _("How quickly cover and screenshot images fade in.\n"
              "Set to 0 for instant display.")
        )
        self.slider_fade.setMinimum(IMAGE_FADE_MIN_MS)
        self.slider_fade.setMaximum(IMAGE_FADE_MAX_MS)
        self.slider_fade.setValue(DEFAULT_IMAGE_FADE_MS)
        self.slider_fade.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.slider_fade.setTickInterval(50)
        self.slider_fade.setSingleStep(10)
        fade_row.addWidget(self.slider_fade)

        self.lbl_fade = QLabel(_("{v} ms").format(v=DEFAULT_IMAGE_FADE_MS))
        self.lbl_fade.setMinimumWidth(50)
        fade_row.addWidget(self.lbl_fade)

        self.slider_fade.valueChanged.connect(
            lambda v: self.lbl_fade.setText(_("Off") if v == 0 else _("{v} ms").format(v=v))
        )

        fade_layout.addLayout(fade_row)

        scale_row2 = QHBoxLayout()
        scale_row2.addWidget(QLabel(_("Cover scaling:")))
        self.combo_cover_scaling = QComboBox()
        self.combo_cover_scaling.addItem(_("Original proportions (default)"), "none")
        self.combo_cover_scaling.addItem(_("Stretch to fill frame"), "stretch")
        self.combo_cover_scaling.addItem(_("Zoom to fill frame, crop overflow"), "fill")
        self.combo_cover_scaling.setToolTip(
            _("How cover images fit their grid cells.\n"
              "Original keeps the image as-is, Stretch fills the cell,\n"
              "Zoom crops to fill without distortion.")
        )
        scale_row2.addWidget(self.combo_cover_scaling)
        fade_layout.addLayout(scale_row2)

        layout.addWidget(fade_group)

        # Grid Density group
        density_group = QGroupBox(_("Grid Density Defaults"))
        density_layout = QFormLayout(density_group)

        self.spin_cover_density = QSpinBox()
        self.spin_cover_density.setToolTip(
            _("How large cover tiles are. Smaller values fit more games per row")
        )
        self.spin_cover_density.setMinimum(GRID_DENSITY_MIN)
        self.spin_cover_density.setMaximum(GRID_DENSITY_MAX)
        self.spin_cover_density.setSuffix(" px")
        density_layout.addRow(_("Cover view:"), self.spin_cover_density)

        self.spin_screenshot_density = QSpinBox()
        self.spin_screenshot_density.setToolTip(
            _("How large screenshot tiles are. Smaller values fit more games per row")
        )
        self.spin_screenshot_density.setMinimum(GRID_DENSITY_MIN)
        self.spin_screenshot_density.setMaximum(GRID_DENSITY_MAX)
        self.spin_screenshot_density.setSuffix(" px")
        density_layout.addRow(_("Screenshot view:"), self.spin_screenshot_density)

        layout.addWidget(density_group)

        layout.addStretch()

    def _populate_themes(self) -> None:
        """Populate theme dropdown with available themes.

        Uses ThemeManager to get themes in order:
        1. System theme
        2. Variant-based themes (new format)
        3. Legacy QSS themes (backward compatible)
        """
        self.cmb_theme.clear()

        # Get theme manager from main window
        main_window = self.parent()
        while main_window and not hasattr(main_window, "theme_manager"):
            main_window = main_window.parent()

        if main_window and hasattr(main_window, "theme_manager"):
            themes = main_window.theme_manager.get_available_themes()
        else:
            # Fallback: use theme manager directly
            from ...core.theme_manager import ThemeManager
            from PySide6.QtWidgets import QApplication
            # Create temporary manager just to get theme list
            temp_mgr = ThemeManager(QApplication.instance())
            themes = temp_mgr.get_available_themes()

        for theme in themes:
            self.cmb_theme.addItem(theme["name"], theme["id"])

    def _load_settings(self) -> None:
        # Load theme - handle migration from old values
        theme = self.config.get("appearance.theme", "system")

        # Migrate old "auto"/"light"/"dark" values
        if theme in ("auto", "light", "dark"):
            theme = "system"

        # Try to find the theme in dropdown
        idx = self.cmb_theme.findData(theme)

        # If not found and it's a custom: theme, try the variant: equivalent
        if idx < 0 and theme.startswith("custom:"):
            theme_name = theme[7:]  # Remove "custom:" prefix
            # Normalize theme name for variant lookup
            variant_name = theme_name.replace("_", "-").lower()
            variant_theme = f"variant:{variant_name}"
            idx = self.cmb_theme.findData(variant_theme)
            if idx >= 0:
                # Found variant, update config
                theme = variant_theme
                self.config.set("appearance.theme", theme)

        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)

        zoom = self.config.get("appearance.ui_zoom", 100)
        self.slider_zoom.setValue(zoom)
        self.lbl_zoom.setText(f"{zoom}%")

        self.spin_cover_density.setValue(
            self.config.get("appearance.cover_grid_density", DEFAULT_GRID_DENSITY)
        )
        self.spin_screenshot_density.setValue(
            self.config.get("appearance.screenshot_grid_density", DEFAULT_GRID_DENSITY)
        )
        fade_ms = self.config.get("appearance.image_fade_duration", DEFAULT_IMAGE_FADE_MS)
        self.slider_fade.setValue(fade_ms)
        self.lbl_fade.setText(_("Off") if fade_ms == 0 else _("{v} ms").format(v=fade_ms))

        scaling = self.config.get("appearance.cover_scaling", "none")
        idx = self.combo_cover_scaling.findData(scaling)
        if idx >= 0:
            self.combo_cover_scaling.setCurrentIndex(idx)

    def save_settings(self) -> None:
        self.config.set("appearance.theme", self.cmb_theme.currentData())
        self.config.set("appearance.ui_zoom", self.slider_zoom.value())
        self.config.set("appearance.cover_grid_density", self.spin_cover_density.value())
        self.config.set("appearance.screenshot_grid_density", self.spin_screenshot_density.value())
        self.config.set("appearance.image_fade_duration", self.slider_fade.value())
        self.config.set("appearance.cover_scaling", self.combo_cover_scaling.currentData())

    def reset_to_defaults(self) -> None:
        """Reset appearance settings to defaults."""
        from ...core.theme_manager import THEME_SYSTEM
        idx = self.cmb_theme.findData(THEME_SYSTEM)
        if idx >= 0:
            self.cmb_theme.setCurrentIndex(idx)
        self.slider_zoom.setValue(100)
        self.lbl_zoom.setText("100%")
        self.spin_cover_density.setValue(DEFAULT_GRID_DENSITY)
        self.spin_screenshot_density.setValue(DEFAULT_GRID_DENSITY)
        self.slider_fade.setValue(DEFAULT_IMAGE_FADE_MS)
        self.lbl_fade.setText(_("{v} ms").format(v=DEFAULT_IMAGE_FADE_MS))
        self.combo_cover_scaling.setCurrentIndex(0)


class PluginListItem(QWidget):
    """Custom widget for plugin list items"""

    configure_clicked = Signal(str)  # plugin_name

    def __init__(
        self,
        plugin_name: str,
        display_name: str,
        version: str,
        description: str,
        is_enabled: bool,
        is_authenticated: bool,
        plugin_types: Optional[List[str]] = None,
        is_in_development: bool = False,
        is_security_disabled: bool = False,
        no_config_metadata: bool = False,
        is_platform_unsupported: bool = False,
        supported_platforms: Optional[List[str]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)

        self.plugin_name = plugin_name
        self.is_in_development = is_in_development
        self.is_security_disabled = is_security_disabled
        self.is_platform_unsupported = is_platform_unsupported
        self._supported_platforms = supported_platforms
        self._plugin_types = plugin_types or ["store"]

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Enable checkbox
        self.chk_enabled = QCheckBox()
        self.chk_enabled.setChecked(is_enabled)
        self.chk_enabled.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self.chk_enabled)

        self._is_authenticated = is_authenticated

        # Info section
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        # Title row
        title_row = QHBoxLayout()

        # Type badge only for multi-type plugins (visual only)
        if plugin_types and len(plugin_types) > 1:
            type_text = "/".join(t.capitalize() for t in plugin_types)
            type_label = QLabel(f"[{type_text}]")
            type_label.setObjectName("pluginTypeBadge")
            title_row.addWidget(type_label)

        title_label = QLabel(
            _("<b>{name}</b> v{version}").format(
                name=display_name, version=version
            )
        )
        title_row.addWidget(title_label)

        # Status indicator (stored for later updates)
        self.status_label = QLabel()
        self._update_status_display(is_authenticated)
        title_row.addWidget(self.status_label)
        title_row.addStretch()

        info_layout.addLayout(title_row)

        # Description
        desc_label = QLabel(description)
        desc_label.setObjectName("dialogDescription")
        desc_label.setWordWrap(True)
        info_layout.addWidget(desc_label)

        layout.addLayout(info_layout, 1)

        # Configure button — hidden for:
        #   - in-dev platform/runner plugins
        #   - security-disabled plugins
        #   - metadata plugins with nothing to configure
        self.btn_configure = QPushButton(_("Configure"))
        self.btn_configure.clicked.connect(
            lambda: self.configure_clicked.emit(self.plugin_name)
        )
        is_platform = "platform" in self._plugin_types
        is_runner = "runner" in self._plugin_types
        hide_configure = (
            ((is_platform or is_runner) and is_in_development)
            or is_runner  # runners configured via Launchers tab
            or is_security_disabled
            or no_config_metadata
            or is_platform_unsupported
        )
        self._hide_configure = hide_configure
        if not hide_configure:
            layout.addWidget(self.btn_configure)

    def is_enabled(self) -> bool:
        return self.chk_enabled.isChecked()

    def _update_status_display(self, is_authenticated: bool) -> None:
        """Update the status label display"""
        is_store = "store" in self._plugin_types

        if self.is_security_disabled:
            self.status_label.setText(_("⚠ Disabled (integrity)"))
            self.status_label.setObjectName("pluginStatusDisabled")
        elif self.is_platform_unsupported:
            platform_map = {"linux": "Linux", "windows": "Windows", "darwin": "macOS"}
            if self._supported_platforms:
                names = ", ".join(
                    platform_map.get(p, p) for p in self._supported_platforms
                )
                self.status_label.setText(
                    _("— {platforms} only").format(platforms=names)
                )
            else:
                self.status_label.setText(_("— Not available"))
            self.status_label.setObjectName("pluginStatusNotAvailable")
        elif self.is_in_development:
            self.status_label.setText(_("⚠ In development"))
            self.status_label.setObjectName("pluginStatusInDevelopment")
        elif not self.is_enabled():
            self.status_label.setText(_("— Disabled"))
            self.status_label.setObjectName("pluginStatusNotAvailable")
        elif is_authenticated:
            if is_store:
                self.status_label.setText(_("✓ Logged in"))
            else:
                self.status_label.setText(_("✓ Configured"))
            self.status_label.setObjectName("pluginStatusConfigured")
        else:
            if is_store:
                self.status_label.setText(_("⚠ Not logged in"))
            else:
                self.status_label.setText(_("⚠ Not configured"))
            self.status_label.setObjectName("pluginStatusNotConfigured")
        # Force style refresh
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def update_status(self, is_authenticated: bool) -> None:
        """Update the plugin's connection status"""
        self._is_authenticated = is_authenticated
        self._update_status_display(is_authenticated)

    def _on_enabled_toggled(self, checked: bool) -> None:
        """Refresh status display when enable checkbox changes."""
        self._update_status_display(self._is_authenticated)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-click opens config dialog (or prompts to enable first)."""
        if self.is_in_development or self.is_security_disabled or self._hide_configure:
            return
        if not self.chk_enabled.isChecked():
            reply = QMessageBox.question(
                self,
                _("Plugin disabled"),
                _("Enable this plugin first?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self.chk_enabled.setChecked(True)
        self.configure_clicked.emit(self.plugin_name)


class PluginsSettingsTab(QWidget):
    """Plugins settings tab with sidebar category navigation."""

    configure_plugin = Signal(str)  # plugin_name

    # Plugin categories: (type_key, display_label, icon_filename)
    _PLUGIN_CATEGORIES = [
        ("store", N_("Library"), "plug-store.svg"),
        ("metadata", N_("Metadata"), "plug-metadata.svg"),
        ("runner", N_("Runners"), "plug-runner.svg"),
        ("platform", N_("Platforms"), "plug-platform.svg"),
    ]

    def __init__(
        self,
        config: Config,
        plugin_manager: PluginManager,
        game_service=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager
        self.game_service = game_service
        self.reenabled_stores: List[str] = []
        self._plugin_items: Dict[str, PluginListItem] = {}
        self._panel_layouts: Dict[str, QVBoxLayout] = {}
        self._setup_ui()
        self._load_plugins()

    def _setup_ui(self) -> None:
        from luducat.ui.widgets.category_sidebar import CategorySidebar

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header
        header = QLabel(_("Manage plugins (game stores and metadata providers)"))
        header.setObjectName("dialogDescription")
        layout.addWidget(header)

        # Main content: sidebar + stacked panels
        content_layout = QHBoxLayout()
        content_layout.setSpacing(8)

        # Left: category sidebar + reload button in bordered container
        left_container = QWidget()
        left_container.setObjectName("prioritySidebarPanel")
        left_container.setFixedWidth(182)
        sidebar_layout = QVBoxLayout(left_container)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(4)

        self._sidebar = CategorySidebar()
        for type_key, label, icon in self._PLUGIN_CATEGORIES:
            self._sidebar.add_category(type_key, _(label), icon)
        self._sidebar.currentChanged.connect(self._on_category_changed)
        sidebar_layout.addWidget(self._sidebar, 1)

        # Reload button below sidebar
        self.btn_reload = QPushButton(_("Reload Plugins"))
        self.btn_reload.setToolTip(_("Look for newly installed or updated plugins"))
        self.btn_reload.clicked.connect(self._reload_plugins)
        sidebar_layout.addWidget(self.btn_reload)

        content_layout.addWidget(left_container)

        # Right: stacked widget with scroll areas per category
        self._stack = QStackedWidget()

        for type_key, label, _icon in self._PLUGIN_CATEGORIES:
            # Each category gets a scroll area with a vertical layout
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.StyledPanel)

            container = QWidget()
            container_layout = QVBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(1)
            self._panel_layouts[type_key] = container_layout

            scroll.setWidget(container)
            self._stack.addWidget(scroll)

        content_layout.addWidget(self._stack, 1)
        layout.addLayout(content_layout, 1)

        # Select first category
        self._sidebar.select_category("store")

    @staticmethod
    def _get_primary_type(metadata) -> str:
        """Get primary plugin type for sidebar categorization.

        Store plugins that also declare metadata/runner types
        are shown only under Game Stores (their primary identity).
        """
        if "store" in metadata.plugin_types:
            return "store"
        if "metadata" in metadata.plugin_types:
            return "metadata"
        if "runner" in metadata.plugin_types:
            return "runner"
        if "platform" in metadata.plugin_types:
            return "platform"
        return "store"  # fallback

    def _load_plugins(self) -> None:
        """Load plugin list into categorized stacked panels."""
        # Clear existing widgets from all panels
        for item in self._plugin_items.values():
            item.setParent(None)
            item.deleteLater()
        self._plugin_items.clear()

        for type_key, panel_layout in self._panel_layouts.items():
            while panel_layout.count() > 0:
                child = panel_layout.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()

        # Group plugins by primary type
        discovered = self.plugin_manager.get_discovered_plugins()
        grouped: Dict[str, List[tuple]] = {k: [] for k, _lbl, _icn in self._PLUGIN_CATEGORIES}

        for name, metadata in discovered.items():
            primary = self._get_primary_type(metadata)
            if primary in grouped:
                grouped[primary].append((name, metadata))

        # Sort each group alphabetically by display name
        for group in grouped.values():
            group.sort(key=lambda x: x[1].display_name.lower())

        # Populate each panel
        for type_key, plugins in grouped.items():
            panel_layout = self._panel_layouts[type_key]

            if not plugins:
                # Empty category hint
                category_label = dict(
                    (k, _(lbl)) for k, lbl, _icn in self._PLUGIN_CATEGORIES
                ).get(type_key, type_key)
                hint = QLabel(
                    _("No {category} plugins installed yet.")
                    .format(category=category_label.lower())
                )
                hint.setObjectName("hintLabel")
                hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
                hint.setContentsMargins(0, 40, 0, 0)
                panel_layout.addWidget(hint)
                panel_layout.addStretch()
                continue

            for name, metadata in plugins:
                plugin_settings = self.config.get_plugin_settings(name)
                is_enabled = plugin_settings.get("enabled", True)

                loaded = self.plugin_manager._loaded.get(name)

                # Detect security-disabled plugins (integrity verification failed)
                is_security_disabled = False
                if loaded and not loaded.enabled and loaded.error:
                    error_lower = str(loaded.error).lower()
                    if (
                        "integrity" in error_lower
                        or "fingerprint" in error_lower
                        or "trust" in error_lower
                    ):
                        is_security_disabled = True

                is_auth = False
                auth_info = getattr(metadata, "auth", {}) or {}
                if is_security_disabled:
                    is_auth = False
                elif auth_info.get("type") == "none":
                    is_auth = True
                elif loaded and loaded.instance:
                    try:
                        if (
                            hasattr(loaded.instance, 'is_authenticated')
                            and callable(loaded.instance.is_authenticated)
                        ):
                            is_auth = loaded.instance.is_authenticated()
                        elif hasattr(loaded.instance, 'is_available'):
                            # Only use is_available() for plugins without
                            # is_authenticated() — is_available() checks
                            # dependencies, not login state
                            result = loaded.instance.is_available()
                            if callable(result):
                                is_auth = result()
                            else:
                                is_auth = bool(result)
                    except Exception:
                        pass

                capabilities = metadata.capabilities or {}
                is_in_dev = capabilities.get("status") in ("stub", "in_development")

                # Detect plugins with nothing to configure
                is_metadata = "metadata" in (metadata.plugin_types or [])
                is_platform = "platform" in (metadata.plugin_types or [])
                is_runner = "runner" in (metadata.plugin_types or [])
                has_config = (
                    bool(metadata.settings_schema)
                    or auth_info.get("type") not in ("none", "")
                    or bool(metadata.config_actions)
                    or bool(metadata.config_dialog_class)
                )
                no_config_plugin = (
                    (is_metadata or is_platform or is_runner)
                    and not has_config
                )

                # Detect runner plugins on unsupported platforms
                import sys as _sys
                platforms = capabilities.get("platforms", [])
                _plat_current = {"win32": "windows", "darwin": "darwin"}.get(
                    _sys.platform, "linux"
                )
                is_platform_unsupported = (
                    is_runner and bool(platforms) and _plat_current not in platforms
                )

                item = PluginListItem(
                    plugin_name=name,
                    display_name=metadata.display_name,
                    version=metadata.version,
                    description=metadata.description,
                    is_enabled=is_enabled,
                    is_authenticated=is_auth,
                    plugin_types=metadata.plugin_types,
                    is_in_development=is_in_dev,
                    is_security_disabled=is_security_disabled,
                    no_config_metadata=no_config_plugin,
                    is_platform_unsupported=is_platform_unsupported,
                    supported_platforms=platforms if is_platform_unsupported else None,
                )
                item.configure_clicked.connect(self.configure_plugin.emit)

                panel_layout.addWidget(item)
                self._plugin_items[name] = item

            panel_layout.addStretch()

        self._update_sidebar_counts()

    def _update_sidebar_counts(self) -> None:
        """Update sidebar labels with enabled/total counts per category."""
        discovered = self.plugin_manager.get_discovered_plugins()

        # Count plugins per category
        for idx, (type_key, base_label, _icon) in enumerate(self._PLUGIN_CATEGORIES):
            total = 0
            enabled = 0
            for name, metadata in discovered.items():
                if self._get_primary_type(metadata) == type_key:
                    total += 1
                    item = self._plugin_items.get(name)
                    if item and item.is_enabled():
                        enabled += 1
            label = _("{label} ({enabled}/{total})").format(
                label=_(base_label),
                enabled=enabled, total=total,
            )
            list_item = self._sidebar._list.item(idx)
            if list_item:
                list_item.setText(label)

    def _on_category_changed(self, key: str) -> None:
        """Switch stacked widget to the selected category panel."""
        for idx, (type_key, _lbl, _icn) in enumerate(self._PLUGIN_CATEGORIES):
            if type_key == key:
                self._stack.setCurrentIndex(idx)
                break

    def _reload_plugins(self) -> None:
        """Reload plugins from disk."""
        self.plugin_manager.discover_plugins()
        self._load_plugins()

    def update_plugin_status(self, plugin_name: str, is_authenticated: bool) -> None:
        """Update a specific plugin's connection status in the list

        Called when connection test succeeds in the config dialog.
        """
        item = self._plugin_items.get(plugin_name)
        if item:
            item.update_status(is_authenticated)

    def save_settings(self) -> None:
        """Save plugin enabled states with confirmation on disable"""
        self.reenabled_stores = []
        discovered = self.plugin_manager.get_discovered_plugins()

        for name, item in self._plugin_items.items():
            enabled = item.is_enabled()

            # Check previous state
            loaded = self.plugin_manager._loaded.get(name)
            was_enabled = loaded.enabled if loaded else False
            metadata = discovered.get(name)
            is_store = "store" in (metadata.plugin_types if metadata else [])

            # Newly disabled store plugin - confirm with user
            if was_enabled and not enabled and is_store:
                if not self._confirm_disable_plugin(name):
                    item.chk_enabled.setChecked(True)
                    continue

            # Newly disabled non-store plugin — clear credentials silently
            if was_enabled and not enabled and not is_store:
                self._clear_plugin_credentials(name)

            self.config.set(f"plugins.{name}.enabled", enabled)

            if enabled:
                self.plugin_manager.enable_plugin(name)
                # Track re-enabled store plugins for auto-sync
                if not was_enabled and is_store:
                    self.reenabled_stores.append(name)
            else:
                self.plugin_manager.disable_plugin(name)

    def _confirm_disable_plugin(self, plugin_name: str) -> bool:
        """Show confirmation dialog when disabling a store plugin.

        Returns:
            True if user confirms, False if cancelled.
        """
        discovered = self.plugin_manager.get_discovered_plugins()
        metadata = discovered.get(plugin_name)
        display_name = metadata.display_name if metadata else plugin_name

        # Count games that would be hidden
        game_count = 0
        if self.game_service:
            game_count = self.game_service.count_store_exclusive_games(plugin_name)

        msg = _("Disable {name}?\n\n").format(name=display_name)
        if game_count > 0:
            msg += (
                _("{count} game(s) that exist only in {name} "
                  "will be hidden from the library.\n\n")
                .format(count=game_count, name=display_name)
            )
        else:
            msg += (
                _("No games will be hidden (all {name} games "
                  "also exist in other enabled stores).\n\n").format(name=display_name)
            )
        msg += (
            _("Games and user data (favorites, tags) are preserved "
              "and will reappear when the plugin is re-enabled.\n\n"
              "Stored credentials will be cleared. "
              "You will need to log in again when re-enabling.")
        )

        dialog = QMessageBox(self)
        dialog.setWindowTitle(_("Disable {name}").format(name=display_name))
        dialog.setText(msg)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)

        chk_keep_creds = QCheckBox(_("Keep stored credentials"))
        dialog.setCheckBox(chk_keep_creds)

        result = dialog.exec()

        if result == QMessageBox.StandardButton.Ok:
            if not chk_keep_creds.isChecked():
                self._clear_plugin_credentials(plugin_name)
            return True
        return False

    def _clear_plugin_credentials(self, plugin_name: str) -> None:
        """Clear all credentials for a plugin."""
        discovered = self.plugin_manager.get_discovered_plugins()
        metadata = discovered.get(plugin_name)
        if not metadata:
            return

        # Get credential keys from settings schema
        schema = metadata.settings_schema or {}
        secret_keys = [
            key for key, field_def in schema.items()
            if field_def.get("secret", False)
        ]

        cred_manager = self.plugin_manager.credential_manager
        if cred_manager:
            cred_manager.clear_plugin_credentials(plugin_name, secret_keys)
            logger.info(f"Cleared credentials for {plugin_name}")


class AdvancedSettingsTab(QWidget):
    """Advanced settings tab"""

    def __init__(self, config: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Browser Selection group
        browser_group = QGroupBox(_("Browser Selection"))
        browser_layout = QFormLayout(browser_group)

        self.cmb_browser = QComboBox()
        self.cmb_browser.setToolTip(
            _("Which browser to read your store login sessions from.\n"
              "Auto-detect tries all supported browsers in order.\n\n"
              "Store plugins use this browser for reading game store cookies "
              "to authenticate. Cookies are read-only, never modified or stored.")
        )
        self.cmb_browser.addItem(_("Auto-detect"), "auto")
        self._populate_browsers()
        browser_layout.addRow(_("Browser to use:"), self.cmb_browser)

        layout.addWidget(browser_group)

        # Paths group with health indicators
        paths_group = QGroupBox(_("Data Locations"))
        paths_layout = QFormLayout(paths_group)

        from ...core.config import get_config_dir, get_data_dir, get_cache_dir

        self._config_path_row = self._create_path_row(get_config_dir(), movable=False)
        paths_layout.addRow(_("Config:"), self._config_path_row)
        self._data_path_row = self._create_path_row(
            get_data_dir(), movable=True, config_key="app.custom_data_dir")
        paths_layout.addRow(_("Data:"), self._data_path_row)
        self._cache_path_row = self._create_path_row(
            get_cache_dir(), movable=True, config_key="app.custom_cache_dir")
        paths_layout.addRow(_("Cache:"), self._cache_path_row)

        layout.addWidget(paths_group)

        # Cache group (RAM budget + disk cache controls)
        cache_group = QGroupBox(_("Cache"))
        cache_layout = QVBoxLayout(cache_group)

        # Mode row
        mode_row = QHBoxLayout()
        mode_label = QLabel(_("Mode:"))
        self.cmb_ram_cache_mode = QComboBox()
        self.cmb_ram_cache_mode.addItem(_("Automatic"), "auto")
        self.cmb_ram_cache_mode.addItem(_("Manual"), "manual")
        self.cmb_ram_cache_mode.setToolTip(
            _("Automatic computes the optimal budget from your grid density\n"
              "and window size. Manual lets you set a fixed budget.")
        )
        self.cmb_ram_cache_mode.currentIndexChanged.connect(self._on_ram_cache_mode_changed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.cmb_ram_cache_mode)
        mode_row.addStretch()
        cache_layout.addLayout(mode_row)

        # Slider row
        slider_row = QHBoxLayout()
        budget_label = QLabel(_("Budget:"))
        slider_row.addWidget(budget_label)
        self.sld_ram_cache = QSlider(Qt.Orientation.Horizontal)
        self.sld_ram_cache.setMinimum(64)
        self.sld_ram_cache.setMaximum(2048)
        self.sld_ram_cache.setTickInterval(256)
        self.sld_ram_cache.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sld_ram_cache.setSingleStep(16)
        self.sld_ram_cache.setPageStep(64)
        self.sld_ram_cache.setToolTip(
            _("Maximum memory for cached cover and screenshot images (64\u2013\u20092048 MB)")
        )
        self.sld_ram_cache.valueChanged.connect(self._on_ram_cache_slider_changed)
        slider_row.addWidget(self.sld_ram_cache, 1)
        self.lbl_ram_cache_value = QLabel("0 MB")
        self.lbl_ram_cache_value.setMinimumWidth(60)
        slider_row.addWidget(self.lbl_ram_cache_value)
        cache_layout.addLayout(slider_row)

        # Info label
        self.lbl_ram_cache_info = QLabel()
        self.lbl_ram_cache_info.setObjectName("hintLabel")
        self.lbl_ram_cache_info.setWordWrap(True)
        cache_layout.addWidget(self.lbl_ram_cache_info)

        # Disk cache controls (moved from Data Locations)
        cache_layout.addSpacing(8)

        self.chk_offline_mode = QCheckBox(_("Keep cached images indefinitely"))
        self.chk_offline_mode.setToolTip(
            _("Keep all downloaded images on disk permanently.\n"
              "Useful for browsing your library without an internet connection.\n"
              "You can still clear the cache manually at any time.")
        )
        cache_layout.addWidget(self.chk_offline_mode)

        self.btn_clear_cache = QPushButton(_("Clear Cache..."))
        self.btn_clear_cache.setToolTip(
            _("Delete all downloaded cover art and screenshots.\n"
              "They will be re-downloaded automatically when needed.")
        )
        self.btn_clear_cache.clicked.connect(self._clear_cache)
        cache_layout.addWidget(self.btn_clear_cache)

        layout.addWidget(cache_group, 1)  # stretch factor 1 to fill space

        # Content Filter group (at the bottom)
        content_group = QGroupBox(_("Content Filter"))
        content_layout = QVBoxLayout(content_group)

        self.chk_content_filter = QCheckBox(_("Hide adult-rated games"))
        self.chk_content_filter.setToolTip(
            _("Hide games flagged as adult content based on age ratings\n"
              "and content descriptors from Steam, IGDB, and other sources.\n\n"
              "Uses confidence scoring from Steam content descriptors, IGDB age "
              "ratings, and other sources. Games rated Adults Only or with explicit "
              "content descriptors are hidden by default.")
        )
        content_layout.addWidget(self.chk_content_filter)

        layout.addWidget(content_group)

    def _compute_auto_budgets(self) -> Tuple[int, int]:
        """Compute optimal and minimum RAM cache budgets from current config."""
        from ...utils.image_cache import compute_auto_budgets
        cover_density = self.config.get("appearance.cover_grid_density", 250)
        screenshot_density = self.config.get("appearance.screenshot_grid_density", 250)
        viewport_w = self.config.get("ui.window_width", 1200)
        viewport_h = self.config.get("ui.window_height", 800)
        return compute_auto_budgets(
            cover_density, screenshot_density, viewport_w, viewport_h
        )

    def _update_ram_cache_info(self) -> None:
        """Update the info label and slider state for current mode."""
        optimal, minimum = self._compute_auto_budgets()
        is_auto = self.cmb_ram_cache_mode.currentData() == "auto"

        self.sld_ram_cache.setEnabled(not is_auto)
        if is_auto:
            self.sld_ram_cache.setValue(optimal)

        self.lbl_ram_cache_info.setText(
            _("Recommended: {optimal} MB  ·  Minimum: {minimum} MB").format(
                optimal=optimal, minimum=minimum
            )
            + "\n"
            + _("Based on current grid density and window size. "
                "Hero and description caches use a fixed 50 MB.")
        )

    def _on_ram_cache_mode_changed(self, _index: int) -> None:
        """Handle mode combo change."""
        self._update_ram_cache_info()

    def _on_ram_cache_slider_changed(self, value: int) -> None:
        """Handle slider value change."""
        self.lbl_ram_cache_value.setText(_("{v} MB").format(v=value))

    def _populate_browsers(self) -> None:
        """Populate browser dropdown with available browsers."""
        from ...core.browser_cookies import SUPPORTED_BROWSERS
        for display_name, config_key, _ in SUPPORTED_BROWSERS:
            self.cmb_browser.addItem(display_name, config_key)

    def _load_settings(self) -> None:
        self.chk_offline_mode.setChecked(
            self.config.get("cache.offline_mode", True)  # Default: offline mode enabled
        )
        browser = self.config.get("sync.preferred_browser", "auto")
        idx = self.cmb_browser.findData(browser)
        if idx >= 0:
            self.cmb_browser.setCurrentIndex(idx)
        self.chk_content_filter.setChecked(
            self.config.get("content_filter.enabled", True)
        )

        # RAM cache settings
        mode = self.config.get("cache.ram_cache_mode", "auto")
        mode_idx = self.cmb_ram_cache_mode.findData(mode)
        if mode_idx >= 0:
            self.cmb_ram_cache_mode.setCurrentIndex(mode_idx)
        manual_mb = self.config.get("cache.ram_cache_manual_mb", 0)
        if manual_mb > 0 and mode == "manual":
            self.sld_ram_cache.setValue(manual_mb)
        self._update_ram_cache_info()

    def save_settings(self) -> None:
        self.config.set("cache.offline_mode", self.chk_offline_mode.isChecked())
        self.config.set("sync.preferred_browser", self.cmb_browser.currentData())
        self.config.set("content_filter.enabled", self.chk_content_filter.isChecked())

        # RAM cache settings
        mode = self.cmb_ram_cache_mode.currentData()
        self.config.set("cache.ram_cache_mode", mode)
        if mode == "manual":
            self.config.set("cache.ram_cache_manual_mb", self.sld_ram_cache.value())
        else:
            self.config.set("cache.ram_cache_manual_mb", 0)

    def reset_to_defaults(self) -> None:
        """Reset advanced settings to defaults."""
        self.chk_offline_mode.setChecked(True)  # Offline mode default is True
        idx = self.cmb_browser.findData("auto")
        if idx >= 0:
            self.cmb_browser.setCurrentIndex(idx)
        self.chk_content_filter.setChecked(True)  # Content filter default is enabled
        # RAM cache: reset to automatic
        auto_idx = self.cmb_ram_cache_mode.findData("auto")
        if auto_idx >= 0:
            self.cmb_ram_cache_mode.setCurrentIndex(auto_idx)
        self._update_ram_cache_info()

    def _create_path_row(self, path, movable: bool = False,
                          config_key: str = "") -> QWidget:
        """Create a path display row with health indicator,
        open, optional move button, and stats."""
        from pathlib import Path
        from ...core.directory_health import check_directory
        path = Path(path)

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        # Health indicator dot
        health_dot = QLabel()
        health_dot.setFixedSize(12, 12)
        self._update_health_dot(health_dot, path)
        row.addWidget(health_dot)

        path_edit = QLineEdit(str(path))
        path_edit.setReadOnly(True)
        path_edit.setMaximumWidth(300)
        row.addWidget(path_edit, 1)

        btn_open = QPushButton()
        btn_open.setIcon(load_tinted_icon("folder-open.svg", 16))
        btn_open.setFixedSize(28, 28)
        btn_open.setToolTip(_("Open folder in file browser"))
        btn_open.clicked.connect(lambda checked=False, p=path: self._open_path(p))
        row.addWidget(btn_open)

        if movable and config_key:
            btn_move = QPushButton()
            btn_move.setIcon(load_tinted_icon("folder-move.svg", 16))
            btn_move.setFixedSize(28, 28)
            btn_move.setToolTip(_("Move this directory to a new location"))
            btn_move.clicked.connect(
                lambda checked=False, p=path, k=config_key: self._move_directory(p, k))
            row.addWidget(btn_move)
        else:
            # Spacer matching Move button width for alignment
            spacer = QWidget()
            spacer.setFixedWidth(32)  # 28 + spacing
            row.addWidget(spacer)

        # Inline stats (right-justified)
        health = check_directory(path)
        stats_label = QLabel(
            _("{used} MB used, {free} free").format(
                used=health.used_mb,
                free=self._format_size_mb(health.free_mb),
            )
        )
        stats_label.setObjectName("hintLabel")
        stats_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        stats_label.setMinimumWidth(180)
        row.addWidget(stats_label)

        return container

    def _update_health_dot(self, label: QLabel, path) -> None:
        """Set health dot color and tooltip based on directory health."""
        from pathlib import Path
        from ...core.directory_health import check_directory
        path = Path(path)

        health = check_directory(path)
        label.setObjectName("healthDot")
        label.setProperty("health", health.status)
        label.style().unpolish(label)
        label.style().polish(label)
        label.setToolTip(health.tooltip)

    @staticmethod
    def _format_size_mb(mb: int) -> str:
        if mb >= 1024:
            return f"{mb / 1024:.1f} GB"
        return f"{mb} MB"

    def _move_directory(self, current_path, config_key: str) -> None:
        """Show move directory dialog and perform migration."""
        from pathlib import Path
        from ...core.directory_health import check_directory, get_dir_size
        current_path = Path(current_path)

        target = QFileDialog.getExistingDirectory(
            self,
            _("Select New Location"),
            str(current_path.parent),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not target:
            return
        target_path = Path(target)

        # Validate
        if target_path == current_path:
            QMessageBox.warning(self, _("Same Location"),
                                _("The selected directory is the same as the current one."))
            return

        target_health = check_directory(target_path)
        if not target_health.reachable or not target_health.writable:
            QMessageBox.warning(
                self, _("Invalid Target"),
                _("Target is not writable:\n{error}")
                .format(error=target_health.error),
            )
            return

        # Check space (need 1.1x current size)
        current_size = get_dir_size(current_path)
        required_mb = int(current_size * 1.1 / (1024 * 1024))
        if target_health.free_mb < required_mb:
            QMessageBox.warning(
                self, _("Insufficient Space"),
                _("Target has {free} free, "
                  "but {required} is needed.").format(
                    free=self._format_size_mb(target_health.free_mb),
                    required=self._format_size_mb(required_mb))
            )
            return

        # Check target is empty or a luducat dir
        if target_path.exists() and any(target_path.iterdir()):
            # Look for luducat markers (games.db, plugins/, covers/)
            markers = ["games.db", "plugins", "covers", "screenshots"]
            has_marker = any((target_path / m).exists() for m in markers)
            if not has_marker:
                QMessageBox.warning(
                    self, _("Non-Empty Target"),
                    _("The target directory is not empty and doesn't appear to be\n"
                      "a luducat directory. Please choose an empty directory.")
                )
                return

        # Confirm
        size_str = self._format_size_mb(int(current_size / (1024 * 1024)))
        reply = QMessageBox.question(
            self, _("Confirm Move"),
            _("Move directory contents?\n\n"
              "From: {source}\n"
              "To: {target}\n"
              "Size: {size}\n\n"
              "The application will need to be restarted after the move.").format(
                source=current_path, target=target_path, size=size_str),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Perform migration with progress dialog
        from ...core.directory_migrate import DirectoryMigration

        migration = DirectoryMigration(self)
        progress_dialog = QDialog(self)
        progress_dialog.setWindowTitle(_("Moving Directory..."))
        progress_dialog.setModal(True)
        progress_dialog.setMinimumWidth(400)

        dlg_layout = QVBoxLayout(progress_dialog)
        progress_label = QLabel(_("Preparing..."))
        dlg_layout.addWidget(progress_label)

        from PySide6.QtWidgets import QProgressBar
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        dlg_layout.addWidget(progress_bar)

        btn_cancel = QPushButton(_("Cancel"))
        dlg_layout.addWidget(btn_cancel)
        btn_cancel.clicked.connect(migration.cancel)

        def on_progress(current, total, filename):
            progress_bar.setRange(0, total)
            progress_bar.setValue(current)
            progress_label.setText(_("Copying: {filename}").format(filename=filename))

        def on_finished(success, message):
            progress_dialog.accept()
            if success:
                # Offer to delete old location
                reply = QMessageBox.question(
                    self, _("Migration Complete"),
                    _("{message}\n\n"
                      "Delete the old directory?\n"
                      "(The application needs to be restarted.)").format(message=message),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    import shutil
                    try:
                        shutil.rmtree(current_path)
                    except OSError as e:
                        QMessageBox.warning(
                            self, _("Cleanup Failed"),
                            _("Could not delete old "
                              "directory:\n{error}")
                            .format(error=e),
                        )
                QMessageBox.information(
                    self, _("Restart Required"),
                    _("Please restart luducat for the change to take effect.")
                )
            else:
                QMessageBox.critical(self, _("Migration Failed"), message)

        migration.progress.connect(on_progress)
        migration.finished.connect(on_finished)

        # Run migration in a thread
        from PySide6.QtCore import QThread

        class MigrationThread(QThread):
            def __init__(self, migration_obj, src, tgt, key):
                super().__init__()
                self._migration = migration_obj
                self._src = src
                self._tgt = tgt
                self._key = key

            def run(self):
                self._migration.migrate(self._src, self._tgt, self._key)

        thread = MigrationThread(migration, current_path, target_path, config_key)
        thread.start()
        progress_dialog.exec()
        thread.wait()

    def _open_path(self, path) -> None:
        """Open path in system file browser"""
        from pathlib import Path
        path = Path(path)

        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.warning(
                self,
                _("Path Not Found"),
                _("The path does not exist:\n{path}").format(path=path)
            )

    def _clear_cache(self) -> None:
        """Clear all cached images from disk and memory."""
        from pathlib import Path
        import shutil
        from ...core.config import get_cache_dir
        from ...utils.image_cache import get_cover_cache, get_screenshot_cache

        cache_dir = get_cache_dir()

        # Calculate current cache size
        def get_dir_size(path: Path) -> int:
            """Get total size of directory in bytes."""
            total = 0
            if path.exists():
                for f in path.rglob("*"):
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
            return total

        cache_subdirs = ["covers", "screenshots", "plugins"]
        total_size = 0
        for subdir in cache_subdirs:
            total_size += get_dir_size(cache_dir / subdir)

        # Format size for display
        if total_size < 1024:
            size_str = f"{total_size} bytes"
        elif total_size < 1024 * 1024:
            size_str = f"{total_size / 1024:.1f} KB"
        elif total_size < 1024 * 1024 * 1024:
            size_str = f"{total_size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"

        reply = QMessageBox.question(
            self,
            _("Clear Cache"),
            _("This will delete all cached images ({size}).\n\n"
              "Covers and screenshots will be re-downloaded as needed.\n\n"
              "Are you sure?").format(size=size_str),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Clear in-memory caches first
        try:
            get_cover_cache().clear_memory_cache()
            get_screenshot_cache().clear_memory_cache()
        except Exception as e:
            logger.warning(f"Failed to clear memory cache: {e}")

        # Clear disk cache
        errors = []
        cleared_count = 0
        for subdir in cache_subdirs:
            subdir_path = cache_dir / subdir
            if subdir_path.exists():
                try:
                    # Remove all files in the directory but keep the directory
                    for item in subdir_path.iterdir():
                        if item.is_file():
                            item.unlink()
                            cleared_count += 1
                        elif item.is_dir():
                            shutil.rmtree(item)
                            cleared_count += 1
                except Exception as e:
                    errors.append(f"{subdir}: {e}")
                    logger.error(f"Failed to clear cache directory {subdir}: {e}")

        if errors:
            QMessageBox.warning(
                self,
                _("Cache Partially Cleared"),
                _("Some cache directories could not be fully cleared:\n\n")
                + "\n".join(errors)
            )
        else:
            QMessageBox.information(
                self,
                _("Cache Cleared"),
                _("Cache has been cleared ({size} freed).\n\n"
                  "Images will be re-downloaded as you browse.").format(size=size_str)
            )

class BackupSettingsTab(QWidget):
    """Backup & Restore settings tab with scheduling and GFS retention."""

    restart_required = Signal()  # Emitted when restore completes

    def __init__(self, config: Config, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self._retention_acknowledged = False
        self._setup_ui()
        self._load_settings()
        self._update_status_display()

    def _setup_ui(self) -> None:
        # Use scroll area to handle overflow on smaller windows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(0, 0, 8, 0)  # Right margin for scrollbar

        # --- Status group with integrated action buttons ---
        status_group = QGroupBox(_("Backup Status"))
        status_outer = QHBoxLayout(status_group)

        # Left: status labels
        status_form = QFormLayout()
        self.lbl_last_backup = QLabel(_("Never"))
        status_form.addRow(_("Last backup:"), self.lbl_last_backup)
        self.lbl_next_backup = QLabel(_("Not scheduled"))
        status_form.addRow(_("Next backup:"), self.lbl_next_backup)
        self.lbl_backup_count = QLabel(_("0 backups"))
        status_form.addRow(_("Stored backups:"), self.lbl_backup_count)
        status_outer.addLayout(status_form, 1)

        # Right: action buttons (top-aligned to match group padding)
        btn_col = QVBoxLayout()
        btn_col.setSpacing(8)
        btn_col.setContentsMargins(0, 0, 0, 0)
        btn_backup_now = QPushButton(_("Backup Now\u2026"))
        btn_backup_now.setToolTip(
            _("Create a backup of your game database and settings right now"))
        btn_backup_now.clicked.connect(self._backup_now)
        btn_col.addWidget(btn_backup_now)
        btn_restore = QPushButton(_("Restore\u2026"))
        btn_restore.setToolTip(
            _("Restore your game database and settings from a previous backup"))
        btn_restore.clicked.connect(self._restore_backup)
        btn_col.addWidget(btn_restore)
        btn_col.addStretch()
        status_outer.addLayout(btn_col)
        status_outer.setContentsMargins(12, 8, 12, 8)

        layout.addWidget(status_group)

        # --- What to backup group ---
        contents_group = QGroupBox(_("What to backup"))
        contents_layout = QVBoxLayout(contents_group)
        contents_layout.setSpacing(4)

        # Core items (always included, grayed out + checked)
        core_items = [
            _("Configuration (config.toml)"),
            _("Game database (tags, favorites, notes)"),
            _("Plugin data (store caches, enrichment data)"),
            _("Installed plugins"),
            _("Custom themes"),
        ]
        for label_text in core_items:
            chk = QCheckBox(label_text)
            chk.setChecked(True)
            chk.setEnabled(False)
            contents_layout.addWidget(chk)

        contents_layout.addSpacing(8)
        cache_hint = QLabel(_("Image cache (can be large, re-downloaded on demand):"))
        cache_hint.setObjectName("hintLabel")
        contents_layout.addWidget(cache_hint)

        # Cache toggles with live sizes
        self.chk_include_covers = QCheckBox(_("Cover images"))
        self.chk_include_covers.setToolTip(
            _("Include cover art images in backup"))
        contents_layout.addWidget(self.chk_include_covers)

        self.chk_include_heroes = QCheckBox(_("Hero banners"))
        self.chk_include_heroes.setToolTip(
            _("Include hero banners and description images in backup"))
        contents_layout.addWidget(self.chk_include_heroes)

        self.chk_include_screenshots = QCheckBox(_("Screenshots"))
        self.chk_include_screenshots.setToolTip(
            _("Include screenshot images in backup"))
        contents_layout.addWidget(self.chk_include_screenshots)

        layout.addWidget(contents_group)

        # Compute and display cache sizes
        self._update_cache_sizes()

        # --- Schedule & Retention group ---
        schedule_group = QGroupBox(_("Schedule && Retention"))
        schedule_layout = QVBoxLayout(schedule_group)

        self.chk_schedule_enabled = QCheckBox(_("Enable automatic backups"))
        self.chk_schedule_enabled.setToolTip(
            _("Automatically back up your game database and settings on a regular schedule")
        )
        self.chk_schedule_enabled.toggled.connect(self._on_schedule_toggled)
        schedule_layout.addWidget(self.chk_schedule_enabled)

        # Container for all schedule/retention controls (hidden when unchecked)
        self._schedule_detail_container = QWidget()
        detail_layout = QVBoxLayout(self._schedule_detail_container)
        detail_layout.setContentsMargins(16, 8, 0, 0)
        detail_layout.setSpacing(8)

        schedule_interval_row = QHBoxLayout()
        schedule_interval_row.addWidget(QLabel(_("Backup every")))
        self.spin_interval_days = QSpinBox()
        self.spin_interval_days.setToolTip(_("How many days between each automatic backup"))
        self.spin_interval_days.setMinimum(1)
        self.spin_interval_days.setMaximum(30)
        self.spin_interval_days.setValue(1)
        self.spin_interval_days.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.spin_interval_days.valueChanged.connect(self._update_status_display)
        schedule_interval_row.addWidget(self.spin_interval_days)
        schedule_interval_row.addWidget(QLabel(_("day(s)")))
        schedule_interval_row.addStretch()
        detail_layout.addLayout(schedule_interval_row)

        self.chk_backup_on_startup = QCheckBox(_("Check and run backup on application startup"))
        self.chk_backup_on_startup.setToolTip(
            _("Check whether a backup is due each time luducat starts")
        )
        detail_layout.addWidget(self.chk_backup_on_startup)

        self.chk_backup_silent = QCheckBox(_("Don't ask before automatic backups"))
        self.chk_backup_silent.setToolTip(
            _("Run scheduled backups in the background without asking first")
        )
        detail_layout.addWidget(self.chk_backup_silent)

        # Retention Policy section (inline)
        retention_header = QLabel(_("Retention Policy"))
        f = retention_header.font()
        f.setBold(True)
        retention_header.setFont(f)
        detail_layout.addSpacing(4)
        detail_layout.addWidget(retention_header)

        # Retention spinboxes with inline stats
        retention_grid = QGridLayout()
        retention_grid.setSpacing(6)

        self.spin_retention_daily = QSpinBox()
        self.spin_retention_daily.setToolTip(
            _("How many daily backups to keep. "
              "Older ones are deleted automatically")
        )
        self.spin_retention_daily.setMinimum(1)
        self.spin_retention_daily.setMaximum(30)
        retention_grid.addWidget(QLabel(_("<b>Daily</b> backups to keep:")), 0, 0)
        retention_grid.addWidget(self.spin_retention_daily, 0, 1)
        self.lbl_retention_daily_stats = QLabel()
        self.lbl_retention_daily_stats.setObjectName("hintLabel")
        retention_grid.addWidget(self.lbl_retention_daily_stats, 0, 2)

        self.spin_retention_weekly = QSpinBox()
        self.spin_retention_weekly.setToolTip(
            _("How many weekly backups to keep. "
              "Older ones are deleted automatically")
        )
        self.spin_retention_weekly.setMinimum(0)
        self.spin_retention_weekly.setMaximum(12)
        retention_grid.addWidget(QLabel(_("<b>Weekly</b> backups to keep:")), 1, 0)
        retention_grid.addWidget(self.spin_retention_weekly, 1, 1)
        self.lbl_retention_weekly_stats = QLabel()
        self.lbl_retention_weekly_stats.setObjectName("hintLabel")
        retention_grid.addWidget(self.lbl_retention_weekly_stats, 1, 2)

        self.spin_retention_monthly = QSpinBox()
        self.spin_retention_monthly.setToolTip(
            _("How many monthly backups to keep. "
              "Older ones are deleted automatically")
        )
        self.spin_retention_monthly.setMinimum(0)
        self.spin_retention_monthly.setMaximum(24)
        retention_grid.addWidget(QLabel(_("<b>Monthly</b> backups to keep:")), 2, 0)
        retention_grid.addWidget(self.spin_retention_monthly, 2, 1)
        self.lbl_retention_monthly_stats = QLabel()
        self.lbl_retention_monthly_stats.setObjectName("hintLabel")
        retention_grid.addWidget(self.lbl_retention_monthly_stats, 2, 2)

        self.spin_retention_yearly = QSpinBox()
        self.spin_retention_yearly.setToolTip(
            _("How many yearly backups to keep. "
              "Older ones are deleted automatically")
        )
        self.spin_retention_yearly.setMinimum(0)
        self.spin_retention_yearly.setMaximum(10)
        retention_grid.addWidget(QLabel(_("<b>Yearly</b> backups to keep:")), 3, 0)
        retention_grid.addWidget(self.spin_retention_yearly, 3, 1)
        self.lbl_retention_yearly_stats = QLabel()
        self.lbl_retention_yearly_stats.setObjectName("hintLabel")
        retention_grid.addWidget(self.lbl_retention_yearly_stats, 3, 2)

        # Allow labels column to stretch
        for lbl in [self.lbl_retention_daily_stats, self.lbl_retention_weekly_stats,
                     self.lbl_retention_monthly_stats, self.lbl_retention_yearly_stats]:
            lbl.setTextFormat(Qt.TextFormat.RichText)
        retention_grid.setColumnStretch(2, 1)

        detail_layout.addLayout(retention_grid)

        # Maximum backups summary + Clean Up button
        max_row = QHBoxLayout()
        self.lbl_max_backups = QLabel(_("Maximum backups: {count}").format(count=24))
        max_row.addWidget(self.lbl_max_backups)
        max_row.addStretch()

        self.btn_cleanup = QPushButton(_("Clean Up Now\u2026"))
        self.btn_cleanup.setToolTip(
            _("Delete old backups that exceed the retention limits above"))
        self.btn_cleanup.clicked.connect(self._cleanup_now)
        max_row.addWidget(self.btn_cleanup)

        detail_layout.addLayout(max_row)

        # Wire up retention spinbox changes
        for spin in [self.spin_retention_daily, self.spin_retention_weekly,
                      self.spin_retention_monthly, self.spin_retention_yearly]:
            spin.valueChanged.connect(self._update_retention_summary)

        schedule_layout.addWidget(self._schedule_detail_container)

        # Location row + disk space stats at bottom of group
        location_row = QHBoxLayout()
        location_row.addWidget(QLabel(_("Backup location:")))
        self.txt_backup_path = QLineEdit()
        self.txt_backup_path.setToolTip(
            _("Where backups are stored on disk.\n"
              "Leave empty to use the default location."))
        from pathlib import Path as _Path
        from ...core.backup_manager import get_default_backup_dir
        default_path = str(get_default_backup_dir()).replace(
            str(_Path.home()), "~", 1)
        self.txt_backup_path.setPlaceholderText(
            _("Default: {path}").format(path=default_path))
        location_row.addWidget(self.txt_backup_path, 1)
        btn_browse = QPushButton(_("Browse\u2026"))
        btn_browse.setToolTip(
            _("Choose a different folder for storing backups"))
        btn_browse.clicked.connect(self._browse_backup_location)
        location_row.addWidget(btn_browse)
        schedule_layout.addLayout(location_row)

        self.lbl_disk_space = QLabel()
        self.lbl_disk_space.setObjectName("hintLabel")
        schedule_layout.addWidget(self.lbl_disk_space)

        layout.addWidget(schedule_group)

        layout.addStretch()

        # Set up scroll area
        scroll.setWidget(content)

        # Main layout for this tab
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

    def _load_settings(self) -> None:
        """Load backup settings from config."""
        self._retention_acknowledged = self.config.get(
            "backup.retention_acknowledged", False
        )
        self.txt_backup_path.setText(
            self.config.get("backup.location", "")
        )
        self.chk_schedule_enabled.setChecked(
            self.config.get("backup.schedule_enabled", False)
        )
        self.spin_interval_days.setValue(
            self.config.get("backup.interval_days", 1)
        )
        self.chk_backup_on_startup.setChecked(
            self.config.get("backup.check_on_startup", True)
        )
        self.chk_backup_silent.setChecked(
            self.config.get("backup.silent", False)
        )
        # Retention spinboxes
        self.spin_retention_daily.setValue(self.config.get("backup.retention_daily", 7))
        self.spin_retention_weekly.setValue(self.config.get("backup.retention_weekly", 4))
        self.spin_retention_monthly.setValue(self.config.get("backup.retention_monthly", 12))
        self.spin_retention_yearly.setValue(self.config.get("backup.retention_yearly", 1))

        # Cache inclusion toggles
        self.chk_include_covers.setChecked(self.config.get("backup.include_covers", False))
        self.chk_include_heroes.setChecked(self.config.get("backup.include_heroes", False))
        self.chk_include_screenshots.setChecked(self.config.get("backup.include_screenshots", False))

        self._on_schedule_toggled(self.chk_schedule_enabled.isChecked())
        self._update_retention_summary()

    def save_settings(self) -> None:
        """Save backup settings to config."""
        self.config.set("backup.location", self.txt_backup_path.text())
        self.config.set("backup.schedule_enabled", self.chk_schedule_enabled.isChecked())
        self.config.set("backup.interval_days", self.spin_interval_days.value())
        self.config.set("backup.check_on_startup", self.chk_backup_on_startup.isChecked())
        self.config.set("backup.silent", self.chk_backup_silent.isChecked())
        self.config.set("backup.retention_daily", self.spin_retention_daily.value())
        self.config.set("backup.retention_weekly", self.spin_retention_weekly.value())
        self.config.set("backup.retention_monthly", self.spin_retention_monthly.value())
        self.config.set("backup.retention_yearly", self.spin_retention_yearly.value())
        self.config.set("backup.retention_acknowledged", self._retention_acknowledged)
        self.config.set("backup.include_covers", self.chk_include_covers.isChecked())
        self.config.set("backup.include_heroes", self.chk_include_heroes.isChecked())
        self.config.set("backup.include_screenshots", self.chk_include_screenshots.isChecked())

    def reset_to_defaults(self) -> None:
        """Reset backup settings to defaults."""
        self._retention_acknowledged = False
        self.txt_backup_path.clear()  # Empty = use default location
        self.chk_schedule_enabled.setChecked(False)
        self.spin_interval_days.setValue(1)
        self.chk_backup_on_startup.setChecked(True)
        self.chk_backup_silent.setChecked(False)
        self.spin_retention_daily.setValue(7)
        self.spin_retention_weekly.setValue(4)
        self.spin_retention_monthly.setValue(12)
        self.spin_retention_yearly.setValue(1)
        self.chk_include_covers.setChecked(False)
        self.chk_include_heroes.setChecked(False)
        self.chk_include_screenshots.setChecked(False)
        self._on_schedule_toggled(False)
        self._update_status_display()
        self._update_retention_summary()

    def _update_cache_sizes(self) -> None:
        """Update cache toggle labels with live directory sizes."""
        try:
            from ...core.backup_manager import get_cache_dir_sizes
            sizes = get_cache_dir_sizes()
            for chk, key in [
                (self.chk_include_covers, "covers"),
                (self.chk_include_heroes, "heroes"),
                (self.chk_include_screenshots, "screenshots"),
            ]:
                size_bytes = sizes.get(key, 0)
                chk.setText(
                    f"{chk.text()}  ({self._format_cache_size(size_bytes)})"
                )
        except Exception:
            pass

    @staticmethod
    def _format_cache_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

    def _on_schedule_toggled(self, enabled: bool) -> None:
        """Show/hide schedule detail controls.

        When enabling for the first time, show a retention warning dialog
        so the user understands that old backups will be automatically deleted.
        """
        if enabled and not self._retention_acknowledged:
            if not self._show_retention_warning():
                self.chk_schedule_enabled.setChecked(False)
                return
        self._schedule_detail_container.setVisible(enabled)
        self._update_status_display()

    def _show_retention_warning(self) -> bool:
        """Show a one-time warning about automatic backup retention.

        Returns True if the user acknowledged, False if cancelled.
        """
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle(_("Automatic Backup Retention"))
        msg.setText(
            _("When automatic backups are enabled, old backups that exceed "
              "your retention policy will be permanently deleted after each "
              "backup.\n\n"
              "You can adjust retention limits below.")
        )

        chk = QCheckBox(
            _("I understand that old backups will be automatically deleted")
        )
        msg.setCheckBox(chk)

        msg.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        ok_btn = msg.button(QMessageBox.StandardButton.Ok)
        ok_btn.setEnabled(False)
        chk.toggled.connect(ok_btn.setEnabled)

        result = msg.exec()
        if result == QMessageBox.StandardButton.Ok:
            self._retention_acknowledged = True
            return True
        return False

    def _update_retention_summary(self) -> None:
        """Update retention stats and maximum backups label."""
        total = (self.spin_retention_daily.value() +
                 self.spin_retention_weekly.value() +
                 self.spin_retention_monthly.value() +
                 self.spin_retention_yearly.value())
        self.lbl_max_backups.setText(_("Maximum backups: {count}").format(count=total))

        # Update per-category stats from actual backup files
        from ...core.backup_manager import categorize_backups
        cats = categorize_backups(self.config)

        for period, spin_lbl in [
            ("daily", self.lbl_retention_daily_stats),
            ("weekly", self.lbl_retention_weekly_stats),
            ("monthly", self.lbl_retention_monthly_stats),
            ("yearly", self.lbl_retention_yearly_stats),
        ]:
            info = cats[period]
            if info["count"] == 0:
                spin_lbl.setText(_("(0 backups)"))
            else:
                size_mb = info["total_size"] / (1024 * 1024)
                if size_mb < 1:
                    size_str = "{:.1f} KB".format(info['total_size'] / 1024)
                else:
                    size_str = "{:.1f} MB".format(size_mb)
                date_str = info["newest"].strftime("%Y-%m-%d") if info["newest"] else ""
                count = info['count']
                backup_word = _("backup") if count == 1 else _("backups")
                spin_lbl.setText(
                    _("({count} {backup_word}, {size}, last: {date})").format(
                        count=count, backup_word=backup_word, size=size_str, date=date_str)
                )

    def _cleanup_now(self) -> None:
        """Calculate and apply retention policy with user confirmation."""
        from ...core.backup_manager import apply_retention_policy

        backup_dir = self._get_backup_dir()
        if not backup_dir.exists():
            QMessageBox.information(self, _("Clean Up"), _("No backups found."))
            return

        # Collect all backups
        from datetime import datetime as _dt
        backups = []
        for f in backup_dir.glob("luducat_backup_*.zip"):
            try:
                name = f.stem
                ts_str = name.replace("luducat_backup_", "")
                _dt.strptime(ts_str, "%Y%m%d_%H%M%S")
                backups.append(f)
            except ValueError:
                continue

        if not backups:
            QMessageBox.information(self, _("Clean Up"), _("No backups found."))
            return

        # Save current retention values to config temporarily for dry-run
        self.save_settings()
        self.config.save()

        # Calculate what would be kept (by running categorize)
        from ...core.backup_manager import categorize_backups
        cats = categorize_backups(self.config)
        kept_count = sum(c["count"] for c in cats.values())
        total_count = len(backups)
        delete_count = total_count - kept_count

        if delete_count <= 0:
            QMessageBox.information(
                self, _("Clean Up"),
                _("All backups are within retention limits. Nothing to delete.")
            )
            return

        # Calculate size to be freed
        total_size = sum(f.stat().st_size for f in backups)
        kept_size = sum(c["total_size"] for c in cats.values())
        delete_size = total_size - kept_size
        if delete_size < 1024 * 1024:
            size_str = f"{delete_size / 1024:.1f} KB"
        else:
            size_str = f"{delete_size / (1024 * 1024):.1f} MB"

        reply = QMessageBox.question(
            self,
            _("Clean Up Backups"),
            _("{count} backup(s) will be deleted ({size}).\n\n"
              "This cannot be undone.\n\nProceed?").format(count=delete_count, size=size_str),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = apply_retention_policy(self.config)
        self._update_status_display()
        self._update_retention_summary()
        QMessageBox.information(
            self, _("Clean Up Complete"),
            _("Deleted {count} old backup(s).").format(count=deleted)
        )

    def _browse_backup_location(self) -> None:
        """Open folder picker for backup location."""
        current = self.txt_backup_path.text() or str(self._get_default_backup_dir())
        folder = QFileDialog.getExistingDirectory(
            self,
            _("Select Backup Location"),
            current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.txt_backup_path.setText(folder)

    def _get_default_backup_dir(self):
        """Get the default backup directory."""
        from ...core.backup_manager import get_default_backup_dir
        return get_default_backup_dir()

    def _get_backup_dir(self):
        """Get the configured or default backup directory."""
        from pathlib import Path
        custom_path = self.txt_backup_path.text().strip()
        if custom_path:
            return Path(custom_path)
        return self._get_default_backup_dir()

    def _update_status_display(self) -> None:
        """Update the status labels with current backup info."""
        from datetime import datetime, timedelta

        # Last backup
        last_backup_str = self.config.get("backup.last_backup", "")
        if last_backup_str:
            try:
                last_backup = datetime.fromisoformat(last_backup_str)
                self.lbl_last_backup.setText(last_backup.strftime("%Y-%m-%d %H:%M"))
            except ValueError:
                self.lbl_last_backup.setText(_("Unknown"))
        else:
            self.lbl_last_backup.setText(_("Never"))

        # Next backup
        if self.chk_schedule_enabled.isChecked() and last_backup_str:
            try:
                last_backup = datetime.fromisoformat(last_backup_str)
                interval = self.spin_interval_days.value()
                next_backup = last_backup + timedelta(days=interval)
                if next_backup <= datetime.now():
                    self.lbl_next_backup.setText(_("Due now"))
                else:
                    self.lbl_next_backup.setText(next_backup.strftime("%Y-%m-%d"))
            except ValueError:
                self.lbl_next_backup.setText(_("Unknown"))
        elif self.chk_schedule_enabled.isChecked():
            self.lbl_next_backup.setText(_("On next startup"))
        else:
            self.lbl_next_backup.setText(_("Not scheduled"))

        # Backup count — guard against unreachable paths
        backup_dir = self._get_backup_dir()
        try:
            dir_exists = backup_dir.exists()
        except OSError:
            dir_exists = False

        if dir_exists:
            try:
                backups = list(backup_dir.glob("luducat_backup_*.zip"))
                count = len(backups)
                if count > 0:
                    total_size = sum(f.stat().st_size for f in backups)
                    if total_size < 1024 * 1024:
                        size_str = f"{total_size / 1024:.1f} KB"
                    else:
                        size_str = f"{total_size / (1024 * 1024):.1f} MB"
                    self.lbl_backup_count.setText(
                        _("{count} backups ({size})").format(
                            count=count, size=size_str
                        )
                    )
                else:
                    self.lbl_backup_count.setText(_("0 backups"))
            except OSError:
                self.lbl_backup_count.setText(_("0 backups"))
        else:
            self.lbl_backup_count.setText(_("0 backups"))

        # Disk space stats — only probe if directory already exists
        # (check_directory creates missing dirs as a side-effect, which is
        # wrong here; and it can hang on unreachable network paths)
        if backup_dir.exists():
            try:
                from ...core.directory_health import check_directory
                health = check_directory(backup_dir)
                self.lbl_disk_space.setText(
                    _("Disk space: {used} MB used, {free} free").format(
                        used=health.used_mb,
                        free=AdvancedSettingsTab._format_size_mb(health.free_mb))
                )
            except OSError:
                self.lbl_disk_space.setText("")
        else:
            self.lbl_disk_space.setText("")

    def _backup_now(self) -> None:
        """Execute backup immediately with progress dialog."""
        from ...core.backup_manager import (
            create_backup, collect_backup_items, collect_assets_items,
        )

        # Show busy cursor while scanning files
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            backup_items = collect_backup_items()
            if not backup_items:
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(
                    self,
                    _("Nothing to Backup"),
                    _("No configuration or data files found to backup.")
                )
                return

            # Persist cache toggle states so collect_assets_items reads current UI
            self.config.set("backup.include_covers", self.chk_include_covers.isChecked())
            self.config.set("backup.include_heroes", self.chk_include_heroes.isChecked())
            self.config.set("backup.include_screenshots", self.chk_include_screenshots.isChecked())

            assets_items = collect_assets_items(self.config)
        finally:
            QApplication.restoreOverrideCursor()

        # Confirmation dialog
        summary = _("Configuration, database, plugins, and themes")
        if assets_items:
            summary += "\n" + _("Image cache (covers, heroes, screenshots)")
        reply = QMessageBox.question(
            self,
            _("Create Backup"),
            _("Create a backup of your data now?\n\n"
              "Included:\n{summary}").format(summary=summary),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        total_items = len(backup_items) + len(assets_items) + 1  # +1 metadata

        # Create progress dialog, register work, then show (showEvent fires the timer)
        progress_dialog = BackupProgressDialog(
            _("Creating Backup"), total_items, self,
        )
        progress_dialog.set_backup_folder(str(self._get_backup_dir()))

        def run_backup():
            _category_map = {
                "config.toml": _("Configuration"),
                "games.db": _("Game database"),
                "trust-state.json": _("Configuration"),
                "plugins/": _("Installed plugins"),
                "plugins-data/": _("Plugin data"),
                "themes/": _("Custom themes"),
                "covers/": _("Cover images"),
                "heroes/": _("Hero banners"),
                "description_images/": _("Hero banners"),
                "screenshots/": _("Screenshots"),
            }
            _last_category = [None]

            def _archive_to_category(archive_name: str) -> str:
                """Map archive_name to a user-facing category label."""
                if archive_name in _category_map:
                    return _category_map[archive_name]
                for prefix, label in _category_map.items():
                    if prefix.endswith("/") and archive_name.startswith(prefix):
                        return label
                return archive_name

            def progress_callback(message: str, current: int, total: int) -> None:
                if message.startswith("Backing up "):
                    archive = message.replace("Backing up ", "").rstrip("...")
                    cat = _archive_to_category(archive)
                    if cat != _last_category[0]:
                        _last_category[0] = cat
                        progress_dialog.set_item(cat)
                elif message.startswith("Writing metadata"):
                    progress_dialog.set_item(_("Writing metadata..."))
                elif message.startswith("Finalizing"):
                    progress_dialog.set_item(_("Finalizing..."))
                elif message.startswith("Creating"):
                    progress_dialog.set_item(_("Preparing..."))
                else:
                    progress_dialog.set_item(message)
                progress_dialog.set_progress(current)

            def file_callback(path: str) -> None:
                progress_dialog.set_path(path)

            success, result, assets_path = create_backup(
                self.config, progress_callback, file_callback,
            )

            self._update_status_display()

            if success:
                msg = _("Backup created successfully.")
                parts = [result.rsplit("/", 1)[-1]]
                if assets_path:
                    parts.append(assets_path.rsplit("/", 1)[-1])
                msg += "\n" + "\n".join(parts)
                progress_dialog.finish(True, msg)
            else:
                progress_dialog.finish(
                    False, _("Backup failed: {error}").format(error=result),
                )

        # Register work first, then show — showEvent fires the deferred timer
        progress_dialog.start_work(run_backup)
        progress_dialog.show()

    @staticmethod
    def _compare_user_data(backup_zip_path, current_db_path) -> List[str]:
        """Compare user data between current DB and backup DB.

        Uses raw SQLite (no ORM) to count tags, favorites, hidden, notes
        in both databases. Returns warning lines for data that would be lost.
        """
        import sqlite3
        import tempfile
        import zipfile
        from pathlib import Path

        def _count_user_data(db_path):
            counts = {}
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                for key, query in [
                    ("tags", "SELECT COUNT(*) FROM user_tags"),
                    ("tag_assignments", "SELECT COUNT(*) FROM game_tags"),
                    ("favorites", "SELECT COUNT(*) FROM user_game_data WHERE is_favorite = 1"),
                    ("hidden", "SELECT COUNT(*) FROM user_game_data WHERE is_hidden = 1"),
                    ("notes",
                     "SELECT COUNT(*) FROM user_game_data"
                     " WHERE custom_notes IS NOT NULL"
                     " AND custom_notes != ''"),
                ]:
                    try:
                        counts[key] = conn.execute(query).fetchone()[0]
                    except sqlite3.OperationalError:
                        counts[key] = 0  # Table doesn't exist in older schemas
                conn.close()
            except Exception:
                pass
            return counts

        current = _count_user_data(current_db_path)
        if not current:
            return []

        # Extract backup's games.db to temp file for querying
        backup = {}
        tmp_path = None
        try:
            with zipfile.ZipFile(backup_zip_path, 'r') as zf:
                if "games.db" not in zf.namelist():
                    return []
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                    tmp.write(zf.read("games.db"))
                    tmp_path = Path(tmp.name)
            backup = _count_user_data(tmp_path)
        except Exception:
            return []
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)

        # Build warning lines for data that would be lost
        warnings = []
        labels = {
            "tags": _("Tags"),
            "tag_assignments": _("Tag assignments"),
            "favorites": _("Favorites"),
            "hidden": _("Hidden games"),
            "notes": _("Notes"),
        }
        for key, label in labels.items():
            cur = current.get(key, 0)
            bak = backup.get(key, 0)
            if cur > bak:
                lost = cur - bak
                warnings.append(f"  {label}: {cur} \u2192 {bak} (-{lost})")

        return warnings

    def _restore_backup(self) -> None:
        """Restore from a backup file with progress dialog."""
        import zipfile
        from datetime import datetime
        from pathlib import Path
        from PySide6.QtCore import QTimer
        from ...core.config import get_config_dir, get_data_dir

        # Show file picker — safe directory resolution
        backup_dir = self._get_backup_dir()
        try:
            start_dir = str(backup_dir) if backup_dir.exists() else str(Path.home())
        except OSError:
            start_dir = str(Path.home())

        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Select Backup to Restore"),
            start_dir,
            _("ZIP Archives (*.zip)")
        )

        if not file_path:
            return

        file_path = Path(file_path)

        # Reject assets ZIP if user selected it directly
        if file_path.name.startswith("luducat_assets_"):
            QMessageBox.information(
                self,
                _("Wrong File"),
                _("This is an image cache backup. "
                  "Please select the data backup file "
                  "(luducat_backup_...) instead.")
            )
            return

        # Validate backup
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                if "backup_info.json" not in zf.namelist():
                    QMessageBox.warning(
                        self,
                        _("Invalid Backup"),
                        _("This file does not appear to be a valid luducat backup.")
                    )
                    return

                backup_info = json.loads(zf.read("backup_info.json"))
                files_raw = backup_info.get("files", [])
                created = backup_info.get("created", "unknown")
                version = backup_info.get("version", "unknown")

                if isinstance(files_raw, dict):
                    file_names = list(files_raw.keys())
                else:
                    file_names = files_raw

        except Exception as e:
            QMessageBox.critical(
                self,
                _("Error Reading Backup"),
                _("Failed to read backup file:\n{error}").format(error=e)
            )
            return

        # Verify integrity
        from ...core.backup_manager import verify_backup, MIN_RESTORE_VERSION
        ok, problems = verify_backup(file_path)
        if not ok:
            detail = "\n".join(problems)
            reply = QMessageBox.warning(
                self,
                _("Backup Integrity Warning"),
                _("Integrity verification found problems:\n\n"
                  "{detail}\n\n"
                  "The backup may be corrupt. Continue restoring anyway?").format(
                    detail=detail),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Check minimum restore version
        if version != "unknown" and version < MIN_RESTORE_VERSION:
            QMessageBox.critical(
                self,
                _("Incompatible Backup"),
                _("This backup was created with version {backup_version}, "
                  "which is too old to restore.\n\n"
                  "Minimum supported version: {min_version}").format(
                    backup_version=version,
                    min_version=MIN_RESTORE_VERSION),
            )
            return

        # Build version mismatch warning if needed
        version_warning = ""
        if version != APP_VERSION:
            version_warning = _(
                "WARNING: This backup was created with a different version "
                "of luducat ({backup_version}). The current version is "
                "{current_version}.\n"
                "Restoring may require database migration.\n\n"
            ).format(backup_version=version, current_version=APP_VERSION)

        # Compare user data to warn about potential data loss
        data_warning = ""
        data_dir = get_data_dir()
        data_warnings = self._compare_user_data(file_path, data_dir / "games.db")
        if data_warnings:
            data_warning = _(
                "WARNING: Your current database has more user data than "
                "the backup. The following will be lost:\n"
            ) + "\n".join(data_warnings) + "\n\n"

        # Check for companion assets ZIP
        restore_assets = False
        assets_filename = backup_info.get("assets_file")
        assets_path = None
        assets_count = 0
        if assets_filename:
            assets_path = file_path.parent / assets_filename
            if assets_path.exists():
                assets_reply = QMessageBox.question(
                    self,
                    _("Restore Image Cache?"),
                    _("This backup includes image cache data.\n"
                      "Restore image cache too?"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                restore_assets = assets_reply == QMessageBox.StandardButton.Yes
                if restore_assets:
                    try:
                        with zipfile.ZipFile(assets_path, 'r') as azf:
                            assets_count = len(azf.namelist())
                    except Exception:
                        assets_count = 0
            else:
                logger.info(
                    "Companion assets ZIP %s not found, skipping", assets_filename,
                )

        # Show confirmation with details
        reply = QMessageBox.warning(
            self,
            _("Confirm Restore"),
            _("{version_warning}{data_warning}"
              "Restore from backup?\n\n"
              "Created: {created}\n"
              "Version: {version}\n"
              "Files: {files}\n\n"
              "THIS WILL REPLACE YOUR CURRENT DATA.\n"
              "A safety backup will be created first.\n"
              "If anything goes wrong, use 'Restore' again to roll back.\n\n"
              "The application will restart after restore.").format(
                version_warning=version_warning,
                data_warning=data_warning,
                created=created, version=version, files=len(file_names)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        config_dir = get_config_dir()
        data_dir = get_data_dir()

        # Total: 1 safety backup + data files + optional assets files
        total_items = 1 + len(file_names) + assets_count

        # Create progress dialog
        progress_dialog = BackupProgressDialog(
            _("Restoring Backup"), total_items, self,
        )

        def run_restore():
            current = 0

            # Create safety backup first
            safety_backup_dir = data_dir / "pre_restore_backups"
            safety_backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safety_backup = safety_backup_dir / f"pre_restore_{timestamp}.zip"

            progress_dialog.set_item(_("Creating safety backup..."))
            progress_dialog.set_path(str(safety_backup))
            QApplication.processEvents()

            try:
                with zipfile.ZipFile(safety_backup, 'w', zipfile.ZIP_DEFLATED) as zf:
                    config_file = config_dir / "config.toml"
                    if config_file.exists():
                        zf.write(config_file, "config.toml")
                    db_file = data_dir / "games.db"
                    if db_file.exists():
                        zf.write(db_file, "games.db")
                logger.info(f"Created safety backup: {safety_backup}")
            except Exception as e:
                logger.warning(f"Failed to create safety backup: {e}")

            current += 1
            progress_dialog.set_progress(current)

            # Extract backup
            try:
                progress_dialog.set_path(str(file_path))
                with zipfile.ZipFile(file_path, 'r') as zf:
                    for name in file_names:
                        if name == "config.toml":
                            target = config_dir / "config.toml"
                        elif name == "games.db":
                            target = data_dir / "games.db"
                        elif name == "trust-state.json":
                            target = data_dir / "trust-state.json"
                        elif name.startswith("plugins/"):
                            target = config_dir / name
                        elif name.startswith("plugins-data/"):
                            target = data_dir / name
                        elif name.startswith("themes/"):
                            target = config_dir / name
                        else:
                            current += 1
                            progress_dialog.set_progress(current)
                            continue

                        progress_dialog.set_item(name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, open(target, 'wb') as dst:
                            dst.write(src.read())
                        logger.info(f"Restored: {name}")

                        current += 1
                        progress_dialog.set_progress(current)

                # Restore companion assets if requested
                if restore_assets and assets_path and assets_path.exists():
                    from ...core.config import get_cache_dir as _get_cache_dir
                    cache_dir = _get_cache_dir()
                    progress_dialog.set_path(str(assets_path))
                    try:
                        with zipfile.ZipFile(assets_path, 'r') as azf:
                            for aname in azf.namelist():
                                progress_dialog.set_item(aname)
                                target = cache_dir / aname
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with azf.open(aname) as src, \
                                        open(target, 'wb') as dst:
                                    dst.write(src.read())
                                current += 1
                                progress_dialog.set_progress(current)
                        logger.info("Assets restored from %s", assets_path.name)
                    except OSError as e:
                        logger.warning(f"Assets restore failed: {e}")

                progress_dialog.finish(
                    True,
                    _("Restore complete. The application will now restart."),
                    show_close=False,
                )

                # Auto-restart after a short delay
                QTimer.singleShot(1500, self._force_restart_after_restore)

            except (OSError, zipfile.BadZipFile) as e:
                logger.error(f"Failed to restore backup: {e}")
                progress_dialog.finish(
                    False,
                    _("Restore failed: {error}").format(error=e),
                )

        # Register work first, then show — showEvent fires the deferred timer
        progress_dialog.start_work(run_restore)
        progress_dialog.show()

    def _force_restart_after_restore(self) -> None:
        """Emit restart signal to force application restart."""
        self.restart_required.emit()


# TagManagerDialog moved to tag_manager_dialog.py
# Backwards compat re-export
from .tag_manager_dialog import TagManagerDialog  # noqa: F401, E402
TagManagerTab = TagManagerDialog  # Alias for any remaining references


class CollapsibleSection(QWidget):
    """A collapsible section with header button and content area"""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._expanded = True
        self._title = title
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header button with arrow
        self._header = QPushButton(f"\u25BC  {self._title}")
        self._header.setObjectName("collapsibleHeader")
        self._header.setFlat(True)
        self._header.clicked.connect(self._toggle)
        layout.addWidget(self._header)

        # Content container
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 4, 0, 8)
        self._content_layout.setSpacing(4)
        layout.addWidget(self._content)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "\u25BC" if self._expanded else "\u25B6"
        self._header.setText(f"{arrow}  {self._title}")

    def content_layout(self) -> QVBoxLayout:
        """Get the content layout to add widgets to"""
        return self._content_layout

    def set_expanded(self, expanded: bool) -> None:
        """Set expanded state"""
        if expanded != self._expanded:
            self._toggle()


class MetadataSettingsTab(QWidget):
    """Metadata priority settings tab with category sidebar and field panels.

    Allows users to configure which data source has priority for each
    metadata field. Fields are organized into categories via a sidebar.
    """

    # Category definitions: (key, label, icon_filename)
    _CATEGORIES = [
        ("General", N_("General"), "cat-general.svg"),
        ("Media", N_("Media"), "cat-media.svg"),
        ("Ratings", N_("Ratings"), "cat-ratings.svg"),
        ("Extended", N_("Extended"), "cat-extended.svg"),
        ("Technical", N_("Technical"), "cat-technical.svg"),
        ("Statistics", N_("Statistics"), "cat-statistics.svg"),
    ]

    def __init__(
        self,
        config: Config,
        plugin_manager: PluginManager,
        game_service=None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager
        self.game_service = game_service
        self._field_buttons: Dict[str, QPushButton] = {}
        self._current_priorities: Dict[str, List[str]] = {}
        self._initial_priorities: Dict[str, List[str]] = {}

        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        from luducat.core.metadata_resolver import (
            FIELD_GROUPS,
        )
        from luducat.ui.widgets.category_sidebar import CategorySidebar

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)

        # Header with description
        header = QLabel(
            _("Configure which data source has priority for each metadata field. "
              "Click a field button to edit its priority order. Priority is left to right, "
              "first source with data wins.")
            + "<br><br><b>" + _("Note:") + " "
            + _("Changing cover, hero, or screenshot priorities automatically "
                "resets those fields so they refetch from the new order. "
                "Other fields take effect on next sync.") + "</b>"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        header.setObjectName("dialogDescription")
        main_layout.addWidget(header)

        # Horizontal splitter: sidebar left, content right
        content_layout = QHBoxLayout()
        content_layout.setSpacing(12)

        # Left side: sidebar + buttons in a bordered container
        left_container = QWidget()
        left_container.setObjectName("prioritySidebarPanel")
        left_container.setFixedWidth(182)
        left_panel = QVBoxLayout(left_container)
        left_panel.setContentsMargins(4, 4, 4, 4)
        left_panel.setSpacing(8)

        # Category sidebar
        self._sidebar = CategorySidebar()
        for key, label, icon in self._CATEGORIES:
            self._sidebar.add_category(key, _(label), icon)
        left_panel.addWidget(self._sidebar, 1)

        # Button grid (2x2) below sidebar
        btn_grid = QGridLayout()
        btn_grid.setSpacing(6)

        btn_import = QPushButton(_("Import"))
        btn_import.setToolTip(
            _("Load priorities from a saved preset, "
              "e.g. your own backup or one shared "
              "by a friend")
        )
        btn_import.clicked.connect(self._load_preset)
        btn_grid.addWidget(btn_import, 0, 0)

        btn_export = QPushButton(_("Export"))
        btn_export.setToolTip(
            _("Save your current priorities as a reusable preset.\n"
              "You can share presets with friends or edit them with a text editor.")
        )
        btn_export.clicked.connect(self._save_preset)
        btn_grid.addWidget(btn_export, 1, 0)

        btn_preview = QPushButton(_("Preview"))
        btn_preview.setToolTip(_("See how your priority settings affect a specific game"))
        btn_preview.clicked.connect(self._open_preview)
        btn_grid.addWidget(btn_preview, 0, 1)

        self._btn_defaults = QPushButton(_("Defaults"))
        self._btn_defaults.setToolTip(
            _("Revert all changes made this session. "
              "Right-click for factory reset.")
        )
        self._btn_defaults.clicked.connect(self._restore_initial)
        self._btn_defaults.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._btn_defaults.customContextMenuRequested.connect(
            self._show_defaults_context_menu
        )
        btn_grid.addWidget(self._btn_defaults, 1, 1)

        left_panel.addLayout(btn_grid)
        content_layout.addWidget(left_container)

        # Right side: stacked widget with one scroll panel per category
        self._stack = QStackedWidget()
        self._stack.setObjectName("priorityStack")
        self._category_indices: Dict[str, int] = {}

        for key, _label, _icon in self._CATEGORIES:
            fields = FIELD_GROUPS.get(key, [])

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)

            panel = QWidget()
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(4, 4, 4, 0)
            outer.setSpacing(0)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(4)
            # Column 0: fixed-width labels, Column 1: stretching buttons
            grid.setColumnMinimumWidth(0, 170)
            grid.setColumnStretch(1, 1)

            for row_idx, field_name in enumerate(fields):
                label, btn = self._create_field_pair(field_name)
                grid.addWidget(label, row_idx, 0)
                grid.addWidget(btn, row_idx, 1)

            outer.addLayout(grid)
            outer.addStretch()
            scroll.setWidget(panel)

            idx = self._stack.addWidget(scroll)
            self._category_indices[key] = idx

        content_layout.addWidget(self._stack, 1)
        main_layout.addLayout(content_layout, 1)

        # Connect sidebar to stacked widget
        self._sidebar.currentChanged.connect(self._on_category_changed)
        self._sidebar.select_category("General")

    def _create_field_pair(self, field_name: str) -> Tuple[QLabel, QPushButton]:
        """Create a label + priority button pair for grid layout.

        Returns:
            (label, button) tuple — caller places them in QGridLayout columns.
        """
        from luducat.core.metadata_resolver import FIELD_LABELS, FIELD_TOOLTIPS

        label = QLabel(_(FIELD_LABELS.get(field_name, field_name.title())))
        label.setFixedWidth(170)
        label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        tooltip = FIELD_TOOLTIPS.get(field_name, "")
        if tooltip:
            label.setToolTip(_(tooltip))

        btn = QPushButton()
        btn.setObjectName("priorityFieldButton")
        btn.clicked.connect(lambda: self._edit_field_priority(field_name))
        if tooltip:
            btn.setToolTip(tooltip)

        self._field_buttons[field_name] = btn
        return label, btn

    def _on_category_changed(self, key: str) -> None:
        """Switch the stacked widget to the selected category panel."""
        idx = self._category_indices.get(key)
        if idx is not None:
            self._stack.setCurrentIndex(idx)

    def _restore_initial(self) -> None:
        """Restore priorities to the snapshot taken when the dialog opened."""
        from copy import deepcopy
        from luducat.core.metadata_resolver import FIELD_GROUPS

        if not self._initial_priorities:
            return

        self._current_priorities = deepcopy(self._initial_priorities)
        for group_fields in FIELD_GROUPS.values():
            for field_name in group_fields:
                self._update_button_text(field_name)

    def _show_defaults_context_menu(self, pos) -> None:
        """Show context menu on Defaults button with factory reset option."""
        menu = QMenu(self)
        action = menu.addAction(_("Reset to Factory Defaults"))
        action.triggered.connect(self._reset_all_to_defaults)
        menu.exec(self._btn_defaults.mapToGlobal(pos))

    def _update_button_text(self, field_name: str) -> None:
        """Update button to show current priority.

        Prefixes with '*' if the field has been changed from the initial
        snapshot (unsaved change indicator).
        """
        from luducat.core.metadata_resolver import SOURCE_LABELS

        priority = self._current_priorities.get(field_name, [])
        btn = self._field_buttons[field_name]

        if not priority:
            text = _("(using defaults)")
        else:
            labels = [
                SOURCE_LABELS.get(
                    s, PluginManager.get_store_display_name(s)
                )
                for s in priority
            ]
            text = " > ".join(labels)

        # Mark unsaved changes
        initial = self._initial_priorities.get(field_name)
        if initial is not None and priority != initial:
            text = "* " + text

        btn.setText(text)

    def _get_enabled_plugins(self, authenticated_only: bool = False) -> Set[str]:
        """Get set of currently enabled plugin names

        Args:
            authenticated_only: If True, only return plugins that are authenticated
                              (or don't require authentication)

        Returns:
            Set of plugin names
        """
        from luducat.plugins.base import PluginType

        enabled = set()

        # Add store plugins
        store_plugins = self.plugin_manager.get_plugins_by_type(PluginType.STORE)
        for name, plugin in store_plugins.items():
            if authenticated_only:
                if hasattr(plugin, 'is_authenticated') and not plugin.is_authenticated():
                    continue
            enabled.add(name)

        # Add metadata plugins
        metadata_plugins = self.plugin_manager.get_plugins_by_type(PluginType.METADATA)
        for name, plugin in metadata_plugins.items():
            if authenticated_only:
                if hasattr(plugin, 'is_authenticated') and not plugin.is_authenticated():
                    continue
            enabled.add(name)

        return enabled

    def _edit_field_priority(self, field_name: str) -> None:
        """Open priority editor for a field"""
        from luducat.core.metadata_resolver import FIELD_LABELS, get_resolver
        from .priority_editor import PriorityEditorDialog

        current = self._current_priorities.get(field_name, [])
        if not current:
            # Use default if no custom priority set
            current = get_resolver().get_effective_defaults().get(field_name, [])

        enabled = self._get_enabled_plugins()
        authenticated = self._get_enabled_plugins(authenticated_only=True)

        dialog = PriorityEditorDialog(
            field_name,
            _(FIELD_LABELS.get(field_name, field_name.title())),
            current,
            enabled,
            self,
            authenticated_plugins=authenticated,
        )

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_priority = dialog.get_priority()
            self._current_priorities[field_name] = new_priority
            self._update_button_text(field_name)

    def _load_settings(self) -> None:
        """Load settings from config and snapshot for Defaults button."""
        from copy import deepcopy
        from luducat.core.metadata_resolver import FIELD_GROUPS, get_resolver

        saved = self.config.get_metadata_priorities()
        defaults = get_resolver().get_effective_defaults()

        # Load saved priorities or use defaults
        for group_fields in FIELD_GROUPS.values():
            for field_name in group_fields:
                if field_name in saved and saved[field_name]:
                    self._current_priorities[field_name] = saved[field_name].copy()
                else:
                    # Use default
                    self._current_priorities[field_name] = (
                        defaults.get(field_name, []).copy()
                    )
                self._update_button_text(field_name)

        # Snapshot for Defaults button (reverts to dialog-open state)
        self._initial_priorities = deepcopy(self._current_priorities)

    def has_unsaved_changes(self) -> bool:
        """Check if any field priorities differ from the saved snapshot."""
        return self._current_priorities != self._initial_priorities

    def save_settings(self) -> None:
        """Save priority settings to config and push to live resolver.

        Detects all fields whose priority changed, warns the user, then
        saves config, updates the resolver, and resets those fields in
        the main DB so they re-resolve from the new order on next sync.
        The actual field reset is done last.
        """
        from copy import deepcopy
        from luducat.core.metadata_resolver import FIELD_GROUPS, get_resolver

        resolver = get_resolver()

        # Detect ALL fields whose priority changed
        changed_fields = []
        for group_fields in FIELD_GROUPS.values():
            for field_name in group_fields:
                old = self._initial_priorities.get(field_name, [])
                new = self._current_priorities.get(field_name, [])
                if old != new:
                    changed_fields.append(field_name)

        # Warn about field resets before proceeding
        if changed_fields and self.game_service:
            from luducat.core.metadata_resolver import FIELD_LABELS
            labels = [_(FIELD_LABELS.get(f, f)) for f in changed_fields]
            reply = QMessageBox.question(
                self,
                _("Reset Changed Fields"),
                _("The following fields will be reset on all games so they "
                  "refetch from the new priority order on next sync:\n\n"
                  "{fields}\n\nProceed?").format(
                    fields=", ".join(labels)
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            # 1. Save to config + live resolver
            self.config.set_metadata_priorities(self._current_priorities)
            resolver.update_field_priorities(self._current_priorities)

            # 2. Update initial snapshot so Defaults button reflects saved state
            self._initial_priorities = deepcopy(self._current_priorities)

            # 3. Refresh button text to clear unsaved-change indicators
            for field_name in self._field_buttons:
                self._update_button_text(field_name)

            # 4. Reset changed fields (queued last)
            if changed_fields and self.game_service:
                try:
                    count = self.game_service.reset_media_fields(changed_fields)
                    logger.info(
                        f"Priority change: reset {changed_fields} in {count} store_games"
                    )
                except Exception as e:
                    logger.error(f"Failed to reset fields after priority change: {e}")
        finally:
            QApplication.restoreOverrideCursor()

    def reset_to_defaults(self) -> None:
        """Reset all priorities to defaults (called by main dialog)"""
        self._reset_all_to_defaults(confirm=False)

    def _reset_all_to_defaults(self, confirm: bool = True) -> None:
        """Reset all field priorities to defaults"""
        from luducat.core.metadata_resolver import FIELD_GROUPS, get_resolver

        if confirm:
            reply = QMessageBox.question(
                self,
                _("Reset to Defaults"),
                _("Reset all metadata priorities to Luducat defaults?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        defaults = get_resolver().get_effective_defaults()
        for group_fields in FIELD_GROUPS.values():
            for field_name in group_fields:
                self._current_priorities[field_name] = (
                    defaults.get(field_name, []).copy()
                )
                self._update_button_text(field_name)

    def _save_preset(self) -> None:
        """Save current priorities as a preset file"""

        # Ask for preset name
        name, ok = QInputDialog.getText(
            self, _("Save Preset"), _("Preset name:")
        )
        if not ok or not name.strip():
            return

        # Get save path
        presets_dir = self.config.config_dir / "presets"
        presets_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename
        safe_name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
        default_path = presets_dir / f"{safe_name}.json"

        file_path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Save Metadata Priority Preset"),
            str(default_path),
            _("JSON Files (*.json)"),
        )

        if not file_path:
            return

        try:
            preset = {
                "version": 1,
                "name": name,
                "priorities": self._current_priorities,
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(preset, f, indent=2)

            QMessageBox.information(
                self, _("Preset Saved"), _("Preset saved to:\n{path}").format(path=file_path)
            )
        except Exception as e:
            QMessageBox.critical(
                self, _("Error"), _("Failed to save preset:\n{error}").format(error=e)
            )

    def _load_preset(self) -> None:
        """Load priorities from a preset file"""

        presets_dir = self.config.config_dir / "presets"
        presets_dir.mkdir(parents=True, exist_ok=True)

        file_path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Load Metadata Priority Preset"),
            str(presets_dir),
            _("JSON Files (*.json)"),
        )

        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                preset = json.load(f)
        except json.JSONDecodeError as e:
            QMessageBox.critical(
                self, _("Load Failed"),
                _("The file contains invalid JSON (syntax error).\n\n"
                  "Line {line}, column {col}: {msg}\n\n"
                  "Open the file in a text editor and check that line "
                  "for missing commas, brackets, or quotes.").format(
                    line=e.lineno, col=e.colno, msg=e.msg)
            )
            return
        except OSError as e:
            QMessageBox.critical(
                self, _("Load Failed"), _("Could not read the file:\n{error}").format(error=e)
            )
            return

        # Validate structure
        if not isinstance(preset, dict):
            QMessageBox.critical(
                self, _("Load Failed"),
                _("The file does not contain a valid preset.\n\n"
                  'Expected a JSON object with a "priorities" key, like:\n'
                  '{"name": "My Preset", "priorities": {"title": ["steam", "gog"], ...}}')
            )
            return

        if "priorities" not in preset:
            QMessageBox.critical(
                self, _("Load Failed"),
                _('The preset file is missing the "priorities" key.\n\n'
                  "A valid preset looks like:\n"
                  '{"name": "My Preset", "priorities": {"title": ["steam", "gog"], ...}}')
            )
            return

        if not isinstance(preset["priorities"], dict):
            QMessageBox.critical(
                self, _("Load Failed"),
                _('The "priorities" key should contain an object mapping field names '
                  "to source lists, like:\n"
                  '{"title": ["steam", "gog", "igdb"], ...}')
            )
            return

        # Load priorities (only for known fields)
        loaded = 0
        skipped = []
        for field_name, priority in preset["priorities"].items():
            if field_name in self._current_priorities:
                if not isinstance(priority, list):
                    skipped.append(
                        f'  "{field_name}": expected a list,'
                        f' got {type(priority).__name__}'
                    )
                    continue
                self._current_priorities[field_name] = priority
                self._update_button_text(field_name)
                loaded += 1
            else:
                skipped.append(f'  "{field_name}": not a recognized field name')

        name = preset.get("name", _("Unknown"))
        msg = _(
            "Loaded preset: {name}\n\n"
            "{count} field(s) updated."
        ).format(name=name, count=loaded)
        if skipped:
            details = "\n".join(skipped[:10])
            suffix = (
                "\n  ... and {n} more".format(
                    n=len(skipped) - 10
                )
                if len(skipped) > 10
                else ""
            )
            msg += _(
                "\n\nSkipped entries:\n{details}{suffix}"
            ).format(details=details, suffix=suffix)

        QMessageBox.information(self, _("Preset Loaded"), msg)

    def _open_preview(self) -> None:
        """Open preview dialog"""
        if not self.game_service:
            QMessageBox.information(
                self,
                _("Preview Unavailable"),
                _("The preview requires a game service. Please try again from the main window.")
            )
            return

        from .metadata_preview import MetadataPreviewDialog

        # Get enabled AND authenticated plugins only
        enabled_plugins = self._get_enabled_plugins(authenticated_only=True)

        dialog = MetadataPreviewDialog(
            game_service=self.game_service,
            priorities=self._current_priorities,
            enabled_plugins=enabled_plugins,
            parent=self,
        )
        dialog.exec()


class PrivacySettingsTab(QWidget):
    """Privacy settings tab"""

    def __init__(self, config: Config, plugin_manager=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self._plugin_manager = plugin_manager
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- Local Data Access group ---
        consent_group = QGroupBox(_("Local Data Access"))
        consent_layout = QVBoxLayout(consent_group)
        consent_layout.setSpacing(8)

        self.consent_checkbox = QCheckBox(
            _("Allow access to browser cookies and local game launcher data")
        )
        self.consent_checkbox.setToolTip(
            _("Required for GOG browser login, Steam VDF import, "
              "and Heroic tag/favourite sync")
        )
        self.consent_checkbox.setChecked(
            self.config.get("privacy.local_data_access_consent", False)
        )
        consent_layout.addWidget(self.consent_checkbox)

        consent_desc = QLabel(
            _("Lets {app_name} read browser cookies to log into game stores, "
              "and import tags, favorites, and playtime from local launchers "
              "(Steam, GOG Galaxy, Heroic). Nothing is sent anywhere \u2014 "
              "all imported data stays on your computer.").format(
                app_name=APP_NAME)
        )
        consent_desc.setWordWrap(True)
        consent_desc.setObjectName("hintLabel")
        consent_layout.addWidget(consent_desc)

        layout.addWidget(consent_group)

        # --- Your Data group ---
        data_group = QGroupBox(_("Your Data"))
        data_layout = QVBoxLayout(data_group)
        data_layout.setSpacing(6)

        data_items = [
            _("Everything stays on your computer \u2014 game data, "
              "settings, metadata, all of it"),
            _("No account needed \u2014 {app_name} has no server, "
              "no cloud, no tracking").format(app_name=APP_NAME),
            _("Nothing goes online unless you turn on a feature "
              "that needs it"),
            _("Passwords and API keys are kept in your system's "
              "secure storage (keyring)"),
            _("Browser cookies are only read, never changed or copied"),
        ]
        for item_text in data_items:
            item_label = QLabel("\u2022  " + item_text)
            item_label.setWordWrap(True)
            item_label.setContentsMargins(8, 0, 0, 0)
            data_layout.addWidget(item_label)

        layout.addWidget(data_group)

        # --- Online Features group ---
        network_group = QGroupBox(_("Online Features"))
        network_layout = QVBoxLayout(network_group)
        network_layout.setSpacing(8)

        network_intro = QLabel(
            _("When you enable store or metadata plugins, {app_name} "
              "connects to these services to fetch game information:").format(
                app_name=APP_NAME)
        )
        network_intro.setWordWrap(True)
        network_layout.addWidget(network_intro)

        # Build service list dynamically from loaded plugins
        store_names = []
        metadata_names = []
        if self._plugin_manager:
            for name, loaded in self._plugin_manager._loaded.items():
                if not loaded.enabled:
                    continue
                if "store" in loaded.metadata.plugin_types:
                    store_names.append(loaded.metadata.display_name)
                elif "metadata" in loaded.metadata.plugin_types:
                    metadata_names.append(loaded.metadata.display_name)

        store_str = ", ".join(sorted(store_names)) if store_names else _("none enabled")
        metadata_str = ", ".join(sorted(metadata_names)) if metadata_names else _("none enabled")

        service_items = [
            _("<b>Game stores</b> \u2014 {stores} (using your credentials)").format(
                stores=store_str),
            _("<b>Game databases</b> \u2014 {databases}").format(
                databases=metadata_str),
            _("<b>Update check</b> \u2014 checks for new {app_name} versions").format(
                app_name=APP_NAME),
        ]
        for svc_text in service_items:
            svc_label = QLabel("\u2022  " + svc_text)
            svc_label.setTextFormat(Qt.TextFormat.RichText)
            svc_label.setWordWrap(True)
            svc_label.setContentsMargins(8, 0, 0, 0)
            network_layout.addWidget(svc_label)

        proxy_text = QLabel(
            _("IGDB access goes through a free proxy on Cloudflare. "
              "You can use your own Twitch API credentials instead "
              "if you prefer (see Plugins tab).").format(app_name=APP_NAME)
        )
        proxy_text.setWordWrap(True)
        proxy_text.setObjectName("hintLabel")
        network_layout.addWidget(proxy_text)

        offline_text = QLabel(
            _("You can disable any of these in the Plugins tab, or go fully "
              "offline with the offline toggle in the bottom-right corner "
              "of the main window.")
        )
        offline_text.setWordWrap(True)
        offline_text.setObjectName("hintLabel")
        network_layout.addWidget(offline_text)

        layout.addWidget(network_group, 1)

    def save_settings(self) -> None:
        consent = self.consent_checkbox.isChecked()
        self.config.set("privacy.local_data_access_consent", consent)
        # Propagate consent change to all loaded plugins
        if self._plugin_manager and hasattr(self._plugin_manager, 'refresh_all_consent'):
            self._plugin_manager.refresh_all_consent()

    def reset_to_defaults(self) -> None:
        self.consent_checkbox.setChecked(False)


class TagSettingsTab(QWidget):
    """Tag synchronization settings tab.

    Centralizes tag sync configuration from individual plugins into one place.
    """

    source_colors_changed = Signal(bool)  # Emitted when source colors toggle changes

    def __init__(
        self, config: Config,
        plugin_manager: PluginManager,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager

        self._setup_ui()
        self._load_settings()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- Tag Synchronization header ---
        header = QLabel(_("Tag Synchronization"))
        header.setObjectName("dialogDescription")
        layout.addWidget(header)

        # Global default sync mode
        global_form = QFormLayout()
        self.combo_default_mode = QComboBox()
        self.combo_default_mode.addItem(_("Add only"), "add_only")
        self.combo_default_mode.addItem(_("Full sync"), "full_sync")
        self.combo_default_mode.addItem(_("Disabled"), "none")
        self.combo_default_mode.setToolTip(
            _("Add only: import new tags, keep manually deleted ones.\n"
              "Full sync: mirror source tags exactly (adds and removes).\n"
              "Disabled: do not import tags from any source.")
        )
        global_form.addRow(_("Default sync mode:"), self.combo_default_mode)
        layout.addLayout(global_form)

        # Suppress re-import checkbox
        self.chk_suppress = QCheckBox(_("Suppress re-import of deleted tags"))
        self.chk_suppress.setToolTip(
            _("When enabled, imported tags that you delete will not be recreated on the next sync.")
        )
        layout.addWidget(self.chk_suppress)

        # --- Per-Plugin Tag Sync ---
        plugin_group = QGroupBox(_("Per-Plugin Tag Sync"))
        plugin_group_layout = QVBoxLayout(plugin_group)
        plugin_group_layout.setSpacing(4)

        # Scroll area for many plugins
        self._plugin_scroll = QScrollArea()
        self._plugin_scroll.setWidgetResizable(True)
        self._plugin_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._plugin_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        scroll_content = QWidget()
        self._plugin_content_layout = QVBoxLayout(scroll_content)
        self._plugin_content_layout.setContentsMargins(0, 0, 0, 0)
        self._plugin_content_layout.setSpacing(4)
        self._plugin_scroll.setWidget(scroll_content)

        plugin_group_layout.addWidget(self._plugin_scroll)

        # Will be populated dynamically in _load_settings
        self._plugin_rows: Dict[str, Dict[str, Any]] = {}
        self._plugin_grid: Optional[QGridLayout] = None
        self._plugin_grid_row: int = 0

        layout.addWidget(plugin_group, 1)  # stretch=1: expand to fill

        # --- Display (pinned to bottom) ---
        display_group = QGroupBox(_("Display"))
        display_layout = QVBoxLayout(display_group)

        self.chk_source_colors = QCheckBox(_("Show source color accents on tags"))
        self.chk_source_colors.setToolTip(
            _("When enabled, imported tags show a colored "
              "left border indicating their source "
              "(Steam blue, GOG purple, etc.)")
        )
        display_layout.addWidget(self.chk_source_colors)

        layout.addWidget(display_group)

    def _load_settings(self) -> None:
        """Load tag settings from config and populate plugin rows."""
        # Global default
        mode = self.config.get("tags.default_sync_mode", "add_only")
        for i in range(self.combo_default_mode.count()):
            if self.combo_default_mode.itemData(i) == mode:
                self.combo_default_mode.setCurrentIndex(i)
                break

        # Suppress re-import
        self.chk_suppress.setChecked(
            self.config.get("tags.suppress_deleted_reimport", True)
        )

        # Source colors
        self.chk_source_colors.setChecked(
            self.config.get("tags.source_colors", False)
        )

        # Build plugin rows dynamically
        self._build_plugin_rows()

    def _build_plugin_rows(self) -> None:
        """Build per-plugin tag sync rows from discovered plugins.

        Uses QGridLayout for aligned columns:
          col 0: enabled checkbox (fixed width)
          col 1: sync mode combo (stretch)
          col 2: extras like "Import favourites" (optional)
        """
        # Clear existing content
        for row_data in self._plugin_rows.values():
            if row_data.get("widget"):
                row_data["widget"].deleteLater()
        self._plugin_rows.clear()

        # Clear the content layout
        while self._plugin_content_layout.count():
            item = self._plugin_content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                # Clear sub-layout items
                sub = item.layout()
                while sub.count():
                    sub_item = sub.takeAt(0)
                    if sub_item.widget():
                        sub_item.widget().deleteLater()

        self._plugin_grid = None
        self._plugin_grid_row = 0

        overrides = self.config.get("tags.plugin_overrides", {})

        # Discover tag-sync-capable plugins
        active_plugins = []
        inactive_plugins = []

        for plugin_name, plugin in self.plugin_manager.get_all_plugins().items():
            has_tag_sync = hasattr(plugin, "get_tag_sync_data")
            loaded = self.plugin_manager.get_loaded_plugin(plugin_name)
            meta = loaded.metadata if loaded else None
            has_tag_capability = (
                meta.capabilities.get("tag_sync", False)
                if meta else False
            )
            if not has_tag_sync and not has_tag_capability:
                continue

            display_name = meta.display_name if meta else plugin_name
            is_enabled = loaded.enabled if loaded else False

            entry = (plugin_name, display_name, overrides.get(plugin_name, {}))
            if is_enabled:
                active_plugins.append(entry)
            else:
                inactive_plugins.append(entry)

        # Create grid layout for aligned columns
        self._plugin_grid = QGridLayout()
        self._plugin_grid.setContentsMargins(4, 0, 4, 0)
        self._plugin_grid.setHorizontalSpacing(8)
        self._plugin_grid.setVerticalSpacing(4)
        # Col 0: checkbox (auto), Col 1: combo (stretch), Col 2: extras (auto)
        self._plugin_grid.setColumnStretch(1, 1)

        # Add section labels and rows
        if active_plugins:
            label = QLabel(_("Active Plugins:"))
            label.setObjectName("hintLabel")
            self._plugin_grid.addWidget(label, self._plugin_grid_row, 0, 1, 3)
            self._plugin_grid_row += 1
            for pname, dname, override in active_plugins:
                self._add_plugin_row(pname, dname, override)

        if inactive_plugins:
            label = QLabel(_("Inactive Plugins:"))
            label.setObjectName("hintLabel")
            self._plugin_grid.addWidget(label, self._plugin_grid_row, 0, 1, 3)
            self._plugin_grid_row += 1
            for pname, dname, override in inactive_plugins:
                self._add_plugin_row(pname, dname, override)

        if not active_plugins and not inactive_plugins:
            label = QLabel(_("No tag-sync-capable plugins found."))
            label.setObjectName("hintLabel")
            self._plugin_grid.addWidget(label, self._plugin_grid_row, 0, 1, 3)
            self._plugin_grid_row += 1

        self._plugin_content_layout.addLayout(self._plugin_grid)
        self._plugin_content_layout.addStretch()

    def _add_plugin_row(self, plugin_name: str, display_name: str, override: dict) -> None:
        """Add a row for a tag-sync-capable plugin into the grid."""
        row = self._plugin_grid_row

        # Col 0: Enabled checkbox
        chk_enabled = QCheckBox(display_name)
        chk_enabled.setToolTip(
            _("Enable or disable tag import from {source}").format(source=display_name)
        )
        chk_enabled.setChecked(override.get("enabled", True))
        self._plugin_grid.addWidget(chk_enabled, row, 0)

        # Col 1: Sync mode combo
        combo_mode = QComboBox()
        combo_mode.setToolTip(
            _("Override the default sync mode for this source")
        )
        default_label = self.combo_default_mode.currentText()
        combo_mode.addItem(
            _("Use default ({mode})").format(mode=default_label),
            "default",
        )
        combo_mode.addItem(_("Add only"), "add_only")
        combo_mode.addItem(_("Full sync"), "full_sync")
        combo_mode.addItem(_("Disabled"), "none")

        # Set current mode
        current_mode = override.get("sync_mode", "default")
        for i in range(combo_mode.count()):
            if combo_mode.itemData(i) == current_mode:
                combo_mode.setCurrentIndex(i)
                break

        self._plugin_grid.addWidget(combo_mode, row, 1)

        # Col 2: Plugin-specific extras (e.g. Heroic "Import favourites")
        chk_favourites = None
        if plugin_name == "heroic":
            chk_favourites = QCheckBox(_("Import favourites"))
            chk_favourites.setToolTip(
                _("Sync Heroic favourites to the luducat favourites list")
            )
            chk_favourites.setChecked(override.get("import_favourites", True))
            self._plugin_grid.addWidget(chk_favourites, row, 2)

        self._plugin_grid_row += 1

        # No separate widget — grid cells are the widgets
        self._plugin_rows[plugin_name] = {
            "widget": chk_enabled,  # For cleanup (grid owns all widgets)
            "chk_enabled": chk_enabled,
            "combo_mode": combo_mode,
            "chk_favourites": chk_favourites,
        }

        # Update "Use default" label when global default changes
        self.combo_default_mode.currentIndexChanged.connect(
            lambda: combo_mode.setItemText(
                0,
                _("Use default ({mode})").format(mode=self.combo_default_mode.currentText()),
            )
        )

    def save_settings(self) -> None:
        """Save tag settings to config."""
        # Global default
        self.config.set(
            "tags.default_sync_mode",
            self.combo_default_mode.currentData(),
        )

        # Suppress re-import
        self.config.set("tags.suppress_deleted_reimport", self.chk_suppress.isChecked())

        # Source colors
        new_source_colors = self.chk_source_colors.isChecked()
        old_source_colors = self.config.get("tags.source_colors", False)
        self.config.set("tags.source_colors", new_source_colors)
        if new_source_colors != old_source_colors:
            self.source_colors_changed.emit(new_source_colors)

        # Per-plugin overrides
        overrides = {}
        for plugin_name, row_data in self._plugin_rows.items():
            override = {
                "enabled": row_data["chk_enabled"].isChecked(),
                "sync_mode": row_data["combo_mode"].currentData(),
            }
            if row_data.get("chk_favourites") is not None:
                override["import_favourites"] = row_data["chk_favourites"].isChecked()
            overrides[plugin_name] = override

        self.config.set("tags.plugin_overrides", overrides)

    def reset_to_defaults(self) -> None:
        """Reset tag settings to defaults."""
        self.combo_default_mode.setCurrentIndex(0)  # add_only
        self.chk_suppress.setChecked(True)
        self.chk_source_colors.setChecked(False)
        for row_data in self._plugin_rows.values():
            row_data["chk_enabled"].setChecked(True)
            row_data["combo_mode"].setCurrentIndex(0)  # Use default
            if row_data.get("chk_favourites") is not None:
                row_data["chk_favourites"].setChecked(True)


_LAUNCHER_TOOLTIPS = {
    "native_runner": N_("Run games directly without a launcher"),
    "heroic_runner": N_("Open-source launcher for GOG and Epic games"),
    "steam_runner": N_("Launches games through your Steam client"),
    "epic_launcher_runner": N_("Official Epic Games Launcher (Windows only)"),
    "galaxy_runner": N_("Official GOG Galaxy client (Windows only)"),
    "dosbox": N_("Emulator for classic DOS games"),
    "scummvm": N_("Emulator for classic point-and-click adventures"),
    "wine": N_("Runs Windows games on Linux"),
}


class LaunchingSettingsTab(QWidget):
    """Launching tab — store-centric launch method combos on top,
    launcher/emulator detection status on bottom.

    Shows only enabled runner/platform plugins. Reads detection status from
    RuntimeManager and plugin settings.
    """

    def __init__(
        self,
        config: Config,
        plugin_manager: PluginManager,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.plugin_manager = plugin_manager

        # Widgets tracked for save
        self._path_edits: Dict[str, Tuple[QLineEdit, str, str]] = {}
        # key -> (line_edit, config_key, auto_detected_path)
        self._store_runner_combos: Dict[str, Tuple[QComboBox, str]] = {}
        # store_name -> (combo, config_key)
        self._status_labels: Dict[str, QLabel] = {}
        # plugin_name -> status label (for reset refresh)
        self._runtime_combos: Dict[str, Tuple[QComboBox, str]] = {}
        # plugin_name -> (combo, config_key)

        self._setup_ui()

    def _get_runtime_manager(self):
        """Get RuntimeManager singleton (may not be initialized yet)."""
        try:
            from ...core.runtime_manager import get_runtime_manager
            rm = get_runtime_manager()
            if rm._initialized:
                return rm
        except Exception:
            pass
        return None

    @staticmethod
    def _get_plat_map() -> str:
        """Return normalized platform string."""
        import sys as _sys
        return {"win32": "windows", "darwin": "darwin"}.get(
            _sys.platform, "linux"
        )

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(0, 0, 0, 0)

        # === Section 1: Default launch methods (top — user-facing config) ===
        self._build_launch_methods(layout)

        # === Section 2: Launcher & emulator status (bottom — reference) ===
        self._build_launcher_status(layout)

        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ── Section builders ──────────────────────────────────────────────

    def _build_launch_methods(self, layout: QVBoxLayout) -> None:
        """Build store-centric launch method combo section."""
        import sys as _sys

        discovered = self.plugin_manager.get_discovered_plugins()
        plat_map = self._get_plat_map()

        # Find all enabled store plugins, sorted by display name
        store_plugins = []
        for name, meta in discovered.items():
            ptypes = meta.plugin_types or []
            if "store" not in ptypes:
                continue
            if not self.plugin_manager.is_plugin_enabled(name):
                continue
            store_plugins.append((name, meta.display_name))
        store_plugins.sort(key=lambda x: x[1].lower())

        if not store_plugins:
            return

        # Build runner choices per store (no availability check — show all compatible)
        runner_choices_by_store: Dict[str, list] = {}
        for store_name, _display in store_plugins:
            choices = []
            for rname, rmeta in discovered.items():
                rptypes = rmeta.plugin_types or []
                if "runner" not in rptypes:
                    continue
                if not self.plugin_manager.is_plugin_enabled(rname):
                    continue
                rcaps = rmeta.capabilities or {}
                supported = rcaps.get("supported_stores", [])
                if store_name not in supported:
                    continue
                # Platform filter
                rplatforms = rcaps.get("platforms", [])
                if rplatforms and plat_map not in rplatforms:
                    continue
                if rcaps.get("status") in ("stub",):
                    continue
                choices.append({"value": rname, "label": rmeta.display_name})
            choices.sort(key=lambda c: c["label"].lower())
            runner_choices_by_store[store_name] = choices

        group = QGroupBox(_("Choose your launch methods"))
        form = QFormLayout(group)
        form.setSpacing(12)

        for store_name, store_display in store_plugins:
            choices = runner_choices_by_store.get(store_name, [])
            combo = QComboBox()
            combo.setToolTip(
                _("Which launcher to use when launching {store} games").format(
                    store=store_display)
            )

            if not choices:
                combo.addItem(_("No launcher available"), "")
                combo.setEnabled(False)
            else:
                for choice in choices:
                    combo.addItem(choice["label"], choice["value"])

                config_key = f"plugins.{store_name}.default_runner"
                current = self.config.get(config_key, "")
                if current:
                    idx = combo.findData(current)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                else:
                    # Platform-aware defaults
                    if _sys.platform != "win32":
                        # Linux/macOS: prefer Heroic for GOG/Epic
                        heroic_idx = combo.findData("heroic_runner")
                        if heroic_idx >= 0:
                            combo.setCurrentIndex(heroic_idx)
                    else:
                        # Windows: prefer Galaxy for GOG, Epic Launcher for Epic
                        if store_name == "gog":
                            galaxy_idx = combo.findData("galaxy_runner")
                            if galaxy_idx >= 0:
                                combo.setCurrentIndex(galaxy_idx)
                        elif store_name == "epic":
                            epic_idx = combo.findData("epic_launcher_runner")
                            if epic_idx >= 0:
                                combo.setCurrentIndex(epic_idx)

            label_text = _("{store} games launch via:").format(store=store_display)
            form.addRow(label_text, combo)
            self._store_runner_combos[store_name] = (
                combo, f"plugins.{store_name}.default_runner"
            )

        hint = QLabel(
            _("Default launch method per store. Can be overridden "
              "in individual game settings.")
        )
        hint.setObjectName("fieldDescription")
        hint.setWordWrap(True)
        form.addRow("", hint)

        layout.addWidget(group)

    def _build_launcher_status(self, layout: QVBoxLayout) -> None:
        """Build launcher/emulator detection status grid."""
        import platform as _platform
        import sys as _sys

        rm = self._get_runtime_manager()
        discovered = self.plugin_manager.get_discovered_plugins()
        plat_map = self._get_plat_map()

        available_runners = rm.get_available_runners() if rm else {}
        available_platforms = rm.get_available_platforms() if rm else []

        # Build platform lookup: plugin_name -> list[PlatformInfo]
        platform_lookup: Dict[str, list] = {}
        for pi in available_platforms:
            pname = pi.platform_id.split("/")[0] if "/" in pi.platform_id else pi.platform_id
            platform_lookup.setdefault(pname, []).append(pi)

        # Categorize plugins into 4 groups
        native_entries = []     # native_runner
        store_runners = []      # runners with non-empty supported_stores (excl. wine)
        nonstore_runners = []   # runners with empty supported_stores (excl. native)
        platform_entries = []   # platform plugins

        for name, meta in discovered.items():
            ptypes = meta.plugin_types or []
            if "runner" not in ptypes and "platform" not in ptypes:
                continue
            if not self.plugin_manager.is_plugin_enabled(name):
                continue
            caps = meta.capabilities or {}
            if caps.get("status") in ("stub",):
                continue

            entry = self._build_status_entry(
                name, meta, plat_map, available_runners, platform_lookup,
            )
            if entry is None:
                continue

            if "platform" in ptypes:
                platform_entries.append(entry)
            elif name == "native_runner":
                native_entries.append(entry)
            elif caps.get("supported_stores"):
                store_runners.append(entry)
            else:
                nonstore_runners.append(entry)

        # Sort within categories alphabetically
        for group in (store_runners, nonstore_runners, platform_entries):
            group.sort(key=lambda e: e["display_name"].lower())

        group = QGroupBox(_("Launcher status"))
        grid = QGridLayout(group)
        grid.setSpacing(8)
        grid.setColumnStretch(2, 1)  # Path column stretches

        # Header row
        for col, text in enumerate(
            [_("Launcher"), _("Status"), _("Path"), "", ""]
        ):
            lbl = QLabel(f"<b>{text}</b>") if text else QLabel("")
            grid.addWidget(lbl, 0, col)

        row = 1
        categories = [
            native_entries,
            store_runners,
            nonstore_runners,
            platform_entries,
        ]
        for cat_idx, group_items in enumerate(categories):
            if not group_items:
                continue
            # Separator between groups (not before first)
            if row > 1:
                sep = QFrame()
                sep.setObjectName("gridSeparator")
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setFrameShadow(QFrame.Shadow.Sunken)
                grid.addWidget(sep, row, 0, 1, 5)
                row += 1

            for entry in group_items:
                self._add_status_row(grid, row, entry)
                grid.setRowMinimumHeight(row, 30)
                row += 1

        layout.addWidget(group)

    def _build_status_entry(
        self,
        name: str,
        meta,
        plat_map: str,
        available_runners: dict,
        platform_lookup: dict,
    ) -> Optional[dict]:
        """Build a status entry dict for one plugin."""
        import platform as _platform

        ptypes = meta.plugin_types or []
        caps = meta.capabilities or {}
        schema = meta.settings_schema or {}
        display_name = meta.display_name

        # Platform availability
        is_platform_available = True
        if "runner" in ptypes:
            platforms = caps.get("platforms", [])
            if platforms and plat_map not in platforms:
                is_platform_available = False

        is_bridge = False
        if "runner" in ptypes:
            if name == "native_runner":
                status = _("Detected")
                path_str = ""
                hint_text = _("Per-game executable assignment")
            elif not is_platform_available:
                status = _("Not available")
                path_str = ""
                hint_text = ""
            else:
                runner_name = (
                    name.removesuffix("_runner")
                    if name.endswith("_runner") else name
                )

                # Bridge runners (e.g. Playnite) — status from plugin,
                # no path, Configure button instead
                loaded = self.plugin_manager.get_loaded_plugin(name)
                plugin_inst = loaded.instance if loaded else None
                is_bridge = (
                    plugin_inst
                    and getattr(plugin_inst, "has_bridge_pairing", False)
                )

                info = available_runners.get(runner_name)
                hint_text = ""
                if is_bridge:
                    bridge_status = plugin_inst.get_bridge_status()
                    bs = bridge_status.get("status", "not_configured")
                    if bs == "connected":
                        status = _("Connected")
                    elif bs == "paired":
                        status = _("Paired")
                    else:
                        status = _("Not configured")
                    path_str = ""
                elif info:
                    if info.install_type == "reroute":
                        # Rerouting is internal — show as unavailable
                        status = _("Not available")
                        path_str = ""
                        is_platform_available = False
                    else:
                        status = _("Detected")
                        path_str = str(info.path) if info.path else ""
                        if not path_str and info.url_scheme:
                            # Try to find the actual binary for
                            # URL-scheme runners (e.g. Steam)
                            try:
                                from ...plugins.sdk.app_finder import (
                                    find_application,
                                )
                                results = find_application([runner_name])
                                if results:
                                    path_str = str(results[0].path)
                            except Exception:
                                pass
                            if not path_str:
                                path_str = info.url_scheme
                else:
                    status = _("Not found")
                    path_str = ""

            # Config key for path override
            path_key = ""
            path_type = "directory"
            for k, field_def in schema.items():
                if isinstance(field_def, dict) and field_def.get("type") == "path":
                    path_key = f"plugins.{name}.{k}"
                    path_type = field_def.get("path_type", "directory")
                    break

            # Fallback: allow manual path override for detected executables
            if (not path_key and path_str
                    and not path_str.startswith(
                        ("steam://", "heroic://", "com."))):
                path_key = f"plugins.{name}.path_override"
                path_type = "file"

        elif "platform" in ptypes:
            detected_list = platform_lookup.get(name, [])
            hint_text = ""
            if detected_list:
                best = detected_list[0]
                status = _("Detected")
                path_str = (
                    str(best.executable_path) if best.executable_path else ""
                )
            else:
                status = _("Not found")
                path_str = ""

            path_key = ""
            path_type = "file"
            for k, field_def in schema.items():
                if isinstance(field_def, dict) and field_def.get("type") == "path":
                    path_key = f"plugins.{name}.{k}"
                    path_type = field_def.get("path_type", "file")
                    break

            # Fallback: allow manual path override for detected executables
            if not path_key and path_str:
                path_key = f"plugins.{name}.path_override"
                path_type = "file"
        else:
            return None

        # Wine platform gets a runtime dropdown instead of path field
        use_runtime_dropdown = (
            "platform" in ptypes
            and name == "wine"
            and caps.get("runtime_type") == "wine"
        )

        # Bridge runners use a Configure button instead of path field
        use_bridge_configure = is_bridge

        return {
            "plugin_name": name,
            "display_name": display_name,
            "status": status,
            "path_str": path_str,
            "path_key": path_key,
            "path_type": path_type,
            "hint_text": hint_text,
            "is_platform_available": is_platform_available,
            "use_runtime_dropdown": use_runtime_dropdown,
            "use_bridge_configure": use_bridge_configure,
        }

    def _add_status_row(self, grid: QGridLayout, row: int, entry: dict) -> None:
        """Add a single launcher status row to the grid."""
        plugin_name = entry["plugin_name"]
        display_name = entry["display_name"]
        status = entry["status"]
        path_str = entry["path_str"]
        path_key = entry["path_key"]
        path_type = entry["path_type"]
        hint_text = entry["hint_text"]
        is_platform_available = entry["is_platform_available"]

        # Row tooltip
        tooltip = _(_LAUNCHER_TOOLTIPS.get(plugin_name, ""))

        # Name label — gray out if not available on this platform
        name_label = QLabel(display_name)
        if tooltip:
            name_label.setToolTip(tooltip)
        if not is_platform_available:
            name_label.setEnabled(False)
        grid.addWidget(name_label, row, 0)

        # Status label
        status_label = QLabel(status)
        if tooltip:
            status_label.setToolTip(tooltip)
        if not is_platform_available:
            status_label.setEnabled(False)
            status_label.setObjectName("pluginStatusNotAvailable")
        elif status in (_("Detected"), _("Connected"), _("Paired")):
            status_label.setObjectName("pluginStatusConfigured")
        elif status in (_("Not found"), _("Not configured")):
            status_label.setObjectName("pluginStatusNotConfigured")
        grid.addWidget(status_label, row, 1)
        self._status_labels[plugin_name] = status_label

        # Special: native runner or platform-unavailable — show hint
        if hint_text:
            hint = QLabel(hint_text)
            hint.setObjectName("hintLabel")
            if not is_platform_available:
                hint.setEnabled(False)
            grid.addWidget(hint, row, 2)
            grid.addWidget(QLabel(""), row, 3)
            grid.addWidget(QLabel(""), row, 4)
            return

        if not is_platform_available and not path_key:
            # Use a disabled QLineEdit to match row height of available items
            placeholder = QLineEdit()
            placeholder.setReadOnly(True)
            placeholder.setEnabled(False)
            placeholder.setObjectName("launcherPathEdit")
            placeholder.setPlaceholderText("—")
            grid.addWidget(placeholder, row, 2)
            grid.addWidget(QLabel(""), row, 3)
            grid.addWidget(QLabel(""), row, 4)
            return

        # Special: Bridge runner gets a Configure button instead of path
        if entry.get("use_bridge_configure"):
            hint = QLabel(_("Bridge (IPC)"))
            hint.setObjectName("hintLabel")
            grid.addWidget(hint, row, 2)

            configure_btn = QPushButton()
            configure_btn.setFixedSize(28, 28)
            configure_btn.setIcon(load_tinted_icon("cogwheel.svg", 16))
            configure_btn.setToolTip(
                _("Configure bridge connection and pairing")
            )

            def _open_bridge_config(checked=False, _name=plugin_name):
                from .plugin_config import PluginConfigDialog
                dlg = PluginConfigDialog(
                    plugin_name=_name,
                    config=self.config,
                    plugin_manager=self.plugin_manager,
                    credentials=self.plugin_manager.credential_manager,
                    parent=self,
                )
                dlg.exec()
                # Refresh status after configure
                self._refresh_launcher_status()

            configure_btn.clicked.connect(_open_bridge_config)
            grid.addWidget(
                configure_btn, row, 3, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(QLabel(""), row, 4)
            return

        # Special: Wine platform gets a runtime picker + Configure button
        if entry.get("use_runtime_dropdown") and is_platform_available:
            combo = QComboBox()
            combo.setToolTip(
                _("Select the default Wine or Proton runtime for Windows games")
            )

            # Populate from RuntimeScanner (already sorted by recommendation)
            try:
                from luducat.plugins.platforms.wine.runtime_scanner import (
                    scan_installed_runtimes,
                )
                for rt in scan_installed_runtimes():
                    combo.addItem(rt.display_label, rt.identifier)
            except Exception:
                pass

            config_key = f"plugins.{plugin_name}.default_runtime"
            current = self.config.get(config_key, "")
            # Treat legacy "auto" same as empty — stays on index 0
            if current and current != "auto":
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

            grid.addWidget(combo, row, 2)
            self._runtime_combos[plugin_name] = (combo, config_key)

            # Configure button opens WineConfigDialog
            configure_btn = QPushButton()
            configure_btn.setFixedSize(28, 28)
            configure_btn.setIcon(load_tinted_icon("cogwheel.svg", 16))
            configure_btn.setToolTip(
                _("Open Wine configuration (per-runtime and per-game settings)")
            )

            def _open_wine_config(
                checked=False, _combo=combo, _key=config_key,
            ):
                from luducat.plugins.platforms.wine.config_dialog import (
                    WineConfigDialog,
                )
                dlg = WineConfigDialog(
                    self.config, self.plugin_manager, parent=self,
                )
                if dlg.exec() == WineConfigDialog.DialogCode.Accepted:
                    # Refresh runtime combo to match saved value
                    saved = self.config.get(_key, "")
                    if saved:
                        new_idx = _combo.findData(saved)
                        if new_idx >= 0:
                            _combo.setCurrentIndex(new_idx)
                    else:
                        _combo.setCurrentIndex(0)

            configure_btn.clicked.connect(_open_wine_config)
            grid.addWidget(
                configure_btn, row, 3, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(QLabel(""), row, 4)
            return

        # Path: show user override if set, else auto-detected in italic
        user_path = self.config.get(path_key, "") if path_key else ""
        path_edit = QLineEdit()
        path_edit.setReadOnly(True)
        path_edit.setObjectName("launcherPathEdit")
        if tooltip:
            path_edit.setToolTip(tooltip)
        if not is_platform_available:
            path_edit.setEnabled(False)
            path_edit.setPlaceholderText("—")
        elif user_path:
            path_edit.setText(user_path)
        elif path_str:
            path_edit.setText(path_str)
            path_edit.setObjectName("launcherPathEditAuto")
        else:
            path_edit.setPlaceholderText("—")
        grid.addWidget(path_edit, row, 2)

        if path_key and is_platform_available:
            # Browse button
            browse_btn = QPushButton()
            browse_btn.setFixedSize(28, 28)
            browse_btn.setIcon(load_tinted_icon("folder-open.svg", 16))
            browse_btn.setToolTip(_("Browse for custom path"))

            def _browse(
                checked=False, _key=path_key, _edit=path_edit,
                _ptype=path_type, _auto=path_str,
            ):
                if _ptype == "file":
                    path, _filt = QFileDialog.getOpenFileName(
                        self, _("Select executable"), "",
                    )
                else:
                    path = QFileDialog.getExistingDirectory(
                        self, _("Select directory"), "",
                    )
                if path:
                    _edit.setText(path)
                    _edit.setStyleSheet("")

            browse_btn.clicked.connect(_browse)
            grid.addWidget(
                browse_btn, row, 3, Qt.AlignmentFlag.AlignVCenter)

            # Reset button
            reset_btn = QPushButton()
            reset_btn.setFixedSize(28, 28)
            reset_btn.setIcon(load_tinted_icon("reload.svg", 16))
            reset_btn.setToolTip(_("Reset to auto-detected path"))

            def _reset(
                checked=False, _key=path_key, _edit=path_edit, _auto=path_str,
            ):
                _edit.setText(_auto)
                _edit.setStyleSheet(
                    "font-style: italic; opacity: 0.7;" if _auto else ""
                )

            reset_btn.clicked.connect(_reset)
            grid.addWidget(
                reset_btn, row, 4, Qt.AlignmentFlag.AlignVCenter)

            self._path_edits[plugin_name] = (path_edit, path_key, path_str)
        else:
            grid.addWidget(QLabel(""), row, 3)
            grid.addWidget(QLabel(""), row, 4)

    # ── Save / Reset ──────────────────────────────────────────────────

    def save_settings(self) -> None:
        """Save all launching settings to config."""
        # Path overrides
        for plugin_name, (edit, config_key, auto_path) in self._path_edits.items():
            text = edit.text().strip()
            if text and text != auto_path:
                self.config.set(config_key, text)
            else:
                # Clear override — use auto-detect
                self.config.set(config_key, "")

        # Runtime picker dropdowns (e.g. Wine)
        for plugin_name, (combo, config_key) in self._runtime_combos.items():
            value = combo.currentData()
            self.config.set(config_key, value or "")

        # Store runner defaults
        for store_name, (combo, config_key) in self._store_runner_combos.items():
            value = combo.currentData()
            if value:
                self.config.set(config_key, value)

        # Epic backward compat: sync launch_backend when saving epic default_runner
        epic_combo_data = self._store_runner_combos.get("epic")
        if epic_combo_data:
            runner_value = epic_combo_data[0].currentData()
            backend_map = {
                "heroic_runner": "heroic",
                "epic_launcher_runner": "epic_launcher",
                "wine_runner": "wine",
                "native_runner": "native",
            }
            backend = backend_map.get(runner_value, "")
            if backend:
                self.config.set(
                    "plugins.epic_launcher_runner.launch_backend", backend,
                )

    def _refresh_launcher_status(self) -> None:
        """Refresh status labels for bridge runners after configure dialog."""
        for plugin_name, status_label in self._status_labels.items():
            loaded = self.plugin_manager.get_loaded_plugin(plugin_name)
            plugin_inst = loaded.instance if loaded else None
            if not plugin_inst or not getattr(plugin_inst, "has_bridge_pairing", False):
                continue
            bridge_status = plugin_inst.get_bridge_status()
            bs = bridge_status.get("status", "not_configured")
            if bs == "connected":
                text = _("Connected")
                status_label.setObjectName("pluginStatusConfigured")
            elif bs == "paired":
                text = _("Paired")
                status_label.setObjectName("pluginStatusConfigured")
            else:
                text = _("Not configured")
                status_label.setObjectName("pluginStatusNotConfigured")
            status_label.setText(text)
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)

    def reset_to_defaults(self) -> None:
        """Reset launching settings to defaults."""
        # Re-detect launchers
        rm = self._get_runtime_manager()
        if rm:
            rm._detect_launchers()

        # Clear all path overrides and refresh from detection
        available_runners = rm.get_available_runners() if rm else {}
        available_platforms = rm.get_available_platforms() if rm else []

        # Rebuild platform lookup
        platform_lookup: Dict[str, list] = {}
        for pi in available_platforms:
            pname = (
                pi.platform_id.split("/")[0]
                if "/" in pi.platform_id else pi.platform_id
            )
            platform_lookup.setdefault(pname, []).append(pi)

        for plugin_name, (edit, config_key, _old_auto) in self._path_edits.items():
            # Determine fresh auto-detected path
            new_auto = ""
            runner_name = (
                plugin_name.removesuffix("_runner")
                if plugin_name.endswith("_runner") else plugin_name
            )
            info = available_runners.get(runner_name)
            if info:
                new_auto = str(info.path) if info.path else ""
                if not new_auto and info.url_scheme:
                    new_auto = info.url_scheme
            else:
                detected_list = platform_lookup.get(plugin_name, [])
                if detected_list:
                    new_auto = (
                        str(detected_list[0].executable_path)
                        if detected_list[0].executable_path else ""
                    )

            edit.setText(new_auto)
            edit.setStyleSheet(
                "font-style: italic; opacity: 0.7;" if new_auto else ""
            )
            # Update cached auto path
            self._path_edits[plugin_name] = (edit, config_key, new_auto)

        # Refresh status labels
        for plugin_name, status_label in self._status_labels.items():
            runner_name = (
                plugin_name.removesuffix("_runner")
                if plugin_name.endswith("_runner") else plugin_name
            )
            info = available_runners.get(runner_name)
            detected_list = platform_lookup.get(plugin_name, [])

            if info:
                if info.install_type == "reroute":
                    new_status = _("Not available")
                else:
                    new_status = _("Detected")
            elif detected_list:
                new_status = _("Detected")
            elif plugin_name == "native_runner":
                new_status = _("Detected")
            else:
                new_status = _("Not found")

            status_label.setText(new_status)
            if new_status == _("Detected"):
                status_label.setObjectName("pluginStatusConfigured")
            elif new_status == _("Not available"):
                status_label.setObjectName("pluginStatusNotAvailable")
            else:
                status_label.setObjectName("pluginStatusNotConfigured")
            # Force QSS repaint
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)

        # Reset store runner combos to first item
        for store_name, (combo, config_key) in self._store_runner_combos.items():
            combo.setCurrentIndex(0)


class SettingsDialog(QDialog):
    """Main settings dialog with tabs"""

    configure_plugin = Signal(str)  # plugin_name
    tags_changed = Signal()  # Emitted when tags are modified
    restart_required = Signal()  # Emitted when app needs to restart (e.g., after restore)
    show_update_requested = Signal()  # Close settings and show update dialog

    def __init__(
        self,
        config: Config,
        plugin_manager: PluginManager,
        game_service=None,  # GameService for tag management
        theme_manager=None,  # Optional ThemeManager for live updates
        parent: Optional[QWidget] = None,
        open_tab: Optional[str] = None,  # Tab name to open (e.g. "Backup")
        update_info=None,  # Optional UpdateInfo from update checker
    ):
        super().__init__(parent)

        self.config = config
        self.plugin_manager = plugin_manager
        self.game_service = game_service
        self.theme_manager = theme_manager
        self._update_info = update_info

        self.setWindowTitle(_("Settings"))
        self.setMinimumSize(780, 660)
        self.resize(840, 740)

        self._setup_ui()

        # Switch to requested tab if specified
        if open_tab:
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i) == open_tab:
                    self.tabs.setCurrentIndex(i)
                    break

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Tab widget
        self.tabs = QTabWidget()

        # General tab
        self.general_tab = GeneralSettingsTab(
            self.config, plugin_manager=self.plugin_manager,
            update_info=self._update_info,
        )
        self.tabs.addTab(self.general_tab, _("General"))

        # Appearance tab
        self.appearance_tab = AppearanceSettingsTab(self.config)
        self.tabs.addTab(self.appearance_tab, _("Appearance"))

        # Metadata priority tab
        self.metadata_tab = MetadataSettingsTab(
            self.config, self.plugin_manager, game_service=self.game_service
        )
        self.tabs.addTab(self.metadata_tab, _("Metadata"))

        # Tag Manager is now a standalone dialog (Tools → Tag Manager)

        # Tags tab (sync settings)
        self.tags_tab = TagSettingsTab(self.config, self.plugin_manager)
        self.tabs.addTab(self.tags_tab, _("Tags"))

        # Launching tab
        self.launching_tab = LaunchingSettingsTab(self.config, self.plugin_manager)
        self.tabs.addTab(self.launching_tab, _("Launching"))

        # Plugins tab
        self.plugins_tab = PluginsSettingsTab(
            self.config, self.plugin_manager, game_service=self.game_service
        )
        self.plugins_tab.configure_plugin.connect(self.configure_plugin.emit)
        self.tabs.addTab(self.plugins_tab, _("Plugins"))

        # Backup tab
        self.backup_tab = BackupSettingsTab(self.config)
        self.backup_tab.restart_required.connect(self._on_restart_required)
        self.tabs.addTab(self.backup_tab, _("Backup"))

        # Advanced tab
        self.advanced_tab = AdvancedSettingsTab(self.config)
        self.tabs.addTab(self.advanced_tab, _("Advanced"))

        # Privacy tab (last)
        self.privacy_tab = PrivacySettingsTab(self.config, plugin_manager=self.plugin_manager)
        self.tabs.addTab(self.privacy_tab, _("Privacy"))

        layout.addWidget(self.tabs)

        # Dialog buttons
        button_layout = QHBoxLayout()

        # Reset to Defaults button (left side)
        btn_reset = QPushButton(_("Reset to Defaults"))
        btn_reset.setToolTip(_("Reset all settings in this dialog to their default values"))
        btn_reset.clicked.connect(self._reset_to_defaults)
        button_layout.addWidget(btn_reset)

        button_layout.addStretch()

        # Standard buttons (right side)
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.Apply
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply_settings
        )
        button_layout.addWidget(button_box)

        layout.addLayout(button_layout)

    def reject(self) -> None:
        """Check for unsaved metadata priority changes before closing."""
        if self.metadata_tab.has_unsaved_changes():
            reply = QMessageBox.question(
                self,
                _("Unsaved Changes"),
                _("You have unsaved changes in the metadata priority settings.\n\n"
                  "Discard changes?"),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Discard:
                return
        super().reject()

    def _apply_settings(self) -> None:
        """Apply settings without closing"""
        self.general_tab.save_settings()
        self.appearance_tab.save_settings()
        self.privacy_tab.save_settings()
        self.tags_tab.save_settings()
        self.launching_tab.save_settings()
        self.metadata_tab.save_settings()
        self.plugins_tab.save_settings()
        self.advanced_tab.save_settings()
        self.backup_tab.save_settings()
        self.config.save()
        logger.info("Settings applied")

        # Apply RAM cache budgets immediately
        from ...utils.image_cache import apply_cache_budgets
        apply_cache_budgets(self.config)

        # Apply theme changes immediately
        main_window = self.parent()
        if main_window and hasattr(main_window, "theme_manager"):
            saved_theme = self.config.get("appearance.theme", "system")
            main_window.theme_manager.apply_theme(saved_theme)

    def _on_accept(self) -> None:
        """Apply settings and close"""
        self._apply_settings()
        self.accept()

    def _on_restart_required(self) -> None:
        """Handle restart request from backup restore."""
        self.accept()  # Close dialog first
        self.restart_required.emit()

    def update_plugin_status(self, plugin_name: str, is_authenticated: bool) -> None:
        """Update a plugin's connection status in the plugins tab

        Called when connection test succeeds in the config dialog.
        """
        self.plugins_tab.update_plugin_status(plugin_name, is_authenticated)

    def _reset_to_defaults(self) -> None:
        """Reset all settings to default values."""
        reply = QMessageBox.question(
            self,
            _("Reset to Defaults"),
            _("This will reset all settings to their default values.\n\n"
              "Are you sure?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Reset each tab
        self.general_tab.reset_to_defaults()
        self.appearance_tab.reset_to_defaults()
        self.privacy_tab.reset_to_defaults()
        self.tags_tab.reset_to_defaults()
        self.launching_tab.reset_to_defaults()
        self.metadata_tab.reset_to_defaults()
        # Plugins tab has no defaults to reset
        self.advanced_tab.reset_to_defaults()
        self.backup_tab.reset_to_defaults()

        QMessageBox.information(
            self,
            _("Settings Reset"),
            _("All settings have been reset to defaults.\n\n"
              "Click OK or Apply to save the changes.")
        )
