# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# directory_migrate.py

"""Copy a luducat directory for a move, rolling back on failure."""

import logging
import os
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .config import is_case_insensitive_platform, path_contains

logger = logging.getLogger(__name__)


def _raise_walk_error(error: OSError) -> None:
    # os.walk skips unreadable folders silently; a move must not.
    raise error


class DirectoryMoveError(RuntimeError):
    """The copy failed or was cancelled; migrate() removed the partial copy."""


class DirectoryMigration(QObject):
    """Emits progress per copied entry; cancel() is safe to call from another thread."""

    progress = Signal(int, int, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation; migrate() raises after rolling back."""
        self._cancelled = True

    def migrate(self, source: Path, target: Path) -> list[Path]:
        """Copy source to target; return the copied entries relative to source.

        Raises DirectoryMoveError after removing the partial copy.
        """
        self._cancelled = False
        created_target = not target.exists()

        try:
            files = self._enumerate_files(source)
            dirs = self._enumerate_dirs(source)
        except OSError as e:
            raise DirectoryMoveError(f"Failed to enumerate source: {e}") from e
        if not files and not dirs:
            raise DirectoryMoveError("Source directory is empty.")
        try:
            bad_links = self._links_that_would_break(source, files)
        except OSError as e:
            raise DirectoryMoveError(f"Failed to read links in source: {e}") from e
        if bad_links:
            raise DirectoryMoveError(
                "These links would point somewhere else after the move: "
                + ", ".join(str(p) for p in bad_links)
            )

        total = len(files)
        copied: list[Path] = []
        rel_path = Path()
        try:
            # Empty folders too: plugins expect their cache dirs to exist
            target.mkdir(parents=True, exist_ok=True)
            for rel_dir in dirs:
                (target / rel_dir).mkdir(parents=True, exist_ok=True)
            for i, rel_path in enumerate(files):
                if self._cancelled:
                    self._rollback(target, copied, remove_root=created_target)
                    raise DirectoryMoveError("Move cancelled by user.")
                dst_file = target / rel_path
                # Links are copied as links; following them would copy their
                # target, and a linked folder would arrive empty.
                shutil.copy2(str(source / rel_path), str(dst_file), follow_symlinks=False)
                copied.append(dst_file)
                self.progress.emit(i + 1, total, rel_path.name)
        except OSError as e:
            logger.error("Directory move copy failed at %s: %s", rel_path, e)
            self._rollback(target, copied, remove_root=created_target)
            raise DirectoryMoveError(f"Copy failed: {e}") from e

        try:
            self._copy_dir_timestamps(source, target, dirs)
            ok, verify_msg = self._verify(source, target, files)
        except OSError as e:
            logger.error("Directory move could not finish the copy: %s", e)
            self._rollback(target, copied, remove_root=created_target)
            raise DirectoryMoveError(f"Finishing the copy failed: {e}") from e
        if not ok:
            self._rollback(target, copied, remove_root=created_target)
            raise DirectoryMoveError(f"Verification failed: {verify_msg}")

        return files

    def _enumerate_files(self, directory: Path) -> list[Path]:
        """Files and links (to files or folders) below directory, relative to it."""
        entries = []
        for dirpath, dirnames, filenames in os.walk(
            directory, onerror=_raise_walk_error, followlinks=False
        ):
            base = Path(dirpath)
            entries.extend(base / name for name in filenames)
            entries.extend(base / name for name in dirnames if (base / name).is_symlink())
        return sorted(e.relative_to(directory) for e in entries)

    def _enumerate_dirs(self, directory: Path) -> list[Path]:
        """Real folders below directory, relative to it, empty ones included."""
        dirs = []
        for dirpath, dirnames, _filenames in os.walk(
            directory, onerror=_raise_walk_error, followlinks=False
        ):
            base = Path(dirpath)
            dirs.extend(base / name for name in dirnames if not (base / name).is_symlink())
        return sorted(d.relative_to(directory) for d in dirs)

    def _links_that_would_break(self, source: Path, entries: list[Path]) -> list[Path]:
        """Relative links must stay inside the tree; absolute links into it would still point at the old place."""
        ci = is_case_insensitive_platform()
        root = os.path.normpath(str(source.absolute()))
        if ci:
            root = root.casefold()
        bad = []
        for rel in entries:
            path = source / rel
            if not path.is_symlink():
                continue
            link = os.readlink(path)
            if os.path.isabs(link):
                if path_contains(source, Path(link), ci):
                    bad.append(rel)
                continue
            landing = os.path.normpath(os.path.join(str(path.parent.absolute()), link))
            if ci:
                landing = landing.casefold()
            if landing != root and not landing.startswith(root + os.sep):
                bad.append(rel)
        return bad

    def _copy_dir_timestamps(self, source: Path, target: Path, dirs: list[Path]) -> None:
        # After all files exist: creating them bumped every folder's mtime.
        for rel_dir in sorted(dirs, key=lambda d: len(d.parts), reverse=True):
            shutil.copystat(source / rel_dir, target / rel_dir)
        shutil.copystat(source, target)

    def _verify(
        self, source: Path, target: Path, files: list[Path]
    ) -> tuple[bool, str]:
        """Every entry exists in target; links point to the same place, files have the same size."""
        for rel_path in files:
            src_file = source / rel_path
            dst_file = target / rel_path
            if not os.path.lexists(dst_file):
                return False, f"Missing file: {rel_path}"
            if src_file.is_symlink():
                if not dst_file.is_symlink() or os.readlink(dst_file) != os.readlink(src_file):
                    return False, f"Link not recreated: {rel_path}"
                continue
            try:
                src_size = src_file.stat().st_size
                dst_size = dst_file.stat().st_size
                if src_size != dst_size:
                    return False, f"Size mismatch: {rel_path} ({src_size} vs {dst_size})"
            except OSError as e:
                return False, f"Stat failed: {rel_path}: {e}"
        return True, ""

    def _rollback(self, target: Path, copied: list[Path], remove_root: bool = False) -> None:
        """Remove partially copied files and now-empty folders from target.

        remove_root also removes target itself when the move created it.
        """
        logger.info("Rolling back directory move: removing %d files from %s",
                    len(copied), target)
        for f in reversed(copied):
            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Rollback could not remove %s: %s", f, e)
        try:
            for d in sorted(target.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()  # Only removes empty dirs
                    except OSError:
                        logger.debug("Rollback kept non-empty dir %s", d)
        except OSError as e:
            logger.warning("Rollback could not scan %s: %s", target, e)
        if remove_root:
            try:
                target.rmdir()
            except OSError as e:
                logger.warning("Rollback kept %s: %s", target, e)
