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
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication


# Pre-built border pens (~30% opacity) — light for dark badges, dark for light
_BORDER_LIGHT = QPen(QColor(255, 255, 255, 77), 1)
_BORDER_DARK = QPen(QColor(0, 0, 0, 77), 1)

# Icon directory (game modes only — store badges are text-rendered)
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


# ─── Status dots ─────────────────────────────────────────────────────

# Steam status indicator colors
STATUS_DOT_PRIVATE = QColor(255, 152, 0)    # Amber/orange — user-managed privacy
STATUS_DOT_DELISTED = QColor(244, 67, 54)   # Red — removed from store


def draw_status_dot(
    painter: QPainter,
    center_x: int,
    center_y: int,
    diameter: int,
    color: QColor,
) -> None:
    """Draw a small filled circle status indicator.

    Used for Steam private/delisted game markers.

    Args:
        painter: Active QPainter (caller manages save/restore).
        center_x: Center X of the dot.
        center_y: Center Y of the dot.
        diameter: Dot diameter in pixels (7 for list, 8 for cover/screenshot).
        color: Fill color.
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    radius = diameter / 2.0
    painter.drawEllipse(
        int(center_x - radius), int(center_y - radius),
        diameter, diameter,
    )


# ─── Store text badges ───────────────────────────────────────────────

# Width-to-height ratio for uniform store badges (all same width)
STORE_BADGE_WIDTH_RATIO = 2.4


def store_badge_width(height: int) -> int:
    """Calculate uniform store badge width from height."""
    return int(height * STORE_BADGE_WIDTH_RATIO)


def _store_badge_font(height: int) -> QFont:
    """Bold font sized relative to badge height."""
    font = QApplication.instance().font()
    font.setPointSize(max(7, int(height * 0.45)))
    font.setBold(True)
    return font


def draw_store_icon_badge(
    painter: QPainter,
    rect: QRect,
    store_name: str,
    bg_color: str,
    text_color: str,
    badge_label: str = "",
    heart_color: str = "",
    radius: int = 4,
) -> int:
    """Draw a store badge as a text label in a rounded rectangle.

    All store badges are uniform width (height * 2.4). Uses brand
    background color with bold text label.

    Args:
        painter: Active QPainter (caller manages save/restore).
        rect: Badge bounding rectangle.
        store_name: Lowercase store identifier.
        bg_color: Background color hex from brand_colors.
        text_color: Text color hex from brand_colors.
        badge_label: Label text (e.g. "STEAM", "GOG", "M♥G").
        heart_color: Unused, kept for API compat.
        radius: Corner radius (default 4).

    Returns:
        Actual width drawn.
    """
    bg = QColor(bg_color)
    font = _store_badge_font(rect.height())
    painter.setFont(font)

    # Draw rounded rect background + border
    painter.setPen(_border_pen_for_bg(bg))
    painter.setBrush(bg)
    painter.drawRoundedRect(rect, radius, radius)

    painter.setPen(QColor(text_color))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge_label)

    return rect.width()


# ─── Corner triangle badges ─────────────────────────────────────────


def draw_corner_triangle(
    painter: QPainter,
    rect: QRect,
    label: str,
    bg_color: str,
    text_color: str,
) -> None:
    """Draw a colored corner triangle at top-left of rect with diagonal text.

    Args:
        painter: Active QPainter (caller manages save/restore).
        rect: Bounding rectangle (cover or screenshot area).
        label: Short text (e.g. "FREE", "DEMO").
        bg_color: Triangle fill color (hex string).
        text_color: Text color (hex string).
    """
    # Scale triangle to ~25% of cover width, clamped 24-50px
    size = max(24, min(50, rect.width() // 4))
    x, y = rect.x(), rect.y()

    path = QPainterPath()
    path.moveTo(x, y)
    path.lineTo(x + size, y)
    path.lineTo(x, y + size)
    path.closeSubpath()

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    bg = QColor(bg_color)
    painter.fillPath(path, bg)
    # Subtle border along hypotenuse
    painter.setPen(_border_pen_for_bg(bg))
    painter.drawLine(x + size, y, x, y + size)

    # Diagonal text centered on true triangle centroid, rotated -45°
    cx = x + size / 3
    cy = y + size / 3
    painter.translate(cx, cy)
    painter.rotate(-45)

    font = QFont()
    font.setPointSize(max(4, size // 7) if size < 32 else max(5, size // 6))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(text_color))

    fm = QFontMetrics(font)
    tw = fm.horizontalAdvance(label)
    th = fm.capHeight()
    painter.drawText(-tw // 2, th // 2, label)

    painter.restore()


# ─── Overflow pill (+N) ────────────────────────────────────────────


def draw_overflow_pill(
    painter: QPainter,
    x: int,
    y: int,
    height: int,
    text: str,
    anchor_left: bool = False,
) -> int:
    """Draw a small '+N' overflow pill. Returns new x after drawing.

    anchor_left=True: pill starts at x, returns x + pill_width (left-to-right).
    anchor_left=False: pill ends at x, returns x - pill_width (right-to-left).
    """
    base_size = QApplication.instance().font().pointSize()
    if base_size <= 0:
        base_size = 10
    font = QFont()
    font.setPointSize(max(6, base_size - 3))
    font.setBold(True)

    fm = QFontMetrics(font)
    tw = fm.horizontalAdvance(text)
    pill_w = tw + 6
    pill_h = height

    if anchor_left:
        px = x
    else:
        px = x - pill_w

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    palette = QApplication.palette()
    bg = palette.color(QPalette.ColorRole.Mid)
    bg.setAlpha(200)
    fg = palette.color(QPalette.ColorRole.WindowText)

    pill_rect = QRect(px, y, pill_w, pill_h)
    path = QPainterPath()
    path.addRoundedRect(pill_rect, 3, 3)
    painter.fillPath(path, bg)
    painter.setPen(_border_pen_for_bg(bg))
    painter.drawPath(path)

    painter.setFont(font)
    painter.setPen(fg)
    painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, text)

    painter.restore()

    if anchor_left:
        return x + pill_w
    return x - pill_w
