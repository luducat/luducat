# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Background worker for archive audit scans.

Runs ArchiveAuditor.scan_updates / scan_missing off the main thread.
A library-scale scan walks every archived (or owned) game through the
store API at roughly one request per second, so the UI shows inline
progress and offers cancellation instead of blocking.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QThread, Signal

from luducat.core.archivist.audit import ArchiveAuditor

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s

_SCAN_KINDS = ("update", "missing")


def _run_scan_sync(kind, engine, handler, config,
                   progress_cb=None, cancel_check=None,
                   force_refresh=False) -> dict:
    """Run one audit scan (blocking).

    Returns dict with:
      - {"type": "done", "candidates": [...], "errors": [...]}
      - {"type": "error", "message": str}
    """
    auth = handler.check_auth()
    if not auth:
        logger.debug("Audit scan (%s) blocked by auth: %s", kind, auth.message)
        return {"type": "error", "message": auth.message}

    try:
        auditor = ArchiveAuditor(engine, handler, config)
        scan = auditor.scan_updates if kind == "update" else auditor.scan_missing
        candidates = scan(progress_cb=progress_cb, cancel_check=cancel_check,
                          force_refresh=force_refresh)
        return {
            "type": "done",
            "candidates": candidates,
            "errors": list(auditor.errors),
        }
    except Exception as e:
        logger.exception("Audit scan (%s) failed", kind)
        return {"type": "error", "message": str(e)}


class AuditWorker(QThread):
    """Background thread for one audit scan.

    Usage:
        worker = AuditWorker(kind="update", engine=eng, handler=h, config=cfg)
        worker.progress.connect(on_progress)
        worker.completed.connect(on_completed)
        worker.failed.connect(on_failed)
        worker.start()
        ...
        worker.cancel()   # aborts between games; partial results persist

    The finished-scan signal is named `completed` because QThread already
    owns `finished`.
    """

    progress = Signal(int, int, str)   # done, total, game title
    completed = Signal(list, list)     # candidates, per-game error strings
    failed = Signal(str)               # error message (auth, crash)

    def __init__(self, kind: str, engine, handler, config,
                 parent=None, force_refresh=False) -> None:
        if kind not in _SCAN_KINDS:
            raise ValueError(f"invalid audit scan kind: {kind!r}")
        super().__init__(parent)
        self.kind = kind
        self._engine = engine
        self._handler = handler
        self._config = config
        self._force_refresh = force_refresh
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:
        result = _run_scan_sync(
            self.kind, self._engine, self._handler, self._config,
            progress_cb=lambda done, total, title:
                self.progress.emit(done, total, title),
            cancel_check=self._cancel_event.is_set,
            force_refresh=self._force_refresh,
        )
        if result["type"] == "error":
            self.failed.emit(result["message"])
        else:
            self.completed.emit(result["candidates"], result["errors"])
