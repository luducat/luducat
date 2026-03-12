# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# status_bar.py

"""Status bar for luducat

Always visible at the bottom of the window.
Contains:
- Game count (left)
- Refresh button (left)
- Density slider (right, hidden in list mode)
- Network status indicator (right, clickable)
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
)

from ..core.constants import GRID_DENSITY_MIN, GRID_DENSITY_MAX, DEFAULT_GRID_DENSITY

logger = logging.getLogger(__name__)


class StatusBar(QWidget):
    """Status bar with game count, refresh button, and density slider

    Signals:
        density_changed: Emitted when density slider changes
        refresh_requested: Emitted when refresh button is clicked
    """

    density_changed = Signal(int)
    refresh_requested = Signal()
    network_toggle_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._game_count = 0
        self._is_online = True

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Create status bar layout"""
        self.setMinimumHeight(28)
        self.setMaximumHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 12, 4)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Refresh button (leftmost)
        self._refresh_btn = QPushButton()
        self._refresh_btn.setObjectName("refreshButton")
        self._refresh_btn.setFixedSize(24, 24)
        self._refresh_btn.setToolTip(_("Refresh game list"))
        self._refresh_btn.clicked.connect(lambda: self.refresh_requested.emit())
        self._update_reload_icon()
        layout.addWidget(self._refresh_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Game count
        self.count_label = QLabel(_("0 games"))
        self.count_label.setObjectName("gameCount")
        layout.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addStretch()

        # Density slider (right, no labels)
        self.density_slider = QSlider(Qt.Orientation.Horizontal)
        self.density_slider.setObjectName("densitySlider")
        self.density_slider.setMinimum(GRID_DENSITY_MIN)
        self.density_slider.setMaximum(GRID_DENSITY_MAX)
        self.density_slider.setValue(DEFAULT_GRID_DENSITY)
        self.density_slider.setFixedWidth(120)
        self.density_slider.setInvertedAppearance(True)  # Left = more items
        self.density_slider.valueChanged.connect(self._on_slider_changed)
        layout.addWidget(self.density_slider, 0, Qt.AlignmentFlag.AlignVCenter)

        # Separator before network indicator
        self._network_sep = QLabel(" ")   # was "|"
        self._network_sep.setObjectName("statusSeparator")
        layout.addWidget(self._network_sep, 0, Qt.AlignmentFlag.AlignVCenter)

        # Network indicator (right, clickable)
        self._network_label = QLabel(_("● Online"))
        self._network_label.setObjectName("networkIndicator")
        self._network_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._network_label.mousePressEvent = self._on_network_clicked
        layout.addWidget(self._network_label, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_density_visible(self, visible: bool) -> None:
        """Show or hide the density slider (hidden in list mode).

        Args:
            visible: True to show density slider, False to hide it
        """
        self.density_slider.setVisible(visible)

    def _on_slider_changed(self, value: int) -> None:
        """Handle slider value change"""
        self.density_changed.emit(value)

    def set_game_count(self, count: int) -> None:
        """Set game count display immediately.

        Args:
            count: Number of games
        """
        self._game_count = count
        self.count_label.setText(
            ngettext("{count} game", "{count} games", count).format(count=f"{count:,}")
        )

    def set_density(self, density: int) -> None:
        """Set density slider value

        Args:
            density: Density value (grid item width)
        """
        self.density_slider.blockSignals(True)
        self.density_slider.setValue(density)
        self.density_slider.blockSignals(False)

    def get_density(self) -> int:
        """Get current density value"""
        return self.density_slider.value()

    # Semantic status colors — set via palette, not QSS
    _COLOR_ONLINE = QColor(46, 158, 65)   # Green
    _COLOR_OFFLINE = QColor(224, 138, 0)  # Amber

    def set_online_status(self, online: bool) -> None:
        """Update network indicator display.

        Args:
            online: True for online mode, False for offline
        """
        self._is_online = online
        if online:
            self._network_label.setText(_("● Online"))
            self._network_label.setToolTip(
                _("Connected — click to switch to offline mode")
            )
        else:
            self._network_label.setText(_("● Offline"))
            self._network_label.setToolTip(
                _("Offline — click to go online")
            )

        # Widget-level QSS overrides application-level QSS rules
        color = self._COLOR_ONLINE if online else self._COLOR_OFFLINE
        self._network_label.setStyleSheet(f"color: {color.name()};")

    def set_connectivity_hint(self, available: bool) -> None:
        """Temporarily show connectivity hint while in offline mode.

        Called when DNS succeeds while offline — hints user can go online.

        Args:
            available: True if connectivity detected
        """
        if available and not self._is_online:
            self._network_label.setText(_("● Offline (connection available)"))

    def clear_connectivity_hint(self) -> None:
        """Revert network label to plain offline text."""
        if not self._is_online:
            self._network_label.setText(_("● Offline"))

    def _on_network_clicked(self, event) -> None:
        """Handle click on network indicator."""
        self.network_toggle_requested.emit()

    def show_temporary_message(self, message: str, duration_ms: int = 5000) -> None:
        """Show a temporary message in the count label area.

        The message is automatically replaced with the game count after
        ``duration_ms`` milliseconds.

        Args:
            message: Message text to display
            duration_ms: Duration in milliseconds before restoring count
        """
        self.count_label.setText(message)
        self.setVisible(True)
        QTimer.singleShot(
            duration_ms,
            lambda: self.count_label.setText(
                ngettext("{count} game", "{count} games", self._game_count).format(
                    count=f"{self._game_count:,}"
                )
            ),
        )

    def _update_reload_icon(self) -> None:
        """Create a theme-aware reload icon for the refresh button."""
        svg_path = Path(__file__).parent.parent / "assets" / "icons" / "reload.svg"
        source = QPixmap(str(svg_path))
        if source.isNull():
            self._refresh_btn.setText("\u21bb")
            return
        size = 16
        source = source.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        color = QApplication.palette().color(QPalette.ColorRole.WindowText)
        colored = QPixmap(source.size())
        colored.fill(Qt.GlobalColor.transparent)
        painter = QPainter(colored)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawPixmap(0, 0, source)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(colored.rect(), color)
        painter.end()
        self._refresh_btn.setIcon(QIcon(colored))
        self._refresh_btn.setIconSize(colored.size())
        self._refresh_btn.setText("")

    def refresh_icons(self) -> None:
        """Refresh theme-aware icons after a theme change."""
        self._update_reload_icon()
