# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sleep_inhibitor.py

"""Sleep inhibitor for gameplay sessions.

Prevents system sleep/suspend while a game is running.
Linux: gdbus (freedesktop portal) with systemd-inhibit fallback.
Windows/macOS: no-op (future).
"""

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class SleepInhibitor:
    """Context manager that prevents system sleep during gameplay.

    Usage:
        with SleepInhibitor():
            # game process runs here
            process.wait()
    """

    def __init__(self):
        self._handle: Optional[str] = None
        self._systemd_proc: Optional[subprocess.Popen] = None
        self._active = False

    def __enter__(self) -> "SleepInhibitor":
        if platform.system() != "Linux":
            return self

        if self._acquire_dbus():
            self._active = True
        else:
            logger.debug("gdbus inhibit failed, systemd-inhibit will be "
                         "used as process prefix if available")
        return self

    def __exit__(self, *exc) -> None:
        if self._active:
            self._release_dbus()
            self._active = False

    def _acquire_dbus(self) -> bool:
        """Acquire sleep inhibit via freedesktop portal (gdbus)."""
        try:
            result = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "-d", "org.freedesktop.portal.Desktop",
                    "-o", "/org/freedesktop/portal/desktop",
                    "-m", "org.freedesktop.portal.Inhibit.Inhibit",
                    "", "8",
                    '{"reason": <"Game running">}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse object path from output like "('/org/freedesktop/portal/desktop/request/...',)"
                output = result.stdout.strip()
                if "'" in output:
                    self._handle = output.split("'")[1]
                    logger.debug("Sleep inhibit acquired: %s", self._handle)
                    return True
            logger.debug("gdbus inhibit returned: rc=%d, out=%s, err=%s",
                         result.returncode, result.stdout.strip(), result.stderr.strip())
        except FileNotFoundError:
            logger.debug("gdbus not found")
        except subprocess.TimeoutExpired:
            logger.debug("gdbus inhibit timed out")
        except Exception as e:
            logger.debug("gdbus inhibit failed: %s", e)
        return False

    def _release_dbus(self) -> None:
        """Release the freedesktop portal inhibit."""
        if not self._handle:
            return
        try:
            subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "-d", "org.freedesktop.portal.Desktop",
                    "-o", self._handle,
                    "-m", "org.freedesktop.portal.Request.Close",
                ],
                capture_output=True,
                timeout=5,
            )
            logger.debug("Sleep inhibit released: %s", self._handle)
        except Exception as e:
            logger.debug("Failed to release sleep inhibit: %s", e)
        finally:
            self._handle = None

    @staticmethod
    def get_systemd_inhibit_prefix() -> list:
        """Return command prefix for systemd-inhibit wrapping.

        Used as a fallback when gdbus portal is unavailable.
        The caller prepends this to the game command.

        Returns:
            Command prefix list, or empty list if unavailable.
        """
        try:
            result = subprocess.run(
                ["systemd-inhibit", "--help"],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                return [
                    "systemd-inhibit",
                    "--what=idle",
                    "--who=luducat",
                    "--why=Game running",
                    "--",
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return []
