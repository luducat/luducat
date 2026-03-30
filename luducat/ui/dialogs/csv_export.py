# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# csv_export.py

"""CSV Export dialog for luducat

Provides a two-mode export dialog:
- Standard mode: exports with saved defaults, one-click
- Advanced mode: field selection, column splitting, sort control

Respects current filter state and exports only visible games.
"""

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)

_COMPANY_PREFIX = re.compile(r'^Company:\s*', re.IGNORECASE)

# ── Field definitions ─────────────────────────────────────────────────

# (key, header_label_func, source, splittable)
# source: "list" = from _games_cache, "detail" = needs get_detail_fields()
# header_label_func deferred via N_() so translations resolve at dialog open time

_FIELD_DEFS: List[Tuple[str, str, str, bool]] = [
    ("title",            N_("Title"),           "list",   False),
    ("stores",           N_("Stores"),          "list",   True),
    ("release_date",     N_("Release Date"),    "list",   False),
    ("developers",       N_("Developers"),      "list",   True),
    ("publishers",       N_("Publishers"),      "list",   True),
    ("genres",           N_("Genres"),           "list",   True),
    ("themes",           N_("Themes"),           "list",   True),
    ("game_modes",       N_("Game Modes"),       "list",   True),
    ("tags",             N_("Tags"),             "list",   True),
    ("perspectives",     N_("Perspectives"),     "detail", True),
    ("is_favorite",      N_("Favorite"),         "list",   False),
    ("is_hidden",        N_("Hidden"),           "list",   False),
    ("is_installed",     N_("Installed"),        "list",   False),
    ("is_free",          N_("Free"),             "list",   False),
    ("is_family_shared", N_("Family Shared"),    "list",   False),
    ("achievements",     N_("Achievements"),     "detail", False),
    ("playtime_minutes", N_("Playtime (min)"),   "list",   False),
    ("launch_count",     N_("Launch Count"),     "list",   False),
    ("last_launched",    N_("Last Launched"),     "list",   True),
    ("added_at",         N_("Added"),             "list",   True),
    ("local_players",    N_("Local Players"),     "detail", False),
    ("online_players",   N_("Online Players"),    "detail", False),
    ("adult_confidence", N_("NSFW Score"),        "list",   False),
    ("protondb_rating",  N_("ProtonDB"),          "list",   False),
    ("steam_deck_compat", N_("Steam Deck"),       "list",   False),
    ("notes",            N_("Notes"),              "list",   False),
]

ALL_FIELD_KEYS = [f[0] for f in _FIELD_DEFS]
_FIELD_BY_KEY = {f[0]: f for f in _FIELD_DEFS}
_SPLITTABLE_KEYS = {f[0] for f in _FIELD_DEFS if f[3]}
_DETAIL_KEYS = {f[0] for f in _FIELD_DEFS if f[2] == "detail"}

# Date/time fields that split into date + time columns
_DATETIME_SPLIT_KEYS = {"last_launched", "added_at"}

# List fields that split into numbered columns
_LIST_SPLIT_KEYS = _SPLITTABLE_KEYS - _DATETIME_SPLIT_KEYS

# Default split: everything splittable EXCEPT date fields
_DEFAULT_SPLIT_FIELDS = _SPLITTABLE_KEYS - _DATETIME_SPLIT_KEYS

def _default_filename() -> str:
    """Generate timestamped default filename."""
    from datetime import date
    return f"luducat-export-{date.today().strftime('%Y%m%d')}.csv"


def _header_label(key: str) -> str:
    """Get translated header label for a field key."""
    fdef = _FIELD_BY_KEY.get(key)
    return _(fdef[1]) if fdef else key


def _summary_text(keys: list, max_shown: int = 3) -> str:
    """Build button text like 'Title, Stores, Developers, +17 more'."""
    if not keys:
        return _("(none)")
    labels = [_header_label(k) for k in keys[:max_shown]]
    rest = len(keys) - max_shown
    text = ", ".join(labels)
    if rest > 0:
        text += _(", +{count} more").format(count=rest)
    return text


