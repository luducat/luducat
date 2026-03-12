# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# badge_painter.py

"""Shared badge drawing helper for all views.

Provides draw_badge() for text badges and draw_icon_badge() for game mode
icon badges, used by list, cover, and screenshot delegates to ensure
consistent rendering (rounded corners, semi-transparent border).
"""

from pathlib import Path
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication


# Pre-built border pens (~15% opacity) — light for dark badges, dark for light
_BORDER_LIGHT = QPen(QColor(255, 255, 255, 38), 1)
_BORDER_DARK = QPen(QColor(0, 0, 0, 38), 1)

# Icon directory
_GAME_MODE_ICONS_DIR = Path(__file__).parent.parent / "assets" / "icons" / "game_modes"

# Mode name → SVG filename (must match files in assets/icons/game_modes/)
GAME_MODE_ICON_FILES = {
    "PVP": "pvp.svg",
    "Multiplayer": "mp.svg",
    "Co-operative": "coop.svg",
    "Local Co-op": "local-coop.svg",
    "Local Versus": "local-vs.svg",
    "Online Versus": "online-vs.svg",
    "Split screen": "local.svg",
    "LAN": "lan.svg",
    "Massively Multiplayer Online (MMO)": "mmo.svg",
    "Battle Royale": "br.svg",
}

# Mode name → player count field key in game data dict
PLAYER_COUNT_FIELD = {
    "Multiplayer": "online_players",
    "Co-operative": "online_players",
    "Online Versus": "online_players",
    "Local Co-op": "local_players",
    "Local Versus": "local_players",
    "Split screen": "local_players",
    "LAN": "lan_players",
}

# Cache: (svg_filename, size, color_rgba) → tinted QPixmap
_icon_cache: Dict[Tuple[str, int, int], Optional[QPixmap]] = {}


def get_player_count(game: dict, mode_name: str) -> str:
    """Get player count for a game mode, with fallback to game_modes_detail.

    Tries the promoted top-level field first (from LIST_FIELDS cache).
    Falls back to game_modes_detail dict (from lazy DETAIL_FIELDS).

    Returns:
        Player count string (e.g. "8") or empty string if unavailable.
    """
    count_field = PLAYER_COUNT_FIELD.get(mode_name)
    if not count_field:
        return ""
    val = game.get(count_field)
    if val is not None:
        try:
            int(val)
            return str(val)
        except (ValueError, TypeError):
            pass
    # Fallback: read from game_modes_detail dict (lazy-loaded detail field)
    gmd = game.get("game_modes_detail")
    if isinstance(gmd, dict):
        val = gmd.get(count_field)
        if val is not None:
            try:
                int(val)
                return str(val)
            except (ValueError, TypeError):
                pass
    return ""


def _load_mode_icon(filename: str, size: int, tint: QColor) -> Optional[QPixmap]:
    """Load a game mode SVG and tint it, cached by (filename, size, color)."""
    key = (filename, size, tint.rgba())
    if key in _icon_cache:
        return _icon_cache[key]

    path = _GAME_MODE_ICONS_DIR / filename
    source = QPixmap(str(path))
    if source.isNull():
        _icon_cache[key] = None
        return None

    source = source.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    colored = QPixmap(source.size())
    colored.fill(Qt.GlobalColor.transparent)
    p = QPainter(colored)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    p.drawPixmap(0, 0, source)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(colored.rect(), tint)
    p.end()

    _icon_cache[key] = colored
    return colored


def _border_pen_for_bg(bg: QColor) -> QPen:
    """Pick border pen based on background luminance."""
    # ITU-R BT.601 luma
    lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return _BORDER_DARK if lum > 128 else _BORDER_LIGHT


def _count_font() -> QFont:
    """Small font for player count text on badges."""
    base_size = QApplication.instance().font().pointSize()
    font = QApplication.instance().font()
    font.setPointSize(max(7, base_size - 2))
    return font


def game_mode_badge_width(mode_name: str, badge_size: int, player_count: str = "") -> int:
    """Calculate badge width for a game mode, accounting for player count text.

    Returns:
        0 if mode has no icon (caller uses text fallback).
        badge_size if no count (square, unchanged).
        badge_size + text_width + 3 if count present (wider).
    """
    if mode_name not in GAME_MODE_ICON_FILES:
        return 0
    if not player_count:
        return badge_size
    fm = QFontMetrics(_count_font())
    text_width = fm.horizontalAdvance(player_count)
    return badge_size + text_width + 3


