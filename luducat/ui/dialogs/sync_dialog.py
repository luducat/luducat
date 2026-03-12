# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sync_dialog.py

"""Sync Dialog - Progress dialog for store and metadata synchronization.

Layout:
+---------------------------------------------------+
|                                                   |
| Stores:                              [PICTURE]    |
|   ✓ 1234 / 1234 Steam                             |
|   ● 56 / 200 GOG                     [game img]   |
|   ○ Epic                                          |
|                                                   |
| Metadata:                                         |
|   ● PCGamingWiki                                  |
|   ● SteamGridDB                                   |
|   - IGDB (skipped)                                |
|                                                   |
| Activity: GOG - Fetching "The Witcher 3"          |
| [████████████████████░░░░░░░░░░░░░░░░] 56 / 200   |
|                                                   |
|                  [Cancel]                         |
+---------------------------------------------------+

Visual States:
- ✓ Finished: normal text, checkmark
- ● Current/Running: bold text
- ○ Pending: dimmed text
- - Skipped: dimmed with "(skipped)"
"""

from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QIcon, QImage, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.plugin_manager import PluginManager
from ...utils.random_image_picker import RandomImagePicker


class SyncItemState(Enum):
    """State of a sync item (store or metadata plugin)."""
    PENDING = "pending"      # Not yet started (dimmed)
    RUNNING = "running"      # Currently processing (bold)
    FINISHED = "finished"    # Completed (checkmark)
    SKIPPED = "skipped"      # Skipped by user setting


class SyncItemWidget(QWidget):
    """Widget representing a store or metadata plugin in the sync list.

    Shows state indicator, name, and optional progress (for stores).
    """

    def __init__(self, name: str, display_name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._name = name
        self._display_name = display_name  # Store original display name
        self._state = SyncItemState.PENDING
        self._current = 0
        self._total = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        # State indicator (checkmark, bullet, etc.)
        self._indicator = QLabel()
        self._indicator.setFixedWidth(20)
        self._indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._indicator)

        # Name and progress label
        self._label = QLabel(display_name)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._label)

        self._update_display()

    @property
    def name(self) -> str:
        return self._name

    def set_state(self, state: SyncItemState) -> None:
        """Set the item state."""
        self._state = state
        self._update_display()

    def set_progress(self, current: int, total: int) -> None:
        """Set progress (for stores only)."""
        self._current = current
        self._total = total
        self._update_display()

    def _update_display(self) -> None:
        """Update visual appearance based on state."""
        if self._state == SyncItemState.PENDING:
            self._indicator.setText("○")
            self._label.setText(self._display_name)
            self._set_dimmed(True)
            self._set_bold(False)

        elif self._state == SyncItemState.RUNNING:
            self._indicator.setText("●")
            if self._total > 0:
                self._label.setText(f"{self._display_name} ({self._current}/{self._total})")
            else:
                self._label.setText(self._display_name)
            self._set_dimmed(False)
            self._set_bold(True)

        elif self._state == SyncItemState.FINISHED:
            self._indicator.setText("✓")
            if self._total > 0:
                self._label.setText(f"{self._display_name} ({self._total})")
            else:
                self._label.setText(self._display_name)
            self._set_dimmed(False)
            self._set_bold(False)
            # Checkmark same color as text
            self._indicator.setStyleSheet("")

        elif self._state == SyncItemState.SKIPPED:
            self._indicator.setText("-")
            self._label.setText(f"{self._display_name} (skipped)")
            self._set_dimmed(True)
            self._set_bold(False)

    def _set_dimmed(self, dimmed: bool) -> None:
        """Set dimmed appearance."""
        opacity = 0.5 if dimmed else 1.0
        self._indicator.setStyleSheet(f"opacity: {opacity};" if dimmed else "")
        self._label.setStyleSheet("color: palette(mid);" if dimmed else "")

    def _set_bold(self, bold: bool) -> None:
        """Set bold text."""
        font = self._label.font()
        font.setBold(bold)
        self._label.setFont(font)


