# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# download_covers.py

"""Batch cover image downloader.

Pre-downloads all missing cover images to disk cache so the cover view
can scroll without network-induced lag.
"""

import logging
import threading
from typing import List, Tuple

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
)

from ..sync_widget import StripedProgressBar
from ...core.constants import APP_NAME
from ...utils.image_cache import get_cover_cache

logger = logging.getLogger(__name__)

try:
    from ...core.i18n import _, ngettext
except ImportError:
    def _(s): return s
    def ngettext(s, p, n): return s if n == 1 else p


class DownloadCoversWorker(QThread):
    """Background worker that downloads missing cover images to disk cache.

    Processes sequentially with per-domain rate limiting via ImageCache's
    public download_to_disk() API.
    """

    progress = Signal(int, str)  # current_download_index, game_name
    stats = Signal(int, int, int)  # downloaded, skipped, failed (live)
    finished_signal = Signal(int, int, int)  # downloaded, skipped, failed
    error = Signal(str, str)  # game_name, error_message

    def __init__(
        self,
        games: List[Tuple[str, str]],  # (title, cover_url)
        parent=None,
    ):
        super().__init__(parent)
        self._games = games
        self._cancelled = False
        self._cancel_lock = threading.Lock()

    def cancel(self) -> None:
        with self._cancel_lock:
            self._cancelled = True

    def _is_cancelled(self) -> bool:
        with self._cancel_lock:
            return self._cancelled

    def run(self) -> None:
        downloaded = 0
        skipped = 0
        failed = 0
        progress_idx = 0  # counts all non-cached items (= missing items processed)
        cache = get_cover_cache()

        for title, url in self._games:
            if self._is_cancelled():
                break

            status, err = cache.download_to_disk(url)

            if status == "cached":
                # Already on disk — not part of missing count, don't advance progress
                skipped += 1
            elif status == "not_found":
                # 404 — was counted as missing, advances progress
                skipped += 1
                progress_idx += 1
                self.progress.emit(progress_idx, title)
            elif status == "downloaded":
                downloaded += 1
                progress_idx += 1
                self.progress.emit(progress_idx, title)
            else:
                failed += 1
                progress_idx += 1
                self.progress.emit(progress_idx, title)
                if err:
                    self.error.emit(title, err)

            self.stats.emit(downloaded, skipped, failed)

            if self._is_cancelled():
                break

        self.finished_signal.emit(downloaded, skipped, failed)


class DownloadCoversDialog(QDialog):
    """Modal dialog for batch cover downloading with progress."""

    def __init__(self, games: List[Tuple[str, str]], parent=None):
        """
        Args:
            games: List of (title, cover_url) for all games with covers.
        """
        super().__init__(parent)
        self._games = games
        self._worker = None
        self._downloaded = 0
        self._skipped = 0
        self._failed = 0
        self._errors: list = []

        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(450)
        self.setModal(True)

        self._setup_ui()
        self._missing = self._count_missing()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # Header
        self._lbl_header = QLabel(_("Download Missing Covers"))
        font = self._lbl_header.font()
        font.setBold(True)
        self._lbl_header.setFont(font)
        layout.addWidget(self._lbl_header)

        # Info line (filled by _count_missing)
        self._lbl_info = QLabel()
        self._lbl_info.setWordWrap(True)
        layout.addWidget(self._lbl_info)

        # Current game
        self._lbl_current = QLabel()
        self._lbl_current.setObjectName("hintLabel")
        layout.addWidget(self._lbl_current)

        # Progress bar
        self._progress = StripedProgressBar()
        self._progress.setMinimum(0)
        self._progress.setMaximum(0)  # indeterminate until started
        self._progress.setFixedHeight(22)
        layout.addWidget(self._progress)

        # Stats line
        self._lbl_stats = QLabel()
        self._lbl_stats.setObjectName("hintLabel")
        layout.addWidget(self._lbl_stats)
        self._update_stats()

        # Error details (hidden until errors occur)
        self._error_details = QTextEdit()
        self._error_details.setReadOnly(True)
        self._error_details.setMaximumHeight(120)
        self._error_details.setVisible(False)
        self._error_details.setObjectName("hintLabel")
        layout.addWidget(self._error_details)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._btn_start = QPushButton(_("Download"))
        self._btn_start.clicked.connect(self._start_download)
        btn_layout.addWidget(self._btn_start)

        self._btn_close = QPushButton(_("Close"))
        self._btn_close.clicked.connect(self._on_close_clicked)
        btn_layout.addWidget(self._btn_close)

        layout.addLayout(btn_layout)

    def _count_missing(self) -> int:
        """Count how many covers are missing from disk cache."""
        cache = get_cover_cache()
        total = len(self._games)
        cached = sum(1 for _, url in self._games if cache.get_disk_path(url))
        missing = total - cached

        self._lbl_info.setText(
            ngettext(
                "{missing} of {total} cover needs downloading.",
                "{missing} of {total} covers need downloading.",
                missing,
            ).format(missing=missing, total=total)
        )

        if missing == 0:
            self._lbl_info.setText(_("All covers are already cached."))
            self._btn_start.setVisible(False)

        return missing

    def _start_download(self) -> None:
        # Check network before starting
        try:
            from ...core.network_monitor import get_network_monitor
            if not get_network_monitor().is_online:
                self._lbl_info.setText(_("Cannot download covers while offline."))
                return
        except RuntimeError:
            pass  # Monitor not initialized

        self._btn_start.setEnabled(False)
        self._btn_start.setVisible(False)

        self._progress.setMaximum(self._missing)
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m")

        self._worker = DownloadCoversWorker(self._games, parent=None)
        self._worker.progress.connect(self._on_progress)
        self._worker.stats.connect(self._on_stats)
        self._worker.finished_signal.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

        self._btn_close.setText(_("Cancel"))

    def _on_progress(self, current: int, game_name: str) -> None:
        self._progress.setValue(current)
        if game_name:
            self._lbl_current.setText(game_name)
        else:
            self._lbl_current.setText("")

    def _on_stats(self, downloaded: int, skipped: int, failed: int) -> None:
        self._downloaded = downloaded
        self._skipped = skipped
        self._failed = failed
        self._update_stats()

    def _on_error(self, game_name: str, error_msg: str) -> None:
        self._errors.append((game_name, error_msg))

    def _on_finished(self, downloaded: int, skipped: int, failed: int) -> None:
        self._downloaded = downloaded
        self._skipped = skipped
        self._failed = failed
        self._update_stats()

        self._progress.setValue(self._progress.maximum())
        self._lbl_current.setText("")

        self._lbl_info.setText(
            _("Done. {downloaded} downloaded, {skipped} already cached, "
              "{failed} failed.").format(
                downloaded=downloaded, skipped=skipped, failed=failed
            )
        )

        # Show error details if any
        if self._errors:
            lines = []
            for name, msg in self._errors:
                lines.append(f"{name}: {msg}")
            self._error_details.setPlainText("\n".join(lines))
            self._error_details.setVisible(True)

        self._btn_close.setText(_("Close"))
        self._btn_close.setEnabled(True)
        self._worker = None

    def _update_stats(self) -> None:
        self._lbl_stats.setText(
            _("Downloaded: {downloaded}  |  Cached: {skipped}  |  "
              "Failed: {failed}").format(
                downloaded=self._downloaded,
                skipped=self._skipped,
                failed=self._failed,
            )
        )

    def _on_close_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._lbl_current.setText(_("Cancelling..."))
            self._btn_close.setEnabled(False)
            # Worker will emit finished_signal which re-enables close
        else:
            self.close()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(event)
