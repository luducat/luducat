# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# list_view.py

"""List view (detail panel) for luducat

Shows comprehensive information about the selected game with tabbed interface:
- About: Screenshots, metadata, description
- Settings: Runtime selection, launch arguments
- Stats: Play time, launch count
- Files: Installed files, archives
- Notes: User notes
"""

import logging
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QUrl, QEvent, QRect
from PySide6.QtGui import (
    QDesktopServices, QPixmap, QFont, QPainter, QPalette, QLinearGradient, QColor, QPainterPath,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QToolButton,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QMenu,
    QTextBrowser,
    QStackedWidget,
    QButtonGroup,
    QComboBox,
    QLineEdit,
    QFormLayout,
    QGroupBox,
    QPlainTextEdit,
    QSplitter,
)

from ..core.constants import (
    GAME_MODE_LABELS,
    GAME_MODE_FILTERS,
    INSTALLED_BADGE_LABEL,
    PROTONDB_TIER_LABELS,
    STEAM_DECK_LABELS,
)
from ..core.plugin_manager import PluginManager
from ..utils.icons import load_tinted_icon
from ..utils.image_cache import get_screenshot_cache, get_hero_cache, ImageCache
from ..utils.workers import DataLoaderWorker
from .badge_painter import GAME_MODE_ICON_FILES, get_player_count

logger = logging.getLogger(__name__)


# Shared cache for description images (singleton)
_description_image_cache: Optional[ImageCache] = None


def get_description_image_cache() -> ImageCache:
    """Get shared description image cache

    Description images displayed at ~400×300, cached at 2× (800×600).
    Budget: 30 MB (~15-20 images at 1.9 MB each).
    """
    global _description_image_cache
    if _description_image_cache is None:
        from ..utils.image_cache import DEFAULT_DESCRIPTION_BUDGET_BYTES
        _description_image_cache = ImageCache(
            "description_images",
            max_memory_items=50,
            max_memory_bytes=DEFAULT_DESCRIPTION_BUDGET_BYTES,
            max_size=(800, 600),
        )
    return _description_image_cache


class RemoteImageTextBrowser(QTextBrowser):
    """QTextBrowser with async remote image loading via ImageCache"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # Use shared cache for description images
        self._image_cache = get_description_image_cache()
        self._image_cache.image_loaded.connect(self._on_image_loaded)
        self._pending_urls: set = set()
        self._current_html: str = ""

    def setHtml(self, html: str) -> None:
        """Override to track current HTML for refresh after image load"""
        self._current_html = html
        self._pending_urls.clear()
        super().setHtml(html)

    def loadResource(self, type: int, url: QUrl) -> any:
        """Override to load remote images via cache"""
        # Only handle images
        if type != 2:  # QTextDocument.ResourceType.ImageResource
            return super().loadResource(type, url)

        url_str = url.toString()

        # Skip non-HTTP URLs
        if not url_str.startswith(("http://", "https://")):
            return super().loadResource(type, url)

        # Try to get from cache (returns pixmap if cached, None if loading)
        pixmap = self._image_cache.get_image(url_str)
        if pixmap and not pixmap.isNull():
            return pixmap

        # Track pending URL for refresh
        self._pending_urls.add(url_str)
        return None  # Return None while loading

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle image loaded from cache - refresh document"""
        if url in self._pending_urls:
            self._pending_urls.discard(url)
            # Re-set HTML to trigger reload with cached image
            if self._current_html:
                # Save scroll position
                scrollbar = self.verticalScrollBar()
                scroll_pos = scrollbar.value() if scrollbar else 0
                # Reload
                super().setHtml(self._current_html)
                # Restore scroll position
                if scrollbar:
                    scrollbar.setValue(scroll_pos)


def format_metadata_list(items: list, prefix: str = "") -> tuple[str, str]:
    """Format a list of items for display with '+x more' truncation.

    Args:
        items: List of strings (e.g., publishers, developers)
        prefix: Optional prefix like "By " or "Pub: "

    Returns:
        Tuple of (display_text, tooltip_text)
        tooltip_text is empty if no truncation needed
    """
    if not items:
        return "", ""

    # Handle string input (shouldn't happen but be safe)
    if isinstance(items, str):
        items = [items]

    # Filter empty items
    items = [str(i).strip() for i in items if i and str(i).strip()]
    if not items:
        return "", ""

    if len(items) == 1:
        return f"{prefix}{items[0]}", ""

    # Multiple items - show first with "+x more"
    display = _("{prefix}{first}, +{n} more").format(
        prefix=prefix, first=items[0], n=len(items) - 1
    )
    tooltip = "\n".join(items)
    return display, tooltip


def normalize_release_date(date_str: str) -> str:
    """Format a release date for display.

    Since dates are now normalised to ISO at cache-build time, this is
    a thin wrapper around :func:`~luducat.core.dt.format_release_date`.
    Falls back to the raw string for any non-ISO input.
    """
    from ..core.dt import format_release_date

    return format_release_date(date_str)


class ClickableLabel(QLabel):
    """Screenshot thumbnail with rounded corners and hover highlight."""

    CORNER_RADIUS = 8

    clicked = Signal(int)  # Emits index on single-click

    def __init__(self, index: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._index = index
        self._thumb_pixmap: Optional[QPixmap] = None
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def setThumbPixmap(self, pixmap: QPixmap) -> None:
        """Store pixmap for custom rounded painting."""
        self._thumb_pixmap = pixmap
        self.setText("")
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if self._thumb_pixmap and not self._thumb_pixmap.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            rect = self.rect()

            # Clip to rounded rect
            path = QPainterPath()
            path.addRoundedRect(
                rect.x(), rect.y(), rect.width(), rect.height(),
                self.CORNER_RADIUS, self.CORNER_RADIUS,
            )
            painter.setClipPath(path)

            # Calculate scaled size without allocating a new QPixmap
            # (SmoothPixmapTransform render hint handles bilinear filtering)
            pw, ph = self._thumb_pixmap.width(), self._thumb_pixmap.height()
            tw, th = rect.width(), rect.height()
            scale = min(tw / pw, th / ph)
            sw, sh = int(pw * scale), int(ph * scale)
            x = (tw - sw) // 2
            y = (th - sh) // 2
            dest = QRect(x, y, sw, sh)
            painter.drawPixmap(dest, self._thumb_pixmap)

            # Hover highlight overlay (palette-aware: adapts to light/dark themes)
            if self._hovered:
                painter.setClipPath(path)
                highlight = self.palette().color(QPalette.ColorRole.Highlight)
                highlight.setAlpha(40)
                painter.fillRect(rect, highlight)

            # Border (inside the rounded rect, palette-aware)
            painter.setClipping(False)
            border_base = self.palette().color(QPalette.ColorRole.Highlight)
            border_base.setAlpha(130 if self._hovered else 40)
            border_color = border_base
            painter.setPen(border_color)
            painter.drawRoundedRect(
                rect.adjusted(0, 0, -1, -1),
                self.CORNER_RADIUS, self.CORNER_RADIUS,
            )
            painter.end()
        else:
            # Fallback: default QLabel painting for "Loading..." text
            super().paintEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self._index)
            super().mouseReleaseEvent(event)
        except RuntimeError:
            pass  # C++ object already deleted (game selection changed during click)


class ScreenshotCarousel(QWidget):
    """Horizontal scrolling screenshot carousel"""

    screenshot_clicked = Signal(int)  # Screenshot index

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._screenshots: List[str] = []
        self._image_cache = get_screenshot_cache()
        self._thumb_labels: List[ClickableLabel] = []

        self._setup_ui()

        # Connect to image cache for async updates
        self._image_cache.image_loaded.connect(self._on_image_loaded)
        self._image_cache.image_not_found.connect(self._on_image_not_found)

    def _setup_ui(self) -> None:
        """Create carousel layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)  # Compact spacing

        # Label
        #label = QLabel("Screenshots")
        #label.setObjectName("sectionLabel")
        #layout.addWidget(label)

        # Scroll area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(170)  # Compact height for banner overlay
        self.scroll.setStyleSheet("background: transparent; border: none;")

        # Container
        self.container = QWidget()
        self.container.setStyleSheet("background: transparent;")
        self.container_layout = QHBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 0, 0, 0)
        self.container_layout.setSpacing(6)  # Compact spacing
        self.container_layout.addStretch()

        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

    def set_screenshots(self, screenshots: List[str]) -> None:
        """Set screenshot URLs/paths

        Args:
            screenshots: List of screenshot URLs or file paths
        """
        logger.debug(
            "set_screenshots called with %d screenshots",
            len(screenshots),
        )
        self._screenshots = screenshots
        self._thumb_labels.clear()

        # Clear existing
        while self.container_layout.count() > 1:
            item = self.container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Add thumbnails (16:9 aspect ratio, compact size)
        for i, url in enumerate(screenshots[:10]):  # Limit to 10
            thumb = ClickableLabel(i)
            thumb.setFixedSize(260, 146)  # 16:9 aspect ratio, compact for banner overlay
            thumb.setObjectName("screenshotThumb")
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setScaledContents(False)
            thumb.setProperty("screenshot_url", url)

            # Connect double-click to emit screenshot_clicked
            thumb.clicked.connect(self.screenshot_clicked.emit)

            # Try to load from cache
            pixmap = self._image_cache.get_image(url)
            if pixmap and not pixmap.isNull():
                logger.debug(f"Screenshot loaded immediately: {url}")
                self._set_thumb_pixmap(thumb, pixmap)
            else:
                logger.debug(f"Screenshot not available, showing Loading...: {url}")
                thumb.setText(_("Loading..."))

            self._thumb_labels.append(thumb)
            self.container_layout.insertWidget(i, thumb)

    def _set_thumb_pixmap(self, label: ClickableLabel, pixmap: QPixmap) -> None:
        """Set pixmap on thumbnail (custom painting handles scaling/rounding)"""
        label.setThumbPixmap(pixmap)

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle async image load"""
        # Find and update matching thumbnail
        for thumb in self._thumb_labels:
            if thumb.property("screenshot_url") == url:
                self._set_thumb_pixmap(thumb, pixmap)
                break

    def _on_image_not_found(self, url: str, error: str) -> None:
        """Handle HTTP 404 — mark thumbnail as unavailable"""
        for thumb in self._thumb_labels:
            if thumb.property("screenshot_url") == url:
                thumb.setText(_("Unavailable"))
                break

    def clear(self) -> None:
        """Clear screenshots"""
        self._screenshots = []
        while self.container_layout.count() > 1:
            item = self.container_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def get_screenshots(self) -> List[str]:
        """Get current screenshot URLs"""
        return self._screenshots


class HeroBanner(QWidget):
    """Background image with action buttons and screenshot carousel overlay.

    Displays a hero/background image for the selected game. Action buttons
    (Play, Favorite, Store) are overlaid at the top-right. The screenshot
    carousel overlays the bottom portion with transparent backgrounds so the
    artwork shows through gaps between thumbnails.

    No gradient overlay — the image is shown as-is.
    """

    BANNER_HEIGHT = 290          # Full height with hero image
    COLLAPSED_HEIGHT = 220       # No-image: buttons + breathing room + carousel

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(self.COLLAPSED_HEIGHT)
        self.setObjectName("heroBanner")
        self._background_pixmap: Optional[QPixmap] = None
        self._current_image_url = ""
        self._image_cache = get_hero_cache()
        self._image_cache.image_loaded.connect(self._on_hero_loaded)
        self._image_cache.image_failed.connect(self._on_hero_failed)
        self._image_cache.image_not_found.connect(self._on_hero_failed)
        self._setup_overlay()

    def _setup_overlay(self) -> None:
        """Create action button overlay and carousel placeholder."""
        self._overlay_layout = QVBoxLayout(self)
        self._overlay_layout.setContentsMargins(16, 8, 16, 0)

        # Top row: action buttons right-aligned
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addStretch()

        # Launch buttons container
        self.launch_container = QWidget()
        self.launch_container.setObjectName("heroBannerActions")
        self.launch_layout = QHBoxLayout(self.launch_container)
        self.launch_layout.setContentsMargins(0, 0, 0, 0)
        self.launch_layout.setSpacing(8)
        self.launch_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.launch_container)

        # Store button (after launch, before favorite)
        self.store_btn = QToolButton()
        self.store_btn.setText(_("Store"))
        self.store_btn.setObjectName("storeButton")
        self.store_btn.setToolTip(_("Open store page in browser"))
        top.addWidget(self.store_btn)

        # Favorite button (last)
        self.favorite_btn = QPushButton(_("Favorite"))
        self.favorite_btn.setObjectName("favoriteButton")
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.setToolTip(_("Mark this game as a favorite"))
        top.addWidget(self.favorite_btn)

        self._overlay_layout.addLayout(top)
        self._overlay_layout.addStretch()
        # Carousel will be added at the bottom via set_carousel()

    def set_carousel(self, carousel: QWidget) -> None:
        """Add the screenshot carousel at the bottom of the banner overlay."""
        self._overlay_layout.addWidget(carousel)

    EDGE_FADE = 16  # Pixels for top/bottom edge gradient fade

    def paintEvent(self, event) -> None:
        """Draw background image with subtle edge fades."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()
        bg_color = self.palette().color(self.backgroundRole())

        if self._background_pixmap and not self._background_pixmap.isNull():
            # Calculate source crop rect (KeepAspectRatioByExpanding)
            # without allocating a new QPixmap — painter handles scaling
            pw, ph = self._background_pixmap.width(), self._background_pixmap.height()
            tw, th = rect.width(), rect.height()
            scale = max(tw / pw, th / ph)
            crop_w = int(tw / scale)
            crop_h = int(th / scale)
            src_x = (pw - crop_w) // 2
            src_y = (ph - crop_h) // 2
            src_rect = QRect(src_x, src_y, crop_w, crop_h)
            painter.drawPixmap(rect, self._background_pixmap, src_rect)

            # Top edge fade: theme bg → transparent over EDGE_FADE pixels
            top_grad = QLinearGradient(0, 0, 0, self.EDGE_FADE)
            top_grad.setColorAt(0.0, QColor(
                bg_color.red(), bg_color.green(), bg_color.blue(), 220))
            top_grad.setColorAt(1.0, QColor(
                bg_color.red(), bg_color.green(), bg_color.blue(), 0))
            painter.fillRect(0, 0, rect.width(), self.EDGE_FADE, top_grad)

            # Bottom edge fade: transparent → theme bg over EDGE_FADE pixels
            bottom_y = rect.height() - self.EDGE_FADE
            bottom_grad = QLinearGradient(0, bottom_y, 0, rect.height())
            bottom_grad.setColorAt(0.0, QColor(
                bg_color.red(), bg_color.green(), bg_color.blue(), 0))
            bottom_grad.setColorAt(1.0, QColor(
                bg_color.red(), bg_color.green(), bg_color.blue(), 220))
            painter.fillRect(0, bottom_y, rect.width(), self.EDGE_FADE, bottom_grad)
        else:
            # No hero image — subtle vertical gradient
            grad = QLinearGradient(0, 0, 0, rect.height())
            lighter = bg_color.lighter(120)
            grad.setColorAt(0.0, lighter)
            grad.setColorAt(1.0, bg_color)
            painter.fillRect(rect, grad)

        painter.end()

    def set_background(self, url: str) -> None:
        """Load and set the background image from URL."""
        self._current_image_url = url or ""
        if not url:
            self._background_pixmap = None
            self.setFixedHeight(self.COLLAPSED_HEIGHT)
            self.update()
            return

        pixmap = self._image_cache.get_image(url)
        if pixmap and not pixmap.isNull():
            self._background_pixmap = pixmap
            self.setFixedHeight(self.BANNER_HEIGHT)
        else:
            self._background_pixmap = None
            self.setFixedHeight(self.COLLAPSED_HEIGHT)
        self.update()

    def _on_hero_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle async hero image load."""
        if url == self._current_image_url and pixmap and not pixmap.isNull():
            self._background_pixmap = pixmap
            self.setFixedHeight(self.BANNER_HEIGHT)
            self.update()

    def _on_hero_failed(self, url: str, error: str) -> None:
        """Handle failed hero image load — clear stale background."""
        if url == self._current_image_url:
            self._background_pixmap = None
            self.setFixedHeight(self.COLLAPSED_HEIGHT)
            self.update()

    def clear(self) -> None:
        """Clear the banner state."""
        self._background_pixmap = None
        self._current_image_url = ""
        self.setFixedHeight(self.COLLAPSED_HEIGHT)
        while self.launch_layout.count():
            item = self.launch_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.favorite_btn.setChecked(False)
        self.update()


