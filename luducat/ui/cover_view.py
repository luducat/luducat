# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# cover_view.py

"""Cover view (grid) for luducat

Grid display of game covers in 2:3 portrait format.
Uses QListView in IconMode with custom delegate for virtual scrolling.
"""

import html
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from PySide6.QtCore import (
    Qt,
    Signal,
    QSize,
    QRect,
    QModelIndex,
    QAbstractListModel,
    QTimer,
    QThread,
    QObject,
    QEvent,
)
from PySide6.QtGui import (
    QPainter,
    QPalette,
    QFont,
    QFontMetrics,
    QColor,
    QPen,
    QPixmap,
    QHelpEvent,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QListView,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QAbstractItemView,
    QApplication,
    QToolTip,
)

from ..core.constants import (
    GAME_MODE_LABELS,
    INSTALLED_BADGE_COLOR,
    INSTALLED_BADGE_LABEL,
    PROTONDB_TIER_LABELS,
    PROTONDB_TIER_COLORS,
    STEAM_DECK_LABELS,
    STEAM_DECK_COLORS,
    DEFAULT_IMAGE_FADE_MS,
)
from ..core.plugin_manager import PluginManager, _DEFAULT_BRAND_COLORS
from ..utils.image_cache import get_cover_cache, ImageCache
from .badge_painter import draw_badge, draw_icon_badge, draw_license_circle, get_player_count, game_mode_badge_width

logger = logging.getLogger(__name__)


class CoverFetchWorker(QThread):
    """Worker thread for fetching covers from fallback sources."""

    cover_fetched = Signal(str, str)  # game_id, cover_url (empty string if not found)

    def __init__(
        self,
        game_id: str,
        callback: Callable[[str], str],
        parent: Optional[QObject] = None
    ):
        super().__init__(parent)
        self._game_id = game_id
        self._callback = callback

    def run(self) -> None:
        """Fetch cover in background thread."""
        try:
            cover_url = self._callback(self._game_id)
            self.cover_fetched.emit(self._game_id, cover_url or "")
        except Exception as e:
            logger.debug(f"Failed to fetch cover for {self._game_id}: {e}")
            self.cover_fetched.emit(self._game_id, "")


# Custom roles for game data
class CoverRoles:
    GameId = Qt.ItemDataRole.UserRole + 1
    Title = Qt.ItemDataRole.UserRole + 2
    Stores = Qt.ItemDataRole.UserRole + 3
    IsFavorite = Qt.ItemDataRole.UserRole + 4
    GameData = Qt.ItemDataRole.UserRole + 5
    CoverUrl = Qt.ItemDataRole.UserRole + 6
    IsFamilyShared = Qt.ItemDataRole.UserRole + 7
    GameModes = Qt.ItemDataRole.UserRole + 8  # List of IGDB game mode names
    ProtonDbTier = Qt.ItemDataRole.UserRole + 9  # ProtonDB tier (Platinum, Gold, etc.)
    SteamDeckCompat = Qt.ItemDataRole.UserRole + 10  # Steam Deck compat (verified, playable, etc.)
    IsInstalled = Qt.ItemDataRole.UserRole + 11  # Installed locally


