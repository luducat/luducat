# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# tag_manager_dialog.py

"""Standalone Tag Manager dialog for luducat.

Extracted from SettingsDialog — provides full CRUD for user tags,
merge, score management, and import/export.

Uses QTreeWidget with sortable columns, Thunderbird-style column
visibility, and score-tinted row delegate (same pattern as
SteamGridDB AuthorScoreDialog). Compact layout: table fills most
space, icon-button controls row below, flat bottom row.
"""

import logging
from typing import Dict, List, Optional, Set, Tuple

from luducat.core.json_compat import json
from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QColor, QPainter

from ...core.constants import DEFAULT_TAG_COLOR
from ...utils.icons import load_tinted_icon

logger = logging.getLogger(__name__)

# Column indices
COL_NAME = 0    # Stretch — delegate paints 12px color dot + text
COL_COUNT = 1   # 70px, numeric sort, center
COL_SCORE = 2   # 60px, numeric sort, center
COL_NSFW = 3    # 50px, center
COL_SOURCE = 4  # 80px
COL_MENU = 5    # 24px (hamburger)

_NUMERIC_COLS = {COL_COUNT, COL_SCORE}

# NSFW override label map
_NSFW_LABELS = {-1: N_("SFW"), 0: "", 1: N_("NSFW")}


def _lerp_color(base: QColor, target: QColor, t: float) -> QColor:
    """Linear interpolation between two colors."""
    return QColor(
        int(base.red() + (target.red() - base.red()) * t),
        int(base.green() + (target.green() - base.green()) * t),
        int(base.blue() + (target.blue() - base.blue()) * t),
        int(base.alpha() + (target.alpha() - base.alpha()) * t),
    )


class _TagTreeItem(QTreeWidgetItem):
    """QTreeWidgetItem with numeric sort for count/score columns."""

    def __init__(
        self,
        tag_id: int,
        tag_color: str,
        tag_source: str = "native",
        nsfw_override: int = 0,
    ):
        super().__init__()
        self.tag_id = tag_id
        self.tag_color = tag_color
        self.tag_source = tag_source
        self.nsfw_override = nsfw_override

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column in _NUMERIC_COLS:
            try:
                a_val = int(self.text(column)) if self.text(column) else 0
                b_val = int(other.text(column)) if other.text(column) else 0
                return a_val < b_val
            except ValueError:
                pass
        return self.text(column).lower() < other.text(column).lower()


_DOT_SIZE = 12
_DOT_LEFT_PAD = 6
_DOT_TEXT_GAP = 4
_DOT_TOTAL_OFFSET = _DOT_LEFT_PAD + _DOT_SIZE + _DOT_TEXT_GAP


class _TagScoreDelegate(QStyledItemDelegate):
    """Delegate that tints row background green/red based on score,
    and paints a color dot in the Name column."""

    GREEN_TARGET = QColor(40, 180, 60)
    RED_TARGET = QColor(200, 50, 50)
    MIN_BLEND = 0.15
    MAX_BLEND = 0.5

    def update_score_colors(self, positive: str, negative: str) -> None:
        """Update score tint colors from current theme."""
        self.GREEN_TARGET = QColor(positive)
        self.RED_TARGET = QColor(negative)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        # Score-based row tint (skip selected rows)
        if not (option.state & QStyle.StateFlag.State_Selected):
            model = index.model()
            if model is not None:
                score_index = model.index(index.row(), COL_SCORE, index.parent())
                score_text = model.data(score_index, Qt.ItemDataRole.DisplayRole)
                try:
                    score = int(score_text)
                except (ValueError, TypeError):
                    score = 0

                if score != 0:
                    intensity = min(1.0, abs(score) / 50.0)
                    base_color = option.palette.base().color()
                    target = self.GREEN_TARGET if score > 0 else self.RED_TARGET
                    blend = self.MIN_BLEND + intensity * (self.MAX_BLEND - self.MIN_BLEND)
                    tinted = _lerp_color(base_color, target, blend)
                    painter.fillRect(option.rect, tinted)

        # Paint color dot + shifted text in COL_NAME
        if index.column() == COL_NAME:
            tree = self.parent()
            if tree and hasattr(tree, "itemFromIndex"):
                item = tree.itemFromIndex(index)
                if item and hasattr(item, "tag_color"):
                    rect = option.rect
                    # Draw the color dot
                    painter.save()
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    color = QColor(item.tag_color)
                    painter.setBrush(color)
                    painter.setPen(Qt.PenStyle.NoPen)
                    dot_x = rect.x() + _DOT_LEFT_PAD
                    dot_y = rect.y() + (rect.height() - _DOT_SIZE) // 2
                    painter.drawEllipse(dot_x, dot_y, _DOT_SIZE, _DOT_SIZE)
                    painter.restore()

                    # Draw text with shifted rect so it doesn't overlap
                    shifted = QStyleOptionViewItem(option)
                    shifted.rect = QRect(
                        rect.x() + _DOT_TOTAL_OFFSET,
                        rect.y(),
                        rect.width() - _DOT_TOTAL_OFFSET,
                        rect.height(),
                    )
                    super().paint(painter, shifted, index)
                    return

        super().paint(painter, option, index)


