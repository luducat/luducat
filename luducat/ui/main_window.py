# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# main_window.py

"""Main application window for luducat

The main window contains:
- Toolbar (search, view modes, sort, sync, settings)
- Filter bar (quick filters, store filters, tags)
- Splitter with game list (left) and content area (right)
- Status bar (visible in cover/screenshot modes)
"""

import logging
import random
import time
from typing import Any, Dict, List, Optional

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QMetaObject
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QMainWindow,
    QProgressDialog,
    QWidget,
    QVBoxLayout,
    QSplitter,
    QMessageBox,
    QWizard,
    QFrame,
)

from ..core.config import Config
from ..core.runtime_base import LaunchMethod
from ..core.constants import (
    APP_NAME,
    APP_RELEASES_URL,
    APP_VERSION,
    APP_VERSION_FULL,
    DEFAULT_LIST_PANEL_WIDTH,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    VIEW_MODE_COVER,
    VIEW_MODE_LIST,
    VIEW_MODE_SCREENSHOT,
    SORT_MODE_NAME,
    SORT_MODE_RECENT,
    SORT_MODE_ADDED,
    SORT_MODE_PUBLISHER,
    SORT_MODE_DEVELOPER,
    SORT_MODE_RELEASE,
    SORT_MODE_FRANCHISE,
    SORT_MODE_FAMILY_LICENSES,
    DEFAULT_IMAGE_FADE_MS,
)
from ..core.database import Database
from ..core.game_entry import GameEntry
from ..core.network_monitor import get_network_monitor
from ..core.plugin_manager import PluginManager
from ..core.game_service import GameService
from ..utils.browser import open_url
from ..utils.workers import SyncWorker, DataLoaderWorker
from ..core.sync_queue import SyncJobQueue, SyncJob, SyncPhase, JobPriority

