# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# image_viewer.py

"""Fullscreen image viewer dialog for luducat

Modal dialog for viewing screenshots and cover images in fullscreen.
Supports navigation between images with keyboard and mouse controls.

Loads full-resolution images directly from disk cache (bypassing the
memory cache's downscaled copies) for maximum quality when zooming.
"""

import logging
from typing import List, Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
    QSizePolicy,
)

from ...utils.image_cache import get_screenshot_cache

logger = logging.getLogger(__name__)


class ImageViewerDialog(QDialog):
    """Fullscreen image viewer dialog

    Displays images with navigation controls and keyboard shortcuts.

    Keyboard shortcuts:
        Left/Right: Navigate between images
        +/-: Zoom in/out
        0: Reset zoom to fit
        F: Toggle fit mode (fit window / actual size)
        Escape: Close viewer
    """

    image_changed = Signal(int)  # Emitted when current image changes

    def __init__(
        self,
        images: List[str],
        start_index: int = 0,
        title: str = None,
        parent: Optional[QWidget] = None,
    ):
        """Initialize image viewer

        Args:
            images: List of image URLs or file paths
            start_index: Index of image to show first
            title: Window title
            parent: Parent widget
        """
        super().__init__(parent)

        self._images = images
        self._current_index = max(0, min(start_index, len(images) - 1))
        self._zoom_level = 1.0
        self._fit_mode = True  # True = fit to window, False = actual size
        self._image_cache = get_screenshot_cache()

        self.setWindowTitle(title or _("Image Viewer"))
        self.setModal(True)

        # ObjectName for styling via theme system
        self.setObjectName("imageViewerDialog")

        logger.info(f"ImageViewerDialog.__init__: {len(images)} images, start_index={start_index}")

        self._setup_ui()
        self._setup_shortcuts()
        self._connect_signals()

        # Start maximized for better viewing (after UI setup to avoid resizeEvent errors)
        self.setWindowState(Qt.WindowState.WindowMaximized)

        # Load initial image
        if self._images:
            logger.info(f"ImageViewerDialog: Loading initial image at index {self._current_index}")
            self._load_current_image()
        else:
            logger.warning("ImageViewerDialog: No images provided")

    def _setup_ui(self) -> None:
        """Create viewer layout"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Image display area
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.image_label.setMinimumSize(400, 300)
        layout.addWidget(self.image_label, 1)

        # Control bar at bottom
        control_bar = QWidget()
        control_bar.setObjectName("imageViewerControlBar")
        control_layout = QHBoxLayout(control_bar)
        control_layout.setContentsMargins(8, 4, 8, 4)
        control_layout.setSpacing(8)

        # Zoom controls (left side)
        self.btn_zoom_out = QPushButton("-")
        self.btn_zoom_out.setToolTip(_("Zoom out (-)"))
        self.btn_zoom_out.clicked.connect(self._zoom_out)
        control_layout.addWidget(self.btn_zoom_out)

        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("imageViewerZoomLabel")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_label.setMinimumWidth(50)
        control_layout.addWidget(self.zoom_label)

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setToolTip(_("Zoom in (+)"))
        self.btn_zoom_in.clicked.connect(self._zoom_in)
        control_layout.addWidget(self.btn_zoom_in)

        self.btn_fit = QPushButton(_("Fit"))
        self.btn_fit.setToolTip(_("Toggle fit mode (F)"))
        self.btn_fit.setCheckable(True)
        self.btn_fit.setChecked(True)
        self.btn_fit.clicked.connect(self._toggle_fit_mode)
        control_layout.addWidget(self.btn_fit)

        control_layout.addStretch()

        # Navigation buttons (center)
        self.btn_prev = QPushButton("<")
        self.btn_prev.setToolTip(_("Previous image (Left arrow)"))
        self.btn_prev.clicked.connect(self._go_prev)
        control_layout.addWidget(self.btn_prev)

        self.counter_label = QLabel()
        self.counter_label.setObjectName("imageViewerCounterLabel")
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter_label.setMinimumWidth(60)
        control_layout.addWidget(self.counter_label)

        self.btn_next = QPushButton(">")
        self.btn_next.setToolTip(_("Next image (Right arrow)"))
        self.btn_next.clicked.connect(self._go_next)
        control_layout.addWidget(self.btn_next)

        control_layout.addStretch()

        # Close button (right side)
        self.btn_close = QPushButton(_("Close"))
        self.btn_close.setToolTip(_("Close viewer (Escape)"))
        self.btn_close.clicked.connect(self.close)
        control_layout.addWidget(self.btn_close)

        layout.addWidget(control_bar)

        # Update button states
        self._update_controls()

    def _setup_shortcuts(self) -> None:
        """Setup keyboard shortcuts"""
        # Navigation
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, self._go_prev)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, self._go_next)
        QShortcut(QKeySequence(Qt.Key.Key_Home), self, self._go_first)
        QShortcut(QKeySequence(Qt.Key.Key_End), self, self._go_last)

        # Zoom
        QShortcut(QKeySequence(Qt.Key.Key_Plus), self, self._zoom_in)
        QShortcut(QKeySequence(Qt.Key.Key_Equal), self, self._zoom_in)  # + without shift
        QShortcut(QKeySequence(Qt.Key.Key_Minus), self, self._zoom_out)
        QShortcut(QKeySequence(Qt.Key.Key_0), self, self._reset_zoom)
        QShortcut(QKeySequence("F"), self, self._toggle_fit_mode)

        # Close
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)
        QShortcut(QKeySequence("Q"), self, self.close)

    def _connect_signals(self) -> None:
        """Connect signals"""
        self._image_cache.image_loaded.connect(self._on_image_loaded)
        self._image_cache.image_not_found.connect(self._on_image_not_found)

    def closeEvent(self, event) -> None:
        """Clean up signal connections and free full-res pixmap on close"""
        logger.info("ImageViewerDialog: closeEvent called")
        for sig in (self._image_cache.image_loaded, self._image_cache.image_not_found):
            try:
                sig.disconnect(self._on_image_loaded if sig is self._image_cache.image_loaded
                               else self._on_image_not_found)
            except (RuntimeError, TypeError):
                pass

        # Free full-res pixmap immediately
        if hasattr(self, '_current_pixmap') and self._current_pixmap:
            self._current_pixmap.swap(QPixmap())
            self._current_pixmap = None
        self.image_label.setPixmap(QPixmap())

        super().closeEvent(event)

    def _load_full_res(self, url: str) -> Optional[QPixmap]:
        """Load full-resolution pixmap from disk cache, bypassing memory cache.

        Falls back to the memory cache (downscaled) if disk path unavailable.
        """
        disk_path = self._image_cache.get_disk_path(url)
        if disk_path:
            pixmap = QPixmap(str(disk_path))
            if not pixmap.isNull():
                return pixmap
        # Fallback: use the memory-cached (possibly downscaled) version
        return self._image_cache.get_image(url)

    def _load_current_image(self) -> None:
        """Load and display the current image at full resolution"""
        if not self._images or self._current_index >= len(self._images):
            logger.warning(
                "ImageViewerDialog._load_current_image: "
                f"No images or invalid index ({self._current_index})"
            )
            self.image_label.setText(_("No image available"))
            return

        url = self._images[self._current_index]
        logger.info(f"ImageViewerDialog._load_current_image: Loading {url[:80]}...")

        # Load full-res from disk (bypasses memory cache downscaling)
        pixmap = self._load_full_res(url)
        if pixmap and not pixmap.isNull():
            logger.info(f"ImageViewerDialog: Got pixmap: {pixmap.width()}x{pixmap.height()}")
            self._current_pixmap = pixmap
            self._display_pixmap(pixmap)
        else:
            logger.info("ImageViewerDialog: Pixmap not available, showing Loading...")
            self._current_pixmap = None
            self.image_label.setText(_("Loading..."))

        self._update_controls()

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle async image load — reload full-res from disk"""
        if not self._images:
            return

        current_url = self._images[self._current_index]
        if url == current_url:
            # The cache signal delivers a downscaled pixmap — load full-res instead
            full_res = self._load_full_res(url)
            if full_res and not full_res.isNull():
                pixmap = full_res
            logger.info(
                "ImageViewerDialog._on_image_loaded: "
                f"Displaying {pixmap.width()}x{pixmap.height()}"
            )
            self._current_pixmap = pixmap
            self._display_pixmap(pixmap)

    def _on_image_not_found(self, url: str, error: str) -> None:
        """Handle HTTP 404 — show unavailable message for the current image"""
        if not self._images:
            return
        current_url = self._images[self._current_index]
        if url == current_url:
            logger.info(f"ImageViewerDialog: Image not found (404): {url[:80]}")
            self._current_pixmap = None
            self.image_label.setText(_("Image unavailable (404)"))

    def _display_pixmap(self, pixmap: QPixmap) -> None:
        """Display pixmap with current zoom/fit settings"""
        if pixmap.isNull():
            logger.warning("ImageViewerDialog._display_pixmap: Pixmap is null")
            self.image_label.setText(_("Failed to load image"))
            return

        logger.info(
            "ImageViewerDialog._display_pixmap: "
            f"Displaying {pixmap.width()}x{pixmap.height()}, "
            f"fit_mode={self._fit_mode}"
        )

        if self._fit_mode:
            # Fit to window
            available_size = self.image_label.size()
            scaled = pixmap.scaled(
                available_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)

            # Calculate effective zoom for display
            scale_x = scaled.width() / pixmap.width()
            scale_y = scaled.height() / pixmap.height()
            self._zoom_level = min(scale_x, scale_y)
        else:
            # Apply manual zoom
            new_size = QSize(
                int(pixmap.width() * self._zoom_level),
                int(pixmap.height() * self._zoom_level),
            )
            scaled = pixmap.scaled(
                new_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)

        self._update_zoom_label()

    def _update_controls(self) -> None:
        """Update button states and counter"""
        total = len(self._images)
        current = self._current_index + 1

        counter_text = (
            _("{current} / {total}").format(
                current=current, total=total
            )
            if total > 0
            else _("0 / 0")
        )
        self.counter_label.setText(counter_text)
        self.btn_prev.setEnabled(self._current_index > 0)
        self.btn_next.setEnabled(self._current_index < total - 1)

    def _update_zoom_label(self) -> None:
        """Update zoom percentage display"""
        self.zoom_label.setText(f"{int(self._zoom_level * 100)}%")

    def _go_prev(self) -> None:
        """Go to previous image"""
        if self._current_index > 0:
            self._current_index -= 1
            self._load_current_image()
            self.image_changed.emit(self._current_index)

    def _go_next(self) -> None:
        """Go to next image"""
        if self._current_index < len(self._images) - 1:
            self._current_index += 1
            self._load_current_image()
            self.image_changed.emit(self._current_index)

    def _go_first(self) -> None:
        """Go to first image"""
        if self._current_index != 0:
            self._current_index = 0
            self._load_current_image()
            self.image_changed.emit(self._current_index)

    def _go_last(self) -> None:
        """Go to last image"""
        last = len(self._images) - 1
        if self._current_index != last:
            self._current_index = last
            self._load_current_image()
            self.image_changed.emit(self._current_index)

    def _zoom_in(self) -> None:
        """Zoom in by 25%"""
        self._fit_mode = False
        self.btn_fit.setChecked(False)
        self._zoom_level = min(5.0, self._zoom_level * 1.25)
        self._reload_current_pixmap()

    def _zoom_out(self) -> None:
        """Zoom out by 25%"""
        self._fit_mode = False
        self.btn_fit.setChecked(False)
        self._zoom_level = max(0.1, self._zoom_level / 1.25)
        self._reload_current_pixmap()

    def _reset_zoom(self) -> None:
        """Reset zoom to 100%"""
        self._fit_mode = False
        self.btn_fit.setChecked(False)
        self._zoom_level = 1.0
        self._reload_current_pixmap()

    def _toggle_fit_mode(self) -> None:
        """Toggle between fit-to-window and manual zoom"""
        self._fit_mode = not self._fit_mode
        self.btn_fit.setChecked(self._fit_mode)
        if self._fit_mode:
            self._reload_current_pixmap()

    def _reload_current_pixmap(self) -> None:
        """Reload current image with new zoom settings"""
        if not self._images or self._current_index >= len(self._images):
            return

        pixmap = getattr(self, '_current_pixmap', None)
        if pixmap and not pixmap.isNull():
            self._display_pixmap(pixmap)

    def resizeEvent(self, event) -> None:
        """Handle resize - update image scaling if in fit mode"""
        super().resizeEvent(event)
        # Guard against resize during init before UI is set up
        if self._fit_mode and hasattr(self, 'image_label'):
            self._reload_current_pixmap()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle mouse wheel for zoom"""
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom_in()
        elif delta < 0:
            self._zoom_out()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Left click = previous image, right click = next image."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._go_prev()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._go_next()
            event.accept()
        else:
            super().mousePressEvent(event)

    def current_index(self) -> int:
        """Get current image index"""
        return self._current_index

    def set_images(self, images: List[str], start_index: int = 0) -> None:
        """Set new image list

        Args:
            images: List of image URLs or file paths
            start_index: Index to start at
        """
        self._images = images
        self._current_index = max(0, min(start_index, len(images) - 1))
        self._load_current_image()