def draw_badge(
    painter: QPainter,
    rect: QRect,
    bg_color: str,
    text_color: str,
    text: str,
    radius: int = 2,
) -> None:
    """Draw a badge with rounded corners and semi-transparent border.

    Border color auto-adapts: light border on dark backgrounds, dark border
    on light backgrounds (preserves ProtonDB tier colors, etc.).

    Args:
        painter: Active QPainter (caller manages save/restore).
        rect: Badge bounding rectangle.
        bg_color: Background color (hex string).
        text_color: Text color (hex string).
        text: Badge label text.
        radius: Corner radius in pixels (default 2).
    """
    bg = QColor(bg_color)
    painter.setPen(_border_pen_for_bg(bg))
    painter.setBrush(bg)
    painter.drawRoundedRect(rect, radius, radius)
    painter.setPen(QColor(text_color))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def draw_icon_badge(
    painter: QPainter,
    rect: QRect,
    mode_name: str,
    player_count: str = "",
    radius: int = 2,
) -> int:
    """Draw a game mode badge with an SVG icon and optional player count.

    Uses the current palette for colors: palette(Mid) for background,
    palette(WindowText) for icon tint. Adapts to any theme automatically.

    Args:
        painter: Active QPainter (caller manages save/restore).
        rect: Badge bounding rectangle.
        mode_name: Game mode name key (e.g. "Multiplayer", "Local Co-op").
        player_count: Player count string (e.g. "8"). Empty = no count shown.
        radius: Corner radius in pixels (default 2).

    Returns:
        0 if the mode has no icon (caller should fall back to text badge).
        Positive int = actual width drawn.
    """
    filename = GAME_MODE_ICON_FILES.get(mode_name)
    if not filename:
        return 0

    # Get theme-aware colors from palette
    palette = QApplication.palette()
    bg = palette.color(QPalette.ColorRole.Mid)
    icon_color = palette.color(QPalette.ColorRole.WindowText)

    # Icon size = badge height minus 2px padding on each side
    icon_size = max(6, rect.height() - 4)
    pixmap = _load_mode_icon(filename, icon_size, icon_color)
    if pixmap is None:
        return 0

    # Draw badge background
    painter.setPen(_border_pen_for_bg(bg))
    painter.setBrush(bg)
    painter.drawRoundedRect(rect, radius, radius)

    if player_count:
        # Icon at left + 2px, count text right of icon
        ix = rect.x() + 2
        iy = rect.y() + (rect.height() - pixmap.height()) // 2
        painter.drawPixmap(QPoint(ix, iy), pixmap)

        font = _count_font()
        painter.setFont(font)
        painter.setPen(icon_color)
        text_x = ix + pixmap.width() + 1
        text_rect = QRect(text_x, rect.y(), rect.right() - text_x, rect.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, player_count)
    else:
        # Center icon in badge (original behavior)
        ix = rect.x() + (rect.width() - pixmap.width()) // 2
        iy = rect.y() + (rect.height() - pixmap.height()) // 2
        painter.drawPixmap(QPoint(ix, iy), pixmap)

    return rect.width()


def draw_license_circle(
    painter: QPainter,
    center_x: int,
    center_y: int,
    diameter: int,
    count_text: str,
    color: Optional[QColor] = None,
) -> None:
    """Draw a circle outline with a license count number centered inside.

    Used for family sharing badges — shows how many external licenses
    exist in the family pool.

    Args:
        painter: Active QPainter (caller manages save/restore).
        center_x: Center X coordinate of the circle.
        center_y: Center Y coordinate of the circle.
        diameter: Circle diameter in pixels.
        count_text: The count number to display (e.g. "2").
        color: Circle and text color. None = white.
    """
    if color is None:
        color = QColor(255, 255, 255)

    pen = QPen(color, 1.5)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    radius = diameter / 2.0
    painter.drawEllipse(
        int(center_x - radius), int(center_y - radius),
        diameter, diameter,
    )

    painter.setPen(color)
    font = _count_font()
    painter.setFont(font)
    text_rect = QRect(
        int(center_x - radius), int(center_y - radius),
        diameter, diameter,
    )
    painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, count_text)