from .toolbar import Toolbar
from .filter_bar import FilterBar
from .game_list import GameList
from .content_area import ContentArea
from .status_bar import StatusBar
from .sync_widget import SyncWidget
from .dialogs import (
    AboutDialog, SettingsDialog, PluginConfigDialog,
    TagEditorDialog, ImageViewerDialog, SetupWizard,
)
from .widgets import LoadingOverlay, LaunchOverlay

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window

    Layout:
    ┌─────────────────────────────────────────────────┐
    │ [Window Title - handled by WM]                  │
    ├─────────────────────────────────────────────────┤
    │ Toolbar                                         │
    ├─────────────────────────────────────────────────┤
    │ Filter Bar                                      │
    ├──────────────┬──────────────────────────────────┤
    │ Game List    │ Content Area                     │
    │ (resizable)  │ (list/cover/screenshot view)     │
    │              │                                  │
    ├──────────────┴──────────────────────────────────┤
    │ Status Bar (cover/screenshot modes only)        │
    └─────────────────────────────────────────────────┘
    """

    # Signals
    game_selected = Signal(str)  # game_id
    view_mode_changed = Signal(str)  # view mode name
    # game_id, description (thread-safe UI updates)
    _description_fetched = Signal(str, str)
    _api_rate_limited = Signal(str)  # rate limit status message (thread-safe)

    def __init__(
        self,
        config: Config,
        database: Database,
        plugin_manager: PluginManager,
        theme_manager=None,  # Optional ThemeManager for live updates
        runtime_manager=None,  # Optional RuntimeManager for game execution
        game_manager=None,  # Optional GameManager for installation
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.runtime_manager = runtime_manager
        self.game_manager = game_manager

        self.config = config
        self.database = database
        self.plugin_manager = plugin_manager

        # Create game service with config for metadata priorities
        self.game_service = GameService(database, plugin_manager, config=config)

        # Inject MainDbAccessor into store plugins now that GameService exists
        plugin_manager.inject_main_db_accessors(self.game_service)

        # Wire game_service to RuntimeManager for per-game launch config
        if self.runtime_manager:
            self.runtime_manager.set_game_service(self.game_service)

        # Wire priority change callback — invalidate caches when user saves new priorities
        self.game_service._resolver.set_on_priorities_changed(
            self._on_priorities_changed
        )

        # Track sync worker
        self._sync_worker: Optional[SyncWorker] = None

        # Track data loader worker
        self._data_loader: Optional[DataLoaderWorker] = None
        self._loading_in_progress = False

        # All games cache
        self._all_games: List[Dict[str, Any]] = []
        self._filtered_games: List[Dict[str, Any]] = []

        # Pre-built filter indexes (built once in _build_filter_indexes)
        self._games_by_id: Dict[str, Dict[str, Any]] = {}
        self._all_game_ids: set = set()
        self._non_hidden_ids: set = set()
        self._hidden_ids: set = set()
        self._favorite_ids: set = set()
        self._free_ids: set = set()
        self._installed_ids: set = set()
        self._demo_ids: set = set()
        self._store_index: Dict[str, set] = {}
        self._tag_index: Dict[str, set] = {}
        self._game_mode_index: Dict[str, set] = {}
        self._developer_index: Dict[str, set] = {}
        self._publisher_index: Dict[str, set] = {}
        self._genre_index: Dict[str, set] = {}
        self._year_index: Dict[str, set] = {}
        self._title_index: Dict[str, str] = {}
        self._adult_content_ids: set = set()
        self._family_shared_ids: set = set()
        self._orphaned_ids: set = set()  # Games with no store links
        self._protondb_game_ids: set = set()
        self._steam_deck_game_ids: set = set()
        self._recent_cache: set = set()  # computed on demand
        self._recent_cache_threshold = None  # datetime threshold for recent

        # Progressive loading: track game IDs and stores added during sync
        self._progressive_ids: set = set()
        self._progressive_stores: set = set()

        # Update checker state (set by background check, consumed by About/Settings)
        self._pending_update = None  # Optional[UpdateInfo]

        # Theme-aware score colors (set by _on_theme_delegate_config_changed)
        self._score_colors: Dict[str, str] = {"positive": "#28b43c", "negative": "#c83232"}

        # Incremental filtering: cache filter result to avoid rebuild on search-only changes
        self._prev_filter_state: Optional[dict] = None  # filters without search text
        self._prev_filter_result_ids: set = set()  # result set from filters only

        # Sort settings (will be restored from config)
        self._current_sort_mode = SORT_MODE_NAME
        self._sort_reverse = False
        self._favorites_first = False

        # Track if news dialog has been shown this session
        self._news_check_done = False

        # Developer console singleton (lazy-created)
        self._dev_console = None

        self._setup_window()
        self._setup_ui()
        self._connect_signals()
        self._restore_state()

        # Schedule initial load or wizard
        if self.config.is_first_run:
            # First run - show wizard after window is visible
            QTimer.singleShot(100, self._show_setup_wizard_first_run)
        else:
            # One-time media cleanup (e.g. after config migration v5)
            self._run_pending_media_cleanup()
            # One-time content_descriptors repair (config migration v6)
            self._run_pending_content_descriptors_repair()
            # Normal startup - load games
            QTimer.singleShot(100, self._load_games)
            # Check for auto-sync after window is fully loaded
            QTimer.singleShot(500, self._check_auto_sync)

        # Memory stats logging timer (every 5 minutes)
        self._memory_timer = QTimer()
        self._memory_timer.timeout.connect(self._log_memory_stats)
        self._memory_timer.start(300000)  # 5 minutes
        self._memory_cleanup_suppressed_until = 0  # timestamp — suppress cleanup after no-op

        # Disk health check timer (hourly) — warns on status change only
        self._last_disk_status: str = "green"
        self._disk_health_timer = QTimer()
        self._disk_health_timer.timeout.connect(self._check_disk_health)
        self._disk_health_timer.start(3600000)  # 1 hour

        # Connect image cache circuit breaker callback for one-time warning
        from ..utils.image_cache import register_disk_write_callback
        self._disk_write_warning_shown = False
        register_disk_write_callback(self._on_disk_write_disabled)

        # Apply RAM cache budgets from config (after caches are lazily created)
        from ..utils.image_cache import apply_cache_budgets
        QTimer.singleShot(200, lambda: apply_cache_budgets(self.config))

        # Network monitor (online/offline mode)
        self._network_monitor = get_network_monitor(config=self.config, parent=self)
        self._network_monitor.status_changed.connect(self._on_network_status_changed)
        self._network_monitor.connectivity_restored.connect(self._on_connectivity_restored)
        # Register with SDK so plugins can check online status
        from ..plugins.sdk import _registry as sdk_registry
        sdk_registry.register_network_monitor(self._network_monitor)
        self.status_bar.network_toggle_requested.connect(self._on_network_toggle_requested)
        self.status_bar.set_online_status(self._network_monitor.is_online)

        # Timer for clearing connectivity hint after 10 seconds
        self._connectivity_hint_timer = QTimer()
        self._connectivity_hint_timer.setSingleShot(True)
        self._connectivity_hint_timer.timeout.connect(self.status_bar.clear_connectivity_hint)

        # Debounce timer for cache_refresh_requested during sync
        # Coalesces rapid signals into one _load_games() call per 10 seconds,
        # giving the event loop time to repaint the progress bar.
        self._cache_refresh_timer = QTimer()
        self._cache_refresh_timer.setSingleShot(True)
        self._cache_refresh_timer.setInterval(10000)  # 10 seconds
        self._cache_refresh_timer.timeout.connect(self._load_games)

    def _setup_window(self) -> None:
        """Configure window properties"""
        self.setWindowTitle(
            _("{app_name} v{version}").format(
                app_name=APP_NAME,
                version=APP_VERSION_FULL,
            )
        )

        # Set minimum size
        self.setMinimumSize(800, 600)

        # Don't set custom window flags - let WM handle title bar
        # self.setWindowFlags(...) - intentionally not set

    def _setup_ui(self) -> None:
        """Create and arrange UI components"""
        # Central widget
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        # Main layout (no margins, no spacing between major sections)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Navigation bar wrapper (toolbar + filter bar with shared background)
        self.nav_bar = QFrame()
        self.nav_bar.setObjectName("navigationBar")
        nav_layout = QVBoxLayout(self.nav_bar)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        # Toolbar
        self.toolbar = Toolbar()
        nav_layout.addWidget(self.toolbar)

        # Filter bar (crumb bar — controls are embedded into toolbar)
        self.filter_bar = FilterBar()
        nav_layout.addWidget(self.filter_bar)

        # Embed filter controls (Filter▼, All, Favorites, Sort▼, 🎲) into toolbar
        self.toolbar.embed_filter_controls(self.filter_bar.get_controls_widget())

        layout.addWidget(self.nav_bar)

        # Main content area with splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("mainSplitter")

        # Game list (left panel)
        self.game_list = GameList()
        self.game_list.setMinimumWidth(200)
        self.game_list.setMaximumWidth(600)
        self.splitter.addWidget(self.game_list)

        # Content area (right panel)
        self.content_area = ContentArea()
        self.splitter.addWidget(self.content_area)

        # Wire managers to list view (for Settings tab)
        self.content_area.set_list_view_managers(
            runtime_manager=self.runtime_manager,
            game_manager=self.game_manager,
        )

        # Provide runner query callback for launch button display
        if self.runtime_manager:
            self.content_area.set_runner_query(self._get_runner_info_for_store)

        # Configure splitter
        self.splitter.setStretchFactor(0, 0)  # Game list doesn't stretch
        self.splitter.setStretchFactor(1, 1)  # Content area stretches
        self.splitter.setCollapsible(0, False)  # Can't collapse game list
        self.splitter.setCollapsible(1, False)  # Can't collapse content

        layout.addWidget(self.splitter, 1)  # Takes remaining space

        # Sync widget (visible only during sync, above status bar)
        self._sync_widget = SyncWidget()
        self._sync_widget.setVisible(False)
        layout.addWidget(self._sync_widget)

        # Status bar (always visible; density slider hidden in list mode)
        self.status_bar = StatusBar()
        layout.addWidget(self.status_bar)

        # Loading overlay (covers central widget during data loading)
        self._loading_overlay = LoadingOverlay(central)
        self._loading_overlay.hide()

        # Launch overlay (covers central widget during game launch)
        self._launch_overlay = LaunchOverlay(central)
        self._launch_process_timer: Optional[QTimer] = None
        self._launch_monitor_data: Optional[Dict[str, Any]] = None

    def _connect_signals(self) -> None:
        """Connect signals between components"""
        # View mode changes
        self.toolbar.view_mode_changed.connect(self._on_view_mode_changed)
        # Sync toolbar when content area changes view mode programmatically
        self.content_area.view_mode_changed.connect(self.toolbar.set_view_mode)

        # Game selection
        self.game_list.game_selected.connect(self._on_game_selected)
        # Sync game list when game selected in grid views (visual sidebar sync)
        self.content_area.game_selected.connect(self.game_list.select_game)
        # Grid selections also need config save + detail update
        self.content_area.game_selected.connect(self._on_game_selected)

        # Density slider - route through handler to save per-view density
        self.status_bar.density_changed.connect(self._on_density_changed)

        # Refresh button in status bar
        self.status_bar.refresh_requested.connect(self._force_refresh_games)

        # Search
        self.toolbar.search_changed.connect(self._on_search_changed)

        # Filters
        self.filter_bar.filters_changed.connect(self._on_filters_changed)
        self.filter_bar.add_tag_requested.connect(self._on_add_tag_from_filter_bar)

        # Sort (moved from toolbar to filter bar)
        self.filter_bar.sort_changed.connect(self._on_sort_changed)

        # Refresh (reload from database without syncing)
        self.toolbar.refresh_requested.connect(self._force_refresh_games)

        # Sync
        self.toolbar.sync_requested.connect(self._on_sync_requested)
        self.toolbar.sync_store_requested.connect(self._on_sync_store_requested)
        self.toolbar.sync_metadata_requested.connect(self._on_sync_metadata_requested)
        self.toolbar.full_resync_requested.connect(self._on_full_resync_requested)

        # Settings
        self.toolbar.settings_requested.connect(self._on_settings_requested)
        self.toolbar.about_requested.connect(self._on_about_requested)

        # Setup wizard (from Tools menu)
        self.toolbar.wizard_requested.connect(self._show_setup_wizard_manual)
        self.toolbar.backup_requested.connect(self._on_backup_requested)
        self.toolbar.action_csv_export.triggered.connect(self._export_csv)
        self.toolbar.tag_manager_requested.connect(self._on_open_tag_manager)
        self.toolbar.dev_console_requested.connect(self._on_dev_console_requested)

        # Random game (dice button is on filter bar)
        self.filter_bar.random_game_requested.connect(self._on_random_game)

        # Screenshot lazy loading callback
        self.content_area.set_screenshot_callback(self.game_service.get_screenshots)
        # Screenshot 404 fallback — retry with next priority source
        self.content_area.set_screenshot_invalidate_callback(self.game_service.invalidate_screenshots)

        # Description lazy loading callback
        self.content_area.set_description_callback(self.game_service.get_description)

        # Ensure metadata complete callback (fills missing fields from IGDB fallback)
        self.content_area.set_ensure_metadata_callback(self.game_service.ensure_metadata_complete)

        # Cover lazy loading callback (for cover view grid)
        self.content_area.set_cover_callback(self.game_service.get_cover)

        # Store URL callback (gets URL from plugin)
        self.content_area.set_store_url_callback(self._get_store_url)

        # Detail fields lazy loading callback (loads metadata panel fields on demand)
        self.content_area.set_detail_fields_callback(self.game_service.get_detail_fields)

        # Play sessions callback for Stats tab breakdown
        self.content_area.set_play_sessions_callback(self.game_service.get_play_sessions_summary)

        # Keyboard shortcuts for refresh (only active in main window, not dialogs)
        refresh_f5 = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        refresh_f5.setContext(Qt.ShortcutContext.WindowShortcut)
        refresh_f5.activated.connect(self._force_refresh_games)

        refresh_ctrl_r = QShortcut(QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_R), self)
        refresh_ctrl_r.setContext(Qt.ShortcutContext.WindowShortcut)
        refresh_ctrl_r.activated.connect(self._force_refresh_games)

        # Borrowed from name resolution callback (for family shared games)
        self.content_area.set_borrowed_from_callback(self._resolve_family_member_name)

        # Description refresh for plain text descriptions (from Kaggle dataset)
        self.content_area.description_refresh_requested.connect(self._on_description_refresh_requested)

        # Game launch / install
        self.content_area.game_launched.connect(self._on_game_launched)
        self.content_area.game_launched_via_runner.connect(self._on_game_launched_via_runner)
        self.content_area.game_install_requested.connect(self._on_game_install)

        # Favorite toggle
        self.content_area.favorite_toggled.connect(self._on_favorite_toggled)

        # Hidden toggle
        self.content_area.hidden_toggled.connect(self._on_hidden_toggled)

        # Edit tags
        self.content_area.edit_tags_requested.connect(self._on_edit_tags_requested)

        # View screenshots
        self.content_area.view_screenshots_requested.connect(self._on_view_screenshots_requested)

        # View cover
        self.content_area.view_cover_requested.connect(self._on_view_cover_requested)

        # List view Settings/Notes tab signals
        self.content_area.platform_changed.connect(self._on_platform_changed)
        self.content_area.notes_changed.connect(self._on_notes_changed)
        self.content_area.settings_changed.connect(self._on_settings_changed)

        # Filter from detail view clickable metadata
        self.content_area.filter_developer_requested.connect(self._on_filter_developers_from_context)
        self.content_area.filter_publisher_requested.connect(self._on_filter_publishers_from_context)
        self.content_area.filter_genre_requested.connect(self._on_filter_genres_from_context)
        self.content_area.filter_tag_requested.connect(self._on_filter_tags_from_detail)
        self.content_area.filter_year_requested.connect(self._on_filter_year_from_context)

        # Context menu from game list and grid views
        self.game_list.context_menu_requested.connect(self._on_context_menu_requested)
        self.content_area.context_menu_requested.connect(self._on_context_menu_requested)

        # Set default store on list view
        default_store = self.config.get("ui.default_store", "")
        self.content_area.list_view.set_default_store(default_store)

        # Internal signal for thread-safe description updates from background thread
        self._description_fetched.connect(self._update_game_description)
        self._api_rate_limited.connect(self._on_rate_limit_status)

        # Theme delegate config (push per-theme visual config to delegates)
        if self.theme_manager:
            self.theme_manager.theme_changed.connect(self._on_theme_delegate_config_changed)
            self.theme_manager.theme_changed.connect(self.filter_bar.refresh_icons)
            self.theme_manager.theme_changed.connect(self.status_bar.refresh_icons)
            # Apply initial config from current theme
            self._on_theme_delegate_config_changed()

    def _restore_state(self) -> None:
        """Restore window state from config"""
        # Window geometry
        width = self.config.get("ui.window_width", DEFAULT_WINDOW_WIDTH)
        height = self.config.get("ui.window_height", DEFAULT_WINDOW_HEIGHT)
        self.resize(width, height)

        # Window position (if saved and within screen bounds)
        x = self.config.get("ui.window_x", None)
        y = self.config.get("ui.window_y", None)
        if x is not None and y is not None:
            # Check if position is within any available screen
            screens = QGuiApplication.screens()
            position_valid = False
            for screen in screens:
                geom = screen.availableGeometry()
                # Check if at least part of the window title bar is visible
                if (geom.left() <= x < geom.right() and
                    geom.top() <= y < geom.bottom()):
                    position_valid = True
                    break

            if position_valid:
                self.move(x, y)
            else:
                # Center on primary screen
                primary = QGuiApplication.primaryScreen()
                if primary:
                    geom = primary.availableGeometry()
                    self.move(
                        geom.x() + (geom.width() - self.width()) // 2,
                        geom.y() + (geom.height() - self.height()) // 2
                    )

        # Maximized state
        if self.config.get("ui.window_maximized", False):
            self.showMaximized()

        # Splitter position (game list width)
        list_width = self.config.get("ui.list_panel_width", DEFAULT_LIST_PANEL_WIDTH)
        self.splitter.setSizes([list_width, width - list_width])

        # About splitter (description / metadata panel)
        about_width = self.config.get("ui.about_panel_width", None)
        if about_width is not None:
            lv = self.content_area.list_view
            total = lv.about_splitter.width() or 980
            lv.about_splitter.setSizes([total - about_width, about_width])

        # Grid density - restore BEFORE view mode so views have correct density
        cover_density = self.config.get("appearance.cover_grid_density", 250)
        screenshot_density = self.config.get("appearance.screenshot_grid_density", 250)
        logger.debug(f"Restoring densities: cover={cover_density}, screenshot={screenshot_density}")
        self.content_area.cover_view.set_density(cover_density)
        self.content_area.screenshot_view.set_density(screenshot_density)

        # Ctrl+Scroll zoom → sync status bar slider and save config
        self.content_area.cover_view.density_changed.connect(
            self._on_view_density_changed)
        self.content_area.screenshot_view.density_changed.connect(
            self._on_view_density_changed)

        # Image fade-in duration
        fade_ms = self.config.get("appearance.image_fade_duration", DEFAULT_IMAGE_FADE_MS)
        self.content_area.cover_view.delegate.set_fade_duration(fade_ms)
        self.content_area.screenshot_view.delegate.set_fade_duration(fade_ms)

        # Cover scaling mode
        cover_scaling = self.config.get("appearance.cover_scaling", "none")
        self.content_area.cover_view.delegate.set_cover_scaling(cover_scaling)

        # Badge visibility
        show_modes = self.config.get("appearance.show_game_mode_badges", True)
        show_stores = self.config.get("appearance.show_store_badges", True)
        self.content_area.cover_view.delegate.set_badge_visibility(show_modes, show_stores)
        self.content_area.screenshot_view.delegate.set_badge_visibility(show_modes, show_stores)
        default_store = self.config.get("ui.default_store", "")
        self.content_area.cover_view.delegate.set_default_store(default_store)
        self.content_area.screenshot_view.delegate.set_default_store(default_store)

        # View mode - restored AFTER density so slider syncs to correct value
        view_mode = self.config.get("ui.view_mode", VIEW_MODE_LIST)
        self.toolbar.set_view_mode(view_mode)
        self._on_view_mode_changed(view_mode)

        # Sort mode - restore to filter bar and instance variables
        sort_mode = self.config.get("ui.sort_mode", SORT_MODE_NAME)
        sort_reverse = self.config.get("ui.sort_reverse", False)
        favorites_first = self.config.get("ui.favorites_first", False)
        self._current_sort_mode = sort_mode
        self._sort_reverse = sort_reverse
        self._favorites_first = favorites_first
        self.filter_bar.set_sort_mode(sort_mode, sort_reverse, favorites_first)

    def _save_state(self) -> None:
        """Save window state to config"""
        # Window geometry (only if not maximized)
        if not self.isMaximized():
            self.config.set("ui.window_width", self.width())
            self.config.set("ui.window_height", self.height())
            # Also save position
            pos = self.pos()
            self.config.set("ui.window_x", pos.x())
            self.config.set("ui.window_y", pos.y())

        self.config.set("ui.window_maximized", self.isMaximized())

        # Splitter position
        sizes = self.splitter.sizes()
        if sizes:
            self.config.set("ui.list_panel_width", sizes[0])

        # About splitter (metadata panel width)
        about_sizes = self.content_area.list_view.about_splitter.sizes()
        if about_sizes and len(about_sizes) == 2:
            self.config.set("ui.about_panel_width", about_sizes[1])

        # View mode
        self.config.set("ui.view_mode", self.content_area.current_view_mode())

        # Sort mode
        self.config.set("ui.sort_mode", self._current_sort_mode)
        self.config.set("ui.sort_reverse", self._sort_reverse)
        self.config.set("ui.favorites_first", self._favorites_first)

        # Grid density - save current values from views
        cover_density = self.content_area.cover_view.get_density()
        screenshot_density = self.content_area.screenshot_view.get_density()
        self.config.set("appearance.cover_grid_density", cover_density)
        self.config.set("appearance.screenshot_grid_density", screenshot_density)
        logger.debug(f"Saving densities: cover={cover_density}, screenshot={screenshot_density}")

    def _on_view_mode_changed(self, mode: str) -> None:
        """Handle view mode change

        Args:
            mode: New view mode (list, cover, screenshot, downloads)
        """
        logger.debug(f"View mode changed to: {mode}")

        # Reset memory cleanup suppression — view change may free caches
        self._memory_cleanup_suppressed_until = 0

        # Cancel pending image requests when switching views to prevent thread crashes
        from ..utils.image_cache import get_cover_cache, get_screenshot_cache
        if mode != VIEW_MODE_SCREENSHOT:
            get_screenshot_cache().cancel_pending()
        if mode != VIEW_MODE_COVER:
            get_cover_cache().cancel_pending()

        # Update content area
        self.content_area.set_view_mode(mode)

        # List view mode always shows game list
        self.game_list.setVisible(True)

        # Show/hide density slider based on view mode (status bar always visible)
        if mode in (VIEW_MODE_COVER, VIEW_MODE_SCREENSHOT):
            self.status_bar.set_density_visible(True)
            # Sync slider with current view's density
            if mode == VIEW_MODE_COVER:
                current_density = self.config.get("appearance.cover_grid_density", 250)
            else:
                current_density = self.config.get("appearance.screenshot_grid_density", 250)
            self.status_bar.set_density(current_density)
        else:
            self.status_bar.set_density_visible(False)

        # Emit signal
        self.view_mode_changed.emit(mode)

    def _on_density_changed(self, density: int) -> None:
        """Handle density slider change - save per-view and apply

        Args:
            density: New density value
        """
        current_mode = self.toolbar.get_current_view_mode()

        if current_mode == VIEW_MODE_COVER:
            self.content_area.cover_view.set_density(density)
            self.config.set("appearance.cover_grid_density", density)
        elif current_mode == VIEW_MODE_SCREENSHOT:
            self.content_area.screenshot_view.set_density(density)
            self.config.set("appearance.screenshot_grid_density", density)

    def _on_view_density_changed(self, density: int) -> None:
        """Handle Ctrl+Scroll zoom from grid views — sync slider and save."""
        self.status_bar.set_density(density)
        current_mode = self.toolbar.get_current_view_mode()
        if current_mode == VIEW_MODE_COVER:
            self.config.set("appearance.cover_grid_density", density)
        elif current_mode == VIEW_MODE_SCREENSHOT:
            self.config.set("appearance.screenshot_grid_density", density)

    def _on_game_selected(self, game_id: str) -> None:
        """Handle game selection

        Args:
            game_id: Selected game ID
        """
        logger.debug(f"Game selected: {game_id}")

        # Save selection to config for persistence across restarts
        self.config.set("ui.selected_game_id", game_id)

        # Update content area with selected game
        self.content_area.show_game(game_id)

        # Emit signal
        self.game_selected.emit(game_id)

        # If sync is running, request priority enrichment for unenriched games
        self._maybe_request_priority_enrichment(game_id)

    def _maybe_request_priority_enrichment(self, game_id: str) -> None:
        """Insert a priority enrichment job if sync is running and game is unenriched."""
        if not self._sync_worker or not self._sync_worker.isRunning():
            return

        # Find game in cache
        game_data = next((g for g in self._all_games if g["id"] == game_id), None)
        if not game_data:
            return

        # Heuristic: if genres is empty, game hasn't been enriched by IGDB yet
        if game_data.get("genres"):
            return

        store_app_ids = game_data.get("store_app_ids", {})
        if not store_app_ids:
            return

        store_name = next(iter(store_app_ids))
        store_app_id = store_app_ids[store_name]

        job = SyncJob(
            phase=SyncPhase.METADATA,
            plugin_name="all",
            task_type="enrich_single",
            game_ids=[store_app_id],
            store_name=store_name,
            priority=JobPriority.HIGH,
        )
        self._sync_worker.queue.insert_priority_job(job)
        logger.info(
            f"Priority enrichment requested for {game_data.get('title', game_id)}"
        )

    def _get_runner_info_for_store(self, store_name: str) -> Optional[str]:
        """Returns primary runner display name, or None if no runner available."""
        if not self.runtime_manager:
            return None
        runners = self.runtime_manager.get_runners_for_store(store_name)
        if not runners:
            return None
        runner = self.runtime_manager.get_runner(runners[0])
        return runner.display_name if runner else runners[0]

    def _on_game_launched(self, game_id: str, store_name: str) -> None:
        """Handle game launch request

        Uses RuntimeManager if available, falls back to direct plugin launch.

        Args:
            game_id: Game UUID
            store_name: Store to launch from (e.g., "steam")
        """
        # Prevent double-launch while overlay is visible
        if self._launch_overlay.isVisible():
            return

        game = self.game_service.get_game(game_id)
        if not game:
            logger.warning(f"Game not found for launch: {game_id}")
            return

        # Check if confirmation is required
        if self.config.get("ui.confirm_launch", False):
            reply = QMessageBox.question(
                self,
                _("Launch Game"),
                _("Launch \"{title}\" via {store}?").format(
                    title=game.get('title'),
                    store=PluginManager.get_store_display_name(
                        store_name
                    ),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        store_app_id = game.get("store_app_ids", {}).get(store_name)
        if not store_app_id:
            logger.warning(f"No store app ID for {game_id} on {store_name}")
            QMessageBox.warning(
                self,
                _("Cannot Launch"),
                _(
                    "No store app ID available for"
                    " this game on {store_name}."
                ).format(store_name=store_name),
            )
            return

        # Show launch overlay
        store_display = PluginManager.get_store_display_name(store_name)
        runner_display = ""
        if self.runtime_manager:
            runner_names = self.runtime_manager.get_runners_for_store(store_name)
            if runner_names:
                runner_display = PluginManager.get_store_display_name(runner_names[0])

        cover_pixmap = None
        cover_url = game.get("cover_image", "")
        if cover_url:
            from ..utils.image_cache import get_cover_cache
            cover_pixmap = get_cover_cache().get_image(cover_url)

        self._launch_overlay.show_launch(
            game.get("title", ""),
            store_display,
            runner_display,
            cover_pixmap,
        )
        self._set_game_running(game_id, True)

        # Try RuntimeManager first if available
        if self.runtime_manager:
            self._launch_via_runtime_manager(game_id, game, store_name, store_app_id)
        else:
            self._launch_via_plugin(game_id, game, store_name, store_app_id)

    def _on_game_launched_via_runner(
        self, game_id: str, store_name: str, runner_name: str
    ) -> None:
        """Handle game launch with explicit runner override (e.g. Playnite)."""
        if self._launch_overlay.isVisible():
            return

        game = self.game_service.get_game(game_id)
        if not game:
            return

        store_app_id = game.get("store_app_ids", {}).get(store_name)
        if not store_app_id:
            return

        store_display = PluginManager.get_store_display_name(store_name)
        runner = self.runtime_manager.get_runner(runner_name) if self.runtime_manager else None
        runner_display = runner.display_name if runner else runner_name

        cover_pixmap = None
        cover_url = game.get("cover_image", "")
        if cover_url:
            from ..utils.image_cache import get_cover_cache
            cover_pixmap = get_cover_cache().get_image(cover_url)

        self._launch_overlay.show_launch(
            game.get("title", ""), store_display, runner_display, cover_pixmap,
        )
        self._set_game_running(game_id, True)

        if self.runtime_manager:
            self._launch_via_runtime_manager(
                game_id, game, store_name, store_app_id,
                runner_name=runner_name,
            )
        else:
            self._launch_via_plugin(game_id, game, store_name, store_app_id)

    def _launch_via_runtime_manager(
        self,
        game_id: str,
        game: Dict[str, Any],
        store_name: str,
        store_app_id: str,
        runner_name: Optional[str] = None,
    ) -> None:
        """Launch game using RuntimeManager

        Args:
            game_id: Game UUID
            game: Game data dict
            store_name: Store name
            store_app_id: Store-specific app ID
            runner_name: Explicit runner override (e.g. "playnite")
        """
        import asyncio

        # Create a game-like object for RuntimeManager
        class GameProxy:
            def __init__(self, data, store, app_id):
                self.id = data.get("id", game_id)
                self.title = data.get("title", "Unknown")
                self.store_name = store
                self.store_app_id = app_id
                self.store_app_ids = data.get("store_app_ids", {})

        game_proxy = GameProxy(game, store_name, store_app_id)

        logger.info(f"Launching game {game.get('title')} via RuntimeManager")

        async def do_launch():
            return await self.runtime_manager.launch_game(
                game_proxy,
                runner_name=runner_name,
                exe_selection_callback=self._show_exe_selection_dialog,
                native_exe_callback=lambda: self._show_native_exe_picker(
                    game.get("title", "Unknown"),
                ),
                save_launch_config=lambda cfg: self.game_service.set_launch_config(game_id, cfg),
            )

        try:
            # Run async launch in event loop
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(do_launch())
            finally:
                loop.close()

            self._launch_overlay.hide_launch()

            if result.success:
                logger.info(
                    f"RuntimeManager launched {game.get('title')} "
                    f"with platform {result.platform_id}"
                )
                # Runner subprocess records its own session for EXECUTABLE
                # launches. Only record here for URL_SCHEME and other methods.
                if result.launch_method != LaunchMethod.EXECUTABLE:
                    self.game_service.record_launch(game_id, store_name)

                # Track process for EXECUTABLE launches
                if (
                    result.launch_method == LaunchMethod.EXECUTABLE
                    and result.process_id
                ):
                    self._start_process_monitor(
                        result.process_id, game_id, store_name
                    )
                else:
                    # URL_SCHEME: no process to track, revert after timeout
                    QTimer.singleShot(
                        5000,
                        lambda gid=game_id: self._set_game_running(gid, False),
                    )
            else:
                logger.warning(
                    f"RuntimeManager launch failed: {result.error_message}"
                )
                self._set_game_running(game_id, False)
                error_msg = result.error_message or _("Launch failed")
                QMessageBox.warning(self, _("Cannot Launch"), error_msg)

        except Exception as e:
            logger.error(f"RuntimeManager launch error: {e}")
            self._launch_overlay.hide_launch()
            self._set_game_running(game_id, False)
            QMessageBox.warning(
                self,
                _("Cannot Launch"),
                _("Launch failed: {error}").format(error=e),
            )

    def _show_exe_selection_dialog(self, candidates, game_title, prefix):
        """Show exe selection dialog for ambiguous Wine launch."""
        from luducat.ui.dialogs.exe_selection_dialog import ExeSelectionDialog

        dialog = ExeSelectionDialog(candidates, game_title, prefix, parent=self)
        if dialog.exec() == ExeSelectionDialog.DialogCode.Accepted:
            return dialog.get_result()
        return None

    def _show_native_exe_picker(self, game_title: str) -> Optional[str]:
        """Show file picker for native executable selection."""
        from PySide6.QtWidgets import QFileDialog

        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Select Executable for {title}").format(title=game_title),
            "",
            _("All Files (*)"),
        )
        return path if path else None

    def _on_game_install(self, game_id: str, store_name: str) -> None:
        """Handle game install request.

        Finds the best runner for the store and opens its install URL.
        """
        game = self.game_service.get_game(game_id)
        if not game:
            return

        store_app_id = game.get("store_app_ids", {}).get(store_name)
        if not store_app_id:
            QMessageBox.warning(
                self,
                _("Cannot Install"),
                _("No store app ID available for this game on {store}.").format(
                    store=PluginManager.get_store_display_name(store_name)
                ),
            )
            return

        # Find a runner that can build an install URL
        install_url = None
        if self.runtime_manager:
            runner_names = self.runtime_manager.get_runners_for_store(store_name)
            for runner_name in runner_names:
                runner = self.runtime_manager.get_runner(runner_name)
                if runner:
                    url = runner.build_install_url(store_name, store_app_id)
                    if url:
                        install_url = url
                        break

        if install_url:
            open_url(install_url)
        else:
            QMessageBox.information(
                self,
                _("Cannot Install"),
                _("No compatible launcher detected to install this game."),
            )

    def _launch_via_plugin(
        self,
        game_id: str,
        game: Dict[str, Any],
        store_name: str,
        store_app_id: str
    ) -> None:
        """Deprecated fallback — only reached when RuntimeManager is None."""
        self._launch_overlay.hide_launch()
        self._set_game_running(game_id, False)
        QMessageBox.warning(
            self,
            _("Cannot Launch"),
            _("Launch system not available."),
        )

    # -----------------------------------------------------------------
    # Launch process monitoring
    # -----------------------------------------------------------------

    def _set_game_running(self, game_id: str, is_running: bool) -> None:
        """Update running state in the detail view."""
        self.content_area.set_game_running(game_id, is_running)

    def _start_process_monitor(
        self, pid: int, game_id: str, store_name: str
    ) -> None:
        """Start polling a runner subprocess to detect game exit."""
        self._stop_process_monitor()

        self._launch_monitor_data = {
            "pid": pid,
            "game_id": game_id,
            "store_name": store_name,
            "started": time.monotonic(),
        }

        self._launch_process_timer = QTimer(self)
        self._launch_process_timer.timeout.connect(self._poll_launch_process)
        self._launch_process_timer.start(5000)
        logger.debug("Process monitor started for PID %d (game %s)", pid, game_id)

    def _poll_launch_process(self) -> None:
        """Check if the monitored runner subprocess is still alive."""
        import os

        if not self._launch_monitor_data:
            self._stop_process_monitor()
            return

        pid = self._launch_monitor_data["pid"]
        try:
            os.kill(pid, 0)  # Signal 0: existence check, no actual signal
        except ProcessLookupError:
            # Process has exited
            self._on_launch_process_ended()
        except PermissionError:
            # Process alive but owned by another user — treat as alive
            pass
        except OSError:
            # Unexpected error — assume dead
            self._on_launch_process_ended()

    def _on_launch_process_ended(self) -> None:
        """Handle runner subprocess exit — revert running state."""
        if not self._launch_monitor_data:
            return

        game_id = self._launch_monitor_data["game_id"]
        elapsed = time.monotonic() - self._launch_monitor_data["started"]
        logger.info(
            "Game process ended: %s (ran %.0fs)", game_id, elapsed
        )

        self._set_game_running(game_id, False)
        self._stop_process_monitor()

    def _stop_process_monitor(self) -> None:
        """Stop the process polling timer and clear state."""
        if self._launch_process_timer is not None:
            self._launch_process_timer.stop()
            self._launch_process_timer.deleteLater()
            self._launch_process_timer = None
        self._launch_monitor_data = None

    # -----------------------------------------------------------------

    def _on_favorite_toggled(self, game_id: str, is_favorite: bool) -> None:
        """Handle favorite toggle from content area

        Args:
            game_id: Game UUID
            is_favorite: New favorite status
        """
        logger.debug(f"Favorite toggled: {game_id} -> {is_favorite}")
        self.game_service.set_favorite(game_id, is_favorite)

        # Update pre-built filter index (used by _apply_filters when
        # the "favorites" type filter is active)
        if is_favorite:
            self._favorite_ids.add(game_id)
        else:
            self._favorite_ids.discard(game_id)
        self._prev_filter_state = None

        # Update the game list to reflect the change
        self.game_list.update_game_favorite(game_id, is_favorite)

        # Update content area's cached data for grid views
        self.content_area.update_game_favorite(game_id, is_favorite)

    def _on_hidden_toggled(self, game_id: str, is_hidden: bool) -> None:
        """Handle hidden toggle from content area

        Args:
            game_id: Game UUID
            is_hidden: New hidden status
        """
        logger.debug(f"Hidden toggled: {game_id} -> {is_hidden}")
        self.game_service.set_hidden(game_id, is_hidden)

        # Update source data so filters reflect the change
        for game in self._all_games:
            if game.get("id") == game_id:
                game.is_hidden = is_hidden
                break

        # Update pre-built filter indexes (used by _apply_filters)
        if is_hidden:
            self._non_hidden_ids.discard(game_id)
            self._hidden_ids.add(game_id)
        else:
            self._hidden_ids.discard(game_id)
            self._non_hidden_ids.add(game_id)

        # Invalidate incremental filter cache so _apply_filters re-evaluates
        self._prev_filter_state = None

        # Re-filter and update all displays (list view + grid views)
        self._apply_filters()
        self._update_game_displays()

    def _on_nsfw_override_changed(self, game_id: str, nsfw_override: int) -> None:
        """Handle per-game content filter override from context menu.

        Args:
            game_id: Game UUID
            nsfw_override: Override value (0=auto, 1=NSFW, -1=SFW)
        """
        logger.debug(f"NSFW override changed: {game_id} -> {nsfw_override}")
        self.game_service.set_nsfw_override(game_id, nsfw_override)

        # Update source data so filters reflect the change
        from luducat.core.content_filter import adult_confidence_from_sources
        for game in self._all_games:
            if game.get("id") == game_id:
                game["nsfw_override"] = nsfw_override
                # Recompute adult_confidence with the new override
                game["adult_confidence"] = adult_confidence_from_sources(
                    {}, game.get("tags", []), nsfw_override
                ) if nsfw_override != 0 else game.get("adult_confidence", 0.0)
                break

        # Re-filter to apply content filter changes
        self._apply_filters()
        self._update_game_displays()

    def _on_edit_tags_requested(self, game_id: str) -> None:
        """Handle request to edit tags for a game

        Args:
            game_id: Game UUID
        """
        # Get game info
        game = self.game_service.get_game(game_id)
        if not game:
            logger.warning(f"Game not found for tag editing: {game_id}")
            return

        game_title = game.get("title", _("Unknown"))

        # Get all available tags and current game tags
        all_tags = self.game_service.get_all_tags()
        game_tags = self.game_service.get_game_tags(game_id)
        game_tag_names = [t["name"] for t in game_tags]

        # Open tag editor dialog
        dialog = TagEditorDialog(
            game_title=game_title,
            all_tags=all_tags,
            game_tags=game_tag_names,
            parent=self
        )

        # Connect signal for creating new tags inline
        dialog.tag_created.connect(
            lambda name, color: self._on_tag_created_inline(name, color)
        )

        if dialog.exec_():
            # Get selected tags and update game
            selected_tags = dialog.get_selected_tags()
            self.game_service.set_game_tags(game_id, selected_tags)

            # Update the content area with new tags
            new_tags = self.game_service.get_game_tags(game_id)
            self.content_area.update_game_tags(game_id, new_tags)
            self._update_game_tags_in_cache(game_id, new_tags)

            # Refresh filter bar tags
            self._refresh_filter_bar_tags()

            logger.info(f"Updated tags for {game_title}: {selected_tags}")

    def _on_tag_created_inline(self, name: str, color: str) -> None:
        """Handle new tag created from within TagEditorDialog

        Args:
            name: Tag name
            color: Tag color
        """
        try:
            self.game_service.create_tag(name, color)
            logger.info(f"Created tag inline: {name}")
        except ValueError as e:
            logger.warning(f"Failed to create tag inline: {e}")

    def _update_game_tags_in_cache(self, game_id: str, tags: list) -> None:
        """Update tag data in game cache and tag index after tag assignment."""
        if game_id in self._games_by_id:
            old_tags = self._games_by_id[game_id].get("tags", [])
            self._games_by_id[game_id]["tags"] = tags

            # Remove old tag index entries for this game
            for tag in old_tags:
                if isinstance(tag, dict):
                    tag_name = tag.get("name", "")
                    if tag_name and tag_name in self._tag_index:
                        self._tag_index[tag_name].discard(game_id)
                        if not self._tag_index[tag_name]:
                            del self._tag_index[tag_name]

            # Add new tag index entries
            for tag in tags:
                if isinstance(tag, dict):
                    tag_name = tag.get("name", "")
                    if tag_name:
                        if tag_name not in self._tag_index:
                            self._tag_index[tag_name] = set()
                        self._tag_index[tag_name].add(game_id)

        # Invalidate filter cache so next _apply_filters() rebuilds
        self._prev_filter_state = None

    def _rebuild_tag_index(self) -> None:
        """Rebuild _tag_index from current game cache data."""
        self._tag_index.clear()
        for gid, game in self._games_by_id.items():
            for tag in game.get("tags", []):
                if isinstance(tag, dict):
                    tag_name = tag.get("name", "")
                    if tag_name:
                        if tag_name not in self._tag_index:
                            self._tag_index[tag_name] = set()
                        self._tag_index[tag_name].add(gid)
        self._prev_filter_state = None

    def _on_view_screenshots_requested(self, game_id: str, index: int) -> None:
        """Handle request to view screenshots in fullscreen

        Args:
            game_id: Game UUID
            index: Screenshot index to start at
        """
        logger.info(f"Screenshot viewer requested for game_id={game_id}, index={index}")

        # Get screenshots from game_service (handles caching and lazy loading)
        # This is more reliable than content_area.get_screenshots() which depends
        # on list_view state that may not be updated yet on Windows
        screenshots = self.game_service.get_screenshots(game_id)
        n_ss = len(screenshots) if screenshots else 0
        logger.info(
            f"game_service.get_screenshots returned"
            f" {n_ss} screenshots"
        )

        if not screenshots:
            # Fallback to content_area in case list_view has them
            screenshots = self.content_area.get_screenshots()
            n_ss = len(screenshots) if screenshots else 0
            logger.info(
                f"Fallback: content_area.get_screenshots"
                f" returned {n_ss} screenshots"
            )

        if not screenshots:
            logger.warning(
                f"No screenshots available for game"
                f" {game_id}"
            )
            return

        first = screenshots[0][:80] if screenshots else 'none'
        logger.info(
            f"Opening screenshot viewer with"
            f" {len(screenshots)} screenshots,"
            f" first: {first}..."
        )

        # Get game title for dialog
        game = self.game_service.get_game(game_id)
        title = game.get("title", _("Screenshots")) if game else _("Screenshots")

        dialog = ImageViewerDialog(
            images=screenshots,
            start_index=index,
            title=_("Screenshots - {title}").format(title=title),
            parent=self
        )
        dialog.exec_()

    def _on_view_cover_requested(self, game_id: str) -> None:
        """Handle request to view cover in fullscreen

        Args:
            game_id: Game UUID
        """
        game = self.game_service.get_game(game_id)
        if not game:
            logger.warning(f"Game not found: {game_id}")
            return

        cover_url = game.get("cover_image", "")
        if not cover_url:
            logger.warning(f"No cover image available for game {game_id}")
            return

        title = game.get("title", _("Cover"))

        dialog = ImageViewerDialog(
            images=[cover_url],
            start_index=0,
            title=_("Cover - {title}").format(title=title),
            parent=self
        )
        dialog.exec_()

    def _on_search_changed(self, text: str) -> None:
        """Handle search text change

        Args:
            text: Search query
        """
        logger.debug(f"Search: {text}")
        # Apply filters to all views (game list, cover view, screenshot view)
        self._apply_filters()

    def _on_filters_changed(self, filters: dict) -> None:
        """Handle filter changes

        Args:
            filters: Dict of active filters
        """
        logger.debug(f"Filters changed: {filters}")
        # Apply filters to all views (game list, cover view, screenshot view)
        self._apply_filters()

    def _on_sort_changed(self, mode: str, reverse: bool, favorites_first: bool) -> None:
        """Handle sort option change from toolbar

        Args:
            mode: Sort mode (name, recent, added, franchise, publisher, developer, release)
            reverse: If True, reverse the sort order (e.g., Z-A instead of A-Z)
            favorites_first: If True, sort favorites to the top regardless of sort mode
        """
        logger.debug(
            f"Sort changed: mode={mode},"
            f" reverse={reverse},"
            f" favorites_first={favorites_first}"
        )

        self._current_sort_mode = mode
        self._sort_reverse = reverse
        self._favorites_first = favorites_first

        # Re-sort and update displays
        self._sort_games()
        self._update_game_displays()

    def _sort_games(self) -> None:
        """Sort the filtered games list based on current sort settings.

        Uses self._current_sort_mode, self._sort_reverse, and self._favorites_first.
        Modifies self._filtered_games in place.

        Uses two-pass stable sort: first by value (with direction), then by
        favorites (always ascending). This ensures favorites-first works
        regardless of sort direction.
        """
        if not self._filtered_games:
            return

        def sort_title(game: Dict[str, Any]) -> str:
            """Title lowered with leading whitespace stripped for sort."""
            return (game.get("title") or "").lower().lstrip()

        def get_first_sorted(items: list) -> str:
            """Get first item from a sorted list of strings.

            For publishers/developers, sort alphabetically and return first.
            """
            if not items:
                return ""
            if isinstance(items, str):
                return items.lower()
            sorted_items = sorted([str(i).lower() for i in items if i])
            return sorted_items[0] if sorted_items else ""

        # Compute sort direction BEFORE defining key function (closured)
        # Date-based sorts default to newest-first (descending)
        # Name-based sorts default to A-Z (ascending)
        date_based_modes = {
            SORT_MODE_RECENT, SORT_MODE_ADDED,
            SORT_MODE_RELEASE, SORT_MODE_FAMILY_LICENSES,
        }
        default_descending = self._current_sort_mode in date_based_modes
        should_reverse = default_descending != self._sort_reverse

        # Sentinel values: always sort missing data to the end
        end_sentinel_hi = "9999-99-99"  # sorts last in ascending
        end_sentinel_lo = "0000-00-00"  # sorts last in descending (after reverse)
        end_sentinel = end_sentinel_hi if not should_reverse else end_sentinel_lo

        def get_sort_key(game: Dict[str, Any]):
            """Generate sort key for a game (value only, no favorites)."""
            if self._current_sort_mode == SORT_MODE_NAME:
                return sort_title(game)

            elif self._current_sort_mode == SORT_MODE_RECENT:
                last_launched = game.get("last_launched")
                key = last_launched if last_launched else end_sentinel
                return (key, sort_title(game))

            elif self._current_sort_mode == SORT_MODE_ADDED:
                added = game.get("added_at") or ""
                key = added if added else end_sentinel
                return (key, sort_title(game))

            elif self._current_sort_mode == SORT_MODE_PUBLISHER:
                publishers = game.get("publishers") or []
                publisher = get_first_sorted(publishers)
                if not publisher:
                    publisher = "zzzzz" if not should_reverse else ""
                return (publisher, sort_title(game))

            elif self._current_sort_mode == SORT_MODE_DEVELOPER:
                developers = game.get("developers") or []
                developer = get_first_sorted(developers)
                if not developer:
                    developer = "zzzzz" if not should_reverse else ""
                return (developer, sort_title(game))

            elif self._current_sort_mode == SORT_MODE_FRANCHISE:
                franchise = (game.get("franchise") or "").lower()
                if not franchise:
                    franchise = "zzzzz" if not should_reverse else ""
                return (franchise, sort_title(game))

            elif self._current_sort_mode == SORT_MODE_RELEASE:
                release = game.get("release_date") or ""
                st = sort_title(game)
                # Fast path: already ISO from cache normalisation
                if release and len(release) >= 10 and release[4] == "-" and release[:4].isdigit():
                    return (release[:10], st)
                # Fallback: try parsing non-ISO values that slipped through
                if release:
                    from ..core.dt import parse_release_date
                    iso = parse_release_date(release)
                    if iso:
                        return (iso, st)
                return (end_sentinel, st)

            elif self._current_sort_mode == SORT_MODE_FAMILY_LICENSES:
                count = game.get("family_license_count", 0)
                return (count, sort_title(game))

            else:
                return sort_title(game)

        # Pass 1: Sort by value with appropriate direction
        self._filtered_games.sort(key=get_sort_key, reverse=should_reverse)

        # Diagnostic: log games with missing/unparseable release dates
        if self._current_sort_mode == SORT_MODE_RELEASE:
            sentinel_games = [
                g for g in self._filtered_games
                if not (g.get("release_date") or "")[:4].isdigit()
                or len(g.get("release_date") or "") < 10
            ]
            if sentinel_games:
                logger.info(
                    f"Release date sort: {len(sentinel_games)}/{len(self._filtered_games)} "
                    f"games have missing/unparseable dates"
                )
                for g in sentinel_games:
                    logger.debug(
                        f"  No release date: id={g.get('id')}, "
                        f"title={g.get('title')!r}, "
                        f"raw={g.get('release_date')!r}, "
                        f"stores={list((g.get('store_app_ids') or {}).keys())}"
                    )

        # Pass 2: Stable sort favorites to top (always ascending)
        # TimSort is stable, so value ordering is preserved within each group
        if self._favorites_first:
            self._filtered_games.sort(
                key=lambda g: 0 if g.get("is_favorite") else 1
            )

    def set_game_count(self, count: int) -> None:
        """Update game count display

        Args:
            count: Number of games
        """
        self.status_bar.set_game_count(count)
        self._update_game_count_tooltip()

    def _update_game_count_tooltip(self) -> None:
        """Build a tooltip showing per-store game counts."""
        try:
            from ..core.plugin_manager import PluginManager

            unique, total_store = self.game_service.get_game_counts()
            per_store = self.game_service.get_per_store_counts()

            if not per_store:
                self.status_bar.count_label.setToolTip("")
                return

            lines = []
            for store_name in sorted(per_store, key=lambda s: per_store[s], reverse=True):
                display = PluginManager.get_store_display_name(store_name)
                lines.append(f"{display}: {per_store[store_name]:,}")

            duplicates = total_store - unique
            lines.append("")
            lines.append(
                ngettext(
                    "{count} unique game",
                    "{count} unique games",
                    unique,
                ).format(count=f"{unique:,}")
            )
            if duplicates > 0:
                lines.append(
                    ngettext(
                        "{count} game owned on multiple stores",
                        "{count} games owned on multiple stores",
                        duplicates,
                    ).format(count=f"{duplicates:,}")
                )

            self.status_bar.count_label.setToolTip("\n".join(lines))
        except Exception:
            pass

    # Memory pressure threshold (MB) - trigger cache cleanup above this
    MEMORY_PRESSURE_THRESHOLD_MB = 800

    def _log_memory_stats(self) -> None:
        """Log periodic memory statistics (DEBUG level)"""
        import gc
        from ..utils.image_cache import get_cover_cache, get_screenshot_cache
        from ..ui.list_view import get_description_image_cache

        parts = []
        rss_mb = 0

        # Process memory (psutil)
        if HAS_PSUTIL:
            mem = psutil.Process().memory_info()  # pyright: ignore[reportPossiblyUnboundVariable]
            rss_mb = mem.rss // (1024 * 1024)
            parts.append(f"RSS={rss_mb}MB")

        # Image caches
        cover_cache = get_cover_cache()
        screenshot_cache = get_screenshot_cache()
        desc_cache = get_description_image_cache()

        cover = cover_cache.get_cache_stats()
        screenshot = screenshot_cache.get_cache_stats()
        desc = desc_cache.get_cache_stats()
        parts.append(
            f"covers={cover['memory_bytes'] / 1e6:.0f}MB/{cover['max_memory_bytes'] / 1e6:.0f}MB "
            f"({cover['memory_items']} items)"
        )
        ss_mem = screenshot['memory_bytes'] / 1e6
        ss_max = screenshot['max_memory_bytes'] / 1e6
        ss_items = screenshot['memory_items']
        parts.append(
            f"screenshots={ss_mem:.0f}MB/{ss_max:.0f}MB "
            f"({ss_items} items)"
        )
        parts.append(
            f"desc_imgs={desc['memory_bytes'] / 1e6:.0f}MB/{desc['max_memory_bytes'] / 1e6:.0f}MB "
            f"({desc['memory_items']} items)"
        )

        # Game cache
        if self.game_service:
            parts.append(f"games={len(self.game_service._games_cache)}")

        logger.debug(f"Memory: {', '.join(parts)}")

        # Memory pressure: clear inactive caches if above threshold
        import time as _time
        if rss_mb > self.MEMORY_PRESSURE_THRESHOLD_MB:
            # Skip cleanup if suppressed after a previous no-op cleanup
            now = _time.monotonic()
            if now < self._memory_cleanup_suppressed_until:
                return

            current_mode = self.content_area.current_view_mode()
            threshold = self.MEMORY_PRESSURE_THRESHOLD_MB
            logger.info(
                f"Memory pressure ({rss_mb}MB > {threshold}MB)"
                f" - clearing inactive caches"
                f" (current: {current_mode})"
            )

            # Only clear caches not currently in use (preserve active view's images)
            if current_mode != VIEW_MODE_COVER:
                cover_cache.clear_memory_cache()
                logger.debug("Cleared cover cache (not in use)")
            if current_mode != VIEW_MODE_SCREENSHOT:
                screenshot_cache.clear_memory_cache()
                logger.debug("Cleared screenshot cache (not in use)")
            # Description images always safe to clear (lazy-loaded on demand)
            desc_cache.clear_memory_cache()

            # Clear Qt's global pixmap cache
            from PySide6.QtGui import QPixmapCache
            QPixmapCache.clear()

            # Clear description text cache
            if self.game_service:
                self.game_service._description_cache.clear()

            # Force garbage collection (multiple passes for generations)
            gc.collect(0)
            gc.collect(1)
            gc.collect(2)

            # Platform-specific: force memory release to OS
            from ..utils.memory import release_memory_to_os
            release_memory_to_os()

            # Log after cleanup and suppress if ineffective
            if HAS_PSUTIL:
                mem_after = psutil.Process().memory_info()  # pyright: ignore[reportPossiblyUnboundVariable]
                after_mb = mem_after.rss // (1024 * 1024)
                freed = rss_mb - after_mb
                logger.info(f"Memory after cleanup: {after_mb}MB (freed {freed}MB)")
                if freed < 10:
                    # Cleanup was ineffective — remaining memory is baseline data
                    # (games cache, models, etc.). Suppress for 30 minutes.
                    self._memory_cleanup_suppressed_until = now + 1800
                    logger.debug("Memory cleanup ineffective, suppressing for 30 minutes")

    def _check_disk_health(self) -> None:
        """Periodic disk health check — only warns if status worsened."""
        from ..core.config import get_data_dir, get_cache_dir
        from ..core.directory_health import check_directory

        worst = "green"
        details = []
        for label, path in [(_("Data"), get_data_dir()), (_("Cache"), get_cache_dir())]:
            health = check_directory(path)
            if health.status == "red":
                worst = "red"
                details.append(_("{label}: {error}").format(
                    label=label, error=health.error or _("not writable")))
            elif health.status == "yellow" and worst != "red":
                worst = "yellow"
                details.append(_("{label}: {free_mb} MB free").format(
                    label=label, free_mb=health.free_mb))

        # Only warn if status degraded since last check
        severity_order = {"green": 0, "yellow": 1, "red": 2}
        if severity_order[worst] > severity_order[self._last_disk_status]:
            from PySide6.QtWidgets import QMessageBox
            if worst == "red":
                QMessageBox.warning(
                    self, _("Disk Problem"),
                    _("Directory problems detected:\n\n{details}\n\n"
                      "The application may not work correctly.").format(
                        details="\n".join(details)))
            else:
                QMessageBox.information(
                    self, _("Low Disk Space"),
                    _("Low disk space detected:\n\n{details}\n\n"
                      "Consider freeing disk space.").format(
                        details="\n".join(details)))
        self._last_disk_status = worst

    def _on_disk_write_disabled(self, reason: str) -> None:
        """Called by image cache circuit breaker when disk writes are disabled."""
        if self._disk_write_warning_shown:
            return
        self._disk_write_warning_shown = True
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            self, _("Cache Disk Writes Disabled"),
            _("Image cache can no longer write to disk ({reason}).\n\n"
              "Covers and screenshots will still load from the network "
              "but won't be saved locally.").format(reason=reason))

    def _on_restart_after_restore(self) -> None:
        """Restart the application after a backup restore.

        Closes the database, runs migrations on the restored DB to bring
        it up to the current schema, then re-execs the process for a
        clean restart.
        """
        import os
        import sys

        logger.info("Restarting after backup restore...")

        # Close the current database connection
        self.database.close()

        # Run migrations on the restored database
        try:
            temp_db = Database()  # triggers init_or_migrate on restored games.db
            temp_db.close()
            logger.info("Post-restore migrations completed successfully")
        except Exception as e:
            logger.error(f"Post-restore migration failed: {e}")
            # Continue with restart anyway — the app will handle it on startup

        # Re-exec the process for a clean restart
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def closeEvent(self, event) -> None:
        """Handle window close"""
        # Stop memory logging timer
        self._memory_timer.stop()

        # Check if we need to wait for background tasks
        if (self._sync_worker and self._sync_worker.isRunning()) or \
           (self._data_loader and self._data_loader.isRunning()):
            self.status_bar.set_game_count(0)  # Clear count
            self.status_bar.count_label.setText(_("Shutting down..."))
            # Update sync widget too (it sits above the status bar)
            if self._sync_worker and self._sync_worker.isRunning():
                self._sync_widget.shutdown()
            QApplication.processEvents()

        # Cancel any running sync — store fetches now respect cancel_check
        # and should stop within 1-2s. Terminate as last resort.
        if self._sync_worker and self._sync_worker.isRunning():
            self._sync_worker.cancel()
            if not self._sync_worker.wait(10000):
                logger.warning("Sync worker did not stop in 10s, terminating")
                self._sync_worker.terminate()
                self._sync_worker.wait(2000)

        # Wait for data loader to finish
        if self._data_loader and self._data_loader.isRunning():
            self._data_loader.wait(3000)

        self._save_state()

        # Shutdown cover/screenshot view workers (must be before image cache shutdown)
        self.content_area.cover_view.shutdown()
        self.content_area.screenshot_view.shutdown()

        # Clean up image caches and HTTP session
        from ..utils.image_cache import shutdown_all_caches
        shutdown_all_caches()

        # Clean up — persist plugin state BEFORE final config save so
        # runtime changes (hit counters, etc.) are included on disk
        self.plugin_manager.close()
        self.config.save()
        self.database.close()

        event.accept()

    # === Data Loading ===

    def _initialize_managers(self) -> None:
        """Initialize RuntimeManager and GameManager

        Called during startup to detect available runtimes.
        """
        import asyncio

        if self.runtime_manager:
            logger.debug("Initializing RuntimeManager...")
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self.runtime_manager.initialize())
                finally:
                    loop.close()

                stats = self.runtime_manager.get_runtime_stats()
                logger.info(
                    f"RuntimeManager ready: {stats['total_platforms']} platforms, "
                    f"{stats['provider_types']} provider types, "
                    f"{stats['available_runners']} runners"
                )
                # Re-populate platform combo now that runners/platforms are detected
                self.content_area.refresh_platform_combo()
            except Exception as e:
                logger.warning(f"RuntimeManager initialization failed: {e}")

        if self.game_manager:
            logger.debug("Initializing GameManager...")
            try:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self.game_manager.initialize())
                finally:
                    loop.close()
                logger.info("GameManager ready")
            except Exception as e:
                logger.warning(f"GameManager initialization failed: {e}")

    def _run_pending_media_cleanup(self) -> None:
        """Run one-time media field cleanup if flagged by config migration."""
        if not self.config.get("_pending_media_cleanup", False):
            return
        try:
            count = self.game_service.reset_media_fields(
                ["cover", "hero", "screenshots"],
                only_sources={"pcgamingwiki"},
            )
            logger.info(f"Startup media cleanup: reset {count} store_games")
        except Exception as e:
            logger.error(f"Startup media cleanup failed: {e}")
        finally:
            self.config.set("_pending_media_cleanup", None)
            self.config.save()

    def _run_pending_content_descriptors_repair(self) -> None:
        """Run one-time content_descriptors repair if flagged by config migration v6."""
        if not self.config.get("_pending_content_descriptors_repair", False):
            return
        try:
            # Find the Steam plugin
            steam_plugin = None
            from luducat.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if pm:
                steam_plugin = pm.get_plugin("steam")
            if not steam_plugin or not steam_plugin.is_authenticated():
                logger.debug(
                    "Content descriptors repair deferred: Steam not authenticated"
                )
                return
            stats = steam_plugin.repair_content_descriptors()
            # Only clear flag if all games were processed (not rate-limited)
            if not stats.get("rate_limited", False):
                self.config.set("_pending_content_descriptors_repair", None)
                self.config.save()
                logger.info(f"Content descriptors repair complete: {stats}")
            else:
                logger.info(
                    f"Content descriptors repair partial (rate limited): {stats}"
                )
        except Exception as e:
            logger.error(f"Content descriptors repair failed: {e}")
            # Clear flag on unexpected errors to avoid infinite retry
            self.config.set("_pending_content_descriptors_repair", None)
            self.config.save()

    def _force_refresh_games(self) -> None:
        """Force cache invalidation and reload all games.

        Re-resolves all metadata (covers, screenshots, hero banners)
        from database according to current priority order.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            self.game_service.invalidate_cache()
            self._load_games()
        finally:
            QApplication.restoreOverrideCursor()

    def _schedule_cache_refresh(self) -> None:
        """Debounce handler for cache_refresh_requested signals.

        Restarts the 10-second timer so rapid signals coalesce into
        a single _load_games() call, keeping the event loop free to
        repaint the progress bar between refreshes.
        """
        self._cache_refresh_timer.start()

    def _load_games(self) -> None:
        """Load games from database with loading overlay

        Shows a loading overlay while loading games synchronously.
        The overlay keeps the UI responsive (moveable, resizable) by
        processing Qt events during loading phases.

        Note: Uses synchronous loading instead of background threads
        to avoid segfaults in AppImage (FUSE) environments.
        """
        # Prevent reentrant/rapid successive loads
        if self._loading_in_progress:
            logger.warning("_load_games: already in progress, skipping")
            return

        self._loading_in_progress = True

        # Diagnostic: log who called _load_games to identify double-load triggers
        import traceback
        caller = traceback.extract_stack(limit=3)[-2]
        logger.debug(
            "Loading games from database... (caller: %s:%d %s)",
            caller.filename.rsplit("/", 1)[-1], caller.lineno, caller.name,
        )

        # Initialize managers on first load
        if self.runtime_manager and not getattr(self, '_managers_initialized', False):
            self._initialize_managers()
            self._managers_initialized = True

        # Skip loading overlay when grid is already populated via progressive loading —
        # the set_games() model reset is instant (covers are memory-cached).
        show_overlay = not self._progressive_ids
        if show_overlay:
            self._loading_overlay.show_loading(_("Loading games..."), _("Preparing..."))

        try:
            # Load games synchronously with progress callback
            games = self.game_service.get_all_games(
                progress_callback=self._on_loading_progress
            )
            self._on_games_loaded(games)
        except Exception as e:
            self._on_games_load_error(str(e))
        finally:
            self._loading_in_progress = False
            if show_overlay:
                self._loading_overlay.hide_loading()
            else:
                # No overlay was shown (progressive loading skip) — force
                # a repaint so the model-reset changes become visible.
                QApplication.processEvents()

    def _on_loading_progress(self, message: str, detail: str, progress: int) -> None:
        """Handle loading progress updates

        Updates the loading overlay and processes Qt events to keep
        the UI responsive during loading.

        Args:
            message: Main status message
            detail: Detail text (current operation)
            progress: Progress percentage (0-100)
        """
        self._loading_overlay.update_status(message, detail, progress)

    def _on_games_loaded(self, games: List[Dict[str, Any]]) -> None:
        """Handle games loaded successfully"""
        # Clear progressive loading state — full rebuild replaces everything
        self._progressive_ids.clear()
        self._progressive_stores.clear()

        # Reassign rather than clear() — clear() mutates the list in place,
        # which corrupts the model's shared reference before it gets a proper
        # beginResetModel/endResetModel via _update_game_displays().
        self._all_games = games
        self._filtered_games = []

        # Build inverted indexes for fast set-based filtering
        self._build_filter_indexes()

        # Save current filter state BEFORE updating available stores/tags
        # (set_available_stores/tags resets checkboxes to defaults)
        saved_filters = self.filter_bar.get_active_filters()

        # Update store/tag filters (this resets selections to defaults)
        stores = self.game_service.get_store_info()
        self.filter_bar.set_available_stores(stores)
        self._update_store_sync_menu()
        self._update_metadata_sync_menu()

        self._refresh_filter_bar_tags()

        # Check if any games have game modes data (from IGDB or other metadata plugins)
        has_game_modes = any(g.get("game_modes") for g in self._all_games)
        self.filter_bar.set_game_modes_available(has_game_modes)

        # Set compatibility filter visibility based on available data
        has_family_shared = len(self._family_shared_ids) > 0
        has_orphaned = len(self._orphaned_ids) > 0
        has_protondb = len(self._protondb_game_ids) > 0
        has_steam_deck = len(self._steam_deck_game_ids) > 0
        self.filter_bar.set_compat_filters_available(
            has_protondb, has_steam_deck,
            family_shared=has_family_shared, orphaned=has_orphaned,
        )
        self.filter_bar.set_family_sort_available(has_family_shared)

        # Collect unique developers, publishers, and release years from game data
        developers = sorted(
            {d for g in self._all_games for d in (g.get("developers") or []) if d},
            key=str.lower
        )
        publishers = sorted(
            {p for g in self._all_games for p in (g.get("publishers") or []) if p},
            key=str.lower
        )
        genres = sorted(
            {g for game in self._all_games for g in (game.get("genres") or []) if g},
            key=str.lower
        )
        years_set = set()
        for g in self._all_games:
            rd = g.get("release_date", "")
            if rd and len(rd) >= 4 and rd[:4].isdigit():
                years_set.add(rd[:4])
        years = sorted(years_set, reverse=True)

        self.filter_bar.set_available_developers(developers)
        self.filter_bar.set_available_publishers(publishers)
        self.filter_bar.set_available_genres(genres)
        self.filter_bar.set_available_years(years)

        # Restore filter state (handles gracefully if stores/tags changed)
        # Only restore if we had active filters (not first load)
        if saved_filters.get("stores") or saved_filters.get("tags"):
            self.filter_bar.set_active_filters(saved_filters)

        # Now apply filters (includes base filter to exclude hidden games by default)
        self._apply_filters()

        # Restore last selected game if available and visible in current filter
        last_game_id = self.config.get("ui.selected_game_id", "")
        if last_game_id:
            if any(g.get("id") == last_game_id for g in self._filtered_games):
                self.game_list.select_game(last_game_id)
                self._on_game_selected(last_game_id)
                logger.debug(f"Restored last selected game: {last_game_id}")
            elif self._filtered_games:
                # Last game not in current filter, select first game
                first_id = self._filtered_games[0].get("id", "")
                if first_id:
                    self.game_list.select_game(first_id)
                    self._on_game_selected(first_id)
        elif self._filtered_games:
            # No saved game, select first game
            first_id = self._filtered_games[0].get("id", "")
            if first_id:
                self.game_list.select_game(first_id)
                self._on_game_selected(first_id)
                self._on_game_selected(first_id)

        # Check if we should show news dialog (first load only)
        if not self._news_check_done:
            self._news_check_done = True
            # Use QTimer to show dialog after UI is fully rendered
            QTimer.singleShot(200, self._check_show_news)
            # Deferred update check (3s after UI settles)
            QTimer.singleShot(3000, self._check_for_updates)

        # Restore density slider visibility based on view mode
        current_mode = self.toolbar.get_current_view_mode()
        if current_mode in (VIEW_MODE_COVER, VIEW_MODE_SCREENSHOT):
            self.status_bar.set_density_visible(True)
            if current_mode == VIEW_MODE_COVER:
                current_density = self.config.get("appearance.cover_grid_density", 250)
            else:
                current_density = self.config.get("appearance.screenshot_grid_density", 250)
            self.status_bar.set_density(current_density)
        else:
            self.status_bar.set_density_visible(False)

        logger.info(f"Loaded {len(self._all_games)} games")

        # Clean up worker reference (legacy, kept for compatibility)
        self._data_loader = None

    def _on_games_load_error(self, error: str) -> None:
        """Handle error loading games"""
        logger.error(f"Failed to load games: {error}")

        QMessageBox.warning(
            self,
            _("Error Loading Games"),
            _("Failed to load games from database:\n{error}").format(error=error)
        )

        # Clean up worker reference (legacy, kept for compatibility)
        self._data_loader = None

    def _update_game_displays(self, update_filters: bool = False) -> None:
        """Update all displays with current filtered games

        Args:
            update_filters: If True, also refresh store/tag filters (slow)
        """
        # Update game list
        self.game_list.set_games(self._filtered_games)

        # Update content views (convert list to dict keyed by game_id)
        games_dict = {g.get("id", ""): g for g in self._filtered_games if g.get("id")}
        self.content_area.set_games(games_dict)

        # Update counts
        self.set_game_count(len(self._filtered_games))

        # Only update stores/tags when explicitly requested (on initial load or after sync)
        if update_filters:
            stores = self.game_service.get_store_info()
            self.filter_bar.set_available_stores(stores)
            self._update_store_sync_menu()
            self._update_metadata_sync_menu()

            self._refresh_filter_bar_tags()

            # Check if any games have game modes data
            has_game_modes = any(g.get("game_modes") for g in self._all_games)
            self.filter_bar.set_game_modes_available(has_game_modes)

            # Update developer/publisher/year filters
            developers = sorted(
                {d for g in self._all_games for d in (g.get("developers") or []) if d},
                key=str.lower
            )
            publishers = sorted(
                {p for g in self._all_games for p in (g.get("publishers") or []) if p},
                key=str.lower
            )
            years_set = set()
            for g in self._all_games:
                rd = g.get("release_date", "")
                if rd and len(rd) >= 4 and rd[:4].isdigit():
                    years_set.add(rd[:4])
            years = sorted(years_set, reverse=True)

            self.filter_bar.set_available_developers(developers)
            self.filter_bar.set_available_publishers(publishers)
            self.filter_bar.set_available_years(years)

    def _build_filter_indexes(self) -> None:
        """Build inverted indexes for fast set-based filtering.

        Called once when _all_games is populated. Subsequent _apply_filters()
        calls use set intersections instead of per-game iteration.
        """
        from datetime import datetime, timedelta
        from ..core.constants import DEFAULT_RECENT_PLAYED_DAYS

        # Invalidate incremental filter cache — new data, stale IDs must go
        self._prev_filter_state = None
        self._prev_filter_result_ids = set()

        # Clear all indexes
        self._games_by_id.clear()
        self._all_game_ids.clear()
        self._non_hidden_ids.clear()
        self._hidden_ids.clear()
        self._favorite_ids.clear()
        self._free_ids.clear()
        self._installed_ids.clear()
        self._demo_ids.clear()
        self._store_index.clear()
        self._tag_index.clear()
        self._game_mode_index.clear()
        self._developer_index.clear()
        self._publisher_index.clear()
        self._genre_index.clear()
        self._year_index.clear()
        self._title_index.clear()
        self._adult_content_ids.clear()
        self._family_shared_ids.clear()
        self._orphaned_ids.clear()
        self._protondb_game_ids.clear()
        self._steam_deck_game_ids.clear()
        self._recent_cache.clear()

        # Compute recent threshold
        recent_days = self.config.get("ui.recently_played_days", DEFAULT_RECENT_PLAYED_DAYS)
        recent_threshold = datetime.now() - timedelta(days=recent_days)
        self._recent_cache_threshold = recent_threshold

        # Content filter threshold
        from ..core.content_filter import DEFAULT_ADULT_THRESHOLD
        adult_threshold = self.config.get(
            "content_filter.threshold", DEFAULT_ADULT_THRESHOLD
        )

        for game in self._all_games:
            gid = game.get("id", "")
            if not gid:
                continue

            self._games_by_id[gid] = game
            self._all_game_ids.add(gid)

            # Property-based indexes
            if game.get("is_hidden", False):
                self._hidden_ids.add(gid)
            else:
                self._non_hidden_ids.add(gid)

            if game.get("is_favorite"):
                self._favorite_ids.add(gid)

            if game.get("is_free"):
                self._free_ids.add(gid)

            if game.get("is_installed"):
                self._installed_ids.add(gid)

            if game.get("is_demo"):
                self._demo_ids.add(gid)

            # Title index for substring search
            self._title_index[gid] = (game.get("title") or "").lower()

            # Store index
            for store in game.get("stores", []):
                if store not in self._store_index:
                    self._store_index[store] = set()
                self._store_index[store].add(gid)

            # Tag index
            for tag in game.get("tags", []):
                if isinstance(tag, dict):
                    tag_name = tag.get("name", "")
                    if tag_name:
                        if tag_name not in self._tag_index:
                            self._tag_index[tag_name] = set()
                        self._tag_index[tag_name].add(gid)

            # Game mode index
            for mode in game.get("game_modes", []):
                if mode:
                    if mode not in self._game_mode_index:
                        self._game_mode_index[mode] = set()
                    self._game_mode_index[mode].add(gid)

            # Developer index
            for dev in (game.get("developers") or []):
                if dev:
                    if dev not in self._developer_index:
                        self._developer_index[dev] = set()
                    self._developer_index[dev].add(gid)

            # Publisher index
            for pub in (game.get("publishers") or []):
                if pub:
                    if pub not in self._publisher_index:
                        self._publisher_index[pub] = set()
                    self._publisher_index[pub].add(gid)

            # Genre index
            for genre in (game.get("genres") or []):
                if genre:
                    if genre not in self._genre_index:
                        self._genre_index[genre] = set()
                    self._genre_index[genre].add(gid)

            # Year index
            rd = game.get("release_date", "")
            if rd and len(rd) >= 4 and rd[:4].isdigit():
                year = rd[:4]
                if year not in self._year_index:
                    self._year_index[year] = set()
                self._year_index[year].add(gid)

            # Content filter index
            if game.get("adult_confidence", 0.0) >= adult_threshold:
                self._adult_content_ids.add(gid)

            # Family shared index (all games in the family sharing pool)
            if game.get("family_license_count", 0) >= 1:
                self._family_shared_ids.add(gid)

            # Orphaned index (no store links)
            if not game.get("stores"):
                self._orphaned_ids.add(gid)

            # Compatibility indexes
            if game.get("protondb_rating"):
                self._protondb_game_ids.add(gid)
            if game.get("steam_deck_compat"):
                self._steam_deck_game_ids.add(gid)

            # Recent index (games played within threshold)
            last_launched = game.get("last_launched")
            if last_launched:
                try:
                    if isinstance(last_launched, str):
                        date_str = last_launched.replace(" ", "T").replace("Z", "+00:00")
                        launch_date = datetime.fromisoformat(date_str)
                        if launch_date.tzinfo:
                            launch_date = launch_date.replace(tzinfo=None)
                    else:
                        launch_date = last_launched
                    if launch_date >= recent_threshold:
                        self._recent_cache.add(gid)
                except (ValueError, TypeError):
                    pass

        logger.debug(
            f"Built filter indexes: {len(self._all_game_ids)} games, "
            f"{len(self._store_index)} stores, {len(self._tag_index)} tags, "
            f"{len(self._developer_index)} developers, {len(self._publisher_index)} publishers, "
            f"{len(self._genre_index)} genres, {len(self._year_index)} years, "
            f"{len(self._adult_content_ids)} adult-flagged"
        )

    def _apply_filters(self) -> None:
        """Apply current search and filters to games using pre-built indexes.

        Uses set intersections for O(result_size) filtering instead of O(n) per-game.
        Falls back to linear scan if indexes haven't been built yet.
        """
        if not self._games_by_id:
            # Indexes not built yet (e.g., first call before _build_filter_indexes)
            self._filtered_games = list(self._all_games)
            self._sort_games()
            self._update_game_displays()
            return

        search_text = self.toolbar.get_search_text().lower()
        filters = self.filter_bar.get_active_filters()

        logger.debug(f"Applying filters: {filters}")

        # Extract filter values
        base_filter = filters.get("base_filter", "all")
        type_filters = filters.get("type_filters", [])
        active_stores = filters.get("stores", [])
        active_tags = filters.get("tags", [])
        active_game_modes = filters.get("game_modes", [])
        active_developers = filters.get("developers", [])
        active_publishers = filters.get("publishers", [])
        active_genres = filters.get("genres", [])
        active_years = filters.get("years", [])

        # Build filter state key (everything except search text)
        current_filter_state = {
            "base": base_filter,
            "types": tuple(sorted(type_filters)),
            "stores": tuple(sorted(active_stores)),
            "tags": tuple(sorted(active_tags)),
            "modes": tuple(sorted(active_game_modes)),
            "devs": tuple(sorted(active_developers)),
            "pubs": tuple(sorted(active_publishers)),
            "genres": tuple(sorted(active_genres)),
            "years": tuple(sorted(active_years)),
            "family_shared": filters.get("filter_family_shared", False),
            "orphaned": filters.get("filter_orphaned", False),
            "protondb": filters.get("filter_protondb", False),
            "steam_deck": filters.get("filter_steam_deck", False),
            "exact_stores": filters.get("exact_stores", False),
        }

        # Incremental optimization: if only search text changed, reuse cached filter results
        if self._prev_filter_state == current_filter_state and self._prev_filter_result_ids:
            result = self._prev_filter_result_ids
        else:
            # Step 1: Base filter — start with appropriate base set
            if base_filter == "hidden":
                result = set(self._hidden_ids)
            elif base_filter == "recent":
                # Recent includes hidden games if played recently
                result = set(self._recent_cache)
            else:
                # "all" — non-hidden games
                result = set(self._non_hidden_ids)

            # Step 1b: Content filter — implicit exclusion of adult content
            if self.config.get("content_filter.enabled", True) and self._adult_content_ids:
                result -= self._adult_content_ids

            # Step 2: Type filters (favorites, free)
            if "favorites" in type_filters:
                result &= self._favorite_ids
            if "free" in type_filters:
                result &= self._free_ids
            if "installed" in type_filters:
                result &= self._installed_ids
            if "demos" in type_filters:
                result &= self._demo_ids

            # Step 3: Multi-value filters — union within dimension, intersect across
            exact_stores = filters.get("exact_stores", False)
            if active_stores:
                if exact_stores:
                    # Exact match: game stores must equal selected stores exactly
                    selected = set(active_stores)
                    # Build reverse map: game_id → set of store names
                    game_store_map: Dict[str, set] = {}
                    for store_name, gids in self._store_index.items():
                        for gid in gids:
                            game_store_map.setdefault(gid, set()).add(store_name)
                    exact_ids = {
                        gid for gid in result
                        if game_store_map.get(gid, set()) == selected
                    }
                    result = result & exact_ids
                else:
                    # Union behavior: game has at least one of the selected stores
                    store_union = set()
                    for store in active_stores:
                        store_union |= self._store_index.get(store, set())
                    # Games without store info pass through (match original behavior)
                    games_with_stores = set()
                    for s in self._store_index.values():
                        games_with_stores |= s
                    games_without_stores = self._all_game_ids - games_with_stores
                    result &= (store_union | games_without_stores)

            if active_tags:
                tag_union = set()
                for tag in active_tags:
                    tag_union |= self._tag_index.get(tag, set())
                result &= tag_union

            if active_game_modes:
                mode_union = set()
                for mode in active_game_modes:
                    mode_union |= self._game_mode_index.get(mode, set())
                result &= mode_union

            if active_developers:
                dev_union = set()
                for dev in active_developers:
                    dev_union |= self._developer_index.get(dev, set())
                result &= dev_union

            if active_publishers:
                pub_union = set()
                for pub in active_publishers:
                    pub_union |= self._publisher_index.get(pub, set())
                result &= pub_union

            if active_genres:
                genre_union = set()
                for genre in active_genres:
                    genre_union |= self._genre_index.get(genre, set())
                result &= genre_union

            if active_years:
                year_union = set()
                for year in active_years:
                    year_union |= self._year_index.get(year, set())
                result &= year_union

            # Step 3b: Boolean filters (family shared, orphaned, compatibility)
            if filters.get("filter_family_shared"):
                result &= self._family_shared_ids
            if filters.get("filter_orphaned"):
                result &= self._orphaned_ids
            if filters.get("filter_protondb"):
                result &= self._protondb_game_ids
            if filters.get("filter_steam_deck"):
                result &= self._steam_deck_game_ids

            # Cache filter results for incremental search
            self._prev_filter_state = current_filter_state
            self._prev_filter_result_ids = set(result)

        before_count = len(result)

        # Step 4: Search filter — use FTS5 with fallback to title substring
        if search_text:
            fts_result = self.game_service.search_fts(search_text)
            if fts_result is not None:
                result = result & fts_result
            else:
                # Fallback: in-memory title substring search
                result = {gid for gid in result if search_text in self._title_index.get(gid, "")}

        # Step 5: Convert IDs to game dicts
        self._filtered_games = [
            self._games_by_id[gid]
            for gid in result
            if gid in self._games_by_id
        ]

        logger.debug(
            f"Filter results: {len(self._filtered_games)}/{before_count} passed "
            f"(search={bool(search_text)}, base={base_filter}, "
            f"stores={len(active_stores)}, tags={len(active_tags)}, "
            f"modes={len(active_game_modes)}, devs={len(active_developers)}, "
            f"pubs={len(active_publishers)}, genres={len(active_genres)}, "
            f"years={len(active_years)})"
        )

        # Apply current sort to filtered results
        self._sort_games()

        self._update_game_displays()

    # === Network / Online-Offline ===

    def _on_network_status_changed(self, online: bool) -> None:
        """Handle network monitor state change (auto-offline on connectivity loss)."""
        self.status_bar.set_online_status(online)
        if not online:
            logger.info("Network connectivity lost — switched to offline mode")

    def _on_connectivity_restored(self) -> None:
        """Handle connectivity detected while in offline mode."""
        self.status_bar.set_connectivity_hint(True)
        self._connectivity_hint_timer.start(10000)

    def _on_network_toggle_requested(self) -> None:
        """Handle click on network status indicator — toggle with confirmation."""
        currently_online = self._network_monitor.is_online

        if currently_online:
            reply = QMessageBox.question(
                self,
                _("Switch to Offline Mode"),
                _("Switch to offline mode?\n\n"
                  "Sync and image downloads will be paused."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._network_monitor.set_mode(False)
                self.status_bar.set_online_status(False)
        else:
            reply = QMessageBox.question(
                self,
                _("Switch to Online Mode"),
                _("Switch to online mode?\n\n"
                  "This will enable sync and image downloads."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._connectivity_hint_timer.stop()
                self._network_monitor.set_mode(True)
                self.status_bar.set_online_status(True)
                # Refresh game displays so uncached images re-queue
                self._update_game_displays()

    def _check_online_for_sync(self) -> bool:
        """Check if online before sync. Shows warning if offline.

        Returns True if sync should proceed (online mode).
        """
        if self._network_monitor.is_online:
            return True

        # Check if user suppressed the warning
        if not self.config.get("network.show_offline_sync_warning", True):
            return False

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle(_("Offline Mode"))
        msg.setText(_("Luducat is in offline mode. Switch to online mode to sync."))
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)

        cb = QCheckBox(_("Don't show this again"))
        msg.setCheckBox(cb)
        msg.exec()

        if cb.isChecked():
            self.config.set("network.show_offline_sync_warning", False)
            self.config.save()

        return False

    # === Sync Operations ===

    def _check_auto_sync(self) -> None:
        """Check if auto-sync is enabled and start sync if so."""
        if self.config.get("sync.auto_sync_on_startup", False):
            logger.info("Auto-sync enabled, starting sync...")
            self._start_sync(stores=None)

    def _trigger_initial_sync(self) -> None:
        """Trigger sync for all enabled stores after wizard completes."""
        # Get enabled stores from config
        plugins_config = self.config.get("plugins", {})
        enabled_stores = [
            pid for pid, cfg in plugins_config.items()
            if cfg.get("enabled", False)
        ]

        if enabled_stores:
            logger.info(f"Starting initial sync for enabled stores: {enabled_stores}")
            self._start_sync(stores=None)  # Sync all enabled stores

    def _on_priorities_changed(self) -> None:
        """Handle metadata priority change from settings.

        Invalidates caches so views re-fetch with updated priorities.
        """
        self.game_service.invalidate_cache()
        logger.info("Metadata priorities changed — cache invalidated")

    def _on_sync_requested(self) -> None:
        """Handle sync all stores request"""
        if not self._check_online_for_sync():
            return
        self._start_sync(stores=None)

    def _on_sync_store_requested(self, store_name: str) -> None:
        """Handle sync specific store request"""
        if not self._check_online_for_sync():
            return
        self._start_sync(stores=[store_name])

    def _on_full_resync_requested(self) -> None:
        """Handle full resync request with warning dialog"""
        if not self._check_online_for_sync():
            return
        reply = QMessageBox.warning(
            self,
            _("Full Resync"),
            _("This will re-download metadata for ALL games.\n\n"
              "Custom edits (tags, favorites, notes) will be preserved.\n"
              "Store and plugin metadata will be refreshed.\n\n"
              "Continue?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._start_sync(stores=None, full_resync=True)

    def _start_sync(
        self,
        stores: Optional[List[str]] = None,
        full_resync: bool = False,
    ) -> None:
        """Start non-blocking sync operation.

        Creates a SyncJobQueue, populates it with store fetch jobs and a
        metadata sentinel, then starts a SyncWorker that processes jobs
        sequentially while the user continues browsing.

        Args:
            stores: List of store names to sync, or None for all
            full_resync: If True, re-download metadata for all games
        """
        if self._sync_worker and self._sync_worker.isRunning():
            QMessageBox.information(
                self,
                _("Sync In Progress"),
                _("A sync operation is already in progress.")
            )
            return

        # Prompt for local data consent if not yet granted
        if not self.config.get("privacy.local_data_access_consent", False):
            reply = QMessageBox.question(
                self,
                _("Local Data Access"),
                _("{app_name} can read browser cookies for store authentication "
                  "and import tags, favorites, playtime, and install status "
                  "from local game launchers (Steam, GOG Galaxy, Heroic).\n\n"
                  "All data stays on your machine.\n\n"
                  "Allow access to local data?").format(app_name=APP_NAME),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.config.set("privacy.local_data_access_consent", True)
                self.config.save()
                self.plugin_manager.refresh_all_consent()

        # Get list of stores to sync
        if stores is None:
            plugins_config = self.config.get("plugins", {})
            stores = [
                pid for pid, cfg in plugins_config.items()
                if cfg.get("enabled", False) and self.plugin_manager.get_plugin(pid)
            ]
            known_stores = set(PluginManager.get_store_plugin_names())
            stores = [s for s in stores if s in known_stores]

        if not stores:
            logger.info("No stores to sync")
            return

        # Build job queue
        queue = SyncJobQueue()
        jobs = self.game_service.build_sync_jobs(stores, full_resync)
        queue.add_jobs(jobs)

        # Show sync widget
        self._sync_widget.start(queue)

        # Create and start worker
        self._sync_worker = SyncWorker(
            self.game_service,
            queue,
            full_resync=full_resync,
            parent=self,
        )

        # Phase progress signals -> sync widget
        self._sync_worker.phase_started.connect(self._sync_widget.on_phase_started)
        self._sync_worker.phase_progress.connect(self._sync_widget.on_phase_progress)
        self._sync_worker.phase_finished.connect(self._sync_widget.on_phase_finished)
        self._sync_worker.rate_limit.connect(self._on_rate_limit_status)
        self._sync_worker.rate_limit.connect(self._sync_widget.on_rate_limit)
        self._sync_worker.rate_limit_countdown.connect(self._sync_widget.on_rate_limit_countdown)
        self._sync_worker.cache_refresh_requested.connect(self._schedule_cache_refresh)

        # Progressive loading
        self._sync_worker.games_batch_ready.connect(self._on_games_batch)

        # Completion signals
        self._sync_worker.sync_finished.connect(self._on_sync_finished)
        self._sync_worker.sync_error.connect(self._on_sync_error)
        self._sync_worker.cancelled.connect(self._on_sync_cancelled)
        self._sync_worker.sync_warning.connect(self._on_sync_warning)

        # Sync widget controls
        self._sync_widget.cancel_requested.connect(self._cancel_sync)
        self._sync_widget.skip_requested.connect(self._skip_sync_plugin)

        self._pending_sync_warnings = []
        self._sync_worker.start()

    def _cancel_sync(self) -> None:
        """Cancel running sync"""
        if self._sync_worker:
            self._sync_worker.cancel()

    def _skip_sync_plugin(self) -> None:
        """Skip remaining jobs for the current plugin."""
        if self._sync_worker:
            self._sync_worker.skip_current_plugin()

    def _on_sync_finished(self, stats: Dict[str, Any]) -> None:
        """Handle sync completed — show summary in sync widget, reload games."""
        self._sync_widget.finish(stats)
        logger.info("Sync finished")

        # Persist in-memory plugin state (e.g. author hit counters)
        for name in self.plugin_manager._loaded:
            try:
                self.plugin_manager.persist_plugin_settings(name)
            except Exception as e:
                logger.debug(f"Failed to persist settings for {name}: {e}")
        self.config.save()

        # Final reload to pick up all changes
        self._load_games()

        # Show any collected sync warnings
        for title, msg in getattr(self, "_pending_sync_warnings", []):
            QMessageBox.warning(self, title, msg)
        self._pending_sync_warnings = []

        # Check for updates after sync (deferred to let UI settle)
        QTimer.singleShot(2000, self._check_for_updates)

    def _on_sync_warning(self, title: str, message: str) -> None:
        """Collect sync warnings to show after sync completes."""
        if not hasattr(self, "_pending_sync_warnings"):
            self._pending_sync_warnings = []
        self._pending_sync_warnings.append((title, message))

    def _on_sync_cancelled(self, stats: Dict[str, Any]) -> None:
        """Handle sync cancelled — hide sync widget, refresh with partial data."""
        self._sync_widget.on_cancelled(stats)
        logger.info("Sync cancelled")
        self.status_bar.show_temporary_message(_("Sync cancelled"), 5000)
        self._load_games()

    def _on_games_batch(self, batch: list) -> None:
        """Handle progressive game loading during sync.

        Builds thin GameEntry objects from the batch and appends them to
        the UI. Dedup by UUID avoids duplicates from cross-store games.

        List view: extend _filtered_games, sort in place, reset via
        set_games() (preserves scroll position, maintains sort order).
        Grid views: append_games() (grid order matters less, model reset
        would re-trigger fade-in on all visible covers).

        The final _load_games() at sync end replaces everything.
        """
        if not batch:
            return

        new_games = []
        for game_dict in batch:
            game_id = game_dict.get("id", "")
            if not game_id:
                continue
            # Skip if already in progressive set or existing cache
            if game_id in self._progressive_ids:
                continue
            if game_id in self._games_by_id:
                continue

            self._progressive_ids.add(game_id)
            entry = GameEntry.from_dict(game_dict)
            new_games.append(entry)

        if not new_games:
            return

        # Add to all_games and filtered_games (basic filtering)
        passing_games = []
        for game in new_games:
            self._all_games.append(game)
            # Simple filter: skip hidden games in normal mode, apply store filter
            if game.get("is_hidden", False):
                continue
            passing_games.append(game)

        if not passing_games:
            return

        # Detect new stores for progressive filter bar update
        for game in passing_games:
            store = game.get("primary_store", "")
            if store and store not in self._progressive_stores and store not in self._store_index:
                self._progressive_stores.add(store)
                display = PluginManager.get_store_display_name(store)
                self.filter_bar.add_store(store, display)

        # List view: extend + sort + model reset (sorted insertion)
        # model._games IS _filtered_games (shared ref from set_games),
        # so extending _filtered_games here is safe — the set_games() call
        # rebuilds _filtered_indices correctly.
        self._filtered_games.extend(passing_games)
        self._sort_games()
        self.game_list.set_games(self._filtered_games)

        # Grid views: append (no model reset, preserves fade-in)
        self.content_area.append_games(passing_games)

        # Update status bar count immediately
        self.status_bar.set_game_count(len(self._filtered_games))

    def _on_rate_limit_status(self, message: str) -> None:
        """Show rate limit status on the status bar."""
        if message:
            self.status_bar.count_label.setText(message)
        else:
            count = f"{self.status_bar._game_count:,}"
            self.status_bar.count_label.setText(
                _("{count} games").format(count=count)
            )

    def _on_sync_error(self, error: str) -> None:
        """Handle sync error"""
        self._sync_widget.on_cancelled({})
        QMessageBox.critical(self, _("Sync Error"), _("Sync failed:\n{error}").format(error=error))
        self._load_games()

    # === Metadata-only sync ===

    def _update_store_sync_menu(self) -> None:
        """Populate the toolbar sync menu with explicitly enabled stores."""
        plugins_config = self.config.get("plugins", {})
        all_stores = self.game_service.get_store_info()

        sync_stores = [
            (name, display, auth)
            for name, display, auth in all_stores
            if plugins_config.get(name, {}).get("enabled", False)
        ]
        self.toolbar.update_sync_stores(sync_stores)

    def _update_metadata_sync_menu(self) -> None:
        """Populate the toolbar sync menu with active metadata plugins.

        Includes both enrichment plugins (IGDB, SteamGridDB, ...) and
        tag-sync plugins (Lutris, Heroic) — all enabled metadata plugins
        appear in the sync menu.
        """
        plugins_config = self.config.get("plugins", {})
        enrichment_names = PluginManager.get_enrichment_plugin_names()
        tag_sync_names = PluginManager.get_tag_sync_plugin_names()

        active = []
        for name in enrichment_names + tag_sync_names:
            cfg = plugins_config.get(name, {})
            if not cfg.get("enabled", False):
                continue
            plugin = self.plugin_manager.get_plugin(name)
            if plugin and plugin.is_available():
                display = PluginManager.get_store_display_name(name)
                active.append((name, display))

        self.toolbar.update_sync_metadata_plugins(active)

    def _on_sync_metadata_requested(self, plugin_name: str) -> None:
        """Handle sync request for a specific metadata plugin."""
        # Tag-sync plugins (Lutris, Heroic) are local-only — no online check
        tag_sync_names = PluginManager.get_tag_sync_plugin_names()
        if plugin_name in tag_sync_names:
            self._start_tag_sync(plugin_name)
        else:
            if not self._check_online_for_sync():
                return
            self._start_metadata_sync(plugin_name)

    def _start_metadata_sync(self, plugin_name: str) -> None:
        """Start a metadata-only sync for a single plugin.

        Builds enrichment jobs for the given plugin directly (no store
        fetch, no sentinel), then runs them via a normal SyncWorker.
        """
        if self._sync_worker and self._sync_worker.isRunning():
            QMessageBox.information(
                self,
                _("Sync In Progress"),
                _("A sync operation is already in progress.")
            )
            return

        # Build metadata jobs for ALL plugins, then filter
        all_jobs = self.game_service.build_metadata_jobs()
        jobs = [j for j in all_jobs if j.plugin_name == plugin_name]

        if not jobs:
            display = PluginManager.get_store_display_name(plugin_name)
            self.status_bar.show_temporary_message(
                _("{name}: nothing to enrich").format(name=display), 5000
            )
            return

        queue = SyncJobQueue()
        queue.add_jobs(jobs)

        self._sync_widget.start(queue)

        self._sync_worker = SyncWorker(
            self.game_service,
            queue,
            full_resync=False,
            parent=self,
        )

        self._sync_worker.phase_started.connect(self._sync_widget.on_phase_started)
        self._sync_worker.phase_progress.connect(self._sync_widget.on_phase_progress)
        self._sync_worker.phase_finished.connect(self._sync_widget.on_phase_finished)
        self._sync_worker.rate_limit.connect(self._on_rate_limit_status)
        self._sync_worker.rate_limit.connect(self._sync_widget.on_rate_limit)
        self._sync_worker.rate_limit_countdown.connect(self._sync_widget.on_rate_limit_countdown)
        self._sync_worker.cache_refresh_requested.connect(self._schedule_cache_refresh)

        self._sync_worker.games_batch_ready.connect(self._on_games_batch)

        self._sync_worker.sync_finished.connect(self._on_sync_finished)
        self._sync_worker.sync_error.connect(self._on_sync_error)
        self._sync_worker.cancelled.connect(self._on_sync_cancelled)
        self._sync_worker.sync_warning.connect(self._on_sync_warning)

        self._sync_widget.cancel_requested.connect(self._cancel_sync)
        self._sync_widget.skip_requested.connect(self._skip_sync_plugin)

        self._pending_sync_warnings = []
        self._sync_worker.start()

    def _start_tag_sync(self, plugin_name: str) -> None:
        """Start a tag-sync-only run for a single metadata plugin.

        Creates a tag_sync job and runs it via SyncWorker with progress.
        """
        if self._sync_worker and self._sync_worker.isRunning():
            QMessageBox.information(
                self,
                _("Sync In Progress"),
                _("A sync operation is already in progress.")
            )
            return

        job = SyncJob(
            phase=SyncPhase.METADATA,
            plugin_name=plugin_name,
            task_type="tag_sync",
        )

        queue = SyncJobQueue()
        queue.add_jobs([job])

        self._sync_widget.start(queue)

        self._sync_worker = SyncWorker(
            self.game_service,
            queue,
            full_resync=False,
            parent=self,
        )

        self._sync_worker.phase_started.connect(self._sync_widget.on_phase_started)
        self._sync_worker.phase_progress.connect(self._sync_widget.on_phase_progress)
        self._sync_worker.phase_finished.connect(self._sync_widget.on_phase_finished)
        self._sync_worker.rate_limit.connect(self._on_rate_limit_status)
        self._sync_worker.rate_limit.connect(self._sync_widget.on_rate_limit)
        self._sync_worker.rate_limit_countdown.connect(self._sync_widget.on_rate_limit_countdown)
        self._sync_worker.cache_refresh_requested.connect(self._schedule_cache_refresh)

        self._sync_worker.games_batch_ready.connect(self._on_games_batch)

        self._sync_worker.sync_finished.connect(self._on_sync_finished)
        self._sync_worker.sync_error.connect(self._on_sync_error)
        self._sync_worker.cancelled.connect(self._on_sync_cancelled)
        self._sync_worker.sync_warning.connect(self._on_sync_warning)

        self._sync_widget.cancel_requested.connect(self._cancel_sync)
        self._sync_widget.skip_requested.connect(self._skip_sync_plugin)

        self._pending_sync_warnings = []
        self._sync_worker.start()

    # === Settings ===

    def _on_settings_requested(self, open_tab: str = None) -> None:
        """Handle settings button click"""
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            dialog = SettingsDialog(
                config=self.config,
                plugin_manager=self.plugin_manager,
                game_service=self.game_service,
                theme_manager=self.theme_manager,
                parent=self,
                open_tab=open_tab,
                update_info=self._pending_update,
            )

            # Connect configure_plugin signal
            dialog.configure_plugin.connect(self._on_configure_plugin)

            # Connect tags_changed to refresh filter bar
            dialog.tags_changed.connect(self._on_tags_changed_in_settings)

            # Connect update request to close settings and show update dialog
            dialog.show_update_requested.connect(
                self._on_show_update_from_dialog,
                Qt.ConnectionType.QueuedConnection,
            )

            # Connect restart request from backup restore
            dialog.restart_required.connect(self._on_restart_after_restore)
        finally:
            QApplication.restoreOverrideCursor()

        result = dialog.exec_()
        if result:
            # Settings were saved, may need to refresh UI
            logger.info("Settings saved")

            # Apply theme changes immediately (zoom requires restart)
            if self.theme_manager:
                saved_theme = self.config.get("appearance.theme", "system")
                self.theme_manager.apply_theme(saved_theme)

                # Zoom is applied via QT_SCALE_FACTOR on restart
                # This just updates the tracked value
                saved_zoom = self.config.get("appearance.ui_zoom", 100)
                self.theme_manager.apply_zoom(saved_zoom)

            # Apply fade duration to grid view delegates
            fade_ms = self.config.get("appearance.image_fade_duration", DEFAULT_IMAGE_FADE_MS)
            self.content_area.cover_view.delegate.set_fade_duration(fade_ms)
            self.content_area.screenshot_view.delegate.set_fade_duration(fade_ms)

            # Apply cover scaling mode
            cover_scaling = self.config.get("appearance.cover_scaling", "none")
            self.content_area.cover_view.delegate.set_cover_scaling(cover_scaling)

            # Apply badge visibility to grid delegates
            show_modes = self.config.get("appearance.show_game_mode_badges", True)
            show_stores = self.config.get("appearance.show_store_badges", True)
            self.content_area.cover_view.delegate.set_badge_visibility(show_modes, show_stores)
            self.content_area.screenshot_view.delegate.set_badge_visibility(show_modes, show_stores)

            # Update default store on list view and grid delegates
            new_default_store = self.config.get("ui.default_store", "")
            self.content_area.list_view.set_default_store(new_default_store)
            self.content_area.cover_view.delegate.set_default_store(new_default_store)
            self.content_area.screenshot_view.delegate.set_default_store(new_default_store)

            # Refresh plugin data caches on delegates (picks up new/changed plugins)
            self.game_list.delegate.refresh_plugin_data()
            self.content_area.cover_view.delegate.refresh_plugin_data()
            self.content_area.screenshot_view.delegate.refresh_plugin_data()

            # Reload games if settings changed (invalidate cache to pick up
            # plugin enable/disable changes that affect store filtering)
            self.game_service.invalidate_cache()
            self._load_games()

            # Auto-sync re-enabled store plugins
            reenabled = dialog.plugins_tab.reenabled_stores
            if reenabled:
                logger.info(f"Auto-syncing re-enabled stores: {reenabled}")
                QTimer.singleShot(500, lambda: self._start_sync(stores=reenabled))

    def _on_backup_requested(self) -> None:
        """Handle backup & restore menu click — opens Settings on Backup tab"""
        self._on_settings_requested(open_tab="Backup")

    def _export_csv(self) -> None:
        """Export the currently filtered game list as CSV via export dialog."""
        if not self._filtered_games:
            QMessageBox.information(
                self, APP_NAME, _("No games to export.")
            )
            return

        from .dialogs.csv_export import CsvExportDialog

        dialog = CsvExportDialog(
            self, self._filtered_games, self.game_service, self.config
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            count = len(self._filtered_games)
            self.status_bar.show_temporary_message(
                _("Exported {count} games to CSV").format(count=count),
                10000,
            )

    def _on_dev_console_requested(self) -> None:
        """Open or raise the Developer Console dialog."""
        if self._dev_console is None:
            from .dialogs.developer_console import DeveloperConsoleDialog

            self._dev_console = DeveloperConsoleDialog(
                config=self.config,
                parent=self,
            )
        self._dev_console.show()
        self._dev_console.raise_()
        self._dev_console.activateWindow()

    def _on_theme_delegate_config_changed(self) -> None:
        """Push per-theme delegate visual config to grid view delegates."""
        if not self.theme_manager:
            return
        config = self.theme_manager.get_delegate_config()
        self.content_area.cover_view.delegate.set_delegate_config(config)
        self.content_area.screenshot_view.delegate.set_delegate_config(config)

        # Push per-theme colors to all view delegates
        fav_star = self.theme_manager.get_fav_star_color()
        self.content_area.cover_view.delegate.update_theme_colors(fav_star)
        self.content_area.screenshot_view.delegate.update_theme_colors(fav_star)
        self.game_list.delegate.update_theme_colors(fav_star)

        # Cache score colors for dialogs opened on demand
        self._score_colors = self.theme_manager.get_score_colors()

    def _on_about_requested(self) -> None:
        """Handle about button click"""
        dialog = AboutDialog(
            parent=self,
            plugin_manager=self.plugin_manager,
            update_info=self._pending_update,
        )
        dialog.show_update_requested.connect(
            self._on_show_update_from_dialog,
            Qt.ConnectionType.QueuedConnection,
        )
        dialog.exec_()

    def _on_random_game(self) -> None:
        """Pick a random game from the currently filtered list and select it."""
        if not self._filtered_games:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        game = random.choice(self._filtered_games)
        game_id = game.get("id", "")
        if not game_id:
            QApplication.restoreOverrideCursor()
            return
        self.game_list.select_game(game_id)
        self._on_game_selected(game_id)
        QApplication.restoreOverrideCursor()
        # Blink the selection 3 times to draw attention
        self._blink_selection(game_id, 3)

    def _blink_selection(self, game_id: str, times: int) -> None:
        """Blink the selection in game list and active grid view simultaneously."""
        from .game_list import GameRoles
        from .cover_view import CoverRoles
        from .screenshot_view import ScreenshotRoles

        # Build list of (list_view, index) pairs to blink
        targets = []

        # Always blink the game list
        for row in range(self.game_list.model.rowCount()):
            index = self.game_list.model.index(row, 0)
            if index.data(GameRoles.GameId) == game_id:
                targets.append((self.game_list.list_view, index))
                break

        # Also blink active grid view (cover or screenshot), not detail view
        mode = self.content_area.current_view_mode()
        if mode == VIEW_MODE_COVER:
            cv = self.content_area.cover_view
            for row in range(cv.model.rowCount()):
                index = cv.model.index(row, 0)
                if index.data(CoverRoles.GameId) == game_id:
                    targets.append((cv.list_view, index))
                    break
        elif mode == VIEW_MODE_SCREENSHOT:
            sv = self.content_area.screenshot_view
            for row in range(sv.model.rowCount()):
                index = sv.model.index(row, 0)
                if index.data(ScreenshotRoles.GameId) == game_id:
                    targets.append((sv.list_view, index))
                    break

        if not targets:
            return

        total_steps = times * 2  # on/off pairs
        step = [0]

        def _toggle():
            if step[0] >= total_steps:
                for lv, idx in targets:
                    lv.setCurrentIndex(idx)
                return
            if step[0] % 2 == 0:
                for lv, _ in targets:
                    lv.clearSelection()
            else:
                for lv, idx in targets:
                    lv.setCurrentIndex(idx)
            step[0] += 1
            QTimer.singleShot(120, _toggle)

        QTimer.singleShot(80, _toggle)

    def _on_platform_changed(self, game_id: str, platform_id: str) -> None:
        """Handle platform selection change for a game

        Args:
            game_id: Game UUID
            platform_id: Selected platform ID
        """
        logger.info(f"Platform changed for {game_id}: {platform_id}")
        # TODO: Persist platform selection to GameManager
        # self.game_manager.set_game_platform(game_id, platform_id)

    def _on_notes_changed(self, game_id: str, notes: str) -> None:
        """Handle notes changed for a game

        Args:
            game_id: Game UUID
            notes: Notes text
        """
        logger.info(f"Notes changed for {game_id}: {len(notes)} chars")
        # Save notes to game service
        self.game_service.set_game_notes(game_id, notes)

    def _on_settings_changed(self, game_id: str, config: dict) -> None:
        """Handle per-game launch settings changed.

        Args:
            game_id: Game UUID
            config: Launch configuration dict (empty to clear)
        """
        self.game_service.set_launch_config(game_id, config if config else None)

    def _show_setup_wizard_first_run(self) -> None:
        """Display first-run setup wizard - mandatory completion"""
        wizard = SetupWizard(
            config=self.config,
            plugin_manager=self.plugin_manager,
            theme_manager=self.theme_manager,
            parent=self,
            is_first_run=True
        )

        result = wizard.exec()

        if result == QWizard.DialogCode.Accepted:
            # Credentials, theme, first-run flag already saved by wizard.accept()

            # Enable/disable plugins based on selection
            self._apply_wizard_plugin_settings(wizard)

            # Apply selected theme (live — accept() only saves to config)
            selected_theme = wizard.get_selected_theme()
            if self.theme_manager:
                self.theme_manager.apply_theme(selected_theme)

            # Suppress news dialog on first run — user just saw the wizard
            self.config.set_last_news_version(APP_VERSION)

            # Now trigger initial game load (shows empty list)
            self._load_games()

            # Ask before triggering initial sync
            reply = QMessageBox.question(
                self,
                _("Initial Library Sync"),
                _("Would you like to sync your game libraries now?\n\n"
                  "This may take a while for the initial sync, especially "
                  "with large libraries."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                QTimer.singleShot(500, self._trigger_initial_sync)
        else:
            # User cancelled - exit immediately without triggering closeEvent
            # Using quit() avoids closeEvent which would save config
            QMessageBox.information(
                self,
                _("Setup Required"),
                _(
                    "{app_name} requires at least one game"
                    " store to be configured.\n\n"
                    "The setup wizard will appear again"
                    " next time you start the app."
                ).format(app_name=APP_NAME)
            )
            QApplication.quit()

    def _show_setup_wizard_manual(self) -> None:
        """Show setup wizard from Help menu (existing settings pre-filled)"""
        wizard = SetupWizard(
            config=self.config,
            plugin_manager=self.plugin_manager,
            theme_manager=self.theme_manager,
            parent=self,
            is_first_run=False,
            is_rerun=True
        )

        result = wizard.exec()

        if result == QWizard.DialogCode.Accepted:
            # Credentials, theme already saved by wizard.accept()

            # Enable/disable plugins based on selection
            self._apply_wizard_plugin_settings(wizard)

            # Apply selected theme (live — accept() only saves to config)
            selected_theme = wizard.get_selected_theme()
            if self.theme_manager:
                self.theme_manager.apply_theme(selected_theme)

            # Reload games with updated settings
            self._load_games()

    def _apply_wizard_plugin_settings(self, wizard) -> None:
        """Enable/disable plugins based on wizard selections.

        Only disables store plugins not selected in the wizard.
        Metadata plugins (IGDB, PCGamingWiki) and platform plugins
        (DOSBox, ScummVM, etc.) are enabled by default.
        """
        # Get enabled store IDs from wizard
        enabled_stores = wizard.store_page.get_enabled_stores()

        # Update plugin enabled states in config
        plugins_config = self.config.get("plugins", {})

        # Get ALL discovered plugins (stores + metadata + platform)
        all_plugins = self.plugin_manager.get_discovered_plugins()

        for plugin_name, metadata in all_plugins.items():
            if plugin_name not in plugins_config:
                plugins_config[plugin_name] = {}

            if "store" in metadata.plugin_types:
                # Store plugin: enable only if selected in wizard
                should_enable = plugin_name in enabled_stores
                plugins_config[plugin_name]["enabled"] = should_enable
                # Update in-memory state so Settings doesn't think
                # these were "newly disabled" and show a confirmation
                if not should_enable:
                    self.plugin_manager.disable_plugin(plugin_name)
            else:
                # Metadata/platform plugins: enable by default
                plugins_config[plugin_name]["enabled"] = True

        self.config.set("plugins", plugins_config)
        self.config.save()

    def _check_show_news(self) -> None:
        """Show About dialog with News tab if version changed since last view.

        Called once on startup after games are loaded. Shows the News tab
        automatically if this is the first run or the app version has changed
        since the user last viewed the news.
        """
        last_version = self.config.get_last_news_version()

        # Show if first run (empty) or version changed
        if not last_version or last_version != APP_VERSION:
            logger.info(f"Showing news dialog: last_version={last_version}, current={APP_VERSION}")

            dialog = AboutDialog(
                parent=self,
                plugin_manager=self.plugin_manager,
                update_info=self._pending_update,
            )
            dialog.show_update_requested.connect(
                self._on_show_update_from_dialog,
                Qt.ConnectionType.QueuedConnection,
            )
            dialog.select_news_tab()
            dialog.exec_()

            # Mark as seen
            self.config.set_last_news_version(APP_VERSION)

    def _check_for_updates(self) -> None:
        """Check for app updates in a background thread.

        Called on startup (3s delay) and after sync completion (2s delay).
        Respects opt-in setting and offline mode. Shows a notification if
        a new version is found.
        """
        if not self.config.get("app.check_for_updates", False):
            return

        import threading
        from ..core.update_checker import check_for_update

        def _worker():
            result = check_for_update(self.config)
            if result:
                self._pending_update = result
                # Must use QMetaObject.invokeMethod — Python threads are not
                # QThreads so Signal.emit() may dispatch as DirectConnection
                # (running the slot on this background thread where Qt dialog
                # creation silently fails).
                QMetaObject.invokeMethod(
                    self, "_on_update_available",
                    Qt.ConnectionType.QueuedConnection,
                )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    @Slot()
    def _on_update_available(self) -> None:
        """Show update notification dialog with optional pre-update backup."""
        if not self._pending_update:
            return
        logger.info("Showing update dialog for %s", self._pending_update.version)
        self._show_update_dialog(
            self._pending_update.version,
            self._pending_update.changelog,
        )

    def _show_update_dialog(self, new_version: str, changelog=None) -> None:
        """Show the full update dialog. Called from startup, Settings, and About."""
        from ..core.backup_manager import create_backup
        from .dialogs.update_dialog import UpdateDialog

        dlg = UpdateDialog(new_version, self.config, changelog=changelog, parent=self)
        dlg.exec()

        if dlg.action == "download":
            config_changed = False

            # Handle backup
            if dlg.should_backup:
                if dlg.path_changed:
                    self.config.set("backup.location", str(dlg.backup_path))
                    config_changed = True

                # Run backup with progress
                progress = QProgressDialog(_("Creating backup..."), None, 0, 0, self)
                progress.setWindowTitle(_("Backup in Progress"))
                progress.setWindowModality(Qt.WindowModality.ApplicationModal)
                progress.setMinimumDuration(0)
                progress.setValue(0)
                progress.show()
                QApplication.processEvents()

                success, msg, _assets = create_backup(self.config)
                progress.close()

                if success:
                    logger.info(f"Pre-update backup completed: {msg}")
                else:
                    logger.error(f"Pre-update backup failed: {msg}")
                    QMessageBox.warning(
                        self,
                        _("Backup Failed"),
                        _("Failed to create backup:\n\n{msg}\n\n"
                          "You can still proceed with the download.").format(msg=msg),
                    )

            # Handle schedule opt-in
            if dlg.enable_schedule:
                self.config.set("backup.schedule_enabled", True)
                self.config.set("backup.check_on_startup", True)
                self.config.set("backup.interval_days", 1)
                config_changed = True

            if config_changed:
                self.config.save()

            open_url(APP_RELEASES_URL)

        elif dlg.action == "dismiss_version":
            self.config.set("app.update_dismissed_version", new_version)
            self.config.save()

    @Slot()
    def _on_show_update_from_dialog(self) -> None:
        """Handle 'show update dialog' request from About or Settings."""
        if self._pending_update:
            logger.debug("Showing update dialog from About/Settings for %s",
                         self._pending_update.version)
            self._show_update_dialog(
                self._pending_update.version,
                self._pending_update.changelog,
            )
        else:
            logger.warning("show_update_requested received but _pending_update is None")

    def _create_plugin_config_dialog(self, plugin_name: str):
        """Load custom config dialog from plugin, or fall back to generic.

        Checks plugin.json for ``config_dialog_class`` and dynamically
        loads it via ``PluginManager.load_plugin_class()``.  Falls back
        to the generic ``PluginConfigDialog`` on any error.
        """
        loaded = self.plugin_manager.get_loaded_plugin(plugin_name)
        if loaded and loaded.metadata.config_dialog_class:
            try:
                dialog_cls = self.plugin_manager.load_plugin_class(
                    plugin_name, loaded.metadata.config_dialog_class,
                )
                return dialog_cls(
                    config=self.config,
                    plugin_manager=self.plugin_manager,
                    parent=self,
                )
            except Exception as e:
                logger.warning(
                    "Failed to load config dialog for %s: %s", plugin_name, e,
                )

        # Fallback: generic config dialog
        return PluginConfigDialog(
            plugin_name=plugin_name,
            config=self.config,
            plugin_manager=self.plugin_manager,
            credentials=self.plugin_manager.credential_manager,
            parent=self,
        )

    def _on_configure_plugin(self, plugin_name: str) -> None:
        """Handle plugin configure request

        Args:
            plugin_name: Name of plugin to configure
        """
        dialog = self._create_plugin_config_dialog(plugin_name)

        # Connect signal to update settings dialog if open
        dialog.connection_status_changed.connect(self._on_plugin_connection_changed)

        # Connect store data reset signal if available
        if hasattr(dialog, 'store_data_reset'):
            dialog.store_data_reset.connect(lambda _: self._load_games())

        result = dialog.exec()

        # Clean up dialog resources (especially important after OAuth/WebEngine use)
        dialog.deleteLater()

        if result:
            logger.info(f"Plugin {plugin_name} configured")
            # Reload games in case plugin configuration affected available games
            self._load_games()

    def _on_plugin_connection_changed(self, plugin_name: str, is_authenticated: bool) -> None:
        """Handle plugin connection status change

        Updates the settings dialog's plugin list if it's open and
        refreshes the sync menu to reflect the new state.
        """
        # Find any open SettingsDialog and update its plugin status
        for child in self.findChildren(SettingsDialog):
            child.update_plugin_status(plugin_name, is_authenticated)

        # Refresh sync menu so newly enabled/authenticated plugins appear
        self._update_store_sync_menu()
        self._update_metadata_sync_menu()

        logger.debug(f"Plugin {plugin_name} connection status: {is_authenticated}")

    def _refresh_filter_bar_tags(self) -> None:
        """Refresh filter bar with current tags and quick-access tags."""
        all_tags = self.game_service.get_all_tags()
        self.filter_bar.set_available_tags(all_tags)
        quick_tags = self.game_service.get_quick_access_tags(
            self.config.get("appearance.quick_tag_count", 5)
        )
        self.filter_bar.set_quick_access_tags(quick_tags)
        # Apply source color setting
        self.filter_bar.set_source_colors_enabled(
            self.config.get("tags.source_colors", False)
        )

    def _on_tags_changed_in_settings(self) -> None:
        """Handle tags changed in Settings Tag Manager

        Refreshes the filter bar with updated tags and rebuilds tag index
        (tag names/colors may have changed via rename/delete).
        """
        self._rebuild_tag_index()
        self._refresh_filter_bar_tags()
        logger.debug("Rebuilt tag index and refreshed filter bar after settings change")

    def _on_open_tag_manager(self) -> None:
        """Open standalone Tag Manager dialog (from Tools menu or filter bar)."""
        from .dialogs.tag_manager_dialog import TagManagerDialog

        dialog = TagManagerDialog(
            game_service=self.game_service,
            parent=self,
            score_colors=self._score_colors,
        )
        dialog.tags_changed.connect(self._on_tags_changed_in_settings)
        dialog.exec()

        # Refresh filter bar tags after dialog closes
        self._refresh_filter_bar_tags()

    def _on_add_tag_from_filter_bar(self) -> None:
        """Handle add tag button from filter bar - opens Tag Manager dialog."""
        self._on_open_tag_manager()

    # === Family Member Name Resolution ===

    def _resolve_family_member_name(self, steamid: str) -> str:
        """Resolve a Steam family member steamid to their display name.

        Args:
            steamid: Steam64 ID of the family member

        Returns:
            Display name if found, otherwise the steamid
        """
        try:
            steam_plugin = self.plugin_manager.get_plugin("steam")
            if steam_plugin and hasattr(steam_plugin, 'get_family_member_name'):
                return steam_plugin.get_family_member_name(steamid)
        except Exception as e:
            logger.debug(f"Failed to resolve family member name: {e}")
        return steamid

    def _get_store_url(self, store_name: str, app_id: str) -> str:
        """Get store page URL from plugin.

        Args:
            store_name: Store name (steam, gog, epic)
            app_id: Store-specific app ID

        Returns:
            Store page URL or empty string if unavailable
        """
        try:
            plugin = self.plugin_manager.get_plugin(store_name)
            if plugin and hasattr(plugin, 'get_store_page_url'):
                return plugin.get_store_page_url(app_id)
        except Exception as e:
            logger.debug(f"Failed to get store URL for {store_name}/{app_id}: {e}")
        return ""

    # === Description Refresh ===

    def _on_description_refresh_requested(
        self, game_uuid: str,
        store_app_id: str, store_name: str,
    ) -> None:
        """Handle request to refresh description from API.

        Called when a plain text description is detected (e.g., from Kaggle dataset).
        Only tries stores that are in the user's description priority list.
        Non-priority stores are NEVER used — the priority order is mandatory.

        Args:
            game_uuid: Internal game UUID (for cache update)
            store_app_id: Store-specific app ID (e.g., Steam app ID)
            store_name: Store name (e.g., 'steam')
        """
        logger.debug(
            f"Description refresh requested: "
            f"game_uuid={game_uuid}, "
            f"store_app_id={store_app_id}, "
            f"store={store_name}"
        )

        # Look up game data for all store_app_ids
        game_data = next((g for g in self._all_games if g.get("id") == game_uuid), None)
        if not game_data:
            return

        all_store_app_ids = game_data.get("store_app_ids", {})
        desc_priority = self.game_service._resolver.get_field_priority("description")

        # ONLY try stores that are in the priority list AND have an app_id.
        # Plugin DBs are license-agnostic caches, but store plugins need an
        # app_id for lookup. Use cross-ref resolution for stores without a
        # direct app_id. Never fall back to non-priority stores.
        stores_to_try = []
        normalized_title = game_data.get("normalized_title", "")
        resolver = self.game_service._resolver
        for pstore in desc_priority:
            aid = all_store_app_ids.get(pstore)
            if not aid and normalized_title:
                aid = resolver._resolve_cross_store_app_id(
                    pstore, all_store_app_ids, normalized_title
                )
            if aid:
                stores_to_try.append((pstore, aid))

        if not stores_to_try:
            logger.debug(f"No priority stores available for description refresh of {game_uuid}")
            return

        import threading

        def fetch_description():
            from luducat.plugins.base import RateLimitError

            for try_store, try_app_id in stores_to_try:
                try:
                    store = self.plugin_manager.get_plugin(try_store)
                    if not store or not hasattr(store, 'refresh_game_description'):
                        continue
                    logger.debug(f"Trying {try_store}.refresh_game_description({try_app_id})")
                    html_description = store.refresh_game_description(try_app_id)
                    if html_description:
                        n = len(html_description)
                        logger.debug(
                            f"Got HTML description from"
                            f" {try_store}: {n} chars"
                        )
                        self._description_fetched.emit(game_uuid, html_description)
                        return
                except RateLimitError as e:
                    wait_min = e.wait_seconds // 60
                    self._api_rate_limited.emit(
                        f"{try_store} rate limited: pausing {wait_min} min"
                    )
                    return
                except Exception as e:
                    logger.warning(
                        f"Failed to refresh description"
                        f" from {try_store}/{try_app_id}"
                        f": {e}"
                    )
                    continue
            logger.debug(f"No priority store could provide HTML description for {game_uuid}")

        thread = threading.Thread(target=fetch_description, daemon=True)
        thread.start()

    def _update_game_description(self, game_id: str, description: str) -> None:
        """Update game description in UI and database after API refresh.

        Args:
            game_id: Game ID (UUID)
            description: New HTML description
        """
        logger.debug(f"_update_game_description called for {game_id}, {len(description)} chars")

        # Update the main database
        result = self.game_service.update_game_description(game_id, description)
        logger.debug(f"Database update result: {result}")

        # Update content area (description cache already updated by game_service)
        self.content_area.update_description(game_id, description)
        logger.debug(f"UI update complete for {game_id}")

    # === Context Menu ===

    def _on_context_menu_requested(self, game_data: dict, global_pos) -> None:
        """Handle context menu request from any view.

        Args:
            game_data: Game data dict
            global_pos: Global position for popup
        """
        from .context_menu import GameContextMenu

        default_store = self.config.get("ui.default_store", "")

        menu = GameContextMenu(self)
        active_filters = self.filter_bar.get_active_filters()
        view_mode = self.content_area.current_view_mode()

        # Look up SGDB cover author if applicable
        sgdb_cover_author, sgdb_author_steam_id = self._lookup_cover_author(game_data)

        menu.build(game_data, default_store, active_filters, view_mode,
                   sgdb_cover_author=sgdb_cover_author,
                   sgdb_author_steam_id=sgdb_author_steam_id)

        # Connect signals to handlers
        menu.play_requested.connect(self._on_game_launched)
        menu.favorite_toggled.connect(self._on_favorite_toggled)
        menu.hidden_toggled.connect(self._on_hidden_toggled)
        menu.nsfw_override_changed.connect(self._on_nsfw_override_changed)
        menu.edit_tags_requested.connect(self._on_edit_tags_requested)
        menu.filter_game_modes_requested.connect(self._on_filter_game_modes_from_context)
        menu.filter_developers_requested.connect(self._on_filter_developers_from_context)
        menu.filter_publishers_requested.connect(self._on_filter_publishers_from_context)
        menu.filter_genres_requested.connect(self._on_filter_genres_from_context)
        menu.filter_year_requested.connect(self._on_filter_year_from_context)
        menu.view_screenshots_requested.connect(
            lambda gid: self._on_view_screenshots_requested(gid, 0)
        )
        menu.open_store_page_requested.connect(self._on_open_store_page)
        menu.force_rescan_requested.connect(self._on_force_rescan_requested)
        menu.switch_to_notes_requested.connect(self._on_switch_to_notes)
        menu.switch_to_properties_requested.connect(self._on_switch_to_properties)
        menu.cover_author_score_requested.connect(self._on_cover_author_score)

        menu.exec(global_pos)

    def _on_filter_game_modes_from_context(self, modes: list) -> None:
        """Toggle game mode filter from context menu.

        Empty list means clear the filter; non-empty means set it.
        """
        self.filter_bar.set_game_mode_filters(modes)

    def _on_filter_developers_from_context(self, developers: list) -> None:
        """Toggle developer filter. If the value is already the active filter, clear it."""
        if developers:
            current = set(self.filter_bar.get_active_filters().get("developers", []))
            if current == set(developers):
                developers = []
        self.filter_bar.set_developer_filters(developers)

    def _on_filter_publishers_from_context(self, publishers: list) -> None:
        """Toggle publisher filter. If the value is already the active filter, clear it."""
        if publishers:
            current = set(self.filter_bar.get_active_filters().get("publishers", []))
            if current == set(publishers):
                publishers = []
        self.filter_bar.set_publisher_filters(publishers)

    def _on_filter_genres_from_context(self, genres: list) -> None:
        """Toggle genre filter. If the value is already the active filter, clear it."""
        if genres:
            current = set(self.filter_bar.get_active_filters().get("genres", []))
            if current == set(genres):
                genres = []
        self.filter_bar.set_genre_filters(genres)

    def _on_filter_tags_from_detail(self, tags: list) -> None:
        """Toggle tag filter from detail view tag chip click."""
        if tags:
            current = set(self.filter_bar.get_active_filters().get("tags", []))
            if current == set(tags):
                tags = []
        self.filter_bar.set_tag_filters(tags)

    def _on_filter_year_from_context(self, years: list) -> None:
        """Toggle year filter. If the value is already the active filter, clear it."""
        if years:
            current = set(self.filter_bar.get_active_filters().get("years", []))
            if current == set(years):
                years = []
        self.filter_bar.set_year_filters(years)

    def _lookup_cover_author(self, game_data: dict) -> tuple:
        """Look up the author of a game's cover image via metadata plugins.

        Iterates enabled metadata plugins and asks each whether it can
        attribute the cover URL.  Short-circuits on first match.
        Independent of the (currently broken) cover_source cache field.

        Returns (author_name, steam_id) or ("", "").
        """
        cover_url = game_data.get("cover_image", "")
        if not cover_url:
            return ("", "")
        try:
            for _name, plugin in self.plugin_manager.get_metadata_plugins().items():
                attr = plugin.get_asset_attribution(cover_url)
                if attr and attr.get("author"):
                    return (attr["author"], attr.get("steam_id", ""))
            return ("", "")
        except Exception:
            logger.debug("Failed to look up cover author", exc_info=True)
            return ("", "")

    def _on_cover_author_score(
        self, game_id: str, author_name: str, score_delta: int
    ) -> None:
        """Handle author block/boost from context menu.

        Iterates metadata plugins to find which one claims the cover URL,
        then adjusts the author score on that plugin.  Independent of the
        (currently broken) cover_source cache field.
        """
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            game = self.game_service.get_game(game_id)
            cover_url = game.get("cover_image", "") if game else ""
            if not cover_url:
                return

            # Find which plugin claims this asset
            target_plugin = None
            target_name = ""
            for name, plugin in self.plugin_manager.get_metadata_plugins().items():
                if plugin.get_asset_attribution(cover_url):
                    target_plugin = plugin
                    target_name = name
                    break

            if not target_plugin:
                return

            if target_plugin.adjust_author_score(author_name, score_delta):
                self.plugin_manager.persist_plugin_settings(target_name)
                logger.info(
                    "Cover author '%s' score adjusted by %+d (plugin: %s)",
                    author_name, score_delta, target_name,
                )

            # Re-evaluate cached assets locally (no API calls)
            modified_map = self.game_service.reselect_media_from_plugin(
                target_name, target_plugin
            )

            # Purge blocked author images from disk + memory cache
            if hasattr(target_plugin, 'get_blocked_author_asset_urls'):
                purge_urls = target_plugin.get_blocked_author_asset_urls()
                if purge_urls:
                    from luducat.utils.image_cache import get_cover_cache, get_hero_cache
                    covers = get_cover_cache().remove_urls(purge_urls)
                    heroes = get_hero_cache().remove_urls(purge_urls)
                    logger.info(
                        "Purged %d cover + %d hero files from blocked authors",
                        covers, heroes,
                    )

            logger.info(
                "Re-selected %d covers/heroes after author score change",
                len(modified_map),
            )
            # Targeted view refresh — only update changed games, skip full
            # cache rebuild (_load_games takes ~10s on 15k libraries).
            if modified_map:
                self.content_area.update_game_covers(modified_map)
        except Exception:
            logger.error("Failed to update cover author score", exc_info=True)
        finally:
            QApplication.restoreOverrideCursor()

    def _on_open_store_page(self, game_id: str) -> None:
        """Open store page for game in default browser."""
        game = self.game_service.get_game(game_id)
        if not game:
            return

        default_store = self.config.get("ui.default_store", "")
        store_app_ids = game.get("store_app_ids", {})

        # Use default store if available, otherwise first available
        store_name = default_store if default_store in store_app_ids else None
        if not store_name:
            stores = game.get("stores", [])
            store_name = stores[0] if stores else None

        if store_name and store_name in store_app_ids:
            app_id = store_app_ids[store_name]
            url = self._get_store_url(store_name, app_id)
            if url:
                open_url(url)

    def _on_force_rescan_requested(self, game_id: str) -> None:
        """Handle force rescan request from context menu."""
        game = self.game_service.get_game(game_id)
        title = game.get("title", _("this game")) if game else _("this game")

        reply = QMessageBox.question(
            self,
            _("Force Rescan"),
            _("Re-scan metadata for \"{title}\" from all sources?\n\n"
              "This will clear cached matches and fetch fresh data.").format(title=title),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        import asyncio
        import threading

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        def run_rescan():
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    self.game_service.force_rescan_game(game_id)
                )
                if result:
                    logger.info(f"Force rescan completed for {game_id}")
                else:
                    logger.warning(f"Force rescan returned no data for {game_id}")
            except Exception as e:
                logger.error(f"Force rescan failed: {e}")
            finally:
                loop.close()

            # Post back to main thread (QTimer.singleShot from a non-Qt thread
            # creates the timer in this thread which has no event loop — use
            # QMetaObject.invokeMethod with QueuedConnection instead)
            QMetaObject.invokeMethod(
                self, "_force_rescan_finished",
                Qt.ConnectionType.QueuedConnection,
            )

        thread = threading.Thread(target=run_rescan, daemon=True)
        thread.start()

    @Slot()
    def _force_rescan_finished(self) -> None:
        """Restore cursor and reload games after force rescan (called via QueuedConnection)."""
        QApplication.restoreOverrideCursor()
        # Save scroll position before reload — _load_games() triggers
        # selection restore which scrollTo()'s the previously-selected game
        scroll_pos = self.content_area.get_grid_scroll_position()
        self._load_games()
        # Restore scroll so grid stays where the user was browsing
        self.content_area.restore_grid_scroll_position(scroll_pos)

    def _on_switch_to_notes(self, game_id: str) -> None:
        """Switch to list view Notes tab for game."""
        # Select the game first
        self._on_game_selected(game_id)
        # Switch to list view mode
        self._on_view_mode_changed(VIEW_MODE_LIST)
        # Switch to Notes tab (index 4)
        self.content_area.set_active_tab(4)

    def _on_switch_to_properties(self, game_id: str) -> None:
        """Switch to list view Settings tab for game."""
        self._on_game_selected(game_id)
        self._on_view_mode_changed(VIEW_MODE_LIST)
        # Switch to Settings tab (index 1)
        self.content_area.set_active_tab(1)