class TagChip(QFrame):
    """Small chip for displaying a tag - uses theme styling.

    Clickable: emits tag_clicked(str) with the tag name.
    Shows colored left border from tag color.
    """

    tag_clicked = Signal(str)

    def __init__(
        self,
        name: str,
        color: str = "",
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)
        self.tag_name = name
        self.tag_color = color
        self.setObjectName("tagChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.label = QLabel(name)
        self.label.setObjectName("tagChipLabel")
        layout.addWidget(self.label)

        # Use fixed size policy for proper layout
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # Color accent as left border (dynamic per-tag, rest handled by QSS)
        if color:
            self.setStyleSheet(
                f"QFrame#tagChip {{ border-left: 3px solid {color}; }}"
            )
        self.setToolTip(_("Tag: {name}\nClick to filter by this tag").format(name=name))

    def enterEvent(self, event):
        font = self.label.font()
        font.setUnderline(True)
        self.label.setFont(font)
        super().enterEvent(event)

    def leaveEvent(self, event):
        font = self.label.font()
        font.setUnderline(False)
        self.label.setFont(font)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.tag_clicked.emit(self.tag_name)
        super().mousePressEvent(event)


class FilterLabel(QLabel):
    """QLabel that acts as a clickable filter trigger.

    Shows underline on hover and emits clicked(str) with the item value.
    """

    clicked = Signal(str)

    def __init__(self, text: str, value: str, parent=None):
        super().__init__(text, parent)
        self._value = value
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        font = self.font()
        font.setUnderline(True)
        self.setFont(font)
        super().enterEvent(event)

    def leaveEvent(self, event):
        font = self.font()
        font.setUnderline(False)
        self.setFont(font)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._value)
        super().mousePressEvent(event)


