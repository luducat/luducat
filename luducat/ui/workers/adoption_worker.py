# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Background worker for the archive adoption scan.

Walking a multi-terabyte volume takes a minute or two of stat calls, so
the scan runs off the main thread with directory-level progress and
cancellation. The commit itself is one bulk insert and stays on the
dialog side.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


def _run_adoption_scan_sync(engine, handler, config, allow_network=False,
                            progress_cb=None, cancel_check=None) -> dict:
    """Run one adoption scan (blocking).

    Returns dict with:
      - {"type": "done", "scanner": AdoptionScanner, "report": AdoptionReport}
      - {"type": "error", "message": str}
    """
    from luducat.core.archivist.adoption import AdoptionScanner
    from luducat.core.archivist.manager import ArchivistManager
    from luducat.core.archivist.volume import volume_manager_from_config

    try:
        resolver = handler.adoption_slug_resolver(allow_network=allow_network)
        if resolver is None:
            return {"type": "error",
                    "message": f"{handler.display_name} does not support "
                               f"archive adoption."}

        vm = volume_manager_from_config(config)
        manager = ArchivistManager(
            engine=engine, base_path=vm.base_path,
            organization=vm.organization, custom_layout=vm.custom_layout)
        scanner = AdoptionScanner(
            manager=manager, store_name=handler.store_name,
            resolve_slug=resolver)
        report = scanner.scan(
            vm.base_path, progress_cb=progress_cb, cancel_check=cancel_check)
        return {"type": "done", "scanner": scanner, "report": report}
    except Exception as e:
        logger.exception("Adoption scan failed")
        return {"type": "error", "message": str(e)}


class AdoptionWorker(QThread):
    """Background thread for one adoption scan.

    The completed signal carries the scanner (for the later commit) and
    the dry-run report; nothing has been written when it fires.
    """

    progress = Signal(int, int, str)   # game dirs done, total (0=unknown), dir
    completed = Signal(object, object)  # scanner, report
    failed = Signal(str)

    def __init__(self, engine, handler, config, allow_network=False,
                 parent=None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._handler = handler
        self._config = config
        self._allow_network = allow_network
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:
        result = _run_adoption_scan_sync(
            self._engine, self._handler, self._config,
            allow_network=self._allow_network,
            progress_cb=lambda done, total, label:
                self.progress.emit(done, total, label),
            cancel_check=self._cancel_event.is_set,
        )
        if result["type"] == "error":
            self.failed.emit(result["message"])
        else:
            self.completed.emit(result["scanner"], result["report"])
