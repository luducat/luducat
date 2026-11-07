# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# instance_lock.py

"""Single-instance lock file."""

from pathlib import Path

LOCK_FILE_NAME = "luducat.lock"


def acquire_instance_lock(lock_dir: Path):
    """Take the single-instance lock, or return None if another process holds it.

    The returned QLockFile must stay referenced while the lock is needed; it
    releases the lock when destroyed or unlocked.
    """
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(lock_dir / LOCK_FILE_NAME))
    lock.setStaleLockTime(0)  # Always check PID, never time-based
    if lock.tryLock(100):
        return lock
    return None
