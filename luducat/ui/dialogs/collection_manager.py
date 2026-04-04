# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# collection_manager.py

"""Collection Manager dialog for luducat.

Non-blocking (modeless) dialog for managing dynamic and static collections.
Supports: rename, reorder, hide, delete, color, notes, preview, convert to static.
Static collections use a dual-pane shuttle widget for game management.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Set

from luducat.core.json_compat import json
from PySide6.QtCore import (
    Qt,
    QModelIndex,
    QSize,
    QSortFilterProxyModel,
    QTimer,
    Signal,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QFileDialog,
    QListView,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .tag_editor import ColorButton
from .save_collection import SaveCollectionDialog
from ...core.constants import GAME_MODE_FILTERS
from ...core.plugin_manager import PluginManager
from ...utils.icons import load_tinted_icon

logger = logging.getLogger(__name__)

# Tree columns
COL_NAME = 0
COL_GAMES = 1

# QStandardItem data role for game_id
ROLE_GAME_ID = Qt.ItemDataRole.UserRole + 1


class CollectionManagerDialog(QDialog):
    """Non-blocking collection manager.

    Signals:
        collection_preview_requested: Emitted when user clicks Preview
        save_current_requested: Emitted when user clicks Save Current Filter
        collections_changed: Emitted when collections are modified (for dropdown refresh)
    """

    collection_preview_requested = Signal(dict)  # collection dict
    convert_to_static_requested = Signal(dict)  # dynamic collection dict
    save_current_requested = Signal()
    collections_changed = Signal()

    def __init__(
        self,
        game_service,
        get_game_title: Callable[[str], str],
        get_game_export_data: Callable[[str], Optional[Dict[str, Any]]],
        resolve_game_id: Callable[[Dict[str, Any]], Optional[str]],
        get_all_game_titles: Callable[[], List[tuple]],
        count_dynamic_matches: Callable[[str], int],
        parent=None,
    ):
        """
        Args:
            game_service: GameService instance for collection CRUD
            get_game_title: Callback to resolve game_id -> title string
            get_game_export_data: Callback (game_id) -> export dict
            resolve_game_id: Callback (export_entry) -> game_id or None
            get_all_game_titles: Callback () -> list of (game_id, title)
            count_dynamic_matches: Callback (filter_json) -> approx game count
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle(_("Collection Manager"))
        self.setMinimumSize(750, 600)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._game_service = game_service
        self._get_game_title = get_game_title
        self._get_game_export_data = get_game_export_data
        self._resolve_game_id = resolve_game_id
        self._get_all_game_titles = get_all_game_titles
        self._count_dynamic_matches = count_dynamic_matches
        self._collections: List[Dict[str, Any]] = []
        self._selected_id: Optional[int] = None

        # Cached title list: (game_id, title) sorted by title
        self._cached_titles: Optional[List[tuple]] = None

        # Debounce timer for notes DB writes
        self._notes_timer = QTimer(self)
        self._notes_timer.setSingleShot(True)
        self._notes_timer.setInterval(400)
        self._notes_timer.timeout.connect(self._commit_notes)

        self._setup_ui()
        self._load_collections()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        # ── Main area: left panel (tree + buttons) | right panel (content) ──
        main_split = QHBoxLayout()

        # ── Left panel: collection tree + action buttons ──
        left_panel = QVBoxLayout()

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels([_("Name"), _("Games")])
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.header().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self._tree.header().resizeSection(COL_GAMES, 50)
        self._tree.setFixedWidth(240)
        self._tree.currentItemChanged.connect(self._on_selection_changed)
        left_panel.addWidget(self._tree, stretch=1)

        # Tree action buttons (compact, stacked in pairs)
        tree_btns = QHBoxLayout()
        self._btn_delete = QPushButton(_("Delete"))
        self._btn_delete.setToolTip(_("Delete selected collection"))
        self._btn_delete.clicked.connect(self._delete_collection)
        tree_btns.addWidget(self._btn_delete)
        self._btn_convert = QPushButton(_("Convert"))
        self._btn_convert.setToolTip(_("Convert dynamic collection to static game list"))
        self._btn_convert.clicked.connect(self._convert_to_static)
        tree_btns.addWidget(self._btn_convert)
        left_panel.addLayout(tree_btns)

        tree_btns2 = QHBoxLayout()
        self._btn_export = QPushButton(_("Export..."))
        self._btn_export.setToolTip(_("Export selected collection to file"))
        self._btn_export.clicked.connect(self._export_collection)
        tree_btns2.addWidget(self._btn_export)
        self._btn_import = QPushButton(_("Import..."))
        self._btn_import.setToolTip(_("Import collection from file"))
        self._btn_import.clicked.connect(self._import_collection)
        tree_btns2.addWidget(self._btn_import)
        left_panel.addLayout(tree_btns2)

        tree_btns3 = QHBoxLayout()
        self._btn_preview = QPushButton(_("Preview"))
        self._btn_preview.setToolTip(_("Show this collection in the main window"))
        self._btn_preview.clicked.connect(self._preview_collection)
        tree_btns3.addWidget(self._btn_preview)
        self._btn_save_current = QPushButton(_("Save Filter"))
        self._btn_save_current.setToolTip(_("Save the main window's current filter as a new collection"))
        self._btn_save_current.clicked.connect(self.save_current_requested.emit)
        tree_btns3.addWidget(self._btn_save_current)
        left_panel.addLayout(tree_btns3)

        main_split.addLayout(left_panel)

        # ── Right panel: content area ──
        self._right_panel = QWidget()
        right_layout = QVBoxLayout(self._right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)

        # Collection header: "Collection: name" [pen icon]
        header_row = QHBoxLayout()
        self._collection_header = QLabel()
        self._collection_header.setStyleSheet("font-weight: bold;")
        header_row.addWidget(self._collection_header, stretch=1)

        self._btn_rename = QPushButton()
        self._btn_rename.setIcon(load_tinted_icon("tag-edit.svg", size=14))
        self._btn_rename.setIconSize(QSize(14, 14))
        self._btn_rename.setFixedSize(24, 24)
        self._btn_rename.setToolTip(_("Rename collection"))
        self._btn_rename.clicked.connect(self._rename_collection)
        header_row.addWidget(self._btn_rename)
        right_layout.addLayout(header_row)

        # Filter summary (dynamic collections — shown instead of shuttle)
        self._filter_summary = QLabel()
        self._filter_summary.setWordWrap(True)
        self._filter_summary.setObjectName("hintLabel")
        self._filter_summary.setTextFormat(Qt.TextFormat.RichText)
        self._filter_summary.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        right_layout.addWidget(self._filter_summary)

        # Shuttle widget (static collections — the big content area)
        self._shuttle_widget = QWidget()
        self._setup_shuttle()
        right_layout.addWidget(self._shuttle_widget, stretch=1)

        # Spacer that absorbs space when shuttle is hidden (dynamic view)
        self._dynamic_spacer = QWidget()
        right_layout.addWidget(self._dynamic_spacer, stretch=1)

        # Bottom strip: Notes + color + type
        bottom_strip = QHBoxLayout()
        bottom_strip.setAlignment(Qt.AlignmentFlag.AlignBottom)
        self._edit_notes = QPlainTextEdit()
        self._edit_notes.setFixedHeight(40)
        self._edit_notes.setPlaceholderText(_("Notes"))
        self._edit_notes.textChanged.connect(self._on_notes_changed)
        bottom_strip.addWidget(self._edit_notes, stretch=1)

        self._edit_color = ColorButton(
            "#5c7cfa", dialog_title=_("Choose Collection Color")
        )
        self._edit_color.setToolTip(_("Collection color"))
        self._edit_color.color_changed.connect(self._on_color_changed)
        bottom_strip.addWidget(self._edit_color, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._type_label = QLabel()
        self._type_label.setObjectName("hintLabel")
        bottom_strip.addWidget(self._type_label, alignment=Qt.AlignmentFlag.AlignBottom)

        right_layout.addLayout(bottom_strip)

        self._right_panel.setVisible(False)
        main_split.addWidget(self._right_panel, stretch=1)

        root.addLayout(main_split, stretch=1)

        # ── Bottom bar: close only ──
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_close = QPushButton(_("Close"))
        btn_close.setToolTip(_("Close collection manager"))
        btn_close.clicked.connect(self.close)
        bottom.addWidget(btn_close)
        root.addLayout(bottom)

    def _setup_shuttle(self) -> None:
        """Build the dual-pane shuttle widget for static collection game management.

        Layout: Collection (left) | buttons | Library (right)
        """
        shuttle_layout = QHBoxLayout(self._shuttle_widget)
        shuttle_layout.setContentsMargins(0, 0, 0, 0)

        # --- Left pane: Collection games ---
        left = QVBoxLayout()
        self._collection_label = QLabel()
        self._collection_label.setStyleSheet("font-weight: bold;")
        left.addWidget(self._collection_label)

        self._collection_search = QLineEdit()
        self._collection_search.setPlaceholderText(_("Search collection..."))
        self._collection_search.setClearButtonEnabled(True)
        left.addWidget(self._collection_search)

        self._collection_model = QStandardItemModel()
        self._collection_proxy = QSortFilterProxyModel()
        self._collection_proxy.setSourceModel(self._collection_model)
        self._collection_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._collection_search.textChanged.connect(self._collection_proxy.setFilterFixedString)

        self._collection_view = QListView()
        self._collection_view.setModel(self._collection_proxy)
        self._collection_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._collection_view.setAlternatingRowColors(True)
        self._collection_view.setUniformItemSizes(True)
        self._collection_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._collection_view.doubleClicked.connect(self._on_collection_double_clicked)
        left.addWidget(self._collection_view, stretch=1)

        shuttle_layout.addLayout(left, stretch=1)

        # --- Center: Up / >> / << / Down buttons ---
        center = QVBoxLayout()
        center.addStretch()

        self._btn_up = QPushButton("\u2191")  # ↑
        self._btn_up.setToolTip(_("Move collection up in list"))
        self._btn_up.setFixedWidth(40)
        self._btn_up.clicked.connect(self._move_up)
        center.addWidget(self._btn_up)

        self._btn_add = QPushButton("\u2190")  # ← (library → collection)
        self._btn_add.setToolTip(_("Add selected games to collection"))
        self._btn_add.setFixedWidth(40)
        self._btn_add.setEnabled(False)
        self._btn_add.clicked.connect(self._on_add_to_collection)
        center.addWidget(self._btn_add)

        self._btn_remove = QPushButton("\u2192")  # → (collection → library)
        self._btn_remove.setToolTip(_("Remove selected games from collection"))
        self._btn_remove.setFixedWidth(40)
        self._btn_remove.setEnabled(False)
        self._btn_remove.clicked.connect(self._on_remove_from_collection)
        center.addWidget(self._btn_remove)

        self._btn_down = QPushButton("\u2193")  # ↓
        self._btn_down.setToolTip(_("Move collection down in list"))
        self._btn_down.setFixedWidth(40)
        self._btn_down.clicked.connect(self._move_down)
        center.addWidget(self._btn_down)

        center.addStretch()
        shuttle_layout.addLayout(center)

        # --- Right pane: Library ---
        right = QVBoxLayout()
        self._library_label = QLabel()
        self._library_label.setStyleSheet("font-weight: bold;")
        right.addWidget(self._library_label)

        self._library_search = QLineEdit()
        self._library_search.setPlaceholderText(_("Search library..."))
        self._library_search.setClearButtonEnabled(True)
        right.addWidget(self._library_search)

        self._library_model = QStandardItemModel()
        self._library_proxy = QSortFilterProxyModel()
        self._library_proxy.setSourceModel(self._library_model)
        self._library_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._library_search.textChanged.connect(self._library_proxy.setFilterFixedString)

        self._library_view = QListView()
        self._library_view.setModel(self._library_proxy)
        self._library_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._library_view.setAlternatingRowColors(True)
        self._library_view.setUniformItemSizes(True)
        self._library_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._library_view.doubleClicked.connect(self._on_library_double_clicked)
        right.addWidget(self._library_view, stretch=1)

        shuttle_layout.addLayout(right, stretch=1)

    # -- Data loading ---------------------------------------------------

    def _load_collections(self) -> None:
        """Load collections from DB and populate tree."""
        self._collections = self._game_service.get_collections(include_hidden=True)
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        """Rebuild the tree widget from self._collections."""
        self._tree.blockSignals(True)
        self._tree.clear()
        for coll in self._collections:
            count_str = self._get_game_count_str(coll)
            item = QTreeWidgetItem([
                coll["name"],
                count_str,
            ])
            item.setData(COL_NAME, Qt.ItemDataRole.UserRole, coll["id"])
            item.setTextAlignment(COL_GAMES, Qt.AlignmentFlag.AlignCenter)
            self._tree.addTopLevelItem(item)

        self._tree.blockSignals(False)

        # Reselect previously selected and trigger selection handler
        reselected = False
        if self._selected_id is not None:
            for i in range(self._tree.topLevelItemCount()):
                item = self._tree.topLevelItem(i)
                if item.data(COL_NAME, Qt.ItemDataRole.UserRole) == self._selected_id:
                    self._tree.setCurrentItem(item)
                    reselected = True
                    break
        if reselected:
            self._on_selection_changed(self._tree.currentItem(), None)
        else:
            self._on_selection_changed(None, None)

    def _get_game_count_str(self, coll: dict) -> str:
        """Get game count as a display string. Returns '~' for complex dynamic filters."""
        if coll["type"] == "static":
            return str(self._game_service.get_collection_game_count(coll["id"]))
        filter_json = coll.get("filter_json") or ""
        count = self._count_dynamic_matches(filter_json)
        return "~" if count < 0 else str(count)

    def _get_collection_by_id(self, cid: int) -> Optional[dict]:
        for c in self._collections:
            if c["id"] == cid:
                return c
        return None

    def _ensure_cached_titles(self) -> List[tuple]:
        """Load and cache all (game_id, title) pairs, sorted by title."""
        if self._cached_titles is None:
            titles = self._get_all_game_titles()
            titles.sort(key=lambda t: t[1].lower())
            self._cached_titles = titles
        return self._cached_titles

    # -- Selection ------------------------------------------------------

    def _on_selection_changed(self, current, previous) -> None:
        if current is None:
            self._selected_id = None
            self._right_panel.setVisible(False)
            return

        cid = current.data(COL_NAME, Qt.ItemDataRole.UserRole)
        self._selected_id = cid
        coll = self._get_collection_by_id(cid)
        if coll is None:
            self._right_panel.setVisible(False)
            return

        self._right_panel.setVisible(True)

        # Header
        self._collection_header.setText(
            _("Collection: {name}").format(name=coll["name"])
        )

        # Notes
        self._edit_notes.blockSignals(True)
        self._edit_notes.setPlainText(coll.get("notes") or "")
        self._edit_notes.blockSignals(False)

        # Color
        if coll.get("color"):
            self._edit_color.set_color(coll["color"])

        # Type + date label
        type_str = _("Dynamic") if coll["type"] == "dynamic" else _("Static")
        created = coll.get("created_at", "")
        if created:
            if hasattr(created, "strftime"):
                created = created.strftime("%Y-%m-%d")
            else:
                created = str(created)[:10]
        self._type_label.setText(f"{type_str} — {created}")

        is_static = coll["type"] == "static"
        self._filter_summary.setVisible(not is_static)
        self._shuttle_widget.setVisible(is_static)
        self._dynamic_spacer.setVisible(not is_static)
        self._btn_convert.setEnabled(not is_static)

        if is_static:
            self._populate_shuttle(cid)
        else:
            self._show_filter_summary(coll)

    # -- Shuttle (dual-pane game management) ----------------------------

    def _populate_shuttle(self, collection_id: int) -> None:
        """Fill both shuttle panes from the library and collection data."""
        self._library_search.clear()
        self._collection_search.clear()

        collection_ids = self._game_service.get_collection_game_ids(collection_id)
        all_titles = self._ensure_cached_titles()

        # Build library model (all games NOT in collection)
        self._library_model.clear()
        for game_id, title in all_titles:
            if game_id not in collection_ids:
                item = QStandardItem(title)
                item.setData(game_id, ROLE_GAME_ID)
                item.setEditable(False)
                self._library_model.appendRow(item)

        # Build collection model (games IN collection, sorted by title)
        self._collection_model.clear()
        coll_titles = []
        for gid in collection_ids:
            title = self._get_game_title(gid)
            coll_titles.append((gid, title))
        coll_titles.sort(key=lambda t: t[1].lower())
        for game_id, title in coll_titles:
            item = QStandardItem(title)
            item.setData(game_id, ROLE_GAME_ID)
            item.setEditable(False)
            self._collection_model.appendRow(item)

        self._update_shuttle_counts()
        self._update_shuttle_buttons()

        # Connect selection changes for button state.
        # selectionModel() is recreated when setModel() is called, but we
        # reuse the same models, so the selection model is stable — just
        # connect once on first populate, skip on subsequent calls.
        if not hasattr(self, "_shuttle_connected"):
            self._library_view.selectionModel().selectionChanged.connect(
                self._update_shuttle_buttons
            )
            self._collection_view.selectionModel().selectionChanged.connect(
                self._update_shuttle_buttons
            )
            self._shuttle_connected = True

    def _update_shuttle_counts(self) -> None:
        """Update the header labels with current counts."""
        lib_count = self._library_model.rowCount()
        coll_count = self._collection_model.rowCount()
        self._library_label.setText(
            _("Library ({count})").format(count=lib_count)
        )
        # Collection label includes the collection name
        coll = self._get_collection_by_id(self._selected_id) if self._selected_id else None
        coll_name = coll["name"] if coll else _("Collection")
        self._collection_label.setText(
            f"{coll_name} ({coll_count})"
        )

    def _update_shuttle_buttons(self) -> None:
        """Enable/disable transfer buttons based on selection state."""
        has_lib_sel = bool(self._library_view.selectionModel().selectedIndexes())
        has_coll_sel = bool(self._collection_view.selectionModel().selectedIndexes())
        self._btn_add.setEnabled(has_lib_sel)
        self._btn_remove.setEnabled(has_coll_sel)

    def _on_add_to_collection(self) -> None:
        """Move selected games from library to collection."""
        if self._selected_id is None:
            return
        indexes = self._library_view.selectionModel().selectedIndexes()
        if not indexes:
            return

        source_rows = []
        game_ids = set()
        for proxy_idx in indexes:
            source_idx = self._library_proxy.mapToSource(proxy_idx)
            item = self._library_model.itemFromIndex(source_idx)
            if item:
                game_ids.add(item.data(ROLE_GAME_ID))
                source_rows.append(source_idx.row())

        if not game_ids:
            return

        self._game_service.add_games_to_collection(self._selected_id, game_ids)

        # Move items: remove from library (reverse order), add to collection sorted
        for row in sorted(source_rows, reverse=True):
            items = self._library_model.takeRow(row)
            if items:
                self._insert_sorted(self._collection_model, items[0])

        self._update_shuttle_counts()
        self._update_shuttle_buttons()
        self._update_tree_game_count()
        self.collections_changed.emit()

    def _on_remove_from_collection(self) -> None:
        """Move selected games from collection back to library."""
        if self._selected_id is None:
            return
        indexes = self._collection_view.selectionModel().selectedIndexes()
        if not indexes:
            return

        source_rows = []
        game_ids = set()
        for proxy_idx in indexes:
            source_idx = self._collection_proxy.mapToSource(proxy_idx)
            item = self._collection_model.itemFromIndex(source_idx)
            if item:
                game_ids.add(item.data(ROLE_GAME_ID))
                source_rows.append(source_idx.row())

        if not game_ids:
            return

        self._game_service.remove_games_from_collection(self._selected_id, game_ids)

        for row in sorted(source_rows, reverse=True):
            items = self._collection_model.takeRow(row)
            if items:
                self._insert_sorted(self._library_model, items[0])

        self._update_shuttle_counts()
        self._update_shuttle_buttons()
        self._update_tree_game_count()
        self.collections_changed.emit()

    def _on_library_double_clicked(self, proxy_index: QModelIndex) -> None:
        """Double-click in library: add game to collection."""
        if self._selected_id is None:
            return
        source_idx = self._library_proxy.mapToSource(proxy_index)
        item = self._library_model.itemFromIndex(source_idx)
        if not item:
            return
        game_id = item.data(ROLE_GAME_ID)
        self._game_service.add_games_to_collection(self._selected_id, {game_id})

        taken = self._library_model.takeRow(source_idx.row())
        if taken:
            self._insert_sorted(self._collection_model, taken[0])

        self._update_shuttle_counts()
        self._update_shuttle_buttons()
        self._update_tree_game_count()
        self.collections_changed.emit()

    def _on_collection_double_clicked(self, proxy_index: QModelIndex) -> None:
        """Double-click in collection: remove game from collection."""
        if self._selected_id is None:
            return
        source_idx = self._collection_proxy.mapToSource(proxy_index)
        item = self._collection_model.itemFromIndex(source_idx)
        if not item:
            return
        game_id = item.data(ROLE_GAME_ID)
        self._game_service.remove_games_from_collection(self._selected_id, {game_id})

        taken = self._collection_model.takeRow(source_idx.row())
        if taken:
            self._insert_sorted(self._library_model, taken[0])

        self._update_shuttle_counts()
        self._update_shuttle_buttons()
        self._update_tree_game_count()
        self.collections_changed.emit()

    @staticmethod
    def _insert_sorted(model: QStandardItemModel, item: QStandardItem) -> None:
        """Insert item into model maintaining alphabetical sort by display text."""
        text_lower = item.text().lower()
        lo, hi = 0, model.rowCount()
        while lo < hi:
            mid = (lo + hi) // 2
            if model.item(mid).text().lower() < text_lower:
                lo = mid + 1
            else:
                hi = mid
        model.insertRow(lo, item)

    def _update_tree_game_count(self) -> None:
        """Update the game count in the tree for the selected collection."""
        current = self._tree.currentItem()
        if current and self._selected_id is not None:
            count = self._collection_model.rowCount()
            current.setText(COL_GAMES, str(count))

    # -- Filter summary (dynamic) --------------------------------------

    def _show_filter_summary(self, coll: dict) -> None:
        """Show filter summary for a dynamic collection."""
        filter_json = coll.get("filter_json")
        if not filter_json:
            self._filter_summary.setText("<i>" + _("No filter data") + "</i>")
            return
        try:
            filters = json.loads(filter_json)
        except (json.JSONDecodeError, TypeError):
            self._filter_summary.setText("<i>" + _("Invalid filter data") + "</i>")
            return

        lines = []
        for key, value in filters.items():
            if key == "active_collection":
                continue
            if isinstance(value, list) and value:
                lines.append(f"<b>{key}</b>: {', '.join(str(v) for v in value)}")
            elif isinstance(value, bool) and value:
                lines.append(f"<b>{key}</b>")
            elif isinstance(value, str) and value and value != "all":
                lines.append(f"<b>{key}</b>: {value}")

        if not lines:
            self._filter_summary.setText("<i>" + _("All games (no filters)") + "</i>")
        else:
            self._filter_summary.setText("<br>".join(lines))

    # -- Edit handlers -------------------------------------------------

    def _rename_collection(self) -> None:
        """Open a rename dialog for the selected collection."""
        if self._selected_id is None:
            return
        coll = self._get_collection_by_id(self._selected_id)
        if coll is None:
            return
        new_name, ok = QInputDialog.getText(
            self, _("Rename Collection"),
            _("New name:"), QLineEdit.EchoMode.Normal, coll["name"],
        )
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()
        self._game_service.update_collection(self._selected_id, name=new_name)
        # Update tree + header
        current = self._tree.currentItem()
        if current:
            current.setText(COL_NAME, new_name)
        self._collection_header.setText(
            _("Collection: {name}").format(name=new_name)
        )
        # Update cached collection data
        for c in self._collections:
            if c["id"] == self._selected_id:
                c["name"] = new_name
                break
        self.collections_changed.emit()

    def _on_color_changed(self, color: str) -> None:
        if self._selected_id is not None:
            self._game_service.update_collection(self._selected_id, color=color)
            self.collections_changed.emit()

    def _on_notes_changed(self) -> None:
        if self._selected_id is not None:
            self._notes_timer.start()

    def _commit_notes(self) -> None:
        """Debounced: persist notes change to DB."""
        if self._selected_id is not None:
            notes = self._edit_notes.toPlainText().strip() or None
            self._game_service.update_collection(self._selected_id, notes=notes)

    # -- Actions -------------------------------------------------------

    def _move_up(self) -> None:
        idx = self._tree.indexOfTopLevelItem(self._tree.currentItem())
        if idx <= 0:
            return
        self._swap_positions(idx, idx - 1)

    def _move_down(self) -> None:
        idx = self._tree.indexOfTopLevelItem(self._tree.currentItem())
        if idx < 0 or idx >= self._tree.topLevelItemCount() - 1:
            return
        self._swap_positions(idx, idx + 1)

    def _swap_positions(self, a: int, b: int) -> None:
        id_order = [
            self._tree.topLevelItem(i).data(COL_NAME, Qt.ItemDataRole.UserRole)
            for i in range(self._tree.topLevelItemCount())
        ]
        id_order[a], id_order[b] = id_order[b], id_order[a]
        self._game_service.reorder_collections(id_order)
        self._load_collections()
        self.collections_changed.emit()

    def _delete_collection(self) -> None:
        if self._selected_id is None:
            return
        coll = self._get_collection_by_id(self._selected_id)
        if coll is None:
            return
        reply = QMessageBox.question(
            self,
            _("Delete Collection"),
            _("Delete collection \"{name}\"?").format(name=coll["name"]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._game_service.delete_collection(self._selected_id)
        self._selected_id = None
        self._load_collections()
        self._right_panel.setVisible(False)
        self.collections_changed.emit()

    def _convert_to_static(self) -> None:
        if self._selected_id is None:
            return
        coll = self._get_collection_by_id(self._selected_id)
        if coll is None or coll["type"] != "dynamic":
            return
        reply = QMessageBox.warning(
            self,
            _("Convert to Static"),
            _(
                "This will snapshot the current results as a fixed game list "
                "and discard the filter query. This cannot be undone.\n\n"
                "Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.convert_to_static_requested.emit(coll)

    def _preview_collection(self) -> None:
        if self._selected_id is None:
            return
        coll = self._get_collection_by_id(self._selected_id)
        if coll:
            self.collection_preview_requested.emit(coll)

    # -- Import / Export -----------------------------------------------

    def _export_collection(self) -> None:
        """Export selected collection to a .luducat-collection file."""
        if self._selected_id is None:
            return
        coll = self._get_collection_by_id(self._selected_id)
        if coll is None:
            return

        safe_name = coll["name"].replace(" ", "_").lower()[:40]
        default_name = f"{safe_name}.luducat-collection"
        path, _filt = QFileDialog.getSaveFileName(
            self, _("Export Collection"), default_name,
            _("Luducat Collection (*.luducat-collection)")
        )
        if not path:
            return

        try:
            data = self._game_service.export_collection(
                self._selected_id, self._get_game_export_data
            )
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            QMessageBox.information(
                self, _("Export Complete"),
                _("Exported collection \"{name}\" to {path}").format(
                    name=coll["name"], path=path
                ),
            )
        except Exception as e:
            QMessageBox.critical(self, _("Export Failed"), str(e))
            logger.error(f"Failed to export collection: {e}")

    def _import_collection(self) -> None:
        """Import collection from a .luducat-collection file."""
        path, _filt = QFileDialog.getOpenFileName(
            self, _("Import Collection"), "",
            _("Luducat Collection (*.luducat-collection);;JSON Files (*.json)")
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            QMessageBox.critical(
                self, _("Import Failed"),
                _("Invalid JSON in file.\nLine {line}: {msg}").format(
                    line=e.lineno, msg=e.msg
                ),
            )
            return
        except OSError as e:
            QMessageBox.critical(
                self, _("Import Failed"),
                _("Could not read file:\n{error}").format(error=e),
            )
            return

        if data.get("format") != "luducat-collection-v1":
            QMessageBox.critical(
                self, _("Import Failed"),
                _("Unrecognized file format. Expected luducat-collection-v1."),
            )
            return

        self._show_import_dialog(data)

    def _show_import_dialog(self, data: Dict[str, Any]) -> None:
        """Show import options and run import."""
        dlg = QDialog(self)
        dlg.setWindowTitle(_("Import Collection"))
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)

        form = QFormLayout()
        name_input = QLineEdit(data.get("name", ""))
        form.addRow(_("Name:"), name_input)
        layout.addLayout(form)

        mode_label = QLabel(_("Import mode:"))
        mode_label.setObjectName("hintLabel")
        layout.addWidget(mode_label)

        radio_new = QRadioButton(_("Create new collection"))
        radio_merge = QRadioButton(_("Merge into existing (add games)"))
        radio_overwrite = QRadioButton(_("Overwrite existing (replace contents)"))
        radio_new.setChecked(True)
        layout.addWidget(radio_new)
        layout.addWidget(radio_merge)
        layout.addWidget(radio_overwrite)

        coll_type = data.get("type", "static")
        if coll_type == "static" and data.get("games"):
            total = len(data["games"])
            summary = QLabel(
                _("Static collection with {count} games").format(count=total)
            )
        elif coll_type == "dynamic":
            summary = QLabel(_("Dynamic filter collection"))
        else:
            summary = QLabel(_("Empty collection"))
        summary.setObjectName("hintLabel")
        layout.addWidget(summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        if radio_merge.isChecked():
            mode = "merge"
        elif radio_overwrite.isChecked():
            mode = "overwrite"
        else:
            mode = "new"

        data["name"] = name_input.text().strip() or data.get("name", "Imported")

        stats = self._game_service.import_collection(
            data, mode, self._resolve_game_id
        )

        msg_parts = []
        if stats["matched"]:
            msg_parts.append(
                _("{matched} of {total} games matched").format(
                    matched=stats["matched"], total=stats["total"]
                )
            )
        if stats["unmatched"]:
            titles = stats.get("unmatched_titles", [])
            preview = "\n".join(f"  - {t}" for t in titles[:20])
            if len(titles) > 20:
                preview += f"\n  ... (+{len(titles) - 20})"
            msg_parts.append(
                _("{count} games could not be matched:\n{titles}").format(
                    count=stats["unmatched"], titles=preview
                )
            )
        if not msg_parts:
            msg_parts.append(_("Collection imported successfully."))

        QMessageBox.information(
            self, _("Import Complete"), "\n\n".join(msg_parts)
        )
        self._cached_titles = None
        self._load_collections()
        self.collections_changed.emit()

    # -- Public API ----------------------------------------------------

    def refresh(self) -> None:
        """Reload collections (called after external changes)."""
        self._load_collections()
