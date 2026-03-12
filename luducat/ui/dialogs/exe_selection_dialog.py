# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# exe_selection_dialog.py

"""Executable Selection Dialog

Shown when Wine launch detects multiple candidate executables and
cannot auto-select with high confidence. Presents candidates sorted
by score with radio button selection.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


@dataclass
class ExeSelectionResult:
    """Result returned from the exe selection dialog."""
    path: Path
    score: int
    source: str
    remember: bool


class ExeSelectionDialog(QDialog):
    """Dialog for selecting a game executable from candidates."""

    def __init__(self, candidates, game_title: str, prefix, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Select Game Executable"))
        self.setMinimumWidth(500)
        self._candidates = candidates
        self._result: Optional[ExeSelectionResult] = None

        layout = QVBoxLayout(self)

        # Header
        header = QLabel(_("Multiple executables found for '{title}':").format(
            title=game_title
        ))
        header.setWordWrap(True)
        layout.addWidget(header)

        # Prefix path hint
        prefix_label = QLabel(str(prefix.prefix_path))
        prefix_label.setObjectName("hintLabel")
        prefix_label.setWordWrap(True)
        layout.addWidget(prefix_label)

        # Scrollable candidate list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(4, 4, 4, 4)

        self._button_group = QButtonGroup(self)
        self._radio_buttons: list = []

        for i, candidate in enumerate(candidates):
            # Show relative path within prefix for readability
            try:
                rel_path = candidate.path.relative_to(prefix.prefix_path)
            except ValueError:
                rel_path = candidate.path

            radio = QRadioButton(str(rel_path))
            radio.setToolTip(candidate.reason)

            if i == 0 and candidates[0].score >= 50:
                radio.setChecked(True)

            self._button_group.addButton(radio, i)
            self._radio_buttons.append(radio)
            scroll_layout.addWidget(radio)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        # Remember checkbox
        self._remember_check = QCheckBox(_("Remember this selection"))
        self._remember_check.setChecked(True)
        layout.addWidget(self._remember_check)

        # Standard buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _on_accept(self) -> None:
        """Build result from selected radio button."""
        checked_id = self._button_group.checkedId()
        if checked_id < 0:
            self.reject()
            return

        candidate = self._candidates[checked_id]
        self._result = ExeSelectionResult(
            path=candidate.path,
            score=candidate.score,
            source=candidate.source,
            remember=self._remember_check.isChecked(),
        )
        self.accept()

    def get_result(self) -> Optional[ExeSelectionResult]:
        """Get the selection result after dialog closes."""
        return self._result