class DistractionPicture(QLabel):
    """Image display widget with cross-fade animation.

    Shows game covers/screenshots during sync to keep user engaged.
    """

    CROSSFADE_DURATION_MS = 400
    PICTURE_SIZE = QSize(300, 225)    # Landscape (screenshots, heroes)
    COVER_SIZE = QSize(160, 240)      # Portrait (covers) — 2:3 aspect ratio

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._current_pixmap: Optional[QPixmap] = None
        self._opacity_effect: Optional[QGraphicsOpacityEffect] = None
        self._animation: Optional[QPropertyAnimation] = None
        self._pending_pixmap: Optional[QPixmap] = None

        self.setFixedSize(self.PICTURE_SIZE)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("distractionPicture")
        self.setStyleSheet("""
            #distractionPicture {
                border-radius: 8px;
                background: transparent;
            }
        """)

        # Set up opacity effect for cross-fade
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

    FADE_SIZE = 6  # Edge fade width in pixels

    def set_pixmap_with_crossfade(self, pixmap: QPixmap) -> None:
        """Set new pixmap with cross-fade animation."""
        if pixmap.isNull():
            return

        is_portrait = pixmap.height() > pixmap.width()

        if is_portrait:
            # Portrait (covers): fill dedicated cover frame, 2:3 ratio
            target = self.COVER_SIZE
        else:
            # Landscape (screenshots, heroes): fill landscape frame
            target = self.PICTURE_SIZE

        self.setFixedSize(target)

        scaled = pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.width() > target.width() or scaled.height() > target.height():
            x = (scaled.width() - target.width()) // 2
            y = (scaled.height() - target.height()) // 2
            scaled = scaled.copy(x, y, target.width(), target.height())

        # Bake edge fades into the pixmap (transparent edges)
        scaled = self._apply_edge_fade(scaled)

        if self._current_pixmap is None:
            # First image - no animation needed
            self._current_pixmap = scaled
            self.setPixmap(scaled)
            return

        # Store pending pixmap and start fade-out
        self._pending_pixmap = scaled
        self._start_crossfade()

    def _apply_edge_fade(self, pixmap: QPixmap) -> QPixmap:
        """Apply alpha fade to all 4 edges of the pixmap.

        Uses QImage with CompositionMode_DestinationIn which multiplies
        the destination alpha by the source alpha (gradient).
        """
        w, h = pixmap.width(), pixmap.height()
        fade = self.FADE_SIZE

        # Must use QImage (not QPixmap) for reliable per-pixel alpha
        img = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)

        transparent = QColor(0, 0, 0, 0)
        opaque = QColor(0, 0, 0, 255)

        painter = QPainter(img)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)

        # Top fade
        grad = QLinearGradient(0, 0, 0, fade)
        grad.setColorAt(0, transparent)
        grad.setColorAt(1, opaque)
        painter.fillRect(0, 0, w, fade, grad)

        # Bottom fade
        grad = QLinearGradient(0, h - fade, 0, h)
        grad.setColorAt(0, opaque)
        grad.setColorAt(1, transparent)
        painter.fillRect(0, h - fade, w, fade, grad)

        # Left fade
        grad = QLinearGradient(0, 0, fade, 0)
        grad.setColorAt(0, transparent)
        grad.setColorAt(1, opaque)
        painter.fillRect(0, 0, fade, h, grad)

        # Right fade
        grad = QLinearGradient(w - fade, 0, w, 0)
        grad.setColorAt(0, opaque)
        grad.setColorAt(1, transparent)
        painter.fillRect(w - fade, 0, fade, h, grad)

        painter.end()
        return QPixmap.fromImage(img)

    def _start_crossfade(self) -> None:
        """Start cross-fade animation."""
        if self._animation is not None:
            self._animation.stop()

        self._animation = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._animation.setDuration(self.CROSSFADE_DURATION_MS // 2)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._animation.finished.connect(self._on_fadeout_complete)
        self._animation.start()

    def _on_fadeout_complete(self) -> None:
        """Handle fade-out completion - swap image and fade back in."""
        if self._pending_pixmap is not None:
            self._current_pixmap = self._pending_pixmap
            self.setPixmap(self._pending_pixmap)
            self._pending_pixmap = None

        # Fade back in
        self._animation = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._animation.setDuration(self.CROSSFADE_DURATION_MS // 2)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._animation.start()

    def set_placeholder(self, icon: QIcon) -> None:
        """Set placeholder icon when no image is available."""
        pixmap = icon.pixmap(self.PICTURE_SIZE.width() // 2, self.PICTURE_SIZE.height() // 2)
        self.setPixmap(pixmap)
        self._current_pixmap = pixmap

class SyncDialog(QDialog):
    """Sync progress dialog with stores, metadata, distraction picture.

    Emits:
        cancelled: When user requests cancellation
    """

    cancelled = Signal()

    # Image rotation interval
    IMAGE_ROTATION_MS = 20_000  # 50 seconds

    def __init__(
        self,
        stores: List[str],
        metadata_plugins: List[str],
        skipped_plugins: Optional[List[str]] = None,
        parent: Optional[QWidget] = None,
    ):
        """Initialize sync dialog.

        Args:
            stores: List of store names to sync (e.g., ["steam", "gog"])
            metadata_plugins: List of metadata plugins (e.g., ["igdb", "pcgamingwiki"])
            skipped_plugins: List of plugins marked as skipped
            parent: Parent widget
        """
        super().__init__(parent)
        self._stores = stores
        self._metadata_plugins = metadata_plugins
        self._skipped_plugins = set(skipped_plugins or [])
        self._is_cancelled = False
        self._is_closing = False

        # Item widgets by name
        self._store_items: Dict[str, SyncItemWidget] = {}
        self._metadata_items: Dict[str, SyncItemWidget] = {}

        # Image picker and rotation
        self._image_picker = RandomImagePicker()
        self._rotation_timer: Optional[QTimer] = None

        self._setup_ui()
        self._setup_image_rotation()

    def _setup_ui(self) -> None:
        """Set up dialog UI."""
        self.setWindowTitle(_("Synchronizing"))
        self.setModal(True)
        self.setMinimumWidth(580)
        self.setMinimumHeight(440)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(16, 16, 16, 16)  # Equal margins on all sides

        # Top section: stores + picture side by side
        top_section = QHBoxLayout()
        top_section.setSpacing(12)

        # Left side: stores and metadata lists
        lists_layout = QVBoxLayout()
        lists_layout.setSpacing(8)

        # Stores section
        stores_label = QLabel(_("Stores:"))
        stores_label.setObjectName("syncSectionLabel")
        lists_layout.addWidget(stores_label)

        for store in self._stores:
            display_name = PluginManager.get_store_display_name(store)
            item = SyncItemWidget(store, display_name)
            self._store_items[store] = item
            lists_layout.addWidget(item)

        lists_layout.addSpacing(8)

        # Metadata section
        metadata_label = QLabel(_("Metadata:"))
        metadata_label.setObjectName("syncSectionLabel")
        lists_layout.addWidget(metadata_label)

        for plugin in self._metadata_plugins:
            display_name = self._get_plugin_display_name(plugin)
            item = SyncItemWidget(plugin, display_name)
            if plugin in self._skipped_plugins:
                item.set_state(SyncItemState.SKIPPED)
            self._metadata_items[plugin] = item
            lists_layout.addWidget(item)

        lists_layout.addStretch()
        top_section.addLayout(lists_layout, stretch=1)

        # Right side: distraction picture + game title (top-right aligned)
        picture_layout = QVBoxLayout()
        picture_layout.setContentsMargins(0, 0, 0, 0)
        picture_layout.setSpacing(4)
        self._picture = DistractionPicture()
        picture_layout.addWidget(self._picture, alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        picture_layout.addStretch()
        top_section.addLayout(picture_layout)

        main_layout.addLayout(top_section)

        # Activity label
        self._activity_label = QLabel(_("Activity: Initializing..."))
        self._activity_label.setObjectName("hintLabel")
        main_layout.addWidget(self._activity_label)

        # Progress bar (count info is in activity text, not duplicated here)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)  # Hide percentage text
        main_layout.addWidget(self._progress_bar)

        main_layout.addSpacing(8)

        # Cancel button (centered)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self._cancel_button = QPushButton(_("Cancel"))
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._cancel_button.setMinimumWidth(100)
        button_layout.addWidget(self._cancel_button)
        button_layout.addStretch()
        main_layout.addLayout(button_layout)

        # Rate limit warning (bottom-left, hidden by default)
        self._rate_limit_label = QLabel()
        self._rate_limit_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        from luducat.utils.style_helpers import set_status_property
        set_status_property(self._rate_limit_label, "warning", bold=True)
        self._rate_limit_label.hide()
        main_layout.addWidget(self._rate_limit_label)

    def _setup_image_rotation(self) -> None:
        """Set up image rotation timer."""
        # Index cache images
        self._image_picker.refresh_cache_index()

        # Set initial placeholder
        app = QApplication.instance()
        if app:
            self._picture.set_placeholder(app.windowIcon())

        # Show first image if available
        self._rotate_image()

        # Start rotation timer
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._rotate_image)
        self._rotation_timer.start(self.IMAGE_ROTATION_MS)

    def _rotate_image(self) -> None:
        """Rotate to next image."""
        image_path = self._image_picker.get_next_image()
        if image_path and image_path.exists():
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                self._picture.set_pixmap_with_crossfade(pixmap)

    def _get_plugin_display_name(self, plugin: str) -> str:
        """Get display name for a plugin."""
        return PluginManager.get_store_display_name(plugin)

    # --- Store Methods ---

    def set_store_started(self, store: str) -> None:
        """Mark a store as currently syncing."""
        if store in self._store_items:
            self._store_items[store].set_state(SyncItemState.RUNNING)

    def set_store_progress(self, store: str, current: int, total: int) -> None:
        """Update store progress."""
        if store in self._store_items:
            self._store_items[store].set_progress(current, total)

    def set_store_finished(self, store: str, stats: Optional[dict] = None) -> None:
        """Mark a store as finished."""
        if store in self._store_items:
            self._store_items[store].set_state(SyncItemState.FINISHED)

    # --- Metadata Methods ---

    def set_metadata_started(self, plugin: str) -> None:
        """Mark a metadata plugin as currently running."""
        if plugin in self._metadata_items:
            self._metadata_items[plugin].set_state(SyncItemState.RUNNING)

    def set_metadata_progress(self, plugin: str, current: int, total: int) -> None:
        """Update metadata plugin progress."""
        if plugin in self._metadata_items:
            self._metadata_items[plugin].set_progress(current, total)

    def set_metadata_finished(self, plugin: str) -> None:
        """Mark a metadata plugin as finished."""
        if plugin in self._metadata_items:
            self._metadata_items[plugin].set_state(SyncItemState.FINISHED)

    def set_metadata_skipped(self, plugin: str) -> None:
        """Mark a metadata plugin as skipped."""
        if plugin in self._metadata_items:
            self._metadata_items[plugin].set_state(SyncItemState.SKIPPED)

    # --- Activity/Progress Methods ---

    def set_activity(self, plugin: str, message: str) -> None:
        """Set the activity line text."""
        display_name = self._get_plugin_display_name(plugin)
        self._activity_label.setText(_("Activity: {name} - {msg}").format(name=display_name, msg=message))

    def set_progress(self, current: int, total: int) -> None:
        """Update the main progress bar."""
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)
        else:
            self._progress_bar.setRange(0, 0)  # Indeterminate

    def set_rate_limit_status(self, message: str) -> None:
        """Show or hide rate limit warning."""
        if message:
            self._rate_limit_label.setText(message)
            self._rate_limit_label.show()
        else:
            self._rate_limit_label.hide()

    # --- Game Context for Image ---

    def set_current_game_context(
        self,
        cover_url: Optional[str] = None,
        screenshot_urls: Optional[List[str]] = None,
        game_name: str = "",
    ) -> None:
        """Set current game for image display priority."""
        self._image_picker.set_current_game(cover_url, screenshot_urls)

        # Try to show current game image immediately
        image_path = self._image_picker.get_current_game_image()
        if image_path and image_path.exists():
            pixmap = QPixmap(str(image_path))
            if not pixmap.isNull():
                self._picture.set_pixmap_with_crossfade(pixmap)

    # --- Cancel Handling ---

    def _on_cancel_clicked(self) -> None:
        """Handle cancel button click."""
        if self._is_closing:
            # Already closing - force close
            self.reject()
            return

        if self._is_cancelled:
            # Already cancelled - this click closes
            self.accept()
            return

        # First click - request cancellation
        self._is_cancelled = True
        self._cancel_button.setText(_("Cancelling..."))
        self._cancel_button.setEnabled(False)
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        self.cancelled.emit()

    def on_sync_complete(self) -> None:
        """Called when sync is complete (success or cancelled)."""
        self._is_closing = True

        # Stop image rotation
        if self._rotation_timer:
            self._rotation_timer.stop()

        # Restore cursor
        QApplication.restoreOverrideCursor()

        # Update button
        self._cancel_button.setText(_("Close"))
        self._cancel_button.setEnabled(True)

    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return self._is_cancelled

    def closeEvent(self, event) -> None:
        """Handle dialog close."""
        if self._rotation_timer:
            self._rotation_timer.stop()

        # Restore cursor if we had set it
        if self._is_cancelled and not self._is_closing:
            QApplication.restoreOverrideCursor()

        super().closeEvent(event)
