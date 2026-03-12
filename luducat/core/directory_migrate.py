# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# directory_migrate.py

"""Directory migration logic for luducat

Handles moving data and cache directories to new locations with:
- File-by-file copy with progress reporting
- Verification (file count + total size)
- Config update on success
- Rollback on failure (delete partial copy, revert config)
"""

import logging
import shutil
from pathlib import Path
from typing import List, Tuple

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class DirectoryMigration(QObject):
    """Migrate a luducat directory to a new location.

    Signals:
        progress(current, total, filename): File copy progress
        finished(success, message): Migration result
    """

    progress = Signal(int, int, str)  # current, total, filename
    finished = Signal(bool, str)      # success, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation of the migration."""
        self._cancelled = True

    def migrate(self, source: Path, target: Path, config_key: str) -> None:
        """Copy files from source to target, update config on success.

        Args:
            source: Current directory (e.g. ~/.local/share/luducat)
            target: New directory to move to
            config_key: Config key to update (e.g. "app.custom_data_dir")
        """
        self._cancelled = False

        # 1. Enumerate files
        try:
            files = self._enumerate_files(source)
        except OSError as e:
            self.finished.emit(False, f"Failed to enumerate source: {e}")
            return

        total = len(files)
        if total == 0:
            self.finished.emit(False, "Source directory is empty.")
            return

        # 2. Copy each file
        copied: List[Path] = []
        rel_path = Path()
        try:
            for i, rel_path in enumerate(files):
                if self._cancelled:
                    self._rollback(target, copied)
                    self.finished.emit(False, "Migration cancelled by user.")
                    return

                src_file = source / rel_path
                dst_file = target / rel_path

                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_file), str(dst_file))
                copied.append(dst_file)

                self.progress.emit(i + 1, total, rel_path.name)

        except OSError as e:
            logger.error(f"Migration copy failed at {rel_path}: {e}")
            self._rollback(target, copied)
            self.finished.emit(False, f"Copy failed: {e}")
            return

        # 3. Verify
        ok, verify_msg = self._verify(source, target, files)
        if not ok:
            self._rollback(target, copied)
            self.finished.emit(False, f"Verification failed: {verify_msg}")
            return

        # 4. Update config
        try:
            from .config import Config  # noqa: E402 - deferred import
            config = Config()
            config.set(config_key, str(target))
            config.save()
        except Exception as e:
            logger.error(f"Failed to update config: {e}")
            self._rollback(target, copied)
            self.finished.emit(False, f"Config update failed: {e}")
            return

        self.finished.emit(
            True,
            f"Successfully migrated {total} files to {target}"
        )

    def _enumerate_files(self, directory: Path) -> List[Path]:
        """Get list of all files relative to directory."""
        files = []
        for f in sorted(directory.rglob("*")):
            if f.is_file():
                files.append(f.relative_to(directory))
        return files

    def _verify(
        self, source: Path, target: Path, files: List[Path]
    ) -> Tuple[bool, str]:
        """Verify that target matches source by file count and sizes."""
        for rel_path in files:
            src_file = source / rel_path
            dst_file = target / rel_path
            if not dst_file.exists():
                return False, f"Missing file: {rel_path}"
            try:
                src_size = src_file.stat().st_size
                dst_size = dst_file.stat().st_size
                if src_size != dst_size:
                    return False, f"Size mismatch: {rel_path} ({src_size} vs {dst_size})"
            except OSError as e:
                return False, f"Stat failed: {rel_path}: {e}"
        return True, ""

    def _rollback(self, target: Path, copied: List[Path]) -> None:
        """Remove partially copied files from target."""
        logger.info(f"Rolling back migration: removing {len(copied)} files from {target}")
        for f in reversed(copied):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        # Clean up empty directories
        try:
            for d in sorted(target.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()  # Only removes empty dirs
                    except OSError:
                        pass
        except OSError:
            pass
