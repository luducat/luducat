# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Review dialog for archive audit scan results.

One dialog serves both scans: mode="update" reviews games whose archived
installers are outdated, mode="missing" reviews owned games with nothing
archived yet. Games are top-level rows with tri-state checkboxes, their
files sit below; everything the auditor pre-selected starts checked.
"Enqueue selected" turns the checked files into lazy download targets
(url resolved at worker start, Phase B/C of the update-check plan).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from luducat.core.archivist.audit import (
    AuditCandidate,
    AuditFile,
    build_target,
    release_display,
)
from luducat.core.download_manager import get_download_manager
from luducat.core.theme_variables import DEFAULT_VALUES
from luducat.utils.icons import load_tinted_icon

logger = logging.getLogger(__name__)

try:
    _("")
    ngettext("", "", 1)
except NameError:
    def _(s): return s
    def ngettext(s, p, n): return s if n == 1 else p


_FILE_ROLE = Qt.ItemDataRole.UserRole
_CANDIDATE_ROLE = Qt.ItemDataRole.UserRole + 1

_MODES = ("update", "missing")

_COL_NAME, _COL_OLD, _COL_NEW, _COL_SIZE, _COL_LANG, _COL_OS = range(6)

# "Gerda: A Flame in Winter (Part 1 of 3)" -> "Gerda: A Flame in Winter (1/3)"
_PART_RE = re.compile(r"\(Part\s+(\d+)\s+of\s+(\d+)\)", re.IGNORECASE)

_OS_SHORT = {"windows": "Win", "linux": "Lin", "mac": "Mac"}


def _short_name(name: str) -> str:
    return _PART_RE.sub(r"(\1/\2)", name)


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024**4:
        return f"{size_bytes / 1024**4:.2f} TB"
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.0f} MB"
    if size_bytes > 0:
        return f"{size_bytes / 1024:.0f} KB"
    return "0 MB"


