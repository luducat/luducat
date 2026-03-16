# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# plugin_config.py

"""Plugin configuration dialog for luducat

Dynamic dialog that generates UI from plugin's settings_schema.
Handles credential storage via system keyring.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal, QThread, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QSpinBox,
    QDoubleSpinBox,
    QComboBox,
    QPushButton,
    QDialogButtonBox,
    QMessageBox,
    QGroupBox,
    QFrame,
    QWidget,
    QProgressDialog,
    QFileDialog,
)

from ...core.config import Config
from ...core.credentials import CredentialManager
from ...core.plugin_manager import PluginManager
from ...utils.icons import load_tinted_icon
from luducat.utils.style_helpers import set_status_property

logger = logging.getLogger(__name__)


class SyncCancelled(Exception):
    """Raised when sync is cancelled by user"""
    pass


class _BridgePairWorker(QThread):
    """Background worker for bridge pairing (Playnite runner)."""

    status_update = Signal(str)
    code_display = Signal(str)
    finished = Signal(bool)

    def __init__(self, plugin, host: str, port: int):
        super().__init__()
        self._plugin = plugin
        self._host = host
        self._port = port

    def run(self):
        try:
            ok = self._plugin.pair_bridge(
                self._host, self._port,
                on_status=self.status_update.emit,
                on_code_display=self.code_display.emit,
            )
        except Exception as e:
            logger.debug("Bridge pairing error: %s", e)
            self.status_update.emit(_("Error: {}").format(e))
            ok = False
        self.finished.emit(ok)


class _BridgeTestWorker(QThread):
    """Background worker for bridge connection test."""

    finished = Signal(dict)

    def __init__(self, plugin):
        super().__init__()
        self._plugin = plugin

    def run(self):
        try:
            result = self._plugin.test_bridge_connection()
        except Exception as e:
            logger.debug("Bridge test error: %s", e)
            result = {"status": "error", "detail": str(e)}
        self.finished.emit(result)


class _BridgeCodeDialog(QDialog):
    """Display-only dialog showing the 6-digit pairing code with countdown.

    No Confirm button — the Playnite user enters the code on their side.
    Cancel closes the dialog; the worker times out naturally.
    """

    def __init__(self, code: str, timeout_secs: int = 120, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Pairing Code"))
        self.setMinimumWidth(320)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowContextHelpButtonHint
        )

        self._remaining = timeout_secs

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Instruction
        hint = QLabel(_("Enter this code in Playnite:"))
        hint.setAlignment(Qt.AlignCenter)
        hint.setObjectName("hintLabel")
        layout.addWidget(hint)

        # Large code display with letter spacing
        display = code[:3] + "\u2009" + code[3:] if len(code) == 6 else code
        self._code_label = QLabel(display)
        self._code_label.setAlignment(Qt.AlignCenter)
        base_size = QApplication.instance().font().pointSize()
        code_font = self._code_label.font()
        code_font.setPointSize(base_size + 18)
        from PySide6.QtGui import QFont as _QFont
        code_font.setLetterSpacing(_QFont.SpacingType.AbsoluteSpacing, 6)
        code_font.setBold(True)
        self._code_label.setFont(code_font)
        self._code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._code_label)

        # Countdown
        self._timer_label = QLabel()
        self._timer_label.setAlignment(Qt.AlignCenter)
        self._timer_label.setObjectName("hintLabel")
        self._update_countdown()
        layout.addWidget(self._timer_label)

        # Cancel button
        btn_box = QDialogButtonBox(QDialogButtonBox.Cancel)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Tick every second
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _update_countdown(self):
        mins, secs = divmod(self._remaining, 60)
        self._timer_label.setText(
            _("Expires in {}:{:02d}").format(mins, secs)
        )

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self._timer_label.setText(_("Expired"))
            return
        self._update_countdown()

    def close_with_result(self, success: bool):
        """Close the dialog, briefly flashing success/failure."""
        self._timer.stop()
        if success:
            self._timer_label.setText(_("Paired successfully"))
        else:
            self._timer_label.setText(_("Pairing failed"))
        # Brief flash before closing. Use a parented timer so it's safe
        # if the dialog is destroyed early.
        close_timer = QTimer(self)
        close_timer.setSingleShot(True)
        close_timer.setInterval(800)
        close_timer.timeout.connect(self.accept if success else self.reject)
        close_timer.start()


# ── Shared reset logic ──────────────────────────────────────────────

def reset_plugin_data(
    parent_widget,
    plugin_name: str,
    display_name: str,
    plugin_types: list,
    config,
    status_label,
    store_data_reset_signal,
    get_game_service_fn,
    get_plugin_instance_fn,
    collect_image_urls_fn=None,
) -> None:
    """Reset plugin data with backup offer and optional image purge.

    Shared between PluginConfigDialog and any custom plugin dialogs
    (e.g. EpicConfigDialog) so all plugins go through the same safe
    reset path.

    Args:
        parent_widget: Parent QWidget for dialogs
        plugin_name: Internal plugin name (e.g. "epic")
        display_name: User-facing name (e.g. "Epic Games")
        plugin_types: List of plugin types (e.g. ["store"])
        config: Config instance (for backup)
        status_label: QLabel to update with result
        store_data_reset_signal: Signal to emit on success
        get_game_service_fn: Callable returning GameService or None
        get_plugin_instance_fn: Callable returning plugin instance or None
        collect_image_urls_fn: Callable returning list of URLs, or None
            for default StoreGame.metadata_json collection
    """
    is_store = "store" in plugin_types
    game_service = get_game_service_fn()

    # Count impact for store plugins
    total_store_games = 0
    exclusive_count = 0
    if is_store and game_service:
        from ...core.database import StoreGame
        session = game_service.database.get_session()
        total_store_games = session.query(StoreGame).filter_by(
            store_name=plugin_name,
        ).count()
        exclusive_count = game_service.count_store_exclusive_games(plugin_name)

    shared_count = total_store_games - exclusive_count

    # Build confirmation dialog
    dialog = QDialog(parent_widget)
    dialog.setWindowTitle(_("Reset {} Data").format(display_name))
    dialog.setMinimumWidth(420)
    dlg_layout = QVBoxLayout(dialog)
    dlg_layout.setSpacing(12)

    # Impact message
    if is_store and total_store_games > 0:
        impact_text = _("This will remove {count} {name} game entries "
                        "and clear the {name} catalog database.").format(
            count=total_store_games, name=display_name
        )
        if exclusive_count > 0:
            impact_text += (
                "\n\n" + _("{count} game(s) exist only in {name} "
                           "and will be removed if they have no user data.").format(
                    count=exclusive_count, name=display_name
                )
            )
        if shared_count > 0:
            impact_text += (
                "\n" + _("{count} game(s) also in other stores will keep "
                         "their other store entries.").format(count=shared_count)
            )
    else:
        impact_text = _("This will clear the {} plugin database.").format(display_name)

    impact_label = QLabel(impact_text)
    impact_label.setWordWrap(True)
    dlg_layout.addWidget(impact_label)

    # Backup checkbox
    chk_backup = QCheckBox(_("Create a backup before resetting"))
    chk_backup.setChecked(True)
    dlg_layout.addWidget(chk_backup)

    # Purge checkbox
    chk_purge = QCheckBox(_("Also purge cached image assets"))
    chk_purge.setChecked(False)
    dlg_layout.addWidget(chk_purge)

    # Warning
    warning = QLabel(_("This cannot be undone."))
    warning.setObjectName("hintLabel")
    dlg_layout.addWidget(warning)

    # Buttons
    btn_box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok |
        QDialogButtonBox.StandardButton.Cancel
    )
    btn_box.button(QDialogButtonBox.StandardButton.Ok).setText(_("Reset"))
    btn_box.accepted.connect(dialog.accept)
    btn_box.rejected.connect(dialog.reject)
    dlg_layout.addWidget(btn_box)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return

    do_backup = chk_backup.isChecked()
    do_purge = chk_purge.isChecked()

    # Backup first if requested (before cursor — may show dialog)
    error_msg = None
    if do_backup:
        from ...core.backup_manager import create_backup
        success, msg, _assets = create_backup(config)
        if not success:
            reply = QMessageBox.warning(
                parent_widget,
                _("Backup Failed"),
                _("Backup failed: {}\n\nContinue with reset anyway?").format(msg),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        else:
            logger.info(f"Pre-reset backup created: {msg}")

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        # Collect image URLs before deleting data (if purge requested)
        image_urls = []
        if do_purge:
            if collect_image_urls_fn:
                image_urls = collect_image_urls_fn()
            elif is_store and game_service:
                image_urls = _collect_store_image_urls(game_service, plugin_name)

        # Remove store data from main database
        stats = {}
        if is_store and game_service:
            stats = game_service.remove_store_data(plugin_name)

        # Clear plugin's catalog/enrichment database
        plugin = get_plugin_instance_fn()
        if plugin and hasattr(plugin, 'get_database_path'):
            db_path = plugin.get_database_path()
            plugin.close()
            if db_path.exists():
                db_path.unlink()
                logger.info(f"Deleted plugin DB: {db_path}")

        # Purge cached images
        purge_count = 0
        purge_bytes = 0
        if do_purge and image_urls:
            purge_count, purge_bytes = _purge_cached_images(plugin_name, image_urls)

        # Build status message
        parts = []
        if is_store:
            removed = stats.get("store_games_removed", 0)
            orphans = stats.get("orphans_removed", 0)
            preserved = stats.get("orphans_preserved", 0)
            parts.append(_("{} entries removed").format(removed))
            if orphans:
                parts.append(_("{} orphaned games cleaned up").format(orphans))
            if preserved:
                parts.append(_("{} preserved (have user data)").format(preserved))
        else:
            parts.append(_("database cleared"))

        if purge_count > 0:
            if purge_bytes >= 1024 * 1024:
                size_str = f"{purge_bytes / (1024 * 1024):.1f} MB"
            else:
                size_str = f"{purge_bytes / 1024:.0f} KB"
            parts.append(_("{count} cached images purged ({size} freed)").format(
                count=purge_count, size=size_str
            ))

        status_label.setText(
            _("{name} data reset: {details}").format(
                name=display_name, details=", ".join(parts)
            )
        )

        store_data_reset_signal.emit(plugin_name)

    except Exception as e:
        logger.error(f"Failed to reset plugin data: {e}")
        error_msg = str(e)
    finally:
        QApplication.restoreOverrideCursor()

    if error_msg:
        QMessageBox.critical(
            parent_widget, _("Error"),
            _("Failed to reset plugin data:\n{}").format(error_msg)
        )


def _collect_store_image_urls(game_service, plugin_name: str) -> list:
    """Collect image URLs from StoreGame.metadata_json for cache purging."""
    urls = []
    try:
        from ...core.database import StoreGame
        session = game_service.database.get_session()
        store_games = session.query(StoreGame).filter_by(
            store_name=plugin_name,
        ).all()
        for sg in store_games:
            meta = sg.metadata_json or {}
            for key in ("cover", "cover_url", "header_url", "background_url", "hero"):
                url = meta.get(key)
                if url and isinstance(url, str):
                    urls.append(url)
            screenshots = meta.get("screenshots", [])
            if isinstance(screenshots, list):
                urls.extend(s for s in screenshots if isinstance(s, str) and s)
    except Exception as e:
        logger.warning(f"Error collecting image URLs for purge: {e}")
    logger.info(f"Collected {len(urls)} image URLs for {plugin_name} purge")
    return urls


def _purge_cached_images(plugin_name: str, urls: list) -> tuple:
    """Delete cached image files matching the given URLs.

    Returns:
        Tuple of (files_deleted, bytes_freed)
    """
    import hashlib
    from pathlib import Path
    from urllib.parse import urlparse
    from ...core.config import get_cache_dir

    cache_base = get_cache_dir()
    cache_dirs = [
        cache_base / "covers",
        cache_base / "screenshots",
        cache_base / "heroes",
    ]

    files_deleted = 0
    bytes_freed = 0

    for url in urls:
        try:
            url_hash = hashlib.sha256(url.encode()).hexdigest()
            parsed = urlparse(url)
            ext = Path(parsed.path).suffix or ".jpg"
            filename = f"{url_hash}{ext}"

            for cache_dir in cache_dirs:
                cache_file = cache_dir / filename
                if cache_file.exists():
                    bytes_freed += cache_file.stat().st_size
                    cache_file.unlink()
                    files_deleted += 1
        except Exception:
            continue

    logger.info(
        f"Image purge for {plugin_name}: "
        f"{files_deleted} files deleted, {bytes_freed} bytes freed"
    )
    return files_deleted, bytes_freed


class IgdbSyncWorker(QThread):
    """Background worker for IGDB sync operations"""

    # Signals: message, current, total, success_count
    progress = Signal(str, int, int, int)
    finished = Signal(dict)  # result stats
    error = Signal(str)

    def __init__(self, plugin, failed_only: bool = True, title_lookup=None, api=None, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.failed_only = failed_only
        self.title_lookup = title_lookup
        self._api = api
        self._cancelled = False

    def run(self):
        """Execute sync in background thread"""
        try:
            def progress_callback(message: str, current: int, total: int, success_count: int):
                # Raise exception to stop sync if cancelled
                if self._cancelled:
                    raise SyncCancelled()
                self.progress.emit(message, current, total, success_count)

            if self.failed_only:
                result = self.plugin.sync_failed_matches(
                    progress_callback,
                    title_lookup=self.title_lookup
                )
            else:
                result = {"total": 0, "success": 0, "failed": 0}

            if not self._cancelled:
                self.finished.emit(result)

        except SyncCancelled:
            logger.info("IGDB sync cancelled by user")
            self.finished.emit({"total": 0, "success": 0, "failed": 0, "cancelled": True})
        except Exception as e:
            if not self._cancelled:
                logger.exception("IGDB sync worker error")
                self.error.emit(str(e))
        finally:
            # Reset cancel state so the API can be reused
            if self._api:
                self._api.reset_cancel()

    def cancel(self):
        """Request cancellation — sets flag and interrupts API waits/requests"""
        self._cancelled = True
        if self._api:
            self._api.cancel()


class PcgwSyncWorker(QThread):
    """Background worker for PCGamingWiki sync operations"""

    # Signals: message, current, total, success_count
    progress = Signal(str, int, int, int)
    finished = Signal(dict)  # result stats
    error = Signal(str)

    def __init__(self, plugin, mode: str = "retry_failed", api=None, parent=None):
        """
        Args:
            plugin: PcgwProvider instance
            mode: "retry_failed" or "refresh_all"
            api: PcgwApi instance for cancel signaling
            parent: Parent QObject
        """
        super().__init__(parent)
        self.plugin = plugin
        self.mode = mode
        self._api = api
        self._cancelled = False

    def run(self):
        """Execute sync in background thread"""
        try:
            def progress_callback(
                message: str, current: int, total: int, success_count: int
            ):
                if self._cancelled:
                    raise SyncCancelled()
                self.progress.emit(message, current, total, success_count)

            if self.mode == "retry_failed":
                result = self.plugin.sync_failed_matches(progress_callback)
            elif self.mode == "refresh_all":
                result = self.plugin.sync_refresh_all(progress_callback)
            else:
                result = {"total": 0, "success": 0, "failed": 0}

            if not self._cancelled:
                self.finished.emit(result)

        except SyncCancelled:
            logger.info("PCGamingWiki sync cancelled by user")
            self.finished.emit(
                {"total": 0, "success": 0, "failed": 0, "cancelled": True}
            )
        except Exception as e:
            if not self._cancelled:
                logger.exception("PCGamingWiki sync worker error")
                self.error.emit(str(e))
        finally:
            if self._api:
                self._api.reset_cancel()

    def cancel(self):
        """Request cancellation — sets flag and interrupts API batch loop"""
        self._cancelled = True
        if self._api:
            self._api.cancel()


class ProtonDbSyncWorker(QThread):
    """Background worker for ProtonDB sync operations"""

    # Signals: message, current, total, success_count
    progress = Signal(str, int, int, int)
    finished = Signal(dict)  # result stats
    error = Signal(str)

    def __init__(self, plugin, mode: str = "retry_failed", api=None, parent=None):
        """
        Args:
            plugin: ProtonDbProvider instance
            mode: "retry_failed" or "refresh_all"
            api: ProtonDbApi instance for cancel signaling
            parent: Parent QObject
        """
        super().__init__(parent)
        self.plugin = plugin
        self.mode = mode
        self._api = api
        self._cancelled = False

    def run(self):
        """Execute sync in background thread"""
        try:
            def progress_callback(
                message: str, current: int, total: int, success_count: int
            ):
                if self._cancelled:
                    raise SyncCancelled()
                self.progress.emit(message, current, total, success_count)

            if self.mode == "retry_failed":
                result = self.plugin.sync_failed_matches(progress_callback)
            elif self.mode == "refresh_all":
                result = self.plugin.sync_refresh_all(progress_callback)
            else:
                result = {"total": 0, "success": 0, "failed": 0}

            if not self._cancelled:
                self.finished.emit(result)

        except SyncCancelled:
            logger.info("ProtonDB sync cancelled by user")
            self.finished.emit(
                {"total": 0, "success": 0, "failed": 0, "cancelled": True}
            )
        except Exception as e:
            if not self._cancelled:
                logger.exception("ProtonDB sync worker error")
                self.error.emit(str(e))
        finally:
            if self._api:
                self._api.reset_cancel()

    def cancel(self):
        """Request cancellation — sets flag and interrupts API rate limit"""
        self._cancelled = True
        if self._api:
            self._api.cancel()


class PluginConfigDialog(QDialog):
    """Configuration dialog for a specific plugin

    Generates UI dynamically from plugin's settings_schema in plugin.json.
    Handles both regular settings (stored in config) and secrets (stored in keyring).
    """

    # Emitted when connection status changes (plugin_name, is_authenticated)
    connection_status_changed = Signal(str, bool)
    # Emitted when store data is reset (plugin_name)
    store_data_reset = Signal(str)

    def __init__(
        self,
        plugin_name: str,
        config: Config,
        plugin_manager: PluginManager,
        credentials: CredentialManager,
        parent: Optional[QDialog] = None,
    ):
        super().__init__(parent)

        self.plugin_name = plugin_name
        self.config = config
        self.plugin_manager = plugin_manager
        self.credentials = credentials

        # Get plugin metadata
        discovered = plugin_manager.get_discovered_plugins()
        self.metadata = discovered.get(plugin_name)

        if not self.metadata:
            raise ValueError(f"Plugin not found: {plugin_name}")

        self.setWindowTitle(_("Configure {}").format(self.metadata.display_name))
        capabilities = self.metadata.capabilities or {}
        plugin_types = self.metadata.plugin_types or []
        if capabilities.get("two_column_settings"):
            self.setMinimumWidth(750)
        else:
            self.setMinimumWidth(620)
        # Uniform minimum heights per plugin type
        if "store" in plugin_types:
            self.setMinimumHeight(400)
        elif "runner" in plugin_types:
            self.setMinimumHeight(350)

        # Track input widgets
        self._widgets: Dict[str, Any] = {}
        # Track action buttons by action id
        self._action_buttons: Dict[str, QPushButton] = {}

        self._setup_ui()
        self._load_settings()
        self.adjustSize()

    # --- Lifecycle ---

    def closeEvent(self, event) -> None:
        """Cancel running sync workers before closing."""
        self._stop_sync_worker()
        super().closeEvent(event)

    def reject(self) -> None:
        """Ensure workers are stopped on Escape/close."""
        self._stop_sync_worker()
        super().reject()

    def _stop_sync_worker(self) -> None:
        """Cancel and wait for any running sync worker."""
        if hasattr(self, '_sync_worker') and self._sync_worker:
            # Disconnect signals first — prevents delivery to dead slots
            try:
                self._sync_worker.progress.disconnect()
                self._sync_worker.finished.disconnect()
                self._sync_worker.error.disconnect()
            except RuntimeError:
                pass  # Already disconnected

            if self._sync_worker.isRunning():
                self._sync_worker.cancel()
                if not self._sync_worker.wait(10000):
                    logger.warning("Sync worker didn't stop in 10s, terminating")
                    self._sync_worker.terminate()
                    self._sync_worker.wait(2000)
            self._sync_worker.deleteLater()
            self._sync_worker = None
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        # Header with category icon
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)

        # Resolve icon: plugin's own icon, or category icon from plugin type
        icon_name = None
        if self.metadata.icon and self.metadata.plugin_dir:
            icon_path = self.metadata.plugin_dir / self.metadata.icon
            if icon_path.exists():
                icon_name = str(icon_path)
        if not icon_name:
            _TYPE_ICONS = {
                "store": "plug-store.svg",
                "metadata": "plug-metadata.svg",
                "runner": "plug-runner.svg",
                "platform": "plug-platform.svg",
            }
            for ptype in (self.metadata.plugin_types or ["store"]):
                if ptype in _TYPE_ICONS:
                    icon_name = _TYPE_ICONS[ptype]
                    break

        if icon_name:
            icon = load_tinted_icon(icon_name, size=32)
            icon_label = QLabel()
            icon_label.setPixmap(icon.pixmap(32, 32))
            icon_label.setFixedSize(32, 32)
            header_layout.addWidget(icon_label)

        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        title = QLabel(f"<b>{self.metadata.display_name}</b>  v{self.metadata.version}")
        base_size = QApplication.instance().font().pointSize()
        title_font = title.font()
        title_font.setPointSize(base_size + 3)
        title.setFont(title_font)
        header_text.addWidget(title)

        desc = QLabel(self.metadata.description)
        desc.setWordWrap(True)
        desc.setObjectName("dialogDescription")
        header_text.addWidget(desc)

        header_layout.addLayout(header_text, 1)
        layout.addLayout(header_layout)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # Check development status
        capabilities = self.metadata.capabilities or {}
        plugin_types = self.metadata.plugin_types or []
        is_in_dev = capabilities.get("status") in ("stub", "in_development")
        is_runner = "runner" in plugin_types

        # "In development" banner for platform/runner plugins still in development
        if is_in_dev:
            dev_msg = QLabel(_("This plugin is in development and not yet fully functional."))
            dev_msg.setObjectName("pluginStatusInDevelopment")
            dev_msg.setWordWrap(True)
            layout.addWidget(dev_msg)

        # Runner plugins get specialized UI
        if is_runner and not is_in_dev:
            self._build_runner_settings(layout)
        else:
            self._build_generic_settings(layout, capabilities, is_in_dev)

        # Bridge runners handle their own status/buttons — skip generic section
        if not getattr(self, '_has_bridge_settings', False):
            # Build action buttons from collected actions
            actions = self._collect_actions()

            # Separate by group
            auth_actions = [a for a in actions if a.group == "auth"]
            data_actions = [a for a in actions if a.group == "data"]
            bottom_actions = [a for a in actions if a.group == "bottom"]

            # Create all action buttons (so they exist in _action_buttons)
            for action in auth_actions + data_actions:
                self._create_action_button(action)

            # Status row: [status_label ............ Refresh]
            self.status_label = QLabel("")
            self.status_label.setWordWrap(True)

            status_row = QHBoxLayout()
            status_row.addWidget(self.status_label, 1)

            refresh_btn = self._action_buttons.get("test_connection")
            if refresh_btn:
                status_row.addWidget(refresh_btn)
            layout.addLayout(status_row)

            # Show initial connection status for all plugins
            self._update_connection_status()

            # Update requires_auth buttons after status is known
            self._update_auth_dependent_buttons()

            # Combined action row: data buttons left, auth buttons right
            remaining_auth = [
                a for a in auth_actions
                if a.id != "test_connection"
            ]
            if data_actions or remaining_auth:
                action_row = QHBoxLayout()
                for action in data_actions:
                    btn = self._action_buttons.get(action.id)
                    if btn:
                        action_row.addWidget(btn)
                action_row.addStretch()
                for action in remaining_auth:
                    btn = self._action_buttons.get(action.id)
                    if btn:
                        action_row.addWidget(btn)
                # Ensure login/logout visibility is correct
                if self._action_buttons.get("login") or self._action_buttons.get("logout"):
                    self._update_oauth_buttons()
                layout.addLayout(action_row)

            layout.addStretch()
        else:
            bottom_actions = []

        # Bottom row: [Reset...] ... [OK] [Cancel]
        bottom_layout = QHBoxLayout()

        if getattr(self, '_has_bridge_settings', False):
            reset_btn = QPushButton(_("Reset..."))
            reset_btn.clicked.connect(self._on_bridge_reset)
            bottom_layout.addWidget(reset_btn)

        for action in bottom_actions:
            btn = self._create_action_button(action)
            bottom_layout.addWidget(btn)

        bottom_layout.addStretch()

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        bottom_layout.addWidget(button_box)

        layout.addLayout(bottom_layout)

    # ── Settings builders ────────────────────────────────────────────

    def _build_generic_settings(self, layout, capabilities, is_in_dev):
        """Build the standard settings form (non-runner plugins)."""
        schema = self.metadata.settings_schema or {}
        if not schema:
            return  # No settings — hide the group entirely

        # Separate fields into main group vs sectioned groups
        main_fields = {}
        sections: Dict[str, Dict] = {}  # section_title -> {key: field_def}
        for key, field_def in schema.items():
            if not isinstance(field_def, dict):
                continue
            section = field_def.get("section")
            if section:
                sections.setdefault(section, {})[key] = field_def
            else:
                main_fields[key] = field_def

        # Build main settings group (fields without a section)
        if main_fields:
            title = _(self.metadata.settings_title) if self.metadata.settings_title else _("Settings")
            settings_group = QGroupBox(title)
            form_layout = QFormLayout(settings_group)
            form_layout.setSpacing(12)

            if self.metadata.settings_description:
                desc_label = QLabel(_(self.metadata.settings_description))
                desc_label.setObjectName("hintLabel")
                desc_label.setWordWrap(True)
                form_layout.addRow(desc_label)

            use_two_columns = capabilities.get("two_column_settings", False)

            if use_two_columns:
                self._build_two_column_settings(settings_group, form_layout, main_fields)
                layout.addWidget(settings_group)
            else:
                self._add_fields_to_form(form_layout, main_fields, checkbox_left=True)
                layout.addWidget(settings_group)

        # Build separate group boxes for each section
        for section_title, section_fields in sections.items():
            section_group = QGroupBox(_(section_title))
            section_layout = QFormLayout(section_group)
            section_layout.setSpacing(12)

            has_columns = any(
                isinstance(f, dict) and f.get("column")
                for f in section_fields.values()
            )
            if has_columns:
                self._build_columned_section(section_layout, section_fields)
            else:
                self._add_fields_to_form(section_layout, section_fields, checkbox_left=True)
            layout.addWidget(section_group)

    def _add_fields_to_form(self, form_layout, fields, checkbox_left=False):
        """Add field widgets to a QFormLayout."""
        for key, field_def in fields.items():
            widget = self._create_field_widget(key, field_def)
            if not widget:
                continue

            if checkbox_left and field_def.get("type") == "boolean":
                # Checkbox with label text, spanning full row
                widget.setText(_(field_def.get("label", key)))
                form_layout.addRow(widget)
            elif field_def.get("suffix") and field_def.get("type") == "number":
                # Label on its own line, spinner + suffix in an HBox below
                label = _(field_def.get("label", key))
                form_layout.addRow(QLabel(label))
                suffix_layout = QHBoxLayout()
                suffix_layout.setContentsMargins(0, 0, 0, 0)
                suffix_layout.addWidget(widget)
                suffix_layout.addWidget(QLabel(_(field_def["suffix"])))
                suffix_layout.addStretch()
                form_layout.addRow(suffix_layout)
            elif checkbox_left and field_def.get("label_above"):
                # Label on its own line, widget below (spanning)
                label = _(field_def.get("label", key))
                form_layout.addRow(QLabel(label))
                form_layout.addRow(widget)
            else:
                label = _(field_def.get("label", key))
                if field_def.get("required", False):
                    label += " *"
                form_layout.addRow(f"{label}:", widget)

            if "description_link" in field_def:
                link_def = field_def["description_link"]
                link_label = QLabel(
                    '<a href="{url}">{text}</a>'.format(
                        url=link_def["url"],
                        text=_(link_def["text"]),
                    )
                )
                link_label.setObjectName("fieldDescription")
                link_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                link_label.setCursor(Qt.CursorShape.PointingHandCursor)
                link_label.linkActivated.connect(
                    lambda url: __import__(
                        'luducat.utils.browser', fromlist=['open_url']
                    ).open_url(url)
                )
                form_layout.addRow("", link_label)
            elif "description" in field_def:
                use_tooltip = checkbox_left and (
                    field_def.get("type") == "boolean"
                    or field_def.get("suffix")
                )
                if use_tooltip:
                    widget.setToolTip(_(field_def["description"]))
                else:
                    desc_label = QLabel(_(field_def["description"]))
                    desc_label.setObjectName("fieldDescription")
                    desc_label.setWordWrap(True)
                    form_layout.addRow("", desc_label)

    def _build_columned_section(self, form_layout, fields):
        """Build a two-column section layout from fields with 'column' property."""
        left_fields = []
        right_fields = []
        spanning_fields = []

        for key, field_def in fields.items():
            if not isinstance(field_def, dict):
                continue
            col = field_def.get("column")
            if col == "left":
                left_fields.append((key, field_def))
            elif col == "right":
                right_fields.append((key, field_def))
            else:
                spanning_fields.append((key, field_def))

        # Two-column area
        columns_widget = QWidget()
        columns_layout = QHBoxLayout(columns_widget)
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(24)

        for column_fields in (left_fields, right_fields):
            col_widget = QWidget()
            col_form = QFormLayout(col_widget)
            col_form.setContentsMargins(0, 0, 0, 0)
            col_form.setSpacing(8)
            for key, field_def in column_fields:
                widget = self._create_field_widget(key, field_def)
                if widget:
                    label = _(field_def.get("label", key))
                    col_form.addRow(QLabel(label))
                    col_form.addRow(widget)
                    if "description" in field_def:
                        widget.setToolTip(_(field_def["description"]))
            columns_layout.addWidget(col_widget, 1)

        form_layout.addRow(columns_widget)

        # Spanning fields below columns (checkboxes etc.)
        for key, field_def in spanning_fields:
            widget = self._create_field_widget(key, field_def)
            if widget:
                if field_def.get("type") == "boolean":
                    widget.setText(_(field_def.get("label", key)))
                    form_layout.addRow(widget)
                else:
                    label = _(field_def.get("label", key))
                    form_layout.addRow(f"{label}:", widget)
                if "description" in field_def:
                    widget.setToolTip(_(field_def["description"]))

    def _build_runner_settings(self, layout):
        """Build runner-specific settings UI with specialized group boxes."""
        capabilities = self.metadata.capabilities or {}

        # Supported libraries line
        supported_stores = capabilities.get("supported_stores", [])
        if supported_stores:
            store_display_names = []
            discovered = self.plugin_manager.get_discovered_plugins()
            for store_name in supported_stores:
                store_meta = discovered.get(store_name)
                if store_meta:
                    store_display_names.append(store_meta.display_name)
                else:
                    store_display_names.append(store_name.upper())

            libraries_label = QLabel(
                _("Supported libraries: {}").format(", ".join(store_display_names))
            )
            libraries_label.setObjectName("dialogDescription")
            layout.addWidget(libraries_label)

        # Launcher Path group box — use Source+Path pattern when plugin
        # provides source detection, otherwise fall back to schema-driven UI
        plugin = self._get_plugin_instance()

        has_bridge = plugin and getattr(plugin, "has_bridge_pairing", False)
        if has_bridge:
            self._build_bridge_settings(layout, plugin)
            return

        has_source_detection = (
            plugin
            and hasattr(plugin, "get_heroic_sources_with_custom")
        )

        if has_source_detection:
            self._build_runner_source_group(layout, plugin)
        else:
            schema = self.metadata.settings_schema or {}
            if schema:
                launcher_group = QGroupBox(_("Launcher Path"))
                launcher_form = QFormLayout(launcher_group)
                launcher_form.setSpacing(12)

                for key, field_def in schema.items():
                    if not isinstance(field_def, dict):
                        continue
                    widget = self._create_field_widget(key, field_def)
                    if widget:
                        label = _(field_def.get("label", key))
                        launcher_form.addRow(f"{label}:", widget)

                        if "description" in field_def:
                            desc_label = QLabel(_(field_def["description"]))
                            desc_label.setObjectName("fieldDescription")
                            desc_label.setWordWrap(True)
                            launcher_form.addRow("", desc_label)

                layout.addWidget(launcher_group)

        # Helper Application group box (JSON-driven from plugin.json)
        helper_tool = getattr(self.metadata, 'runner_config', {})
        # Check raw plugin.json for helper_tool
        if self.metadata.plugin_dir:
            import json
            pjson_path = self.metadata.plugin_dir / "plugin.json"
            if pjson_path.exists():
                try:
                    with open(pjson_path, "r") as f:
                        pjson = json.load(f)
                    helper_tool = pjson.get("helper_tool")
                except Exception:
                    helper_tool = None

        if helper_tool and isinstance(helper_tool, dict):
            self._build_helper_tool_group(layout, helper_tool)

    def _build_runner_source_group(self, layout, plugin):
        """Build Source+Path group for runner plugins with source detection.

        Layout matches the Helper Application pattern:
          Source:  [ AppImage                          v ]
          Path:   [/home/.../Heroic-2.18.1.AppImage    ] [Browse...]

        Used by Heroic runner (and any future runner with get_*_sources_with_custom).
        """
        source_names = {
            "system": _("System (PATH)"),
            "appimage": _("AppImage"),
            "flatpak": _("Flatpak"),
            "bundle": _("Application Bundle"),
            "registry": _("Installed"),
            "custom": _("Custom..."),
        }

        group = QGroupBox(_("Launcher Path"))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(12)

        # Get sources
        self._runner_source_combo = None
        self._runner_path_field = None
        self._runner_browse_btn = None
        self._runner_sources_data = []

        self._runner_sources_data = plugin.get_heroic_sources_with_custom()

        form = QFormLayout()
        form.setSpacing(8)

        # Source combo
        self._runner_source_combo = QComboBox()
        non_custom = [
            s for s in self._runner_sources_data if s["source"] != "custom"
        ]
        for src in non_custom:
            label = source_names.get(src["source"], src["source"].capitalize())
            if src.get("version"):
                label += f"  v{src['version']}"
            self._runner_source_combo.addItem(label, src["source"])

        # Separator before Custom
        if non_custom:
            self._runner_source_combo.insertSeparator(
                self._runner_source_combo.count()
            )

        self._runner_source_combo.addItem(source_names["custom"], "custom")
        form.addRow(_("Source:"), self._runner_source_combo)

        # Path field with Browse button
        self._runner_path_field = QLineEdit()
        self._runner_path_field.setReadOnly(True)
        if self._runner_sources_data:
            self._runner_path_field.setText(
                self._runner_sources_data[0].get("path", "")
            )

        self._runner_browse_btn = QPushButton(_("Browse..."))
        self._runner_browse_btn.setMinimumWidth(80)
        self._runner_browse_btn.setVisible(False)
        self._runner_browse_btn.clicked.connect(self._on_runner_browse)

        path_container = QWidget()
        path_layout = QHBoxLayout(path_container)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(4)
        path_layout.addWidget(self._runner_path_field, 1)
        path_layout.addWidget(self._runner_browse_btn)
        form.addRow(_("Path:"), path_container)

        group_layout.addLayout(form)

        # Wire dropdown change
        self._runner_source_combo.currentIndexChanged.connect(
            self._on_runner_source_changed
        )

        layout.addWidget(group)

    def _on_runner_source_changed(self, index: int):
        """Handle runner source dropdown selection change."""
        if not self._runner_source_combo:
            return

        source_id = self._runner_source_combo.currentData()
        is_custom = source_id == "custom"

        self._runner_path_field.setReadOnly(not is_custom)
        self._runner_browse_btn.setVisible(is_custom)

        if is_custom:
            plugin = self._get_plugin_instance()
            custom_path = ""
            if plugin:
                custom_path = plugin.get_setting("heroic_path", "")
            self._runner_path_field.setText(custom_path)
            self._runner_path_field.setPlaceholderText(
                _("/path/to/binary")
            )
        else:
            self._runner_path_field.setPlaceholderText("")
            for src in self._runner_sources_data:
                if src["source"] == source_id:
                    self._runner_path_field.setText(src.get("path", ""))
                    break

    def _on_runner_browse(self):
        """Browse for a custom runner binary path."""
        current = self._runner_path_field.text() or ""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Select Binary"),
            current,
            _("All Files (*)"),
        )
        if path:
            self._runner_path_field.setText(path)

    # ── Bridge pairing (Playnite) ─────────────────────────────────────

    def _build_bridge_settings(self, layout, plugin):
        """Build the Connection group for bridge-paired runners (Playnite).

        Contains host/port fields, status indicator, and Pair/Unpair buttons.
        """
        self._has_bridge_settings = True
        self.setMinimumWidth(max(self.minimumWidth(), 744))
        schema = self.metadata.settings_schema or {}

        group = QGroupBox(_("Connection"))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(8)

        # Host field
        host_def = schema.get("bridge_host", {
            "type": "string", "label": "Bridge host",
            "default": "127.0.0.1",
            "description": "IP address of the Playnite bridge",
        })
        host_widget = self._create_field_widget("bridge_host", host_def)
        if host_widget:
            form.addRow(_("Bridge host:"), host_widget)
            host_desc = QLabel(_(host_def.get("description", "")))
            host_desc.setObjectName("fieldDescription")
            host_desc.setWordWrap(True)
            form.addRow("", host_desc)

        # Port field
        port_def = schema.get("bridge_port", {
            "type": "integer", "label": "Bridge port",
            "default": 39817, "min": 1, "max": 65535,
            "description": "TCP port the bridge listens on",
        })
        port_widget = self._create_field_widget("bridge_port", port_def)
        if port_widget:
            form.addRow(_("Bridge port:"), port_widget)
            port_desc = QLabel(_(port_def.get("description", "")))
            port_desc.setObjectName("fieldDescription")
            port_desc.setWordWrap(True)
            form.addRow("", port_desc)

        group_layout.addLayout(form)

        # Buttons row inside the group
        btn_row = QHBoxLayout()
        self._bridge_pair_btn = QPushButton(_("Pair..."))
        self._bridge_test_btn = QPushButton(_("Test"))
        self._bridge_unpair_btn = QPushButton(_("Unpair"))
        btn_row.addWidget(self._bridge_pair_btn)
        btn_row.addWidget(self._bridge_test_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._bridge_unpair_btn)
        group_layout.addLayout(btn_row)

        layout.addWidget(group)

        # Status label — below the group, above OK/Cancel
        self._bridge_status_label = QLabel()
        self._bridge_status_label.setObjectName("bridgeStatus")
        self._bridge_status_label.setWordWrap(True)
        layout.addWidget(self._bridge_status_label)

        # Keep plugin ref for button handlers
        self._bridge_plugin = plugin
        self._bridge_pair_worker = None
        self._bridge_test_worker = None
        self._bridge_code_dialog = None

        # Wire buttons
        self._bridge_pair_btn.clicked.connect(self._on_bridge_pair)
        self._bridge_test_btn.clicked.connect(self._on_bridge_test)
        self._bridge_unpair_btn.clicked.connect(self._on_bridge_unpair)

        # Initial status
        self._refresh_bridge_status()

    def _refresh_bridge_status(self, status_override=None):
        """Query the plugin for bridge status and update the label.

        Args:
            status_override: Optional dict to use instead of querying plugin
                             (used after test_bridge_connection).
        """
        plugin = self._bridge_plugin
        status = status_override or plugin.get_bridge_status()
        state = status.get("status", "not_configured")
        detail = status.get("detail", "")

        if state == "not_configured":
            text = _("Not configured")
            css_status = "warning"
        elif state == "paired":
            text = _("Paired ({})").format(detail) if detail else _("Paired (not connected)")
            css_status = ""
        elif state == "connected":
            text = _("Connected")
            css_status = "success"
        elif state == "error":
            text = _("Error: {}").format(detail)
            css_status = "error"
        else:
            text = state
            css_status = ""

        self._bridge_status_label.setText("\u25cf " + text)
        set_status_property(self._bridge_status_label, css_status)

        is_paired = state in ("paired", "connected")
        self._bridge_pair_btn.setEnabled(not is_paired)
        self._bridge_test_btn.setEnabled(True)
        self._bridge_unpair_btn.setEnabled(is_paired)

    def _on_bridge_pair(self):
        """Start the pairing flow in a background thread."""
        # Read current host/port from widgets
        host_entry = self._widgets.get("bridge_host")
        port_entry = self._widgets.get("bridge_port")
        host = host_entry[1].text() if host_entry else "127.0.0.1"
        port = port_entry[1].value() if port_entry else 39817

        # Save current host/port before pairing so the plugin uses them
        try:
            self._save_settings()
        except Exception as e:
            self._bridge_status_label.setText(
                "\u25cf " + _("Error: {}").format(e)
            )
            set_status_property(self._bridge_status_label, "error")
            return

        self._bridge_pair_btn.setEnabled(False)
        self._bridge_test_btn.setEnabled(False)
        self._bridge_unpair_btn.setEnabled(False)

        self._bridge_pair_worker = _BridgePairWorker(
            self._bridge_plugin, host, port
        )
        self._bridge_pair_worker.status_update.connect(
            self._on_bridge_pair_status
        )
        self._bridge_pair_worker.code_display.connect(
            self._on_bridge_code_display
        )
        self._bridge_pair_worker.finished.connect(
            self._on_bridge_pair_finished
        )
        self._bridge_pair_worker.start()

    def _on_bridge_pair_status(self, msg: str):
        """Update status label during pairing."""
        self._bridge_status_label.setText("\u25cf " + msg)
        css = "error" if msg.startswith(_("Error:")) or msg.startswith("Error:") else ""
        set_status_property(self._bridge_status_label, css)

        if "Already paired" in msg:
            QMessageBox.information(
                self,
                _("Bridge Already Paired"),
                _("The Playnite bridge is already paired with a different client "
                  "(e.g. a previous luducat session or test run).\n\n"
                  "This luducat instance has no local credentials for that pairing, "
                  "so it cannot connect.\n\n"
                  "To fix this, open Playnite and unpair or reset the bridge plugin "
                  "there, then try pairing again from here."),
            )

    def _on_bridge_code_display(self, code: str):
        """Show the pairing code dialog when the code becomes available."""
        self._bridge_code_dialog = _BridgeCodeDialog(
            code, timeout_secs=120, parent=self
        )
        self._bridge_code_dialog.show()

    def _on_bridge_pair_finished(self, success: bool):
        """Handle pairing completion."""
        self._bridge_pair_worker = None

        # Close code dialog if open — clean up ref after close completes
        if self._bridge_code_dialog is not None:
            dlg = self._bridge_code_dialog
            self._bridge_code_dialog = None
            dlg.finished.connect(dlg.deleteLater)
            dlg.close_with_result(success)

        if success:
            # Pairing just succeeded — skip _refresh_bridge_status() which
            # would do a blocking connect+ping that races with bridge cleanup.
            self._bridge_status_label.setText(
                "\u25cf " + _("Paired successfully")
            )
            set_status_property(self._bridge_status_label, "success")
            self._bridge_pair_btn.setEnabled(False)
            self._bridge_test_btn.setEnabled(True)
            self._bridge_unpair_btn.setEnabled(True)
        else:
            # Keep the error message from _on_bridge_pair_status visible.
            # Just re-enable buttons based on current bridge state.
            status = self._bridge_plugin.get_bridge_status()
            is_paired = status.get("status") in ("paired", "connected")
            self._bridge_pair_btn.setEnabled(not is_paired)
            self._bridge_test_btn.setEnabled(True)
            self._bridge_unpair_btn.setEnabled(is_paired)
            set_status_property(self._bridge_status_label, "error")

    def _on_bridge_test(self):
        """Test bridge connectivity in a background thread."""
        self._bridge_test_btn.setEnabled(False)
        self._bridge_status_label.setText("\u25cf " + _("Testing..."))
        set_status_property(self._bridge_status_label, "")

        self._bridge_test_worker = _BridgeTestWorker(self._bridge_plugin)
        self._bridge_test_worker.finished.connect(self._on_bridge_test_finished)
        self._bridge_test_worker.start()

    def _on_bridge_test_finished(self, result: dict):
        """Handle test completion."""
        self._bridge_test_worker = None
        self._refresh_bridge_status(status_override=result)

        state = result.get("status", "")

        if state == "connected":
            QMessageBox.information(
                self,
                _("Connection Successful"),
                _("Connected to the Playnite bridge."),
            )
        elif result.get("not_paired"):
            if result.get("reachable"):
                msg = _("The bridge is reachable but not paired with this client.\n\n"
                        "Unpair in Playnite first if a previous pairing exists, "
                        "then pair again from here.")
            else:
                msg = _("Cannot reach the bridge ({}).").format(
                    result.get("detail", _("unreachable")))
            QMessageBox.warning(
                self,
                _("Not Paired"),
                msg,
            )
        elif state == "error":
            QMessageBox.warning(
                self,
                _("Connection Failed"),
                _("Could not connect to the bridge: {}").format(
                    result.get("detail", _("unknown error"))),
            )

    def _on_bridge_unpair(self):
        """Unpair the bridge (synchronous — just deletes files)."""
        self._bridge_plugin.unpair_bridge()
        self._refresh_bridge_status()

    def _on_bridge_reset(self):
        """Reset bridge settings to defaults, warning about pairing loss."""
        reply = QMessageBox.warning(
            self,
            _("Reset Bridge Settings"),
            _("This will reset all connection settings to their defaults "
              "and disconnect the current pairing.\n\n"
              "You will need to pair again after resetting.\n\n"
              "Continue?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Unpair if currently paired
        try:
            self._bridge_plugin.unpair_bridge()
        except Exception:
            pass

        # Reset widget values to defaults from schema
        schema = self.metadata.settings_schema or {}
        for key, field_def in schema.items():
            if not isinstance(field_def, dict):
                continue
            default = field_def.get("default")
            entry = self._widgets.get(key)
            if entry and default is not None:
                _field_type, widget, _fdef = entry
                if isinstance(widget, QLineEdit):
                    widget.setText(str(default))
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(default))

        self._save_settings()
        self._refresh_bridge_status()

    def _build_helper_tool_group(self, layout, helper_tool):
        """Build the Helper Application group box.

        Layout:
          Source:  [ luducat (managed) v0.20.34      v ]
          Path:   [/home/.../bin/legendary             ]  (readonly)
          [x] Auto-download Legendary if not present
          [x] Auto-update Legendary on startup
                                        [ Download ]

        Args:
            layout: Parent layout to add the group box to
            helper_tool: Dict from plugin.json "helper_tool" section
        """
        group = QGroupBox(_("Helper Application"))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(12)

        settings = helper_tool.get("settings", {})
        self._helper_tool_name = helper_tool.get("name", "Helper")

        # --- Source dropdown + Path field ---
        self._helper_source_combo = None
        self._helper_path_field = None
        self._helper_download_btn = None
        self._helper_sources_data = []
        self._download_worker = None

        plugin = self._get_plugin_instance()

        if plugin and hasattr(plugin, "get_legendary_sources_with_custom"):
            self._helper_sources_data = plugin.get_legendary_sources_with_custom()
        elif plugin and hasattr(plugin, "get_legendary_sources"):
            self._helper_sources_data = list(plugin.get_legendary_sources())
            custom_path = plugin.get_setting("helper_custom_path", "") if plugin else ""
            self._helper_sources_data.append({
                "source": "custom",
                "path": custom_path,
                "version": "",
            })

        if self._helper_sources_data:
            form = QFormLayout()
            form.setSpacing(8)

            # Source combo
            self._helper_source_combo = QComboBox()
            non_custom = [
                s for s in self._helper_sources_data if s["source"] != "custom"
            ]
            for src in non_custom:
                label = self._format_helper_source_label(src)
                self._helper_source_combo.addItem(label, src["source"])

            # Separator before Custom
            if non_custom:
                self._helper_source_combo.insertSeparator(
                    self._helper_source_combo.count()
                )

            self._helper_source_combo.addItem(_("Custom..."), "custom")

            form.addRow(_("Source:"), self._helper_source_combo)

            # Path field with Browse button
            self._helper_path_field = QLineEdit()
            self._helper_path_field.setReadOnly(True)
            if self._helper_sources_data:
                self._helper_path_field.setText(
                    self._helper_sources_data[0].get("path", "")
                )
            self._helper_browse_btn = QPushButton(_("Browse..."))
            self._helper_browse_btn.setMinimumWidth(80)
            self._helper_browse_btn.setVisible(False)
            self._helper_browse_btn.clicked.connect(self._on_helper_browse)

            path_container = QWidget()
            path_layout = QHBoxLayout(path_container)
            path_layout.setContentsMargins(0, 0, 0, 0)
            path_layout.setSpacing(4)
            path_layout.addWidget(self._helper_path_field, 1)
            path_layout.addWidget(self._helper_browse_btn)
            form.addRow(_("Path:"), path_container)

            group_layout.addLayout(form)

            # Wire dropdown change
            self._helper_source_combo.currentIndexChanged.connect(
                self._on_helper_source_changed
            )

        # --- Boolean settings (checkboxes) ---
        for key, field_def in settings.items():
            if not isinstance(field_def, dict):
                continue
            if field_def.get("type") != "boolean":
                continue

            prefixed_key = f"helper_{key}"
            checkbox = QCheckBox(_(field_def.get("label", key)))
            checkbox.setChecked(field_def.get("default", False))
            self._widgets[prefixed_key] = ("boolean", checkbox, field_def)
            group_layout.addWidget(checkbox)

        # --- Download button ---
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._helper_download_btn = QPushButton(_("Download"))
        btn_row.addWidget(self._helper_download_btn)
        group_layout.addLayout(btn_row)

        self._helper_download_btn.clicked.connect(self._start_legendary_download)

        layout.addWidget(group)

        # Update button label and path for initial state
        self._update_download_button_label()

    def _on_helper_source_changed(self, index: int):
        """Handle source dropdown selection change."""
        if not self._helper_source_combo:
            return

        source_id = self._helper_source_combo.currentData()
        is_custom = source_id == "custom"

        # Toggle editable path + Browse button for Custom mode
        self._helper_path_field.setReadOnly(not is_custom)
        if hasattr(self, "_helper_browse_btn"):
            self._helper_browse_btn.setVisible(is_custom)

        if is_custom:
            # Pre-fill with stored custom path
            plugin = self._get_plugin_instance()
            custom_path = ""
            if plugin:
                custom_path = plugin.get_setting("helper_custom_path", "")
            self._helper_path_field.setText(custom_path)
            self._helper_path_field.setPlaceholderText(
                _("/path/to/binary")
            )
        else:
            self._helper_path_field.setPlaceholderText("")
            for src in self._helper_sources_data:
                if src["source"] == source_id:
                    self._helper_path_field.setText(src.get("path", ""))
                    break

        self._update_download_button_label()

    def _on_helper_browse(self):
        """Browse for a custom binary path."""
        current = self._helper_path_field.text() or ""
        path, _filter = QFileDialog.getOpenFileName(
            self,
            _("Select Binary"),
            current,
            _("All Files (*)"),
        )
        if path:
            self._helper_path_field.setText(path)

    def _update_download_button_label(self):
        """Update the download button label based on current state."""
        if not self._helper_download_btn:
            return

        has_luducat = any(
            s["source"] == "luducat" for s in self._helper_sources_data
        )

        if not has_luducat:
            self._helper_download_btn.setText(_("Download"))
            self._helper_download_btn.setEnabled(True)
        else:
            # Check if update is available
            plugin = self._get_plugin_instance()
            update_available = False
            if plugin:
                try:
                    mgr = plugin._get_legendary_manager()
                    if plugin._http_client:
                        mgr.set_http_client(plugin._http_client)
                    new_ver = mgr.check_for_updates()
                    if new_ver:
                        update_available = True
                except Exception:
                    pass

            if update_available:
                self._helper_download_btn.setText(_("Update"))
            else:
                self._helper_download_btn.setText(_("Re-Download"))
            self._helper_download_btn.setEnabled(True)

        # Only enable when source is luducat (managed) or no luducat copy yet
        source_id = (
            self._helper_source_combo.currentData()
            if self._helper_source_combo else None
        )
        if source_id and source_id not in ("luducat", "custom"):
            # For non-luducat sources, only enable if no luducat copy exists
            if has_luducat:
                self._helper_download_btn.setEnabled(True)

    def _start_legendary_download(self):
        """Start Legendary download (disabled — Legendary removed in Epic v2.0.0)."""
        QMessageBox.information(
            self,
            _("Download"),
            _("Legendary download is no longer available."),
        )

    def _format_helper_source_label(self, src: dict) -> str:
        """Format a helper source entry for display in combo box.

        Produces labels like "Legendary v0.20.34 (luducat)" where the
        tool name and version come first, source origin in parentheses.
        """
        source_origins = {
            "luducat": _("managed"),
            "heroic": _("Heroic"),
            "system": _("system"),
            "epic_launcher": _("Epic Launcher"),
        }
        tool_name = getattr(self, "_helper_tool_name", "Helper")
        version = src.get("version", "")
        origin = source_origins.get(src["source"], src["source"])

        if version:
            return f"{tool_name} v{version} ({origin})"
        return f"{tool_name} ({origin})"

    def _refresh_helper_sources(self):
        """Refresh the source dropdown after download/update."""
        if not self._helper_source_combo:
            return

        plugin = self._get_plugin_instance()
        if not plugin:
            return

        if hasattr(plugin, "get_legendary_sources_with_custom"):
            self._helper_sources_data = plugin.get_legendary_sources_with_custom()
        elif hasattr(plugin, "get_legendary_sources"):
            self._helper_sources_data = list(plugin.get_legendary_sources())
            custom_path = plugin.get_setting("helper_custom_path", "") if plugin else ""
            self._helper_sources_data.append({
                "source": "custom",
                "path": custom_path,
                "version": "",
            })

        self._helper_source_combo.blockSignals(True)
        self._helper_source_combo.clear()

        non_custom = [
            s for s in self._helper_sources_data if s["source"] != "custom"
        ]
        for src in non_custom:
            label = self._format_helper_source_label(src)
            self._helper_source_combo.addItem(label, src["source"])

        if non_custom:
            self._helper_source_combo.insertSeparator(
                self._helper_source_combo.count()
            )
        self._helper_source_combo.addItem(_("Custom..."), "custom")

        self._helper_source_combo.blockSignals(False)

        # Update path field for first source
        if self._helper_sources_data and self._helper_path_field:
            self._helper_path_field.setText(
                self._helper_sources_data[0].get("path", "")
            )

    # ── Action system ─────────────────────────────────────────────────

    def _collect_actions(self):
        """Collect config dialog actions from all sources.

        Merge priority (highest wins on same id):
        1. Auto-generated from plugin metadata (test, login, help, reset)
        2. JSON-declared config_actions from plugin.json
        3. Programmatic get_config_actions() from plugin instance
        """
        from luducat.plugins.base import ConfigAction

        actions_by_id = {}
        capabilities = self.metadata.capabilities or {}
        plugin_types = self.metadata.plugin_types or []
        auth = self.metadata.auth or {}
        is_platform = "platform" in plugin_types
        is_store = "store" in plugin_types
        is_metadata = "metadata" in plugin_types

        is_runner = "runner" in plugin_types

        # --- Auto-generated standard actions ---

        auth_type = (self.metadata.auth or {}).get("type", "")

        # Test connection / Refresh (non-platform, non-runner plugins)
        # Skip for metadata plugins with auth:none (public API, nothing to test)
        if not is_platform and not is_runner:
            if not (is_metadata and auth_type == "none"):
                label = "Refresh" if is_store else "Test Connection"
                actions_by_id["test_connection"] = ConfigAction(
                    id="test_connection",
                    label=label,
                    callback="_test_connection",
                    group="auth",
                )

        # Login/Logout (OAuth/browser cookie plugins)
        if capabilities.get("oauth_login"):
            actions_by_id["login"] = ConfigAction(
                id="login",
                label=_("Login to {}...").format(self.metadata.display_name),
                callback="_open_oauth_login",
                group="auth",
            )
            actions_by_id["logout"] = ConfigAction(
                id="logout",
                label="Logout",
                callback="_oauth_logout",
                group="auth",
            )

        # Help URL (from auth block)
        help_url = auth.get("help_url")
        if help_url:
            actions_by_id["help"] = ConfigAction(
                id="help",
                label=auth.get("help_label", auth.get("help_text", "Get Credentials")),
                callback="_open_help_url",
                group="auth",
                tooltip=help_url,
            )

        # Reset data (store + metadata plugins)
        if is_store or is_metadata:
            actions_by_id["reset_data"] = ConfigAction(
                id="reset_data",
                label="Reset...",
                callback="_reset_store_data",
                group="bottom",
                tooltip="Remove all data from this plugin and clear its database.\n"
                        "User data (favorites, tags) for multi-store games is preserved.",
            )

        # --- JSON-declared config_actions (override auto-generated) ---

        for action_def in (self.metadata.config_actions or []):
            action = ConfigAction(
                id=action_def["id"],
                label=action_def.get("label", action_def["id"]),
                callback=action_def.get("callback", action_def["id"]),
                group=action_def.get("group", "general"),
                icon=action_def.get("icon"),
                requires_auth=action_def.get("requires_auth", False),
                tooltip=action_def.get("tooltip"),
                dialog_class=action_def.get("dialog_class"),
            )
            actions_by_id[action.id] = action

        # --- Programmatic get_config_actions() (highest priority) ---

        try:
            plugin = self._get_plugin_instance()
            if plugin and hasattr(plugin, 'get_config_actions'):
                for action in plugin.get_config_actions():
                    actions_by_id[action.id] = action
        except Exception as e:
            logger.debug(f"Could not get programmatic config actions: {e}")

        return list(actions_by_id.values())

    def _create_action_button(self, action) -> QPushButton:
        """Create a button for a ConfigAction and wire its callback."""
        btn = QPushButton(_(action.label))

        if action.tooltip:
            btn.setToolTip(_(action.tooltip))

        if not action.enabled:
            btn.setEnabled(False)

        # Wire callback
        callback = action.callback
        if callable(callback):
            btn.clicked.connect(lambda checked, cb=callback: cb())
        elif isinstance(callback, str):
            # Dialog method (starts with '_') or plugin method
            if hasattr(self, callback):
                btn.clicked.connect(
                    lambda checked, name=callback: getattr(self, name)()
                )
            else:
                # Plugin method — resolve at click time
                def _make_plugin_callback(method_name):
                    def _call():
                        plugin = self._get_plugin_instance()
                        if plugin and hasattr(plugin, method_name):
                            getattr(plugin, method_name)()
                        else:
                            logger.warning(
                                f"Plugin {self.plugin_name} has no method {method_name}"
                            )
                    return _call
                btn.clicked.connect(
                    lambda checked, cb=_make_plugin_callback(callback): cb()
                )

        # Help URL gets special cursor
        if action.id == "help":
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # Store reference
        self._action_buttons[action.id] = btn
        return btn

    def _update_auth_dependent_buttons(self) -> None:
        """Disable buttons that require authentication when not authenticated."""
        try:
            plugin = self._get_plugin_instance()
            if not plugin or not hasattr(plugin, 'is_authenticated'):
                return

            is_auth = plugin.is_authenticated()
            # Check JSON-declared actions with requires_auth
            for action_def in (self.metadata.config_actions or []):
                if action_def.get("requires_auth", False):
                    btn = self._action_buttons.get(action_def["id"])
                    if btn:
                        btn.setEnabled(is_auth)
        except Exception:
            pass

    def _open_help_url(self) -> None:
        """Open the auth help URL in the default browser."""
        from ...utils.browser import open_url

        auth = self.metadata.auth or {}
        help_url = auth.get("help_url", "")
        if help_url:
            open_url(help_url)

    def _build_two_column_settings(
        self, group: QGroupBox, top_form: QFormLayout, schema: Dict
    ) -> None:
        """Build a two-column settings layout.

        Secret fields go full-width on top. Remaining fields split into
        two side-by-side QFormLayouts.
        """
        # Separate secret fields from regular fields
        secret_keys = []
        regular_keys = []
        for key, field_def in schema.items():
            if not isinstance(field_def, dict):
                continue  # Skip non-dict entries (e.g. JSON Schema "type")
            if field_def.get("secret", False):
                secret_keys.append((key, field_def))
            else:
                regular_keys.append((key, field_def))

        # Add secret fields to the top form (full width)
        for key, field_def in secret_keys:
            widget = self._create_field_widget(key, field_def)
            if widget:
                label = _(field_def.get("label", key))
                if field_def.get("required", False):
                    label += " *"
                top_form.addRow(f"{label}:", widget)

                if "description_link" in field_def:
                    link_def = field_def["description_link"]
                    link_label = QLabel(
                        '<a href="{url}">{text}</a>'.format(
                            url=link_def["url"],
                            text=_(link_def["text"]),
                        )
                    )
                    link_label.setObjectName("fieldDescription")
                    link_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                    link_label.setCursor(Qt.CursorShape.PointingHandCursor)
                    link_label.linkActivated.connect(
                        lambda url: __import__(
                            'luducat.utils.browser', fromlist=['open_url']
                        ).open_url(url)
                    )
                    top_form.addRow("", link_label)
                elif "description" in field_def:
                    desc_label = QLabel(_(field_def["description"]))
                    desc_label.setObjectName("fieldDescription")
                    desc_label.setWordWrap(True)
                    top_form.addRow("", desc_label)

        if not regular_keys:
            return

        # Split remaining fields into two columns
        mid = (len(regular_keys) + 1) // 2  # left gets extra if odd
        left_keys = regular_keys[:mid]
        right_keys = regular_keys[mid:]

        columns_widget = QWidget()
        columns_layout = QHBoxLayout(columns_widget)
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(24)

        for column_keys in (left_keys, right_keys):
            col_widget = QWidget()
            col_form = QFormLayout(col_widget)
            col_form.setContentsMargins(0, 0, 0, 0)
            col_form.setSpacing(8)
            for key, field_def in column_keys:
                widget = self._create_field_widget(key, field_def)
                if widget:
                    label = _(field_def.get("label", key))
                    if field_def.get("required", False):
                        label += " *"
                    col_form.addRow(f"{label}:", widget)

                    if "description_link" in field_def:
                        link_def = field_def["description_link"]
                        link_label = QLabel(
                            '<a href="{url}">{text}</a>'.format(
                                url=link_def["url"],
                                text=_(link_def["text"]),
                            )
                        )
                        link_label.setObjectName("fieldDescription")
                        link_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                        link_label.setCursor(Qt.CursorShape.PointingHandCursor)
                        link_label.linkActivated.connect(
                            lambda url: __import__(
                                'luducat.utils.browser', fromlist=['open_url']
                            ).open_url(url)
                        )
                        col_form.addRow("", link_label)
                    elif "description" in field_def:
                        desc_label = QLabel(_(field_def["description"]))
                        desc_label.setObjectName("fieldDescription")
                        desc_label.setWordWrap(True)
                        col_form.addRow("", desc_label)

            columns_layout.addWidget(col_widget, 1)

        top_form.addRow(columns_widget)

    def _open_author_lists(self) -> None:
        """Open the author scores dialog."""
        from datetime import datetime, timedelta
        from luducat.plugins.steamgriddb.ui.author_dialog import AuthorScoreDialog

        config_key = f"plugins.{self.plugin_name}"

        # Build author_data dict {name: {score, steam_id, hits}}
        raw_scores = self.config.get(f"{config_key}.author_scores", None)

        if raw_scores is None or not isinstance(raw_scores, dict):
            # Migration: convert old blacklist/preferred to new format
            old_blacklist = self.config.get(f"{config_key}.author_blacklist", [])
            old_preferred = self.config.get(f"{config_key}.author_preferred", [])
            author_data = {}
            if isinstance(old_blacklist, list):
                for name in old_blacklist:
                    author_data[str(name)] = {"score": -10, "steam_id": "", "hits": 0}
            if isinstance(old_preferred, list):
                for name in old_preferred:
                    author_data[str(name)] = {"score": 10, "steam_id": "", "hits": 0}
        else:
            # Parse existing config — support both flat and dict-value formats
            author_data = {}
            for k, v in raw_scores.items():
                name = str(k)
                if isinstance(v, dict):
                    author_data[name] = {
                        "score": int(v.get("score", 0)),
                        "steam_id": str(v.get("steam_id", "") or ""),
                        "hits": int(v.get("hits", 0)),
                    }
                elif isinstance(v, (int, float)):
                    author_data[name] = {"score": int(v), "steam_id": "", "hits": 0}

        # Check online status (used for API calls and vanity URL resolution)
        is_online = True
        try:
            from luducat.core.network_monitor import get_network_monitor
            is_online = get_network_monitor().is_online
        except RuntimeError:
            pass  # Monitor not initialized

        # Get per-type asset counts (daily cache via SteamGridDB API)
        plugin = self._get_plugin_instance()
        asset_counts = {}
        asset_count_refresh = None

        if plugin and hasattr(plugin, 'fetch_user_stats'):
            # Build refresh callback that fetches from SteamGridDB API.
            # Accepts current_authors dict from the dialog so newly added
            # authors are included (not just the snapshot from dialog-open).
            # Adds jittered delay between requests to avoid burst patterns.
            def _refresh_counts(usernames=None, current_authors=None):
                import random
                import time

                source = current_authors if current_authors is not None else author_data
                counts = {}
                fetched = 0
                for name, entry in source.items():
                    if usernames is not None and name.lower() not in usernames:
                        continue
                    steam_id = entry.get("steam_id", "")
                    if not steam_id:
                        continue
                    # Jittered delay between requests (0.4–1.2s)
                    if fetched > 0:
                        time.sleep(0.4 + random.random() * 0.8)
                    stats = plugin.fetch_user_stats(steam_id)
                    fetched += 1
                    if stats:
                        counts[name.lower()] = stats
                # Update config cache (only on full refresh)
                if usernames is None:
                    self.config.set(
                        f"{config_key}.asset_counts_cache", counts
                    )
                    self.config.set(
                        f"{config_key}.asset_counts_updated",
                        datetime.now().isoformat(),
                    )
                    self.config.save()
                return counts

            asset_count_refresh = _refresh_counts

            # Check daily cache (24h TTL — API data is authoritative)
            cached_counts = self.config.get(
                f"{config_key}.asset_counts_cache", None
            )
            cache_ts = self.config.get(
                f"{config_key}.asset_counts_updated", None
            )
            cache_fresh = False
            if cached_counts is not None and isinstance(cached_counts, dict) and cache_ts:
                try:
                    updated = datetime.fromisoformat(cache_ts)
                    if datetime.now() - updated < timedelta(hours=24):
                        cache_fresh = True
                except (ValueError, TypeError):
                    pass

            if cache_fresh:
                asset_counts = cached_counts
            elif not is_online:
                # Offline — use stale cache if available
                if cached_counts and isinstance(cached_counts, dict):
                    asset_counts = cached_counts
            else:
                # Stale or missing — refresh from API
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    asset_counts = _refresh_counts(None)
                except Exception as e:
                    logger.warning(f"Failed to load asset counts: {e}")
                finally:
                    QApplication.restoreOverrideCursor()

            # Merge DB steam_ids into author_data — DB IDs are authoritative
            # because they come from SGDB API responses (the actual SGDB account's
            # Steam ID), while vanity URL resolution may give wrong IDs.
            try:
                db = plugin._get_db()
                db_steam_ids = db.get_author_steam_ids()
                for name_lower, steam_id in db_steam_ids.items():
                    for name, entry in author_data.items():
                        if name.lower() == name_lower and steam_id:
                            entry["steam_id"] = steam_id
            except Exception as e:
                logger.debug(f"Failed to query author steam_ids from DB: {e}")

        # Build steam_id_lookup callback from Steam plugin
        steam_id_lookup = self._build_steam_id_lookup()

        # Batch resolve missing steam_ids before opening dialog
        # Skip when offline — vanity URL resolution requires Steam API
        if steam_id_lookup and is_online:
            # TTL check — skip batch resolution if resolved recently (24h)
            resolve_ts = self.config.get(
                f"{config_key}.steam_id_resolved_at", None
            )
            ttl_expired = True
            if resolve_ts:
                try:
                    last_resolved = datetime.fromisoformat(resolve_ts)
                    if datetime.now() - last_resolved < timedelta(hours=24):
                        ttl_expired = False
                except (ValueError, TypeError):
                    pass

            if ttl_expired:
                missing = [
                    name for name, entry in author_data.items()
                    if not entry.get("steam_id")
                ]
                if missing:
                    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                    try:
                        for name in missing:
                            try:
                                resolved = steam_id_lookup(name)
                                if resolved:
                                    author_data[name]["steam_id"] = resolved
                            except Exception:
                                pass
                    finally:
                        QApplication.restoreOverrideCursor()

                    # Persist resolved steam_ids so they survive Cancel
                    self.config.set(f"{config_key}.author_scores", author_data)

                # Record resolution timestamp (even if no missing names)
                self.config.set(
                    f"{config_key}.steam_id_resolved_at",
                    datetime.now().isoformat(),
                )
                self.config.save()

        # Get score colors from theme manager via main window
        score_colors = None
        main_window = self.window()
        if main_window and hasattr(main_window, "_score_colors"):
            score_colors = main_window._score_colors
        # Get http_client from plugin for profile verification
        http_client = plugin.http if plugin else None
        dialog = AuthorScoreDialog(
            author_data, asset_counts,
            steam_id_lookup=steam_id_lookup,
            asset_count_refresh=asset_count_refresh,
            parent=self,
            score_colors=score_colors,
            http_client=http_client,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if not dialog.data_changed():
            return

        new_author_data = dialog.get_author_data()

        # Scores changed — clear resolved covers so they re-resolve with new scoring.
        # The SGDB DB stays intact: get_best_asset() applies author scores at
        # query time against the cached assets, so no nuke is needed.
        error_msg = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # Save new author data dict to config (new format)
            self.config.set(f"{config_key}.author_scores", new_author_data)

            # Also update the plugin's in-memory _settings so that
            # persist_plugin_settings() (called after sync) doesn't
            # merge back the stale pre-dialog snapshot.
            plugin = self._get_plugin_instance()
            if plugin and hasattr(plugin, '_settings'):
                plugin._settings["author_scores"] = new_author_data

            # Re-evaluate cached assets locally (no API calls)
            game_service = self._get_game_service()
            if game_service and plugin:
                game_service.reselect_media_from_plugin(self.plugin_name, plugin)

            # Purge blocked author images from disk + memory cache
            if hasattr(plugin, 'get_blocked_author_asset_urls'):
                purge_urls = plugin.get_blocked_author_asset_urls()
                if purge_urls:
                    from luducat.utils.image_cache import get_cover_cache, get_hero_cache
                    covers = get_cover_cache().remove_urls(purge_urls)
                    heroes = get_hero_cache().remove_urls(purge_urls)
                    logger.info(
                        "Purged %d cover + %d hero files from blocked authors",
                        covers, heroes,
                    )

            self.status_label.setText(
                _("Author scores updated. Covers will re-resolve on next access.")
            )

            # Clean up old config keys if they existed (get returns reference)
            plugin_section = self.config.get(config_key, {})
            if isinstance(plugin_section, dict):
                plugin_section.pop("author_blacklist", None)
                plugin_section.pop("author_preferred", None)

            self.config.save()

            logger.info(
                "Author scores updated: %d entries saved, covers re-selected locally",
                len(new_author_data),
            )

            # Trigger game list reload so covers update
            self.store_data_reset.emit(self.plugin_name)

        except Exception as e:
            logger.error(f"Failed to update author scores: {e}")
            error_msg = str(e)
        finally:
            QApplication.restoreOverrideCursor()

        if error_msg:
            QMessageBox.critical(
                self, _("Error"), _("Failed to update author scores:\n{}").format(error_msg)
            )

    def _build_steam_id_lookup(self):
        """Build a steam_id_lookup callback from the Steam plugin.

        Returns a callable that resolves a vanity name to Steam64 ID,
        or None if the Steam plugin is not available/authenticated.
        """
        try:
            steam_loaded = self.plugin_manager._loaded.get("steam")
            if not steam_loaded or not steam_loaded.instance:
                try:
                    self.plugin_manager.load_plugin("steam")
                    steam_loaded = self.plugin_manager._loaded.get("steam")
                except Exception:
                    return None

            if not steam_loaded or not steam_loaded.instance:
                return None

            steam_plugin = steam_loaded.instance
            if not hasattr(steam_plugin, 'is_authenticated') or not steam_plugin.is_authenticated():
                return None

            if not hasattr(steam_plugin, 'resolve_vanity_url'):
                return None

            return steam_plugin.resolve_vanity_url

        except Exception as e:
            logger.debug(f"Could not build steam_id_lookup: {e}")
            return None

    def _create_field_widget(self, key: str, field_def: Dict) -> Optional[QWidget]:
        """Create appropriate widget for field type"""
        field_type = field_def.get("type", "string")

        if field_type == "string":
            widget = QLineEdit()
            if field_def.get("secret", False):
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            widget.setPlaceholderText(field_def.get("placeholder", ""))
            self._widgets[key] = ("string", widget, field_def)

            # Inline field action button (e.g. "Login..." next to Steam ID)
            field_action = field_def.get("field_action")
            if field_action:
                container = QWidget()
                row = QHBoxLayout(container)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(4)
                row.addWidget(widget, 1)
                btn = QPushButton(_(field_action.get("label", "...")))
                btn.clicked.connect(
                    lambda checked, fa=field_action: self._handle_field_action(fa, key)
                )
                row.addWidget(btn)
                return container

            return widget

        elif field_type == "boolean":
            widget = QCheckBox()
            widget.setChecked(field_def.get("default", False))
            self._widgets[key] = ("boolean", widget, field_def)
            return widget

        elif field_type == "integer":
            widget = QSpinBox()
            widget.setMinimum(field_def.get("min", 0))
            widget.setMaximum(field_def.get("max", 999999))
            widget.setValue(field_def.get("default", 0))
            self._widgets[key] = ("integer", widget, field_def)
            return widget

        elif field_type == "number":
            widget = QDoubleSpinBox()
            widget.setMinimum(field_def.get("min", 0.0))
            widget.setMaximum(field_def.get("max", 999999.0))
            widget.setDecimals(field_def.get("decimals", 2))
            widget.setSingleStep(field_def.get("step", 0.1))
            widget.setValue(field_def.get("default", 0.0))
            self._widgets[key] = ("number", widget, field_def)
            return widget

        elif field_type == "path":
            # Path input with browse and auto-detect buttons
            container = QWidget()
            path_layout = QHBoxLayout(container)
            path_layout.setContentsMargins(0, 0, 0, 0)
            path_layout.setSpacing(4)

            line_edit = QLineEdit()
            line_edit.setPlaceholderText(field_def.get("placeholder", ""))
            path_layout.addWidget(line_edit, 1)

            # Browse button
            btn_browse = QPushButton(_("Browse..."))
            btn_browse.setMinimumWidth(80)
            path_type = field_def.get("path_type", "directory")
            file_filter = field_def.get("file_filter", "")

            def make_browse_handler(le, pt, ff):
                def handler():
                    if pt == "file":
                        path, _filter = QFileDialog.getOpenFileName(
                            self, _("Select File"), le.text() or "", ff
                        )
                    else:
                        path = QFileDialog.getExistingDirectory(
                            self, _("Select Directory"), le.text() or ""
                        )
                    if path:
                        le.setText(path)
                return handler

            btn_browse.clicked.connect(make_browse_handler(line_edit, path_type, file_filter))
            path_layout.addWidget(btn_browse)

            self._widgets[key] = ("path", line_edit, field_def)
            return container

        elif field_type == "choice":
            widget = QComboBox()

            # Check for dynamic choices from plugin method
            dynamic_method = field_def.get("dynamic_choices")
            choices = []

            if dynamic_method:
                # Try to get choices from plugin instance
                try:
                    plugin = self._get_plugin_instance()
                    if plugin and hasattr(plugin, dynamic_method):
                        choices = getattr(plugin, dynamic_method)()
                except Exception as e:
                    logger.debug(f"Could not get dynamic choices: {e}")

            # Fall back to static choices if no dynamic choices
            if not choices:
                choices = field_def.get("choices", [])

            # Populate combo box
            for choice in choices:
                if isinstance(choice, dict):
                    # Dynamic choices format: {value, label, available}
                    label = _(choice.get("label", choice.get("value", "")))
                    value = choice.get("value", "")
                    widget.addItem(label, value)
                else:
                    # Static choices: translate display, keep original as data
                    widget.addItem(_(str(choice)), choice)

            self._widgets[key] = ("choice", widget, field_def)
            return widget

        else:
            logger.warning(f"Unknown field type: {field_type}")
            return None

    def _load_settings(self) -> None:
        """Load current settings into widgets"""
        for key, (field_type, widget, field_def) in self._widgets.items():
            # Check if it's a secret (stored in keyring)
            if field_def.get("secret", False):
                value = self.credentials.get(self.plugin_name, key)
            else:
                # Allow custom config_key override
                config_key = field_def.get(
                    "config_key", f"plugins.{self.plugin_name}.{key}"
                )
                value = self.config.get(config_key, field_def.get("default"))

            if value is None:
                continue

            if field_type == "string" or field_type == "path":
                widget.setText(str(value))
            elif field_type == "boolean":
                widget.setChecked(bool(value))
            elif field_type == "integer":
                widget.setValue(int(value))
            elif field_type == "number":
                widget.setValue(float(value))
            elif field_type == "choice":
                idx = widget.findData(value)
                if idx >= 0:
                    widget.setCurrentIndex(idx)

    def _save_settings(self) -> None:
        """Save settings from widgets"""
        for key, (field_type, widget, field_def) in self._widgets.items():
            # Get value from widget
            if field_type == "string" or field_type == "path":
                value = widget.text()
            elif field_type == "boolean":
                value = widget.isChecked()
            elif field_type == "integer":
                value = widget.value()
            elif field_type == "number":
                value = widget.value()
            elif field_type == "choice":
                value = widget.currentData()
            else:
                continue

            # Check if required
            if field_def.get("required", False):
                if (field_type == "string" or field_type == "path") and not value:
                    raise ValueError(_("{} is required").format(field_def.get('label', key)))

            # Store secret or regular setting
            if field_def.get("secret", False):
                if value:  # Only store non-empty secrets
                    self.credentials.store(self.plugin_name, key, str(value))
                else:
                    # Clear the secret if empty
                    self.credentials.delete(self.plugin_name, key)
            else:
                config_key = field_def.get(
                    "config_key", f"plugins.{self.plugin_name}.{key}"
                )
                self.config.set(config_key, value)

        self.config.save()

        # Update plugin's in-memory _settings so persist_plugin_settings()
        # (called after sync or app close) doesn't overwrite with stale snapshot
        plugin = self._get_plugin_instance()
        if plugin and hasattr(plugin, '_settings'):
            fresh = self.config.get_plugin_settings(self.plugin_name)
            plugin._settings.update(fresh)

    def _prompt_consent_if_needed(self) -> bool:
        """Prompt for local data consent if not yet granted.

        Returns True if consent is now granted (or was already), False
        if the user declined.
        """
        if self.config.get("privacy.local_data_access_consent", False):
            return True

        from ...core.constants import APP_NAME
        reply = QMessageBox.question(
            self,
            _("Local Data Access"),
            _("{app_name} can read browser cookies for store authentication "
              "and import tags, favorites, playtime, and install status "
              "from local game launchers (Steam, GOG Galaxy, Heroic).\n\n"
              "All data stays on your machine.\n\n"
              "Allow access to local data?").format(app_name=APP_NAME),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.config.set("privacy.local_data_access_consent", True)
            self.config.save()
            self.plugin_manager.refresh_all_consent()
            return True
        return False

    def _handle_field_action(self, field_action: dict, field_key: str) -> None:
        """Handle an inline field action button click.

        Calls the plugin method specified in field_action["callback"].
        If the method returns a value, fills the field.
        If it returns None, opens the auth help_url in browser.
        """
        callback_name = field_action.get("callback", "")
        if not callback_name:
            return

        plugin = self._get_plugin_instance()
        result = None
        if plugin and hasattr(plugin, callback_name):
            try:
                result = getattr(plugin, callback_name)()
            except Exception as e:
                logger.debug("Field action %s failed: %s", callback_name, e)

        if result:
            # Fill the field widget
            widget_info = self._widgets.get(field_key)
            if widget_info:
                _ftype, widget, _fdef = widget_info
                if hasattr(widget, 'setText'):
                    widget.setText(str(result))
        else:
            # No result — open help URL if available
            auth = self.metadata.auth or {}
            help_url = auth.get("help_url", "")
            if help_url:
                from ...utils.browser import open_url
                open_url(help_url)

    def _test_connection(self) -> None:
        """Test plugin connection with current settings"""
        # Check if this plugin needs local data consent (browser cookies auth)
        auth_type = (self.metadata.auth or {}).get("type", "") if self.metadata else ""
        if auth_type == "browser_cookies":
            if not self._prompt_consent_if_needed():
                self.status_label.setText(
                    _("Local data access required for browser cookie authentication")
                )
                set_status_property(self.status_label, "warning")
                return

        btn = self._action_buttons.get("test_connection")
        if btn:
            btn.setEnabled(False)
        self.status_label.setText(_("Testing connection..."))
        set_status_property(self.status_label, "")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        try:
            # Save settings first
            self._save_settings()

            # Get plugin instance
            loaded = self.plugin_manager._loaded.get(self.plugin_name)
            if not loaded or not loaded.instance:
                # Try to load it
                try:
                    self.plugin_manager.load_plugin(self.plugin_name)
                    loaded = self.plugin_manager._loaded.get(self.plugin_name)
                except Exception as e:
                    raise Exception(_("Failed to load plugin: {}").format(e)) from e

            if not loaded or not loaded.instance:
                raise Exception(_("Plugin not loaded"))

            # Refresh plugin settings after save (so it sees the new values)
            loaded.instance.set_settings(self.config.get_plugin_settings(self.plugin_name))

            # Run authentication test
            import asyncio

            async def test_auth():
                return await loaded.instance.authenticate()

            # Run in new event loop
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(test_auth())
                if result:
                    # Show detailed status (browser, launcher) via get_auth_status()
                    self._update_connection_status()
                    if not self.status_label.text():
                        self.status_label.setText(_("Connection successful!"))
                        set_status_property(self.status_label, "success", bold=True)
                    self.connection_status_changed.emit(self.plugin_name, True)
                else:
                    self.status_label.setText(_("Authentication failed"))
                    set_status_property(self.status_label, "error")
            finally:
                loop.close()

        except ValueError as e:
            self.status_label.setText(f"{e}")
            set_status_property(self.status_label, "error")
        except Exception as e:
            self.status_label.setText(_("Error: {}").format(e))
            set_status_property(self.status_label, "error")
            logger.exception("Connection test failed")
        finally:
            QApplication.restoreOverrideCursor()
            if btn:
                btn.setEnabled(True)

    def _open_gogdb_import(self) -> None:
        """Open GOGdb import dialog"""
        from .gogdb_import import GogdbImportDialog

        try:
            # Get plugin instance
            loaded = self.plugin_manager._loaded.get(self.plugin_name)
            if not loaded or not loaded.instance:
                # Try to load it
                try:
                    self.plugin_manager.load_plugin(self.plugin_name)
                    loaded = self.plugin_manager._loaded.get(self.plugin_name)
                except Exception as e:
                    QMessageBox.critical(
                        self, _("Error"), _("Failed to load plugin: {}").format(e)
                    )
                    return

            if not loaded or not loaded.instance:
                QMessageBox.critical(self, _("Error"), _("Plugin not loaded"))
                return

            # Open import dialog
            dialog = GogdbImportDialog(loaded.instance, self)
            dialog.exec_()

        except Exception as e:
            QMessageBox.critical(self, _("Error"), _("Failed to open import dialog: {}").format(e))
            logger.exception("GOGdb import dialog error")

    def _open_igdb_sync(self) -> None:
        """Open IGDB sync dialog"""
        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                QMessageBox.critical(self, _("Error"), _("IGDB plugin not loaded"))
                return

            # Get current stats
            stats = plugin.get_sync_stats()
            total = stats.get("total", 0)
            matched = stats.get("matched", 0)
            failed = stats.get("failed", 0)

            # Build message
            msg = _("IGDB Match Statistics:") + "\n\n"
            msg += _("  Total store entries: {}").format(total) + "\n"
            msg += _("  Successfully matched: {}").format(matched) + "\n"
            msg += _("  Failed matches: {}").format(failed) + "\n\n"
            msg += _("What would you like to do?")

            # Create dialog with custom buttons
            dialog = QMessageBox(self)
            dialog.setWindowTitle(_("IGDB Sync"))
            dialog.setText(msg)
            dialog.setIcon(QMessageBox.Icon.Question)

            btn_failed = dialog.addButton(
                _("Retry Failed ({})").format(failed), QMessageBox.ButtonRole.AcceptRole
            )
            dialog.addButton(_("Cancel"), QMessageBox.ButtonRole.RejectRole)

            dialog.exec()

            if dialog.clickedButton() == btn_failed:
                self._run_igdb_sync(plugin, failed_only=True)

        except Exception as e:
            QMessageBox.critical(self, _("Error"), _("Failed to open sync dialog: {}").format(e))
            logger.exception("IGDB sync dialog error")

    def _run_igdb_sync(self, plugin, failed_only: bool = True) -> None:
        """Run IGDB sync with progress dialog in background thread"""
        # Create progress dialog
        self._sync_progress = QProgressDialog(
            _("Starting IGDB sync..."), _("Cancel"), 0, 100, self
        )
        self._sync_progress.setWindowTitle(_("IGDB Sync"))
        self._sync_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._sync_progress.setMinimumDuration(0)
        self._sync_progress.setMinimumWidth(400)
        self._sync_progress.setValue(0)
        self._sync_progress.setStyleSheet(
            "QProgressBar { text-align: center; }"
            "QProgressBar::chunk { background-color: palette(highlight); }"
        )

        # Center on parent dialog
        parent_geo = self.geometry()
        progress_size = self._sync_progress.sizeHint()
        x = parent_geo.x() + (parent_geo.width() - progress_size.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - progress_size.height()) // 2
        self._sync_progress.move(x, y)

        # Create title lookup callback using game_service
        title_lookup = self._create_title_lookup()

        # Get API reference for cancel signaling
        api = plugin._get_api() if hasattr(plugin, '_get_api') else None

        # Create worker thread
        self._sync_worker = IgdbSyncWorker(
            plugin, failed_only,
            title_lookup=title_lookup, api=api, parent=self,
        )
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished.connect(self._on_sync_finished)
        self._sync_worker.error.connect(self._on_sync_error)

        # Handle cancel button
        self._sync_progress.canceled.connect(self._on_sync_cancel)

        # Start worker
        self._sync_worker.start()

    def _create_title_lookup(self):
        """Create a title lookup callback for IGDB sync

        Returns a function that looks up the ORIGINAL game title by store_name and store_app_id.
        The original title preserves casing and punctuation (colons, semicolons, etc.)
        which is needed for literal title search fallback.
        Returns None if game_service is not available.
        """
        game_service = self._get_game_service()
        if not game_service:
            return None

        # Get database from game_service
        db = game_service.database

        def lookup_title(store_name: str, store_app_id: str):
            """Look up original title for a store game"""
            from ...core.database import StoreGame, Game
            try:
                session = db.get_session()
                try:
                    # Return original title (not normalized) - search function handles normalization
                    result = (
                        session.query(Game.title)
                        .join(StoreGame, StoreGame.game_id == Game.id)
                        .filter(
                            StoreGame.store_name == store_name,
                            StoreGame.store_app_id == str(store_app_id)
                        )
                        .first()
                    )
                    return result[0] if result else None
                finally:
                    session.close()
            except Exception as e:
                logger.debug(f"Title lookup failed for {store_name}:{store_app_id}: {e}")
                return None

        return lookup_title

    def _on_sync_progress(self, message: str, current: int, total: int, success_count: int) -> None:
        """Handle progress update from worker"""
        if not hasattr(self, '_sync_progress') or self._sync_progress is None:
            return
        percent = int((current / total) * 100) if total > 0 else 0
        self._sync_progress.setValue(percent)
        self._sync_progress.setLabelText(
            _("{message}\n({current}/{total}) - {matches} matches found").format(
                message=message, current=current, total=total, matches=success_count
            )
        )

    def _on_sync_finished(self, result: dict) -> None:
        """Handle sync completion"""
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

        if result.get("cancelled"):
            self.status_label.setText(_("Sync cancelled"))
        else:
            success = result.get("success", 0)
            still_failed = result.get("failed", 0)
            self.status_label.setText(
                _("IGDB sync complete: {success} new matches found, "
                  "{failed} still unmatched").format(success=success, failed=still_failed)
            )

        # Clean up worker
        self._cleanup_sync_worker()

    def _on_sync_error(self, error_msg: str) -> None:
        """Handle sync error"""
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

        QMessageBox.critical(self, _("Sync Error"), _("Sync failed: {}").format(error_msg))

        # Clean up worker
        self._cleanup_sync_worker()

    def _on_sync_cancel(self) -> None:
        """Handle cancel button click"""
        if hasattr(self, '_sync_worker') and self._sync_worker:
            self._sync_worker.cancel()
            # Don't delete here - let finished signal handle cleanup
            # The SyncCancelled exception will cause the thread to stop
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

    def _cleanup_sync_worker(self) -> None:
        """Clean up sync worker thread safely"""
        if hasattr(self, '_sync_worker') and self._sync_worker:
            if self._sync_worker.isRunning():
                # Disconnect signals to prevent delivery to dead slots
                try:
                    self._sync_worker.progress.disconnect()
                    self._sync_worker.finished.disconnect()
                    self._sync_worker.error.disconnect()
                except RuntimeError:
                    pass
                if not self._sync_worker.wait(10000):
                    logger.warning("Sync worker cleanup: didn't stop in 10s, terminating")
                    self._sync_worker.terminate()
                    self._sync_worker.wait(2000)
            self._sync_worker.deleteLater()
            self._sync_worker = None

    # -------------------------------------------------------------------------
    # PCGamingWiki Sync
    # -------------------------------------------------------------------------

    def _open_pcgw_sync(self) -> None:
        """Open PCGamingWiki sync dialog"""
        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                QMessageBox.critical(
                    self, _("Error"), _("PCGamingWiki plugin not loaded")
                )
                return

            # Get current stats
            stats = plugin.get_sync_stats()
            total = stats.get("total", 0)
            matched = stats.get("matched", 0)
            failed = stats.get("failed", 0)

            # Build message
            msg = _("PCGamingWiki Match Statistics:") + "\n\n"
            msg += _("  Total store entries: {}").format(total) + "\n"
            msg += _("  Successfully matched: {}").format(matched) + "\n"
            msg += _("  Failed matches: {}").format(failed) + "\n\n"
            msg += _("What would you like to do?")

            # Create dialog with custom buttons
            dialog = QMessageBox(self)
            dialog.setWindowTitle(_("PCGamingWiki Sync"))
            dialog.setText(msg)
            dialog.setIcon(QMessageBox.Icon.Question)

            btn_failed = dialog.addButton(
                _("Retry Failed ({})").format(failed),
                QMessageBox.ButtonRole.AcceptRole,
            )
            btn_refresh = dialog.addButton(
                _("Refresh All ({})").format(matched),
                QMessageBox.ButtonRole.AcceptRole,
            )
            dialog.addButton(_("Cancel"), QMessageBox.ButtonRole.RejectRole)

            dialog.exec()

            clicked = dialog.clickedButton()
            if clicked == btn_failed:
                self._run_pcgw_sync(plugin, mode="retry_failed")
            elif clicked == btn_refresh:
                self._run_pcgw_sync(plugin, mode="refresh_all")

        except Exception as e:
            QMessageBox.critical(
                self, _("Error"), _("Failed to open sync dialog: {}").format(e)
            )
            logger.exception("PCGamingWiki sync dialog error")

    def _run_pcgw_sync(self, plugin, mode: str = "retry_failed") -> None:
        """Run PCGamingWiki sync with progress dialog in background thread"""
        mode_label = _("Retry Failed") if mode == "retry_failed" else _("Refresh All")

        self._sync_progress = QProgressDialog(
            _("Starting PCGamingWiki {}...").format(mode_label.lower()),
            _("Cancel"), 0, 100, self,
        )
        self._sync_progress.setWindowTitle(_("PCGamingWiki Sync - {}").format(mode_label))
        self._sync_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._sync_progress.setMinimumDuration(0)
        self._sync_progress.setMinimumWidth(400)
        self._sync_progress.setValue(0)
        self._sync_progress.setStyleSheet(
            "QProgressBar { text-align: center; }"
            "QProgressBar::chunk { background-color: palette(highlight); }"
        )

        # Center on parent dialog
        parent_geo = self.geometry()
        progress_size = self._sync_progress.sizeHint()
        x = parent_geo.x() + (parent_geo.width() - progress_size.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - progress_size.height()) // 2
        self._sync_progress.move(x, y)

        # Get API reference for cancel signaling
        api = plugin._get_api() if hasattr(plugin, '_get_api') else None

        # Create worker thread
        self._sync_worker = PcgwSyncWorker(plugin, mode=mode, api=api, parent=self)
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished.connect(self._on_pcgw_sync_finished)
        self._sync_worker.error.connect(self._on_sync_error)

        # Handle cancel button
        self._sync_progress.canceled.connect(self._on_sync_cancel)

        # Start worker
        self._sync_worker.start()

    def _on_pcgw_sync_finished(self, result: dict) -> None:
        """Handle PCGamingWiki sync completion"""
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

        if result.get("cancelled"):
            self.status_label.setText(_("PCGamingWiki sync cancelled"))
        else:
            success = result.get("success", 0)
            still_failed = result.get("failed", 0)
            total = result.get("total", 0)
            self.status_label.setText(
                _("PCGamingWiki sync complete: {success} matches "
                  "({failed} unmatched of {total} total)").format(
                    success=success, failed=still_failed, total=total
                )
            )

        self._cleanup_sync_worker()

    # -------------------------------------------------------------------------
    # ProtonDB Sync
    # -------------------------------------------------------------------------

    def _open_protondb_sync(self) -> None:
        """Open ProtonDB sync dialog"""
        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                QMessageBox.critical(
                    self, _("Error"), _("ProtonDB plugin not loaded")
                )
                return

            # Get current stats
            stats = plugin.get_sync_stats()
            matched = stats.get("matched", 0)
            failed = stats.get("failed", 0)

            # Build message
            msg = _("ProtonDB Rating Statistics:") + "\n\n"
            msg += _("  Rated games: {}").format(matched) + "\n"
            msg += _("  Not found on ProtonDB: {}").format(failed) + "\n\n"
            msg += _("What would you like to do?")

            # Create dialog with custom buttons
            dialog = QMessageBox(self)
            dialog.setWindowTitle(_("ProtonDB Sync"))
            dialog.setText(msg)
            dialog.setIcon(QMessageBox.Icon.Question)

            btn_failed = dialog.addButton(
                _("Retry Not Found ({})").format(failed),
                QMessageBox.ButtonRole.AcceptRole,
            )
            btn_refresh = dialog.addButton(
                _("Refresh All ({})").format(matched),
                QMessageBox.ButtonRole.AcceptRole,
            )
            dialog.addButton(_("Cancel"), QMessageBox.ButtonRole.RejectRole)

            dialog.exec()

            clicked = dialog.clickedButton()
            if clicked == btn_failed:
                self._run_protondb_sync(plugin, mode="retry_failed")
            elif clicked == btn_refresh:
                self._run_protondb_sync(plugin, mode="refresh_all")

        except Exception as e:
            QMessageBox.critical(
                self, _("Error"), _("Failed to open sync dialog: {}").format(e)
            )
            logger.exception("ProtonDB sync dialog error")

    def _run_protondb_sync(self, plugin, mode: str = "retry_failed") -> None:
        """Run ProtonDB sync with progress dialog in background thread"""
        mode_label = _("Retry Not Found") if mode == "retry_failed" else _("Refresh All")

        self._sync_progress = QProgressDialog(
            _("Starting ProtonDB {}...").format(mode_label.lower()),
            _("Cancel"), 0, 100, self,
        )
        self._sync_progress.setWindowTitle(_("ProtonDB Sync - {}").format(mode_label))
        self._sync_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._sync_progress.setMinimumDuration(0)
        self._sync_progress.setMinimumWidth(400)
        self._sync_progress.setValue(0)
        self._sync_progress.setStyleSheet(
            "QProgressBar { text-align: center; }"
            "QProgressBar::chunk { background-color: palette(highlight); }"
        )

        # Center on parent dialog
        parent_geo = self.geometry()
        progress_size = self._sync_progress.sizeHint()
        x = parent_geo.x() + (parent_geo.width() - progress_size.width()) // 2
        y = parent_geo.y() + (parent_geo.height() - progress_size.height()) // 2
        self._sync_progress.move(x, y)

        # Get API reference for cancel signaling
        api = plugin._get_api() if hasattr(plugin, '_get_api') else None

        # Create worker thread
        self._sync_worker = ProtonDbSyncWorker(plugin, mode=mode, api=api, parent=self)
        self._sync_worker.progress.connect(self._on_sync_progress)
        self._sync_worker.finished.connect(self._on_protondb_sync_finished)
        self._sync_worker.error.connect(self._on_sync_error)

        # Handle cancel button
        self._sync_progress.canceled.connect(self._on_sync_cancel)

        # Start worker
        self._sync_worker.start()

    def _on_protondb_sync_finished(self, result: dict) -> None:
        """Handle ProtonDB sync completion"""
        if hasattr(self, '_sync_progress') and self._sync_progress:
            self._sync_progress.close()
            self._sync_progress = None

        if result.get("cancelled"):
            self.status_label.setText(_("ProtonDB sync cancelled"))
        else:
            success = result.get("success", 0)
            still_failed = result.get("failed", 0)
            total = result.get("total", 0)
            self.status_label.setText(
                _("ProtonDB sync complete: {success} updated "
                  "({failed} not found of {total} total)").format(
                    success=success, failed=still_failed, total=total
                )
            )

        self._cleanup_sync_worker()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_plugin_instance(self):
        """Get or load the plugin instance"""
        loaded = self.plugin_manager._loaded.get(self.plugin_name)
        if not loaded or not loaded.instance:
            self.plugin_manager.load_plugin(self.plugin_name)
            loaded = self.plugin_manager._loaded.get(self.plugin_name)
        return loaded.instance if loaded else None

    def _get_game_service(self):
        """Get game_service from parent window chain."""
        parent = self.parent()
        while parent:
            if hasattr(parent, 'game_service'):
                return parent.game_service
            parent = parent.parent()
        return None

    def _reset_store_data(self) -> None:
        """Reset all plugin data with backup and optional image purge."""
        plugin_types = self.metadata.plugin_types or []
        reset_plugin_data(
            parent_widget=self,
            plugin_name=self.plugin_name,
            display_name=self.metadata.display_name,
            plugin_types=plugin_types,
            config=self.config,
            status_label=self.status_label,
            store_data_reset_signal=self.store_data_reset,
            get_game_service_fn=self._get_game_service,
            get_plugin_instance_fn=self._get_plugin_instance,
            collect_image_urls_fn=self._collect_plugin_image_urls,
        )

    def _collect_plugin_image_urls(self) -> list:
        """Collect all image URLs associated with this plugin for cache purging."""
        urls = []
        plugin_types = self.metadata.plugin_types or []

        try:
            if "store" in plugin_types:
                # Collect from StoreGame.metadata_json
                game_service = self._get_game_service()
                if game_service:
                    from ...core.database import StoreGame
                    session = game_service.database.get_session()
                    store_games = session.query(StoreGame).filter_by(
                        store_name=self.plugin_name,
                    ).all()
                    for sg in store_games:
                        meta = sg.metadata_json or {}
                        for key in ("cover_url", "header_url", "background_url"):
                            url = meta.get(key)
                            if url:
                                urls.append(url)
                        screenshots = meta.get("screenshots", [])
                        if isinstance(screenshots, list):
                            urls.extend(s for s in screenshots if isinstance(s, str) and s)

            if "metadata" in plugin_types:
                self._collect_metadata_plugin_urls(urls)

        except Exception as e:
            logger.warning(f"Error collecting image URLs for purge: {e}")

        logger.info(f"Collected {len(urls)} image URLs for {self.plugin_name} purge")
        return urls

    def _collect_metadata_plugin_urls(self, urls: list) -> None:
        """Collect image URLs from metadata plugin databases."""
        import sqlite3

        plugin = self._get_plugin_instance()
        if not plugin or not hasattr(plugin, 'get_database_path'):
            return

        db_path = plugin.get_database_path()
        if not db_path.exists():
            return

        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            if self.plugin_name == "igdb":
                # Cover and background URLs from igdb_games
                for row in cursor.execute(
                    "SELECT cover_url FROM igdb_games WHERE cover_url IS NOT NULL"
                ):
                    urls.append(row[0])
                for row in cursor.execute(
                    "SELECT background_url FROM igdb_games WHERE background_url IS NOT NULL"
                ):
                    urls.append(row[0])
                # Screenshots
                for row in cursor.execute(
                    "SELECT url FROM igdb_screenshots WHERE url IS NOT NULL"
                ):
                    urls.append(row[0])
                # Artworks
                for row in cursor.execute(
                    "SELECT url FROM igdb_artworks WHERE url IS NOT NULL"
                ):
                    urls.append(row[0])

            elif self.plugin_name == "steamgriddb":
                # All asset URLs (grids, heroes, logos)
                for row in cursor.execute(
                    "SELECT url FROM sgdb_assets WHERE url IS NOT NULL"
                ):
                    urls.append(row[0])
                for row in cursor.execute(
                    "SELECT thumb FROM sgdb_assets WHERE thumb IS NOT NULL"
                ):
                    urls.append(row[0])

            # PCGamingWiki and ProtonDB have no images — skip

            conn.close()
        except Exception as e:
            logger.warning(f"Error reading {self.plugin_name} DB for image URLs: {e}")

    def _update_connection_status(self) -> None:
        """Update connection status display for all plugins"""
        # Skip status for auth:none plugins (public API, nothing to display)
        auth_type = (self.metadata.auth or {}).get("type", "")
        if auth_type == "none":
            return

        # Runner plugins without auth don't need connection status
        plugin_types = self.metadata.plugin_types or []
        if "runner" in plugin_types:
            return

        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                self.status_label.setText(_("Not connected"))
                set_status_property(self.status_label, "")
                return

            # Use get_auth_status if available (provides detailed info)
            if hasattr(plugin, 'get_auth_status') and callable(plugin.get_auth_status):
                is_auth, status_msg = plugin.get_auth_status()
                self.status_label.setText(status_msg)
                set_status_property(self.status_label, "success" if is_auth else "")
            elif hasattr(plugin, 'is_authenticated') and callable(plugin.is_authenticated):
                is_auth = plugin.is_authenticated()
                if is_auth:
                    self.status_label.setText(
                        _("Connected to {}").format(
                            self.metadata.display_name
                        )
                    )
                    set_status_property(self.status_label, "success")
                else:
                    self.status_label.setText(_("Not connected"))
                    set_status_property(self.status_label, "")
        except Exception as e:
            logger.debug("Could not get connection status: %s", e, exc_info=True)
            self.status_label.setText(_("Not connected"))
            set_status_property(self.status_label, "")

    def _update_oauth_buttons(self) -> None:
        """Update Login/Logout button visibility based on auth status"""
        btn_login = self._action_buttons.get("login")
        btn_logout = self._action_buttons.get("logout")
        if not btn_login and not btn_logout:
            return

        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                if btn_login:
                    btn_login.setVisible(True)
                if btn_logout:
                    btn_logout.setVisible(False)
                return

            # Check auth status (guard against bool attributes)
            if hasattr(plugin, 'is_authenticated') and callable(plugin.is_authenticated):
                is_auth = plugin.is_authenticated()
                if btn_login:
                    btn_login.setVisible(not is_auth)
                if btn_logout:
                    btn_logout.setVisible(is_auth)
            else:
                if btn_login:
                    btn_login.setVisible(True)
                if btn_logout:
                    btn_logout.setVisible(False)

            # Update connection status display
            self._update_connection_status()

        except Exception as e:
            logger.debug(f"Could not get auth status: {e}")
            if btn_login:
                btn_login.setVisible(True)
            if btn_logout:
                btn_logout.setVisible(False)

    def _open_oauth_login(self) -> None:
        """Open login dialog (browser cookie-based)"""
        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                QMessageBox.critical(self, _("Error"), _("Plugin not loaded"))
                return

            # Get login config from plugin
            if hasattr(plugin, 'get_login_config'):
                login_config = plugin.get_login_config()
            elif hasattr(plugin, 'get_oauth_config'):
                # Backwards compatibility
                login_config = plugin.get_oauth_config()
            else:
                QMessageBox.critical(
                    self, _("Error"), _("Plugin does not support login")
                )
                return

            # Open login dialog
            from .oauth_dialog import BrowserLoginDialog

            dialog = BrowserLoginDialog(login_config, self)

            # Use exec() for blocking modal
            result = dialog.exec()

            # Check if auth was successful - get cookies
            cookies = dialog.get_cookies()

            # Clean up dialog
            dialog.deleteLater()

            # Process result
            if cookies:
                self._process_cookies(cookies, plugin)
            elif result == 0:  # Rejected/cancelled
                self.status_label.setText(_("Login cancelled"))
                set_status_property(self.status_label, "")

        except Exception as e:
            QMessageBox.critical(self, _("Error"), _("Failed to open login dialog: {}").format(e))
            logger.exception("Login dialog error")

    def _process_cookies(self, cookies: Dict[str, str], plugin) -> None:
        """Process browser cookies for authentication"""
        self.status_label.setText(_("Processing login..."))
        set_status_property(self.status_label, "")

        try:
            if hasattr(plugin, 'handle_cookies'):
                success = plugin.handle_cookies(cookies)
            else:
                # Fallback: store the main auth cookie directly
                auth_cookie = cookies.get("gog-al")
                if auth_cookie and hasattr(plugin, 'set_credential'):
                    plugin.set_credential("gog_al", auth_cookie)
                    success = True
                else:
                    success = False

            # Refresh plugin's auth state after storing cookies
            if hasattr(plugin, 'refresh_auth_state'):
                plugin.refresh_auth_state()

            if success:
                self.status_label.setText(
                    _("Successfully logged in to {}!").format(self.metadata.display_name)
                )
                set_status_property(self.status_label, "success", bold=True)
                self._update_oauth_buttons()
            else:
                self.status_label.setText(_("Login failed: Could not store credentials"))
                set_status_property(self.status_label, "error")

        except Exception as e:
            self.status_label.setText(_("Login failed: {}").format(e))
            set_status_property(self.status_label, "error")
            logger.exception("Cookie processing failed")

    def _oauth_logout(self) -> None:
        """Log out from the store"""
        try:
            plugin = self._get_plugin_instance()
            if not plugin:
                QMessageBox.critical(self, _("Error"), _("Plugin not loaded"))
                return

            if hasattr(plugin, 'logout'):
                plugin.logout()
                self.status_label.setText(
                    _("Logged out from {}").format(self.metadata.display_name)
                )
                set_status_property(self.status_label, "")
                self._update_oauth_buttons()
            else:
                QMessageBox.warning(
                    self, _("Warning"), _("Plugin does not support logout")
                )

        except Exception as e:
            QMessageBox.critical(self, _("Error"), _("Logout failed: {}").format(e))
            logger.exception("OAuth logout failed")

    def _validate_runner_paths(self) -> bool:
        """Validate runner plugin path selections before saving.

        Returns True if valid, False if validation failed (dialog stays open).
        """
        # Helper tool source (Epic runner)
        if (
            hasattr(self, "_helper_source_combo")
            and self._helper_source_combo
            and hasattr(self, "_helper_path_field")
            and self._helper_path_field
        ):
            source_id = self._helper_source_combo.currentData()
            path_text = self._helper_path_field.text().strip()

            if source_id == "custom":
                if not path_text:
                    QMessageBox.warning(
                        self,
                        _("Validation Error"),
                        _("Please specify a path for the custom binary."),
                    )
                    return False
                p = Path(path_text)
                if not p.exists():
                    QMessageBox.warning(
                        self,
                        _("Validation Error"),
                        _("The specified binary does not exist: {}").format(
                            path_text
                        ),
                    )
                    return False

                # Save custom path
                config_key = f"plugins.{self.plugin_name}.helper_custom_path"
                self.config.set(config_key, path_text)
                plugin = self._get_plugin_instance()
                if plugin and hasattr(plugin, "_settings"):
                    plugin._settings["helper_custom_path"] = path_text

        # Runner launcher source (Heroic runner)
        if (
            hasattr(self, "_runner_source_combo")
            and self._runner_source_combo
            and hasattr(self, "_runner_path_field")
            and self._runner_path_field
        ):
            source_id = self._runner_source_combo.currentData()
            path_text = self._runner_path_field.text().strip()

            if source_id == "custom":
                if not path_text:
                    QMessageBox.warning(
                        self,
                        _("Validation Error"),
                        _("Please specify a path for the custom binary."),
                    )
                    return False
                p = Path(path_text)
                if not p.exists():
                    QMessageBox.warning(
                        self,
                        _("Validation Error"),
                        _("The specified binary does not exist: {}").format(
                            path_text
                        ),
                    )
                    return False

                # Save heroic_path setting
                config_key = f"plugins.{self.plugin_name}.heroic_path"
                self.config.set(config_key, path_text)
                plugin = self._get_plugin_instance()
                if plugin and hasattr(plugin, "_settings"):
                    plugin._settings["heroic_path"] = path_text

        return True

    def _on_accept(self) -> None:
        """Save and close"""
        try:
            # Validate runner paths before saving
            if not self._validate_runner_paths():
                return

            # Snapshot SteamGridDB image-affecting settings before save
            _SGDB_IMAGE_KEYS = (
                "hero_style", "grid_style", "nsfw_filter",
                "humor_filter", "allow_animated",
            )
            sgdb_old = {}
            if self.plugin_name == "steamgriddb":
                for key in _SGDB_IMAGE_KEYS:
                    config_key = f"plugins.{self.plugin_name}.{key}"
                    sgdb_old[key] = self.config.get(config_key)

            self._save_settings()

            # Check if SteamGridDB image-affecting settings changed
            if self.plugin_name == "steamgriddb" and sgdb_old:
                changed_keys = []
                for key in _SGDB_IMAGE_KEYS:
                    config_key = f"plugins.{self.plugin_name}.{key}"
                    if self.config.get(config_key) != sgdb_old[key]:
                        changed_keys.append(key)

                if changed_keys:
                    self._offer_sgdb_cache_purge(changed_keys)

            # Clear any cached state that depends on settings
            try:
                plugin = self._get_plugin_instance()
                if plugin and hasattr(plugin, 'clear_launcher_cache'):
                    plugin.clear_launcher_cache()
            except Exception as e:
                logger.debug(f"Could not clear plugin cache: {e}")

            self.accept()
        except ValueError as e:
            QMessageBox.warning(self, _("Validation Error"), str(e))
        except Exception as e:
            QMessageBox.critical(self, _("Error"), _("Failed to save settings: {}").format(e))

    def _offer_sgdb_cache_purge(self, changed_keys: list) -> None:
        """Offer to purge cached SteamGridDB assets after settings change."""
        # Collect asset URLs to count them
        urls = []
        self._collect_metadata_plugin_urls(urls)
        asset_count = len(urls)

        if asset_count == 0:
            return  # Nothing cached, no need to prompt

        # Build confirmation dialog
        dialog = QDialog(self)
        dialog.setWindowTitle(_("SteamGridDB Settings Changed"))
        dialog.setMinimumWidth(420)
        dlg_layout = QVBoxLayout(dialog)
        dlg_layout.setSpacing(12)

        # Bold warning
        warning = QLabel(
            _("<b>Image filter settings have changed.</b>")
        )
        warning.setWordWrap(True)
        dlg_layout.addWidget(warning)

        # Explanation with count
        explain = QLabel(
            _("There are {count} cached SteamGridDB assets that were fetched "
              "with the previous filter settings.\n\n"
              "Purging the cache ensures images match your new preferences. "
              "They will re-download automatically on next access.").format(
                count=asset_count
            )
        )
        explain.setWordWrap(True)
        dlg_layout.addWidget(explain)

        # Buttons
        btn_box = QDialogButtonBox()
        btn_purge = btn_box.addButton(
            _("Purge Cache"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        btn_box.addButton(
            _("Keep Cache"), QDialogButtonBox.ButtonRole.RejectRole
        )
        btn_purge.clicked.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        dlg_layout.addWidget(btn_box)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return  # User chose to keep stale cache

        # Purge cached images
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            purge_count, purge_bytes = _purge_cached_images(self.plugin_name, urls)

            # Also nuke assets from plugin DB so they re-download
            plugin = self._get_plugin_instance()
            nuked = 0
            if plugin and hasattr(plugin, '_get_db'):
                db = plugin._get_db()
                nuked = db.nuke_all_assets()

            # Clear resolved covers/heroes from main DB so they re-resolve
            game_service = self._get_game_service()
            if game_service:
                game_service.reset_media_fields(
                    ["cover", "hero"],
                    only_sources={self.plugin_name},
                )

            # Build status message
            parts = []
            if nuked:
                parts.append(_("{} cached assets purged").format(nuked))
            if purge_count > 0:
                if purge_bytes >= 1024 * 1024:
                    size_str = f"{purge_bytes / (1024 * 1024):.1f} MB"
                else:
                    size_str = f"{purge_bytes / 1024:.0f} KB"
                parts.append(_("{count} images deleted ({size} freed)").format(
                    count=purge_count, size=size_str
                ))

            if parts:
                self.status_label.setText(
                    _("Cache purged: {}. "
                      "Images will re-download on next access.").format(", ".join(parts))
                )

            logger.info(
                f"SteamGridDB settings changed ({changed_keys}): "
                f"nuked {nuked} DB assets, purged {purge_count} cached images"
            )

            # Trigger game list reload so covers re-resolve
            self.store_data_reset.emit(self.plugin_name)

        except Exception as e:
            logger.error(f"Failed to purge SteamGridDB cache: {e}")
            self.status_label.setText(_("Error purging cache: {}").format(e))
        finally:
            QApplication.restoreOverrideCursor()

