# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# tag_editor.py

"""Tag editor dialog for luducat

Modal dialog for editing tags on a game.
Shows checkboxes for all available tags with option to create new ones.
"""

import logging
from typing import List, Dict, Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QWidget,
    QCheckBox,
    QLineEdit,
    QColorDialog,
    QDialogButtonBox,
    QFrame,
    QMessageBox,
)

from ...core.constants import DEFAULT_TAG_COLOR

logger = logging.getLogger(__name__)


class ColorButton(QPushButton):
    """Button that shows a color and opens color picker on click"""

    color_changed = Signal(str)

    def __init__(self, color: str = DEFAULT_TAG_COLOR, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(24, 24)
        self.clicked.connect(self._pick_color)
        self._update_style()

    def _update_style(self) -> None:
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._color};
                border: 1px solid palette(mid);
                border-radius: 3px;
            }}
            QPushButton:hover {{
                border: 2px solid palette(highlight);
            }}
        """)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._color),
            self,
            _("Choose Tag Color")
        )
        if color.isValid():
            self._color = color.name()
            self._update_style()
            self.color_changed.emit(self._color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self._update_style()


class TagCheckbox(QWidget):
    """Checkbox with colored indicator for a tag"""

    def __init__(
        self,
        tag_name: str,
        tag_color: str,
        checked: bool = False,
        parent: Optional[QWidget] = None
    ):
        super().__init__(parent)

        self.tag_name = tag_name
        self.tag_color = tag_color

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        # Color indicator
        self.color_label = QLabel()
        self.color_label.setFixedSize(12, 12)
        self._update_color_style()
        layout.addWidget(self.color_label)

        # Checkbox
        self.checkbox = QCheckBox(tag_name)
        self.checkbox.setChecked(checked)
        layout.addWidget(self.checkbox)

        layout.addStretch()

    def _update_color_style(self) -> None:
        self.color_label.setStyleSheet(f"""
            QLabel {{
                background-color: {self.tag_color};
                border-radius: 6px;
            }}
        """)

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self.checkbox.setChecked(checked)


class TagEditorDialog(QDialog):
    """Dialog for editing tags on a game

    Shows all available tags with checkboxes.
    Pre-checks tags already assigned to the game.
    Allows creating new tags inline.

    Signals:
        tags_updated: Emitted when dialog is accepted with new tag list
        tag_created: Emitted when a new tag is created (name, color)
    """

    tags_updated = Signal(list)  # List of selected tag names
    tag_created = Signal(str, str)  # name, color

    def __init__(
        self,
        game_title: str,
        all_tags: List[Dict[str, Any]],
        game_tags: List[str],
        parent: Optional[QWidget] = None
    ):
        """Initialize tag editor dialog

        Args:
            game_title: Title of the game being edited
            all_tags: List of all available tags (dicts with id, name, color)
            game_tags: List of tag names currently on the game
            parent: Parent widget
        """
        super().__init__(parent)

        self.game_title = game_title
        self.all_tags = all_tags
        self.game_tags = set(game_tags)
        self._tag_checkboxes: Dict[str, TagCheckbox] = {}

        self.setWindowTitle(_("Edit Tags: {title}").format(title=game_title))
        self.setMinimumSize(350, 400)
        self.resize(400, 500)

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Create dialog UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Header
        header = QLabel(_("Select tags for this game:"))
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header)

        # Scrollable tag list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.StyledPanel)

        self.tag_list_widget = QWidget()
        self.tag_list_layout = QVBoxLayout(self.tag_list_widget)
        self.tag_list_layout.setContentsMargins(8, 8, 8, 8)
        self.tag_list_layout.setSpacing(4)

        # Add checkboxes for each tag
        for tag in self.all_tags:
            checkbox = TagCheckbox(
                tag["name"],
                tag["color"],
                checked=tag["name"] in self.game_tags
            )
            self._tag_checkboxes[tag["name"]] = checkbox
            self.tag_list_layout.addWidget(checkbox)

        self.tag_list_layout.addStretch()
        scroll.setWidget(self.tag_list_widget)
        layout.addWidget(scroll)

        # Create new tag section
        create_frame = QFrame()
        create_frame.setFrameShape(QFrame.Shape.StyledPanel)
        create_layout = QHBoxLayout(create_frame)
        create_layout.setContentsMargins(8, 8, 8, 8)

        self.new_tag_input = QLineEdit()
        self.new_tag_input.setPlaceholderText(_("New tag name..."))
        self.new_tag_input.returnPressed.connect(self._create_tag)
        create_layout.addWidget(self.new_tag_input)

        self.new_tag_color = ColorButton(DEFAULT_TAG_COLOR)
        create_layout.addWidget(self.new_tag_color)

        self.btn_create = QPushButton(_("Create"))
        self.btn_create.clicked.connect(self._create_tag)
        create_layout.addWidget(self.btn_create)

        layout.addWidget(create_frame)

        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _create_tag(self) -> None:
        """Create a new tag and add checkbox for it"""
        name = self.new_tag_input.text().strip()
        if not name:
            return

        # Check for duplicate
        if name in self._tag_checkboxes:
            QMessageBox.warning(
                self,
                _("Tag Exists"),
                _("Tag '{name}' already exists.").format(name=name)
            )
            self.new_tag_input.selectAll()
            return

        color = self.new_tag_color.color()

        # Emit signal to create tag in database
        self.tag_created.emit(name, color)

        # Add checkbox (checked by default for new tags)
        checkbox = TagCheckbox(name, color, checked=True)
        self._tag_checkboxes[name] = checkbox

        # Insert before the stretch
        count = self.tag_list_layout.count()
        self.tag_list_layout.insertWidget(count - 1, checkbox)

        # Clear input
        self.new_tag_input.clear()
        self.new_tag_color.set_color(DEFAULT_TAG_COLOR)

        logger.info(f"Created new tag: {name} ({color})")

    def _on_accept(self) -> None:
        """Handle OK button - emit selected tags and close"""
        selected = self.get_selected_tags()
        self.tags_updated.emit(selected)
        self.accept()

    def get_selected_tags(self) -> List[str]:
        """Get list of selected tag names"""
        return [
            name for name, checkbox in self._tag_checkboxes.items()
            if checkbox.is_checked()
        ]
