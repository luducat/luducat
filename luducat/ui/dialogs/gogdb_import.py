# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# gogdb_import.py

"""GOG Data Update Dialog

Two-phase dialog for updating GOG game data:
  Phase 1: GOGdb dump import (bulk metadata, incremental)
  Phase 2: Catalog API scan (authoritative overwrite, covers + ratings + pricing)

Both phases run sequentially in a background worker thread.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

logger = logging.getLogger(__name__)


class GogDataUpdateWorker(QThread):
    """Background worker for 2-phase GOG data update.

    Phase 1: GOGdb dump import (incremental, fills gaps)
    Phase 2: Catalog API scan (authoritative overwrite)
    """

    # Signals
    progress = Signal(str, int, int)  # message, current, total
    phase_changed = Signal(int, str)  # phase_number (1 or 2), status text
    finished = Signal(dict)  # combined stats
    error = Signal(str)  # error message

    def __init__(self, gog_store, skip_gogdb: bool = False):
        super().__init__()
        self.gog_store = gog_store
        self.skip_gogdb = skip_gogdb
        self._cancelled = False

    def run(self):
        """Execute both phases in background thread."""
        combined_stats = {
            "phase1_completed": False,
            "phase2_completed": False,
        }

        # ── Phase 1: GOGdb dump import ────────────────────────
        if not self.skip_gogdb and not self._cancelled:
            self.phase_changed.emit(1, "running")
            try:
                def gogdb_progress(phase: str, current: int, total: int):
                    if not self._cancelled:
                        self.progress.emit(phase, current, total)

                stats = self.gog_store.import_from_gogdb(gogdb_progress)
                combined_stats["phase1"] = stats
                combined_stats["phase1_completed"] = True

                if stats.get("preflight_failed"):
                    self.phase_changed.emit(1, "failed")
                else:
                    self.phase_changed.emit(1, "done")

            except Exception as e:
                logger.error(f"GOGdb import error: {e}")
                combined_stats["phase1_error"] = str(e)
                self.phase_changed.emit(1, "error")
        elif self.skip_gogdb:
            self.phase_changed.emit(1, "skipped")
            combined_stats["phase1_completed"] = True

        if self._cancelled:
            self.finished.emit(combined_stats)
            return

        # ── Phase 2: Catalog API scan ─────────────────────────
        self.phase_changed.emit(2, "running")
        try:
            def catalog_progress(message: str, current: int, total: int):
                if not self._cancelled:
                    self.progress.emit(message, current, total)

            def cancel_check() -> bool:
                return self._cancelled

            # Run async method in a new event loop for this thread
            loop = asyncio.new_event_loop()
            try:
                catalog_stats = loop.run_until_complete(
                    self.gog_store._enrich_bulk_catalog(
                        status_callback=catalog_progress,
                        cancel_check=cancel_check,
                    )
                )
            finally:
                loop.close()

            combined_stats["phase2"] = catalog_stats
            combined_stats["phase2_completed"] = True
            self.phase_changed.emit(2, "done")

            # Update last-scanned timestamp
            try:
                from luducat.core.config import Config
                config = Config()
                config.set("gog.gogdb_last_scanned", datetime.now().isoformat())
            except Exception as e:
                logger.debug(f"Could not update gogdb_last_scanned: {e}")

        except Exception as e:
            logger.error(f"Catalog scan error: {e}")
            combined_stats["phase2_error"] = str(e)
            self.phase_changed.emit(2, "error")

        if not self._cancelled:
            self.finished.emit(combined_stats)

    def cancel(self):
        """Request cancellation."""
        self._cancelled = True


# Keep old name as alias for backwards compatibility
GogdbImportWorker = GogDataUpdateWorker


class GogdbImportDialog(QDialog):
    """Two-phase GOG data update dialog.

    Phase 1: GOGdb dump import — bulk metadata (incremental, fills gaps)
    Phase 2: Catalog API scan — authoritative overwrite (covers, ratings, pricing)

    Shows:
    - Phase status indicators
    - Progress bar with phase details
    - Log output area
    - Start/Cancel/Close buttons
    """

    def __init__(self, gog_store, parent=None):
        super().__init__(parent)
        self.gog_store = gog_store
        self.worker: Optional[GogDataUpdateWorker] = None
        self._started = False
        self._finished = False

        self.setWindowTitle(_("Update GOG Data"))
        self.setMinimumWidth(550)
        self.setup_ui()
        self.adjustSize()

    def setup_ui(self):
        """Create dialog UI."""
        layout = QVBoxLayout(self)

        # Description
        desc_label = QLabel(
            _("Update GOG game data from two sources:\n\n"
              "Phase 1 — GOGdb dump: Downloads the GOGdb.org database and\n"
              "fills in missing metadata (developers, genres, series, etc.).\n\n"
              "Phase 2 — Catalog scan: Fetches GOG's catalog API for vertical\n"
              "covers, content ratings, pricing, and screenshots. This data\n"
              "overwrites older values with the latest from GOG.\n\n"
              "Both phases require an internet connection.")
        )
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # Current catalog stats
        try:
            stats = self.gog_store.get_catalog_stats()
            stats_label = QLabel(
                _("Current catalog: {game_count} games "
                  "({total_count} total with DLC)").format(
                    game_count=stats['game_count'],
                    total_count=stats['total_count'])
            )
            layout.addWidget(stats_label)
        except Exception:
            pass

        # Phase indicators
        phase_layout = QHBoxLayout()
        self._phase1_label = QLabel(_("Phase 1: GOGdb Dump"))
        self._phase1_status = QLabel(_("[pending]"))
        phase_layout.addWidget(self._phase1_label)
        phase_layout.addWidget(self._phase1_status)
        phase_layout.addStretch()
        layout.addLayout(phase_layout)

        phase2_layout = QHBoxLayout()
        self._phase2_label = QLabel(_("Phase 2: Catalog Scan"))
        self._phase2_status = QLabel(_("[pending]"))
        phase2_layout.addWidget(self._phase2_label)
        phase2_layout.addWidget(self._phase2_status)
        phase2_layout.addStretch()
        layout.addLayout(phase2_layout)

        # Progress section
        self.phase_label = QLabel(_("Ready"))
        layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Log output
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(200)
        layout.addWidget(self.log_output)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.start_button = QPushButton(_("Start"))
        self.start_button.clicked.connect(self.start_update)
        button_layout.addWidget(self.start_button)

        self.cancel_button = QPushButton(_("Cancel"))
        self.cancel_button.clicked.connect(self.cancel_update)
        self.cancel_button.setEnabled(False)
        button_layout.addWidget(self.cancel_button)

        self.close_button = QPushButton(_("Close"))
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)

        layout.addLayout(button_layout)

    def log(self, message: str):
        """Add message to log output."""
        self.log_output.append(message)
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _update_phase_status(self, phase: int, status: str):
        """Update phase status indicator."""
        status_map = {
            "pending": _("[pending]"),
            "running": _("[running...]"),
            "done": _("[done]"),
            "skipped": _("[skipped]"),
            "error": _("[error]"),
            "failed": _("[failed]"),
        }
        text = status_map.get(status, f"[{status}]")
        if phase == 1:
            self._phase1_status.setText(text)
        elif phase == 2:
            self._phase2_status.setText(text)

    def start_update(self):
        """Start the 2-phase update process."""
        if self._started:
            return

        self._started = True
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.close_button.setEnabled(False)

        self.log(_("Starting GOG data update..."))
        self.log("")

        # Create and start worker
        self.worker = GogDataUpdateWorker(self.gog_store)
        self.worker.progress.connect(self.on_progress)
        self.worker.phase_changed.connect(self._on_phase_changed)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.start()

    # Keep old method name as alias for callers that use start_import()
    start_import = start_update

    def cancel_update(self):
        """Cancel the update process."""
        if self.worker and self.worker.isRunning():
            self.log(_("Cancelling..."))
            self.worker.cancel()
            self.worker.wait(5000)

        self._finished = True
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

    # Keep old method name as alias
    cancel_import = cancel_update

    def _on_phase_changed(self, phase: int, status: str):
        """Handle phase status change from worker."""
        self._update_phase_status(phase, status)

        if phase == 1 and status == "running":
            self.log("=" * 40)
            self.log(_("Phase 1: GOGdb Dump Import"))
            self.log("=" * 40)
            self.progress_bar.setRange(0, 0)  # Indeterminate initially
        elif phase == 1 and status == "skipped":
            self.log(_("Phase 1: GOGdb dump — skipped"))
        elif phase == 2 and status == "running":
            self.log("")
            self.log("=" * 40)
            self.log(_("Phase 2: Catalog API Scan"))
            self.log("=" * 40)
            self.progress_bar.setRange(0, 0)

    def on_progress(self, message: str, current: int, total: int):
        """Handle progress updates from worker."""
        self.phase_label.setText(message)

        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)

            # Log occasional updates
            if current == 0 or current == total or current % 1000 == 0:
                self.log(f"{message} ({current}/{total})")
        else:
            self.progress_bar.setRange(0, 0)
            if current == 0:
                self.log(message)

    def on_finished(self, stats: Dict[str, Any]):
        """Handle completion of both phases."""
        self._finished = True

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

        self.log("")
        self.log("=" * 40)
        self.log(_("Update Complete!"))
        self.log("=" * 40)

        # Phase 1 results
        p1 = stats.get("phase1", {})
        if stats.get("phase1_completed") and p1:
            if p1.get("preflight_failed"):
                self.log("")
                self.log(_("Phase 1 — GOGdb: Preflight failed"))
                for error in p1.get("preflight_errors", []):
                    self.log(_("  ERROR: {error}").format(error=error))
            else:
                self.log(_("  GOGdb dump date: {dump_date}").format(
                    dump_date=p1.get('dump_date', _('unknown'))))
                self.log(_("  Products found: {count}").format(
                    count=p1.get('products_found', 0)))
                self.log(_("  Games imported: {count}").format(
                    count=p1.get('imported', 0)))
                self.log(_("  Skipped (in DB): {count}").format(
                    count=p1.get('skipped', 0)))
                skipped_non_game = p1.get('skipped_non_game', 0)
                if skipped_non_game > 0:
                    self.log(_("  Skipped (DLC): {count}").format(
                        count=skipped_non_game))
                errors = p1.get('errors', 0)
                if errors > 0:
                    self.log(_("  Errors: {count}").format(count=errors))
        elif stats.get("phase1_error"):
            self.log(_("  Phase 1 error: {error}").format(
                error=stats['phase1_error']))

        # Phase 2 results
        p2 = stats.get("phase2", {})
        if stats.get("phase2_completed") and p2:
            self.log("")
            self.log(_("  Catalog products: {count}").format(
                count=p2.get('catalog_fetched', 0)))
            self.log(_("  Games enriched: {count}").format(
                count=p2.get('enriched', 0)))
            errors = p2.get('errors', 0)
            if errors > 0:
                self.log(_("  Errors: {count}").format(count=errors))
        elif stats.get("phase2_error"):
            self.log(_("  Phase 2 error: {error}").format(
                error=stats['phase2_error']))

        self.phase_label.setText(_("Update complete!"))
        self.close_button.setFocus()

    def on_error(self, error_msg: str):
        """Handle fatal error."""
        self._finished = True

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.phase_label.setText(_("Update failed!"))

        self.log("")
        self.log("=" * 40)
        self.log(_("Update Failed!"))
        self.log("=" * 40)
        self.log(_("Error: {error}").format(error=error_msg))

        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

    def closeEvent(self, event):
        """Handle dialog close."""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(2000)
        event.accept()
