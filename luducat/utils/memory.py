# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# memory.py

"""Platform-portable memory management helpers."""

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

# Resolve glibc once at import time (Linux only).
# ctypes.util.find_library returns e.g. "libc.so.6" on glibc systems,
# None on musl/Alpine or non-Linux.
_glibc = None
if sys.platform.startswith("linux"):
    try:
        from ctypes.util import find_library
        _libc_name = find_library("c")
        if _libc_name:
            _candidate = ctypes.CDLL(_libc_name, use_errno=True)
            # Probe for malloc_trim — musl libc doesn't have it
            if hasattr(_candidate, "malloc_trim"):
                _glibc = _candidate
    except Exception:
        pass


def has_malloc_trim() -> bool:
    """Return True if malloc_trim is available on this platform."""
    return _glibc is not None


def release_memory_to_os() -> None:
    """Best-effort platform-specific memory release.

    - Linux (glibc): malloc_trim(0) — returns free heap pages to OS
    - Windows: EmptyWorkingSet — trims working set
    - macOS: no reliable equivalent (madvise is per-mapping, not useful here)
    - musl/other: silently skipped
    """
    try:
        if _glibc is not None:
            # Returns 1 if pages were released, 0 if nothing to trim
            ret = _glibc.malloc_trim(0)
            logger.debug("malloc_trim: %s", "trimmed" if ret else "no-op")
        elif sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            handle = kernel32.GetCurrentProcess()
            psapi.EmptyWorkingSet(handle)
            logger.debug("EmptyWorkingSet called")
        # macOS / other: no action
    except Exception:
        pass
