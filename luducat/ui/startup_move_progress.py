# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# startup_move_progress.py

"""Progress dialog for a directory move run at startup, before the main window."""

import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QThread, Slot
from PySide6.QtWidgets import QApplication, QProgressDialog

from ..core.constants import APP_NAME
from ..core.directory_migrate import DirectoryMigration


class _ProgressSink(QObject):
    """Lives in the GUI thread so the worker's progress signals arrive queued."""

    def __init__(self, dialog: QProgressDialog):
        super().__init__()
        self._dialog = dialog

    @Slot(int, int, str)
    def on_progress(self, current: int, total: int, name: str) -> None:
        self._dialog.setMaximum(total)
        self._dialog.setValue(current)
        self._dialog.setLabelText(_("Copying: {filename}").format(filename=name))


class _CopyThread(QThread):
    def __init__(self, migration: DirectoryMigration, source: Path, target: Path):
        super().__init__()
        self._migration = migration
        self._source = source
        self._target = target
        self.files: list[Path] = []
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.files = self._migration.migrate(self._source, self._target)
        except Exception as e:  # handed to the GUI thread, re-raised there
            self.error = e


def copy_with_progress(kind: str, source: Path, target: Path) -> list[Path]:
    """Copy source to target in a worker thread behind a modal progress dialog.

    Returns the copied files relative to source; re-raises the worker's
    DirectoryMoveError (DirectoryMigration already rolled back the partial copy).
    """
    QApplication.instance() or QApplication(sys.argv)

    dialog = QProgressDialog(
        _("Moving {source} to {target}").format(source=source, target=target),
        _("Cancel"), 0, 0,
    )
    if kind == "data":
        title = _("Moving the Data Folder")
    else:
        title = _("Moving the Cache Folder")
    dialog.setWindowTitle(f"{title} - {APP_NAME}")
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)

    migration = DirectoryMigration()
    sink = _ProgressSink(dialog)
    migration.progress.connect(sink.on_progress)
    dialog.canceled.connect(migration.cancel)

    worker = _CopyThread(migration, source, target)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    worker.start()
    dialog.show()
    loop.exec()
    worker.wait()
    dialog.close()

    if worker.error is not None:
        raise worker.error
    return worker.files