class DownloadAuditReviewDialog(QDialog):
    """Review and enqueue the results of an archive audit scan.

    Usage:
        dialog = DownloadAuditReviewDialog(candidates, mode="update",
                                           config=config, parent=self)
        dialog.exec()   # enqueues on accept
    """

    def __init__(
        self,
        candidates: list[AuditCandidate],
        mode: str,
        config=None,
        status_colors: Optional[dict] = None,
        settings_stale: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"invalid review mode: {mode!r}")
        super().__init__(parent)
        self._candidates = candidates
        self._mode = mode
        self._config = config
        self._settings_stale = bool(settings_stale)
        self._stale_label: Optional[QLabel] = None
        self._ok_button: Optional[QPushButton] = None
        self._size_label: Optional[QLabel] = None
        self._footer_dirty = False
        self._over_capacity = False
        #: set when the user asked for a fresh scan; the caller checks
        #: this after exec() and restarts the scan for this dialog's mode
        self.rescan_requested = False
        self._build_mode = bool(
            config.get("downloads.show_build_numbers", False)) if config \
            else False
        # Patch visibility follows the live setting, not the scan-time
        # payload, so toggling "Download patches" needs no rescan; with
        # the setting off patch rows are hidden entirely (the Settings
        # tooltip promises they are "not offered").
        self._check_patches = bool(
            config.get("downloads.download_patches", True)) if config \
            else True
        # State colors follow the theme's download status colors:
        # red (failed) = not archived, mid-blue (outdated) = outdated
        colors = status_colors or {}
        self._color_missing = QColor(
            colors.get("failed", DEFAULT_VALUES["download_failed"]))
        self._color_update = QColor(
            colors.get("outdated", DEFAULT_VALUES["download_outdated"]))
        self._setup_ui()

    # -- UI setup --

    def _setup_ui(self) -> None:
        if self._mode == "update":
            self.setWindowTitle(_("Archive updates"))
            intro = _("These archived GOG installers are outdated. "
                      "Checked files will be downloaded.")
        else:
            self.setWindowTitle(_("Not yet archived"))
            intro = _("These owned GOG games have nothing in the archive yet. "
                      "Checked files will be downloaded.")
        self.setObjectName("downloadAuditReviewDialog")
        self.setMinimumWidth(980)
        self.setMinimumHeight(540)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        intro_label = QLabel(intro)
        intro_label.setObjectName("hintLabel")
        intro_label.setWordWrap(True)
        top.addWidget(intro_label, 1)

        self._btn_rescan = QPushButton(_("Rescan"))
        self._btn_rescan.setIcon(load_tinted_icon("reload.svg", size=16))
        self._btn_rescan.setToolTip(
            _("Discard these results and run the scan again"))
        self._btn_rescan.clicked.connect(self._on_rescan)
        top.addWidget(self._btn_rescan, 0,
                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top)

        if self._settings_stale:
            # OS-excluded files never made it into the persisted payload,
            # so the results cannot be re-filtered here -- only flagged.
            self._stale_label = QLabel(_(
                "These results were scanned with different download "
                "settings. Rescan to apply the current ones."))
            self._stale_label.setObjectName("hintLabel")
            self._stale_label.setWordWrap(True)
            layout.addWidget(self._stale_label)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(6)
        self._tree.setHeaderLabels(
            [_("Name"), _("Old"), _("New"), _("Size"), _("Lang"), _("OS")])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._populate_tree()
        self._tree.expandAll()
        # Interactive everywhere: fixed modes (Stretch/ResizeToContents)
        # make the sections undraggable. Content sizing happens once,
        # then the user owns the widths.
        header = self._tree.header()
        header.setStretchLastSection(False)
        for col in range(self._tree.columnCount()):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive)
        for col in (_COL_OLD, _COL_NEW, _COL_SIZE, _COL_LANG, _COL_OS):
            self._tree.resizeColumnToContents(col)
        metrics = self._tree.fontMetrics()
        # room for the widest release identity either display mode shows
        version_width = metrics.horizontalAdvance("2.10.35.123 (123456)  ")
        for col in (_COL_OLD, _COL_NEW):
            self._tree.setColumnWidth(
                col, max(self._tree.columnWidth(col), version_width))
        self._tree.setColumnWidth(_COL_NAME, 380)
        self._tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self._tree, 1)

        # Quick selection toggles, same idea as the installer picker
        quick = QHBoxLayout()
        os_names = {"windows": _("Windows"), "linux": _("Linux"),
                    "mac": _("macOS")}
        platforms = sorted({
            f.platform for c in self._candidates for f in c.files
            if f.platform})
        for plat in platforms:
            btn = QPushButton(os_names.get(plat, plat.capitalize()))
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setToolTip(_("Check or uncheck all {os} files").format(
                os=os_names.get(plat, plat)))
            btn.clicked.connect(
                lambda checked, p=plat: self._toggle_platform(p, checked))
            quick.addWidget(btn)

        kind_names = {"installer": _("Installers"), "patch": _("Patches"),
                      "extra": _("Extras")}
        kinds = sorted({
            f.kind for c in self._candidates for f in c.files
            if f.kind != "patch" or self._check_patches})
        if len(kinds) > 1:
            for kind in ("installer", "patch", "extra"):
                if kind not in kinds:
                    continue
                btn = QPushButton(kind_names[kind])
                btn.setCheckable(True)
                btn.setChecked(True)
                btn.setToolTip(
                    _("Check or uncheck all files of this type"))
                btn.clicked.connect(
                    lambda checked, k=kind: self._toggle_kind(k, checked))
                quick.addWidget(btn)
        quick.addStretch()
        layout.addLayout(quick)

        footer = QHBoxLayout()
        self._size_label = QLabel("")
        self._size_label.setObjectName("hintLabel")
        footer.addWidget(self._size_label)
        footer.addStretch()

        btn_all = QPushButton(_("Select all"))
        btn_all.clicked.connect(lambda: self._set_all(True))
        footer.addWidget(btn_all)
        btn_none = QPushButton(_("Select none"))
        btn_none.clicked.connect(lambda: self._set_all(False))
        footer.addWidget(btn_none)

        self._ok_button = QPushButton(_("Enqueue selected"))
        self._ok_button.setDefault(True)
        self._ok_button.clicked.connect(self._on_accept)
        footer.addWidget(self._ok_button)
        btn_cancel = QPushButton(_("Cancel"))
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)
        layout.addLayout(footer)

        self._update_footer()

    def _populate_tree(self) -> None:
        kind_labels = {
            "installer": _("installer"),
            "patch": _("patch"),
            "extra": _("extra"),
        }
        for candidate in self._candidates:
            game_item = QTreeWidgetItem(self._tree)
            game_item.setText(_COL_NAME, candidate.game_title)
            game_item.setData(0, _CANDIDATE_ROLE, candidate)
            game_item.setFlags(
                game_item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsAutoTristate)

            outstanding = sum(
                1 for f in candidate.files if f.selected)
            if self._mode == "update":
                summary = ngettext(
                    "{n} file outdated", "{n} files outdated", outstanding)
            else:
                summary = ngettext(
                    "{n} file missing", "{n} files missing", outstanding)
            game_item.setToolTip(_COL_NAME, summary.format(n=outstanding))

            for f in candidate.files:
                if f.kind == "patch" and not self._check_patches:
                    # "Download patches" off means patches are not offered
                    # at all; the rows stay in the persisted payload so
                    # turning the setting on shows them without a rescan.
                    continue
                file_item = QTreeWidgetItem(game_item)
                file_item.setText(_COL_NAME, _short_name(f.name))
                tooltip_parts = [kind_labels.get(f.kind, f.kind)]
                if f.reason:
                    tooltip_parts.append(f.reason)
                file_item.setToolTip(_COL_NAME, " - ".join(tooltip_parts))
                # Build mode renders the cached scan-time build numbers
                # (ld-d8st); nothing is looked up while the dialog is
                # open. Old build 0 means nothing usable is archived.
                # Candidates persisted before the cache existed carry
                # None and fall back to the version-string displays.
                if f.kind == "installer":
                    if self._build_mode and candidate.local_build is not None:
                        old_display = (str(candidate.local_build)
                                       if candidate.local_build else "")
                    else:
                        old_display = release_display(
                            f.archived_version, self._build_mode)
                    file_item.setText(_COL_OLD, old_display or "-")
                if (f.kind == "installer" and self._build_mode
                        and candidate.online_build is not None):
                    new_display = str(candidate.online_build)
                else:
                    new_display = release_display(f.version, self._build_mode)
                file_item.setText(_COL_NEW, new_display)
                file_item.setText(
                    _COL_SIZE,
                    _format_size(f.size_bytes) if f.size_bytes else "")
                file_item.setText(_COL_LANG, (f.language or "").upper())
                file_item.setText(
                    _COL_OS, _OS_SHORT.get(f.platform, f.platform))
                file_item.setData(0, _FILE_ROLE, f)
                file_item.setFlags(
                    file_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = (self._check_patches if f.kind == "patch"
                           else f.selected)
                file_item.setCheckState(
                    0, Qt.CheckState.Checked if checked
                    else Qt.CheckState.Unchecked)
                self._apply_row_color(file_item)

    def _apply_row_color(self, item: QTreeWidgetItem) -> None:
        """State by color: red = not archived, mid-blue = outdated,
        dimmed = not selected."""
        f = item.data(0, _FILE_ROLE)
        if f is None:
            return
        if item.checkState(0) != Qt.CheckState.Checked:
            color = self.palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        elif f.kind == "installer" and f.archived_version:
            color = self._color_update
        elif f.kind == "installer":
            color = self._color_missing
        else:
            color = self.palette().color(QPalette.ColorRole.Text)
        brush = QBrush(color)
        was_blocked = self._tree.signalsBlocked()
        self._tree.blockSignals(True)
        try:
            for col in range(self._tree.columnCount()):
                item.setForeground(col, brush)
        finally:
            self._tree.blockSignals(was_blocked)

    # -- Selection state --

    def _file_items(self):
        for i in range(self._tree.topLevelItemCount()):
            game_item = self._tree.topLevelItem(i)
            for j in range(game_item.childCount()):
                yield game_item.child(j)

    def _selected_bytes(self) -> int:
        total = 0
        for item in self._file_items():
            if item.checkState(0) == Qt.CheckState.Checked:
                f = item.data(0, _FILE_ROLE)
                total += f.size_bytes or 0
        return total

    def _has_selection(self) -> bool:
        return any(item.checkState(0) == Qt.CheckState.Checked
                   for item in self._file_items())

    def _set_all(self, checked: bool) -> None:
        # Signals blocked: one footer update instead of one per row --
        # library-scale result sets made per-row updates crawl.
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        try:
            for item in self._file_items():
                item.setCheckState(0, state)
                self._apply_row_color(item)
        finally:
            self._tree.blockSignals(False)
        self._update_footer()

    def _toggle_platform(self, platform: str, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        try:
            for item in self._file_items():
                f = item.data(0, _FILE_ROLE)
                if f.platform == platform:
                    item.setCheckState(0, state)
                    self._apply_row_color(item)
        finally:
            self._tree.blockSignals(False)
        self._update_footer()

    def _toggle_kind(self, kind: str, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self._tree.blockSignals(True)
        try:
            for item in self._file_items():
                f = item.data(0, _FILE_ROLE)
                if f.kind == kind:
                    item.setCheckState(0, state)
                    self._apply_row_color(item)
        finally:
            self._tree.blockSignals(False)
        self._update_footer()

    def _on_item_changed(self, item, column) -> None:
        # One click cascades through tri-state parents; coalesce the
        # recompute (disk-usage probe + restyle) into a single deferred pass.
        # Row color follows the check state immediately (single item).
        if column == 0:
            self._apply_row_color(item)
        if self._footer_dirty:
            return
        self._footer_dirty = True
        QTimer.singleShot(0, self._flush_footer_update)

    def _flush_footer_update(self) -> None:
        self._footer_dirty = False
        self._update_footer()

    def _update_game_totals(self) -> None:
        """Per-game Size column shows the checked total for that game."""
        was_blocked = self._tree.signalsBlocked()
        self._tree.blockSignals(True)
        try:
            for i in range(self._tree.topLevelItemCount()):
                game_item = self._tree.topLevelItem(i)
                total = 0
                for j in range(game_item.childCount()):
                    child = game_item.child(j)
                    if child.checkState(0) == Qt.CheckState.Checked:
                        f = child.data(0, _FILE_ROLE)
                        total += f.size_bytes or 0
                game_item.setText(
                    _COL_SIZE, _format_size(total) if total else "")
        finally:
            self._tree.blockSignals(was_blocked)

    def _update_footer(self) -> None:
        self._update_game_totals()
        total = self._selected_bytes()
        text = _("Selected: {size}").format(size=_format_size(total))

        fits = True
        free_bytes = self._free_archive_bytes()
        if free_bytes is not None:
            text += "  -  " + _("{free} free").format(
                free=_format_size(free_bytes))
            fits = total <= free_bytes

        self._size_label.setText(text)
        if fits != (not self._over_capacity):
            # Restyle only on state flips -- setStyleSheet is not cheap
            self._over_capacity = not fits
            self._size_label.setStyleSheet(
                "color: red;" if self._over_capacity else "")
        self._ok_button.setEnabled(fits and self._has_selection())

    def _free_archive_bytes(self) -> Optional[int]:
        if not self._config:
            return None
        archive_path = self._config.get("downloads.archive_path", "")
        if not archive_path:
            return None
        path = Path(archive_path)
        if not path.exists():
            return None
        try:
            # shutil.disk_usage works on Windows too; os.statvfs does not
            # exist there and its AttributeError killed the constructor
            return shutil.disk_usage(path).free
        except OSError:
            return None

    # -- Enqueue --

    def _selected_by_game(self) -> list[tuple[AuditCandidate, list[AuditFile]]]:
        result = []
        for i in range(self._tree.topLevelItemCount()):
            game_item = self._tree.topLevelItem(i)
            candidate = game_item.data(0, _CANDIDATE_ROLE)
            picked = [
                game_item.child(j).data(0, _FILE_ROLE)
                for j in range(game_item.childCount())
                if game_item.child(j).checkState(0) == Qt.CheckState.Checked
            ]
            if picked:
                result.append((candidate, picked))
        return result

    def _enqueue_selected(self) -> int:
        """Submit one lazy DownloadTarget per game with checked files."""
        dm = get_download_manager()
        submitted = 0
        for candidate, picked in self._selected_by_game():
            target = build_target(candidate, picked)
            dm.submit(target)
            submitted += 1
        logger.info("Audit review enqueued %d game(s)", submitted)
        return submitted

    def _on_rescan(self) -> None:
        self.rescan_requested = True
        self.reject()

    def _on_accept(self) -> None:
        try:
            self._enqueue_selected()
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            logger.exception("Enqueue from audit review failed")
            QMessageBox.warning(self, _("Download Error"), str(e))
            return
        self.accept()
