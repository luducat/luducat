# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# loading_overlay.py

"""Loading overlay widget for luducat

Displays a semi-transparent overlay with loading indicator while
the application loads data. Keeps UI responsive by allowing
window movement and resize during loading.
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


class LoadingOverlay(QWidget):
    """Semi-transparent overlay with loading indicator

    Shows a centered loading box with:
    - Indeterminate progress bar (spinner effect)
    - Status message
    - Detail text

    The overlay covers its parent widget and allows the parent
    to remain responsive (moveable, resizable) during loading.

    Usage:
        overlay = LoadingOverlay(parent_widget)
        overlay.show_loading("Loading games...")
        # ... do work, periodically call:
        overlay.update_status("Loading", "Fetching Steam data...")
        # ... when done:
        overlay.hide_loading()
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        # Make overlay fill parent and stay on top
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_StyledBackground, True)

        # Semi-transparent background — styled via QSS #loadingOverlay selector
        self.setObjectName("loadingOverlay")

        # Create centered content box
        self._setup_ui()

        # Start hidden
        self.hide()
        self._cursor_pushed = False

    def _setup_ui(self) -> None:
        """Set up the overlay UI"""
        # Main layout to center content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Center container
        self._container = QWidget()
        self._container.setFixedSize(420, 140)
        self._container.setStyleSheet("""
            QWidget {
                background-color: palette(window);
                border-radius: 8px;
            }
        """)

        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(24, 20, 24, 20)
        container_layout.setSpacing(10)

        # Status label (main message) — on top, styled via QSS #loadingStatusLabel
        self._status_label = QLabel(_("Loading..."))
        self._status_label.setObjectName("loadingStatusLabel")
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setWordWrap(True)
        container_layout.addWidget(self._status_label)

        # Progress bar (determinate mode with phase-based progress)
        # Structural styling is inline to keep the slim borderless look
        # across all themes; chunk COLOR is left to the theme QSS so
        # themes like Windows 7 can use their own accent (green).
        self._progress = QProgressBar()
        self._progress.setObjectName("loadingProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 3px;
                background-color: palette(mid);
            }
        """)
        container_layout.addWidget(self._progress)

        # Detail label (sub-message) — below progress
        self._detail_label = QLabel("")
        self._detail_label.setAlignment(Qt.AlignCenter)
        self._detail_label.setWordWrap(True)
        container_layout.addWidget(self._detail_label)

        # Add container to center of layout
        layout.addStretch()
        layout.addWidget(self._container, 0, Qt.AlignCenter)
        layout.addStretch()

    def show_loading(self, message: str = "Loading...", detail: str = "", progress: int = 0) -> None:
        """Show the overlay with a loading message

        Args:
            message: Main status message
            detail: Optional detail text
            progress: Progress percentage (0-100)
        """
        self._status_label.setText(message)
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))
        self._progress.setValue(progress)

        # Resize to cover parent
        if self.parent():
            self.setGeometry(self.parent().rect())

        # Show busy cursor while loading (guard against double-push)
        if not self._cursor_pushed:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._cursor_pushed = True

        self.show()
        self.raise_()

        # Process events to ensure overlay is painted
        QApplication.processEvents()

        logger.debug(f"Loading overlay shown: {message}")

    def update_status(self, message: str, detail: str = "", progress: int = -1) -> None:
        """Update the status text and progress

        Args:
            message: Main status message
            detail: Optional detail text
            progress: Progress percentage (0-100), or -1 to keep current value
        """
        self._status_label.setText(message)
        self._detail_label.setText(detail)
        self._detail_label.setVisible(bool(detail))
        if progress >= 0:
            self._progress.setValue(progress)

        # Process events to update display and keep UI responsive
        QApplication.processEvents()

    def hide_loading(self) -> None:
        """Hide the overlay"""
        self.hide()
        if self._cursor_pushed:
            QApplication.restoreOverrideCursor()
            self._cursor_pushed = False
        logger.debug("Loading overlay hidden")

    def resizeEvent(self, event) -> None:
        """Keep overlay sized to parent when parent resizes"""
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())

    def showEvent(self, event) -> None:
        """Ensure overlay covers parent when shown"""
        super().showEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())
