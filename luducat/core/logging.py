# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# logging.py

"""Colorized logging formatter for luducat.

Provides color-coded console output based on logger name and log level.
Includes a filter to redact secrets (API keys) from third-party log messages.
Provides a memory-backed handler for the in-app Developer Console.
"""

import logging
import re
from collections import deque
from typing import Callable, Optional

# ANSI color codes
_RESET = "\033[0m"
_DIM = "\033[2m"
_YELLOW = "\033[93m"
_RED = "\033[91m"


class ColoredFormatter(logging.Formatter):
    """Formatter that colors log output by level.

    DEBUG is dimmed, WARNING is yellow, ERROR/CRITICAL is red.
    INFO is uncolored.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)

        if record.levelno >= logging.ERROR:
            return f"{_RED}{message}{_RESET}"
        elif record.levelno >= logging.WARNING:
            return f"{_YELLOW}{message}{_RESET}"
        elif record.levelno <= logging.DEBUG:
            return f"{_DIM}{message}{_RESET}"
        return message


# Regex to match key=<hex value> in URL query parameters
_SECRET_PARAM_RE = re.compile(r"(\bkey=)[0-9A-Fa-f]{16,}")


class SecretRedactingFilter(logging.Filter):
    """Filter that redacts API keys from log messages.

    Targets urllib3/requests debug logs that include full URLs with
    query parameters like ``key=3AF60EB21CA204A0736FD93022EC93A6``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            # urllib3 uses %-formatting with args tuple
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(
                    _SECRET_PARAM_RE.sub(r"\1[REDACTED]", str(a))
                    if isinstance(a, str) and "key=" in a
                    else a
                    for a in args
                )
        elif isinstance(record.msg, str) and "key=" in record.msg:
            record.msg = _SECRET_PARAM_RE.sub(r"\1[REDACTED]", record.msg)
        return True


# ── Memory-backed handler for in-app log viewer ─────────────────────

class MemoryLogHandler(logging.Handler):
    """Buffered handler that stores log records in a capped deque.

    Installed at module level in main.py alongside StreamHandler.
    The UI log viewer reads from .records when opened.
    """

    def __init__(self, capacity: int = 5000):
        super().__init__()
        self.records: deque[logging.LogRecord] = deque(maxlen=capacity)
        self._callback: Optional[Callable] = None

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if self._callback:
            try:
                self._callback(record)
            except Exception:
                pass  # Never let callback errors break logging

    def set_callback(self, cb: Optional[Callable]) -> None:
        self._callback = cb


_memory_handler: Optional[MemoryLogHandler] = None


def get_memory_handler() -> Optional[MemoryLogHandler]:
    """Return the global MemoryLogHandler instance, or None if not installed."""
    return _memory_handler


def install_memory_handler(capacity: int = 5000) -> MemoryLogHandler:
    """Create and return a MemoryLogHandler (singleton)."""
    global _memory_handler
    _memory_handler = MemoryLogHandler(capacity)
    return _memory_handler