class MetadataPanel(QScrollArea):
    """Right-side scrollable metadata panel with key-value rows.

    Displays game metadata in a two-column grid layout (key: value).
    Rows with empty values are automatically hidden. Some values are
    clickable and trigger filter signals.
    """

    filter_developer_requested = Signal(list)
    filter_publisher_requested = Signal(list)
    filter_genre_requested = Signal(list)
    filter_tag_requested = Signal(list)
    filter_year_requested = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("metadataPanel")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumWidth(260)

        content = QWidget()
        content.setObjectName("metadataPanelContent")
        self._grid = QGridLayout(content)
        self._grid.setContentsMargins(6, 4, 4, 4)
        self._grid.setHorizontalSpacing(4)
        self._grid.setVerticalSpacing(2)
        self._grid.setColumnStretch(1, 1)

        self._rows: Dict[str, tuple] = {}  # name -> (key_label, value_widget)
        self._row_count = 0
        self._tag_chips: List[TagChip] = []

        self._create_rows()
        self._grid.setRowStretch(self._row_count, 1)  # push content up

        self.setWidget(content)

    def _add_row(self, key_text: str, value_widget: QWidget, name: str) -> None:
        """Add a key-value row to the grid."""
        key_label = QLabel(key_text)
        key_label.setObjectName("metadataKey")
        key_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._grid.addWidget(
            key_label, self._row_count, 0, Qt.AlignmentFlag.AlignTop
        )
        self._grid.addWidget(value_widget, self._row_count, 1)
        self._rows[name] = (key_label, value_widget)
        self._row_count += 1

    def _make_clickable_label(self, signal: Signal) -> QLabel:
        """Create a QLabel with clickable rich text links."""
        label = QLabel()
        label.setObjectName("metadataValue")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        label.setWordWrap(True)
        label.linkActivated.connect(lambda href: signal.emit([href]))
        return label

    def _make_plain_label(self) -> QLabel:
        """Create a plain text QLabel for non-clickable values."""
        label = QLabel()
        label.setObjectName("metadataValue")
        label.setWordWrap(True)
        return label

    def _create_rows(self) -> None:
        """Create all metadata rows (initially hidden)."""
        # Clickable rows
        self.dev_value = self._make_clickable_label(
            self.filter_developer_requested
        )
        self._add_row(_("Developer"), self.dev_value, "developer")

        self.pub_value = self._make_clickable_label(
            self.filter_publisher_requested
        )
        self._add_row(_("Publisher"), self.pub_value, "publisher")

        self.genre_value = self._make_clickable_label(
            self.filter_genre_requested
        )
        self._add_row(_("Genre"), self.genre_value, "genre")

        # Tags row (flow of colored TagChip widgets)
        self.tags_container = QWidget()
        self.tags_layout = QHBoxLayout(self.tags_container)
        self.tags_layout.setContentsMargins(0, 0, 0, 0)
        self.tags_layout.setSpacing(4)
        self.tags_layout.addStretch()
        self._add_row(_("Tags"), self.tags_container, "tags")

        self.year_value = FilterLabel("", "")
        self.year_value.setObjectName("metadataValue")
        self.year_value.clicked.connect(
            lambda y: self.filter_year_requested.emit([y])
        )
        self._add_row(_("Release"), self.year_value, "release")

        # Plain rows
        self.franchise_value = self._make_plain_label()
        self._add_row(_("Franchise"), self.franchise_value, "franchise")

        self.series_value = self._make_plain_label()
        self._add_row(_("Series"), self.series_value, "series")

        self.engine_value = self._make_plain_label()
        self._add_row(_("Engine"), self.engine_value, "engine")

        self.themes_value = self._make_plain_label()
        self._add_row(_("Themes"), self.themes_value, "themes")

        self.perspective_value = self._make_plain_label()
        self._add_row(_("Perspective"), self.perspective_value, "perspective")

        self.pacing_value = self._make_plain_label()
        self._add_row(_("Pacing"), self.pacing_value, "pacing")

        self.art_style_value = self._make_plain_label()
        self._add_row(_("Art Style"), self.art_style_value, "art_style")

        self.platforms_value = self._make_plain_label()
        self._add_row(_("Platforms"), self.platforms_value, "platforms")

        self.stores_value = self._make_plain_label()
        self._add_row(_("Stores"), self.stores_value, "stores")

        self.features_value = self._make_plain_label()
        self._add_row(_("Features"), self.features_value, "features")

        self.game_modes_info_value = self._make_plain_label()
        self._add_row(_("Game Modes"), self.game_modes_info_value, "game_modes_info")

        self.languages_value = self._make_plain_label()
        self._add_row(_("Languages"), self.languages_value, "languages")

        self.controller_value = self._make_plain_label()
        self._add_row(_("Controller"), self.controller_value, "controller")

        self.controls_value = self._make_plain_label()
        self._add_row(_("Controls"), self.controls_value, "controls")

        self.monetization_value = self._make_plain_label()
        self._add_row(_("Monetization"), self.monetization_value, "monetization")

        self.rating_value = self._make_plain_label()
        self._add_row(_("Rating"), self.rating_value, "rating")

        self.metacritic_value = QLabel()
        self.metacritic_value.setObjectName("metadataValue")
        self.metacritic_value.setTextFormat(Qt.TextFormat.RichText)
        self.metacritic_value.linkActivated.connect(self._on_link_activated)
        self.metacritic_value.setWordWrap(True)
        self._add_row(_("Metacritic"), self.metacritic_value, "metacritic")

        self.protondb_value = QLabel()
        self.protondb_value.setObjectName("metadataValue")
        self.protondb_value.setTextFormat(Qt.TextFormat.RichText)
        self.protondb_value.linkActivated.connect(self._on_link_activated)
        self.protondb_value.setWordWrap(True)
        self._add_row(_("ProtonDB"), self.protondb_value, "protondb")

        self.steam_deck_value = self._make_plain_label()
        self._add_row(_("Steam Deck"), self.steam_deck_value, "steam_deck")

        self.achievements_value = self._make_plain_label()
        self._add_row(_("Achievements"), self.achievements_value, "achievements")

        self.avg_playtime_value = self._make_plain_label()
        self._add_row(_("Avg. Playtime"), self.avg_playtime_value, "avg_playtime")

        self.peak_players_value = self._make_plain_label()
        self._add_row(_("Peak Players"), self.peak_players_value, "peak_players")

        self.owners_value = self._make_plain_label()
        self._add_row(_("Owners"), self.owners_value, "owners")

        self.recommendations_value = self._make_plain_label()
        self._add_row(_("Recommendations"), self.recommendations_value, "recommendations")

        self.age_rating_value = self._make_plain_label()
        self._add_row(_("Age Rating"), self.age_rating_value, "age_rating")

        self.players_value = self._make_plain_label()
        self._add_row(_("Players"), self.players_value, "players")

        self.crossplay_value = self._make_plain_label()
        self._add_row(_("Crossplay"), self.crossplay_value, "crossplay")

        self.links_value = QLabel()
        self.links_value.setObjectName("metadataValue")
        self.links_value.setTextFormat(Qt.TextFormat.RichText)
        self.links_value.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.links_value.linkActivated.connect(self._on_link_activated)
        self.links_value.setWordWrap(True)
        self._add_row(_("Links"), self.links_value, "links")

        self.storyline_value = self._make_plain_label()
        self._add_row(_("Storyline"), self.storyline_value, "storyline")

        # Initially hide all rows
        for name in self._rows:
            self._set_row_visible(name, False)

    def _set_row_visible(self, name: str, visible: bool) -> None:
        """Show or hide a metadata row by name."""
        if name in self._rows:
            key_label, value_widget = self._rows[name]
            key_label.setVisible(visible)
            value_widget.setVisible(visible)

    @staticmethod
    def _on_link_activated(url: str) -> None:
        """Open a clicked link in the user's preferred browser."""
        from ..utils.browser import open_url
        open_url(url)

    def _set_clickable_items(
        self, label: QLabel, items: list, max_items: int = 5
    ) -> bool:
        """Set clickable rich text on a label. Returns True if items set."""
        if not items:
            return False

        items = [str(i).strip() for i in items if i and str(i).strip()]
        if not items:
            return False

        import html as html_mod

        color = label.palette().color(label.foregroundRole()).name()
        style = f"color:{color}; text-decoration:none"

        shown = items[:max_items]
        parts = []
        for item in shown:
            escaped = html_mod.escape(item)
            parts.append(
                f'<a href="{escaped}" style="{style}">{escaped}</a>'
            )

        text = ", ".join(parts)
        if len(items) > max_items:
            text += " " + _("+{n} more").format(n=len(items) - max_items)

        label.setText(text)
        label.setToolTip(
            "\n".join(items) if len(items) > max_items else ""
        )
        return True

    def set_game_metadata(self, game: Dict[str, Any]) -> None:
        """Populate all metadata rows from a game data dict.

        Empty fields are automatically hidden.
        """
        # Developer (clickable)
        devs = game.get("developers", [])
        visible = self._set_clickable_items(self.dev_value, devs)
        self._set_row_visible("developer", visible)

        # Publisher (clickable)
        pubs = game.get("publishers", [])
        visible = self._set_clickable_items(self.pub_value, pubs)
        self._set_row_visible("publisher", visible)

        # Genre (clickable)
        genres = game.get("genres", [])
        visible = self._set_clickable_items(self.genre_value, genres)
        self._set_row_visible("genre", visible)

        # Release date (clickable by year)
        release_date = game.get("release_date", "")
        display_date = normalize_release_date(release_date)
        year = ""
        if release_date and len(release_date) >= 4 and release_date[:4].isdigit():
            year = release_date[:4]
        self.year_value.setText(display_date or year)
        self.year_value._value = year
        self._set_row_visible("release", bool(display_date or year))

        # Franchise
        franchise = game.get("franchise", "")
        self.franchise_value.setText(franchise)
        self._set_row_visible("franchise", bool(franchise))

        # Series
        series = game.get("series", "")
        self.series_value.setText(series)
        self._set_row_visible("series", bool(series))

        # Engine
        engine = game.get("engine", "")
        self.engine_value.setText(engine)
        self._set_row_visible("engine", bool(engine))

        # Themes
        themes = game.get("themes", [])
        if themes:
            self.themes_value.setText(
                ", ".join(str(t) for t in themes)
            )
            self._set_row_visible("themes", True)
        else:
            self._set_row_visible("themes", False)

        # Player perspectives
        perspectives = game.get("perspectives", [])
        if perspectives:
            self.perspective_value.setText(
                ", ".join(str(p) for p in perspectives)
            )
            self._set_row_visible("perspective", True)
        else:
            self._set_row_visible("perspective", False)

        # Pacing (PCGW)
        pacing = game.get("pacing", [])
        if pacing:
            self.pacing_value.setText(", ".join(str(p) for p in pacing))
        self._set_row_visible("pacing", bool(pacing))

        # Art Style (PCGW)
        art_styles = game.get("art_styles", [])
        if art_styles:
            self.art_style_value.setText(", ".join(str(a) for a in art_styles))
        self._set_row_visible("art_style", bool(art_styles))

        # Platforms
        platforms = game.get("platforms", [])
        if platforms:
            self.platforms_value.setText(
                ", ".join(str(p) for p in platforms)
            )
            self._set_row_visible("platforms", True)
        else:
            self._set_row_visible("platforms", False)

        # Stores
        stores = game.get("stores", [])
        if stores:
            store_names = ", ".join(
                PluginManager.get_store_display_name(s)
                for s in stores
            )
            self.stores_value.setText(store_names)
            self._set_row_visible("stores", True)
        else:
            self._set_row_visible("stores", False)

        # Features (Steam categories / GOG features)
        features = game.get("features", [])
        if features:
            self.features_value.setText(", ".join(str(f) for f in features[:10]))
        self._set_row_visible("features", bool(features))

        # Game Modes
        game_modes = game.get("game_modes", [])
        if game_modes:
            labels = []
            for mode in game_modes:
                filter_label = GAME_MODE_FILTERS.get(mode)
                if filter_label:
                    labels.append(_(filter_label))
            self.game_modes_info_value.setText(", ".join(labels))
        self._set_row_visible("game_modes_info", bool(game_modes))

        # Languages
        langs = game.get("supported_languages", [])
        if langs:
            self.languages_value.setText(
                ", ".join(str(lang) for lang in langs[:15])
            )
        self._set_row_visible("languages", bool(langs))

        # Controller support
        controller = game.get("controller_support", "")
        if controller:
            if isinstance(controller, bool):
                self.controller_value.setText(_("Yes"))
            else:
                self.controller_value.setText(
                    str(controller).replace("_", " ").title()
                )
            self._set_row_visible("controller", True)
        else:
            self._set_row_visible("controller", False)

        # Controls (PCGW)
        controls = game.get("controls", [])
        if controls:
            self.controls_value.setText(", ".join(str(c) for c in controls))
        self._set_row_visible("controls", bool(controls))

        # Monetization (PCGW — combine monetization + microtransactions)
        monetization = game.get("monetization", [])
        microtx = game.get("microtransactions", [])
        all_monetization = list(monetization) + [
            _("MTX: {item}").format(item=m) for m in microtx if m not in monetization
        ]
        if all_monetization:
            self.monetization_value.setText(
                ", ".join(str(m) for m in all_monetization)
            )
        self._set_row_visible("monetization", bool(all_monetization))

        # Rating (IGDB user_rating + Steam positive/negative)
        user_rating = game.get("user_rating")
        user_rating_count = game.get("user_rating_count")
        rating_positive = game.get("rating_positive")
        rating_negative = game.get("rating_negative")
        rating_text = ""
        if user_rating:
            rating_text = _("{rating}/100").format(rating=f"{user_rating:.0f}")
            if user_rating_count:
                if user_rating_count >= 1000:
                    rating_text += _(" ({count}k)").format(count=f"{user_rating_count / 1000:.1f}")
                else:
                    rating_text += _(" ({count})").format(count=user_rating_count)
        if rating_positive is not None and rating_negative is not None:
            total = rating_positive + rating_negative
            if total > 0:
                pct = round(rating_positive / total * 100, 1)
                if not user_rating:
                    rating_text = f"{pct:.0f}%"
                rating_text += _(" ({positive}\u2191 {negative}\u2193)").format(
                    positive=f"{rating_positive:,}", negative=f"{rating_negative:,}"
                )
        if rating_text:
            self.rating_value.setText(rating_text)
            self._set_row_visible("rating", True)
        else:
            self._set_row_visible("rating", False)

        # Metacritic (clickable link when URL available)
        metacritic = game.get("critic_rating")
        critic_url = game.get("critic_rating_url", "")
        if metacritic:
            if critic_url:
                from html import escape
                safe_url = escape(critic_url, quote=True)
                link_color = self.metacritic_value.palette().link().color().name()
                self.metacritic_value.setText(
                    f'<a href="{safe_url}" style="color:{link_color}">{metacritic}</a>'
                )
            else:
                self.metacritic_value.setText(str(metacritic))
            self._set_row_visible("metacritic", True)
        else:
            self._set_row_visible("metacritic", False)

        # ProtonDB
        protondb = game.get("protondb_rating", "")
        if protondb:
            steam_id = game.get("store_app_ids", {}).get("steam")
            if steam_id:
                url = f"https://www.protondb.com/app/{steam_id}"
                self.protondb_value.setText(
                    f"<a href='{url}'>{protondb.title()}</a>"
                )
            else:
                self.protondb_value.setText(protondb.title())
            self._set_row_visible("protondb", True)
        else:
            self._set_row_visible("protondb", False)

        # Steam Deck
        steam_deck = game.get("steam_deck_compat", "")
        if steam_deck:
            self.steam_deck_value.setText(steam_deck.title())
            self._set_row_visible("steam_deck", True)
        else:
            self._set_row_visible("steam_deck", False)

        # Achievements
        achievements = game.get("achievements")
        if achievements and achievements > 0:
            self.achievements_value.setText(f"{achievements:,}")
            self._set_row_visible("achievements", True)
        else:
            self._set_row_visible("achievements", False)

        # Avg. Playtime (minutes -> formatted)
        avg_playtime = game.get("average_playtime_forever")
        if avg_playtime and avg_playtime > 0:
            hours = avg_playtime // 60
            mins = avg_playtime % 60
            if hours > 0:
                txt = _("{hours}h {mins}m").format(
                    hours=hours, mins=mins
                )
                self.avg_playtime_value.setText(txt)
            else:
                self.avg_playtime_value.setText(_("{mins}m").format(mins=mins))
            self._set_row_visible("avg_playtime", True)
        else:
            self._set_row_visible("avg_playtime", False)

        # Peak Players
        peak_ccu = game.get("peak_ccu")
        if peak_ccu and peak_ccu > 0:
            self.peak_players_value.setText(f"{peak_ccu:,}")
            self._set_row_visible("peak_players", True)
        else:
            self._set_row_visible("peak_players", False)

        # Owners
        owners = game.get("estimated_owners", "")
        if owners:
            self.owners_value.setText(owners)
        self._set_row_visible("owners", bool(owners))

        # Recommendations
        recs = game.get("recommendations")
        if recs and recs > 0:
            self.recommendations_value.setText(f"{recs:,}")
            self._set_row_visible("recommendations", True)
        else:
            self._set_row_visible("recommendations", False)

        # Age ratings
        age_ratings = game.get("age_ratings", [])
        if age_ratings:
            parts = []
            for ar in age_ratings:
                system = ar.get("system", "")
                rating = ar.get("rating", "")
                if system and rating:
                    parts.append(f"{system}: {rating}")
                elif rating:
                    parts.append(rating)
            if parts:
                self.age_rating_value.setText(", ".join(parts))
                self._set_row_visible("age_rating", True)
            else:
                self._set_row_visible("age_rating", False)
        else:
            self._set_row_visible("age_rating", False)

        # Players / multiplayer
        game_modes_detail = game.get("game_modes_detail", {})
        player_parts = []
        if game_modes_detail.get("online_players"):
            player_parts.append(
                _("Online: {players}").format(players=game_modes_detail['online_players'])
            )
        if game_modes_detail.get("local_players"):
            player_parts.append(
                _("Local: {players}").format(players=game_modes_detail['local_players'])
            )
        if game_modes_detail.get("lan_players"):
            player_parts.append(
                _("LAN: {players}").format(players=game_modes_detail['lan_players'])
            )
        if player_parts:
            self.players_value.setText(", ".join(player_parts))
            self._set_row_visible("players", True)
        else:
            self._set_row_visible("players", False)

        # Crossplay
        crossplay = game_modes_detail.get("crossplay")
        if crossplay:
            cp_text = _("Yes")
            cp_platforms = game_modes_detail.get("crossplay_platforms")
            if cp_platforms:
                cp_text += f" ({cp_platforms})"  # Platform names are proper nouns
            self.crossplay_value.setText(cp_text)
            self._set_row_visible("crossplay", True)
        else:
            self._set_row_visible("crossplay", False)

        # Links / websites
        _LINK_DISPLAY_NAMES = {
            "official": _("Official"),
            "steam": "Steam",
            "gog": "GOG",
            "epic": "Epic Games",
            "wikipedia": "Wikipedia",
            "reddit": "Reddit",
            "discord": "Discord",
        }
        websites = game.get("links") or game.get("websites", [])
        if websites:
            import html as html_mod

            link_color = self.links_value.palette().link().color().name()
            link_parts = []
            for w in websites[:8]:
                wtype = w.get("type", "link")
                url = w.get("url", "")
                if url:
                    escaped_url = html_mod.escape(url)
                    display_name = _LINK_DISPLAY_NAMES.get(wtype, wtype.title())
                    escaped_name = html_mod.escape(display_name)
                    link_parts.append(
                        f'<a href="{escaped_url}" '
                        f'style="color:{link_color}">'
                        f'{escaped_name}</a>'
                    )
            if link_parts:
                self.links_value.setText(" · ".join(link_parts))
                self._set_row_visible("links", True)
            else:
                self._set_row_visible("links", False)
        else:
            self._set_row_visible("links", False)

        # Storyline (IGDB)
        storyline = game.get("storyline", "")
        if storyline:
            display = storyline[:200] + "..." if len(storyline) > 200 else storyline
            self.storyline_value.setText(display)
        self._set_row_visible("storyline", bool(storyline))

    def set_tags(self, tags: List[Dict[str, Any]]) -> None:
        """Set tag chips in the panel."""
        # Remove existing chips (keep stretch at end)
        for chip in self._tag_chips:
            self.tags_layout.removeWidget(chip)
            chip.deleteLater()
        self._tag_chips.clear()

        # Insert chips before the stretch
        insert_pos = 0
        for tag in tags:
            chip = TagChip(tag.get("name", ""), tag.get("color", ""))
            chip.tag_clicked.connect(
                lambda name: self.filter_tag_requested.emit([name])
            )
            self.tags_layout.insertWidget(insert_pos, chip)
            self._tag_chips.append(chip)
            insert_pos += 1

        self._set_row_visible("tags", bool(tags))

    def clear(self) -> None:
        """Hide all metadata rows and clear tags."""
        for name in self._rows:
            self._set_row_visible(name, False)
        self.set_tags([])