class TagManagerDialog(QDialog):
    """Standalone Tag Manager dialog.

    Provides full CRUD for user tags, merge, score management,
    and import/export. Opened from Tools menu.
    """

    tags_changed = Signal()  # Emitted when tags are modified

    def __init__(
        self,
        game_service,
        parent: Optional[QWidget] = None,
        score_colors: Optional[Dict[str, str]] = None,
    ):
        super().__init__(parent)
        self.game_service = game_service
        self._tag_items: Dict[int, _TagTreeItem] = {}
        self._score_colors = score_colors
        self._icon_buttons: list[QPushButton] = []
        self._hidden_columns: set[int] = set()

        # Pending changes (applied only on _apply_changes)
        self._pending_creates: List[Tuple[str, str, int]] = []  # (name, color, nsfw_override)
        self._pending_updates: Dict[int, Tuple[str, str, int]] = {}  # tag_id: (name, color, nsfw_override)
        self._pending_deletes: Set[int] = set()  # tag_ids to delete
        self._pending_scores: Dict[int, int] = {}  # tag_id -> new_score
        self._next_temp_id = -1  # Negative IDs for unsaved tags

        self.setWindowTitle(_("Tag Manager"))
        self.setMinimumSize(700, 480)
        self.resize(820, 600)

        self._setup_ui()
        self._load_tags()

    def _has_pending_changes(self) -> bool:
        """Check if there are any unsaved changes."""
        return bool(self._pending_creates or self._pending_updates or self._pending_deletes or self._pending_scores)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Tree widget — fills most of the dialog
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            _("Name"), _("Games"), _("Score"),
            _("NSFW"), _("Source"), "",
        ])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._tree.setFrameShape(QTreeWidget.Shape.StyledPanel)

        # Delegate
        self._delegate = _TagScoreDelegate(self._tree)
        if self._score_colors:
            self._delegate.update_score_colors(
                self._score_colors.get("positive", "#28b43c"),
                self._score_colors.get("negative", "#c83232"),
            )
        self._tree.setItemDelegate(self._delegate)

        # Header sizing
        hdr = self._tree.header()
        hdr.setStretchLastSection(False)

        hdr.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)

        hdr.setSectionResizeMode(COL_COUNT, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_COUNT, 70)

        hdr.setSectionResizeMode(COL_SCORE, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_SCORE, 60)

        hdr.setSectionResizeMode(COL_NSFW, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_NSFW, 50)

        hdr.setSectionResizeMode(COL_SOURCE, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_SOURCE, 80)

        _BTN_COL_W = 24
        hdr.setSectionResizeMode(COL_MENU, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_MENU, _BTN_COL_W)

        # Center-align numeric/badge column headers
        for col in (COL_COUNT, COL_SCORE, COL_NSFW):
            self._tree.headerItem().setTextAlignment(
                col, Qt.AlignmentFlag.AlignCenter
            )

        # Sorting — enabled after population to avoid per-item resort
        self._tree.setSortingEnabled(False)

        # Enable score buttons based on selection
        self._tree.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # Column visibility button (Thunderbird-style)
        self._col_menu = QMenu(hdr)
        self._col_btn = QToolButton(hdr)
        self._col_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._col_btn.setMenu(self._col_menu)
        self._col_btn.setAutoRaise(True)
        self._col_btn.setToolTip(_("Column visibility"))
        self._col_btn.setText("\u2630")
        hdr.geometriesChanged.connect(self._position_col_btn)
        self._build_column_menu()

        layout.addWidget(self._tree, 1)

        # Controls row: [-remove] [edit] [+add] [input (stretch)] [color] [up] [0] [down]
        controls = QHBoxLayout()
        controls.setSpacing(6)

        self._btn_remove = QPushButton()
        self._btn_remove.setToolTip(_("Delete selected tags"))
        self._btn_remove.setIconSize(QSize(16, 16))
        self._btn_remove.setEnabled(False)
        self._btn_remove.clicked.connect(self._on_delete_selected)
        self._icon_buttons.append(self._btn_remove)
        controls.addWidget(self._btn_remove)

        self._btn_edit = QPushButton()
        self._btn_edit.setToolTip(_("Edit first selected tag"))
        self._btn_edit.setIconSize(QSize(16, 16))
        self._btn_edit.setEnabled(False)
        self._btn_edit.clicked.connect(self._on_edit_selected)
        self._icon_buttons.append(self._btn_edit)
        controls.addWidget(self._btn_edit)

        self._btn_add = QPushButton()
        self._btn_add.setToolTip(_("Create a new tag"))
        self._btn_add.setIconSize(QSize(16, 16))
        self._btn_add.clicked.connect(self._create_tag)
        self._icon_buttons.append(self._btn_add)
        controls.addWidget(self._btn_add)

        self.new_tag_input = QLineEdit()
        self.new_tag_input.setPlaceholderText(_("Tag name..."))
        self.new_tag_input.returnPressed.connect(self._create_tag)
        controls.addWidget(self.new_tag_input, 1)

        # Color button
        self.new_tag_color_btn = QPushButton()
        self.new_tag_color_btn.setFixedSize(24, 24)
        self._new_tag_color = DEFAULT_TAG_COLOR
        self._update_color_button()
        self.new_tag_color_btn.clicked.connect(self._pick_new_tag_color)
        controls.addWidget(self.new_tag_color_btn)

        self._btn_score_up = QPushButton()
        self._btn_score_up.setToolTip(_("Increase score (+1, Shift +5, Ctrl +10)"))
        self._btn_score_up.setIconSize(QSize(16, 16))
        self._btn_score_up.setEnabled(False)
        self._btn_score_up.clicked.connect(self._on_score_up)
        self._icon_buttons.append(self._btn_score_up)
        controls.addWidget(self._btn_score_up)

        self._btn_score_zero = QPushButton()
        self._btn_score_zero.setToolTip(_("Reset score to 0"))
        self._btn_score_zero.setIconSize(QSize(16, 16))
        self._btn_score_zero.setEnabled(False)
        self._btn_score_zero.clicked.connect(self._on_score_zero)
        self._icon_buttons.append(self._btn_score_zero)
        controls.addWidget(self._btn_score_zero)

        self._btn_score_down = QPushButton()
        self._btn_score_down.setToolTip(_("Decrease score (\u22121, Shift \u22125, Ctrl \u221210)"))
        self._btn_score_down.setIconSize(QSize(16, 16))
        self._btn_score_down.setEnabled(False)
        self._btn_score_down.clicked.connect(self._on_score_down)
        self._icon_buttons.append(self._btn_score_down)
        controls.addWidget(self._btn_score_down)

        layout.addLayout(controls)

        # Score summary (right-aligned) + Search row
        self._score_summary_label = QLabel()
        self._score_summary_label.setObjectName("hintLabel")
        self._score_summary_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._score_summary_label)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_label = QLabel(_("Search"))
        search_row.addWidget(search_label)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(_("Search tags..."))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_input, 1)
        layout.addLayout(search_row)

        # Bottom row: Merge, Import, Export | OK, Apply, Cancel
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        self.btn_merge = QPushButton(_("Merge Selected..."))
        self.btn_merge.setToolTip(_("Select two tags to merge (keeps the first, absorbs the second)"))
        self.btn_merge.clicked.connect(self._merge_tags)
        bottom.addWidget(self.btn_merge)

        # Import button with dropdown menu — styled via QSS #importButton
        self._import_btn = QToolButton()
        self._import_btn.setObjectName("importButton")
        self._import_btn.setText(_("Import..."))
        self._import_btn.setToolTip(_("Import tags from a file or from your game stores"))
        self._import_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._import_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._import_btn.clicked.connect(self._import_from_file)
        self._import_menu = QMenu(self._import_btn)
        self._import_menu.aboutToShow.connect(self._build_import_menu)
        self._import_btn.setMenu(self._import_menu)
        bottom.addWidget(self._import_btn)

        self.btn_export = QPushButton(_("Export..."))
        self.btn_export.setToolTip(
            _("Save all your tags to a file.\n"
              "You can share this file with friends or edit it with a text editor.")
        )
        self.btn_export.clicked.connect(self._export_tags)
        bottom.addWidget(self.btn_export)

        bottom.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self._on_reject)
        button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply_changes
        )
        bottom.addWidget(button_box)

        layout.addLayout(bottom)

    def _on_accept(self) -> None:
        """Apply changes and close."""
        self._apply_changes()
        self.accept()

    def _on_reject(self) -> None:
        """Check for unsaved changes before closing."""
        if self._has_pending_changes():
            reply = QMessageBox.question(
                self,
                _("Unsaved Changes"),
                _("You have unsaved tag changes.\n\nDiscard changes?"),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Discard:
                return
        self.reject()

    def _update_color_button(self) -> None:
        """Update color button style"""
        self.new_tag_color_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._new_tag_color};
                border: 1px solid palette(mid);
                border-radius: 3px;
            }}
            QPushButton:hover {{
                border: 2px solid palette(highlight);
            }}
        """)

    def _pick_new_tag_color(self) -> None:
        """Open color picker for new tag"""
        color = QColorDialog.getColor(
            QColor(self._new_tag_color),
            self,
            _("Choose Tag Color")
        )
        if color.isValid():
            self._new_tag_color = color.name()
            self._update_color_button()

    # -----------------------------------------------------------------
    # Column visibility (Thunderbird-style)
    # -----------------------------------------------------------------

    def _position_col_btn(self) -> None:
        """Position the column-visibility button over the dummy column header."""
        hdr = self._tree.header()
        h = hdr.height()
        x = hdr.sectionPosition(COL_MENU)
        w = hdr.sectionSize(COL_MENU)
        self._col_btn.setFixedSize(w, h)
        self._col_btn.move(x, 0)
        self._col_btn.raise_()

    def _build_column_menu(self) -> None:
        """Build the column visibility dropdown menu."""
        self._col_menu.clear()
        labels = [
            (_("Name"), COL_NAME), (_("Games"), COL_COUNT),
            (_("Score"), COL_SCORE), (_("NSFW"), COL_NSFW),
            (_("Source"), COL_SOURCE),
        ]
        # Always visible (toggle disabled)
        always_visible = {COL_NAME, COL_SCORE}

        for label, col in labels:
            action = self._col_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(col not in self._hidden_columns)
            if col in always_visible:
                action.setEnabled(False)
            else:
                action.triggered.connect(
                    lambda checked, c=col: self._toggle_column(c, not checked)
                )

        self._col_menu.addSeparator()
        act_reset = self._col_menu.addAction(_("Reset All Columns"))
        act_reset.triggered.connect(self._reset_columns)

    def _toggle_column(self, col: int, hidden: bool) -> None:
        """Show or hide a column."""
        self._tree.setColumnHidden(col, hidden)
        if hidden:
            self._hidden_columns.add(col)
        else:
            self._hidden_columns.discard(col)
        self._build_column_menu()

    def _reset_columns(self) -> None:
        """Unhide all columns."""
        for col in list(self._hidden_columns):
            self._tree.setColumnHidden(col, False)
        self._hidden_columns.clear()
        self._build_column_menu()

    # -----------------------------------------------------------------
    # Selection handling
    # -----------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """Enable/disable buttons based on current selection."""
        has_sel = bool(self._tree.selectedItems())
        self._btn_remove.setEnabled(has_sel)
        self._btn_edit.setEnabled(has_sel)
        self._btn_score_up.setEnabled(has_sel)
        self._btn_score_zero.setEnabled(has_sel)
        self._btn_score_down.setEnabled(has_sel)

    def _selected_tag_ids(self) -> List[int]:
        """Return tag IDs of currently selected items."""
        result = []
        for item in self._tree.selectedItems():
            if isinstance(item, _TagTreeItem):
                result.append(item.tag_id)
        return result

    # -----------------------------------------------------------------
    # Tag loading
    # -----------------------------------------------------------------

    def _load_tags(self) -> None:
        """Load tags from database into the tree."""
        self._tree.setSortingEnabled(False)
        self._tree.blockSignals(True)
        self._tree.clear()
        self._tag_items.clear()

        tags = self.game_service.get_all_tags()
        game_counts = self.game_service.get_tag_game_counts()

        for tag in tags:
            self._add_tag_item(
                tag["id"], tag["name"], tag["color"],
                tag.get("source", "native"),
                tag.get("nsfw_override", 0),
                tag.get("score", 0),
                game_counts.get(tag["id"], 0),
            )

        self._tree.blockSignals(False)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
        self._update_score_summary()

    def _add_tag_item(self, tag_id: int, name: str, color: str,
                      source: str = "native", nsfw_override: int = 0,
                      score: int = 0, game_count: int = 0) -> None:
        """Add a tag item to the tree."""
        item = _TagTreeItem(tag_id, color, source, nsfw_override)

        # Name (dot painted by delegate)
        item.setText(COL_NAME, name)

        # Game count
        if game_count > 0:
            item.setText(COL_COUNT, str(game_count))
        else:
            item.setText(COL_COUNT, "")
        item.setTextAlignment(COL_COUNT, Qt.AlignmentFlag.AlignCenter)

        # Score
        if score > 0:
            item.setText(COL_SCORE, f"+{score}")
        elif score < 0:
            item.setText(COL_SCORE, str(score))
        else:
            item.setText(COL_SCORE, "0")
        item.setTextAlignment(COL_SCORE, Qt.AlignmentFlag.AlignCenter)

        # NSFW
        nsfw_label = _NSFW_LABELS.get(nsfw_override, "")
        item.setText(COL_NSFW, _(nsfw_label) if nsfw_label else "")
        item.setTextAlignment(COL_NSFW, Qt.AlignmentFlag.AlignCenter)

        # Source
        item.setText(COL_SOURCE, source)

        self._tree.addTopLevelItem(item)
        self._tag_items[tag_id] = item

    def _find_item(self, tag_id: int) -> Optional[_TagTreeItem]:
        """Find a tree item by tag_id."""
        return self._tag_items.get(tag_id)

    # -----------------------------------------------------------------
    # Tag CRUD
    # -----------------------------------------------------------------

    def _create_tag(self) -> None:
        """Stage a new tag creation (applied on save)"""
        name = self.new_tag_input.text().strip()
        if not name:
            return

        # Check for duplicate (existing tags + pending creates, excluding pending deletes)
        existing_names = set()
        for tag_id, item in self._tag_items.items():
            if tag_id not in self._pending_deletes:
                existing_names.add(item.text(COL_NAME))
        pending_names = {n for n, _c, _o in self._pending_creates}
        if name in existing_names or name in pending_names:
            QMessageBox.warning(self, _("Tag Exists"), _("Tag '{name}' already exists.").format(name=name))
            self.new_tag_input.selectAll()
            return

        # Add to pending creates and show in UI with temp ID
        color = self._new_tag_color
        self._pending_creates.append((name, color, 0))

        self._tree.setSortingEnabled(False)
        self._add_tag_item(self._next_temp_id, name, color)
        self._tree.setSortingEnabled(True)
        self._next_temp_id -= 1

        # Clear input
        self.new_tag_input.clear()
        self._new_tag_color = DEFAULT_TAG_COLOR
        self._update_color_button()
        logger.debug(f"Staged new tag: {name}")

    def _on_edit_selected(self) -> None:
        """Edit the first selected tag."""
        items = self._tree.selectedItems()
        if not items:
            return
        item = items[0]
        if isinstance(item, _TagTreeItem):
            self._edit_tag_by_id(item.tag_id)

    def _edit_tag_by_id(self, tag_id: int) -> None:
        """Stage a tag edit via composite dialog (applied on save)"""
        item = self._tag_items.get(tag_id)
        if not item:
            return
        current_name = item.text(COL_NAME)
        current_color = item.tag_color
        current_nsfw = item.nsfw_override

        # Build edit dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(_("Edit Tag"))
        dlg.setMinimumWidth(300)
        form = QFormLayout(dlg)

        # Name
        name_edit = QLineEdit(current_name)
        form.addRow(_("Name:"), name_edit)

        # Color
        color_btn = QPushButton()
        color_btn.setFixedSize(60, 24)
        edit_color = [current_color]  # mutable for closure

        def _update_btn():
            color_btn.setStyleSheet(
                f"QPushButton {{ background-color: {edit_color[0]}; "
                f"border: 1px solid palette(mid); border-radius: 3px; }}"
            )

        _update_btn()

        def _pick_color():
            c = QColorDialog.getColor(QColor(edit_color[0]), dlg, _("Choose Tag Color"))
            if c.isValid():
                edit_color[0] = c.name()
                _update_btn()

        color_btn.clicked.connect(_pick_color)
        form.addRow(_("Color:"), color_btn)

        # Content filter override
        nsfw_combo = QComboBox()
        nsfw_combo.addItem(_("No effect"), 0)
        nsfw_combo.addItem(_("Mark as adult content"), 1)
        nsfw_combo.addItem(_("Mark as safe content"), -1)
        nsfw_combo.setToolTip(
            _("Override the content filter for games with this tag:\n"
              "- No effect: Let the automatic detection decide\n"
              "- Adult content: Always hide these games\n"
              "- Safe content: Never hide these games")
        )
        for i in range(nsfw_combo.count()):
            if nsfw_combo.itemData(i) == current_nsfw:
                nsfw_combo.setCurrentIndex(i)
                break
        form.addRow(_("Content filter:"), nsfw_combo)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton(_("OK"))
        btn_cancel = QPushButton(_("Cancel"))
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        form.addRow(btn_layout)

        btn_ok.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_name = name_edit.text().strip()
        if not new_name:
            QMessageBox.warning(self, _("Invalid Name"), _("Tag name cannot be empty."))
            return

        new_color_str = edit_color[0]
        new_nsfw = nsfw_combo.currentData()

        # Stage the update
        if tag_id < 0:
            self._pending_creates = [
                (new_name, new_color_str, new_nsfw) if n == current_name else (n, c, o)
                for n, c, o in self._pending_creates
            ]
        else:
            self._pending_updates[tag_id] = (new_name, new_color_str, new_nsfw)

        # Update tree item
        item.setText(COL_NAME, new_name)
        item.tag_color = new_color_str
        item.nsfw_override = new_nsfw
        nsfw_label = _NSFW_LABELS.get(new_nsfw, "")
        item.setText(COL_NSFW, _(nsfw_label) if nsfw_label else "")
        self._tree.viewport().update()
        logger.debug(f"Staged tag edit {tag_id}: {new_name} ({new_color_str}, nsfw={new_nsfw})")

    def _on_delete_selected(self) -> None:
        """Delete all selected tags (with confirmation)."""
        items = self._tree.selectedItems()
        if not items:
            return

        tag_items = [i for i in items if isinstance(i, _TagTreeItem)]
        if not tag_items:
            return

        if len(tag_items) == 1:
            self._delete_tag_by_id(tag_items[0].tag_id)
            return

        # Multi-delete confirmation
        names = [i.text(COL_NAME) for i in tag_items]
        reply = QMessageBox.question(
            self,
            _("Delete Tags"),
            ngettext(
                "Delete {count} selected tag?\n\nThis will remove it from all games.",
                "Delete {count} selected tags?\n\nThis will remove them from all games.",
                len(names),
            ).format(count=len(names)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for item in tag_items:
            tag_id = item.tag_id
            name = item.text(COL_NAME)
            if tag_id < 0:
                self._pending_creates = [(n, c, o) for n, c, o in self._pending_creates if n != name]
            else:
                self._pending_deletes.add(tag_id)
                self._pending_updates.pop(tag_id, None)
            idx = self._tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)
            self._tag_items.pop(tag_id, None)
        self._update_score_summary()

    def _delete_tag_by_id(self, tag_id: int) -> None:
        """Stage a tag deletion (applied on save)"""
        item = self._tag_items.get(tag_id)
        if not item:
            return
        name = item.text(COL_NAME)

        reply = QMessageBox.question(
            self,
            _("Delete Tag"),
            _("Are you sure you want to delete the tag '{name}'?\n\n"
              "This will remove it from all games.").format(name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if tag_id < 0:
            self._pending_creates = [(n, c, o) for n, c, o in self._pending_creates if n != name]
        else:
            self._pending_deletes.add(tag_id)
            self._pending_updates.pop(tag_id, None)

        # Remove from tree
        idx = self._tree.indexOfTopLevelItem(item)
        if idx >= 0:
            self._tree.takeTopLevelItem(idx)
        self._tag_items.pop(tag_id, None)
        logger.debug(f"Staged tag deletion: {name}")

    def _apply_changes(self) -> None:
        """Apply all pending tag changes to database."""
        changed = False

        # 1. Process deletes first (avoid conflicts)
        for tag_id in self._pending_deletes:
            try:
                self.game_service.delete_tag(tag_id)
                changed = True
                logger.info(f"Deleted tag {tag_id}")
            except Exception as e:
                logger.error(f"Failed to delete tag {tag_id}: {e}")

        # 2. Process updates
        for tag_id, (name, color, nsfw) in self._pending_updates.items():
            try:
                self.game_service.update_tag(tag_id, name, color, nsfw_override=nsfw)
                changed = True
                logger.info(f"Updated tag {tag_id}: {name}")
            except Exception as e:
                logger.error(f"Failed to update tag {tag_id}: {e}")

        # 3. Process creates
        for name, color, nsfw in self._pending_creates:
            try:
                self.game_service.create_tag(name, color, nsfw_override=nsfw)
                changed = True
                logger.info(f"Created tag: {name}")
            except Exception as e:
                logger.error(f"Failed to create tag {name}: {e}")

        # 4. Process score changes (skip deleted tags)
        for tag_id, new_score in self._pending_scores.items():
            if tag_id in self._pending_deletes:
                continue
            try:
                self.game_service.set_tag_score(tag_id, new_score)
                changed = True
                logger.info(f"Set tag {tag_id} score to {new_score}")
            except Exception as e:
                logger.error(f"Failed to set tag {tag_id} score: {e}")

        # Clear pending changes
        self._pending_creates.clear()
        self._pending_updates.clear()
        self._pending_deletes.clear()
        self._pending_scores.clear()

        # Emit signal if anything changed
        if changed:
            self.tags_changed.emit()

    # -----------------------------------------------------------------
    # Score controls
    # -----------------------------------------------------------------

    _ICON_NAMES = [
        "author-remove.svg",
        "tag-edit.svg",
        "author-add.svg",
        "chevron-up.svg",
        "score-reset.svg",
        "chevron-down.svg",
    ]

    def _refresh_icons(self) -> None:
        """Load/reload SVG icons with current palette colors."""
        fallbacks = ["\u2212", "\u270e", "+", "\u25b2", "0", "\u25bc"]
        for btn, svg_name, fb in zip(self._icon_buttons, self._ICON_NAMES, fallbacks):
            icon = load_tinted_icon(svg_name)
            if icon.isNull():
                btn.setText(fb)
            else:
                btn.setIcon(icon)
                btn.setText("")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_icons()
        self._position_col_btn()
        self._update_score_summary()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_col_btn()

    def _on_score_up(self) -> None:
        mods = QApplication.keyboardModifiers()
        delta = 10 if mods & Qt.KeyboardModifier.ControlModifier else (5 if mods & Qt.KeyboardModifier.ShiftModifier else 1)
        self._adjust_selected_scores(delta)

    def _on_score_down(self) -> None:
        mods = QApplication.keyboardModifiers()
        delta = -10 if mods & Qt.KeyboardModifier.ControlModifier else (-5 if mods & Qt.KeyboardModifier.ShiftModifier else -1)
        self._adjust_selected_scores(delta)

    def _on_score_zero(self) -> None:
        for tag_id in self._selected_tag_ids():
            item = self._tag_items.get(tag_id)
            if item is None:
                continue
            self._pending_scores[tag_id] = 0
            item.setText(COL_SCORE, "0")
        self._update_score_summary()
        self._tree.viewport().update()

    def _adjust_selected_scores(self, delta: int) -> None:
        for tag_id in self._selected_tag_ids():
            item = self._tag_items.get(tag_id)
            if item is None:
                continue
            # Get current score (from pending or from displayed text)
            current = self._pending_scores.get(tag_id)
            if current is None:
                try:
                    txt = item.text(COL_SCORE)
                    current = int(txt) if txt else 0
                except ValueError:
                    current = 0
            new_score = max(-99, min(99, current + delta))
            self._pending_scores[tag_id] = new_score
            if new_score > 0:
                item.setText(COL_SCORE, f"+{new_score}")
            else:
                item.setText(COL_SCORE, str(new_score))
        self._update_score_summary()
        self._tree.viewport().update()

    def _update_score_summary(self) -> None:
        """Update summary label with preferred/neutral/blocked counts."""
        preferred = 0
        neutral = 0
        blocked = 0
        for item in self._tag_items.values():
            score = self._pending_scores.get(item.tag_id)
            if score is None:
                try:
                    txt = item.text(COL_SCORE)
                    score = int(txt) if txt else 0
                except ValueError:
                    score = 0
            if score > 0:
                preferred += 1
            elif score < 0:
                blocked += 1
            else:
                neutral += 1
        self._score_summary_label.setText(
            _("{preferred} preferred, {neutral} neutral, {blocked} blocked").format(
                preferred=preferred, neutral=neutral, blocked=blocked
            )
        )

    def _on_search_changed(self, text: str) -> None:
        """Filter tree items by search text."""
        search_lower = text.lower()
        for item in self._tag_items.values():
            item.setHidden(
                bool(text) and search_lower not in item.text(COL_NAME).lower()
            )

    # -----------------------------------------------------------------
    # Merge / Export / Import (file)
    # -----------------------------------------------------------------

    def _merge_tags(self) -> None:
        """Open merge dialog to combine two tags."""
        self._apply_changes()

        tags = self.game_service.get_all_tags()
        if len(tags) < 2:
            QMessageBox.information(self, _("Merge Tags"), _("Need at least two tags to merge."))
            return

        tag_names = [t["name"] for t in tags]
        tag_by_name = {t["name"]: t for t in tags}

        keep_name, ok = QInputDialog.getItem(
            self, _("Merge Tags"), _("Tag to keep:"), tag_names, 0, False
        )
        if not ok:
            return

        remaining = [n for n in tag_names if n != keep_name]
        absorb_name, ok = QInputDialog.getItem(
            self, _("Merge Tags"), _("Tag to merge into '{name}':").format(name=keep_name), remaining, 0, False
        )
        if not ok:
            return

        keep_tag = tag_by_name[keep_name]
        absorb_tag = tag_by_name[absorb_name]

        reply = QMessageBox.question(
            self, _("Confirm Merge"),
            _("Merge '{absorb}' into '{keep}'?\n\n"
              "All games tagged '{absorb}' will be retagged '{keep}'.\n"
              "'{absorb}' will be deleted.").format(absorb=absorb_name, keep=keep_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self.game_service.merge_tags(keep_tag["id"], absorb_tag["id"])
            logger.info(f"Merged tag '{absorb_name}' into '{keep_name}'")
            self._load_tags()
            self.tags_changed.emit()
        except Exception as e:
            QMessageBox.critical(self, _("Merge Failed"), str(e))
            logger.error(f"Failed to merge tags: {e}")

    def _export_tags(self) -> None:
        """Export all tags to a JSON file."""
        self._apply_changes()

        path, _filt = QFileDialog.getSaveFileName(
            self, _("Export Tags"), "luducat-tags.json", _("JSON Files (*.json)")
        )
        if not path:
            return

        try:
            data = self.game_service.export_tags()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            QMessageBox.information(
                self, _("Export Complete"),
                _("Exported {count} tags to {path}").format(count=len(data.get('tags', [])), path=path)
            )
            logger.info(f"Exported tags to {path}")
        except Exception as e:
            QMessageBox.critical(self, _("Export Failed"), str(e))
            logger.error(f"Failed to export tags: {e}")

    def _import_from_file(self) -> None:
        """Import tags from a JSON file (direct click on Import button)."""
        path, _filt = QFileDialog.getOpenFileName(
            self, _("Import Tags"), "", _("JSON Files (*.json)")
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            QMessageBox.critical(
                self, _("Import Failed"),
                _("The file contains invalid JSON (syntax error).\n\n"
                  "Line {line}, column {col}: {msg}\n\n"
                  "Open the file in a text editor and check that line "
                  "for missing commas, brackets, or quotes.").format(
                    line=e.lineno, col=e.colno, msg=e.msg)
            )
            logger.error(f"Failed to import tags (JSON syntax): {e}")
            return
        except OSError as e:
            QMessageBox.critical(
                self, _("Import Failed"), _("Could not read the file:\n{error}").format(error=e)
            )
            return

        # Validate structure before importing
        if not isinstance(data, list):
            QMessageBox.critical(
                self, _("Import Failed"),
                _("The file does not contain a tag list.\n\n"
                  "Expected a JSON array like:\n"
                  '[{"name": "My Tag", "color": DEFAULT_TAG_COLOR}, ...]')
            )
            return

        bad_entries = []
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                bad_entries.append(f"  Entry {i + 1}: expected an object, got {type(entry).__name__}")
            elif not entry.get("name", "").strip():
                bad_entries.append(f"  Entry {i + 1}: missing or empty \"name\" field")

        if bad_entries:
            details = "\n".join(bad_entries[:10])
            suffix = f"\n  ... and {len(bad_entries) - 10} more" if len(bad_entries) > 10 else ""
            QMessageBox.warning(
                self, _("Import Warning"),
                _("Some entries in the file have problems and will be skipped:\n\n"
                  "{details}{suffix}\n\n"
                  "Each entry needs at least a \"name\" field.\n"
                  "Continue importing the valid entries?").format(details=details, suffix=suffix)
            )

        try:
            result = self.game_service.import_tags(data)
            self._load_tags()
            self.tags_changed.emit()
            QMessageBox.information(
                self, _("Import Complete"),
                _("Created: {created}, Skipped: {skipped}").format(
                    created=result['created'], skipped=result['skipped'])
            )
            logger.info(f"Imported tags from {path}: {result}")
        except Exception as e:
            QMessageBox.critical(
                self, _("Import Failed"),
                _("An unexpected error occurred while importing:\n\n{error}").format(error=e)
            )
            logger.error(f"Failed to import tags: {e}")

    # -----------------------------------------------------------------
    # Import from plugins (dropdown menu)
    # -----------------------------------------------------------------

    def _build_import_menu(self) -> None:
        """Dynamically build the import dropdown menu."""
        self._import_menu.clear()

        # File import
        act_file = self._import_menu.addAction(_("Import from File..."))
        act_file.triggered.connect(self._import_from_file)

        # Discover available sources
        store_sources = self._get_store_tag_sources()
        metadata_sources = self._get_metadata_tag_sources()

        if store_sources or metadata_sources:
            # Import from all
            act_all = self._import_menu.addAction(_("Import from all supported sources..."))
            all_sources = list(store_sources.keys()) + list(metadata_sources.keys())
            act_all.triggered.connect(
                lambda checked=False, s=all_sources: self._import_from_plugins(s)
            )

        if store_sources:
            self._import_menu.addSeparator()
            for plugin_name, display_name in store_sources.items():
                act = self._import_menu.addAction(
                    _("Import from {source}...").format(source=display_name)
                )
                act.triggered.connect(
                    lambda checked=False, s=plugin_name: self._import_from_plugins([s])
                )

        if metadata_sources:
            self._import_menu.addSeparator()
            for plugin_name, display_name in metadata_sources.items():
                act = self._import_menu.addAction(
                    _("Import from {source}...").format(source=display_name)
                )
                act.triggered.connect(
                    lambda checked=False, s=plugin_name: self._import_from_plugins([s])
                )

    def _get_store_tag_sources(self) -> Dict[str, str]:
        """Get enabled store plugins that support tag sync."""
        from ...core.plugin_manager import PluginManager
        result = {}
        pm = self.game_service.plugin_manager
        for name, plugin in pm.get_store_plugins().items():
            if not pm.is_plugin_enabled(name):
                continue
            if not hasattr(plugin, "get_tag_sync_data"):
                continue
            display = PluginManager.get_store_display_name(name)
            # GOG special: show as "GOG / Galaxy"
            if name == "gog":
                display = "GOG / Galaxy"
            result[name] = display
        return result

    def _get_metadata_tag_sources(self) -> Dict[str, str]:
        """Get enabled metadata plugins that support tag sync."""
        from ...core.plugin_manager import PluginManager
        result = {}
        pm = self.game_service.plugin_manager
        tag_sync_names = PluginManager.get_tag_sync_plugin_names()
        metadata_plugins = pm.get_metadata_plugins()
        for name in tag_sync_names:
            if not pm.is_plugin_enabled(name):
                continue
            plugin = metadata_plugins.get(name)
            if not plugin:
                continue
            if not hasattr(plugin, "get_tag_sync_data"):
                continue
            try:
                if not plugin.is_available():
                    continue
            except Exception:
                continue
            display = PluginManager.get_store_display_name(name)
            result[name] = display
        return result

    def _import_from_plugins(self, sources: List[str]) -> None:
        """Import tags from the given plugin sources."""
        # Flush pending changes first
        self._apply_changes()

        from ...core.plugin_manager import PluginManager
        pm = self.game_service.plugin_manager
        config = self.game_service._config
        overrides = config.get("tags.plugin_overrides", {}) if config else {}

        store_plugins = pm.get_store_plugins()
        metadata_plugins = pm.get_metadata_plugins()
        tag_sync_names = set(PluginManager.get_tag_sync_plugin_names())

        results: List[str] = []
        errors: List[str] = []

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for source in sources:
                display_name = PluginManager.get_store_display_name(source)
                if source == "gog":
                    display_name = "GOG / Galaxy"
                plugin_override = overrides.get(source, {})

                # Determine sync mode
                sync_mode = plugin_override.get("sync_mode", "default")
                if sync_mode == "default":
                    sync_mode = config.get("tags.default_sync_mode", "add_only") if config else "add_only"

                try:
                    if source in store_plugins:
                        # Store plugin path
                        plugin = store_plugins[source]
                        tag_data = plugin.get_tag_sync_data()
                        if not tag_data:
                            results.append(
                                _("{source}: No data").format(source=display_name)
                            )
                            continue
                        tag_data["mode"] = sync_mode
                        stats = self.game_service._apply_tag_sync_data(source, tag_data)
                        results.append(self._format_sync_stats(display_name, stats))

                    elif source in tag_sync_names:
                        # Metadata plugin path
                        plugin = metadata_plugins.get(source)
                        if not plugin:
                            continue
                        kwargs = {}
                        if "import_favourites" in plugin_override:
                            kwargs["import_favourites"] = plugin_override["import_favourites"]
                        tag_data = plugin.get_tag_sync_data(**kwargs)
                        if not tag_data:
                            results.append(
                                _("{source}: No data").format(source=display_name)
                            )
                            continue
                        effective_mode = tag_data.get("mode", sync_mode)
                        stats = self.game_service._apply_metadata_tag_sync(
                            tag_data.get("source", source),
                            effective_mode,
                            tag_data.get("entries", []),
                            removals=tag_data.get("removals"),
                        )
                        results.append(self._format_sync_stats(display_name, stats))
                    else:
                        errors.append(
                            _("{source}: Plugin not available").format(source=display_name)
                        )
                except Exception as e:
                    errors.append(f"{display_name}: {e}")
                    logger.warning(f"Tag import failed for {source}: {e}")
        finally:
            QApplication.restoreOverrideCursor()

        # Reload tags and notify
        self._load_tags()
        self.tags_changed.emit()

        # Show results
        self._show_import_results(results, errors)

    def _format_sync_stats(self, display_name: str, stats: Optional[Dict[str, int]]) -> str:
        """Format sync stats into a human-readable line."""
        if not stats:
            return _("{source}: No changes").format(source=display_name)

        parts = []
        tags_added = stats.get("tags_added", 0)
        tags_removed = stats.get("tags_removed", 0)
        fav_set = stats.get("favorites_set", 0)
        fav_unset = stats.get("favorites_unset", 0)
        hidden_set = stats.get("hidden_set", 0)
        hidden_unset = stats.get("hidden_unset", 0)

        if tags_added:
            parts.append(
                ngettext("{n} tag added", "{n} tags added", tags_added).format(n=tags_added)
            )
        if tags_removed:
            parts.append(
                ngettext("{n} tag removed", "{n} tags removed", tags_removed).format(n=tags_removed)
            )
        if fav_set:
            parts.append(
                ngettext("{n} favourite set", "{n} favourites set", fav_set).format(n=fav_set)
            )
        if fav_unset:
            parts.append(
                ngettext("{n} favourite unset", "{n} favourites unset", fav_unset).format(n=fav_unset)
            )
        if hidden_set:
            parts.append(
                ngettext("{n} hidden set", "{n} hidden set", hidden_set).format(n=hidden_set)
            )
        if hidden_unset:
            parts.append(
                ngettext("{n} unhidden", "{n} unhidden", hidden_unset).format(n=hidden_unset)
            )

        if not parts:
            return _("{source}: No changes").format(source=display_name)
        return f"{display_name}: {', '.join(parts)}"

    def _show_import_results(self, results: List[str], errors: List[str]) -> None:
        """Show a summary of import results."""
        lines = results + errors
        if not lines:
            return

        text = "\n".join(lines)
        if errors:
            QMessageBox.warning(self, _("Import Results"), text)
        else:
            QMessageBox.information(self, _("Import Results"), text)
