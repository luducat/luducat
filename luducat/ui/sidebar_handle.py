# This file is part of the luducat Project.
# License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

"""Sidebar handle - thin vertical strip shown when the game list is collapsed."""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPainter, QPen


class SidebarHandle(QWidget):
    """Grip strip at the left edge that unfolds the collapsed sidebar."""

    double_clicked = Signal()

    HANDLE_WIDTH = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.HANDLE_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("sidebarHandle")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        bg = self.palette().window().color()
        painter.fillRect(self.rect(), bg)

        sep_color = self.palette().mid().color()
        painter.setPen(QPen(sep_color, 1))
        painter.drawLine(w - 1, 0, w - 1, h)

        grip_color = self.palette().mid().color()
        pen = QPen(grip_color, 2)
        painter.setPen(pen)

        dot_count = 3
        dot_spacing = 6
        total_height = (dot_count - 1) * dot_spacing
        start_y = (h - total_height) // 2
        cx = w // 2

        for i in range(dot_count):
            y = start_y + i * dot_spacing
            painter.drawPoint(cx, y)

        painter.end()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)