class CsvExportDialog(QDialog):
    """Export dialog with standard/advanced modes."""

    def __init__(
        self,
        parent,
        filtered_games: List[Dict[str, Any]],
        game_service,
        config,
    ):
        super().__init__(parent)
        self._filtered_games = filtered_games
        self._game_service = game_service
        self._config = config

        self._selected_fields: List[str] = list(ALL_FIELD_KEYS)
        self._split_fields: Set[str] = set(_DEFAULT_SPLIT_FIELDS)
        self._sort_key: str = "title"

        # Load saved defaults
        self._load_defaults()

        self.setWindowTitle(_("Export Games to CSV"))
        self.setMinimumWidth(640)
        self._build_ui()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Hint banner — wizard subtitle style (bold text, no background)
        count = len(self._filtered_games)
        hint = QLabel(
            ngettext(
                "Current filters are applied to export ({count} game)",
                "Current filters are applied to export ({count} games)",
                count,
            ).format(count=count)
        )
        hint.setObjectName("hintLabel")
        font = hint.font()
        font.setBold(True)
        hint.setFont(font)
        outer.addWidget(hint)
        outer.addSpacing(4)

        # ── Grid layout for form rows ──────────────────────────────────
        grid = QGridLayout()
        grid.setColumnMinimumWidth(0, 100)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        row = 0

        # Export to: [file path] [Browse...]
        grid.addWidget(QLabel(_("Export to")), row, 0, Qt.AlignmentFlag.AlignLeft)
        file_row = QHBoxLayout()
        self._file_edit = QLineEdit()
        saved_folder = self._config.get("export.csv_output_folder", "")
        if not saved_folder:
            desktop = Path.home() / "Desktop"
            saved_folder = str(desktop) if desktop.exists() else str(Path.home())
        self._file_edit.setText(str(Path(saved_folder) / _default_filename()))
        self._file_edit.setToolTip(_("File path for the CSV export"))
        browse_btn = QPushButton(_("Browse..."))
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(self._file_edit, 1)
        file_row.addWidget(browse_btn)
        grid.addLayout(file_row, row, 1)
        row += 1

        outer.addLayout(grid)

        # Advanced settings checkbox + group
        self._adv_check = QCheckBox(_("Advanced Settings"))
        outer.addWidget(self._adv_check)

        self._adv_group = QGroupBox()
        self._adv_group.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        adv_grid = QGridLayout(self._adv_group)
        adv_grid.setColumnMinimumWidth(0, 120)
        adv_grid.setColumnStretch(1, 1)
        adv_grid.setHorizontalSpacing(10)
        adv_grid.setVerticalSpacing(6)
        adv_row = 0

        # Sort by [combo for any field]
        adv_grid.addWidget(
            QLabel(_("Sort by")), adv_row, 0, Qt.AlignmentFlag.AlignLeft
        )
        self._sort_combo = QComboBox()
        self._sort_combo.setToolTip(_("Order of rows in the exported file"))
        for key in ALL_FIELD_KEYS:
            self._sort_combo.addItem(_header_label(key), key)
        for i in range(self._sort_combo.count()):
            if self._sort_combo.itemData(i) == self._sort_key:
                self._sort_combo.setCurrentIndex(i)
                break
        adv_grid.addWidget(self._sort_combo, adv_row, 1)
        adv_row += 1

        # Fields [button]
        adv_grid.addWidget(
            QLabel(_("Fields")), adv_row, 0, Qt.AlignmentFlag.AlignLeft
        )
        self._fields_btn = QPushButton()
        self._fields_btn.setToolTip(
            _("Choose which columns appear in the CSV")
        )
        self._fields_btn.clicked.connect(self._open_fields_dialog)
        self._update_fields_btn_text()
        adv_grid.addWidget(
            self._fields_btn, adv_row, 1, Qt.AlignmentFlag.AlignLeft
        )
        adv_row += 1

        # Separate columns [button]
        adv_grid.addWidget(
            QLabel(_("Separate columns")), adv_row, 0,
            Qt.AlignmentFlag.AlignLeft,
        )
        self._split_btn = QPushButton()
        self._split_btn.setToolTip(
            _("Split multi-value fields (like Developers) into separate "
              "columns instead of semicolon-separated")
        )
        self._split_btn.clicked.connect(self._open_split_dialog)
        self._update_split_btn_text()
        adv_grid.addWidget(
            self._split_btn, adv_row, 1, Qt.AlignmentFlag.AlignLeft
        )

        self._adv_group.setVisible(False)
        outer.addWidget(self._adv_group)

        def _on_advanced_toggled(checked: bool) -> None:
            self._adv_group.setVisible(checked)
            # Only adjust height — keep width stable to avoid horizontal flip-flop
            QTimer.singleShot(
                0, lambda: self.resize(self.width(), self.sizeHint().height())
            )

        self._adv_check.toggled.connect(_on_advanced_toggled)

        # Delisted per-store export override
        self._delisted_check = QCheckBox(
            _("Export delisted games per store (no dedup)")
        )
        self._delisted_check.setToolTip(
            _("Override current selection and export all delisted games "
              "as raw per-store entries — one row per store, not deduplicated")
        )
        outer.addWidget(self._delisted_check)

        # ── Bottom button row ──────────────────────────────────────────
        btn_row = QHBoxLayout()

        reset_btn = QPushButton(_("Reset"))
        reset_btn.setToolTip(_("Revert to factory defaults"))
        reset_btn.clicked.connect(self._reset_defaults)

        save_btn = QPushButton(_("Save as Default"))
        save_btn.setToolTip(_("Remember these settings for next time"))
        save_btn.clicked.connect(self._save_defaults)

        export_btn = QPushButton(_("Export"))
        export_btn.setDefault(True)
        export_btn.clicked.connect(self._do_export)

        cancel_btn = QPushButton(_("Cancel"))
        cancel_btn.clicked.connect(self.reject)

        btn_row.addWidget(reset_btn)
        btn_row.addWidget(save_btn)
        btn_row.addStretch()
        btn_row.addWidget(export_btn)
        btn_row.addWidget(cancel_btn)
        outer.addLayout(btn_row)

    # ── Browse ─────────────────────────────────────────────────────────

    def _browse_file(self) -> None:
        current = self._file_edit.text()
        file_path, _filt = QFileDialog.getSaveFileName(
            self,
            _("Export as CSV"),
            current,
            _("CSV files (*.csv)"),
        )
        if file_path:
            self._file_edit.setText(file_path)

    # ── Field selection dialog ─────────────────────────────────────────

    def _open_fields_dialog(self) -> None:
        selected = set(self._selected_fields)
        result = self._search_checkbox_dialog(
            _("Fields in CSV"),
            [(_header_label(k), k) for k in ALL_FIELD_KEYS],
            selected,
        )
        if result is not None:
            # Preserve order from master list
            self._selected_fields = [k for k in ALL_FIELD_KEYS if k in result]
            # Remove split fields that are no longer selected
            self._split_fields &= set(self._selected_fields)
            self._update_fields_btn_text()
            self._update_split_btn_text()

    def _open_split_dialog(self) -> None:
        # Only show fields that are selected AND splittable
        available = [
            k for k in self._selected_fields
            if k in _SPLITTABLE_KEYS
        ]
        if not available:
            QMessageBox.information(
                self, _("Separate Columns"),
                _("No splittable fields are selected."),
            )
            return
        result = self._search_checkbox_dialog(
            _("Separate Columns"),
            [(_header_label(k), k) for k in available],
            self._split_fields,
        )
        if result is not None:
            self._split_fields = result
            self._update_split_btn_text()

    def _search_checkbox_dialog(
        self,
        title: str,
        items: List[Tuple[str, str]],
        active: Set[str],
    ) -> Optional[Set[str]]:
        """Show a search+checkbox dialog with a single list, sorted by name."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        search_input = QLineEdit()
        search_input.setPlaceholderText(_("Search..."))
        layout.addWidget(search_input)

        # Single list with checkboxes, sorted by display name
        source_model = QStandardItemModel()
        key_map: Dict[str, str] = {}
        sorted_items = sorted(items, key=lambda pair: pair[0].casefold())
        for display_text, key in sorted_items:
            item = QStandardItem(display_text)
            item.setCheckable(True)
            if key in active:
                item.setCheckState(Qt.CheckState.Checked)
            item.setEditable(False)
            source_model.appendRow(item)
            key_map[display_text] = key

        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(source_model)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        list_view = QListView()
        list_view.setModel(proxy)
        list_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(list_view)

        search_input.textChanged.connect(proxy.setFilterFixedString)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton(_("OK"))
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton(_("Cancel"))
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        dialog.setMinimumWidth(380)
        dialog.setMinimumHeight(460)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        result: Set[str] = set()
        for row_idx in range(source_model.rowCount()):
            src_item = source_model.item(row_idx)
            if src_item.checkState() == Qt.CheckState.Checked:
                key = key_map.get(src_item.text(), src_item.text())
                result.add(key)
        return result

    def _update_fields_btn_text(self) -> None:
        self._fields_btn.setText(_summary_text(self._selected_fields))

    def _update_split_btn_text(self) -> None:
        self._split_btn.setText(
            _summary_text(sorted(self._split_fields), max_shown=3)
        )

    # ── Config persistence ─────────────────────────────────────────────

    def _load_defaults(self) -> None:
        fields = self._config.get("export.csv_fields", None)
        if fields and isinstance(fields, list):
            valid = {f[0] for f in _FIELD_DEFS}
            self._selected_fields = [k for k in fields if k in valid]

        split = self._config.get("export.csv_split_fields", None)
        if split and isinstance(split, list):
            self._split_fields = set(split) & _SPLITTABLE_KEYS
        # else keep the default (_DEFAULT_SPLIT_FIELDS)

        sort_key = self._config.get("export.csv_sort", "title")
        valid_sorts = {f[0] for f in _FIELD_DEFS}
        if sort_key in valid_sorts:
            self._sort_key = sort_key

    def _save_defaults(self) -> None:
        self._sync_sort_from_combo()
        # Save folder (not full file path)
        file_path = Path(self._file_edit.text())
        self._config.set("export.csv_output_folder", str(file_path.parent))
        self._config.set("export.csv_fields", self._selected_fields)
        self._config.set("export.csv_split_fields", sorted(self._split_fields))
        self._config.set("export.csv_sort", self._sort_key)
        self._config.save()
        QMessageBox.information(
            self, _("Export Settings"),
            _("Settings saved as default."),
        )

    def _reset_defaults(self) -> None:
        self._selected_fields = list(ALL_FIELD_KEYS)
        self._split_fields = set(_DEFAULT_SPLIT_FIELDS)
        self._sort_key = "title"
        self._sort_combo.setCurrentIndex(0)
        self._update_fields_btn_text()
        self._update_split_btn_text()

    def _sync_sort_from_combo(self) -> None:
        self._sort_key = self._sort_combo.currentData() or "title"

    # ── Export execution ───────────────────────────────────────────────

    def _do_export(self) -> None:
        if self._delisted_check.isChecked():
            return self._do_export_delisted_raw()

        if not self._filtered_games:
            QMessageBox.information(
                self, _("Export"), _("No games to export.")
            )
            return

        self._sync_sort_from_combo()
        output_path = Path(self._file_edit.text()).expanduser()
        output_folder = output_path.parent

        # Directory health check
        from luducat.core.directory_health import check_directory
        health = check_directory(output_folder)
        if not health.writable:
            QMessageBox.critical(
                self, _("Export Error"),
                _("Output folder is not writable: {error}").format(
                    error=health.error or _("permission denied")
                ),
            )
            return

        # Estimate file size and warn if low space
        est_bytes = 500 * len(self._filtered_games) * len(self._selected_fields)
        est_mb = est_bytes // (1024 * 1024)
        if health.free_mb > 0 and est_mb > 0 and health.free_mb < est_mb * 2:
            reply = QMessageBox.warning(
                self, _("Low Disk Space"),
                _("Estimated export size is {est} MB but only {free} MB free. Continue?").format(
                    est=est_mb, free=health.free_mb
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Check if any DETAIL fields are needed
        needs_detail = bool(_DETAIL_KEYS & set(self._selected_fields))
        detail_data: Dict[str, Dict[str, Any]] = {}

        if needs_detail:
            detail_data = self._load_detail_fields()
            if detail_data is None:
                return

        try:
            self._write_csv(output_path, detail_data)
        except Exception as e:
            logger.error(f"CSV export failed: {e}")
            QMessageBox.critical(
                self, _("Export Error"),
                _("Failed to export CSV: {error}").format(error=e),
            )
            return

        # Post-export success dialog
        game_count = len(self._filtered_games)
        msg = QMessageBox(self)
        msg.setWindowTitle(_("Export Complete"))
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            _("Exported {count} games to {filename}").format(
                count=game_count, filename=output_path.name
            )
        )
        open_btn = msg.addButton(
            _("Open exported file"), QMessageBox.ButtonRole.AcceptRole
        )
        msg.addButton(_("Close"), QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_path)))

        self.accept()

    def _do_export_delisted_raw(self) -> None:
        """Export all delisted StoreGame entries per store, no dedup."""
        output_path = Path(self._file_edit.text()).expanduser()
        output_folder = output_path.parent

        from luducat.core.directory_health import check_directory
        health = check_directory(output_folder)
        if not health.writable:
            QMessageBox.critical(
                self, _("Export Error"),
                _("Output folder is not writable: {error}").format(
                    error=health.error or _("permission denied")
                ),
            )
            return

        try:
            from luducat.core.database import StoreGame
            session = self._game_service.database.get_session()
            delisted = (
                session.query(
                    StoreGame.store_name,
                    StoreGame.store_app_id,
                    StoreGame.metadata_json,
                )
                .filter(StoreGame.is_delisted == 1)
                .order_by(StoreGame.store_name, StoreGame.store_app_id)
                .all()
            )

            if not delisted:
                QMessageBox.information(
                    self, _("Export"), _("No delisted games found.")
                )
                return

            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([_("Store"), _("App ID"), _("Title")])
                for store_name, app_id, meta_json in delisted:
                    title = ""
                    if meta_json:
                        try:
                            meta = (
                                json.loads(meta_json)
                                if isinstance(meta_json, str) else meta_json
                            )
                            title = meta.get("title", "")
                        except Exception:
                            pass
                    writer.writerow([store_name, app_id, title])

            count = len(delisted)
        except Exception as e:
            logger.error("Delisted CSV export failed: %s", e)
            QMessageBox.critical(
                self, _("Export Error"),
                _("Failed to export CSV: {error}").format(error=e),
            )
            return

        msg = QMessageBox(self)
        msg.setWindowTitle(_("Export Complete"))
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText(
            _("Exported {count} delisted store entries to {filename}").format(
                count=count, filename=output_path.name
            )
        )
        open_btn = msg.addButton(
            _("Open exported file"), QMessageBox.ButtonRole.AcceptRole
        )
        msg.addButton(_("Close"), QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_path)))

        self.accept()

    def _load_detail_fields(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Load detail fields for all filtered games with progress dialog."""
        games = self._filtered_games
        total = len(games)

        progress = QProgressDialog(
            _("Loading detail fields..."), _("Cancel"), 0, total, self
        )
        progress.setWindowTitle(_("Export"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(500)

        detail_data: Dict[str, Dict[str, Any]] = {}

        for idx, game in enumerate(games):
            if progress.wasCanceled():
                return None
            progress.setValue(idx)
            QApplication.processEvents()

            game_id = game.get("id")
            if game_id:
                details = self._game_service.get_detail_fields(game_id)
                if details:
                    detail_data[game_id] = details

        progress.setValue(total)
        return detail_data

    def _write_csv(
        self,
        output_path: Path,
        detail_data: Dict[str, Dict[str, Any]],
    ) -> None:
        """Write the CSV file."""
        games = list(self._filtered_games)
        games = self._sort_games(games, detail_data)
        headers, row_builder = self._build_columns(games, detail_data)

        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for game in games:
                writer.writerow(row_builder(game))

    def _sort_games(
        self,
        games: List[Dict],
        detail_data: Dict[str, Dict[str, Any]],
    ) -> List[Dict]:
        """Sort games by the selected sort key.

        For list fields, entries are sorted alphabetically and the first
        entry is used as the sort key.
        """
        key = self._sort_key

        def sort_val(g):
            val = self._get_raw_value(g, key, detail_data)
            if val is None:
                return ""
            if key == "tags":
                names = sorted(self._extract_tag_names(val), key=str.casefold)
                return names[0].casefold() if names else ""
            if isinstance(val, list):
                items = sorted((str(v) for v in val if v), key=str.casefold)
                return items[0].casefold() if items else ""
            if isinstance(val, bool):
                return int(val)
            if isinstance(val, (int, float)):
                return val
            return str(val).casefold()

        reverse = key in ("playtime_minutes", "last_launched", "added_at", "release_date")
        return sorted(games, key=sort_val, reverse=reverse)

    def _build_columns(
        self,
        games: List[Dict],
        detail_data: Dict[str, Dict[str, Any]],
    ) -> Tuple[List[str], Any]:
        """Build header list and row-builder function."""
        split_fields = self._split_fields

        # Pre-compute max split counts for list-type split fields
        max_counts: Dict[str, int] = {}
        for key in split_fields:
            if key in _LIST_SPLIT_KEYS:
                max_count = 0
                for game in games:
                    items = self._get_list_items(game, key, detail_data)
                    max_count = max(max_count, len(items))
                max_counts[key] = max(max_count, 1)

        headers: List[str] = []
        column_specs: List[Tuple[str, str, int]] = []

        for key in self._selected_fields:
            label = _header_label(key)
            if key in split_fields:
                if key in _DATETIME_SPLIT_KEYS:
                    headers.append(label)
                    column_specs.append((key, "split_date", 0))
                    headers.append(label)
                    column_specs.append((key, "split_time", 0))
                elif key in _LIST_SPLIT_KEYS:
                    col_count = max_counts.get(key, 1)
                    for i in range(col_count):
                        headers.append(label)
                        column_specs.append((key, "split_list", i))
            else:
                headers.append(label)
                column_specs.append((key, "single", 0))

        def row_builder(game: Dict) -> List[str]:
            row = []
            for col_key, mode, idx in column_specs:
                if mode == "single":
                    row.append(self._format_field(game, col_key, detail_data))
                elif mode == "split_list":
                    items = self._get_list_items(game, col_key, detail_data)
                    row.append(items[idx] if idx < len(items) else "")
                elif mode == "split_date":
                    row.append(self._format_date_part(game, col_key))
                elif mode == "split_time":
                    row.append(self._format_time_part(game, col_key))
            return row

        return headers, row_builder

    # ── Field formatting ───────────────────────────────────────────────

    def _get_list_items(
        self,
        game: Dict,
        key: str,
        detail_data: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        """Get sorted list items for a field."""
        val = self._get_raw_value(game, key, detail_data)

        if key == "tags":
            items = self._extract_tag_names(val)
        elif isinstance(val, list):
            items = [str(v) for v in val if v]
        elif isinstance(val, str) and val:
            items = [val]
        else:
            return []

        if key in ("developers", "publishers"):
            items = [_COMPANY_PREFIX.sub('', s) for s in items if s]

        return sorted(items, key=str.casefold)

    def _format_field(
        self,
        game: Dict,
        key: str,
        detail_data: Dict[str, Dict[str, Any]],
    ) -> str:
        """Format a single field value for CSV output."""
        val = self._get_raw_value(game, key, detail_data)

        if key in ("is_favorite", "is_hidden", "is_installed", "is_free", "is_family_shared"):
            return _("Yes") if val else _("No")

        if key in ("playtime_minutes", "launch_count"):
            return str(val) if val else "0"

        if key == "adult_confidence":
            return f"{val:.2f}" if isinstance(val, (int, float)) else ""

        if key == "achievements":
            return _("Yes") if val else _("No")

        if key in ("local_players", "online_players"):
            return str(val) if val else ""

        if key == "release_date":
            return self._format_release_date(val)

        if key in ("last_launched", "added_at"):
            return self._format_date_part(game, key)

        if key == "tags":
            names = self._extract_tag_names(val)
            return "; ".join(sorted(names, key=str.casefold))

        if key == "stores":
            if isinstance(val, list):
                return "; ".join(str(s) for s in val if s)
            return str(val) if val else ""

        if isinstance(val, list):
            items = [str(v) for v in val if v]
            if key in ("developers", "publishers"):
                items = [_COMPANY_PREFIX.sub('', s) for s in items if s]
            return "; ".join(sorted(items, key=str.casefold))

        return str(val) if val else ""

    def _get_raw_value(
        self,
        game: Dict,
        key: str,
        detail_data: Dict[str, Dict[str, Any]],
    ) -> Any:
        """Get raw value from game dict or detail data."""
        game_id = game.get("id", "")
        details = detail_data.get(game_id, {}) if detail_data else {}

        if key == "local_players":
            gmd = details.get("game_modes_detail", {})
            local = gmd.get("local", {}) if isinstance(gmd, dict) else {}
            return local.get("players", "") if isinstance(local, dict) else ""

        if key == "online_players":
            gmd = details.get("game_modes_detail", {})
            online = gmd.get("online", {}) if isinstance(gmd, dict) else {}
            return online.get("players", "") if isinstance(online, dict) else ""

        if key == "perspectives":
            return details.get("perspectives", game.get("perspectives", []))

        if key == "achievements":
            return details.get("achievements", game.get("achievements"))

        return game.get(key)

    def _format_release_date(self, val) -> str:
        if val is None:
            return ""
        if isinstance(val, dict):
            dates = [v for v in val.values() if v]
            return str(min(dates)) if dates else ""
        if hasattr(val, "isoformat"):
            return val.isoformat()
        return str(val) if val else ""

    def _format_date_part(self, game: Dict, key: str) -> str:
        val = game.get(key)
        if not val:
            return ""
        s = str(val)
        if "T" in s:
            return s.split("T")[0]
        return s.split(" ")[0] if " " in s else s

    def _format_time_part(self, game: Dict, key: str) -> str:
        val = game.get(key)
        if not val:
            return ""
        s = str(val)
        if "T" in s:
            return s.split("T")[1].rstrip("Z")
        if " " in s:
            return s.split(" ", 1)[1]
        return ""

    @staticmethod
    def _extract_tag_names(val) -> List[str]:
        if not isinstance(val, list):
            return []
        names = []
        for t in val:
            if isinstance(t, dict):
                name = t.get("name", "")
            else:
                name = str(t)
            if name:
                names.append(name)
        return names