class ListView(QWidget):
    """Detail view for selected game with tabbed interface

    Layout:
    +--------------------------------------------------------------------------------+
    | Title of the Game                    [ About ] [ Settings ] [ Stats ] [ Files ] [ Notes ]
    +--------------------------------------------------------------------------------+
    |                                                                                |
    |  Tab content (scrollable)                                                      |
    |                                                                                |
    +--------------------------------------------------------------------------------+

    Signals:
        game_launched: Emitted when launch button clicked (game_id, store)
        favorite_toggled: Emitted when favorite button toggled
        description_refresh_requested: Emitted when plain text description needs HTML refresh
        edit_tags_requested: Emitted when user wants to edit tags for a game
        view_screenshots_requested: Emitted when user clicks a screenshot (game_id, index)
        notes_changed: Emitted when user saves notes (game_id, notes_text)
        platform_changed: Emitted when platform selection changes (game_id, platform_id)
    """

    game_launched = Signal(str, str)  # game_id, store_name
    game_launched_via_runner = Signal(str, str, str)  # game_id, store_name, runner_name
    game_install_requested = Signal(str, str)  # game_id, store_name
    favorite_toggled = Signal(str, bool)  # game_id, is_favorite
    hidden_toggled = Signal(str, bool)  # game_id, is_hidden
    description_refresh_requested = Signal(str, str, str)  # game_uuid, store_app_id, store_name
    edit_tags_requested = Signal(str)  # game_id
    view_screenshots_requested = Signal(str, int)  # game_id, screenshot_index
    notes_changed = Signal(str, str)  # game_id, notes_text
    platform_changed = Signal(str, str)  # game_id, platform_id
    settings_changed = Signal(str, dict)  # game_id, launch_config_dict
    filter_developer_requested = Signal(list)   # [developer_name]
    filter_publisher_requested = Signal(list)   # [publisher_name]
    filter_genre_requested = Signal(list)       # [genre_name]
    filter_tag_requested = Signal(list)         # [tag_name]
    filter_year_requested = Signal(list)        # [year_string]

    # Tab indices
    TAB_ABOUT = 0
    TAB_SETTINGS = 1
    TAB_STATS = 2
    TAB_FILES = 3
    TAB_NOTES = 4

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._game: Optional[Dict[str, Any]] = None
        self._running_game_id: Optional[str] = None
        self._screenshot_callback = None  # Callback to fetch screenshots
        self._description_callback = None  # Callback to fetch descriptions
        self._borrowed_from_callback = None  # Callback to resolve steamid to name
        self._ensure_metadata_callback = None  # Callback to fill missing metadata
        self._detail_fields_callback = None  # Callback to fetch detail fields on demand
        self._metadata_worker = None  # Background worker for async metadata fetch
        self._platform_manager = None  # RuntimeManager for settings tab
        self._game_manager = None  # GameManager for files tab
        self._runner_query = None  # Callback: store_name -> runner display name or None

        self._setup_ui()

    def set_screenshot_callback(self, callback) -> None:
        """Set callback for lazy-loading screenshots.

        The callback should take a game_id and return a list of screenshot URLs.

        Args:
            callback: Function(game_id: str) -> List[str]
        """
        self._screenshot_callback = callback

    def set_description_callback(self, callback) -> None:
        """Set callback for lazy-loading descriptions.

        The callback should take a game_id and return the description HTML.

        Args:
            callback: Function(game_id: str) -> str
        """
        self._description_callback = callback

    def set_borrowed_from_callback(self, callback) -> None:
        """Set callback for resolving family member steamid to name.

        The callback should take a steamid and return the display name.

        Args:
            callback: Function(steamid: str) -> str
        """
        self._borrowed_from_callback = callback

    def set_ensure_metadata_callback(self, callback) -> None:
        """Set callback for ensuring metadata is complete.

        The callback is called when displaying game details to fill in any
        missing metadata fields (cover, description, etc.) from fallback sources.

        Args:
            callback: Function(game_id: str) -> Dict[str, Any]
        """
        self._ensure_metadata_callback = callback

    def set_detail_fields_callback(self, callback) -> None:
        """Set callback for lazy-loading detail fields.

        The callback is called when metadata worker returns results to merge
        detail fields before displaying.

        Args:
            callback: Function(game_id: str) -> Dict[str, Any]
        """
        self._detail_fields_callback = callback

    def set_runner_query(self, callback) -> None:
        """Set callback for querying runner availability per store.

        Args:
            callback: Function(store_name: str) -> Optional[str] (runner display name)
        """
        self._runner_query = callback

    def _start_metadata_worker(self, game_id: str) -> None:
        """Start background worker to fetch missing metadata for a game."""
        # Cancel any previous worker (user clicked another game quickly)
        if self._metadata_worker is not None:
            try:
                self._metadata_worker.finished.disconnect()
            except RuntimeError:
                pass  # Already disconnected
            self._metadata_worker.quit()
            self._metadata_worker = None
            # Pop the old worker's WaitCursor before pushing a new one
            QApplication.restoreOverrideCursor()

        # Show busy cursor while fetching
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        worker = DataLoaderWorker(
            lambda gid=game_id: self._ensure_metadata_callback(gid),
            parent=self,
        )
        worker.finished.connect(
            lambda result, gid=game_id: self._on_metadata_ready(gid, result)
        )
        worker.error.connect(lambda _: self._on_metadata_worker_done())
        self._metadata_worker = worker
        worker.start()

    def _on_metadata_worker_done(self) -> None:
        """Restore cursor after metadata worker finishes."""
        QApplication.restoreOverrideCursor()

    def _on_metadata_ready(self, game_id: str, result) -> None:
        """Handle background metadata fetch completion."""
        self._metadata_worker = None
        self._on_metadata_worker_done()
        # Only update if user is still looking at the same game
        if result and self._game and self._game.get("id") == game_id:
            if self._detail_fields_callback:
                detail = self._detail_fields_callback(game_id)
                if detail:
                    result = {**result, **detail}
            self.set_game(result, _from_worker=True)

    def set_store_url_callback(self, callback) -> None:
        """Set callback for getting store page URLs.

        The callback should take a store name and app_id, and return the store page URL.

        Args:
            callback: Function(store_name: str, app_id: str) -> str
        """
        self._store_url_callback = callback

    def set_managers(
        self,
        runtime_manager=None,
        game_manager=None,
    ) -> None:
        """Set manager references for Settings and Files tabs.

        Args:
            runtime_manager: RuntimeManager for platform selection
            game_manager: GameManager for installation tracking
        """
        self._platform_manager = runtime_manager
        self._game_manager = game_manager
        # Populate platform dropdown if manager is set
        if runtime_manager:
            self._populate_platforms()

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

    def _has_html_tags(self, text: str) -> bool:
        """Check if text contains HTML tags.

        Args:
            text: Text to check

        Returns:
            True if text contains HTML tags
        """
        import re
        if not text:
            return False
        return bool(re.search(r'<[a-zA-Z][^>]*>', text))

    def update_description(self, game_id: str, description: str) -> None:
        """Update the description for currently displayed game.

        Called after fetching HTML description from API.

        Args:
            game_id: Game ID to verify we're still showing the same game
            description: New HTML description
        """
        if self._game and self._game.get("id") == game_id:
            # Re-render the description (cache managed by LazyMetadata)
            self.description_view.setHtml(self._prepare_description_html(description))

    def _setup_ui(self) -> None:
        """Create list view layout with tabbed interface"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # === Header with title and tab buttons ===
        self.header = QFrame()
        self.header.setObjectName("listViewHeader")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(16, 8, 16, 8)
        header_layout.setSpacing(12)

        # Title
        self.title_label = QLabel()
        self.title_label.setObjectName("gameTitle")
        self.title_label.setWordWrap(True)
        header_layout.addWidget(self.title_label, 1)

        # Game mode badges container (between title and tabs)
        self.game_modes_container = QWidget()
        self.game_modes_layout = QHBoxLayout(self.game_modes_container)
        self.game_modes_layout.setContentsMargins(0, 0, 0, 0)
        self.game_modes_layout.setSpacing(4)
        self.game_modes_container.setVisible(False)
        header_layout.addWidget(self.game_modes_container)

        # Compatibility badges (ProtonDB, Steam Deck)
        self.header_protondb_badge = QLabel()
        self.header_protondb_badge.setObjectName("protondbBadge")
        self.header_protondb_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_protondb_badge.installEventFilter(self)
        self.header_protondb_badge.setVisible(False)
        header_layout.addWidget(self.header_protondb_badge)

        self.header_deck_badge = QLabel()
        self.header_deck_badge.setObjectName("steamDeckBadge")
        self.header_deck_badge.setVisible(False)
        header_layout.addWidget(self.header_deck_badge)

        # Family shared badge (moved from action bar)
        self.header_fs_badge = QLabel(_("FS"))
        self.header_fs_badge.setObjectName("familySharedBadge")
        self.header_fs_badge.setVisible(False)
        header_layout.addWidget(self.header_fs_badge)

        # Installed badge
        self.header_installed_badge = QLabel(_(INSTALLED_BADGE_LABEL))
        self.header_installed_badge.setObjectName("installedBadge")
        self.header_installed_badge.setVisible(False)
        header_layout.addWidget(self.header_installed_badge)

        # Separator between badges and tabs
        self.header_separator = QFrame()
        self.header_separator.setFrameShape(QFrame.Shape.VLine)
        self.header_separator.setObjectName("headerSeparator")
        self.header_separator.setVisible(False)
        header_layout.addWidget(self.header_separator)

        # Tab buttons (styled like toolbar view mode buttons)
        self.tab_button_group = QButtonGroup(self)
        self.tab_button_group.setExclusive(True)

        tab_frame = QFrame()
        tab_frame.setObjectName("viewModeGroup")  # Reuse toolbar view mode styling
        tab_layout = QHBoxLayout(tab_frame)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(0)

        self.btn_about = QPushButton(_("About"))
        self.btn_about.setCheckable(True)
        self.btn_about.setChecked(True)
        self.btn_about.setToolTip(_("Description and screenshots"))
        self.tab_button_group.addButton(self.btn_about, self.TAB_ABOUT)
        tab_layout.addWidget(self.btn_about)

        self.btn_settings = QPushButton(_("Settings"))
        self.btn_settings.setCheckable(True)
        self.btn_settings.setToolTip(_("Game-specific settings"))
        self.tab_button_group.addButton(self.btn_settings, self.TAB_SETTINGS)
        tab_layout.addWidget(self.btn_settings)

        self.btn_stats = QPushButton(_("Stats"))
        self.btn_stats.setCheckable(True)
        self.btn_stats.setToolTip(_("Play time and statistics"))
        self.tab_button_group.addButton(self.btn_stats, self.TAB_STATS)
        tab_layout.addWidget(self.btn_stats)

        self.btn_files = QPushButton(_("Files"))
        self.btn_files.setCheckable(True)
        self.btn_files.setToolTip(_("Game files and install location"))
        self.tab_button_group.addButton(self.btn_files, self.TAB_FILES)
        tab_layout.addWidget(self.btn_files)

        self.btn_notes = QPushButton(_("Notes"))
        self.btn_notes.setCheckable(True)
        self.btn_notes.setToolTip(_("Your personal notes about this game"))
        self.tab_button_group.addButton(self.btn_notes, self.TAB_NOTES)
        tab_layout.addWidget(self.btn_notes)

        header_layout.addWidget(tab_frame)
        main_layout.addWidget(self.header)

        # Connect tab buttons
        self.tab_button_group.idClicked.connect(self._on_tab_clicked)

        # === Stacked widget for tab content ===
        self.tab_stack = QStackedWidget()
        main_layout.addWidget(self.tab_stack, 1)

        # Create all tabs
        self._setup_about_tab()
        self._setup_settings_tab()
        self._setup_stats_tab()
        self._setup_files_tab()
        self._setup_notes_tab()

        # Placeholder for empty state
        self.placeholder = QLabel(_("Select a game from the list"))
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setVisible(False)

    def _on_tab_clicked(self, tab_id: int) -> None:
        """Handle tab button click"""
        self.tab_stack.setCurrentIndex(tab_id)

    def _create_back_to_about_button(self) -> QPushButton:
        """Create a 'Back to About' button for non-About tabs."""
        btn = QPushButton(_("Back to About"))
        btn.setObjectName("backToAboutButton")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self.set_active_tab(self.TAB_ABOUT))
        return btn

    def set_active_tab(self, tab_index: int) -> None:
        """Switch to specified tab programmatically.

        Args:
            tab_index: Tab index (TAB_ABOUT=0, TAB_SETTINGS=1, etc.)
        """
        if 0 <= tab_index <= self.TAB_NOTES:
            button = self.tab_button_group.button(tab_index)
            if button:
                button.setChecked(True)
            self.tab_stack.setCurrentIndex(tab_index)

    def set_default_store(self, store_name: str) -> None:
        """Set default store for launch button priority ordering.

        Only affects Play button ordering. Metadata priority is separate.

        Args:
            store_name: Default store name (e.g., "steam", "gog", "epic")
        """
        self._default_store = store_name

    def _setup_about_tab(self) -> None:
        """Create the About tab (hero banner with carousel, description, metadata)

        Layout:
        +-------------------------------------------------------+
        | Hero Banner (background image + action buttons)        |
        |                        [Play] [Fav] [Store]  (top-right)|
        |  [screenshot1] [screenshot2] [screenshot3]  (bottom)   |
        +----------------------------------+--------------------+
        | Description (scrollable HTML)    | Metadata Panel     |
        |                                  | Developer: ...     |
        |                                  | Publisher: ...     |
        |                                  | Genre: ...         |
        +----------------------------------+--------------------+
        """
        about_page = QWidget()
        about_page.setObjectName("listViewContent")
        about_layout = QVBoxLayout(about_page)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_layout.setSpacing(0)

        # === Hero Banner (full width, fixed height, contains carousel) ===
        self.hero_banner = HeroBanner()
        self.hero_banner.favorite_btn.clicked.connect(self._on_favorite_clicked)

        # Screenshot carousel overlays the bottom of the hero banner
        self.carousel = ScreenshotCarousel()
        self.carousel.screenshot_clicked.connect(self._on_screenshot_clicked)
        self.hero_banner.set_carousel(self.carousel)

        about_layout.addWidget(self.hero_banner)

        # Alias action widgets for backward compatibility with existing methods
        self.launch_buttons_container = self.hero_banner.launch_container
        self.launch_buttons_layout = self.hero_banner.launch_layout
        self.favorite_btn = self.hero_banner.favorite_btn
        self.store_btn = self.hero_banner.store_btn
        self._store_url_callback = None
        self._store_btn_primary_store = None

        # === Two-column layout below the banner ===
        self.about_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.about_splitter.setObjectName("aboutSplitter")
        splitter = self.about_splitter

        # Left column: description only (stretches — carousel is in banner now)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(16, 8, 8, 8)
        left_layout.setSpacing(8)

        self.description_view = RemoteImageTextBrowser()
        self.description_view.setObjectName("descriptionView")
        desc_font = self.description_view.font()
        desc_font.setPointSize(QApplication.instance().font().pointSize() + 1)
        self.description_view.setFont(desc_font)
        self.description_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.description_view.setMinimumHeight(100)
        self.description_view.setOpenExternalLinks(False)
        self.description_view.setOpenLinks(False)
        self.description_view.anchorClicked.connect(self._on_description_link)
        self.description_view.setHtml("")
        left_layout.addWidget(self.description_view, 1)

        # Right column: metadata panel (resizable via splitter)
        self.metadata_panel = MetadataPanel()
        self.metadata_panel.filter_developer_requested.connect(
            self.filter_developer_requested.emit
        )
        self.metadata_panel.filter_publisher_requested.connect(
            self.filter_publisher_requested.emit
        )
        self.metadata_panel.filter_genre_requested.connect(
            self.filter_genre_requested.emit
        )
        self.metadata_panel.filter_tag_requested.connect(
            self.filter_tag_requested.emit
        )
        self.metadata_panel.filter_year_requested.connect(
            self.filter_year_requested.emit
        )

        splitter.addWidget(left_widget)
        splitter.addWidget(self.metadata_panel)
        splitter.setSizes([700, 350])
        splitter.setStretchFactor(0, 1)  # content stretches
        splitter.setStretchFactor(1, 0)  # metadata fixed width

        about_layout.addWidget(splitter, 1)
        self.tab_stack.addWidget(about_page)

    def _setup_settings_tab(self) -> None:
        """Create the Settings tab (platform selection, launch arguments)"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Platform selection
        platform_group = QGroupBox(_("Platform"))
        platform_layout = QFormLayout(platform_group)

        self.platform_combo = QComboBox()
        self.platform_combo.setMinimumWidth(250)
        self.platform_combo.addItem(_("Default (Store Launcher)"), "default")
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        platform_layout.addRow(_("Platform:"), self.platform_combo)

        platform_hint = QLabel(_("Select a platform to use for launching this game."))
        platform_hint.setObjectName("hintLabel")
        platform_hint.setWordWrap(True)
        platform_layout.addRow("", platform_hint)

        self.no_runner_warning = QLabel(_("No compatible launcher detected"))
        self.no_runner_warning.setObjectName("warningLabel")
        self.no_runner_warning.setWordWrap(True)
        self.no_runner_warning.setVisible(False)
        platform_layout.addRow("", self.no_runner_warning)

        layout.addWidget(platform_group)

        # Launch arguments
        args_group = QGroupBox(_("Launch Arguments"))
        args_layout = QFormLayout(args_group)

        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText(_("e.g., -windowed -nosplash"))
        args_layout.addRow(_("Arguments:"), self.args_edit)

        self.working_dir_edit = QLineEdit()
        self.working_dir_edit.setPlaceholderText(_("Leave empty for default"))
        args_layout.addRow(_("Working Dir:"), self.working_dir_edit)

        layout.addWidget(args_group)

        # Environment variables
        env_group = QGroupBox(_("Environment Variables"))
        env_layout = QVBoxLayout(env_group)

        self.env_edit = QPlainTextEdit()
        self.env_edit.setPlaceholderText(_("VAR1=value1\nVAR2=value2"))
        self.env_edit.setMaximumHeight(100)
        env_layout.addWidget(self.env_edit)

        env_hint = QLabel(_("One variable per line in VAR=value format."))
        env_hint.setObjectName("hintLabel")
        env_layout.addWidget(env_hint)

        layout.addWidget(env_group)

        # Wine Options (per-game overrides)
        self.wine_options_group = QGroupBox(_("Wine Options"))
        wine_opts_layout = QFormLayout(self.wine_options_group)

        self.wine_runtime_combo = QComboBox()
        self.wine_runtime_combo.addItem(_("Use global default"), "default")

        # Populate from RuntimeScanner
        try:
            from luducat.plugins.platforms.wine.runtime_scanner import (
                scan_installed_runtimes,
            )
            self._wine_runtimes = scan_installed_runtimes()
            for rt in self._wine_runtimes:
                self.wine_runtime_combo.addItem(
                    rt.display_label, rt.identifier,
                )
        except Exception:
            self._wine_runtimes = []

        wine_opts_layout.addRow(_("Runtime:"), self.wine_runtime_combo)

        self._wine_option_combos = {}
        for label, key in [
            ("ESYNC", "wine_esync"),
            ("FSYNC", "wine_fsync"),
            ("DXVK", "wine_dxvk"),
            ("MangoHud", "wine_mangohud"),
            (_("Virtual Desktop"), "wine_virtual_desktop"),
            ("Gamemode", "wine_gamemode"),
        ]:
            combo = QComboBox()
            combo.addItem(_("Default"), "default")
            combo.addItem(_("On"), True)
            combo.addItem(_("Off"), False)
            wine_opts_layout.addRow(f"{label}:", combo)
            self._wine_option_combos[key] = combo

        # Virtual Desktop resolution
        self.wine_vd_res_edit = QLineEdit()
        self.wine_vd_res_edit.setPlaceholderText(_("(Global default)"))
        self.wine_vd_res_edit.setMaximumWidth(160)
        wine_opts_layout.addRow(_("VD Resolution:"), self.wine_vd_res_edit)

        # WINEDEBUG
        self.wine_debug_combo = QComboBox()
        self.wine_debug_combo.addItem(_("Default"), "default")
        self.wine_debug_combo.addItem("fixme-all", "fixme-all")
        self.wine_debug_combo.addItem("-all", "-all")
        self.wine_debug_combo.addItem("warn+all", "warn+all")
        self.wine_debug_combo.addItem(_("(none)"), "")
        wine_opts_layout.addRow("WINEDEBUG:", self.wine_debug_combo)

        self._wine_reset_btn = QPushButton(_("Reset to global default"))
        self._wine_reset_btn.clicked.connect(self._on_reset_wine_to_global)
        wine_opts_layout.addRow("", self._wine_reset_btn)

        self.wine_options_group.setVisible(False)
        layout.addWidget(self.wine_options_group)

        # Platform configuration (DOSBox/ScummVM)
        self.platform_config_group = QGroupBox(_("Platform Configuration"))
        platform_config_layout = QVBoxLayout(self.platform_config_group)

        self.detection_hint_label = QLabel()
        self.detection_hint_label.setObjectName("hintLabel")
        self.detection_hint_label.setWordWrap(True)
        platform_config_layout.addWidget(self.detection_hint_label)

        self.btn_edit_config = QPushButton(_("Edit Configuration"))
        self.btn_edit_config.clicked.connect(self._on_edit_platform_config)
        platform_config_layout.addWidget(self.btn_edit_config)

        self.btn_config_help = QPushButton(_("Documentation"))
        self.btn_config_help.clicked.connect(self._on_config_help)
        platform_config_layout.addWidget(self.btn_config_help)

        self.platform_config_group.setVisible(False)
        layout.addWidget(self.platform_config_group)

        layout.addStretch()

        # Save/Reset row
        settings_btn_row = QHBoxLayout()
        settings_btn_row.addStretch()

        self.btn_settings_reset = QPushButton(_("Reset"))
        self.btn_settings_reset.setMinimumWidth(60)
        self.btn_settings_reset.setToolTip(_("Revert to last saved settings"))
        self.btn_settings_reset.clicked.connect(self._on_settings_reset)
        settings_btn_row.addWidget(self.btn_settings_reset)

        self.btn_settings_save = QPushButton(_("Save"))
        self.btn_settings_save.setMinimumWidth(60)
        self.btn_settings_save.clicked.connect(self._on_settings_save)
        settings_btn_row.addWidget(self.btn_settings_save)

        settings_btn_row.addWidget(self._create_back_to_about_button())
        layout.addLayout(settings_btn_row)

        # Track saved config for reset
        self._saved_launch_config = {}

        scroll.setWidget(content)
        self.tab_stack.addWidget(scroll)

    def _setup_stats_tab(self) -> None:
        """Create the Stats tab (play time, launch count)"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Play statistics
        stats_group = QGroupBox(_("Play Statistics"))
        stats_layout = QFormLayout(stats_group)

        self.lbl_play_time = QLabel(_("Not tracked"))
        stats_layout.addRow(_("Total Play Time:"), self.lbl_play_time)

        self.lbl_launch_count = QLabel("0")
        stats_layout.addRow(_("Launch Count:"), self.lbl_launch_count)

        self.lbl_last_played = QLabel(_("Never"))
        stats_layout.addRow(_("Last Played:"), self.lbl_last_played)

        self.lbl_first_played = QLabel(_("Not tracked"))
        stats_layout.addRow(_("First Played:"), self.lbl_first_played)

        layout.addWidget(stats_group)

        # Per-store playtime breakdown
        self.store_breakdown_group = QGroupBox(_("Playtime by Store"))
        self.store_breakdown_layout = QFormLayout(self.store_breakdown_group)
        self.store_breakdown_group.setVisible(False)
        layout.addWidget(self.store_breakdown_group)

        # Achievements placeholder
        achievements_group = QGroupBox(_("Achievements"))
        achievements_layout = QVBoxLayout(achievements_group)

        dev_notice = QLabel(_("This feature is in development."))
        dev_notice.setObjectName("hintLabel")
        dev_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        achievements_layout.addWidget(dev_notice)

        layout.addWidget(achievements_group)

        layout.addStretch()

        back_row = QHBoxLayout()
        back_row.addStretch()
        back_row.addWidget(self._create_back_to_about_button())
        layout.addLayout(back_row)

        scroll.setWidget(content)
        self.tab_stack.addWidget(scroll)

    def _setup_files_tab(self) -> None:
        """Create the Files tab (installed files, archives)"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Installed files section
        install_group = QGroupBox(_("Installed Files"))
        install_layout = QVBoxLayout(install_group)

        self.install_status_label = QLabel(_("Not installed"))
        self.install_status_label.setObjectName("hintLabel")
        install_layout.addWidget(self.install_status_label)

        self.install_path_label = QLabel()
        self.install_path_label.setWordWrap(True)
        install_layout.addWidget(self.install_path_label)

        install_actions = QHBoxLayout()
        self.btn_verify = QPushButton(_("Verify"))
        self.btn_verify.setEnabled(False)
        self.btn_open_folder = QPushButton(_("Open Folder"))
        self.btn_open_folder.setEnabled(False)
        install_actions.addWidget(self.btn_verify)
        install_actions.addWidget(self.btn_open_folder)
        install_actions.addStretch()
        install_layout.addLayout(install_actions)

        layout.addWidget(install_group)


        layout.addStretch()

        back_row = QHBoxLayout()
        back_row.addStretch()
        back_row.addWidget(self._create_back_to_about_button())
        layout.addLayout(back_row)

        scroll.setWidget(content)
        self.tab_stack.addWidget(scroll)

    def _setup_notes_tab(self) -> None:
        """Create the Notes tab (user notes with fixed font)"""
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        # Header with buttons
        header = QHBoxLayout()
        label = QLabel(_("Personal Notes"))
        label.setObjectName("sectionHeader")
        header.addWidget(label)
        header.addStretch()

        self.btn_notes_clear = QPushButton(_("Clear"))
        self.btn_notes_clear.setMinimumWidth(60)
        self.btn_notes_clear.clicked.connect(self._on_notes_clear)
        header.addWidget(self.btn_notes_clear)

        self.btn_notes_import = QPushButton(_("Import"))
        self.btn_notes_import.setMinimumWidth(60)
        self.btn_notes_import.clicked.connect(self._on_notes_import)
        header.addWidget(self.btn_notes_import)

        self.btn_notes_export = QPushButton(_("Export"))
        self.btn_notes_export.setMinimumWidth(60)
        self.btn_notes_export.clicked.connect(self._on_notes_export)
        header.addWidget(self.btn_notes_export)

        self.btn_notes_reset = QPushButton(_("Reset"))
        self.btn_notes_reset.setMinimumWidth(60)
        self.btn_notes_reset.setToolTip(_("Revert to last saved version"))
        self.btn_notes_reset.clicked.connect(self._on_notes_reset)
        header.addWidget(self.btn_notes_reset)

        self.btn_notes_save = QPushButton(_("Save"))
        self.btn_notes_save.setMinimumWidth(60)
        self.btn_notes_save.clicked.connect(self._on_notes_save)
        header.addWidget(self.btn_notes_save)

        layout.addLayout(header)

        # Notes editor with fixed font and scrollbars
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            _("Add your personal notes about this game here...") + "\n\n"
            + _("- Tips and tricks") + "\n"
            + _("- Current progress") + "\n"
            + _("- Mod configurations") + "\n"
            + _("- Anything you want to remember")
        )
        # Fixed-width font for notes
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.notes_edit.setFont(font)
        self.notes_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.notes_edit, 1)

        # Hint
        hint = QLabel(_("Notes are saved per-game and stored locally."))
        hint.setObjectName("hintLabel")
        layout.addWidget(hint)

        back_row = QHBoxLayout()
        back_row.addStretch()
        back_row.addWidget(self._create_back_to_about_button())
        layout.addLayout(back_row)

        # Track last saved notes for reset
        self._last_saved_notes = ""

        self.tab_stack.addWidget(content)

    def _on_platform_changed(self, index: int) -> None:
        """Handle platform selection change"""
        if self._game:
            platform_id = self.platform_combo.currentData()
            if platform_id:
                game_id = self._game.get("id", "")
                self.platform_changed.emit(game_id, platform_id)

        # Show/hide platform config for DOSBox/ScummVM and Wine options
        self._update_platform_config_visibility()
        self._update_wine_options_visibility()

    def _on_settings_save(self) -> None:
        """Save per-game launch configuration."""
        if not self._game:
            return

        config = {}

        combo_val = self.platform_combo.currentData() or "default"
        if combo_val.startswith("runner/"):
            config["runner"] = combo_val[7:]
        elif combo_val != "default":
            config["platform"] = combo_val

        args = self.args_edit.text().strip()
        if args:
            config["launch_args"] = args

        wd = self.working_dir_edit.text().strip()
        if wd:
            config["working_dir"] = wd

        env_text = self.env_edit.toPlainText().strip()
        if env_text:
            env = {}
            for line in env_text.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
            if env:
                config["environment"] = env

        # Preserve Wine-specific fields set via exe selection dialog
        if self._saved_launch_config:
            for key in ("wine_exe", "wine_binary"):
                if key in self._saved_launch_config and key not in config:
                    config[key] = self._saved_launch_config[key]

        # Wine per-game overrides
        if self.wine_options_group.isVisible():
            rt_data = self.wine_runtime_combo.currentData()
            if rt_data not in ("default", ""):
                config["wine_runtime"] = rt_data

            for key, combo in self._wine_option_combos.items():
                val = combo.currentData()
                if val != "default":
                    config[key] = val

            vd_res = self.wine_vd_res_edit.text().strip()
            if vd_res:
                config["wine_virtual_desktop_resolution"] = vd_res

            debug_val = self.wine_debug_combo.currentData()
            if debug_val != "default":
                config["wine_winedebug"] = debug_val

        self._saved_launch_config = dict(config)
        self.settings_changed.emit(self._game.get("id", ""), config)
        # Refresh launch buttons to reflect runner/platform override
        stores = self._game.get("stores", [])
        self._update_launch_buttons(stores, self._game)

    def _on_settings_reset(self) -> None:
        """Revert settings widgets to last saved values."""
        if self._game and hasattr(self, "_saved_launch_config"):
            self._update_settings_tab(self._game)
            # Refresh launch buttons to revert override text
            stores = self._game.get("stores", [])
            self._update_launch_buttons(stores, self._game)

    def _on_reset_wine_to_global(self) -> None:
        """Reset all per-game Wine overrides to global defaults."""
        reply = QMessageBox.question(
            self,
            _("Reset Wine Settings"),
            _("Clear all per-game Wine overrides and use global defaults?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Reset UI widgets
        self.wine_runtime_combo.setCurrentIndex(0)
        for combo in self._wine_option_combos.values():
            combo.setCurrentIndex(0)
        self.wine_vd_res_edit.clear()
        self.wine_debug_combo.setCurrentIndex(0)

        # Save immediately (clears all wine_* keys from config)
        self._on_settings_save()

    def _update_platform_config_visibility(self) -> None:
        """Show platform config group for DOSBox/ScummVM platforms."""
        platform_id = self.platform_combo.currentData() or ""
        is_dosbox = "dosbox" in str(platform_id).lower()
        is_scummvm = "scummvm" in str(platform_id).lower()

        self.platform_config_group.setVisible(is_dosbox or is_scummvm)

        if is_dosbox:
            self.detection_hint_label.setText(
                _("DOSBox configuration for this game. "
                  "Per-game configs override global settings.")
            )
            self._config_doc_url = "https://dosbox-staging.github.io/"
        elif is_scummvm:
            self.detection_hint_label.setText(
                _("ScummVM configuration for this game.")
            )
            self._config_doc_url = "https://docs.scummvm.org/"
        else:
            self._config_doc_url = ""

    def _update_wine_options_visibility(self) -> None:
        """Show Wine Options group when Wine platform is selected."""
        platform_id = self.platform_combo.currentData() or ""
        is_wine = "wine" in str(platform_id).lower()
        self.wine_options_group.setVisible(is_wine)

    def _on_edit_platform_config(self) -> None:
        """Open config editor for the selected platform."""
        if not self._game:
            return

        platform_id = self.platform_combo.currentData() or ""
        game_id = self._game.get("id", "")
        game_title = self._game.get("title", "")

        if "dosbox" in str(platform_id).lower():
            self._edit_dosbox_config(game_id, game_title)
        elif "scummvm" in str(platform_id).lower():
            self._edit_scummvm_config(game_id, game_title)

    def _edit_dosbox_config(self, game_id: str, game_title: str) -> None:
        """Open DOSBox config editor."""
        from luducat.ui.dialogs.config_editor import ConfigEditorDialog

        try:
            from luducat.plugins.platforms.dosbox.config_manager import (
                generate_default_config,
            )
            if self._platform_manager:
                plugin = self._platform_manager.get_platform_provider("dosbox")
                if plugin:
                    mgr = plugin._get_config_manager()
                    config_text = mgr.get_game_config(game_id)
                    if config_text is None:
                        config_text = generate_default_config()

                    dialog = ConfigEditorDialog(
                        _("DOSBox Config: {}").format(game_title),
                        config_text,
                        doc_url="https://dosbox-staging.github.io/",
                        parent=self,
                    )
                    if dialog.exec() == ConfigEditorDialog.DialogCode.Accepted:
                        result = dialog.get_text()
                        if result is not None:
                            mgr.set_game_config(game_id, result)
        except Exception as e:
            logger.warning("Failed to open DOSBox config editor: %s", e)

    def _edit_scummvm_config(self, game_id: str, game_title: str) -> None:
        """Open ScummVM config info dialog (read-only for pre-release)."""
        from luducat.ui.dialogs.config_editor import ConfigEditorDialog

        info_text = _(
            "ScummVM games are configured through ScummVM's built-in "
            "options menu.\n\n"
            "Use the ScummVM launcher to adjust graphics, audio, "
            "and game-specific settings."
        )
        dialog = ConfigEditorDialog(
            _("ScummVM: {}").format(game_title),
            info_text,
            doc_url="https://docs.scummvm.org/",
            read_only=True,
            parent=self,
        )
        dialog.exec()

    def _on_config_help(self) -> None:
        """Open documentation URL for the selected platform."""
        url = getattr(self, "_config_doc_url", "")
        if url:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(url))

    def _populate_platforms(self, game: Optional[Dict[str, Any]] = None) -> None:
        """Populate platform/runner dropdown from RuntimeManager.

        Args:
            game: When provided, filter runners to those compatible with
                  the game's stores. Platform providers and Native are
                  always shown (store-agnostic).
        """
        self.platform_combo.blockSignals(True)
        self.platform_combo.clear()
        self.platform_combo.addItem(_("Default (Store Launcher)"), "default")

        if not self._platform_manager:
            self.platform_combo.blockSignals(False)
            self.no_runner_warning.setVisible(False)
            return

        has_compatible = False

        try:
            runners = self._platform_manager.get_available_runners()

            # Build set of compatible runners when a game is selected
            stores = game.get("stores", []) if game else []
            compatible: Optional[set] = None
            if stores:
                compatible = set()
                for store in stores:
                    compatible.update(
                        self._platform_manager.get_runners_for_store(store)
                    )

            # Determine default runner (top priority for primary store)
            default_runner = None
            if stores:
                primary_store = stores[0]
                store_runners = self._platform_manager.get_runners_for_store(
                    primary_store
                )
                if store_runners:
                    default_runner = store_runners[0]

            # Add runner plugins filtered by store compatibility
            for runner_name, info in runners.items():
                if compatible is not None:
                    # Always show native (manual assignment, store-agnostic)
                    if runner_name != "native" and runner_name not in compatible:
                        continue
                if runner_name != "native":
                    has_compatible = True
                display = getattr(info, "runner_name", runner_name).capitalize()
                if default_runner and runner_name == default_runner:
                    display += " " + _("(default)")
                self.platform_combo.addItem(display, f"runner/{runner_name}")

            # Platform providers (DOSBox, ScummVM, Wine) — always shown
            platforms = self._platform_manager.get_available_platforms()
            for plat in platforms:
                name = getattr(plat, "name", _("Unknown"))
                version = getattr(plat, "version", "")
                label = f"{name} {version}".strip() if version else name
                pid = getattr(plat, "platform_id", "")
                self.platform_combo.addItem(label, pid)
                has_compatible = True
        except Exception as e:
            logger.warning("Failed to populate platforms: %s", e)
        finally:
            self.platform_combo.blockSignals(False)

        # Show warning if no compatible runners or platforms found
        # (only "Default" in combo, no actual runner/platform entries)
        show_warning = game is not None and not has_compatible
        self.no_runner_warning.setVisible(show_warning)
        if show_warning:
            self.platform_combo.setVisible(False)
        else:
            self.platform_combo.setVisible(True)

    def _update_default_platform_label(self, game: Dict[str, Any]) -> None:
        """Update the 'Default' combo item to show which runner will be used.

        Resolves the primary store for the game and finds the top-priority
        runner, then updates the label accordingly:
        - "Default (Steam)" when runner matches the store name
        - "Default (GOG — Heroic)" when runner differs from store
        - "Default (Store Launcher)" when no runner detected
        """
        if not self._platform_manager or self.platform_combo.count() == 0:
            return

        fallback = _("Default (Store Launcher)")

        stores = game.get("stores", [])
        if not stores:
            self.platform_combo.setItemText(0, fallback)
            return

        # Resolve primary store using same priority logic as launch buttons
        default = getattr(self, "_default_store", "")
        store_priority = [default] + [
            s for s in PluginManager.get_store_plugin_names() if s != default
        ]

        def priority_key(s: str) -> int:
            s_lower = s.lower()
            if s_lower in store_priority:
                return store_priority.index(s_lower)
            return len(store_priority)

        sorted_stores = sorted(stores, key=priority_key)
        primary_store = sorted_stores[0]

        runners = self._platform_manager.get_runners_for_store(primary_store)
        if not runners:
            store_display = PluginManager.get_store_display_name(primary_store)
            self.platform_combo.setItemText(
                0, _("Default ({name})").format(name=store_display)
            )
            return

        top_runner = runners[0]
        store_display = PluginManager.get_store_display_name(primary_store)
        runner_display = PluginManager.get_store_display_name(top_runner)

        if top_runner.lower() == primary_store.lower():
            label = _("Default ({name})").format(name=store_display)
        else:
            label = _("Default ({store} — {runner})").format(
                store=store_display, runner=runner_display
            )

        self.platform_combo.setItemText(0, label)

    def _on_notes_clear(self) -> None:
        """Clear notes editor"""
        self.notes_edit.clear()

    def _on_notes_import(self) -> None:
        """Import notes from file"""
        from PySide6.QtWidgets import QFileDialog
        path, _filter = QFileDialog.getOpenFileName(
            self, _("Import Notes"), "", _("Text Files (*.txt);;All Files (*)")
        )
        if path:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self.notes_edit.setPlainText(f.read())
            except Exception as e:
                logger.warning(f"Failed to import notes: {e}")

    def _on_notes_export(self) -> None:
        """Export notes to file"""
        from PySide6.QtWidgets import QFileDialog
        if not self._game:
            return
        default_name = f"{self._game.get('title', 'notes')}_notes.txt"
        path, _filter = QFileDialog.getSaveFileName(
            self, _("Export Notes"), default_name, _("Text Files (*.txt);;All Files (*)")
        )
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(self.notes_edit.toPlainText())
            except Exception as e:
                logger.warning(f"Failed to export notes: {e}")

    def _on_notes_reset(self) -> None:
        """Reset notes to last saved version"""
        self.notes_edit.setPlainText(self._last_saved_notes)

    def _on_notes_save(self) -> None:
        """Save notes"""
        if self._game:
            game_id = self._game.get("id", "")
            notes = self.notes_edit.toPlainText()
            self._last_saved_notes = notes
            self.notes_changed.emit(game_id, notes)

    def _on_favorite_clicked(self) -> None:
        """Handle favorite button click"""
        if self._game:
            game_id = self._game.get("id", "")
            is_fav = self.favorite_btn.isChecked()
            self.favorite_toggled.emit(game_id, is_fav)

    def _on_store_clicked(self, store_name: str) -> None:
        """Handle store button click - opens store page in browser

        Args:
            store_name: Store to open (e.g., 'steam', 'gog', 'epic')
        """
        if not self._game or not self._store_url_callback:
            return

        from ..utils.browser import open_url

        store_app_ids = self._game.get("store_app_ids", {})
        app_id = store_app_ids.get(store_name)

        if not app_id:
            logger.warning(f"No app_id for store {store_name}")
            return

        url = self._store_url_callback(store_name, app_id)
        if url:
            logger.info(f"Opening store page: {url}")
            open_url(url)

    def _open_protondb_page(self) -> None:
        """Open ProtonDB page for the current game's Steam app ID."""
        if not self._game:
            return
        store_app_ids = self._game.get("store_app_ids", {})
        steam_id = store_app_ids.get("steam")
        if steam_id:
            from ..utils.browser import open_url
            url = f"https://www.protondb.com/app/{steam_id}"
            open_url(url)

    def eventFilter(self, obj, event):
        """Handle clicks on ProtonDB badge."""
        if obj is self.header_protondb_badge and event.type() == QEvent.Type.MouseButtonRelease:
            self._open_protondb_page()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _on_description_link(url: QUrl) -> None:
        """Open a clicked description link in the user's preferred browser."""
        from ..utils.browser import open_url
        open_url(url)

    def _setup_store_button(self, stores: List[str]) -> None:
        """Configure store button based on available stores

        Args:
            stores: List of store names
        """
        # Disconnect any existing connections
        try:
            self.store_btn.clicked.disconnect()
        except RuntimeError:
            pass

        # Clear existing menu
        self.store_btn.setMenu(None)

        if not stores:
            self.store_btn.setText(_("Store"))
            self.store_btn.setEnabled(False)
            return

        # Priority order for default store selection (same as launch buttons)
        default = getattr(self, "_default_store", "")
        others = [
            s for s in PluginManager.get_store_plugin_names()
            if s != default
        ]
        store_priority = [default] + others

        def priority_key(s: str) -> int:
            s_lower = s.lower()
            if s_lower in store_priority:
                return store_priority.index(s_lower)
            return len(store_priority)

        sorted_stores = sorted(stores, key=priority_key)
        primary_store = sorted_stores[0]

        self.store_btn.setEnabled(True)
        self._store_btn_primary_store = primary_store

        store_tip = _("Open the {name} store page in your browser").format(
            name=PluginManager.get_store_display_name(primary_store),
        )

        if len(sorted_stores) == 1:
            # Single store - simple button
            dname = PluginManager.get_store_display_name(primary_store)
            self.store_btn.setText(
                _("Store ({name})").format(name=dname)
            )
            self.store_btn.setToolTip(store_tip)
            self.store_btn.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
            self.store_btn.clicked.connect(
                lambda checked, s=primary_store: self._on_store_clicked(s)
            )
        else:
            # Multiple stores - button with dropdown
            dname = PluginManager.get_store_display_name(primary_store)
            self.store_btn.setText(
                _("Store ({name})").format(name=dname)
            )
            self.store_btn.setToolTip(store_tip)
            self.store_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)

            # Direct click opens primary store
            self.store_btn.clicked.connect(
                lambda checked, s=primary_store: self._on_store_clicked(s)
            )

            # Dropdown menu with all stores (priority sorted)
            menu = QMenu(self.store_btn)
            for store in sorted_stores:
                action = menu.addAction(f"{PluginManager.get_store_display_name(store)}")
                action.triggered.connect(
                    lambda checked, s=store: self._on_store_clicked(s)
                )
            self.store_btn.setMenu(menu)

    def _on_screenshot_clicked(self, index: int) -> None:
        """Handle screenshot thumbnail click"""
        if self._game:
            game_id = self._game.get("id", "")
            self.view_screenshots_requested.emit(game_id, index)

    def get_screenshots(self) -> List[str]:
        """Get current game's screenshot URLs"""
        return self.carousel.get_screenshots()

    def set_tags(self, tags: List[Dict[str, Any]]) -> None:
        """Set displayed tags for current game

        Args:
            tags: List of tag dicts with name, color keys
        """
        self.metadata_panel.set_tags(tags)

    def set_game_running(self, game_id: str, is_running: bool) -> None:
        """Update running state for a game's launch button.

        Args:
            game_id: Game UUID
            is_running: Whether the game is currently running
        """
        if is_running:
            self._running_game_id = game_id
        elif self._running_game_id == game_id:
            self._running_game_id = None

        # Update buttons if this is the currently displayed game
        if self._game and self._game.get("id") == game_id:
            if is_running:
                self._show_running_button()
            else:
                stores = self._game.get("stores", [])
                self._update_launch_buttons(stores, self._game)

    def _show_running_button(self) -> None:
        """Replace launch buttons with a disabled 'Running...' indicator."""
        while self.launch_buttons_layout.count():
            item = self.launch_buttons_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        btn = QPushButton(_("Running..."))
        btn.setObjectName("launchButtonRunning")
        btn.setEnabled(False)
        self.launch_buttons_layout.addWidget(btn)

    def _on_launch_clicked(self, store: str) -> None:
        """Handle launch button click"""
        if self._game:
            game_id = self._game.get("id", "")
            if game_id == self._running_game_id:
                return  # Prevent re-launch while running
            self.game_launched.emit(game_id, store)

    def _on_launch_via_runner(self, store: str, runner_name: str) -> None:
        """Handle launch button click with explicit runner override."""
        if self._game:
            game_id = self._game.get("id", "")
            if game_id == self._running_game_id:
                return
            self.game_launched_via_runner.emit(game_id, store, runner_name)

    def _on_install_clicked(self, store: str) -> None:
        """Handle install button click"""
        if self._game:
            game_id = self._game.get("id", "")
            self.game_install_requested.emit(game_id, store)

    def set_game(self, game: Dict[str, Any], _from_worker: bool = False) -> None:
        """Set game to display

        Args:
            game: Game data dict
            _from_worker: True when called from background metadata worker (skip re-fetch)
        """
        game_id = game.get("id")

        # Skip redundant calls for the same game (signal cascade dedup).
        # Worker callbacks always proceed — they bring updated metadata.
        if (
            not _from_worker
            and game_id
            and self._game is not None
            and self._game.get("id") == game_id
        ):
            logger.debug(f"set_game skipped (already showing): {game.get('title', 'Unknown')}")
            return

        # Fetch missing metadata in background thread (not on UI thread)
        if game_id and self._ensure_metadata_callback and not _from_worker:
            self._start_metadata_worker(game_id)

        self._game = game
        logger.debug(
            "set_game called: %s", game.get('title', 'Unknown')
        )

        # Title
        self.title_label.setText(game.get("title", _("Unknown")))

        # Hero banner background image
        self.hero_banner.set_background(game.get("background_image", ""))

        # Launch buttons (uses aliased hero_banner.launch_layout)
        stores = game.get("stores", [])
        self._update_launch_buttons(stores, game)

        # Store button (uses aliased hero_banner.store_btn)
        self._setup_store_button(stores)

        # Favorite (uses aliased hero_banner.favorite_btn)
        is_fav = game.get("is_favorite", False)
        self.favorite_btn.setChecked(is_fav)
        self.favorite_btn.setToolTip(
            _("Remove from favorites") if is_fav
            else _("Mark this game as a favorite")
        )

        # Metadata panel (replaces old action bar publisher/developer/genre/year)
        self.metadata_panel.set_game_metadata(game)

        # Game mode badges (header row)
        # Clear previous game mode badges
        while self.game_modes_layout.count():
            child = self.game_modes_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # Add game mode badges (icon glyphs with player counts and tooltips)
        game_modes = game.get("game_modes", [])
        has_modes = False
        for mode_name in game_modes:
            if mode_name not in GAME_MODE_LABELS:
                continue
            # Player count for this mode
            player_count = get_player_count(game, mode_name)

            icon_file = GAME_MODE_ICON_FILES.get(mode_name)
            if icon_file and player_count:
                # Icon + count: QFrame container with icon label + count label
                container = QFrame()
                container.setObjectName("gameModeBadge")
                h = QHBoxLayout(container)
                h.setContentsMargins(2, 1, 4, 1)
                h.setSpacing(2)
                icon = load_tinted_icon(f"game_modes/{icon_file}", size=16)
                icon_lbl = QLabel()
                icon_lbl.setPixmap(icon.pixmap(16, 16))
                icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                h.addWidget(icon_lbl)
                count_lbl = QLabel(player_count)
                count_lbl.setObjectName("gameModeBadgeCount")
                h.addWidget(count_lbl)
                badge = container
            elif icon_file:
                badge = QLabel()
                badge.setPixmap(load_tinted_icon(f"game_modes/{icon_file}", size=16).pixmap(16, 16))
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                badge.setObjectName("gameModeBadge")
            else:
                badge = QLabel(_(GAME_MODE_LABELS[mode_name]))
                badge.setObjectName("gameModeBadge")

            # Tooltip shows full mode name + player count
            filter_label = GAME_MODE_FILTERS.get(mode_name, mode_name)
            tip = _(filter_label)
            if player_count:
                tip += " " + ngettext(
                    "({count} player)", "({count} players)", int(player_count)
                ).format(count=player_count)
            badge.setToolTip(tip)
            self.game_modes_layout.addWidget(badge)
            has_modes = True
        self.game_modes_container.setVisible(has_modes)

        # Family shared badge (header row)
        is_family_shared = game.get("is_family_shared", False)
        family_license_count = game.get("family_license_count", 0)
        if family_license_count >= 2:
            license_text = _("license") if family_license_count == 1 else _("licenses")
            tooltip = _("{count} family {license_text}").format(
                count=family_license_count, license_text=license_text
            )
        else:
            tooltip = _("Family shared game")

        fs_text = (
            str(family_license_count)
            if family_license_count >= 2
            else _("FS")
        )
        self.header_fs_badge.setText(fs_text)
        self.header_fs_badge.setToolTip(tooltip)
        self.header_fs_badge.setVisible(is_family_shared or family_license_count >= 1)

        # Installed badge
        is_installed = game.get("is_installed", False)
        install_info = game.get("install_info", {})
        self.header_installed_badge.setVisible(is_installed)
        if is_installed:
            if install_info:
                tip_lines = []
                for store_name, path in install_info.items():
                    if path:
                        tip_lines.append(f"{store_name}: {path}")
                    else:
                        tip_lines.append(store_name)
                self.header_installed_badge.setToolTip("\n".join(tip_lines))
            else:
                self.header_installed_badge.setToolTip(_("Installed locally"))

        # Compatibility badges (ProtonDB, Steam Deck)
        # Colors driven by QSS property selectors: QLabel#protondbBadge[tier="gold"] etc.
        protondb_tier = game.get("protondb_rating", "")
        if protondb_tier and protondb_tier in PROTONDB_TIER_LABELS:
            self.header_protondb_badge.setText(_(PROTONDB_TIER_LABELS[protondb_tier]))
            self.header_protondb_badge.setProperty("tier", protondb_tier)
            self.header_protondb_badge.style().unpolish(self.header_protondb_badge)
            self.header_protondb_badge.style().polish(self.header_protondb_badge)
            self.header_protondb_badge.setToolTip(
                _("ProtonDB: {tier} (click to open)").format(
                    tier=protondb_tier.title()
                )
            )
            self.header_protondb_badge.setVisible(True)
        else:
            self.header_protondb_badge.setVisible(False)

        steam_deck = game.get("steam_deck_compat", "")
        if steam_deck and steam_deck in STEAM_DECK_LABELS:
            self.header_deck_badge.setText(_(STEAM_DECK_LABELS[steam_deck]))
            self.header_deck_badge.setProperty("tier", steam_deck)
            self.header_deck_badge.style().unpolish(self.header_deck_badge)
            self.header_deck_badge.style().polish(self.header_deck_badge)
            self.header_deck_badge.setToolTip(
                _("Steam Deck: {tier}").format(
                    tier=steam_deck.title()
                )
            )
            self.header_deck_badge.setVisible(True)
        else:
            self.header_deck_badge.setVisible(False)

        has_compat = (protondb_tier in PROTONDB_TIER_LABELS) if protondb_tier else False
        has_compat = has_compat or ((steam_deck in STEAM_DECK_LABELS) if steam_deck else False)

        # Show separator if any header badges are visible
        self.header_separator.setVisible(has_modes or is_family_shared or has_compat)

        # Screenshots - lazy load if empty or containing invalid paths
        screenshots = game.get("screenshots", [])
        if not self._has_valid_screenshots(screenshots) and self._screenshot_callback:
            game_id = game.get("id")
            if game_id:
                logger.debug(f"Lazy-loading screenshots for {game.get('title')}")
                screenshots = self._screenshot_callback(game_id)
                if screenshots:
                    # Update the game dict so it's cached
                    game["screenshots"] = screenshots
        self.carousel.set_screenshots(screenshots)

        # Description - lazy load via callback
        game_id = game.get("id")
        desc = ""
        if game_id and self._description_callback:
            desc = self._description_callback(game_id)
        if not desc:
            desc = game.get("short_description") or ""
        self.description_view.setHtml(self._prepare_description_html(desc))

        # Check if description is plain text and needs refresh from API
        if desc and not self._has_html_tags(desc):
            stores = game.get("stores", [])
            store_app_ids = game.get("store_app_ids", {})
            if game_id and stores and store_app_ids:
                # Signal handler in main_window tries stores in priority order
                store_name = stores[0]
                store_app_id = store_app_ids.get(store_name)
                if store_app_id:
                    logger.debug(
                        "Plain text desc for %s, requesting refresh",
                        game.get('title'),
                    )
                    self.description_refresh_requested.emit(game_id, store_app_id, store_name)

        # Tags - display in metadata panel
        tags = game.get("tags", [])
        self.metadata_panel.set_tags(tags)

        # Update Settings tab
        self._update_settings_tab(game)

        # Update Stats tab
        self._update_stats_tab(game)

        # Update Files tab
        self._update_files_tab(game)

        # Update Notes tab
        self._update_notes_tab(game)

    def _update_settings_tab(self, game: Dict[str, Any]) -> None:
        """Update Settings tab with game's launch configuration."""
        import json as _json

        # Parse launch_config JSON string from GameEntry
        config_raw = game.get("launch_config", "")
        config = {}
        if config_raw:
            try:
                config = _json.loads(config_raw)
            except (_json.JSONDecodeError, TypeError):
                pass

        self._saved_launch_config = dict(config)

        # Platform/runner selection
        runner = config.get("runner", "")
        platform_val = config.get("platform", "")
        combo_value = f"runner/{runner}" if runner else (platform_val or "default")

        # Rebuild combo filtered to runners compatible with this game's stores
        self._populate_platforms(game)
        self.platform_combo.blockSignals(True)
        idx = self.platform_combo.findData(combo_value)
        self.platform_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.platform_combo.blockSignals(False)
        self._update_default_platform_label(game)
        self._update_platform_config_visibility()
        self._update_wine_options_visibility()

        # Launch arguments
        self.args_edit.setText(config.get("launch_args", ""))
        self.working_dir_edit.setText(config.get("working_dir", ""))

        # Environment variables
        env = config.get("environment", {})
        if env:
            self.env_edit.setPlainText("\n".join(f"{k}={v}" for k, v in env.items()))
        else:
            self.env_edit.clear()

        # Wine per-game options
        rt_data = config.get("wine_runtime", "default")
        idx = self.wine_runtime_combo.findData(rt_data)
        self.wine_runtime_combo.setCurrentIndex(idx if idx >= 0 else 0)

        for key, combo in self._wine_option_combos.items():
            val = config.get(key, "default")
            ci = combo.findData(val)
            combo.setCurrentIndex(ci if ci >= 0 else 0)

        self.wine_vd_res_edit.setText(
            config.get("wine_virtual_desktop_resolution", "")
        )

        debug_val = config.get("wine_winedebug", "default")
        di = self.wine_debug_combo.findData(debug_val)
        self.wine_debug_combo.setCurrentIndex(di if di >= 0 else 0)

    def _update_stats_tab(self, game: Dict[str, Any]) -> None:
        """Update Stats tab with play statistics"""
        # Launch count
        launch_count = game.get("launch_count", 0)
        self.lbl_launch_count.setText(str(launch_count))

        # Last played
        last_played = game.get("last_launched")
        if last_played:
            from datetime import datetime
            try:
                if isinstance(last_played, str):
                    date_str = last_played.replace(" ", "T").replace("Z", "+00:00")
                    last_dt = datetime.fromisoformat(date_str)
                    if last_dt.tzinfo:
                        last_dt = last_dt.replace(tzinfo=None)
                    self.lbl_last_played.setText(last_dt.strftime("%b %d, %Y %H:%M"))
                elif isinstance(last_played, datetime):
                    self.lbl_last_played.setText(last_played.strftime("%b %d, %Y %H:%M"))
                else:
                    self.lbl_last_played.setText(str(last_played))
            except (ValueError, TypeError):
                self.lbl_last_played.setText(str(last_played))
        else:
            self.lbl_last_played.setText(_("Never"))

        # First played — updated by set_play_sessions_data if available
        self.lbl_first_played.setText(_("Not tracked"))

        # Play time from store API import
        playtime_minutes = game.get("playtime_minutes", 0)
        if playtime_minutes > 0:
            hours = playtime_minutes // 60
            mins = playtime_minutes % 60
            if hours > 0:
                self.lbl_play_time.setText(_("{hours}h {mins}m").format(hours=hours, mins=mins))
            else:
                self.lbl_play_time.setText(_("{mins}m").format(mins=mins))
        else:
            self.lbl_play_time.setText(_("Not tracked"))

        # Clear store breakdown until session data is loaded
        self.store_breakdown_group.setVisible(False)

    def set_play_sessions_data(self, sessions_data: list) -> None:
        """Set per-store playtime breakdown from play session query.

        Args:
            sessions_data: List of dicts with store_name, total_minutes,
                          session_count, first_played keys.
        """
        # Clear previous breakdown rows
        while self.store_breakdown_layout.count():
            item = self.store_breakdown_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not sessions_data:
            self.store_breakdown_group.setVisible(False)
            return

        self.store_breakdown_group.setVisible(True)
        first_played = None

        for row in sessions_data:
            store = row["store_name"].capitalize()
            minutes = row["total_minutes"] or 0
            count = row["session_count"]

            h, m = divmod(minutes, 60)
            if h:
                time_str = _("{hours}h {mins}m").format(
                    hours=h, mins=m
                )
            else:
                time_str = _("{mins}m").format(mins=m)
            session_label = ngettext(
                "{count} session", "{count} sessions", count
            ).format(count=count)
            value = QLabel(f"{time_str} ({session_label})")
            self.store_breakdown_layout.addRow(f"{store}:", value)

            fp = row.get("first_played")
            if fp:
                if first_played is None or fp < first_played:
                    first_played = fp

        if first_played:
            from datetime import datetime
            try:
                if isinstance(first_played, str):
                    first_played = datetime.fromisoformat(first_played)
                self.lbl_first_played.setText(first_played.strftime("%b %d, %Y"))
            except (ValueError, TypeError):
                pass

    def _update_files_tab(self, game: Dict[str, Any]) -> None:
        """Update Files tab with installation and archive info"""
        stores = game.get("stores", [])
        store_app_ids = game.get("store_app_ids", {})

        # Installation status from game cache (install_info in DETAIL_FIELDS)
        install_info = game.get("install_info", {})
        is_installed = game.get("is_installed", False)

        if is_installed and install_info:
            # Build display: list store names and paths
            store_lines = []
            first_path = None
            for store_name, path in install_info.items():
                if path:
                    store_lines.append(f"{store_name}: {path}")
                    if first_path is None:
                        first_path = path
                else:
                    store_lines.append(f"{store_name}: {_('Path not available')}")

            status_text = ngettext(
                "Installed ({count} store)",
                "Installed ({count} stores)",
                len(install_info),
            ).format(count=len(install_info))
            self.install_status_label.setText(status_text)
            self.install_path_label.setText("\n".join(store_lines))
            self.btn_open_folder.setEnabled(bool(first_path))
            self.btn_verify.setEnabled(False)

            # Wire Open Folder button
            try:
                self.btn_open_folder.clicked.disconnect()
            except RuntimeError:
                pass
            if first_path:
                self.btn_open_folder.clicked.connect(
                    lambda checked=False, p=first_path:
                        QDesktopServices.openUrl(
                            QUrl.fromLocalFile(p)
                        )
                )
        else:
            self.install_status_label.setText(_("Not installed"))
            self.install_path_label.setText("")
            self.btn_open_folder.setEnabled(False)
            self.btn_verify.setEnabled(False)

    def _update_notes_tab(self, game: Dict[str, Any]) -> None:
        """Update Notes tab with saved notes"""
        notes = game.get("notes", "")
        self.notes_edit.setPlainText(notes)
        self._last_saved_notes = notes

    def _prepare_description_html(self, text: str) -> str:
        """Prepare description text as HTML.

        If the text contains HTML tags, use it directly.
        Otherwise, convert plain text to basic HTML (htmlification).

        Args:
            text: Raw description text (may be HTML or plain text)

        Returns:
            HTML string ready for QTextBrowser
        """
        import re
        import html as html_module

        if not text:
            return f"<p><i>{_('No description available.')}</i></p>"

        # Get colors from palette for proper theming
        palette = self.palette()
        text_color = palette.text().color().name()
        link_color = palette.link().color().name()

        # Check if text contains HTML tags
        has_html = bool(re.search(r'<[a-zA-Z][^>]*>', text))

        if has_html:
            # Already HTML - wrap in body with styling
            return f"""
            <html>
            <head>
                <style>
                    body {{
                        font-family: sans-serif;
                        color: {text_color};
                        margin: 0;
                        padding: 0;
                    }}
                    img {{
                        max-width: 100%;
                        height: auto;
                        margin: 8px 0;
                    }}
                    a {{
                        color: {link_color};
                    }}
                    h1, h2, h3, h4 {{
                        color: {text_color};
                        margin-top: 1em;
                        margin-bottom: 0.5em;
                    }}
                    p {{
                        margin-bottom: 0.8em;
                    }}
                </style>
            </head>
            <body>{text}</body>
            </html>
            """
        else:
            # Plain text - convert to HTML
            # Escape HTML special characters
            text = html_module.escape(text)
            # Convert double newlines to paragraphs
            paragraphs = re.split(r'\n\s*\n', text)
            html_paragraphs = [
                f"<p>{p.replace(chr(10), '<br>')}</p>"
                for p in paragraphs if p.strip()
            ]
            html_content = "\n".join(html_paragraphs)

            return f"""
            <html>
            <head>
                <style>
                    body {{
                        font-family: sans-serif;
                        color: {text_color};
                        margin: 0;
                        padding: 0;
                    }}
                    p {{
                        margin-bottom: 0.8em;
                    }}
                </style>
            </head>
            <body>{html_content}</body>
            </html>
            """

    def _update_launch_buttons(
        self, stores: List[str],
        game: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update launch button for game's stores.

        Runner-aware: checks which stores have compatible runners.
        Primary button prefers installed+launchable stores over others.
        """
        # Skip rebuild if this game is currently running
        if game and game.get("id") == self._running_game_id:
            return

        # Clear existing
        while self.launch_buttons_layout.count():
            item = self.launch_buttons_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not stores:
            return

        # Check per-game runner/platform override
        override_label = None
        if game and self._platform_manager:
            lc = getattr(self, "_saved_launch_config", None)
            if not lc and game.get("launch_config"):
                import json as _json
                try:
                    lc = _json.loads(game["launch_config"])
                except (ValueError, TypeError):
                    lc = {}
            if lc:
                runner_name = lc.get("runner", "")
                if runner_name:
                    runner = self._platform_manager.get_runner(runner_name)
                    if runner:
                        override_label = runner.display_name or runner_name.capitalize()

        # Priority order for default store selection
        default = getattr(self, "_default_store", "")
        others = [
            s for s in PluginManager.get_store_plugin_names()
            if s != default
        ]
        store_priority = [default] + others

        # Sort stores by priority (unknown stores go last)
        def priority_key(s: str) -> int:
            s_lower = s.lower()
            if s_lower in store_priority:
                return store_priority.index(s_lower)
            return len(store_priority)

        sorted_stores = sorted(stores, key=priority_key)

        # Determine install status per store from install_info detail field
        install_info = game.get("install_info", {}) if game else {}
        if install_info:
            installed_stores = set(install_info.keys())
        elif game and game.get("is_installed", False):
            installed_stores = set(sorted_stores)
        else:
            installed_stores = set()

        # Query runner availability per store
        runner_info = {}
        for store in sorted_stores:
            runner_info[store] = self._runner_query(store) if self._runner_query else None
        launchable_stores = {s for s, r in runner_info.items() if r}

        # Check for bridge runners (e.g. Playnite) that aren't the primary runner
        # but should appear as extra dropdown options
        bridge_runners = []
        if self._platform_manager:
            available = self._platform_manager.get_available_runners()
            for rn, info in available.items():
                if getattr(info, "install_type", "") != "bridge":
                    continue
                runner = self._platform_manager.get_runner(rn)
                if not runner:
                    continue
                # Only add if this runner supports at least one of the game's stores
                if any(s in runner.supported_stores for s in sorted_stores):
                    bridge_runners.append((rn, runner.display_name or rn.capitalize()))

        # Pick primary: prefer installed+launchable, then launchable, then default
        primary_store = self._pick_primary_store(
            default, sorted_stores, installed_stores, launchable_stores
        )

        # Build tooltip with play statistics
        tooltip = self._build_play_tooltip(game) if game else ""

        primary_installed = primary_store in installed_stores
        primary_launchable = primary_store in launchable_stores

        # Force dropdown when bridge runners exist even for single-store games
        use_dropdown = len(stores) > 1 or bool(bridge_runners)

        if not use_dropdown:
            # Single store - simple button
            display_name = PluginManager.get_store_display_name(primary_store)
            if override_label:
                if primary_installed:
                    btn = QPushButton(
                        _("Play (via {runner})").format(runner=override_label)
                    )
                    btn.setObjectName("launchButtonPrimary")
                    btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
                else:
                    btn = QPushButton(
                        _("Launch ({runner})").format(runner=override_label)
                    )
                    btn.setObjectName("launchButtonInstall")
                    btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
            elif primary_installed:
                btn = QPushButton(_("Play ({name})").format(name=display_name))
                btn.setObjectName("launchButtonPrimary")
                btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
            else:
                btn = QPushButton(_("Install ({name})").format(name=display_name))
                btn.setObjectName("launchButtonInstall")
                btn.clicked.connect(lambda checked, s=primary_store: self._on_install_clicked(s))
            if not override_label and not primary_launchable and not primary_installed:
                btn.setEnabled(False)
                btn.setToolTip(_("No compatible launcher available for this store"))
            else:
                fallback = (
                    _("Launch this game via {name}").format(
                        name=override_label or display_name
                    )
                    if primary_installed or override_label
                    else _("Open the {name} install page").format(name=display_name)
                )
                btn.setToolTip(tooltip if tooltip else fallback)
            self.launch_buttons_layout.addWidget(btn)
        else:
            # Multiple stores - button with dropdown
            display_name = PluginManager.get_store_display_name(primary_store)
            btn = QToolButton()
            if override_label:
                if primary_installed:
                    btn.setText(
                        _("Play (via {runner})").format(runner=override_label)
                    )
                    btn.setObjectName("launchButtonPrimary")
                    btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
                else:
                    btn.setText(
                        _("Launch ({runner})").format(runner=override_label)
                    )
                    btn.setObjectName("launchButtonInstall")
                    btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
            elif primary_installed:
                btn.setText(_("Play ({name})").format(name=display_name))
                btn.setObjectName("launchButtonPrimary")
                btn.clicked.connect(lambda checked, s=primary_store: self._on_launch_clicked(s))
            else:
                btn.setText(_("Install ({name})").format(name=display_name))
                btn.setObjectName("launchButtonInstall")
                btn.clicked.connect(lambda checked, s=primary_store: self._on_install_clicked(s))
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            fallback = (
                _("Launch this game via {name}").format(
                    name=override_label or display_name
                )
                if primary_installed or override_label
                else _("Open the {name} install page").format(name=display_name)
            )
            btn.setToolTip(tooltip if tooltip else fallback)

            # Dropdown menu with runner awareness
            menu = QMenu(btn)
            no_runner_tip = _("No compatible launcher available for this store")
            for store in sorted_stores:
                store_display = PluginManager.get_store_display_name(store)
                runner_name = runner_info.get(store)
                has_runner = store in launchable_stores
                is_installed = store in installed_stores

                if is_installed and has_runner:
                    label = _("Play ({name}) via {runner}").format(
                        name=store_display, runner=runner_name
                    )
                    action = menu.addAction(label)
                    action.triggered.connect(
                        lambda checked, s=store: self._on_launch_clicked(s)
                    )
                elif is_installed and not has_runner:
                    action = menu.addAction(
                        _("Play ({name})").format(name=store_display)
                    )
                    action.setEnabled(False)
                    action.setToolTip(no_runner_tip)
                elif not is_installed and has_runner:
                    action = menu.addAction(
                        _("Install ({name})").format(name=store_display)
                    )
                    action.triggered.connect(
                        lambda checked, s=store: self._on_install_clicked(s)
                    )
                else:
                    action = menu.addAction(
                        _("Install ({name})").format(name=store_display)
                    )
                    action.setEnabled(False)
                    action.setToolTip(no_runner_tip)

            # Add bridge runners (e.g. Playnite) as extra options
            if bridge_runners:
                menu.addSeparator()
                for br_name, br_display in bridge_runners:
                    # Use primary store for the launch
                    label = _("Play via {runner}").format(runner=br_display)
                    action = menu.addAction(label)
                    action.triggered.connect(
                        lambda checked, s=primary_store, r=br_name:
                            self._on_launch_via_runner(s, r)
                    )

            btn.setMenu(menu)

            self.launch_buttons_layout.addWidget(btn)

    @staticmethod
    def _pick_primary_store(
        default_store: str,
        sorted_stores: List[str],
        installed_stores: set,
        launchable_stores: set,
    ) -> str:
        """Pick the best primary store for the launch button.

        Priority:
        1. Default store if installed AND launchable
        2. First store that is installed AND launchable
        3. Default store if launchable (show Install)
        4. First launchable store (show Install)
        5. Default store (fallback, may be disabled)
        6. First store in sorted order
        """
        # 1. Default installed + launchable
        if default_store in installed_stores and default_store in launchable_stores:
            return default_store
        # 2. Any installed + launchable
        for s in sorted_stores:
            if s in installed_stores and s in launchable_stores:
                return s
        # 3. Default launchable
        if default_store in launchable_stores:
            return default_store
        # 4. Any launchable
        for s in sorted_stores:
            if s in launchable_stores:
                return s
        # 5. Default if present
        if default_store in sorted_stores:
            return default_store
        # 6. First
        return sorted_stores[0]

    def _build_play_tooltip(self, game: Dict[str, Any]) -> str:
        """Build tooltip text with play statistics for the play button."""
        lines = []

        # Playtime
        playtime_minutes = game.get("playtime_minutes", 0)
        if playtime_minutes > 0:
            hours = playtime_minutes // 60
            mins = playtime_minutes % 60
            if hours > 0:
                lines.append(_("Playtime: {hours}h {mins}m").format(hours=hours, mins=mins))
            else:
                lines.append(_("Playtime: {mins}m").format(mins=mins))

        # Launch count
        launch_count = game.get("launch_count", 0)
        if launch_count > 0:
            lines.append(_("Launched: {count} time(s)").format(count=launch_count))

        # Last played
        last_launched = game.get("last_launched")
        if last_launched:
            from datetime import datetime
            try:
                if isinstance(last_launched, str):
                    date_str = last_launched.replace(" ", "T").replace("Z", "+00:00")
                    last_dt = datetime.fromisoformat(date_str)
                    if last_dt.tzinfo:
                        last_dt = last_dt.replace(tzinfo=None)
                else:
                    last_dt = last_launched
                lines.append(_("Last played: {date}").format(date=last_dt.strftime('%b %d, %Y')))
            except (ValueError, TypeError):
                pass

        return "\n".join(lines)

    def clear(self) -> None:
        """Clear displayed game"""
        self._game = None

        # Clear About tab
        self.title_label.setText("")
        self.hero_banner.clear()
        self.description_view.setHtml("")
        self.carousel.clear()
        self.metadata_panel.clear()
        # Clear header badges
        while self.game_modes_layout.count():
            child = self.game_modes_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.game_modes_container.setVisible(False)
        self.header_protondb_badge.setVisible(False)
        self.header_deck_badge.setVisible(False)
        self.header_fs_badge.setVisible(False)
        self.header_installed_badge.setVisible(False)
        self.header_separator.setVisible(False)

        # Clear Settings tab
        self.platform_combo.setCurrentIndex(0)
        self.args_edit.clear()
        self.working_dir_edit.clear()
        self.env_edit.clear()

        # Clear Stats tab
        self.lbl_launch_count.setText("0")
        self.lbl_last_played.setText(_("Never"))
        self.lbl_first_played.setText(_("Not tracked"))
        self.lbl_play_time.setText(_("Not tracked"))

        # Clear Files tab
        self.install_status_label.setText(_("Not installed"))
        self.install_path_label.setText("")
        self.btn_open_folder.setEnabled(False)
        self.btn_verify.setEnabled(False)

        # Clear Notes tab
        self.notes_edit.clear()
        self._last_saved_notes = ""

        # Reset to About tab
        self.btn_about.setChecked(True)
        self.tab_stack.setCurrentIndex(self.TAB_ABOUT)
