# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Adoption dialog -- index an existing installer archive in place.

Wraps the Phase E adoption scanner: dry-run scan with progress and
cancel, a report of what would be adopted, then an explicit commit.
Files are never moved or modified; the scan only reads, the commit only
writes manifest rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from luducat.ui.dialogs.download_audit_review import _format_size

logger = logging.getLogger(__name__)

try:
    _("")
    ngettext("", "", 1)
except NameError:
    def _(s): return s
    def ngettext(s, p, n): return s if n == 1 else p


class ArchiveAdoptionDialog(QDialog):
    """Scan the configured archive volume and adopt known files in place."""

    adopted = Signal(int)   # rows written to the manifest

    def __init__(self, config, engine, handler, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._engine = engine
        self._handler = handler
        self._worker = None
        self._scanner = None
        self._report = None

        self.setWindowTitle(_("Adopt existing archive"))
        # Wide enough for the full button row (the adopt button carries
        # a file count, longer still in German), tall enough that the
        # word-wrapped intro never gets squeezed into clipping.
        self.setMinimumSize(700, 480)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(_(
            "Indexes installers, patches and extras that are already in "
            "your archive folder so update checks and missing-file scans "
            "know about them. Files are only read, never moved, renamed "
            "or modified; their timestamps stay untouched. Checksums are "
            "not computed here -- adopted files can be verified later."))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        root_row = QHBoxLayout()
        root_row.addWidget(QLabel(_("Archive folder:")))
        self._root_label = QLabel(str(self._volume_root() or ""))
        self._root_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root_row.addWidget(self._root_label, 1)
        layout.addLayout(root_row)

        hint = QLabel(_(
            "The folder layout from the download settings decides which "
            "game each file belongs to."))
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._network_check = QCheckBox(
            _("Look up games missing from the local catalog on GOG.com"))
        self._network_check.setChecked(False)
        layout.addWidget(self._network_check)

        # Determinate 0/1 keeps the bar empty but its text visible --
        # Qt hides the format string entirely in indeterminate mode.
        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.hide()
        layout.addWidget(self._progress)

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.hide()
        layout.addWidget(self._summary)

        self._details = QPlainTextEdit()
        self._details.setReadOnly(True)
        # Paths read better unwrapped with a horizontal scrollbar, and a
        # minimum height keeps the pane from collapsing into a sliver.
        self._details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._details.setMinimumHeight(180)
        self._details.hide()
        layout.addWidget(self._details, 1)

        buttons = QHBoxLayout()
        self._btn_scan = QPushButton(_("Scan archive"))
        self._btn_scan.clicked.connect(self._start_scan)
        buttons.addWidget(self._btn_scan)

        self._btn_cancel_scan = QPushButton(_("Cancel scan"))
        self._btn_cancel_scan.clicked.connect(self._cancel_scan)
        self._btn_cancel_scan.hide()
        buttons.addWidget(self._btn_cancel_scan)

        self._btn_details = QPushButton(_("Show details"))
        self._btn_details.setCheckable(True)
        self._btn_details.toggled.connect(self._on_details_toggled)
        self._btn_details.hide()
        buttons.addWidget(self._btn_details)

        self._btn_save = QPushButton(_("Save report..."))
        self._btn_save.clicked.connect(self._save_report)
        self._btn_save.hide()
        buttons.addWidget(self._btn_save)

        buttons.addStretch(1)

        self._btn_adopt = QPushButton("")
        self._btn_adopt.clicked.connect(self._commit)
        self._btn_adopt.hide()
        buttons.addWidget(self._btn_adopt)

        self._btn_close = QPushButton(_("Close"))
        self._btn_close.clicked.connect(self.reject)
        buttons.addWidget(self._btn_close)
        layout.addLayout(buttons)

        if self._volume_root() is None:
            self._btn_scan.setEnabled(False)
            self._summary.setText(
                _("No archive folder is configured in the download "
                  "settings."))
            self._summary.show()

    def done(self, result: int) -> None:  # noqa: N802
        # The scan worker is parented to this dialog; destroying it
        # mid-run aborts the process. Cancel and wait -- the walk
        # checks cancellation per directory, so this returns fast.
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait()
            self._worker = None
        super().done(result)

    def _on_details_toggled(self, checked: bool) -> None:
        self._details.setVisible(checked)
        if checked:
            # Grow the window for the pane instead of squeezing the
            # labels above it into clipping.
            self.resize(max(self.width(), 780), max(self.height(), 640))

    def _volume_root(self):
        path = self._config.get("downloads.archive_path", "")
        if not path:
            return None
        root = Path(path)
        return root if root.is_dir() else None

    # -- scanning -----------------------------------------------------------

    def _start_scan(self) -> None:
        if self._worker is not None:
            return

        from luducat.ui.workers.adoption_worker import AdoptionWorker
        worker = AdoptionWorker(
            engine=self._engine, handler=self._handler, config=self._config,
            allow_network=self._network_check.isChecked(), parent=self)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_done)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker

        self._btn_scan.setEnabled(False)
        self._network_check.setEnabled(False)
        self._btn_adopt.hide()
        self._btn_save.hide()
        self._btn_details.hide()
        self._btn_details.setChecked(False)
        self._summary.hide()
        self._progress.setFormat(_("Preparing scan..."))
        self._progress.show()
        self._btn_cancel_scan.show()
        self._btn_cancel_scan.setEnabled(True)
        worker.start()

    def _cancel_scan(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._btn_cancel_scan.setEnabled(False)
            self._progress.setFormat(_("Cancelling..."))

    def _on_progress(self, done: int, total: int, label: str) -> None:
        if total > 0:
            if self._progress.maximum() != total:
                self._progress.setRange(0, total)
            self._progress.setValue(done)
        # done/total live in the text; QProgressBar's %v/%m placeholders
        # stay out of the format (a "%" in a folder name must pass
        # through untouched). Same convention as the audit row bar.
        text = _("Scanning {done}/{total}: {name}").format(
            done=done, total=total or "?", name=label)
        metrics = self._progress.fontMetrics()
        avail = max(200, self._progress.width() - 8)
        self._progress.setFormat(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, avail))

    def _on_completed(self, scanner, report) -> None:
        self._scanner = scanner
        self._report = report

        if report.cancelled:
            self._summary.setText(_("Scan cancelled. Nothing was changed."))
            self._summary.show()
            return

        games = {c.slug for c in report.candidates}
        total = sum(c.size_bytes for c in report.candidates)
        lines = [
            ngettext(
                "{files} file from {games} game can be adopted ({size}).",
                "{files} files from {games} games can be adopted ({size}).",
                len(report.candidates)).format(
                    files=len(report.candidates), games=len(games),
                    size=_format_size(total)),
        ]
        if report.skipped:
            lines.append(_("{n} files are already tracked.").format(
                n=len(report.skipped)))
        if report.unmatched:
            lines.append(_(
                "{n} files could not be attributed to a game and will be "
                "left alone.").format(n=len(report.unmatched)))
        if report.ignored:
            lines.append(_(
                "{n} sidecar files (artwork, changelogs, hidden folders) "
                "were ignored.").format(n=len(report.ignored)))
        if report.errors:
            lines.append(_("{n} files could not be read.").format(
                n=len(report.errors)))
        self._summary.setText(" ".join(lines))
        self._summary.show()

        self._details.setPlainText(self._report_text())
        self._btn_details.show()
        self._btn_save.show()
        if report.candidates:
            self._btn_adopt.setText(
                ngettext("Adopt {n} file", "Adopt {n} files",
                         len(report.candidates)).format(
                             n=len(report.candidates)))
            self._btn_adopt.show()

    def _on_failed(self, message: str) -> None:
        QMessageBox.warning(self, _("Adopt existing archive"), message)

    def _on_done(self) -> None:
        self._worker = None
        self._progress.hide()
        self._btn_cancel_scan.hide()
        self._btn_scan.setEnabled(True)
        self._network_check.setEnabled(True)

    # -- report -------------------------------------------------------------

    def _report_text(self) -> str:
        if self._report is None:
            return ""
        sections = []
        if self._report.unmatched:
            sections.append(_("Unmatched files:"))
            sections.extend(sorted(self._report.unmatched))
            sections.append("")
        if self._report.errors:
            sections.append(_("Read errors:"))
            sections.extend(sorted(self._report.errors))
            sections.append("")
        if self._report.ignored:
            sections.append(_("Ignored sidecar files:"))
            sections.extend(sorted(self._report.ignored))
        return "\n".join(sections) or _("Nothing to report.")

    def _save_report(self) -> None:
        path, __ = QFileDialog.getSaveFileName(
            self, _("Save adoption report"), "adoption-report.txt",
            _("Text files (*.txt)"))
        if not path:
            return
        try:
            Path(path).write_text(self._report_text(), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(
                self, _("Adopt existing archive"),
                _("Could not save the report: {error}").format(error=exc))

    # -- commit -------------------------------------------------------------

    def _commit(self) -> None:
        if self._scanner is None or self._report is None:
            return
        self._btn_adopt.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            written = self._scanner.commit(self._report)
        except Exception as exc:
            logger.exception("Adoption commit failed")
            QMessageBox.warning(
                self, _("Adopt existing archive"),
                _("Adoption failed: {error}").format(error=exc))
            self._btn_adopt.setEnabled(True)
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._summary.setText(
            ngettext("Adopted {n} file into the archive manifest.",
                     "Adopted {n} files into the archive manifest.",
                     written).format(n=written))
        self._btn_adopt.hide()
        self.adopted.emit(written)
