# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# author_dialog.py

"""SteamGridDB Author Score Dialog

Single-table dialog for managing author scores.
Authors get a numeric score (-99 to +99) that modifies asset selection:
- Negative score → author's assets excluded entirely ("blocked")
- Positive score → added to asset's community score as boost ("preferred")
- Zero → in the list but no effect ("neutral")
"""

from luducat.plugins.sdk.json import json
import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QRunnable, QSize, Qt, QThreadPool
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from luducat.plugins.sdk.ui import load_tinted_icon
from luducat.plugins.sdk.ui import open_url

logger = logging.getLogger(__name__)

# Column indices
COL_USERNAME = 0
COL_GRIDS = 1
COL_HEROES = 2
COL_LOGOS = 3
COL_ICONS = 4
COL_SCORE = 5
COL_HITS = 6
COL_STATUS = 7
COL_MENU = 8  # Dummy column for the column-visibility button

# Numeric sort columns (includes all asset type columns + score + hits)
_NUMERIC_COLS = {COL_GRIDS, COL_HEROES, COL_LOGOS, COL_ICONS, COL_SCORE, COL_HITS}


def _score_to_status(score: int) -> str:
    """Return human-readable status label for a score value."""
    if score > 0:
        return _("Preferred")
    elif score < 0:
        return _("Blocked")
    return _("Neutral")


def _lerp_color(base: QColor, target: QColor, t: float) -> QColor:
    """Linear interpolation between two colors."""
    return QColor(
        int(base.red() + (target.red() - base.red()) * t),
        int(base.green() + (target.green() - base.green()) * t),
        int(base.blue() + (target.blue() - base.blue()) * t),
        int(base.alpha() + (target.alpha() - base.alpha()) * t),
    )


