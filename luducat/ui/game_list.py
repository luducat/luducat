# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_list.py

"""Game list sidebar for luducat

Left panel showing all games with virtual scrolling for performance.
Uses QListView with custom delegate for two-row compact layout.
"""

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import (
    Qt,
    Signal,
    QAbstractListModel,
    QModelIndex,
    QSize,
    QRect,
    QEvent,
)
from PySide6.QtGui import (
    QPainter,
    QFont,
    QFontMetrics,
    QPalette,
    QColor,
    QHelpEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QListView,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QAbstractItemView,
    QStyle,
    QToolTip,
)

from ..core.constants import (
    GAME_MODE_LABELS,
    INSTALLED_BADGE_COLOR,
    INSTALLED_BADGE_LABEL,
)
from ..core.plugin_manager import PluginManager, _DEFAULT_BRAND_COLORS
from .badge_painter import draw_badge, draw_icon_badge, get_player_count, game_mode_badge_width

logger = logging.getLogger(__name__)


# Custom roles for game data
class GameRoles:
    GameId = Qt.ItemDataRole.UserRole + 1
    Title = Qt.ItemDataRole.UserRole + 2
    Stores = Qt.ItemDataRole.UserRole + 3  # List of store names
    IsFavorite = Qt.ItemDataRole.UserRole + 4
    GameData = Qt.ItemDataRole.UserRole + 5  # Full game dict
    IsFamilyShared = Qt.ItemDataRole.UserRole + 6  # Borrowed via family sharing
    GameModes = Qt.ItemDataRole.UserRole + 7  # List of IGDB game mode names
    IsInstalled = Qt.ItemDataRole.UserRole + 8  # At least one store is installed


