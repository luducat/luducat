# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# layout_migration.py

"""One-time move from the merged Windows/macOS config+data folder into the split layout.

Every entry is classified first; anything unknown or already present at its
destination stops the move before it touches any file.
"""

import logging
import os
from pathlib import Path

from .config import DirLayout
from .constants import APP_NAME
from .instance_lock import acquire_instance_lock

logger = logging.getLogger(__name__)

CONFIG_ENTRIES = frozenset({
    "config.toml", "credentials.json", "backups", "logs", "luducat.lock",
    "presets", "themes", "plugins",
})
DATA_ENTRIES = frozenset({
    "games.db", "games.db-wal", "games.db-shm", "games.db.bak",
    "plugins-data", "archive", "trust-state.json", "pre_restore_backups",
})
# platformdirs' Windows cache: <old>\Cache becomes the cache dir itself.
WINDOWS_CACHE_ENTRY = "Cache"
# Files the OS file manager drops into folders; never ours, never blocking.
OS_METADATA = frozenset({".DS_Store", "desktop.ini", "Thumbs.db"})


class LayoutMigrationError(RuntimeError):
    """The migration stopped after moving nothing or only part of the old layout."""


def legacy_merged_dir(system: str) -> Path | None:
    """The merged config+data folder of the old layout, or None on Linux."""
    if system == "Windows":
        from platformdirs.windows import Windows
        return Path(Windows(appname=APP_NAME).user_data_dir)
    if system == "Darwin":
        from platformdirs.macos import MacOS
        return Path(MacOS(appname=APP_NAME).user_data_dir)
    return None


def _destination(name: str, layout: DirLayout, system: str) -> Path | None:
    if name in CONFIG_ENTRIES:
        return layout.config / name
    if name in DATA_ENTRIES:
        return layout.data / name
    if system == "Windows" and name == WINDOWS_CACHE_ENTRY:
        return layout.cache
    return None


def migrate_layout(old: Path, layout: DirLayout, system: str) -> list[str]:
    """Move the entries of the merged folder `old` into `layout`; return the names moved."""
    if not old.is_dir():
        return []

    # On macOS the new config/ and data/ live inside the old folder.
    own_dirs = {layout.config, layout.data}
    pending = [
        e for e in old.iterdir()
        if e.name not in OS_METADATA and e not in own_dirs
    ]
    if not pending:
        _remove_if_empty(old, layout)
        return []

    lock = acquire_instance_lock(old)
    if lock is None:
        raise LayoutMigrationError(
            f"An older luducat is still running from {old}. Close it and start again."
        )
    lock.unlock()  # removes the stale lock file from `old`

    moves: list[tuple[Path, Path]] = []
    unknown: list[str] = []
    occupied: list[str] = []
    for entry in sorted(old.iterdir()):
        if entry.name in OS_METADATA or entry in own_dirs:
            continue
        dest = _destination(entry.name, layout, system)
        if dest is None:
            unknown.append(entry.name)
        elif dest.exists():
            occupied.append(f"{entry.name} -> {dest}")
        else:
            moves.append((entry, dest))

    if unknown or occupied:
        problems = []
        if unknown:
            problems.append("unknown entries: " + ", ".join(unknown))
        if occupied:
            problems.append("destination already exists: " + "; ".join(occupied))
        raise LayoutMigrationError(
            f"Cannot move {old} into the new folder layout ({' / '.join(problems)}). "
            "The migration did not touch anything."
        )

    moved: list[str] = []
    for src, dest in moves:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
        except OSError as e:
            raise LayoutMigrationError(
                f"Moving {src} to {dest} failed: {e}. Already moved: "
                f"{', '.join(moved) or 'nothing'}. Start luducat again to continue."
            ) from e
        moved.append(src.name)
        logger.info("Layout migration: %s -> %s", src, dest)

    _remove_if_empty(old, layout)
    return moved


def _remove_if_empty(old: Path, layout: DirLayout) -> None:
    # On macOS `old` is the parent of the new dirs and stays.
    if layout.config.parent == old:
        return
    for name in OS_METADATA:
        (old / name).unlink(missing_ok=True)
    try:
        old.rmdir()
    except OSError as e:
        logger.warning("Old luducat folder %s left in place: %s", old, e)