class CoverListModel(QAbstractListModel):
    """Model for cover grid with virtual scrolling support"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._games: List[Dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._games)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._games):
            return None

        game = self._games[index.row()]

        if role == Qt.ItemDataRole.DisplayRole or role == CoverRoles.Title:
            return game.get("title", _("Unknown"))
        elif role == CoverRoles.GameId:
            return game.get("id", "")
        elif role == CoverRoles.Stores:
            return game.get("stores", [])
        elif role == CoverRoles.IsFavorite:
            return game.get("is_favorite", False)
        elif role == CoverRoles.IsFamilyShared:
            return game.get("is_family_shared", False)
        elif role == CoverRoles.GameModes:
            return game.get("game_modes", [])
        elif role == CoverRoles.ProtonDbTier:
            return game.get("protondb_rating", "")
        elif role == CoverRoles.SteamDeckCompat:
            return game.get("steam_deck_compat", "")
        elif role == CoverRoles.IsInstalled:
            return game.get("is_installed", False)
        elif role == CoverRoles.GameData:
            return game
        elif role == CoverRoles.CoverUrl:
            return game.get("cover_image", "")
        elif role == Qt.ItemDataRole.ToolTipRole:
            return self._format_tooltip(game)

        return None

    def _format_tooltip(self, game: Dict[str, Any]) -> str:
        """Format game data as HTML tooltip"""
        from bs4 import BeautifulSoup

        title = html.escape(game.get("title", _("Unknown")))

        # Prefer short_description, fall back to description only if short is empty
        description = game.get("short_description", "")
        if not description:
            description = game.get("description", "")

        # Convert HTML to plain text using BeautifulSoup
        if description:
            soup = BeautifulSoup(description, "html.parser")
            # Remove all links but keep their text
            for a in soup.find_all("a"):
                a.replace_with(a.get_text())
            # Get plain text
            description = soup.get_text(separator=" ", strip=True)
            # Truncate if needed
            if len(description) > 300:
                description = description[:300] + "..."
            description = html.escape(description)

        stores = game.get("stores", [])
        stores_text = ", ".join(PluginManager.get_store_display_name(s) for s in stores) if stores else ""

        tooltip = f"<b>{title}</b>"
        if stores_text:
            tooltip += f"<br/><i>{stores_text}</i>"
        if description:
            tooltip += f"<br/><br/>{description}"

        if logger.isEnabledFor(logging.DEBUG):
            cover_url = game.get("cover_image", "")
            confidence = game.get("adult_confidence", 0.0)
            tooltip += (
                f"<br/><br/>--- debug ---<br/>"
                f"cover: {html.escape(cover_url) if cover_url else 'none'}<br/>"
                f"nsfw: {confidence:.2f}"
            )

        last_launched = game.get("last_launched")
        if last_launched:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_launched)
                tooltip += f"<br/><br/>{_('Last played')}: {dt.strftime('%Y-%m-%d')}"
            except (ValueError, TypeError):
                pass

        return tooltip

    def set_games(self, games: List[Dict[str, Any]]) -> None:
        """Set the game list"""
        self.beginResetModel()
        self._games = games
        self.endResetModel()

    def append_games(self, games: list) -> None:
        """Append games incrementally during progressive loading.

        Uses beginInsertRows/endInsertRows so existing viewport is untouched.
        """
        if not games:
            return
        start = len(self._games)
        self.beginInsertRows(QModelIndex(), start, start + len(games) - 1)
        self._games.extend(games)
        self.endInsertRows()

    def get_game_at(self, index: QModelIndex) -> Optional[Dict[str, Any]]:
        """Get game data at model index"""
        return self.data(index, CoverRoles.GameData)


class CoverItemDelegate(QStyledItemDelegate):
    """Custom delegate for cover grid items

    Layout:
    ┌──────────────────┐
    │                  │
    │   Cover Image    │  <- 2:3 aspect ratio
    │   (placeholder)  │
    │                  │
    │ [S][G]           │  <- Store badges (bottom-left)
    └──────────────────┘
      Game Title         <- Below cover, centered
    """

    PADDING = 8
    TITLE_HEIGHT = 24
    BADGE_SIZE = 16
    BADGE_PADDING = 2
    GAME_MODE_BADGE_SIZE = 16  # Same size as store badges (icon glyphs)

    def __init__(
        self,
        cover_width: int = 150,
        image_cache: Optional[ImageCache] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._cover_width = cover_width
        self._image_cache = image_cache

        # Use system font as base, with relative sizing
        base_size = QApplication.instance().font().pointSize()
        if base_size <= 0:
            base_size = 10  # Fallback

        self._title_font = QFont()
        self._title_font.setPointSize(base_size)  # System default for titles

        self._badge_font = QFont()
        self._badge_font.setPointSize(max(7, base_size - 3))  # Smaller for badges
        self._badge_font.setBold(True)

        self._game_mode_font = QFont()
        self._game_mode_font.setPointSize(max(7, base_size - 3))  # Compat badges (ProtonDB, Deck)
        self._game_mode_font.setBold(True)

        # Favorite star color — initialized from theme variable default,
        # updated via update_theme_colors() when theme changes
        self._fav_color = QColor("#f1c40f")

        # Fade-in animation state
        self._loading_urls: set = set()   # URLs that returned None (image loading)
        self._fade_start: dict = {}       # URL → monotonic timestamp when image appeared
        self.FADE_DURATION = DEFAULT_IMAGE_FADE_MS / 1000.0

        # Cover scaling mode: "none" | "stretch" | "fill"
        self._cover_scaling = "none"

        # Local cache of plugin data (avoids classmethod dispatch in paint())
        self._brand_colors: Dict[str, Dict[str, str]] = dict(PluginManager._brand_colors)

        # Delegate visual config (overridable per-theme via set_delegate_config)
        self._image_radius = 6
        self._border_radius = 4
        self._hover_border_width = 2

    def refresh_plugin_data(self) -> None:
        """Re-snapshot PluginManager brand colors after plugin changes."""
        self._brand_colors = dict(PluginManager._brand_colors)

    def update_theme_colors(self, fav_color: str) -> None:
        """Update delegate colors from current theme."""
        self._fav_color = QColor(fav_color)

    def has_active_fades(self) -> bool:
        """Check if any fade-in animations are in progress."""
        return bool(self._fade_start)

    def set_fade_duration(self, duration_ms: int) -> None:
        """Set fade-in duration in milliseconds (0 = disabled)."""
        self.FADE_DURATION = duration_ms / 1000.0

    def set_cover_scaling(self, mode: str) -> None:
        """Set cover scaling mode: 'none', 'stretch', or 'fill'."""
        self._cover_scaling = mode

    def set_delegate_config(self, config: dict) -> None:
        """Set theme-specific visual config for painting."""
        self._image_radius = config.get("image_radius", 6)
        self._border_radius = config.get("border_radius", 4)
        self._hover_border_width = config.get("hover_border_width", 2)

    def set_image_cache(self, cache: ImageCache) -> None:
        """Set the image cache for loading covers"""
        self._image_cache = cache

    def set_cover_width(self, width: int) -> None:
        """Set cover width (height = width * 1.5)"""
        self._cover_width = max(80, min(400, width))

    def get_cover_width(self) -> int:
        """Get current cover width"""
        return self._cover_width

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Return size for each item"""
        cover_height = int(self._cover_width * 1.5)  # 2:3 aspect ratio
        total_height = cover_height + self.TITLE_HEIGHT + self.PADDING
        total_width = self._cover_width + self.PADDING * 2
        return QSize(total_width, total_height)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Get data
        title = index.data(CoverRoles.Title) or _("Unknown")
        stores = index.data(CoverRoles.Stores) or []
        is_favorite = index.data(CoverRoles.IsFavorite) or False
        is_installed = index.data(CoverRoles.IsInstalled) or False
        game_modes = index.data(CoverRoles.GameModes) or []
        game = index.data(CoverRoles.GameData) or {}
        protondb_tier = index.data(CoverRoles.ProtonDbTier) or ""
        steam_deck_compat = index.data(CoverRoles.SteamDeckCompat) or ""
        cover_url = index.data(CoverRoles.CoverUrl) or ""

        # Calculate dimensions
        cover_height = int(self._cover_width * 1.5)

        # Cover rect (centered horizontally in item)
        cover_x = option.rect.x() + (option.rect.width() - self._cover_width) // 2
        cover_y = option.rect.y() + self.PADDING // 2
        cover_rect = QRect(cover_x, cover_y, self._cover_width, cover_height)

        # Draw selection/hover background
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            # Subtle hover effect
            hover_color = option.palette.highlight().color()
            hover_color.setAlpha(40)
            painter.fillRect(option.rect, hover_color)

        # Draw cover background
        painter.setPen(QPen(option.palette.mid().color(), 1))
        painter.setBrush(option.palette.base())
        painter.drawRoundedRect(cover_rect, self._border_radius, self._border_radius)

        # Try to draw cover image
        cover_drawn = False
        if self._image_cache and cover_url:
            pixmap = self._image_cache.get_image(cover_url)
            if pixmap and not pixmap.isNull():
                # Fade-in: track transition from loading → available
                opacity = 1.0
                if cover_url in self._loading_urls:
                    self._loading_urls.discard(cover_url)
                    self._fade_start[cover_url] = time.monotonic()
                if cover_url in self._fade_start:
                    elapsed = time.monotonic() - self._fade_start[cover_url]
                    if elapsed < self.FADE_DURATION:
                        opacity = elapsed / self.FADE_DURATION
                    else:
                        del self._fade_start[cover_url]

                # Calculate destination rect based on scaling mode
                pw, ph = pixmap.width(), pixmap.height()
                tw, th = cover_rect.width(), cover_rect.height()

                if self._cover_scaling == "stretch":
                    dest_rect = QRect(cover_rect.x(), cover_rect.y(), tw, th)
                elif self._cover_scaling == "fill":
                    scale = max(tw / pw, th / ph)
                    sw = int(pw * scale)
                    sh = int(ph * scale)
                    dest_x = cover_rect.x() + (tw - sw) // 2
                    dest_y = cover_rect.y() + (th - sh) // 2
                    dest_rect = QRect(dest_x, dest_y, sw, sh)
                else:
                    # "none" — fit inside, preserving aspect ratio
                    scale = min(tw / pw, th / ph)
                    sw = int(pw * scale)
                    sh = int(ph * scale)
                    dest_x = cover_rect.x() + (tw - sw) // 2
                    dest_y = cover_rect.y() + (th - sh) // 2
                    dest_rect = QRect(dest_x, dest_y, sw, sh)

                # Draw pixmap (clip for fill mode to prevent overflow)
                painter.save()
                if self._cover_scaling == "fill":
                    painter.setClipRect(cover_rect)
                if opacity < 1.0:
                    painter.setOpacity(opacity)
                painter.drawPixmap(dest_rect, pixmap)
                painter.restore()
                cover_drawn = True
            else:
                # Image loading — track for fade-in when it arrives
                self._loading_urls.add(cover_url)
                if len(self._loading_urls) > 500:
                    self._loading_urls.clear()

        # Show "No Cover" only when there's genuinely no cover URL.
        # When URL exists but image is loading, just show the blank card.
        if not cover_drawn and not cover_url:
            painter.setPen(option.palette.mid().color())
            painter.setFont(self._title_font)
            painter.drawText(cover_rect, Qt.AlignmentFlag.AlignCenter, _("No Cover"))

        # Draw cover border on hover
        if option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(QPen(option.palette.highlight().color(), self._hover_border_width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(cover_rect.adjusted(1, 1, -1, -1), self._border_radius, self._border_radius)

        # Draw store badges (bottom-left of cover)
        badge_x = cover_rect.x() + 4
        badge_y = cover_rect.bottom() - self.BADGE_SIZE - 4
        painter.setFont(self._badge_font)

        for store in stores[:3]:  # Max 3 badges
            colors = self._brand_colors.get(store, _DEFAULT_BRAND_COLORS)

            badge_rect = QRect(badge_x, badge_y, self.BADGE_SIZE, self.BADGE_SIZE)
            draw_badge(painter, badge_rect, colors["bg"], colors["text"], store[0].upper())

            badge_x += self.BADGE_SIZE + self.BADGE_PADDING

        # Draw favorite star (after badges)
        if is_favorite:
            star_rect = QRect(badge_x, badge_y, self.BADGE_SIZE, self.BADGE_SIZE)
            painter.setPen(self._fav_color)
            painter.drawText(star_rect, Qt.AlignmentFlag.AlignCenter, "★")
            badge_x += self.BADGE_SIZE + self.BADGE_PADDING

        # Draw family license count indicator (circle badge, only for 2+ licenses)
        family_license_count = game.get("family_license_count", 0)
        if family_license_count >= 2:
            circle_size = 14
            cx = badge_x + circle_size // 2
            cy = badge_y + circle_size // 2
            draw_license_circle(painter, cx, cy, circle_size, str(family_license_count))
            badge_x += circle_size + self.BADGE_PADDING

        # Installed badge
        if is_installed:
            inst_text = _(INSTALLED_BADGE_LABEL)
            fm = QFontMetrics(self._badge_font)
            text_width = fm.horizontalAdvance(inst_text)
            badge_width = text_width + 10
            inst_rect = QRect(badge_x, badge_y, badge_width, self.BADGE_SIZE)
            draw_badge(painter, inst_rect, INSTALLED_BADGE_COLOR["bg"], INSTALLED_BADGE_COLOR["text"], inst_text)

        # Draw game mode badges (bottom-right of cover, icon glyphs with player counts)
        if game_modes:
            displayable = [m for m in game_modes if m in GAME_MODE_LABELS]
            if displayable:
                mode_badge_x = cover_rect.right() - 4  # Start from right edge
                mode_badge_y = cover_rect.bottom() - self.GAME_MODE_BADGE_SIZE - 4
                badge_side = self.GAME_MODE_BADGE_SIZE

                for mode_name in reversed(displayable):  # Rightmost first
                    player_count = get_player_count(game, mode_name)
                    badge_w = game_mode_badge_width(mode_name, badge_side, player_count)
                    if badge_w == 0:
                        badge_w = badge_side
                    mode_badge_x -= badge_w
                    badge_rect = QRect(mode_badge_x, mode_badge_y, badge_w, badge_side)
                    drawn = draw_icon_badge(painter, badge_rect, mode_name, player_count)
                    if drawn == 0:
                        # Fallback to text if icon missing
                        painter.setFont(self._game_mode_font)
                        fm = QFontMetrics(self._game_mode_font)
                        label = _(GAME_MODE_LABELS[mode_name])
                        text_width = fm.horizontalAdvance(label)
                        badge_width = text_width + 8
                        mode_badge_x += badge_w - badge_width  # Adjust for wider text
                        badge_rect = QRect(mode_badge_x, mode_badge_y, badge_width, badge_side)
                        palette = QApplication.palette()
                        badge_bg = palette.color(QPalette.ColorRole.Mid).name()
                        badge_fg = palette.color(QPalette.ColorRole.WindowText).name()
                        draw_badge(painter, badge_rect, badge_bg, badge_fg, label)
                    mode_badge_x -= self.BADGE_PADDING

        # Draw compatibility badges (bottom-right of cover, above game mode badges)
        compat_badges = []
        if protondb_tier and protondb_tier in PROTONDB_TIER_LABELS:
            compat_badges.append((
                PROTONDB_TIER_LABELS[protondb_tier],
                PROTONDB_TIER_COLORS[protondb_tier],
            ))
        if steam_deck_compat and steam_deck_compat in STEAM_DECK_LABELS:
            compat_badges.append((
                STEAM_DECK_LABELS[steam_deck_compat],
                STEAM_DECK_COLORS[steam_deck_compat],
            ))
        if compat_badges:
            painter.setFont(self._game_mode_font)
            fm = QFontMetrics(self._game_mode_font)
            compat_x = cover_rect.right() - 4
            # Place above game mode row (or at bottom if no game modes)
            compat_y = cover_rect.bottom() - self.GAME_MODE_BADGE_SIZE - 4
            if game_modes and any(m in GAME_MODE_LABELS for m in game_modes):
                compat_y -= self.GAME_MODE_BADGE_SIZE + self.BADGE_PADDING
            for label, colors in reversed(compat_badges):
                translated = _(label)
                text_width = fm.horizontalAdvance(translated)
                badge_width = text_width + 8
                compat_x -= badge_width
                badge_rect = QRect(compat_x, compat_y, badge_width, self.GAME_MODE_BADGE_SIZE)
                draw_badge(painter, badge_rect, colors["bg"], colors["text"], translated)
                compat_x -= self.BADGE_PADDING

        # Draw title below cover
        title_rect = QRect(
            option.rect.x() + self.PADDING,
            cover_rect.bottom() + 4,
            option.rect.width() - self.PADDING * 2,
            self.TITLE_HEIGHT
        )

        # Title color based on selection
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(option.palette.highlightedText().color())
        else:
            painter.setPen(option.palette.text().color())

        painter.setFont(self._title_font)
        fm = QFontMetrics(self._title_font)
        elided_title = fm.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided_title)

        painter.restore()

    def helpEvent(
        self,
        event: QHelpEvent,
        view: QAbstractItemView,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        """Show tooltip — full title if truncated, otherwise model tooltip."""
        if event.type() == QEvent.Type.ToolTip:
            title = index.data(CoverRoles.Title) or ""
            if not title:
                return False

            # Check if title would be truncated
            title_width = option.rect.width() - self.PADDING * 2
            fm = QFontMetrics(self._title_font)
            elided = fm.elidedText(title, Qt.TextElideMode.ElideRight, title_width)

            if elided != title:
                # Title is truncated — show full title + model tooltip content
                tooltip = index.data(Qt.ItemDataRole.ToolTipRole) or title
                QToolTip.showText(event.globalPos(), tooltip, view)
                return True

            # Not truncated — fall through to model tooltip (title, stores, description)

        return super().helpEvent(event, view, option, index)


class CoverView(QWidget):
    """Grid view of game covers using QListView for virtual scrolling

    Signals:
        game_selected: Emitted when a cover is double-clicked (switches to list view)
        game_launched: Emitted when cover is launched from context menu
        view_cover_requested: Emitted when a cover is clicked (opens fullscreen)
    """

    game_selected = Signal(str)  # game_id
    game_launched = Signal(str, str)  # game_id, store
    view_cover_requested = Signal(str)  # game_id
    context_menu_requested = Signal(object, object)  # game_data, global_pos
    density_changed = Signal(int)  # emitted on Ctrl+Scroll zoom

    # Default cover size
    DEFAULT_COVER_WIDTH = 150

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("coverView")

        self._cover_width = self.DEFAULT_COVER_WIDTH
        self._image_cache = get_cover_cache()

        # Callback for fetching covers on-demand (game_id -> cover_url)
        self._cover_callback: Optional[Callable[[str], str]] = None
        # Track which games we've already requested covers for (avoid duplicates)
        self._pending_cover_requests: Set[str] = set()
        # Games that have been checked and have no cover (don't retry)
        self._no_cover_games: Set[str] = set()
        # Active worker threads (limit concurrency)
        self._active_workers: List[CoverFetchWorker] = []
        self._request_queue: List[str] = []
        self._max_concurrent_workers = 4  # Limit parallel fetches

        self._setup_ui()
        self._connect_signals()

        # Coalesce viewport repaints: multiple image_loaded signals within
        # one frame (~16ms) trigger only a single viewport().update()
        self._viewport_update_timer = QTimer(self)
        self._viewport_update_timer.setSingleShot(True)
        self._viewport_update_timer.setInterval(16)  # ~60fps
        self._viewport_update_timer.timeout.connect(self._coalesced_viewport_update)

        # Scroll-aware image loading: gate disk I/O during fast scroll
        self._scroll_debounce_timer = QTimer(self)
        self._scroll_debounce_timer.setSingleShot(True)
        self._scroll_debounce_timer.setInterval(50)  # 50ms debounce
        self._scroll_debounce_timer.timeout.connect(self._on_scroll_settled)

        # Fade animation timer: drives repaints during active fade-ins
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)  # ~60fps
        self._fade_timer.timeout.connect(self._on_fade_tick)

    def _setup_ui(self) -> None:
        """Create cover view layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # List view in icon mode for grid display
        self.list_view = QListView()
        self.list_view.setObjectName("coverGridView")
        self.list_view.setViewMode(QListView.ViewMode.IconMode)
        self.list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.list_view.setMovement(QListView.Movement.Static)
        self.list_view.setWrapping(True)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSpacing(8)

        # Enable mouse tracking for hover effects and tooltips
        self.list_view.setMouseTracking(True)
        self.list_view.viewport().setMouseTracking(True)

        # Selection and scrolling
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Enable context menu
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # Model
        self.model = CoverListModel()
        self.list_view.setModel(self.model)

        # Delegate with image cache
        self.delegate = CoverItemDelegate(self._cover_width, self._image_cache)
        self.list_view.setItemDelegate(self.delegate)

        layout.addWidget(self.list_view)

        # Ctrl+Scroll zoom — event filter on viewport for wheel events
        self.list_view.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        """Handle Ctrl+Scroll for grid density zoom."""
        if obj is self.list_view.viewport() and event.type() == event.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta != 0:
                    step = 20 if delta > 0 else -20
                    new_density = self._cover_width + step
                    self.set_density(new_density)
                    self.density_changed.emit(self._cover_width)
                return True
        return super().eventFilter(obj, event)

    def _connect_signals(self) -> None:
        """Connect signals"""
        # currentChanged covers: mouse click (when item changes), keyboard nav.
        # clicked was redundant and caused duplicate game_selected emissions.
        self.list_view.selectionModel().currentChanged.connect(
            lambda current, _prev: self._on_item_clicked(current)
        )
        self.list_view.doubleClicked.connect(self._on_item_double_clicked)
        self.list_view.customContextMenuRequested.connect(self._on_context_menu_requested)

        # Repaint when images load
        self._image_cache.image_loaded.connect(self._on_image_loaded)

        # Scroll-aware image loading
        self.list_view.verticalScrollBar().valueChanged.connect(self._on_scroll_started)

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle image loaded - coalesce repaints and start fade animation"""
        if not self.isVisible():
            return
        self._viewport_update_timer.start()
        if not self._fade_timer.isActive():
            self._fade_timer.start()

    def _on_fade_tick(self) -> None:
        """Drive fade-in animation repaints at ~60fps."""
        if self.delegate.has_active_fades():
            self.list_view.viewport().update()
        else:
            self._fade_timer.stop()

    def _coalesced_viewport_update(self) -> None:
        """Single viewport repaint after all pending image_loaded signals settle."""
        self.list_view.viewport().update()

    def _on_scroll_started(self) -> None:
        """Scrollbar moved — gate disk I/O until scroll settles."""
        self._image_cache.set_scroll_active(True)
        self._scroll_debounce_timer.start()

    def _on_scroll_settled(self) -> None:
        """Scroll debounce expired — re-enable disk loads and repaint.

        The repaint triggers get_image() for visible items, which queues async
        disk loads (no longer blocks UI thread with synchronous reads).
        """
        self._image_cache.set_scroll_active(False)
        self.list_view.viewport().update()

    def _on_item_clicked(self, index: QModelIndex) -> None:
        """Handle item click - switches to list view and selects game"""
        game_id = index.data(CoverRoles.GameId)
        if game_id:
            self.game_selected.emit(game_id)

    def _on_item_double_clicked(self, index: QModelIndex) -> None:
        """Handle double click - opens fullscreen cover viewer"""
        game_id = index.data(CoverRoles.GameId)
        if game_id:
            self.view_cover_requested.emit(game_id)

    def _on_context_menu_requested(self, pos) -> None:
        """Handle right-click context menu request"""
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        game_data = index.data(CoverRoles.GameData)
        if game_data:
            global_pos = self.list_view.viewport().mapToGlobal(pos)
            self.context_menu_requested.emit(game_data, global_pos)

    def _on_launch_from_context(self, game_id: str, stores: list) -> None:
        """Handle launch from context menu (preserves old double-click behavior)"""
        if game_id and stores:
            self.game_launched.emit(game_id, stores[0])

    def append_games(self, games: List[Dict[str, Any]]) -> None:
        """Append games incrementally during progressive loading."""
        if not games:
            return
        self.model.append_games(games)
        # Extend lookup table
        start = len(self._game_id_to_row)
        for i, game in enumerate(games):
            game_id = game.get("id")
            if game_id:
                self._game_id_to_row[game_id] = start + i

    def set_games(self, games: List[Dict[str, Any]]) -> None:
        """Set games to display

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

        # Build game_id → row lookup for O(1) select_game()
        self._game_id_to_row: dict[str, int] = {}
        for row, game in enumerate(games):
            game_id = game.get("id")
            if game_id:
                self._game_id_to_row[game_id] = row

        # Queue cover fetches for games without covers (deferred to not block)
        if self._cover_callback:
            QTimer.singleShot(100, lambda: self._queue_missing_covers(games))

    def _queue_missing_covers(self, games: List[Dict[str, Any]]) -> None:
        """Queue cover fetches for games that don't have covers."""
        if not self._cover_callback:
            return

        for game in games:
            game_id = game.get("id")
            if not game_id:
                continue

            # Check if game has a cover
            cover = game.get("cover_image")
            if cover:
                continue

            # Skip if already processed
            if game_id in self._pending_cover_requests:
                continue
            if game_id in self._no_cover_games:
                continue

            # Add to request queue
            self._pending_cover_requests.add(game_id)
            self._request_queue.append(game_id)

        # Start processing
        self._process_request_queue()

    def set_density(self, density: int) -> None:
        """Set grid density (cover width)

        Args:
            density: Cover width in pixels
        """
        self._cover_width = max(80, min(400, density))
        self.delegate.set_cover_width(self._cover_width)

        # Force layout update
        self.list_view.doItemsLayout()

    def get_density(self) -> int:
        """Get current grid density (cover width)"""
        return self._cover_width

    def select_game(self, game_id: str) -> None:
        """Highlight a game in the grid

        Args:
            game_id: Game ID to select
        """
        row = getattr(self, '_game_id_to_row', {}).get(game_id)
        if row is not None and row < self.model.rowCount():
            index = self.model.index(row, 0)
            self.list_view.setCurrentIndex(index)
            self.list_view.doItemsLayout()
            self.list_view.scrollTo(index)

    def clear(self) -> None:
        """Clear all covers"""
        self.model.set_games([])
        self._pending_cover_requests.clear()
        self._request_queue.clear()
        self._no_cover_games.clear()

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """Shutdown cover view and wait for workers to complete.

        Call this before destroying the view to prevent thread crashes.

        Args:
            timeout_ms: Max time to wait for each worker (default 2 seconds)
        """
        # Clear queues to prevent new work
        self._request_queue.clear()
        self._pending_cover_requests.clear()

        # Wait for active workers to finish
        for worker in list(self._active_workers):
            if worker.isRunning():
                logger.debug("Waiting for cover fetch worker to finish...")
                worker.wait(timeout_ms)
                if worker.isRunning():
                    logger.warning("Cover fetch worker did not finish in time")
                    worker.terminate()
                    worker.wait(1000)

        self._active_workers.clear()
        logger.debug("CoverView shutdown complete")

    def set_cover_callback(self, callback: Callable[[str], str]) -> None:
        """Set callback for fetching covers on-demand.

        The callback receives a game_id and should return a cover URL.
        Used as fallback when game has no cover from store metadata.

        Args:
            callback: Function(game_id: str) -> str (cover URL)
        """
        self._cover_callback = callback

    def _process_request_queue(self) -> None:
        """Start workers for pending requests up to concurrency limit."""
        # Clean up finished workers
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

        while self._request_queue and len(self._active_workers) < self._max_concurrent_workers:
            game_id = self._request_queue.pop(0)
            self._start_worker(game_id)

    def _start_worker(self, game_id: str) -> None:
        """Start a worker thread to fetch cover."""
        worker = CoverFetchWorker(game_id, self._cover_callback)
        worker.cover_fetched.connect(self._on_cover_fetched)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._active_workers.append(worker)
        worker.start()

    def _cleanup_worker(self, worker: CoverFetchWorker) -> None:
        """Clean up finished worker and process queue."""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.deleteLater()

        # Process more queued requests
        self._process_request_queue()

    def _on_cover_fetched(self, game_id: str, cover_url: str) -> None:
        """Handle cover fetched from background thread."""
        self._pending_cover_requests.discard(game_id)
        if cover_url:
            self._update_game_cover(game_id, cover_url)
        else:
            # Mark as having no cover so we don't retry
            self._no_cover_games.add(game_id)

    def _update_game_cover(self, game_id: str, cover_url: str) -> None:
        """Update a game's cover in the model and trigger repaint.

        Args:
            game_id: Game UUID
            cover_url: New cover URL
        """
        # Find and update the game in model data
        for i, game in enumerate(self.model._games):
            if game.get("id") == game_id:
                game["cover_image"] = cover_url
                # Emit dataChanged to trigger repaint for this item
                index = self.model.index(i, 0)
                self.model.dataChanged.emit(index, index, [CoverRoles.CoverUrl])
                logger.debug(f"Updated cover for {game_id}: {cover_url[:50]}...")
                break

    def update_game_covers(self, modified: Dict[str, Dict[str, str]]) -> None:
        """Batch-update covers for specific games without model reset.

        Uses _game_id_to_row for O(1) index lookup per game.

        Args:
            modified: Dict mapping game_id to changed fields,
                      e.g. ``{"uuid": {"cover": "https://..."}}``.
        """
        lookup = getattr(self, '_game_id_to_row', {})
        for game_id, updates in modified.items():
            cover_url = updates.get("cover")
            if cover_url is None:
                continue
            row = lookup.get(game_id)
            if row is not None and row < len(self.model._games):
                self.model._games[row]["cover_image"] = cover_url
                index = self.model.index(row, 0)
                self.model.dataChanged.emit(index, index, [CoverRoles.CoverUrl])
