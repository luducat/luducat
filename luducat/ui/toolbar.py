# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# toolbar.py

"""Toolbar for luducat

Contains:
- Filter controls (Filter dropdown, All, Favorites, Sort, Random) — merged from filter bar
- Search box
- View mode buttons (detail, cover, screenshot)
- Sync dropdown
- Tools, Settings buttons
- About button
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QButtonGroup,
    QMenu,
    QFrame,
)
from PySide6.QtGui import QAction

from ..core.constants import (
    APP_NAME,
    VIEW_MODE_LIST,
    VIEW_MODE_COVER,
    VIEW_MODE_SCREENSHOT,
)

logger = logging.getLogger(__name__)


class Toolbar(QWidget):
    """Main toolbar widget

    Layout (merged single row):
    [Filter▼][All][Fav] [Sort▼][🎲] | [Search...] ...stretch... [Detail|Cover|Screenshot] | [Sync▼] | [Tools▼][Settings][?]

    Signals:
        search_changed: Emitted when search text changes (debounced)
        view_mode_changed: Emitted when view mode button clicked
        sync_requested: Emitted when sync all requested
        sync_store_requested: Emitted when specific store sync requested
        full_resync_requested: Emitted when full resync requested
        downloads_requested: Emitted when downloads button clicked
        vault_requested: Emitted when vault button clicked
        settings_requested: Emitted when settings button clicked
        about_requested: Emitted when about button clicked
        wizard_requested: Emitted when setup wizard requested (from Tools menu)
    """

    search_changed = Signal(str)
    view_mode_changed = Signal(str)
    refresh_requested = Signal()  # Still needed for Ctrl+R/F5 (no button)
    sync_requested = Signal()  # sync all stores
    sync_store_requested = Signal(str)  # sync specific store
    sync_metadata_requested = Signal(str)  # sync specific metadata plugin
    full_resync_requested = Signal()  # full resync (re-download all metadata)
    settings_requested = Signal()
    about_requested = Signal()
    wizard_requested = Signal()  # re-run setup wizard
    backup_requested = Signal()  # open settings on Backup tab
    tag_manager_requested = Signal()  # open tag manager dialog
    dev_console_requested = Signal()  # open developer console
    download_covers_requested = Signal()  # batch download missing covers

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("toolbarWidget")

        # Placeholder for filter controls (set by embed_filter_controls)
        self._filter_controls_widget = None

        self._setup_ui()
        self._connect_signals()

        # Debounce timer for search
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._emit_search)

    def _setup_ui(self) -> None:
        """Create toolbar layout"""
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(8)

        # Slot for filter controls (inserted by embed_filter_controls)
        # Will be filled with FilterBar's controls widget

        # Separator after filter controls (initially hidden)
        self._filter_sep = QFrame()
        self._filter_sep.setFrameShape(QFrame.Shape.VLine)
        self._filter_sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._filter_sep.setObjectName("toolbarSeparator")
        self._filter_sep.setVisible(False)
        self._layout.addWidget(self._filter_sep)

        # Search box
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(_("Search games..."))
        self.search_box.setToolTip(_("Search by title, description, developer, or publisher"))
        self.search_box.setClearButtonEnabled(True)
        self._layout.addWidget(self.search_box, 1)

        # === View mode button group ===
        self.view_group_frame = QFrame()
        self.view_group_frame.setObjectName("viewModeGroup")
        view_layout = QHBoxLayout(self.view_group_frame)
        view_layout.setContentsMargins(0, 0, 0, 0)
        view_layout.setSpacing(0)

        self.btn_list = QPushButton(_("Detail"))
        self.btn_list.setToolTip(_("Show game details"))
        self.btn_cover = QPushButton(_("Cover"))
        self.btn_cover.setToolTip(_("Show games as cover art tiles"))
        self.btn_screenshot = QPushButton(_("Screenshot"))
        self.btn_screenshot.setToolTip(_("Show games as screenshot tiles"))

        self.btn_list.setCheckable(True)
        self.btn_cover.setCheckable(True)
        self.btn_screenshot.setCheckable(True)

        self.btn_list.setChecked(True)

        # Button group for mutual exclusion
        self.view_button_group = QButtonGroup(self)
        self.view_button_group.addButton(self.btn_list, 0)
        self.view_button_group.addButton(self.btn_cover, 1)
        self.view_button_group.addButton(self.btn_screenshot, 2)

        view_layout.addWidget(self.btn_list)
        view_layout.addWidget(self.btn_cover)
        view_layout.addWidget(self.btn_screenshot)

        self._layout.addWidget(self.view_group_frame)

        # Separator after view modes
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        sep1.setObjectName("toolbarSeparator")
        self._layout.addWidget(sep1)

        # === Sync dropdown button ===
        self.btn_sync = QPushButton(_("Sync"))
        self.btn_sync.setObjectName("syncButton")
        self.btn_sync.setToolTip(_("Fetch updates from game stores"))
        self._setup_sync_menu()
        self._layout.addWidget(self.btn_sync)

        # Separator after sync
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        sep2.setObjectName("toolbarSeparator")
        self._layout.addWidget(sep2)

        # === Main menu area: Tools, Settings ===

        # Tools dropdown button
        self.btn_tools = QPushButton(_("Tools"))
        self.btn_tools.setObjectName("toolsButton")
        self._setup_tools_menu()
        self._layout.addWidget(self.btn_tools)

        # Settings button
        self.btn_settings = QPushButton(_("Settings"))
        self.btn_settings.setObjectName("settingsButton")
        self._layout.addWidget(self.btn_settings)

        # About button (?)
        self.btn_about = QPushButton("?")
        self.btn_about.setObjectName("aboutButton")
        self.btn_about.setToolTip(_("About {app_name}").format(app_name=APP_NAME))

        self._layout.addWidget(self.btn_about)

    def embed_filter_controls(self, controls_widget: QWidget) -> None:
        """Embed FilterBar's controls widget into the toolbar.

        Called by main_window after both toolbar and filter_bar are created.
        Inserts the controls widget at the beginning of the toolbar layout.

        Args:
            controls_widget: FilterBar's detached controls row widget
        """
        self._filter_controls_widget = controls_widget
        # Insert at position 0 (before the filter separator)
        self._layout.insertWidget(0, controls_widget)
        self._filter_sep.setVisible(True)

    def _setup_sync_menu(self) -> None:
        """Create sync dropdown menu"""
        menu = QMenu(self)

        # Sync all at top
        self.action_sync_all = menu.addAction(_("Sync All"))
        self.action_sync_all.setData("")

        menu.addSeparator()

        # Store-specific sync actions will be added dynamically
        self.store_sync_actions = {}

        # Metadata plugin section (separator + actions added dynamically)
        self._metadata_separator = menu.addSeparator()
        self._metadata_separator.setVisible(False)
        self.metadata_sync_actions = {}

        # Full resync at bottom (added after dynamic store/metadata actions)
        self._full_resync_separator = menu.addSeparator()
        self.action_full_resync = menu.addAction(_("Full Resync..."))
        self.action_full_resync.setData("__full_resync__")
        self.action_full_resync.setToolTip(_("Re-download metadata for ALL games"))

        self.btn_sync.setMenu(menu)
        self.sync_menu = menu

    def _setup_tools_menu(self) -> None:
        """Create Tools dropdown menu"""
        menu = QMenu(self)

        # Backup & Restore
        self.action_backup = menu.addAction(_("Backup && Restore..."))
        self.action_backup.setToolTip(_("Open backup and restore settings"))

        # CSV Export
        self.action_csv_export = menu.addAction(
            _("Export your games as CSV...")
        )
        self.action_csv_export.setToolTip(
            _("Export the currently filtered game list as CSV")
        )

        # Download Missing Covers
        self.action_download_covers = menu.addAction(
            _("Download Missing Covers...")
        )
        self.action_download_covers.setToolTip(
            _("Pre-download all cover images for faster browsing")
        )

        # Tag Manager
        self.action_tag_manager = menu.addAction(_("Tag Manager..."))
        self.action_tag_manager.setToolTip(
            _("Manage tags: create, edit, delete, merge, and import/export")
        )

        menu.addSeparator()

        # Developer Console
        self.action_dev_console = menu.addAction(_("Developer Console..."))
        self.action_dev_console.setToolTip(
            _("Open the developer console with logs and diagnostics")
        )

        # Setup Wizard (at bottom — rarely used after first run)
        self.action_wizard = menu.addAction(_("Setup Wizard..."))
        self.action_wizard.setToolTip(_("Re-run the initial setup wizard"))

        self.btn_tools.setMenu(menu)
        self.tools_menu = menu

    def _connect_signals(self) -> None:
        """Connect internal signals"""
        # Search with debounce
        self.search_box.textChanged.connect(self._on_search_text_changed)

        # View mode buttons
        self.view_button_group.buttonClicked.connect(self._on_view_button_clicked)

        # Sync menu
        self.sync_menu.triggered.connect(self._on_sync_action)

        # Tools menu
        self.action_wizard.triggered.connect(self.wizard_requested.emit)
        self.action_backup.triggered.connect(self.backup_requested.emit)
        self.action_tag_manager.triggered.connect(self.tag_manager_requested.emit)
        self.action_dev_console.triggered.connect(self.dev_console_requested.emit)
        self.action_download_covers.triggered.connect(
            self.download_covers_requested.emit
        )

        # Settings
        self.btn_settings.clicked.connect(self.settings_requested.emit)

        # About
        self.btn_about.clicked.connect(self.about_requested.emit)

    def _on_search_text_changed(self, text: str) -> None:
        """Handle search text change with debounce"""
        self._search_timer.stop()
        self._search_timer.start(300)  # 300ms debounce

    def _emit_search(self) -> None:
        """Emit search signal after debounce"""
        self.search_changed.emit(self.search_box.text())

    def _on_view_button_clicked(self, button: QPushButton) -> None:
        """Handle view mode button click"""
        if button == self.btn_list:
            mode = VIEW_MODE_LIST
        elif button == self.btn_cover:
            mode = VIEW_MODE_COVER
        else:
            mode = VIEW_MODE_SCREENSHOT

        logger.debug(f"View mode button clicked: {mode}")
        self.view_mode_changed.emit(mode)

    def _on_sync_action(self, action: QAction) -> None:
        """Handle sync menu action"""
        data = action.data()
        logger.debug(f"Sync requested: {data or 'all'}")
        if data == "__full_resync__":
            self.full_resync_requested.emit()
        elif isinstance(data, str) and data.startswith("__meta__:"):
            plugin_name = data[9:]  # Strip "__meta__:" prefix
            self.sync_metadata_requested.emit(plugin_name)
        elif data:
            self.sync_store_requested.emit(data)
        else:
            self.sync_requested.emit()

    def get_search_text(self) -> str:
        """Get current search text"""
        return self.search_box.text()

    def set_search_text(self, text: str) -> None:
        """Set search text programmatically (e.g. when restoring a dynamic collection)."""
        self.search_box.setText(text)

    def get_current_view_mode(self) -> str:
        """Get current view mode

        Returns:
            View mode string (detail, cover, screenshot)
        """
        if self.btn_list.isChecked():
            return VIEW_MODE_LIST
        elif self.btn_cover.isChecked():
            return VIEW_MODE_COVER
        else:
            return VIEW_MODE_SCREENSHOT

    def set_view_mode(self, mode: str) -> None:
        """Set current view mode

        Args:
            mode: View mode (detail, cover, screenshot)
        """
        if mode == VIEW_MODE_LIST:
            self.btn_list.setChecked(True)
        elif mode == VIEW_MODE_COVER:
            self.btn_cover.setChecked(True)
        elif mode == VIEW_MODE_SCREENSHOT:
            self.btn_screenshot.setChecked(True)

    def update_sync_stores(self, stores: list[tuple[str, str, bool]]) -> None:
        """Update sync menu with available stores

        Args:
            stores: List of (store_name, display_name, is_authenticated) tuples
        """
        # Clear existing store actions
        for action in self.store_sync_actions.values():
            self.sync_menu.removeAction(action)
        self.store_sync_actions.clear()

        # Insert store actions before the metadata separator
        for store_name, display_name, is_auth in stores:
            action = QAction(display_name, self.sync_menu)
            action.setData(store_name)
            self.sync_menu.insertAction(self._metadata_separator, action)
            self.store_sync_actions[store_name] = action

    def update_sync_metadata_plugins(
        self, plugins: list[tuple[str, str]]
    ) -> None:
        """Update sync menu with active metadata plugins.

        Args:
            plugins: List of (plugin_name, display_name) tuples
        """
        # Clear existing metadata actions
        for action in self.metadata_sync_actions.values():
            self.sync_menu.removeAction(action)
        self.metadata_sync_actions.clear()

        if not plugins:
            self._metadata_separator.setVisible(False)
            return

        self._metadata_separator.setVisible(True)

        # Insert metadata actions before the full-resync separator
        for plugin_name, display_name in plugins:
            action = QAction(display_name, self.sync_menu)
            action.setData(f"__meta__:{plugin_name}")
            action.setToolTip(
                _("Run {name} sync").format(name=display_name)
            )
            self.sync_menu.insertAction(self._full_resync_separator, action)
            self.metadata_sync_actions[plugin_name] = action
