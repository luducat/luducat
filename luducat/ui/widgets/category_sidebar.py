# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# category_sidebar.py

"""Reusable category sidebar widget for settings panels.

Provides a vertical list of icon+label items grouped by category,
designed for sidebar navigation in settings dialogs.
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from luducat.utils.icons import load_tinted_icon

logger = logging.getLogger(__name__)


class CategorySidebar(QWidget):
    """Sidebar widget with icon+label category items.

    Emits currentChanged(str) with the category key when selection changes.
    Designed to be reusable by any settings panel (metadata, plugin manager, etc.).
    """

    currentChanged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("categorySidebar")
        self._keys: list[str] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._list = QListWidget()
        self._list.setObjectName("categoryListWidget")
        self._list.setIconSize(QSize(32, 32))
        self._list.setSpacing(2)
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self._list)

    def add_category(self, key: str, label: str, icon_path: str) -> None:
        """Add a category item to the sidebar.

        Args:
            key: Unique category key (e.g. "General")
            label: Display label
            icon_path: SVG filename in assets/icons/ (e.g. "cat-general.svg")
        """
        icon = load_tinted_icon(icon_path, size=32)
        item = QListWidgetItem(icon, label)
        item.setSizeHint(QSize(0, 44))
        self._list.addItem(item)
        self._keys.append(key)

    def select_category(self, key: str) -> None:
        """Select a category by key."""
        if key in self._keys:
            self._list.setCurrentRow(self._keys.index(key))

    def current_category(self) -> str:
        """Return the currently selected category key."""
        row = self._list.currentRow()
        if 0 <= row < len(self._keys):
            return self._keys[row]
        return ""

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._keys):
            self.currentChanged.emit(self._keys[row])
