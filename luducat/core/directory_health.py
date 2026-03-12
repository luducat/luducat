# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# directory_health.py

"""Directory health check utility for luducat

Provides health checks for data, cache, and config directories:
- Reachability and writability tests
- Free space and usage reporting
- Health status indicators for the settings UI
"""

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DirHealth:
    """Health status of a directory."""
    reachable: bool
    writable: bool
    free_mb: int
    total_mb: int
    used_mb: int  # How much luducat uses in this dir
    error: Optional[str]

    @property
    def status(self) -> str:
        """Return 'green', 'yellow', or 'red' health status."""
        if not self.reachable or not self.writable:
            return "red"
        if self.free_mb < 1024:  # Less than 1 GB free
            return "yellow"
        return "green"

    @property
    def tooltip(self) -> str:
        """Human-readable tooltip for the health indicator."""
        if not self.reachable:
            return f"Not reachable: {self.error or 'directory does not exist'}"
        if not self.writable:
            return f"Not writable: {self.error or 'permission denied'}"
        return f"Writable, {_format_size_mb(self.free_mb)} free"


def _format_size_mb(mb: int) -> str:
    """Format size in MB to human-readable string."""
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def get_dir_size(path: Path) -> int:
    """Get total size of directory contents in bytes."""
    total = 0
    if not path.exists():
        return 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def check_directory(path: Path) -> DirHealth:
    """Check reachability, writability, and free space of a directory.

    Args:
        path: Directory path to check

    Returns:
        DirHealth with status information
    """
    # 1. Reachability
    try:
        if not path.exists():
            # Try to create it
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return DirHealth(
                reachable=False, writable=False,
                free_mb=0, total_mb=0, used_mb=0,
                error="path exists but is not a directory",
            )
    except OSError as e:
        return DirHealth(
            reachable=False, writable=False,
            free_mb=0, total_mb=0, used_mb=0,
            error=str(e),
        )

    # 2. Writability — try writing a temp file
    writable = True
    write_error = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(path), prefix=".luducat_health_")
        import os
        os.close(fd)
        Path(tmp_path).unlink(missing_ok=True)
    except OSError as e:
        writable = False
        write_error = str(e)

    # 3. Free space
    free_mb = 0
    total_mb = 0
    try:
        usage = shutil.disk_usage(path)
        free_mb = int(usage.free / (1024 * 1024))
        total_mb = int(usage.total / (1024 * 1024))
    except OSError:
        pass

    # 4. Luducat usage in this dir
    used_bytes = get_dir_size(path)
    used_mb = int(used_bytes / (1024 * 1024))

    return DirHealth(
        reachable=True,
        writable=writable,
        free_mb=free_mb,
        total_mb=total_mb,
        used_mb=used_mb,
        error=write_error,
    )
