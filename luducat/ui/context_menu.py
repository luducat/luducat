# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# context_menu.py

"""Context menu for game items in all views (list, cover, screenshot).

Reusable QMenu subclass that builds a context menu from game data and emits
signals for all actions. Business logic is handled by MainWindow.
"""

import logging
from typing import Any, Dict, List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QWidgetAction,
)

from ..core.constants import GAME_MODE_FILTERS
from ..core.plugin_manager import PluginManager
from ..utils.browser import open_url

logger = logging.getLogger(__name__)


class GameContextMenu(QMenu):
    """Right-click context menu for game items.

    Emits signals for all actions. Constructed per-invocation via build().
    """

    play_requested = Signal(str, str)              # game_id, store_name
    favorite_toggled = Signal(str, bool)            # game_id, new_state
    hidden_toggled = Signal(str, bool)              # game_id, new_state
    nsfw_override_changed = Signal(str, int)        # game_id, override_value
    edit_tags_requested = Signal(str)               # game_id
    filter_game_modes_requested = Signal(list)      # list of mode names
    filter_developers_requested = Signal(list)      # list of developer names
    filter_publishers_requested = Signal(list)      # list of publisher names
    filter_genres_requested = Signal(list)          # list of genre names
    filter_year_requested = Signal(list)            # list of year strings
    view_screenshots_requested = Signal(str)        # game_id
    open_store_page_requested = Signal(str)         # game_id
    force_rescan_requested = Signal(str)            # game_id
    switch_to_notes_requested = Signal(str)         # game_id
    switch_to_properties_requested = Signal(str)    # game_id
    cover_author_score_requested = Signal(str, str, int)  # game_id, author_name, score_delta

    def build(
        self,
        game: Dict[str, Any],
        default_store: str = "",
        active_filters: Dict[str, Any] = None,
        view_mode: str = "",
        sgdb_cover_author: str = "",
        sgdb_author_steam_id: str = "",
    ) -> None:
        """Build context menu from game data.

        Args:
            game: Full game data dict with keys like id, title, stores, etc.
            default_store: User's preferred default store for launching.
            active_filters: Current filter state dict from filter bar.
            view_mode: Current view mode (list/cover/screenshot).
            sgdb_cover_author: Author name if cover is from SteamGridDB.
            sgdb_author_steam_id: Steam64 ID of the cover author (for profile link).
        """
        self.clear()
        game_id = game.get("id", "")
        self._active_filters = active_filters or {}

        # 1. Title header
        self._add_title_header(game.get("title", _("Unknown")))
        self.addSeparator()

        # 3-4. Play / Play with...
        self._add_play_actions(game, game_id, default_store)
        self.addSeparator()

        # 6. Favorite / Unfavorite
        is_fav = game.get("is_favorite", False)
        fav_action = self.addAction(_("Unfavorite") if is_fav else _("Favorite"))
        fav_action.triggered.connect(
            lambda checked, gid=game_id, f=is_fav: self.favorite_toggled.emit(gid, not f)
        )

        # 7. Hide / Unhide
        is_hidden = game.get("is_hidden", False)
        hide_action = self.addAction(_("Unhide") if is_hidden else _("Hide"))
        hide_action.triggered.connect(
            lambda checked, gid=game_id, h=is_hidden: self.hidden_toggled.emit(gid, not h)
        )

        # 7b. Content Rating submenu
        self._add_content_rating_submenu(game, game_id)

        # 8. Tags
        tags_action = self.addAction(_("Tags..."))
        tags_action.triggered.connect(
            lambda checked, gid=game_id: self.edit_tags_requested.emit(gid)
        )
        self.addSeparator()

        # 10. Filter submenu
        self._add_filter_submenu(game)
        self.addSeparator()

        # 12. Screenshots
        screenshots = game.get("screenshots", [])
        ss_action = self.addAction(_("Screenshots"))
        ss_action.setEnabled(bool(screenshots))
        ss_action.triggered.connect(
            lambda checked, gid=game_id: self.view_screenshots_requested.emit(gid)
        )

        # 13. Store Page
        stores = game.get("stores", [])
        store_action = self.addAction(_("Store Page"))
        store_action.setEnabled(bool(stores))
        store_action.triggered.connect(
            lambda checked, gid=game_id: self.open_store_page_requested.emit(gid)
        )
        self.addSeparator()

        # 15. Force Rescan
        rescan_action = self.addAction(_("Force Rescan..."))
        rescan_action.triggered.connect(
            lambda checked, gid=game_id: self.force_rescan_requested.emit(gid)
        )
        self.addSeparator()

        # 17. Notes
        notes_action = self.addAction(_("Notes"))
        notes_action.triggered.connect(
            lambda checked, gid=game_id: self.switch_to_notes_requested.emit(gid)
        )

        # 18. Properties
        props_action = self.addAction(_("Properties"))
        props_action.triggered.connect(
            lambda checked, gid=game_id: self.switch_to_properties_requested.emit(gid)
        )

        # 19. SteamGridDB author submenu (cover source)
        self._add_sgdb_author_submenu(game, game_id, sgdb_cover_author,
                                      sgdb_author_steam_id)

    def _add_title_header(self, title: str) -> None:
        """Add game title as styled header at top of menu."""
        label = QLabel(f"  {title}  ")
        label.setObjectName("contextMenuTitle")

        # Bold font with relative sizing
        base_size = QApplication.instance().font().pointSize()
        font = label.font()
        font.setPointSize(base_size + 1)
        font.setBold(True)
        label.setFont(font)

        # Accent background using palette highlight role
        label.setAutoFillBackground(True)
        pal = label.palette()
        pal.setColor(label.backgroundRole(), pal.highlight().color())
        pal.setColor(label.foregroundRole(), pal.highlightedText().color())
        label.setPalette(pal)

        widget_action = QWidgetAction(self)
        widget_action.setDefaultWidget(label)
        widget_action.setEnabled(False)
        self.addAction(widget_action)

    def _add_play_actions(
        self, game: Dict[str, Any], game_id: str, default_store: str
    ) -> None:
        """Add Play and Play with... actions."""
        stores = game.get("stores", [])
        if not stores:
            action = self.addAction(_("Play"))
            action.setEnabled(False)
            return

        # Determine primary store
        primary = self._get_primary_store(stores, default_store)

        play_action = self.addAction(_("Play ({store})").format(store=PluginManager.get_store_display_name(primary)))
        play_action.triggered.connect(
            lambda checked, gid=game_id, s=primary: self.play_requested.emit(gid, s)
        )

        # Play with... submenu if multiple stores
        if len(stores) > 1:
            play_menu = self.addMenu(_("Play with..."))
            for store in stores:
                action = play_menu.addAction(PluginManager.get_store_display_name(store))
                action.triggered.connect(
                    lambda checked, gid=game_id, s=store: self.play_requested.emit(gid, s)
                )

    def _add_filter_submenu(self, game: Dict[str, Any]) -> None:
        """Add Filter submenu with game mode, developer, publisher, year options.

        Each entry is a direct filter action (no dialogs from context menu).
        Active filters are marked with checkmarks. Clicking an active filter
        emits an empty list to remove it (toggle behavior).
        """
        game_modes = game.get("game_modes", [])
        developers = game.get("developers") or []
        publishers = game.get("publishers") or []
        genres = game.get("genres") or []
        release_date = game.get("release_date", "")

        if not game_modes and not developers and not publishers and not genres and not release_date:
            return

        # Get active filter sets for comparison
        active_modes = set(self._active_filters.get("game_modes", []))
        active_devs = set(self._active_filters.get("developers", []))
        active_pubs = set(self._active_filters.get("publishers", []))
        active_genres = set(self._active_filters.get("genres", []))
        active_years = set(self._active_filters.get("years", []))

        any_active = False
        filter_menu = self.addMenu(_("Filter"))
        has_items = False

        # Game modes - each as a direct filter action
        for mode in game_modes:
            label = GAME_MODE_FILTERS.get(mode, mode)
            is_active = mode in active_modes
            action = filter_menu.addAction(_("Game Mode: {label}").format(label=label))
            action.setCheckable(True)
            action.setChecked(is_active)
            if is_active:
                any_active = True
            # Toggle: if active, emit empty to remove; otherwise emit [mode]
            action.triggered.connect(
                lambda checked, m=mode, active=is_active: (
                    self.filter_game_modes_requested.emit(
                        [] if active else [m]
                    )
                )
            )
            has_items = True

        # Separator between game modes and dev/pub
        if has_items and (developers or publishers):
            filter_menu.addSeparator()

        # Developers - each as a direct filter action
        for dev in developers:
            is_active = dev in active_devs
            action = filter_menu.addAction(_("Developer: {dev}").format(dev=dev))
            action.setCheckable(True)
            action.setChecked(is_active)
            if is_active:
                any_active = True
            action.triggered.connect(
                lambda checked, d=dev, active=is_active: (
                    self.filter_developers_requested.emit(
                        [] if active else [d]
                    )
                )
            )
            has_items = True

        # Publishers - each as a direct filter action
        for pub in publishers:
            is_active = pub in active_pubs
            action = filter_menu.addAction(_("Publisher: {pub}").format(pub=pub))
            action.setCheckable(True)
            action.setChecked(is_active)
            if is_active:
                any_active = True
            action.triggered.connect(
                lambda checked, p=pub, active=is_active: (
                    self.filter_publishers_requested.emit(
                        [] if active else [p]
                    )
                )
            )
            has_items = True

        # Genres - each as a direct filter action
        if genres:
            if has_items:
                filter_menu.addSeparator()
            for genre in genres:
                is_active = genre in active_genres
                action = filter_menu.addAction(_("Genre: {genre}").format(genre=genre))
                action.setCheckable(True)
                action.setChecked(is_active)
                if is_active:
                    any_active = True
                action.triggered.connect(
                    lambda checked, g=genre, active=is_active: (
                        self.filter_genres_requested.emit(
                            [] if active else [g]
                        )
                    )
                )
                has_items = True

        # Year - direct filter
        if release_date:
            year = self._extract_year(release_date)
            if year:
                if has_items:
                    filter_menu.addSeparator()
                is_active = year in active_years
                action = filter_menu.addAction(_("Year: {year}").format(year=year))
                action.setCheckable(True)
                action.setChecked(is_active)
                if is_active:
                    any_active = True
                action.triggered.connect(
                    lambda checked, y=year, active=is_active: (
                        self.filter_year_requested.emit(
                            [] if active else [y]
                        )
                    )
                )

        # Mark the Filter submenu itself if any entry is active
        if any_active:
            filter_menu.setTitle(_("Filter") + " \u2713")

    @staticmethod
    def _extract_year(date_str: str) -> str:
        """Extract 4-digit year from a date string.

        Dates are normalised to ISO at cache-build time, so ``[:4]``
        covers the common case.  The fallback scan handles any edge
        cases that slipped through un-normalised.
        """
        if not date_str:
            return ""
        if len(date_str) >= 4 and date_str[:4].isdigit():
            return date_str[:4]
        parts = date_str.replace(",", "").split()
        for part in reversed(parts):
            if len(part) == 4 and part.isdigit():
                return part
        return ""

    def _add_content_rating_submenu(
        self, game: Dict[str, Any], game_id: str
    ) -> None:
        """Add Content Rating submenu with per-game NSFW override options."""
        current = game.get("nsfw_override", 0)
        menu = self.addMenu(_("Content Rating"))

        options = [
            (0, _("Automatic"), _("Use automatic detection from store ratings and metadata")),
            (1, _("Always hide (NSFW)"), _("Always hide this game when the content filter is active")),
            (-1, _("Never hide (SFW)"), _("Never hide this game, even if automatic detection flags it")),
        ]

        for value, label, tooltip in options:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(current == value)
            action.setToolTip(tooltip)
            action.triggered.connect(
                lambda checked, gid=game_id, v=value: self.nsfw_override_changed.emit(gid, v)
            )

    def _add_sgdb_author_submenu(
        self, game: Dict[str, Any], game_id: str, author_name: str,
        author_steam_id: str = "",
    ) -> None:
        """Add SteamGridDB author block/boost submenu when cover is from SGDB.

        Only shown when author_name is provided (looked up by MainWindow).
        """
        if not author_name:
            return

        self.addSeparator()
        author_menu = self.addMenu(
            _("Cover by {author}").format(author=author_name)
        )

        if author_steam_id:
            profile_action = author_menu.addAction(
                _("Open Author Profile on SteamGridDB")
            )
            profile_action.triggered.connect(
                lambda checked, sid=author_steam_id: (
                    open_url(f"https://www.steamgriddb.com/profile/{sid}")
                )
            )
            author_menu.addSeparator()

        block_action = author_menu.addAction(_("Block Author (-1)"))
        block_action.triggered.connect(
            lambda checked, gid=game_id, name=author_name: (
                self.cover_author_score_requested.emit(gid, name, -1)
            )
        )

        boost_action = author_menu.addAction(_("Boost Author (+1)"))
        boost_action.triggered.connect(
            lambda checked, gid=game_id, name=author_name: (
                self.cover_author_score_requested.emit(gid, name, 1)
            )
        )

    @staticmethod
    def _get_primary_store(stores: List[str], default_store: str) -> str:
        """Get the primary store for launch actions.

        Uses default_store if the game has it, otherwise falls back
        to the discovered plugin priority order.
        """
        if default_store in stores:
            return default_store

        for s in PluginManager.get_store_plugin_names():
            if s in stores:
                return s

        return stores[0]
