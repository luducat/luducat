# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# network_monitor.py

"""Network connectivity monitor for online/offline mode.

Detects network availability via DNS resolution and provides a
centralized online/offline state that all components can query.

The mode persists across restarts via config. When online, a background
timer triggers a non-blocking DNS check in a worker thread every 30 seconds.
When connectivity is lost, the app auto-switches to offline mode. Recovery
requires manual user action (clicking the status bar indicator).
"""

import logging
import socket
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

logger = logging.getLogger(__name__)

# DNS hosts for connectivity check — neutral, stable hosts
_DNS_CHECK_HOST = "iana.org"
_DNS_CHECK_HOST_FALLBACK = "w3.org"

# DNS resolution timeout in seconds (system default can be 30s+)
_DNS_TIMEOUT_SECS = 3

# Polling interval in milliseconds
_POLL_INTERVAL_MS = 30_000  # 30 seconds

# DNS result cache (avoids hammering resolver if check_now() called frequently)
_DNS_CACHE_TTL = 10.0  # seconds
_dns_cache_result: Optional[bool] = None
_dns_cache_time: float = 0.0


def _dns_resolve_with_timeout(host: str) -> bool:
    """Try to resolve a single hostname with a bounded timeout."""
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_DNS_TIMEOUT_SECS)
        socket.getaddrinfo(host, None)
        return True
    except (socket.gaierror, socket.timeout, OSError):
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


def _dns_available(host: str = _DNS_CHECK_HOST) -> bool:
    """Check if the system DNS resolver can resolve a hostname.

    Tries the primary host first, then a fallback. Results are cached
    for 10 seconds to avoid redundant lookups. Each attempt is bounded
    by a 3-second timeout.
    """
    global _dns_cache_result, _dns_cache_time
    now = time.monotonic()
    if _dns_cache_result is not None and (now - _dns_cache_time) < _DNS_CACHE_TTL:
        return _dns_cache_result

    result = _dns_resolve_with_timeout(host)
    if not result and host == _DNS_CHECK_HOST:
        # Primary failed — try fallback host before declaring offline
        result = _dns_resolve_with_timeout(_DNS_CHECK_HOST_FALLBACK)

    _dns_cache_result = result
    _dns_cache_time = now
    return result


class _DnsCheckWorker(QThread):
    """Runs DNS check off the main thread to avoid blocking the UI."""
    result_ready = Signal(bool)

    def run(self) -> None:
        self.result_ready.emit(_dns_available())


class NetworkMonitor(QObject):
    """Centralized network status monitor.

    Signals:
        status_changed(bool): Emitted when mode changes. True=online, False=offline.
        connectivity_restored: Emitted when DNS succeeds while in offline mode.
            Does NOT auto-switch — user must manually go online.
    """

    status_changed = Signal(bool)
    connectivity_restored = Signal()

    def __init__(self, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._config = config
        self._dns_worker: Optional[_DnsCheckWorker] = None

        # Restore persisted mode (default: online)
        saved_mode = self._config.get("network.mode", "online")
        self._online = saved_mode == "online"

        # Background polling timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

        if self._online:
            # Online mode: start polling, do initial check after 1 second
            self._timer.start(_POLL_INTERVAL_MS)
            QTimer.singleShot(1000, self._poll)
        # Offline mode: no polling until user switches back

        logger.info(f"Network monitor initialized: {'online' if self._online else 'offline'}")

    @property
    def is_online(self) -> bool:
        """Current network mode (thread-safe read)."""
        return self._online

    def check_now(self) -> bool:
        """Synchronous connectivity check. Returns True if DNS resolves."""
        return _dns_available()

    def set_mode(self, online: bool) -> None:
        """Manually set online/offline mode.

        Called when user clicks the status bar indicator.
        Persists the mode to config and controls polling.
        """
        if self._online == online:
            return

        self._online = online
        self._config.set("network.mode", "online" if online else "offline")
        self._config.save()

        if online:
            # Resuming online: start polling
            self._timer.start(_POLL_INTERVAL_MS)
            logger.info("Switched to online mode")
        else:
            # Going offline: stop polling
            self._timer.stop()
            logger.info("Switched to offline mode")

        self.status_changed.emit(online)

    def _poll(self) -> None:
        """Trigger non-blocking DNS check in a worker thread."""
        if self._dns_worker is not None:
            return  # Previous check still running

        worker = _DnsCheckWorker()
        self._dns_worker = worker
        worker.result_ready.connect(self._on_dns_result)
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    def _on_worker_finished(self) -> None:
        """Clear worker reference after thread completes."""
        self._dns_worker = None

    def _on_dns_result(self, has_connectivity: bool) -> None:
        """Handle DNS check result (runs on main thread via signal)."""
        if self._online and not has_connectivity:
            # Lost connectivity → auto-switch to offline
            self._online = False
            self._timer.stop()
            self._config.set("network.mode", "offline")
            self._config.save()
            logger.warning("Network connectivity lost — switching to offline mode")
            self.status_changed.emit(False)

        elif not self._online and has_connectivity:
            # Connectivity restored while offline — notify but don't auto-switch
            logger.info("Network connectivity detected while in offline mode")
            self.connectivity_restored.emit()


# Module-level singleton
_monitor: Optional[NetworkMonitor] = None


def get_network_monitor(config=None, parent: Optional[QObject] = None) -> NetworkMonitor:
    """Get or create the global NetworkMonitor instance.

    Must be called with config on first invocation (typically from MainWindow).
    Subsequent calls can omit config.
    """
    global _monitor
    if _monitor is None:
        if config is None:
            raise RuntimeError("NetworkMonitor not initialized — call with config first")
        _monitor = NetworkMonitor(config, parent)
    return _monitor
