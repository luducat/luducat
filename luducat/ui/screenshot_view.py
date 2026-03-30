# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# screenshot_view.py

"""Screenshot view (grid) for luducat

Grid display of game screenshots in 16:9 landscape format.
Uses QListView in IconMode with custom delegate for virtual scrolling.
"""

import html
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set

from PySide6.QtCore import (
    Qt,
    Signal,
    QSize,
    QRect,
    QModelIndex,
    QObject,
    QAbstractListModel,
    QThread,
    QTimer,
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
    CORNER_DEMO_COLORS,
    CORNER_FREE_COLORS,
    CORNER_TRIANGLE_SIZE,
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
from ..utils.image_cache import get_screenshot_cache, ImageCache
from .badge_painter import draw_badge, draw_icon_badge, draw_store_icon_badge, draw_license_circle, draw_corner_triangle, draw_overflow_pill, draw_status_dot, get_player_count, game_mode_badge_width, store_badge_width, STATUS_DOT_PRIVATE, STATUS_DOT_DELISTED

logger = logging.getLogger(__name__)


def _corner_tooltip(title: str) -> str:
    """Return tooltip text for demo/prologue/trial based on title suffix."""
    lower = title.lower().rstrip()
    if lower.endswith("prologue"):
        return _("Prologue")
    if lower.endswith("trial"):
        return _("Trial")
    return _("Demo")


class ScreenshotFetchWorker(QThread):
    """Worker thread for fetching screenshots from plugins."""

    screenshots_fetched = Signal(str, list)  # game_id, screenshot URLs (empty if not found)

    def __init__(
        self,
        game_id: str,
        callback: Callable[[str], list],
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._game_id = game_id
        self._callback = callback

    def run(self) -> None:
        """Fetch screenshots in background thread."""
        try:
            screenshots = self._callback(self._game_id)
            self.screenshots_fetched.emit(self._game_id, screenshots or [])
        except Exception as e:
            logger.debug(f"Failed to fetch screenshots for {self._game_id}: {e}")
            self.screenshots_fetched.emit(self._game_id, [])


# Custom roles for game data
class ScreenshotRoles:
    GameId = Qt.ItemDataRole.UserRole + 1
    Title = Qt.ItemDataRole.UserRole + 2
    Stores = Qt.ItemDataRole.UserRole + 3
    IsFavorite = Qt.ItemDataRole.UserRole + 4
    GameData = Qt.ItemDataRole.UserRole + 5
    ScreenshotUrl = Qt.ItemDataRole.UserRole + 6
    IsFamilyShared = Qt.ItemDataRole.UserRole + 7
    GameModes = Qt.ItemDataRole.UserRole + 8  # List of IGDB game mode names
    ProtonDbTier = Qt.ItemDataRole.UserRole + 9
    SteamDeckCompat = Qt.ItemDataRole.UserRole + 10
    IsInstalled = Qt.ItemDataRole.UserRole + 11


class ScreenshotListModel(QAbstractListModel):
    """Model for screenshot grid with virtual scrolling support"""

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

        if role == Qt.ItemDataRole.DisplayRole or role == ScreenshotRoles.Title:
            return game.get("title", _("Unknown"))
        elif role == ScreenshotRoles.GameId:
            return game.get("id", "")
        elif role == ScreenshotRoles.Stores:
            return game.get("stores", [])
        elif role == ScreenshotRoles.IsFavorite:
            return game.get("is_favorite", False)
        elif role == ScreenshotRoles.IsFamilyShared:
            return game.get("is_family_shared", False)
        elif role == ScreenshotRoles.GameModes:
            return game.get("game_modes", [])
        elif role == ScreenshotRoles.ProtonDbTier:
            return game.get("protondb_rating", "")
        elif role == ScreenshotRoles.SteamDeckCompat:
            return game.get("steam_deck_compat", "")
        elif role == ScreenshotRoles.IsInstalled:
            return game.get("is_installed", False)
        elif role == ScreenshotRoles.GameData:
            return game
        elif role == ScreenshotRoles.ScreenshotUrl:
            screenshots = game.get("screenshots", [])
            return screenshots[0] if screenshots else ""
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
            screenshots = game.get("screenshots", [])
            ss_url = screenshots[0] if screenshots else ""
            confidence = game.get("adult_confidence", 0.0)
            tooltip += (
                f"<br/><br/>--- debug ---<br/>"
                f"screenshot: {html.escape(ss_url) if ss_url else 'none'}<br/>"
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
        return self.data(index, ScreenshotRoles.GameData)


class ScreenshotItemDelegate(QStyledItemDelegate):
    """Custom delegate for screenshot grid items

    Layout:
    ┌────────────────────────────────┐
    │                                │
    │       Screenshot Image         │  <- 16:9 aspect ratio
    │       (placeholder)            │
    │                                │
    │ [S][G]                         │  <- Store badges (bottom-left)
    └────────────────────────────────┘
           Game Title                  <- Below screenshot, centered
    """

    PADDING = 8
    TITLE_HEIGHT = 24
    BADGE_SIZE = 16
    STORE_BADGE_SIZE = 16
    BADGE_PADDING = 2
    GAME_MODE_BADGE_SIZE = 16

    def __init__(
        self,
        item_width: int = 280,
        image_cache: Optional[ImageCache] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._item_width = item_width
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

        # Local cache of plugin data (avoids classmethod dispatch in paint())
        self._brand_colors: Dict[str, Dict[str, str]] = dict(PluginManager._brand_colors)
        self._badge_labels: Dict[str, str] = dict(PluginManager._badge_labels)

        # Badge visibility (toggled from Settings → Appearance)
        self._show_game_mode_badges = True
        self._show_store_badges = True
        self._default_store = ""

        # Delegate visual config (overridable per-theme via set_delegate_config)
        self._image_radius = 6
        self._border_radius = 4
        self._hover_border_width = 2

    def refresh_plugin_data(self) -> None:
        """Re-snapshot PluginManager brand colors and badge labels after plugin changes."""
        self._brand_colors = dict(PluginManager._brand_colors)
        self._badge_labels = dict(PluginManager._badge_labels)

    def update_theme_colors(self, fav_color: str) -> None:
        """Update delegate colors from current theme."""
        self._fav_color = QColor(fav_color)

    def has_active_fades(self) -> bool:
        """Check if any fade-in animations are in progress."""
        return bool(self._fade_start)

    def set_fade_duration(self, duration_ms: int) -> None:
        """Set fade-in duration in milliseconds (0 = disabled)."""
        self.FADE_DURATION = duration_ms / 1000.0

    def set_badge_visibility(self, game_modes: bool, stores: bool) -> None:
        """Toggle badge types on screenshots."""
        self._show_game_mode_badges = game_modes
        self._show_store_badges = stores

    def set_default_store(self, store: str) -> None:
        """Set default store for primary badge display."""
        self._default_store = store

    def set_delegate_config(self, config: dict) -> None:
        """Set theme-specific visual config for painting."""
        self._image_radius = config.get("image_radius", 6)
        self._border_radius = config.get("border_radius", 4)
        self._hover_border_width = config.get("hover_border_width", 2)

    def set_image_cache(self, cache: ImageCache) -> None:
        """Set the image cache for loading screenshots"""
        self._image_cache = cache

    def set_item_width(self, width: int) -> None:
        """Set screenshot width (height = width * 9/16)"""
        self._item_width = max(150, min(500, width))

    def get_item_width(self) -> int:
        """Get current item width"""
        return self._item_width

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        """Return size for each item"""
        screenshot_height = int(self._item_width * 9 / 16)  # 16:9 aspect ratio
        total_height = screenshot_height + self.TITLE_HEIGHT + self.PADDING
        total_width = self._item_width + self.PADDING * 2
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
        title = index.data(ScreenshotRoles.Title) or _("Unknown")
        stores = index.data(ScreenshotRoles.Stores) or []
        is_favorite = index.data(ScreenshotRoles.IsFavorite) or False
        is_installed = index.data(ScreenshotRoles.IsInstalled) or False
        game_modes = index.data(ScreenshotRoles.GameModes) or []
        game = index.data(ScreenshotRoles.GameData) or {}
        protondb_tier = index.data(ScreenshotRoles.ProtonDbTier) or ""
        steam_deck_compat = index.data(ScreenshotRoles.SteamDeckCompat) or ""
        screenshot_url = index.data(ScreenshotRoles.ScreenshotUrl) or ""

        # Calculate dimensions
        screenshot_height = int(self._item_width * 9 / 16)

        # Screenshot rect (centered horizontally in item)
        ss_x = option.rect.x() + (option.rect.width() - self._item_width) // 2
        ss_y = option.rect.y() + self.PADDING // 2
        ss_rect = QRect(ss_x, ss_y, self._item_width, screenshot_height)

        # Draw selection/hover background
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            # Subtle hover effect
            hover_color = option.palette.highlight().color()
            hover_color.setAlpha(40)
            painter.fillRect(option.rect, hover_color)

        # Draw screenshot background
        painter.setPen(QPen(option.palette.mid().color(), 1))
        painter.setBrush(option.palette.base())
        painter.drawRoundedRect(ss_rect, self._border_radius, self._border_radius)

        # Try to draw screenshot image
        screenshot_drawn = False
        if self._image_cache and screenshot_url:
            pixmap = self._image_cache.get_image(screenshot_url)
            if pixmap and not pixmap.isNull():
                # Fade-in: track transition from loading → available
                opacity = 1.0
                if screenshot_url in self._loading_urls:
                    self._loading_urls.discard(screenshot_url)
                    self._fade_start[screenshot_url] = time.monotonic()
                if screenshot_url in self._fade_start:
                    elapsed = time.monotonic() - self._fade_start[screenshot_url]
                    if elapsed < self.FADE_DURATION:
                        opacity = elapsed / self.FADE_DURATION
                    else:
                        del self._fade_start[screenshot_url]

                # Calculate source crop rect (KeepAspectRatioByExpanding)
                # without allocating a new QPixmap — painter handles scaling
                pw, ph = pixmap.width(), pixmap.height()
                tw, th = ss_rect.width(), ss_rect.height()
                scale = max(tw / pw, th / ph)
                # Source region in original pixmap coordinates
                crop_w = int(tw / scale)
                crop_h = int(th / scale)
                src_x = (pw - crop_w) // 2
                src_y = (ph - crop_h) // 2
                src_rect = QRect(src_x, src_y, crop_w, crop_h)

                # Draw pre-rounded pixmap (corners composited at cache insertion
                # time, eliminating per-paint QPainterPath allocation)
                painter.save()
                if opacity < 1.0:
                    painter.setOpacity(opacity)
                painter.drawPixmap(ss_rect, pixmap, src_rect)
                painter.restore()
                screenshot_drawn = True
            else:
                # Image loading — track for fade-in when it arrives
                self._loading_urls.add(screenshot_url)
                if len(self._loading_urls) > 500:
                    self._loading_urls.clear()

        # Show "No Screenshot" only when there's genuinely no screenshot URL.
        # When URL exists but image is loading, just show the blank card.
        if not screenshot_drawn and not screenshot_url:
            painter.setPen(option.palette.mid().color())
            painter.setFont(self._title_font)
            painter.drawText(ss_rect, Qt.AlignmentFlag.AlignCenter, _("No Screenshot"))

        # Draw border on hover
        if option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(QPen(option.palette.highlight().color(), self._hover_border_width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(ss_rect.adjusted(1, 1, -1, -1), self._border_radius, self._border_radius)

        # Draw top-left corner triangle for free/demo games
        is_free = game.get("is_free", False)
        is_demo = game.get("is_demo", False)
        if is_demo:
            draw_corner_triangle(painter, ss_rect, _("DEMO"), CORNER_DEMO_COLORS["bg"], CORNER_DEMO_COLORS["text"])
        elif is_free:
            draw_corner_triangle(painter, ss_rect, _("FREE"), CORNER_FREE_COLORS["bg"], CORNER_FREE_COLORS["text"])

        # Draw top-right badges: compat + family license
        tr_x = ss_rect.right() - 2
        tr_y = ss_rect.y() + 2

        # Family license circle (rightmost)
        family_license_count = game.get("family_license_count", 0)
        if family_license_count >= 2:
            circle_size = 14
            tr_x -= circle_size
            cx = tr_x + circle_size // 2
            cy = tr_y + circle_size // 2
            draw_license_circle(painter, cx, cy, circle_size, str(family_license_count))
            tr_x -= self.BADGE_PADDING

        # Compat badges (left of family circle)
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
            for label, colors in reversed(compat_badges):
                translated = _(label)
                text_width = fm.horizontalAdvance(translated)
                badge_width = text_width + 8
                tr_x -= badge_width
                badge_rect = QRect(tr_x, tr_y, badge_width, self.GAME_MODE_BADGE_SIZE)
                draw_badge(painter, badge_rect, colors["bg"], colors["text"], translated)
                tr_x -= self.BADGE_PADDING

        # Status dots (private / delisted) — left of compat badges in top-right
        dot_diameter = 8
        dot_cy = tr_y + dot_diameter // 2
        if game.get("is_delisted", False):
            tr_x -= dot_diameter
            draw_status_dot(painter, tr_x + dot_diameter // 2, dot_cy, dot_diameter, STATUS_DOT_DELISTED)
            tr_x -= self.BADGE_PADDING
        if game.get("is_private_app", False):
            tr_x -= dot_diameter
            draw_status_dot(painter, tr_x + dot_diameter // 2, dot_cy, dot_diameter, STATUS_DOT_PRIVATE)
            tr_x -= self.BADGE_PADDING

        # Draw store badges (bottom-left of screenshot) — primary + overflow
        sb = self.STORE_BADGE_SIZE
        sbw = store_badge_width(sb)
        badge_x = ss_rect.x() + 2
        badge_y = ss_rect.bottom() - sb - 2

        if stores and self._show_store_badges:
            primary = self._default_store if self._default_store in stores else stores[0]
            colors = self._brand_colors.get(primary, _DEFAULT_BRAND_COLORS)
            label = self._badge_labels.get(primary, primary.upper()[:3])
            badge_rect = QRect(badge_x, badge_y, sbw, sb)
            draw_store_icon_badge(
                painter, badge_rect, primary, colors["bg"], colors["text"],
                badge_label=label,
                heart_color=colors.get("heart", ""),
            )
            badge_x += sbw + self.BADGE_PADDING

            overflow = len(stores) - 1
            if overflow > 0:
                badge_x = draw_overflow_pill(
                    painter, badge_x, badge_y, sb,
                    f"+{overflow}", anchor_left=True,
                )
                badge_x += self.BADGE_PADDING

        # Draw favorite star (after badges)
        if is_favorite:
            star_rect = QRect(badge_x, badge_y, self.BADGE_SIZE, self.BADGE_SIZE)
            painter.setPen(self._fav_color)
            painter.drawText(star_rect, Qt.AlignmentFlag.AlignCenter, "★")
            badge_x += self.BADGE_SIZE + self.BADGE_PADDING

        # Installed badge
        if is_installed:
            inst_text = _(INSTALLED_BADGE_LABEL)
            fm = QFontMetrics(self._badge_font)
            text_width = fm.horizontalAdvance(inst_text)
            badge_width = text_width + 10
            inst_rect = QRect(badge_x, badge_y, badge_width, self.BADGE_SIZE)
            draw_badge(painter, inst_rect, INSTALLED_BADGE_COLOR["bg"], INSTALLED_BADGE_COLOR["text"], inst_text)

        # Draw game mode badges (bottom-right, condensed: top 2 + overflow)
        if game_modes and self._show_game_mode_badges:
            displayable = [m for m in game_modes if m in GAME_MODE_LABELS]
            if displayable:
                mode_badge_x = ss_rect.right() - 2
                mode_badge_y = ss_rect.bottom() - self.GAME_MODE_BADGE_SIZE - 2
                badge_side = self.GAME_MODE_BADGE_SIZE

                visible = displayable[:2]
                overflow = len(displayable) - len(visible)

                if overflow > 0:
                    mode_badge_x = draw_overflow_pill(
                        painter, mode_badge_x, mode_badge_y, badge_side,
                        f"+{overflow}",
                    )
                    mode_badge_x -= self.BADGE_PADDING

                for mode_name in reversed(visible):
                    player_count = get_player_count(game, mode_name)
                    badge_w = game_mode_badge_width(mode_name, badge_side, player_count)
                    if badge_w == 0:
                        badge_w = badge_side
                    mode_badge_x -= badge_w
                    badge_rect = QRect(mode_badge_x, mode_badge_y, badge_w, badge_side)
                    drawn = draw_icon_badge(painter, badge_rect, mode_name, player_count)
                    if drawn == 0:
                        painter.setFont(self._game_mode_font)
                        fm = QFontMetrics(self._game_mode_font)
                        label = _(GAME_MODE_LABELS[mode_name])
                        text_width = fm.horizontalAdvance(label)
                        badge_width = text_width + 8
                        mode_badge_x += badge_w - badge_width
                        badge_rect = QRect(mode_badge_x, mode_badge_y, badge_width, badge_side)
                        palette = QApplication.palette()
                        badge_bg = palette.color(QPalette.ColorRole.Mid).name()
                        badge_fg = palette.color(QPalette.ColorRole.WindowText).name()
                        draw_badge(painter, badge_rect, badge_bg, badge_fg, label)
                    mode_badge_x -= self.BADGE_PADDING

        # Draw title below screenshot
        title_rect = QRect(
            option.rect.x() + self.PADDING,
            ss_rect.bottom() + 4,
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
        """Show tooltip — corner triangle, truncated title, or model tooltip."""
        if event.type() == QEvent.Type.ToolTip:
            game = index.data(ScreenshotRoles.GameData)
            if game:
                # Hit-test top-left corner triangle (free/demo badge)
                ss_x = option.rect.x() + (option.rect.width() - self._item_width) // 2
                ss_y = option.rect.y() + self.PADDING // 2
                pos = event.pos()
                rx = pos.x() - ss_x
                ry = pos.y() - ss_y
                if rx >= 0 and ry >= 0 and rx + ry <= CORNER_TRIANGLE_SIZE:
                    is_demo = game.get("is_demo", False)
                    is_free = game.get("is_free", False)
                    if is_demo:
                        QToolTip.showText(event.globalPos(), _corner_tooltip(game.get("title", "")), view)
                        return True
                    elif is_free:
                        QToolTip.showText(event.globalPos(), _("Free to Play"), view)
                        return True

                # Hit-test bottom-right game mode badge region
                game_modes = game.get("game_modes", [])
                if game_modes and self._show_game_mode_badges:
                    ss_h = int(self._item_width * 9 / 16)
                    badge_h = self.GAME_MODE_BADGE_SIZE
                    if (rx > self._item_width // 2
                            and ry > ss_h - badge_h - 6):
                        displayable = [m for m in game_modes if m in GAME_MODE_LABELS]
                        if displayable:
                            lines = []
                            for m in displayable:
                                lbl = _(GAME_MODE_LABELS[m])
                                pc = get_player_count(game, m)
                                lines.append(f"{lbl} ({pc})" if pc else lbl)
                            QToolTip.showText(event.globalPos(), "\n".join(lines), view)
                            return True

                # Hit-test bottom-left store badge region
                stores = game.get("stores", [])
                if stores and self._show_store_badges and len(stores) > 1:
                    sbw = store_badge_width(self.STORE_BADGE_SIZE)
                    ss_h = int(self._item_width * 9 / 16)
                    if (rx < sbw + 30
                            and ry > ss_h - self.STORE_BADGE_SIZE - 6):
                        store_names = [PluginManager.get_store_display_name(s) for s in stores]
                        QToolTip.showText(event.globalPos(), "\n".join(store_names), view)
                        return True

            title = index.data(ScreenshotRoles.Title) or ""
            if not title:
                return False

            # Check if title would be truncated
            title_width = option.rect.width() - self.PADDING * 2
            fm = QFontMetrics(self._title_font)
            elided = fm.elidedText(title, Qt.TextElideMode.ElideRight, title_width)

            if elided != title:
                tooltip = index.data(Qt.ItemDataRole.ToolTipRole) or title
                QToolTip.showText(event.globalPos(), tooltip, view)
                return True

        return super().helpEvent(event, view, option, index)


class ScreenshotView(QWidget):
    """Grid view of game screenshots using QListView for virtual scrolling

    Signals:
        game_selected: Emitted when a screenshot is clicked (switches to detail view)
        view_screenshot_requested: Emitted when a screenshot is clicked (opens fullscreen)
    """

    game_selected = Signal(str)  # game_id
    view_screenshot_requested = Signal(str)  # game_id
    context_menu_requested = Signal(object, object)  # game_data, global_pos
    density_changed = Signal(int)  # emitted on Ctrl+Scroll zoom

    DEFAULT_SCREENSHOT_WIDTH = 280

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("screenshotView")

        self._item_width = self.DEFAULT_SCREENSHOT_WIDTH
        self._image_cache = get_screenshot_cache()
        self._screenshot_callback = None  # Callback to fetch screenshots
        self._screenshot_invalidate_callback = None  # Callback for 404 retry
        self._url_to_game: Dict[str, Dict] = {}  # Map screenshot URL → game data

        # Worker queue (same pattern as CoverView)
        self._pending_requests: Set[str] = set()  # Game IDs queued or in-flight
        self._no_screenshots: Set[str] = set()  # Games confirmed to have no screenshots
        self._active_workers: List[ScreenshotFetchWorker] = []
        self._request_queue: List[str] = []
        self._max_concurrent_workers = 2

        self._load_timer = QTimer()  # Debounce timer for lazy loading
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(100)  # 100ms debounce
        self._load_timer.timeout.connect(self._lazy_load_visible_screenshots)

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
        """Create screenshot view layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # List view in icon mode for grid display
        self.list_view = QListView()
        self.list_view.setObjectName("screenshotGridView")
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
        self.model = ScreenshotListModel()
        self.list_view.setModel(self.model)

        # Delegate with image cache
        self.delegate = ScreenshotItemDelegate(self._item_width, self._image_cache)
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
                    new_density = self._item_width + step
                    self.set_density(new_density)
                    self.density_changed.emit(self._item_width)
                return True
        return super().eventFilter(obj, event)

    def _connect_signals(self) -> None:
        """Connect signals"""
        # currentChanged covers: mouse click (when item changes), keyboard nav.
        # clicked was redundant and caused duplicate game_selected emissions.
        self.list_view.selectionModel().currentChanged.connect(
            lambda current, _prev: self._on_item_clicked(current)
        )
        # Double-click opens fullscreen viewer
        self.list_view.doubleClicked.connect(self._on_item_double_clicked)
        self.list_view.customContextMenuRequested.connect(self._on_context_menu_requested)

        # Repaint when images load
        self._image_cache.image_loaded.connect(self._on_image_loaded)
        # Handle 404s — fall back to next priority source
        self._image_cache.image_not_found.connect(self._on_image_not_found)

        # Trigger lazy loading when scrolling
        self.list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def set_screenshot_callback(self, callback) -> None:
        """Set callback for lazy-loading screenshots.

        The callback should take a game_id and return a list of screenshot URLs.

        Args:
            callback: Function(game_id: str) -> List[str]
        """
        self._screenshot_callback = callback

    def set_screenshot_invalidate_callback(self, callback) -> None:
        """Set callback for retrying screenshots after 404.

        The callback should take (game_id, failed_urls) and return new URLs.

        Args:
            callback: Function(game_id: str, failed_urls: List[str]) -> List[str]
        """
        self._screenshot_invalidate_callback = callback

    def _on_scroll(self) -> None:
        """Handle scroll - trigger lazy load after debounce and gate disk I/O"""
        self._load_timer.start()
        # Gate disk I/O during fast scroll
        self._image_cache.set_scroll_active(True)
        self._scroll_debounce_timer.start()

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle image loaded - coalesce repaints and start fade animation"""
        if not self.isVisible():
            return
        self._viewport_update_timer.start()
        if not self._fade_timer.isActive():
            self._fade_timer.start()

    def _on_image_not_found(self, url: str, error: str) -> None:
        """Handle HTTP 404 — retry screenshot from next priority source."""
        if not self._screenshot_invalidate_callback:
            return

        game = self._url_to_game.pop(url, None)
        if not game:
            return

        game_id = game.get("id")
        if not game_id:
            return

        try:
            new_screenshots = self._screenshot_invalidate_callback(game_id, [url])
            if new_screenshots:
                game["screenshots"] = new_screenshots
                # Track new URL for potential further 404s
                self._url_to_game[new_screenshots[0]] = game
                self.list_view.viewport().update()
        except Exception as e:
            logger.error(f"Screenshot 404 fallback failed for {game_id}: {e}")

    def _on_fade_tick(self) -> None:
        """Drive fade-in animation repaints at ~60fps."""
        if self.delegate.has_active_fades():
            self.list_view.viewport().update()
        else:
            self._fade_timer.stop()

    def _coalesced_viewport_update(self) -> None:
        """Single viewport repaint after all pending image_loaded signals settle."""
        self.list_view.viewport().update()

    def _on_scroll_settled(self) -> None:
        """Scroll debounce expired — re-enable disk loads and repaint.

        The repaint triggers get_image() for visible items, which queues async
        disk loads (no longer blocks UI thread with synchronous reads).
        """
        self._image_cache.set_scroll_active(False)
        self.list_view.viewport().update()

    def _on_item_clicked(self, index: QModelIndex) -> None:
        """Handle item click - selects game"""
        logger.info(f"ScreenshotView._on_item_clicked: index={index.row()}")
        game_id = index.data(ScreenshotRoles.GameId)
        if game_id:
            logger.info(f"ScreenshotView._on_item_clicked: Emitting game_selected for {game_id}")
            self.game_selected.emit(game_id)

    def _on_item_double_clicked(self, index: QModelIndex) -> None:
        """Handle item double-click - opens fullscreen viewer"""
        try:
            logger.info(f"ScreenshotView._on_item_double_clicked: index={index.row()}")
            game_id = index.data(ScreenshotRoles.GameId)
            logger.info(f"ScreenshotView._on_item_double_clicked: game_id={game_id}")
            if game_id:
                logger.info("ScreenshotView._on_item_double_clicked: Emitting view_screenshot_requested")
                self.view_screenshot_requested.emit(game_id)
                logger.info("ScreenshotView._on_item_double_clicked: Signal emitted")
        except Exception as e:
            logger.error(f"ScreenshotView._on_item_double_clicked: Exception: {e}", exc_info=True)

    def _on_context_menu_requested(self, pos) -> None:
        """Handle right-click context menu request"""
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        game_data = index.data(ScreenshotRoles.GameData)
        if game_data:
            global_pos = self.list_view.viewport().mapToGlobal(pos)
            self.context_menu_requested.emit(game_data, global_pos)

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
        self._pending_requests.clear()
        self._request_queue.clear()
        self._no_screenshots.clear()
        self._url_to_game.clear()
        self._layout_retries = 0

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

        # Trigger lazy load for visible items after layout settles
        QTimer.singleShot(50, self._lazy_load_visible_screenshots)

    def _has_valid_screenshots(self, screenshots: list) -> bool:
        """Check if screenshots list contains valid URLs or existing files.

        Args:
            screenshots: List of screenshot URLs or file paths

        Returns:
            True if at least one screenshot is valid (URL or existing file)
        """
        if not screenshots:
            return False

        from pathlib import Path

        for ss in screenshots:
            if not ss:
                continue
            # If it's a URL (http/https), it's valid
            if ss.startswith("http://") or ss.startswith("https://"):
                return True
            # If it's a file:// URL, parse it cross-platform
            if ss.startswith("file://"):
                from PySide6.QtCore import QUrl
                path = QUrl(ss).toLocalFile()
                if Path(path).exists():
                    return True
            # If it's an absolute local path, check if file exists
            elif Path(ss).is_absolute():
                if Path(ss).exists():
                    return True

        return False

    def _get_visible_range(self) -> tuple:
        """Get (first_row, last_row) of visible items using indexAt().

        Much faster than iterating all model rows — O(1) instead of O(n).
        """
        from PySide6.QtCore import QPoint

        viewport = self.list_view.viewport()
        rect = viewport.rect()

        # Offset probe point by spacing — QPoint(0,0) may land in the
        # inter-item gap in IconMode, causing indexAt() to return invalid.
        spacing = self.list_view.spacing() + 1
        top_index = self.list_view.indexAt(rect.topLeft() + QPoint(spacing, spacing))
        if not top_index.isValid():
            return (0, 0)

        bottom_index = self.list_view.indexAt(rect.bottomRight() - QPoint(spacing, spacing))
        if not bottom_index.isValid():
            # Estimate visible count from viewport dimensions instead of
            # falling back to rowCount()-1 (which iterates entire model)
            grid = self.list_view.gridSize()
            item_h = grid.height() if grid.height() > 0 else 250
            item_w = grid.width() if grid.width() > 0 else 250
            cols = max(1, viewport.width() // item_w)
            visible_count = ((viewport.height() // item_h) + 2) * cols
            last_row = min(top_index.row() + visible_count, self.model.rowCount() - 1)
        else:
            last_row = bottom_index.row()

        return (top_index.row(), last_row)

    def _lazy_load_visible_screenshots(self) -> None:
        """Load screenshots for visible games that don't have them.

        NOTE: Qt's event loop silently swallows exceptions from QTimer
        callbacks, so the entire body is wrapped in try/except to ensure
        failures are logged.
        """
        try:
            self._lazy_load_visible_screenshots_inner()
        except Exception:
            logger.error("Screenshot lazy loader crashed", exc_info=True)

    def _lazy_load_visible_screenshots_inner(self) -> None:
        """Inner implementation of lazy screenshot loading.

        Scans visible items, queues those without screenshots, and starts
        background workers (same pattern as CoverView).
        """
        if not self._screenshot_callback:
            return

        row_count = self.model.rowCount()
        if row_count == 0:
            return

        first, last = self._get_visible_range()

        # Layout not yet computed after model reset — force it and retry
        if first == 0 and last == 0 and row_count > 0:
            self.list_view.doItemsLayout()
            first, last = self._get_visible_range()
            if first == 0 and last == 0:
                self._layout_retries = getattr(self, '_layout_retries', 0) + 1
                if self._layout_retries <= 20:  # Max ~2s of retries
                    QTimer.singleShot(100, self._lazy_load_visible_screenshots)
                return

        # Find visible items needing screenshots — only what's on screen
        for row in range(first, min(row_count, last + 1)):
            index = self.model.index(row, 0)
            game = index.data(ScreenshotRoles.GameData)
            if not game:
                continue

            game_id = game.get("id")
            if not game_id:
                continue

            screenshots = game.get("screenshots", [])
            if self._has_valid_screenshots(screenshots):
                continue
            if game_id in self._pending_requests:
                continue
            if game_id in self._no_screenshots:
                continue

            self._pending_requests.add(game_id)
            self._request_queue.append(game_id)

        self._process_request_queue()

    def _process_request_queue(self) -> None:
        """Start workers for pending requests up to concurrency limit."""
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

        while self._request_queue and len(self._active_workers) < self._max_concurrent_workers:
            game_id = self._request_queue.pop(0)
            self._start_worker(game_id)

    def _start_worker(self, game_id: str) -> None:
        """Start a worker thread to fetch screenshots."""
        worker = ScreenshotFetchWorker(game_id, self._screenshot_callback)
        worker.screenshots_fetched.connect(self._on_screenshots_fetched)
        worker.finished.connect(lambda: self._cleanup_worker(worker))
        self._active_workers.append(worker)
        worker.start()

    def _cleanup_worker(self, worker: ScreenshotFetchWorker) -> None:
        """Clean up finished worker and process queue."""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.deleteLater()
        self._process_request_queue()

    def _on_screenshots_fetched(self, game_id: str, screenshots: list) -> None:
        """Handle screenshot fetch result on the main thread."""
        self._pending_requests.discard(game_id)

        if screenshots:
            # Find and update the game in model data
            for i, game in enumerate(self.model._games):
                if game.get("id") == game_id:
                    game["screenshots"] = screenshots
                    self._url_to_game[screenshots[0]] = game
                    index = self.model.index(i, 0)
                    self.model.dataChanged.emit(index, index, [ScreenshotRoles.ScreenshotUrl])
                    break
        else:
            self._no_screenshots.add(game_id)

    def shutdown(self, timeout_ms: int = 2000) -> None:
        """Shutdown and wait for workers to complete."""
        self._request_queue.clear()
        self._pending_requests.clear()

        for worker in list(self._active_workers):
            if worker.isRunning():
                worker.wait(timeout_ms)
                if worker.isRunning():
                    worker.terminate()
                    worker.wait(1000)

        self._active_workers.clear()

    def set_density(self, density: int) -> None:
        """Set grid density (item width)

        Args:
            density: Item width in pixels
        """
        self._item_width = max(150, min(500, density))
        self.delegate.set_item_width(self._item_width)

        # Force layout update
        self.list_view.doItemsLayout()

    def get_density(self) -> int:
        """Get current grid density (item width)"""
        return self._item_width

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

    def update_game_covers(self, modified: Dict[str, Dict[str, str]]) -> None:
        """Update cover fields in model data for consistency.

        Screenshot view doesn't display covers, so no repaint is triggered —
        this only keeps the underlying game dicts in sync.

        Args:
            modified: Dict mapping game_id to changed fields.
        """
        lookup = getattr(self, '_game_id_to_row', {})
        for game_id, updates in modified.items():
            cover_url = updates.get("cover")
            if cover_url is None:
                continue
            row = lookup.get(game_id)
            if row is not None and row < len(self.model._games):
                self.model._games[row]["cover_image"] = cover_url

    def clear(self) -> None:
        """Clear all items"""
        self.model.set_games([])