class GameListModel(QAbstractListModel):
    """Model for game list with virtual scrolling support

    Designed for efficient handling of 15,000+ games.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._games: List[Dict[str, Any]] = []
        self._filtered_indices: List[int] = []
        self._search_filter: str = ""
        self._filters: Dict[str, Any] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._filtered_indices)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._filtered_indices):
            return None

        game_idx = self._filtered_indices[index.row()]
        game = self._games[game_idx]

        if role == Qt.ItemDataRole.DisplayRole or role == GameRoles.Title:
            return game.get("title", _("Unknown"))
        elif role == GameRoles.GameId:
            return game.get("id", "")
        elif role == GameRoles.Stores:
            return game.get("stores", [])
        elif role == GameRoles.IsFavorite:
            return game.get("is_favorite", False)
        elif role == GameRoles.IsFamilyShared:
            return game.get("is_family_shared", False)
        elif role == GameRoles.GameModes:
            return game.get("game_modes", [])
        elif role == GameRoles.IsInstalled:
            return game.get("is_installed", False)
        elif role == GameRoles.GameData:
            return game

        return None

    def set_games(self, games: List[Dict[str, Any]]) -> None:
        """Set the full game list

        Games are pre-filtered by main_window, so just display them all.
        Search filter can still be applied locally if set.

        Args:
            games: List of game dicts with id, title, stores, is_favorite
        """
        self.beginResetModel()
        self._games = games
        # Games are already filtered by main_window - just build index for all
        # Only apply search filter if set (for incremental search while typing)
        if self._search_filter:
            self._apply_search_only()
        else:
            self._filtered_indices = list(range(len(games)))
        self.endResetModel()

    def append_games(self, games: list) -> None:
        """Append games incrementally during progressive loading.

        Uses beginInsertRows/endInsertRows so existing viewport is untouched.
        """
        if not games:
            return
        start = len(self._games)
        count = len(games)
        # Insert new filtered indices for all appended games
        filtered_start = len(self._filtered_indices)
        new_indices = list(range(start, start + count))
        self.beginInsertRows(QModelIndex(), filtered_start, filtered_start + count - 1)
        self._games.extend(games)
        self._filtered_indices.extend(new_indices)
        self.endInsertRows()

    def _apply_search_only(self) -> None:
        """Apply only search filter (for use when games are pre-filtered)"""
        self._filtered_indices = []
        search_lower = self._search_filter.lower()
        for i, game in enumerate(self._games):
            title = game.get("title", "").lower()
            if search_lower in title:
                self._filtered_indices.append(i)

    def _apply_filters(self) -> None:
        """Apply current filters to game list

        Filter structure:
        - base_filter: "all", "recent" (recently played), or "hidden" (show hidden games)
        - type_filters: list of ["favorites", "free"] (OR'd)
        - stores: list of store names (OR'd)
        - tags: list of tag names (OR'd)
        """
        self._filtered_indices = []

        search_lower = self._search_filter.lower()
        base_filter = self._filters.get("base_filter", "all")
        type_filters = set(self._filters.get("type_filters", []))
        filter_stores = self._filters.get("stores", [])
        filter_tags = self._filters.get("tags", [])

        for i, game in enumerate(self._games):
            # Search filter
            if search_lower:
                title = game.get("title", "").lower()
                if search_lower not in title:
                    continue

            # Base filter determines visibility of hidden games
            is_hidden = game.get("is_hidden", False)
            if base_filter == "hidden":
                # Only show hidden games
                if not is_hidden:
                    continue
            else:
                # Hide hidden games in "all" and "recent" modes
                if is_hidden:
                    continue

            # Base filter: recently played
            if base_filter == "recent":
                if not game.get("last_launched"):
                    continue

            # Store filter (OR'd)
            if filter_stores:
                game_stores = game.get("stores", [])
                if not any(s in game_stores for s in filter_stores):
                    continue

            # Type filters (OR'd) - if any type filters are selected, game must match at least one
            if type_filters:
                matches_type = False

                # Check favorites
                if "favorites" in type_filters and game.get("is_favorite", False):
                    matches_type = True

                # Check free games
                if "free" in type_filters and game.get("is_free", False):
                    matches_type = True

                # Check installed games
                if "installed" in type_filters and game.get("is_installed", False):
                    matches_type = True

                if not matches_type:
                    continue

            # Tag filter (OR'd)
            if filter_tags:
                game_tag_names = {
                    (tag.get("name", "") if isinstance(tag, dict) else tag)
                    for tag in game.get("tags", [])
                }
                if not any(t in game_tag_names for t in filter_tags):
                    continue

            self._filtered_indices.append(i)

    def set_search_filter(self, text: str) -> None:
        """Set search filter

        Args:
            text: Search query
        """
        self.beginResetModel()
        self._search_filter = text
        self._apply_filters()
        self.endResetModel()

    def set_filters(self, filters: Dict[str, Any]) -> None:
        """Set filter criteria

        Args:
            filters: Dict with quick_filter, stores, tags
        """
        self.beginResetModel()
        self._filters = filters
        self._apply_filters()
        self.endResetModel()

    def get_game_at(self, index: QModelIndex) -> Optional[Dict[str, Any]]:
        """Get game data at model index"""
        return self.data(index, GameRoles.GameData)

    def update_game_favorite(self, game_id: str, is_favorite: bool) -> None:
        """Update favorite status for a game

        Args:
            game_id: Game UUID
            is_favorite: New favorite status
        """
        for i, game in enumerate(self._games):
            if game.get("id") == game_id:
                game["is_favorite"] = is_favorite
                # Find the row in filtered indices and emit dataChanged
                if i in self._filtered_indices:
                    row = self._filtered_indices.index(i)
                    idx = self.index(row, 0)
                    self.dataChanged.emit(idx, idx, [GameRoles.IsFavorite])
                # If favorites filter is active, might need to re-filter
                type_filters = self._filters.get("type_filters", [])
                if "favorites" in type_filters:
                    self.beginResetModel()
                    self._apply_filters()
                    self.endResetModel()
                break

    def update_game_hidden(self, game_id: str, is_hidden: bool) -> None:
        """Update hidden status for a game

        Args:
            game_id: Game UUID
            is_hidden: New hidden status
        """
        for i, game in enumerate(self._games):
            if game.get("id") == game_id:
                game["is_hidden"] = is_hidden
                # Always re-filter since hidden state affects visibility
                self.beginResetModel()
                self._apply_filters()
                self.endResetModel()
                break


class GameListDelegate(QStyledItemDelegate):
    """Custom delegate for two-row compact game list items

    Layout:
    ┌────────────────────────────────┐
    │ Game Title                     │  <- Row 1: Title (truncated)
    │ [Steam] [GOG] ★               │  <- Row 2: Store badges + favorite
    └────────────────────────────────┘
    """

    ITEM_HEIGHT = 44
    PADDING = 6
    BADGE_HEIGHT = 16
    BADGE_PADDING = 4

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # Use system font as base, with relative sizing
        base_size = QApplication.instance().font().pointSize()
        if base_size <= 0:
            base_size = 10  # Fallback

        self._title_font = QFont()
        self._title_font.setPointSize(base_size + 1)  # Slightly larger for titles

        self._badge_font = QFont()
        self._badge_font.setPointSize(max(7, base_size - 2))  # Smaller for badges

        # Favorite star color — initialized from theme variable default,
        # updated via update_theme_colors() when theme changes
        self._fav_color = QColor("#f1c40f")

        # Local cache of plugin data (avoids classmethod dispatch in paint())
        self._brand_colors: Dict[str, Dict[str, str]] = dict(PluginManager._brand_colors)
        self._badge_labels: Dict[str, str] = dict(PluginManager._badge_labels)

    def update_theme_colors(self, fav_color: str) -> None:
        """Update delegate colors from current theme."""
        self._fav_color = QColor(fav_color)

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QSize:
        return QSize(option.rect.width(), self.ITEM_HEIGHT)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        painter.save()

        # Get data
        title = index.data(GameRoles.Title) or _("Unknown")
        stores = index.data(GameRoles.Stores) or []
        is_favorite = index.data(GameRoles.IsFavorite) or False
        is_installed = index.data(GameRoles.IsInstalled) or False
        game_modes = index.data(GameRoles.GameModes) or []
        game = index.data(GameRoles.GameData) or {}

        # Background
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            text_color = option.palette.highlightedText().color()
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, option.palette.midlight())
            text_color = option.palette.text().color()
        else:
            text_color = option.palette.text().color()

        # Calculate positions
        x = option.rect.x() + 10
        y = option.rect.y() + self.PADDING
        width = option.rect.width() - 20

        # Row 1: Title (dimmed for orphaned games with no store links)
        painter.setFont(self._title_font)
        if not stores:
            dimmed = QColor(text_color)
            dimmed.setAlpha(120)
            painter.setPen(dimmed)
        else:
            painter.setPen(text_color)

        title_rect = QRect(x, y, width, 18)
        fm = QFontMetrics(self._title_font)
        elided_title = fm.elidedText(title, Qt.TextElideMode.ElideRight, width)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided_title)

        # Row 2: Store badges + favorite
        y += 20
        badge_x = x

        painter.setFont(self._badge_font)

        badge_fm = QFontMetrics(self._badge_font)

        for store in stores:
            colors = self._brand_colors.get(store, _DEFAULT_BRAND_COLORS)

            badge_text = self._badge_labels.get(store, store.upper()[:3])
            text_width = badge_fm.horizontalAdvance(badge_text)
            badge_width = text_width + 12

            badge_rect = QRect(badge_x, y, badge_width, self.BADGE_HEIGHT)
            draw_badge(painter, badge_rect, colors["bg"], colors["text"], badge_text)

            badge_x += badge_width + self.BADGE_PADDING

        # Favorite star
        if is_favorite:
            painter.setPen(self._fav_color)
            star_rect = QRect(badge_x, y, 14, self.BADGE_HEIGHT)
            painter.drawText(star_rect, Qt.AlignmentFlag.AlignCenter, "★")
            badge_x += 14 + self.BADGE_PADDING

        # Game mode badges (icon glyphs with optional player count)
        for mode_name in game_modes:
            if mode_name not in GAME_MODE_LABELS:
                continue
            player_count = get_player_count(game, mode_name)
            badge_w = game_mode_badge_width(mode_name, self.BADGE_HEIGHT, player_count)
            if badge_w == 0:
                badge_w = self.BADGE_HEIGHT
            badge_rect = QRect(badge_x, y, badge_w, self.BADGE_HEIGHT)
            drawn = draw_icon_badge(painter, badge_rect, mode_name, player_count)
            if drawn == 0:
                # Fallback to text if icon missing
                label = GAME_MODE_LABELS[mode_name]
                display_label = _(label)
                text_width = badge_fm.horizontalAdvance(display_label)
                badge_width = text_width + 10
                badge_rect = QRect(badge_x, y, badge_width, self.BADGE_HEIGHT)
                palette = QApplication.palette()
                badge_bg = palette.color(QPalette.ColorRole.Mid).name()
                badge_fg = palette.color(QPalette.ColorRole.WindowText).name()
                draw_badge(painter, badge_rect, badge_bg, badge_fg, display_label)
                badge_x += badge_width + self.BADGE_PADDING
            else:
                badge_x += badge_w + self.BADGE_PADDING

        # Installed badge
        if is_installed:
            inst_text = _(INSTALLED_BADGE_LABEL)
            text_width = badge_fm.horizontalAdvance(inst_text)
            badge_width = text_width + 10
            badge_rect = QRect(badge_x, y, badge_width, self.BADGE_HEIGHT)
            draw_badge(painter, badge_rect, INSTALLED_BADGE_COLOR["bg"], INSTALLED_BADGE_COLOR["text"], inst_text)
            badge_x += badge_width + self.BADGE_PADDING

        painter.restore()

    def refresh_plugin_data(self) -> None:
        """Re-snapshot PluginManager brand colors and badge labels after plugin changes."""
        self._brand_colors = dict(PluginManager._brand_colors)
        self._badge_labels = dict(PluginManager._badge_labels)

    def helpEvent(
        self,
        event: QHelpEvent,
        view: QAbstractItemView,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        """Show tooltip for truncated titles and game mode icon badges."""
        if event.type() == QEvent.Type.ToolTip:
            pos = event.pos()

            # Row 1 zone: title tooltip (truncation)
            title_y = option.rect.y() + self.PADDING
            if title_y <= pos.y() < title_y + 18:
                title = index.data(GameRoles.Title) or ""
                if title:
                    width = option.rect.width() - 20
                    fm = QFontMetrics(self._title_font)
                    elided = fm.elidedText(title, Qt.TextElideMode.ElideRight, width)
                    if elided != title:
                        QToolTip.showText(event.globalPos(), title, view)
                        return True
                QToolTip.hideText()
                return True

            # Row 2 zone: game mode badge tooltips
            badge_y = option.rect.y() + self.PADDING + 20
            if badge_y <= pos.y() <= badge_y + self.BADGE_HEIGHT:
                tooltip = self._game_mode_tooltip_at(pos.x(), option, index)
                if tooltip:
                    QToolTip.showText(event.globalPos(), tooltip, view)
                    return True

            QToolTip.hideText()
            return True

        return super().helpEvent(event, view, option, index)

    def _game_mode_tooltip_at(
        self, mouse_x: int, option: QStyleOptionViewItem, index: QModelIndex
    ) -> Optional[str]:
        """Check if mouse_x is over a game mode badge, return tooltip text."""
        stores = index.data(GameRoles.Stores) or []
        is_favorite = index.data(GameRoles.IsFavorite) or False
        game_modes = index.data(GameRoles.GameModes) or []
        game = index.data(GameRoles.GameData) or {}

        if not game_modes:
            return None

        # Replay badge X layout to find game mode badge positions
        badge_x = option.rect.x() + 10
        badge_fm = QFontMetrics(self._badge_font)

        # Skip store badges
        for store in stores:
            badge_text = self._badge_labels.get(store, store.upper()[:3])
            badge_x += badge_fm.horizontalAdvance(badge_text) + 12 + self.BADGE_PADDING

        # Skip favorite star
        if is_favorite:
            badge_x += 14 + self.BADGE_PADDING

        # Check game mode badges
        for mode_name in game_modes:
            if mode_name not in GAME_MODE_LABELS:
                continue
            player_count = get_player_count(game, mode_name)
            badge_w = game_mode_badge_width(mode_name, self.BADGE_HEIGHT, player_count)
            if badge_w == 0:
                badge_w = self.BADGE_HEIGHT
            badge_end = badge_x + badge_w
            if badge_x <= mouse_x <= badge_end:
                from ..core.constants import GAME_MODE_FILTERS
                label = GAME_MODE_FILTERS.get(mode_name, mode_name)
                tip = _(label)
                if player_count:
                    tip += " " + ngettext(
                        "({count} player)", "({count} players)", int(player_count)
                    ).format(count=player_count)
                return tip
            badge_x = badge_end + self.BADGE_PADDING

        return None


class GameList(QWidget):
    """Game list sidebar widget

    Signals:
        game_selected: Emitted when a game is selected (game_id)
    """

    game_selected = Signal(str)
    context_menu_requested = Signal(object, object)  # game_data, global_pos

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._selecting_programmatically = False

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        """Create game list layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # List view with virtual scrolling
        self.list_view = QListView()
        self.list_view.setObjectName("gameListView")

        # Model
        self.model = GameListModel()
        self.list_view.setModel(self.model)

        # Delegate
        self.delegate = GameListDelegate()
        self.list_view.setItemDelegate(self.delegate)

        # Virtual scrolling settings
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setUniformItemSizes(True)  # Critical for performance
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Disable horizontal scroll
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Enable context menu
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        layout.addWidget(self.list_view)

    def _connect_signals(self) -> None:
        """Connect signals

        Only currentChanged is needed — it covers mouse click (when item
        changes), keyboard navigation, and programmatic selection.
        clicked/activated caused duplicate emissions (2-3 set_game calls per
        single click).
        """
        self.list_view.selectionModel().currentChanged.connect(
            lambda current, _prev: self._on_item_clicked(current)
        )
        self.list_view.customContextMenuRequested.connect(self._on_context_menu_requested)

    def _on_item_clicked(self, index: QModelIndex) -> None:
        """Handle item selection change.

        Suppressed during programmatic selection (select_game) to prevent
        feedback loops when grid views sync the sidebar.
        """
        if self._selecting_programmatically:
            return
        game_id = index.data(GameRoles.GameId)
        if game_id:
            self.game_selected.emit(game_id)

    def _on_context_menu_requested(self, pos) -> None:
        """Handle right-click context menu request"""
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        game_data = index.data(GameRoles.GameData)
        if game_data:
            global_pos = self.list_view.viewport().mapToGlobal(pos)
            self.context_menu_requested.emit(game_data, global_pos)

    def set_games(self, games: List[Dict[str, Any]]) -> None:
        """Set game list data

        Args:
            games: List of game dicts
        """
        # Preserve scroll position across model reset
        scrollbar = self.list_view.verticalScrollBar()
        saved_scroll = scrollbar.value() if scrollbar else 0

        self.model.set_games(games)

        # Restore scroll position (clamped to new range automatically)
        if scrollbar and saved_scroll > 0:
            scrollbar.setValue(saved_scroll)

    def append_games(self, games: List[Dict[str, Any]]) -> None:
        """Append games incrementally during progressive loading."""
        if not games:
            return
        self.model.append_games(games)

    def set_search_filter(self, text: str) -> None:
        """Set search filter

        Args:
            text: Search query
        """
        self.model.set_search_filter(text)

    def set_filters(self, filters: Dict[str, Any]) -> None:
        """Set filter criteria

        Args:
            filters: Filter dict
        """
        self.model.set_filters(filters)

    def update_game_favorite(self, game_id: str, is_favorite: bool) -> None:
        """Update favorite status for a game

        Args:
            game_id: Game UUID
            is_favorite: New favorite status
        """
        self.model.update_game_favorite(game_id, is_favorite)

    def update_game_hidden(self, game_id: str, is_hidden: bool) -> None:
        """Update hidden status for a game

        Args:
            game_id: Game UUID
            is_hidden: New hidden status
        """
        self.model.update_game_hidden(game_id, is_hidden)

    def get_selected_game_id(self) -> Optional[str]:
        """Get currently selected game ID"""
        indexes = self.list_view.selectedIndexes()
        if indexes:
            return indexes[0].data(GameRoles.GameId)
        return None

    def select_game(self, game_id: str) -> None:
        """Select a game by ID (visual only, no signal emission).

        Sets _selecting_programmatically to suppress currentChanged → game_selected
        feedback when called from grid view sync paths.

        Args:
            game_id: Game ID to select
        """
        self._selecting_programmatically = True
        try:
            for row in range(self.model.rowCount()):
                index = self.model.index(row, 0)
                if index.data(GameRoles.GameId) == game_id:
                    self.list_view.setCurrentIndex(index)
                    self.list_view.scrollTo(index)
                    break
        finally:
            self._selecting_programmatically = False
