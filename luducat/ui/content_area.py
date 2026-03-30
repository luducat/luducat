# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# content_area.py

"""Content area for luducat

Stacked widget containing view modes:
- Detail View: Detail panel for selected game with tabbed interface
- Cover View: Grid of game covers (2:3 portrait)
- Screenshot View: Grid of game screenshots (16:9 landscape)
"""

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Signal, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QStackedWidget,
    QVBoxLayout,
)

from ..core.constants import (
    VIEW_MODE_LIST,
    VIEW_MODE_COVER,
    VIEW_MODE_SCREENSHOT,
)

from .list_view import ListView
from .cover_view import CoverView
from .screenshot_view import ScreenshotView

logger = logging.getLogger(__name__)


class ContentArea(QWidget):
    """Content area with stacked views

    Signals:
        game_launched: Emitted when game launch is requested
        game_selected: Emitted when game is selected in grid views (to sync game list)
        description_refresh_requested: Emitted when plain text description needs HTML refresh
        favorite_toggled: Emitted when favorite status is toggled
        hidden_toggled: Emitted when hidden status is toggled
        edit_tags_requested: Emitted when user wants to edit tags for a game
        view_screenshots_requested: Emitted when user wants to view screenshots fullscreen
        view_cover_requested: Emitted when user wants to view cover fullscreen
        platform_changed: Emitted when platform selection changes in Settings tab
        notes_changed: Emitted when user saves notes in Notes tab
    """

    game_launched = Signal(str, str)  # game_id, store_name
    game_launched_via_runner = Signal(str, str, str)  # game_id, store_name, runner_name
    game_install_requested = Signal(str, str)  # game_id, store_name
    game_selected = Signal(str)  # game_id - for syncing game list selection
    view_mode_changed = Signal(str)  # mode - emitted when view mode changes programmatically
    description_refresh_requested = Signal(str, str, str)  # game_uuid, store_app_id, store_name
    favorite_toggled = Signal(str, bool)  # game_id, is_favorite
    hidden_toggled = Signal(str, bool)  # game_id, is_hidden
    edit_tags_requested = Signal(str)  # game_id
    view_screenshots_requested = Signal(str, int)  # game_id, screenshot_index
    view_cover_requested = Signal(str)  # game_id
    platform_changed = Signal(str, str)  # game_id, platform_id
    notes_changed = Signal(str, str)  # game_id, notes_text
    settings_changed = Signal(str, dict)  # game_id, launch_config_dict
    context_menu_requested = Signal(object, object)  # game_data, global_pos
    filter_developer_requested = Signal(list)   # [developer_name]
    filter_publisher_requested = Signal(list)   # [publisher_name]
    filter_genre_requested = Signal(list)       # [genre_name]
    filter_tag_requested = Signal(list)         # [tag_name]
    filter_year_requested = Signal(list)        # [year_string]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("contentArea")

        self._current_mode = VIEW_MODE_LIST
        self._current_game_id: Optional[str] = None
        self._games: Dict[str, Dict[str, Any]] = {}

        # Track which views need updating (lazy loading)
        self._cover_view_stale = True
        self._screenshot_view_stale = True
        self._detail_fields_callback = None
        self._play_sessions_callback = None

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        """Create content area layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Stacked widget for view modes
        self.stack = QStackedWidget()

        # Detail view (detail panel with tabs)
        self.list_view = ListView()
        self.stack.addWidget(self.list_view)

        # Cover view (grid)
        self.cover_view = CoverView()
        self.stack.addWidget(self.cover_view)

        # Screenshot view (grid)
        self.screenshot_view = ScreenshotView()
        self.stack.addWidget(self.screenshot_view)

        layout.addWidget(self.stack)

    def _connect_signals(self) -> None:
        """Connect view signals"""
        self.list_view.game_launched.connect(self.game_launched.emit)
        self.list_view.game_launched_via_runner.connect(self.game_launched_via_runner.emit)
        self.list_view.game_install_requested.connect(self.game_install_requested.emit)
        self.list_view.description_refresh_requested.connect(
            lambda uuid, app_id, store: self.description_refresh_requested.emit(uuid, app_id, store)
        )
        self.list_view.favorite_toggled.connect(self.favorite_toggled.emit)
        self.list_view.hidden_toggled.connect(self.hidden_toggled.emit)
        self.list_view.edit_tags_requested.connect(self.edit_tags_requested.emit)
        self.list_view.view_screenshots_requested.connect(self.view_screenshots_requested.emit)
        self.list_view.platform_changed.connect(self.platform_changed.emit)
        self.list_view.notes_changed.connect(self.notes_changed.emit)
        self.list_view.settings_changed.connect(self.settings_changed.emit)
        self.list_view.filter_developer_requested.connect(self.filter_developer_requested.emit)
        self.list_view.filter_publisher_requested.connect(self.filter_publisher_requested.emit)
        self.list_view.filter_genre_requested.connect(self.filter_genre_requested.emit)
        self.list_view.filter_tag_requested.connect(self.filter_tag_requested.emit)
        self.list_view.filter_year_requested.connect(self.filter_year_requested.emit)
        self.cover_view.game_launched.connect(self.game_launched.emit)
        self.cover_view.game_selected.connect(self._on_grid_game_selected)
        self.cover_view.view_cover_requested.connect(self._on_cover_view_clicked)
        self.screenshot_view.game_selected.connect(self._on_grid_game_selected)
        self.screenshot_view.view_screenshot_requested.connect(self._on_screenshot_view_clicked)
        self.cover_view.context_menu_requested.connect(self.context_menu_requested.emit)
        self.screenshot_view.context_menu_requested.connect(self.context_menu_requested.emit)

    def set_screenshot_callback(self, callback) -> None:
        """Set callback for lazy-loading screenshots.

        Args:
            callback: Function(game_id: str) -> List[str]
        """
        self.list_view.set_screenshot_callback(callback)
        self.screenshot_view.set_screenshot_callback(callback)

    def set_screenshot_invalidate_callback(self, callback) -> None:
        """Set callback for retrying screenshots after 404.

        Args:
            callback: Function(game_id: str, failed_urls: List[str]) -> List[str]
        """
        self.screenshot_view.set_screenshot_invalidate_callback(callback)

    def set_description_callback(self, callback) -> None:
        """Set callback for lazy-loading descriptions.

        Args:
            callback: Function(game_id: str) -> str
        """
        self.list_view.set_description_callback(callback)

    def set_borrowed_from_callback(self, callback) -> None:
        """Set callback for resolving family member steamid to name.

        Args:
            callback: Function(steamid: str) -> str
        """
        self.list_view.set_borrowed_from_callback(callback)

    def set_ensure_metadata_callback(self, callback) -> None:
        """Set callback for ensuring metadata is complete.

        The callback is called when displaying game details to fill in any
        missing metadata fields (cover, description, etc.) from fallback sources.

        Args:
            callback: Function(game_id: str) -> Dict[str, Any]
        """
        self.list_view.set_ensure_metadata_callback(callback)

    def set_cover_callback(self, callback) -> None:
        """Set callback for lazy-loading covers.

        The callback is called when cover view encounters a game without a cover,
        allowing fallback to IGDB or other metadata sources.

        Args:
            callback: Function(game_id: str) -> str (cover URL)
        """
        self.cover_view.set_cover_callback(callback)

    def set_store_url_callback(self, callback) -> None:
        """Set callback for getting store page URLs.

        Args:
            callback: Function(store_name: str, app_id: str) -> str
        """
        self.list_view.set_store_url_callback(callback)

    def set_detail_fields_callback(self, callback) -> None:
        """Set callback for lazy-loading detail fields.

        The callback is called when a game is selected to merge detail fields
        (metadata panel data) that are not stored in the main games cache.

        Args:
            callback: Function(game_id: str) -> Dict[str, Any]
        """
        self._detail_fields_callback = callback
        self.list_view.set_detail_fields_callback(callback)

    def set_play_sessions_callback(self, callback) -> None:
        """Set callback for loading play session data.

        Args:
            callback: Function(game_id: str) -> List[Dict]
        """
        self._play_sessions_callback = callback

    def set_runner_query(self, callback) -> None:
        """Set callback for querying runner availability per store."""
        self.list_view.set_runner_query(callback)

    def _on_grid_game_selected(self, game_id: str) -> None:
        """Handle game selection in grid views (cover and screenshot)"""
        logger.info(f"_on_grid_game_selected: game_id={game_id}")
        self._current_game_id = game_id
        # Emit signal to sync game list selection immediately
        self.game_selected.emit(game_id)
        logger.info("_on_grid_game_selected: game_selected emitted")
        # Defer list_view update to avoid interfering with double-click detection
        # The set_game() call can trigger heavy initialization (QWebEngineView)
        # which blocks the event loop and causes double-clicks to be lost
        if game_id in self._games:
            game_data = self._games[game_id]
            QTimer.singleShot(0, lambda: self._deferred_set_game(game_id, game_data))

    def _deferred_set_game(self, game_id: str, game_data: Dict[str, Any]) -> None:
        """Deferred game update for list view"""
        # Only update if this game is still selected
        if self._current_game_id == game_id:
            logger.info(f"_deferred_set_game: Updating list_view for {game_id}")
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(game_id)
                if detail:
                    game_data = {**game_data, **detail}
            self.list_view.set_game(game_data)

    def _on_screenshot_view_clicked(self, game_id: str) -> None:
        """Handle click in screenshot grid view - opens fullscreen viewer"""
        logger.info(f"_on_screenshot_view_clicked: game_id={game_id}")
        # First, select the game to load its screenshots into list view
        self._current_game_id = game_id
        if game_id in self._games:
            logger.info("_on_screenshot_view_clicked: Setting game on list_view")
            game = self._games[game_id]
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(game_id)
                if detail:
                    game = {**game, **detail}
            self.list_view.set_game(game)
            logger.info("_on_screenshot_view_clicked: list_view.set_game completed")
        # Then emit signal to open viewer at index 0
        logger.info("_on_screenshot_view_clicked: Emitting view_screenshots_requested")
        self.view_screenshots_requested.emit(game_id, 0)
        logger.info("_on_screenshot_view_clicked: Signal emitted")

    def _on_cover_view_clicked(self, game_id: str) -> None:
        """Handle click in cover grid view - opens fullscreen viewer"""
        self._current_game_id = game_id
        if game_id in self._games:
            game = self._games[game_id]
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(game_id)
                if detail:
                    game = {**game, **detail}
            self.list_view.set_game(game)
        # Emit signal to open cover viewer
        self.view_cover_requested.emit(game_id)

    def set_view_mode(self, mode: str) -> None:
        """Set current view mode

        Args:
            mode: View mode (list, cover, screenshot, downloads)
        """
        self._current_mode = mode

        if mode == VIEW_MODE_LIST:
            self.stack.setCurrentWidget(self.list_view)
        elif mode == VIEW_MODE_COVER:
            self.stack.setCurrentWidget(self.cover_view)
            # Update cover view if stale (deferred loading)
            if self._cover_view_stale and self._games:
                logger.debug("Updating stale cover view...")
                games_list = list(self._games.values())
                self.cover_view.set_games(games_list)
                self._cover_view_stale = False
        elif mode == VIEW_MODE_SCREENSHOT:
            self.stack.setCurrentWidget(self.screenshot_view)
            # Update screenshot view if stale (deferred loading)
            if self._screenshot_view_stale and self._games:
                logger.debug("Updating stale screenshot view...")
                games_list = list(self._games.values())
                self.screenshot_view.set_games(games_list)
                self._screenshot_view_stale = False
        logger.debug(f"View mode set to: {mode}")
        # Emit signal so toolbar can sync
        self.view_mode_changed.emit(mode)

    def current_view_mode(self) -> str:
        """Get current view mode"""
        return self._current_mode

    def show_game(self, game_id: str) -> None:
        """Show details for a game

        Args:
            game_id: Game ID to display
        """
        if game_id == self._current_game_id:
            return  # Already showing — prevents duplicate from feedback loop
        self._current_game_id = game_id

        if game_id in self._games:
            game = self._games[game_id]
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(game_id)
                if detail:
                    game = {**game, **detail}
            self.list_view.set_game(game)
            # Load play session breakdown for Stats tab
            if self._play_sessions_callback:
                try:
                    sessions = self._play_sessions_callback(game_id)
                    self.list_view.set_play_sessions_data(sessions)
                except Exception:
                    pass
            # Only select in grid views if they're not stale
            if not self._cover_view_stale:
                self.cover_view.select_game(game_id)
            if not self._screenshot_view_stale:
                self.screenshot_view.select_game(game_id)

    def append_games(self, games: List[Dict[str, Any]]) -> None:
        """Append games incrementally during progressive loading.

        Only appends to the currently visible grid view.
        """
        if not games:
            return
        # Update internal dict for game lookups
        for game in games:
            gid = game.get("id", "")
            if gid:
                self._games[gid] = game
        # Append to currently visible grid view
        if self._current_mode == VIEW_MODE_COVER:
            self.cover_view.append_games(games)
            self._cover_view_stale = False
        elif self._current_mode == VIEW_MODE_SCREENSHOT:
            self.screenshot_view.append_games(games)
            self._screenshot_view_stale = False

    def update_game_covers(self, modified: Dict[str, Dict[str, str]]) -> None:
        """Update covers/heroes for specific games without full view rebuild.

        Args:
            modified: Dict mapping game_id to changed fields,
                      e.g. ``{"uuid": {"cover": "https://..."}}``.
        """
        # Update the content_area's own game dict references
        for game_id, updates in modified.items():
            if game_id in self._games:
                game = self._games[game_id]
                if "cover" in updates:
                    game["cover_image"] = updates["cover"]

        # Targeted update on grid views (emit dataChanged, no model reset)
        self.cover_view.update_game_covers(modified)
        self.screenshot_view.update_game_covers(modified)

    def set_games(self, games: Dict[str, Dict[str, Any]]) -> None:
        """Set all games data

        Only updates the currently visible view. Grid views are updated
        lazily when switched to (deferred loading for performance).

        Args:
            games: Dict mapping game_id to game data
        """
        self._current_game_id = None  # Force re-show after data refresh
        self._games = games

        # Mark grid views as stale - they'll be updated when visible
        self._cover_view_stale = True
        self._screenshot_view_stale = True

        # Only update the currently visible view
        if self._current_mode == VIEW_MODE_COVER:
            games_list = list(games.values())
            self.cover_view.set_games(games_list)
            self._cover_view_stale = False
        elif self._current_mode == VIEW_MODE_SCREENSHOT:
            games_list = list(games.values())
            self.screenshot_view.set_games(games_list)
            self._screenshot_view_stale = False

        # Update list view if game selected
        if self._current_game_id and self._current_game_id in games:
            game = games[self._current_game_id]
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(self._current_game_id)
                if detail:
                    game = {**game, **detail}
            self.list_view.set_game(game)

    def get_grid_scroll_position(self) -> int:
        """Get current vertical scroll position of the active grid view."""
        if self._current_mode == VIEW_MODE_COVER:
            return self.cover_view.list_view.verticalScrollBar().value()
        elif self._current_mode == VIEW_MODE_SCREENSHOT:
            return self.screenshot_view.list_view.verticalScrollBar().value()
        return 0

    def restore_grid_scroll_position(self, pos: int) -> None:
        """Restore vertical scroll position of the active grid view."""
        if pos <= 0:
            return
        if self._current_mode == VIEW_MODE_COVER:
            self.cover_view.list_view.verticalScrollBar().setValue(pos)
        elif self._current_mode == VIEW_MODE_SCREENSHOT:
            self.screenshot_view.list_view.verticalScrollBar().setValue(pos)

    def set_grid_density(self, density: int) -> None:
        """Set grid density for cover/screenshot views

        Args:
            density: Grid item min width in pixels
        """
        self.cover_view.set_density(density)
        self.screenshot_view.set_density(density)

    def update_description(self, game_id: str, description: str) -> None:
        """Update description for a game after API refresh.

        Args:
            game_id: Game ID
            description: New HTML description
        """
        # Forward to list view (description cache managed by LazyMetadata)
        self.list_view.update_description(game_id, description)

    def update_game_favorite(self, game_id: str, is_favorite: bool) -> None:
        """Update favorite status for a game in cached data

        Args:
            game_id: Game UUID
            is_favorite: New favorite status
        """
        if game_id in self._games:
            self._games[game_id]["is_favorite"] = is_favorite
            # Mark grid views as needing update
            self._cover_view_stale = True
            self._screenshot_view_stale = True

    def update_game_tags(self, game_id: str, tags: list) -> None:
        """Update tags for a game in cached data and display

        Args:
            game_id: Game UUID
            tags: List of tag dicts with name, color keys
        """
        if game_id in self._games:
            self._games[game_id]["tags"] = tags
            # Update the list view if showing this game
            if self._current_game_id == game_id:
                self.list_view.set_tags(tags)

    def clear(self) -> None:
        """Clear all content"""
        self._current_game_id = None
        self._games.clear()
        self._cover_view_stale = True
        self._screenshot_view_stale = True
        self.list_view.clear()
        self.cover_view.clear()
        self.screenshot_view.clear()

    def get_screenshots(self) -> list:
        """Get current game's screenshot URLs"""
        return self.list_view.get_screenshots()

    def set_active_tab(self, tab_index: int) -> None:
        """Switch list view to specified tab programmatically.

        Args:
            tab_index: Tab index to switch to
        """
        self.list_view.set_active_tab(tab_index)

    def set_list_view_managers(
        self,
        runtime_manager=None,
        game_manager=None,
    ) -> None:
        """Set managers for list view Settings tab

        Args:
            runtime_manager: RuntimeManager instance
            game_manager: GameManager instance
        """
        self.list_view.set_managers(
            runtime_manager=runtime_manager,
            game_manager=game_manager,
        )

    def set_game_running(self, game_id: str, is_running: bool) -> None:
        """Update running state for a game's launch button.

        Args:
            game_id: Game UUID
            is_running: Whether the game is currently running
        """
        self.list_view.set_game_running(game_id, is_running)

    def refresh_platform_combo(self) -> None:
        """Re-populate platform/runner dropdown after RuntimeManager init."""
        self.list_view._populate_platforms()
