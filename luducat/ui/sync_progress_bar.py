# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sync_progress_bar.py

"""Non-blocking sync progress bar for luducat

Replaces the modal SyncDialog. Embedded in the toolbar area so the user
can browse, filter, and sort games while sync runs in the background.

Layout when active:
    [{activity}] [====progress_bar====] [{count}] [Pause] [Cancel]

Hidden when no sync is active.
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QFrame,
)

logger = logging.getLogger(__name__)


class SyncProgressBar(QWidget):
    """Non-blocking sync progress bar embedded in the toolbar.

    Signals:
        pause_requested: User clicked pause/resume
        cancel_requested: User clicked cancel
    """

    pause_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("syncProgressBar")
        self._queue = None
        self._is_paused = False
        self._current_plugin = ""

        self._setup_ui()
        self.hide()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)

        # Separator before progress area
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setObjectName("syncSeparator")
        layout.addWidget(sep)

        # Activity text: "igdb: 150/6354"
        self._activity_label = QLabel()
        self._activity_label.setObjectName("syncActivityLabel")
        self._activity_label.setFixedWidth(200)
        layout.addWidget(self._activity_label)

        # Progress bar — taller, fixed width, shows percentage text
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("syncProgressBarWidget")
        self._progress_bar.setMinimum(0)
        self._progress_bar.setMaximum(100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedWidth(200)
        self._progress_bar.setFixedHeight(28)
        layout.addWidget(self._progress_bar)

        # Count label: "3/8" (jobs)
        self._count_label = QLabel()
        self._count_label.setObjectName("syncCountLabel")
        self._count_label.setFixedWidth(80)
        self._count_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._count_label)

        # Pause button
        self._btn_pause = QPushButton(_("Pause"))
        self._btn_pause.setObjectName("syncPauseButton")
        self._btn_pause.setFixedHeight(22)
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        layout.addWidget(self._btn_pause)

        # Cancel button
        self._btn_cancel = QPushButton(_("Cancel"))
        self._btn_cancel.setObjectName("syncCancelButton")
        self._btn_cancel.setFixedHeight(22)
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self._btn_cancel)

    # --- Public API (called by MainWindow) ---

    def start(self, queue) -> None:
        """Show the progress bar and start tracking a queue.

        Args:
            queue: SyncJobQueue instance
        """
        self._queue = queue
        self._is_paused = False
        self._current_plugin = ""
        self._activity_label.setText(_("Starting sync..."))
        self._progress_bar.setValue(0)
        self._count_label.setText("0/0")
        self._btn_pause.setText("Pause")
        self._btn_cancel.setEnabled(True)
        self.show()

    def finish(self, stats: dict) -> None:
        """Hide the progress bar after sync completes."""
        self._queue = None
        self.hide()

    def on_cancelled(self, stats: dict) -> None:
        """Hide the progress bar after sync is cancelled."""
        self._queue = None
        self.hide()

    # --- Slots (connected to SyncWorker signals) ---

    def on_job_started(self, plugin: str, description: str, batch_n: int, batch_total: int) -> None:
        """Update activity text when a new job starts."""
        self._current_plugin = plugin
        if batch_total > 1:
            self._activity_label.setText(f"{plugin}: {batch_n}/{batch_total}")
        else:
            self._activity_label.setText(description)

    def on_job_progress(self, current: int, total: int) -> None:
        """Update activity label with within-job detail (e.g. 'igdb: 150/6354').

        Does NOT touch the progress bar — that is driven solely by
        on_global_progress so the bar rises smoothly across all jobs.
        """
        if total > 0:
            self._activity_label.setText(
                f"{self._current_plugin}: {current}/{total}"
            )

    def on_global_progress(self, completed: int, total: int) -> None:
        """Update the progress bar and count label with overall job progress.

        This is the sole driver of the progress bar fill level.
        """
        self._count_label.setText(f"{completed}/{total}")
        if total > 0:
            pct = int((completed / total) * 100)
            self._progress_bar.setValue(pct)

    def on_rate_limit(self, message: str) -> None:
        """Show rate limit status in activity text."""
        self._activity_label.setText(message)

    # --- Private ---

    def _on_pause_clicked(self) -> None:
        if self._is_paused:
            self._is_paused = False
            self._btn_pause.setText(_("Pause"))
            if self._queue:
                self._queue.resume()
        else:
            self._is_paused = True
            self._btn_pause.setText(_("Resume"))
            if self._queue:
                self._queue.pause()
        self.pause_requested.emit()

    def _on_cancel_clicked(self) -> None:
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.setText(_("Cancelling..."))
        self._activity_label.setText(_("Cancelling..."))
        self.cancel_requested.emit()
