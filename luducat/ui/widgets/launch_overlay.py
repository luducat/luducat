# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# launch_overlay.py

"""Launch overlay widget for luducat

Displays a semi-transparent overlay with game launch information while
a game is being launched. Provides immediate visual feedback and prevents
double-click launches.
"""

import logging
import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..sync_widget import StripedProgressBar

logger = logging.getLogger(__name__)

# Minimum time (seconds) the overlay stays visible to avoid flicker
_MIN_DISPLAY_TIME = 1.5


class LaunchOverlay(QWidget):
    """Semi-transparent overlay shown during game launch

    Displays a centered box with:
    - Header ("Launching")
    - Game title
    - Store/runner subtitle
    - Cover image (if available)
    - Indeterminate striped progress bar

    The overlay covers its parent widget and blocks interaction
    to prevent double-launch. A minimum display time of 1.5s
    prevents jarring flicker for fast launches.

    Usage:
        overlay = LaunchOverlay(parent_widget)
        overlay.show_launch("Game Title", "Steam", "Heroic", pixmap)
        # ... launch completes:
        overlay.hide_launch()
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("launchOverlay")

        self._cursor_pushed = False
        self._shown_at: float = 0.0
        self._hide_pending = False

        self._setup_ui()
        self.hide()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Center container
        self._container = QWidget()
        self._container.setObjectName("launchOverlayContainer")
        self._container.setFixedWidth(480)
        self._container.setStyleSheet("""
            QWidget#launchOverlayContainer {
                background-color: palette(window);
                border-radius: 8px;
            }
        """)

        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(24, 20, 24, 20)
        container_layout.setSpacing(10)

        # Header
        self._header_label = QLabel(_("Launching"))
        self._header_label.setObjectName("launchOverlayHeader")
        self._header_label.setAlignment(Qt.AlignCenter)
        container_layout.addWidget(self._header_label)

        # Game title
        self._title_label = QLabel()
        self._title_label.setObjectName("launchOverlayTitle")
        self._title_label.setAlignment(Qt.AlignCenter)
        self._title_label.setWordWrap(True)
        container_layout.addWidget(self._title_label)

        # Store/runner subtitle
        self._subtitle_label = QLabel()
        self._subtitle_label.setObjectName("launchOverlaySubtitle")
        self._subtitle_label.setAlignment(Qt.AlignCenter)
        self._subtitle_label.setWordWrap(True)
        container_layout.addWidget(self._subtitle_label)

        # Cover image
        self._cover_label = QLabel()
        self._cover_label.setAlignment(Qt.AlignCenter)
        self._cover_label.hide()
        container_layout.addWidget(self._cover_label)

        # Indeterminate progress bar
        self._progress = StripedProgressBar()
        self._progress.setRange(0, 0)  # Indeterminate
        self._progress.setFixedHeight(18)
        self._progress.setTextVisible(False)
        container_layout.addWidget(self._progress)

        layout.addStretch()
        layout.addWidget(self._container, 0, Qt.AlignCenter)
        layout.addStretch()

    def show_launch(
        self,
        title: str,
        store_display: str,
        runner_display: str = "",
        cover_pixmap: Optional[QPixmap] = None,
    ) -> None:
        """Show the launch overlay.

        Args:
            title: Game title
            store_display: Store display name (e.g. "Steam")
            runner_display: Runner display name (e.g. "Heroic"), empty if direct
            cover_pixmap: Optional cover image pixmap
        """
        self._hide_pending = False
        self._shown_at = time.monotonic()

        self._header_label.setText(_("Launching"))
        self._title_label.setText(title)

        if runner_display and runner_display != store_display:
            self._subtitle_label.setText(
                _("{store} (via {runner})").format(
                    store=store_display, runner=runner_display
                )
            )
        else:
            self._subtitle_label.setText(store_display)

        # Cover image
        if cover_pixmap and not cover_pixmap.isNull():
            scaled = cover_pixmap.scaled(
                200, 300,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._cover_label.setPixmap(scaled)
            self._cover_label.show()
        else:
            self._cover_label.hide()

        # Remove any previous fixed height — let layout size naturally
        self._container.setMinimumHeight(0)
        self._container.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX

        # Resize to cover parent
        if self.parent():
            self.setGeometry(self.parent().rect())

        if not self._cursor_pushed:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._cursor_pushed = True

        self.show()
        self.raise_()
        QApplication.processEvents()

        logger.debug("Launch overlay shown: %s", title)

    def update_status(self, text: str) -> None:
        """Update the header text while overlay is visible."""
        self._header_label.setText(text)

    def hide_launch(self) -> None:
        """Hide the overlay, respecting minimum display time."""
        if self._hide_pending:
            return

        elapsed = time.monotonic() - self._shown_at
        remaining_ms = int((_MIN_DISPLAY_TIME - elapsed) * 1000)

        if remaining_ms > 0:
            self._hide_pending = True
            QTimer.singleShot(remaining_ms, self._do_hide)
        else:
            self._do_hide()

    def _do_hide(self) -> None:
        """Actually hide the overlay."""
        self._hide_pending = False
        self.hide()
        if self._cursor_pushed:
            QApplication.restoreOverrideCursor()
            self._cursor_pushed = False
        logger.debug("Launch overlay hidden")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.parent():
            self.setGeometry(self.parent().rect())