class _ScoreItem(QTreeWidgetItem):
    """QTreeWidgetItem with numeric sort for asset type/score/hits columns."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column in _NUMERIC_COLS:
            try:
                a = self.text(column)
                b = other.text(column)
                a_val = int(a) if a != "\u2014" else -1
                b_val = int(b) if b != "\u2014" else -1
                return a_val < b_val
            except ValueError:
                pass
        return self.text(column).lower() < other.text(column).lower()


class _ScoreColorDelegate(QStyledItemDelegate):
    """Delegate that tints row background green/red based on score.

    Uses paint() override instead of initStyleOption() so the tinted
    background is painted before QSS draws text (QSS overrides
    backgroundBrush set in initStyleOption).
    """

    # Defaults — overridden by update_score_colors() from theme manager
    GREEN_TARGET = QColor(40, 180, 60)
    RED_TARGET = QColor(200, 50, 50)
    # Minimum visible blend even at score +/-1, scaling up to full at +/-50
    MIN_BLEND = 0.15
    MAX_BLEND = 0.5

    def update_score_colors(self, positive: str, negative: str) -> None:
        """Update score tint colors from current theme."""
        self.GREEN_TARGET = QColor(positive)
        self.RED_TARGET = QColor(negative)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        # Don't tint selected rows — let super handle selection highlight
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

        super().paint(painter, option, index)


class _AuthorVerifyRunnable(QRunnable):
    """Background: resolve vanity URL + verify SteamGridDB profile."""

    def __init__(self, username: str, lookup_fn, item: _ScoreItem, http_client=None):
        super().__init__()
        self._username = username
        self._lookup_fn = lookup_fn
        self._item = item
        self._http_client = http_client
        self.setAutoDelete(True)

    def run(self):
        # Check online status before making network calls
        from luducat.plugins.sdk.network import is_online
        if not is_online():
            return  # Skip verification when offline

        try:
            steam_id = self._lookup_fn(self._username)
            if not steam_id:
                self._item.setText(COL_STATUS, _("Not Found"))
                return

            self._item.setData(COL_USERNAME, Qt.ItemDataRole.UserRole, steam_id)

            # Verify SteamGridDB profile exists
            try:
                if not self._http_client:
                    return  # No HTTP client — skip verification
                resp = self._http_client.head(
                    f"https://www.steamgriddb.com/profile/{steam_id}",
                    timeout=5,
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    self._item.setText(COL_STATUS, _("Verified"))
                else:
                    self._item.setText(COL_STATUS, _("No SGDB Profile"))
            except Exception:
                pass  # Network error — leave status as-is
        except Exception as e:
            logger.debug(f"Author verify failed for '{self._username}': {e}")


class AuthorScoreDialog(QDialog):
    """Dialog for managing SteamGridDB author scores."""

    def __init__(
        self,
        author_data: Dict[str, dict],
        asset_counts: Dict[str, Dict[str, int]],
        steam_id_lookup: Optional[Callable[[str], Optional[str]]] = None,
        asset_count_refresh: Optional[Callable[..., Dict[str, Dict[str, int]]]] = None,
        parent=None,
        score_colors: Optional[Dict[str, str]] = None,
        http_client=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(_("SteamGridDB Author Rules"))
        self.setMinimumWidth(780)
        self.setMinimumHeight(450)

        self._initial_scores = {
            name: entry.get("score", 0) for name, entry in author_data.items()
        }
        self._asset_counts = asset_counts
        self._steam_id_lookup = steam_id_lookup
        self._asset_count_refresh = asset_count_refresh
        self._http_client = http_client
        self._icon_buttons: list[QPushButton] = []
        self._hidden_columns: set[int] = set()
        self._score_colors = score_colors

        self._setup_ui(author_data)

    def _setup_ui(self, author_data: Dict[str, dict]) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Tree widget
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([
            _("Username"), _("Grids"), _("Heroes"), _("Logos"), _("Icons"),
            _("Score"), _("Hits"), _("Status"), "",
        ])
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(COL_USERNAME, Qt.SortOrder.AscendingOrder)
        self._tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._tree.setRootIsDecorated(False)
        score_delegate = _ScoreColorDelegate(self._tree)
        if self._score_colors:
            score_delegate.update_score_colors(
                self._score_colors.get("positive", "#28b43c"),
                self._score_colors.get("negative", "#c83232"),
            )
        self._tree.setItemDelegate(score_delegate)

        # Context menu
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        # Header: Username stretches, others fixed
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_USERNAME, QHeaderView.ResizeMode.Stretch)
        for col in (COL_GRIDS, COL_HEROES, COL_LOGOS, COL_ICONS):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._tree.setColumnWidth(col, 60)
        header.setSectionResizeMode(COL_SCORE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(COL_HITS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_SCORE, 60)
        self._tree.setColumnWidth(COL_STATUS, 80)

        # Center-align numeric column headers
        for col in (COL_GRIDS, COL_HEROES, COL_LOGOS, COL_ICONS,
                    COL_SCORE, COL_HITS, COL_STATUS):
            self._tree.headerItem().setTextAlignment(
                col, Qt.AlignmentFlag.AlignCenter
            )

        # Dummy column for the column-visibility button (Thunderbird-style)
        _BTN_COL_W = 24
        header.setSectionResizeMode(COL_MENU, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(COL_MENU, _BTN_COL_W)

        # Column visibility button positioned over the dummy column header
        self._col_menu = QMenu(header)
        self._col_btn = QToolButton(header)
        self._col_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._col_btn.setMenu(self._col_menu)
        self._col_btn.setAutoRaise(True)
        self._col_btn.setToolTip(_("Column visibility"))
        self._col_btn.setText("\u2630")
        header.geometriesChanged.connect(self._position_col_btn)
        self._build_column_menu()

        # Populate
        for username, entry in author_data.items():
            score = entry.get("score", 0)
            steam_id = entry.get("steam_id", "")
            hits = entry.get("hits", 0)
            self._add_item(username, score, steam_id=steam_id, hits=hits)

        layout.addWidget(self._tree)

        # Controls row: [-][+] [input] [▲][0][▼]
        controls = QHBoxLayout()
        controls.setSpacing(6)

        btn_remove = QPushButton()
        btn_remove.setToolTip(_("Remove selected authors"))
        btn_remove.setIconSize(QSize(16, 16))
        btn_remove.clicked.connect(self._on_remove)
        self._icon_buttons.append(btn_remove)
        controls.addWidget(btn_remove)

        btn_add = QPushButton()
        btn_add.setToolTip(_("Add author with default score +10"))
        btn_add.setIconSize(QSize(16, 16))
        btn_add.clicked.connect(self._on_add)
        self._icon_buttons.append(btn_add)
        controls.addWidget(btn_add)

        self._input = QLineEdit()
        self._input.setPlaceholderText(_("Username..."))
        self._input.returnPressed.connect(self._on_add)
        controls.addWidget(self._input, 1)

        btn_up = QPushButton()
        btn_up.setToolTip(_("Increase score (+1, Shift +5, Ctrl +10)"))
        btn_up.setIconSize(QSize(16, 16))
        btn_up.clicked.connect(self._on_score_up)
        self._icon_buttons.append(btn_up)
        controls.addWidget(btn_up)

        btn_zero = QPushButton()
        btn_zero.setToolTip(_("Reset score to 0"))
        btn_zero.setIconSize(QSize(16, 16))
        btn_zero.clicked.connect(self._on_score_zero)
        self._icon_buttons.append(btn_zero)
        controls.addWidget(btn_zero)

        btn_down = QPushButton()
        btn_down.setToolTip(_("Decrease score (\u22121, Shift \u22125, Ctrl \u221210)"))
        btn_down.setIconSize(QSize(16, 16))
        btn_down.clicked.connect(self._on_score_down)
        self._icon_buttons.append(btn_down)
        controls.addWidget(btn_down)

        layout.addLayout(controls)

        # Summary line
        self._summary_label = QLabel()
        self._summary_label.setObjectName("hintLabel")
        layout.addWidget(self._summary_label)
        self._update_summary()

        # Info labels
        info = QLabel(_("Rules are case-insensitive"))
        info.setObjectName("hintLabel")
        layout.addWidget(info)

        warn = QLabel(_("Any change clears cached SteamGridDB images"))
        warn.setObjectName("hintLabel")
        layout.addWidget(warn)

        # Bottom buttons
        bottom = QHBoxLayout()

        btn_reset = QPushButton(_("Reset"))
        btn_reset.setToolTip(_("Clear all entries"))
        btn_reset.clicked.connect(self._on_reset)
        bottom.addWidget(btn_reset)

        btn_reset_hits = QPushButton(_("Reset Hits"))
        btn_reset_hits.setToolTip(_("Reset all hit counters to zero"))
        btn_reset_hits.clicked.connect(self._on_reset_hits)
        bottom.addWidget(btn_reset_hits)

        btn_save = QPushButton(_("Save"))
        btn_save.setToolTip(_("Export author scores to JSON file"))
        btn_save.clicked.connect(self._on_save)
        bottom.addWidget(btn_save)

        btn_load = QPushButton(_("Load"))
        btn_load.setToolTip(_("Import author scores from JSON file"))
        btn_load.clicked.connect(self._on_load)
        bottom.addWidget(btn_load)

        btn_refresh = QPushButton(_("Refresh"))
        btn_refresh.setToolTip(_("Refresh asset counts from SteamGridDB"))
        btn_refresh.clicked.connect(self._on_refresh_all)
        btn_refresh.setEnabled(self._asset_count_refresh is not None)
        bottom.addWidget(btn_refresh)

        bottom.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        bottom.addWidget(button_box)

        layout.addLayout(bottom)

    # -----------------------------------------------------------------
    # Icon helpers
    # -----------------------------------------------------------------

    _ICON_NAMES = [
        "author-remove.svg",
        "author-add.svg",
        "chevron-up.svg",
        "score-reset.svg",
        "chevron-down.svg",
    ]

    def _refresh_icons(self) -> None:
        """Load/reload SVG icons with current palette colors."""
        for btn, svg_name in zip(self._icon_buttons, self._ICON_NAMES):
            icon = load_tinted_icon(svg_name)
            if icon.isNull():
                # Fallback text if SVG missing
                fallback = ["\u2212", "+", "\u25b2", "0", "\u25bc"]
                idx = self._ICON_NAMES.index(svg_name)
                btn.setText(fallback[idx])
            else:
                btn.setIcon(icon)
                btn.setText("")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_icons()
        self._position_col_btn()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_col_btn()

    # -----------------------------------------------------------------
    # Column visibility
    # -----------------------------------------------------------------

    def _position_col_btn(self) -> None:
        """Position the column-visibility button over the dummy column header."""
        header = self._tree.header()
        h = header.height()
        # Align button exactly over the dummy column's header section
        x = header.sectionPosition(COL_MENU)
        w = header.sectionSize(COL_MENU)
        self._col_btn.setFixedSize(w, h)
        self._col_btn.move(x, 0)
        self._col_btn.raise_()

    def _build_column_menu(self) -> None:
        """Build the column visibility dropdown menu."""
        self._col_menu.clear()
        labels = [
            _("Username"), _("Grids"), _("Heroes"), _("Logos"), _("Icons"),
            _("Score"), _("Hits"), _("Status"),
        ]
        # Username and Score are always visible (disabled toggles)
        always_visible = {COL_USERNAME, COL_SCORE}

        for col, label in enumerate(labels):
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
    # Refresh asset counts
    # -----------------------------------------------------------------

    def _on_refresh_all(self) -> None:
        """Refresh asset counts for all authors."""
        self._do_refresh(None)

    def _on_refresh_selected(self) -> None:
        """Refresh asset counts for selected authors only."""
        selected = self._tree.selectedItems()
        if not selected:
            return
        usernames = [item.text(COL_USERNAME).lower() for item in selected]
        self._do_refresh(usernames)

    def _do_refresh(self, usernames: Optional[List[str]]) -> None:
        """Refresh asset counts and update tree rows."""
        if not self._asset_count_refresh:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            new_counts = self._asset_count_refresh(
                usernames, current_authors=self.get_author_data()
            )
            # Merge into our local cache
            if usernames is None:
                self._asset_counts = new_counts
            else:
                self._asset_counts.update(new_counts)

            # Update visible rows
            for i in range(self._tree.topLevelItemCount()):
                item = self._tree.topLevelItem(i)
                name_lower = item.text(COL_USERNAME).lower()
                if usernames is not None and name_lower not in usernames:
                    continue
                counts = self._asset_counts.get(name_lower, {})
                for col, key in (
                    (COL_GRIDS, "grid"),
                    (COL_HEROES, "hero"),
                    (COL_LOGOS, "logo"),
                    (COL_ICONS, "icon"),
                ):
                    val = counts.get(key)
                    if val is not None:
                        item.setText(col, str(val))
                    elif self._asset_counts:
                        item.setText(col, "0")
                    else:
                        item.setText(col, "\u2014")

            if not new_counts:
                self._summary_label.setText(
                    _("No SteamGridDB stats available \u2014 authors need Steam IDs to fetch stats")
                )
            else:
                label = _("all") if usernames is None else _("{count} selected").format(count=len(usernames))
                self._summary_label.setText(_("Asset counts refreshed ({label})").format(label=label))
        except Exception as e:
            logger.warning(f"Failed to refresh asset counts: {e}")
            self._summary_label.setText(_("Refresh failed: {error}").format(error=e))
        finally:
            QApplication.restoreOverrideCursor()

    # -----------------------------------------------------------------
    # Item helpers
    # -----------------------------------------------------------------

    def _add_item(
        self,
        username: str,
        score: int,
        steam_id: str = "",
        hits: int = 0,
    ) -> _ScoreItem:
        """Add a row to the tree and return it."""
        item = _ScoreItem()
        item.setText(COL_USERNAME, username)

        # Per-type asset counts from DB (read-only)
        counts = self._asset_counts.get(username.lower(), {})
        for col, key in (
            (COL_GRIDS, "grid"),
            (COL_HEROES, "hero"),
            (COL_LOGOS, "logo"),
            (COL_ICONS, "icon"),
        ):
            val = counts.get(key)
            if val is not None:
                item.setText(col, str(val))
            elif self._asset_counts:
                # Query succeeded but author has no cached assets
                item.setText(col, "0")
            else:
                # No query has run yet
                item.setText(col, "\u2014")
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)

        item.setText(COL_SCORE, str(score))
        item.setTextAlignment(COL_SCORE, Qt.AlignmentFlag.AlignCenter)

        item.setText(COL_HITS, str(hits))
        item.setTextAlignment(COL_HITS, Qt.AlignmentFlag.AlignCenter)

        item.setText(COL_STATUS, _score_to_status(score))
        item.setTextAlignment(COL_STATUS, Qt.AlignmentFlag.AlignCenter)

        # Store steam_id as UserRole data on username column
        item.setData(COL_USERNAME, Qt.ItemDataRole.UserRole, steam_id or "")

        self._tree.addTopLevelItem(item)
        return item

    def _update_item_status(self, item: _ScoreItem) -> None:
        """Update the Status column text from the current score."""
        try:
            score = int(item.text(COL_SCORE))
        except ValueError:
            score = 0
        item.setText(COL_STATUS, _score_to_status(score))

    def _find_item(self, username_lower: str) -> Optional[_ScoreItem]:
        """Find item by case-insensitive username."""
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.text(COL_USERNAME).lower() == username_lower:
                return item
        return None

    # -----------------------------------------------------------------
    # Button handlers
    # -----------------------------------------------------------------

    def _on_add(self) -> None:
        name = self._input.text().strip()
        if not name:
            return

        if self._find_item(name.lower()):
            self._summary_label.setText(_("'{name}' already in the list").format(name=name))
            self._input.clear()
            return

        item = self._add_item(name, 10)
        self._input.clear()
        self._input.setFocus()

        # Select the new row
        self._tree.clearSelection()
        item.setSelected(True)
        self._tree.scrollToItem(item)
        self._update_summary()

        # Background vanity URL + SteamGridDB profile verification
        if self._steam_id_lookup:
            item.setText(COL_STATUS, _("Verifying..."))
            runnable = _AuthorVerifyRunnable(
                name, self._steam_id_lookup, item,
                http_client=self._http_client,
            )
            QThreadPool.globalInstance().start(runnable)

    def _on_remove(self) -> None:
        selected = self._tree.selectedItems()
        if not selected:
            return

        # Build confirmation message
        names = [item.text(COL_USERNAME) for item in selected]
        count = len(names)
        if count <= 5:
            name_list = "\n".join(f"- {n}" for n in names)
        else:
            name_list = "\n".join(f"- {n}" for n in names[:5])
            name_list += "\n" + _("...and {count} more").format(count=count - 5)

        reply = QMessageBox.question(
            self,
            _("Remove Authors"),
            _("Remove {count} selected author(s)?").format(count=count) + f"\n\n{name_list}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for item in selected:
            idx = self._tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self._tree.takeTopLevelItem(idx)
        self._update_summary()

    def _on_score_up(self) -> None:
        mods = QApplication_keyboardModifiers()
        delta = 10 if mods & Qt.KeyboardModifier.ControlModifier else (5 if mods & Qt.KeyboardModifier.ShiftModifier else 1)
        self._adjust_score(delta)

    def _on_score_down(self) -> None:
        mods = QApplication_keyboardModifiers()
        delta = -10 if mods & Qt.KeyboardModifier.ControlModifier else (-5 if mods & Qt.KeyboardModifier.ShiftModifier else -1)
        self._adjust_score(delta)

    def _on_score_zero(self) -> None:
        for item in self._tree.selectedItems():
            item.setText(COL_SCORE, "0")
            self._update_item_status(item)
        self._update_summary()
        self._tree.viewport().update()

    def _adjust_score(self, delta: int) -> None:
        for item in self._tree.selectedItems():
            try:
                current = int(item.text(COL_SCORE))
            except ValueError:
                current = 0
            new_score = max(-99, min(99, current + delta))
            item.setText(COL_SCORE, str(new_score))
            self._update_item_status(item)
        self._update_summary()
        self._tree.viewport().update()

    def _on_reset(self) -> None:
        if self._tree.topLevelItemCount() == 0:
            return
        reply = QMessageBox.question(
            self,
            _("Reset"),
            _("Remove all author entries?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._tree.clear()
            self._update_summary()

    def _on_reset_hits(self) -> None:
        count = self._tree.topLevelItemCount()
        if count == 0:
            return
        total_hits = 0
        for i in range(count):
            try:
                total_hits += int(self._tree.topLevelItem(i).text(COL_HITS))
            except ValueError:
                pass
        if total_hits == 0:
            self._summary_label.setText(_("All hit counters are already zero"))
            return
        reply = QMessageBox.question(
            self,
            _("Reset Hits"),
            _("Reset all hit counters to zero? ({total} hits across {count} authors)").format(
                total=total_hits, count=count
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for i in range(count):
                self._tree.topLevelItem(i).setText(COL_HITS, "0")
            self._tree.viewport().update()
            self._summary_label.setText(_("All hit counters reset to zero"))

    def _on_save(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            _("Save Author Scores"),
            "steamgriddb-authors.json",
            _("JSON Files (*.json);;All Files (*)"),
        )
        if not path:
            return

        include_hits = QMessageBox.question(
            self,
            _("Save Author Scores"),
            _("Include hit counters in the export?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes

        author_data = self.get_author_data()
        if not include_hits:
            author_data = {
                k: {kk: vv for kk, vv in v.items() if kk != "hits"}
                for k, v in author_data.items()
            }

        data = {"authors": author_data}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._summary_label.setText(_("Saved to {path}").format(path=path))
        except OSError as e:
            QMessageBox.critical(self, _("Save Error"), _("Failed to save: {error}").format(error=e))

    def _on_load(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Load Author Scores"),
            "",
            _("JSON Files (*.json);;All Files (*)"),
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, _("Load Error"), _("Failed to load: {error}").format(error=e))
            return

        if not isinstance(data, dict) or "authors" not in data:
            QMessageBox.critical(
                self, _("Load Error"), _("Invalid format: expected {\"authors\": {...}}")
            )
            return

        raw_authors = data["authors"]
        if not isinstance(raw_authors, dict):
            QMessageBox.critical(
                self, _("Load Error"), _("Invalid format: 'authors' must be a dict")
            )
            return

        # Validate entries — support both old flat and new dict-value format
        valid = {}
        skipped = 0
        file_has_hits = False
        for k, v in raw_authors.items():
            if not isinstance(k, str):
                skipped += 1
                continue
            if isinstance(v, dict):
                score = v.get("score", 0)
                if not isinstance(score, (int, float)):
                    skipped += 1
                    continue
                hits = int(v.get("hits", 0))
                if hits:
                    file_has_hits = True
                valid[k] = {
                    "score": max(-99, min(99, int(score))),
                    "steam_id": str(v.get("steam_id", "") or ""),
                    "hits": hits,
                }
            elif isinstance(v, (int, float)):
                valid[k] = {
                    "score": max(-99, min(99, int(v))),
                    "steam_id": "",
                    "hits": 0,
                }
            else:
                skipped += 1

        if skipped:
            logger.warning(f"Skipped {skipped} invalid entries from {path}")

        if not valid:
            QMessageBox.information(
                self, _("Load"), _("No valid entries found in file.")
            )
            return

        # Classify entries vs current list
        current_names = set()
        for i in range(self._tree.topLevelItemCount()):
            current_names.add(self._tree.topLevelItem(i).text(COL_USERNAME).lower())

        new_entries = {k: v for k, v in valid.items() if k.lower() not in current_names}
        overlap_entries = {k: v for k, v in valid.items() if k.lower() in current_names}

        import_preferred = sum(1 for v in valid.values() if v["score"] > 0)
        import_neutral = sum(1 for v in valid.values() if v["score"] == 0)
        import_blocked = sum(1 for v in valid.values() if v["score"] < 0)

        # Build import preview dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(_("Import Author Scores"))
        dlg.setMinimumWidth(420)
        dlg_layout = QVBoxLayout(dlg)

        # File summary
        summary = QLabel(
            _("File: {count} entries ({preferred} preferred, "
              "{neutral} neutral, {blocked} blocked)").format(
                count=len(valid), preferred=import_preferred,
                neutral=import_neutral, blocked=import_blocked
            )
        )
        summary.setWordWrap(True)
        dlg_layout.addWidget(summary)

        if current_names:
            overlap_info = QLabel(
                _("{new} new, {overlap} already in list").format(
                    new=len(new_entries), overlap=len(overlap_entries)
                )
            )
            overlap_info.setObjectName("hintLabel")
            dlg_layout.addWidget(overlap_info)

        # Preview tree (read-only)
        preview = QTreeWidget()
        preview.setHeaderLabels([
            _("Author"), _("Score"), _("Status"), _("Hits"),
        ])
        preview.setRootIsDecorated(False)
        preview.setAlternatingRowColors(True)
        preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        preview.header().setStretchLastSection(False)
        preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            preview.header().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        for username, entry in sorted(valid.items(), key=lambda x: x[0].lower()):
            item = QTreeWidgetItem()
            label = username
            if username.lower() in current_names:
                label += "  \u2190"  # ← arrow marks overlap
            item.setText(0, label)
            item.setText(1, str(entry["score"]))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
            item.setText(2, _score_to_status(entry["score"]))
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
            item.setText(3, str(entry["hits"]) if entry["hits"] else "\u2014")
            item.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)
            preview.addTopLevelItem(item)

        preview.setMaximumHeight(260)
        dlg_layout.addWidget(preview)

        # Import mode (only show if there are existing entries)
        radio_replace = None
        radio_merge = None
        if current_names:
            mode_group = QGroupBox(_("Import mode"))
            mode_layout = QVBoxLayout(mode_group)
            radio_replace = QRadioButton(
                _("Replace — clear current list, load file entries only")
            )
            radio_merge = QRadioButton(
                _("Merge — add new entries, keep existing ones unchanged")
            )
            radio_merge.setChecked(True)
            mode_layout.addWidget(radio_replace)
            mode_layout.addWidget(radio_merge)
            dlg_layout.addWidget(mode_group)

        # Hits checkbox (only show if file contains hits data)
        cb_hits = None
        if file_has_hits:
            cb_hits = QCheckBox(_("Import hit counters from file"))
            cb_hits.setChecked(True)
            dlg_layout.addWidget(cb_hits)

        # Dialog buttons
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        dlg_layout.addWidget(btn_box)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        merge_mode = radio_merge is not None and radio_merge.isChecked()
        import_hits = cb_hits is not None and cb_hits.isChecked()

        if radio_replace is not None and radio_replace.isChecked():
            self._tree.clear()

        if not import_hits:
            for entry in valid.values():
                entry["hits"] = 0

        # Add/update entries
        added = 0
        for username, entry in valid.items():
            existing = self._find_item(username.lower())
            if existing:
                if merge_mode:
                    continue  # Merge: keep existing
            else:
                self._add_item(
                    username,
                    entry["score"],
                    steam_id=entry.get("steam_id", ""),
                    hits=entry.get("hits", 0),
                )
                added += 1

        self._update_summary()
        msg = _("Loaded {count} new entries").format(count=added)
        if skipped:
            msg += " " + _("({count} skipped)").format(count=skipped)
        self._summary_label.setText(msg)

    # -----------------------------------------------------------------
    # Context menu
    # -----------------------------------------------------------------

    def _on_context_menu(self, pos) -> None:
        """Show right-click context menu."""
        selected = self._tree.selectedItems()
        single = len(selected) == 1
        has_selection = len(selected) > 0

        menu = QMenu(self)

        # Add / Remove
        act_add = menu.addAction(_("Add Author..."))
        act_add.triggered.connect(lambda: self._input.setFocus())

        act_remove = menu.addAction(_("Remove Selected"))
        act_remove.setEnabled(has_selection)
        act_remove.triggered.connect(self._on_remove)

        menu.addSeparator()

        # Score adjustments
        act_up = menu.addAction(_("Score Up (+1)"))
        act_up.setEnabled(has_selection)
        act_up.triggered.connect(lambda: self._adjust_score(1))

        act_down = menu.addAction(_("Score Down (\u22121)"))
        act_down.setEnabled(has_selection)
        act_down.triggered.connect(lambda: self._adjust_score(-1))

        act_zero = menu.addAction(_("Reset to 0"))
        act_zero.setEnabled(has_selection)
        act_zero.triggered.connect(self._on_score_zero)

        menu.addSeparator()

        # Copy username
        act_copy = menu.addAction(_("Copy Username"))
        act_copy.setEnabled(single)
        if single:
            act_copy.triggered.connect(
                lambda: QApplication.clipboard().setText(
                    selected[0].text(COL_USERNAME)
                )
            )

        # Profile links
        steam_id = ""
        if single:
            steam_id = selected[0].data(COL_USERNAME, Qt.ItemDataRole.UserRole) or ""

        act_sgdb = menu.addAction(_("Open SteamGridDB Profile"))
        act_sgdb.setEnabled(single and bool(steam_id))
        if single and steam_id:
            act_sgdb.triggered.connect(
                lambda: open_url(
                    f"https://www.steamgriddb.com/profile/{steam_id}"
                )
            )

        act_steam = menu.addAction(_("Open Steam Profile"))
        act_steam.setEnabled(single and bool(steam_id))
        if single and steam_id:
            act_steam.triggered.connect(
                lambda: open_url(
                    f"https://steamcommunity.com/profiles/{steam_id}"
                )
            )

        # Refresh asset counts for selected
        if self._asset_count_refresh:
            menu.addSeparator()
            act_refresh = menu.addAction(_("Refresh Asset Counts"))
            act_refresh.setEnabled(has_selection)
            act_refresh.triggered.connect(self._on_refresh_selected)

        menu.exec(self._tree.viewport().mapToGlobal(pos))

    # -----------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------

    def _update_summary(self) -> None:
        preferred = 0
        neutral = 0
        blocked = 0
        for i in range(self._tree.topLevelItemCount()):
            try:
                score = int(self._tree.topLevelItem(i).text(COL_SCORE))
            except ValueError:
                score = 0
            if score > 0:
                preferred += 1
            elif score < 0:
                blocked += 1
            else:
                neutral += 1
        self._summary_label.setText(
            _("{preferred} preferred, {neutral} neutral, {blocked} blocked").format(
                preferred=preferred, neutral=neutral, blocked=blocked
            )
        )

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def get_author_data(self) -> Dict[str, dict]:
        """Return the current {username: {score, steam_id, hits}} dict."""
        result = {}
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            try:
                score = int(item.text(COL_SCORE))
            except ValueError:
                score = 0
            try:
                hits = int(item.text(COL_HITS))
            except ValueError:
                hits = 0
            steam_id = item.data(COL_USERNAME, Qt.ItemDataRole.UserRole) or ""
            result[item.text(COL_USERNAME)] = {
                "score": score,
                "steam_id": steam_id,
                "hits": hits,
            }
        return result

    def get_scores(self) -> Dict[str, int]:
        """Return the current {username: score} dict (legacy compat)."""
        return {
            name: entry["score"]
            for name, entry in self.get_author_data().items()
        }

    def data_changed(self) -> bool:
        """Check if scores changed from initial values."""
        current = {k.lower(): v for k, v in self.get_scores().items()}
        initial = {k.lower(): v for k, v in self._initial_scores.items()}
        return current != initial

    def scores_changed(self) -> bool:
        """Check if scores changed from initial values (legacy compat)."""
        return self.data_changed()


def QApplication_keyboardModifiers():
    """Get current keyboard modifiers (import-safe helper)."""
    from PySide6.QtWidgets import QApplication
    return QApplication.keyboardModifiers()
