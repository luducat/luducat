# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# main.py

"""luducat - Cross-Platform Game Catalogue Browser

Entry point for the application.
"""

import os as _os

# Limit glibc memory arenas to reduce fragmentation from multi-threaded
# QPixmap allocation/deallocation (worker threads vs main thread).
# Must be set before any threads are created.
_os.environ.setdefault("MALLOC_ARENA_MAX", "2")

import faulthandler
import sys as _sys
# faulthandler.enable() defaults to sys.stderr which is None in frozen
# no-console builds (PyInstaller console=False on Windows).
if _sys.stderr is not None:
    faulthandler.enable()

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from .core.config import Config, get_cache_dir, get_config_dir, get_data_dir
from .core.constants import APP_NAME, APP_VERSION, APP_VERSION_FULL, APP_ICON_BASENAME, APP_ID
from .core.database import Database
from .core.logging import ColoredFormatter, SecretRedactingFilter, install_memory_handler
from .core.plugin_manager import PluginManager

if sys.platform == "win32":
    import ctypes

# Configure logging with colored output
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_datefmt = "%Y-%m-%d %H:%M:%S"

# Skip console handler in frozen builds with no console (PyInstaller console=False)
# — sys.stderr is None, StreamHandler would raise on every write.
if sys.stderr is not None:
    _handler = logging.StreamHandler()
    _handler.setFormatter(ColoredFormatter(fmt=_log_format, datefmt=_log_datefmt))
    _handler.addFilter(SecretRedactingFilter())
    logging.root.addHandler(_handler)

# Memory handler for in-app Developer Console (captures from first line)
_memory_handler = install_memory_handler(capacity=5000)
_memory_handler.setFormatter(logging.Formatter(fmt=_log_format, datefmt=_log_datefmt))
_memory_handler.addFilter(SecretRedactingFilter())
logging.root.addHandler(_memory_handler)

logging.root.setLevel(logging.INFO)

logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False, log_file: Path = None) -> None:
    """Configure logging for the application

    Args:
        debug: Enable debug level logging
        log_file: Optional path to log file (overrides default rotating log)
    """
    from logging.handlers import TimedRotatingFileHandler

    level = logging.DEBUG if debug else logging.INFO

    # Update root logger and existing handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in root_logger.handlers:
        handler.setLevel(level)

    # Always write logs to a rotating file in config dir.
    # --log-file overrides the default path but keeps rotation.
    if log_file:
        log_path = log_file
    else:
        log_dir = get_config_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "luducat.log"

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=7,  # keep 7 days of logs
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter(_log_format, datefmt=_log_datefmt)
    )
    file_handler.addFilter(SecretRedactingFilter())
    root_logger.addHandler(file_handler)


def print_info() -> None:
    """Print application and environment information"""
    print(f"{APP_NAME} v{APP_VERSION_FULL}")
    print()
    print("Directories:")
    print(f"  Config: {get_config_dir()}")
    print(f"  Data:   {get_data_dir()}")
    print(f"  Cache:  {get_cache_dir()}")
    print()


def set_dark_title_bar(widget) -> None:
    """Set dark title bar on Windows 10/11.

    This needs to be called after the window is shown and should be
    reapplied when the theme changes, as Windows may reset the titlebar.

    Args:
        widget: The QWidget to apply dark titlebar to
    """
    if sys.platform == 'win32':
        try:
            hwnd = int(widget.winId())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value),
                ctypes.sizeof(value)
            )
        except Exception as e:
            # Silently fail on older Windows versions or if it doesn't work
            logger.warning(f"Could not set dark title bar: {e}")


def _install_dark_titlebar_filter(app) -> None:
    """Install an event filter that applies dark titlebars to all windows on Windows.

    Catches Show events on any top-level widget (main window, dialogs, wizards)
    so every window gets a dark titlebar automatically. Also connects to
    ThemeManager.theme_changed to reapply when the user switches themes.
    """
    if sys.platform != "win32":
        return

    from PySide6.QtCore import QObject, QEvent

    class DarkTitleBarFilter(QObject):
        def eventFilter(self, obj, event):
            if (event.type() == QEvent.Type.Show
                    and hasattr(obj, 'isWindow') and obj.isWindow()
                    and hasattr(obj, 'winId')):
                set_dark_title_bar(obj)
            return super().eventFilter(obj, event)

    # Must keep a reference to prevent garbage collection
    app._dark_titlebar_filter = DarkTitleBarFilter(app)
    app.installEventFilter(app._dark_titlebar_filter)


def init_application(config: Config) -> tuple[Database, PluginManager]:
    """Initialize application components

    Args:
        config: Application configuration

    Returns:
        Tuple of (database, plugin_manager)
    """
    # Initialize database
    database = Database()

    # Initialize plugin manager
    plugin_manager = PluginManager(config)

    # Install/update bundled plugins (checks versions, only updates if newer)
    plugin_manager.install_bundled_plugins()

    # Discover, verify integrity, and load plugins
    plugin_manager.discover_plugins()
    plugin_manager.verify_plugins()
    plugin_manager.load_enabled_plugins()

    # Initialize the global MetadataResolver singleton with config priorities.
    # Fall back to seed defaults for new installs (empty config.metadata_priority).
    from .core.metadata_resolver import _SEED_FIELD_PRIORITIES, init_resolver
    field_priorities = config.get_metadata_priorities()
    if not field_priorities:
        field_priorities = {k: list(v) for k, v in _SEED_FIELD_PRIORITIES.items()}
        config.set_metadata_priorities(field_priorities)
    else:
        # Merge new sources from seed that the user's config doesn't have yet.
        # Appends at the end so user's custom order is preserved.
        dirty = False
        for field, seed_sources in _SEED_FIELD_PRIORITIES.items():
            if field not in field_priorities:
                field_priorities[field] = list(seed_sources)
                dirty = True
            else:
                existing = field_priorities[field]
                existing_set = set(existing)
                for src in seed_sources:
                    if src not in existing_set:
                        existing.append(src)
                        dirty = True
        if dirty:
            config.set_metadata_priorities(field_priorities)
    init_resolver(field_priorities)

    return database, plugin_manager


def run_cli(args: argparse.Namespace) -> int:
    """Run CLI commands

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code
    """
    setup_logging(debug=args.debug, log_file=args.log_file)
    logger.info("Starting luducat v%s", APP_VERSION_FULL)

    if args.info:
        print_info()
        return 0

    # Load configuration
    config = Config()
    config.load()

    # Initialize i18n (must happen after config load, before any UI imports)
    from luducat.core.i18n import init_i18n
    init_i18n(config.get("app.language", ""))

    # Apply directory overrides before anything opens databases or caches
    from luducat.core.config import apply_dir_overrides
    apply_dir_overrides(config)

    # Initialize browser cookie manager with config (for plugin browser preference)
    from luducat.core.browser_cookies import get_browser_cookie_manager
    get_browser_cookie_manager(config)

    # Initialize browser opener with config (for URL opening in preferred browser)
    from luducat.utils.browser import init_browser_opener
    init_browser_opener(config)

    # Initialize components
    database, plugin_manager = init_application(config)

    try:
        if args.command == "plugins":
            # List plugins
            discovered = plugin_manager.get_discovered_plugins()
            if not discovered:
                print("No plugins discovered.")
                return 0

            print(f"Discovered {len(discovered)} plugin(s):\n")
            for name, meta in discovered.items():
                loaded = plugin_manager._loaded.get(name)
                status = "loaded" if loaded and loaded.enabled else "not loaded"
                print(f"  {meta.display_name} v{meta.version} [{status}]")
                print(f"    {meta.description}")
                print()

        elif args.command == "sync":
            # Sync games from all enabled store plugins
            plugins = plugin_manager.get_store_plugins()
            if not plugins:
                print("No store plugins enabled. Enable a store plugin first.")
                return 1

            import asyncio

            async def sync_all():
                for name, plugin in plugins.items():
                    print(f"Syncing {plugin.display_name}...")

                    if not plugin.is_authenticated():
                        print("  Not authenticated. Run authentication first.")
                        continue

                    try:
                        app_ids = await plugin.fetch_user_games()
                        print(f"  Found {len(app_ids)} games")
                    except Exception as e:
                        print(f"  Error: {e}")

            asyncio.run(sync_all())

        elif args.command == "repair-assets":
            # Repair library assets for games missing them
            store_name = args.store
            plugin = plugin_manager.get_plugin(store_name)

            if not plugin:
                print(f"Plugin '{store_name}' not found or not enabled.")
                return 1

            # Check if plugin has repair_library_assets method
            if not hasattr(plugin, 'repair_library_assets'):
                print(f"Plugin '{store_name}' does not support asset repair.")
                return 1

            print(f"Repairing library assets for {plugin.display_name}...")
            print("This may take a while for large libraries.\n")

            def progress_callback(game_name: str, current: int, total: int):
                print(f"  [{current}/{total}] {game_name}")

            stats = plugin.repair_library_assets(progress_callback=progress_callback)

            print("\nRepair complete:")
            print(f"  Games probed: {stats.get('probed', 0)}")
            print(f"  Assets updated: {stats.get('updated', 0)}")
            print(f"  Failed: {stats.get('failed', 0)}")

        else:
            # Default: launch GUI
            return run_gui(config, database, plugin_manager)

    finally:
        # Cleanup
        plugin_manager.close()
        database.close()
        config.save()

    return 0


def install_desktop_icons(config: Config) -> bool:
    """Install application icons to the user's hicolor icon theme.

    This enables proper icon display in GNOME dock, KDE panel, and window decorations.
    Icons are installed to ~/.local/share/icons/hicolor/{size}x{size}/apps/

    Args:
        config: Application configuration (to track installation state)

    Returns:
        True if icons were installed, False if skipped or failed
    """
    import shutil
    import subprocess
    import platformdirs

    # Check if already installed
    if config.get("desktop.icons_installed", False):
        logger.debug("Desktop icons already installed, skipping")
        return False

    from .core.constants import APP_ID, APP_ICON_BASENAME

    # Source icon directory
    base_dir = Path(__file__).resolve().parent
    icons_src = base_dir / "assets" / "appicons"

    if not icons_src.exists():
        logger.warning(f"Icon assets not found at {icons_src}, skipping desktop integration")
        return False

    # Destination directories (XDG)
    xdg_data_home = Path(platformdirs.user_data_dir()).parent  # ~/.local/share
    icons_dest = xdg_data_home / "icons" / "hicolor"
    applications_dest = xdg_data_home / "applications"

    logger.info("Installing desktop icons for GNOME/KDE integration...")

    try:
        # Install PNG icons at various sizes
        sizes = [16, 24, 32, 48, 64, 128, 256, 512]
        for size in sizes:
            src = icons_src / f"{APP_ICON_BASENAME}_{size}x{size}.png"
            if src.exists():
                dest_dir = icons_dest / f"{size}x{size}" / "apps"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest_dir / f"{APP_ID}.png")

        # Install SVG to scalable (if available)
        svg_src = icons_src / f"{APP_ICON_BASENAME}.svg"
        if svg_src.exists():
            svg_dest_dir = icons_dest / "scalable" / "apps"
            svg_dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(svg_src, svg_dest_dir / f"{APP_ID}.svg")

        # Install desktop file
        desktop_src = base_dir.parent / f"{APP_ID}.desktop"
        if desktop_src.exists():
            applications_dest.mkdir(parents=True, exist_ok=True)
            # Copy and update Exec path
            desktop_content = desktop_src.read_text()
            # Update Exec to use absolute path
            script_path = base_dir.parent / "luducat.sh"
            if script_path.exists():
                desktop_content = desktop_content.replace(
                    "Exec=luducat.sh",
                    f"Exec={script_path}"
                )
            (applications_dest / f"{APP_ID}.desktop").write_text(desktop_content)

        # Update icon cache (best effort)
        try:
            subprocess.run(
                ["gtk-update-icon-cache", "-f", "-t", str(icons_dest)],
                capture_output=True,
                timeout=10
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass  # Not critical if this fails

        # Update desktop database (best effort)
        try:
            subprocess.run(
                ["update-desktop-database", str(applications_dest)],
                capture_output=True,
                timeout=10
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass  # Not critical if this fails

        # Mark as installed in config
        config.set("desktop.icons_installed", True)
        logger.info("Desktop icons installed successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to install desktop icons: {e}")
        return False


def _check_startup_backup(config: Config) -> None:
    """Check if a scheduled backup is due and run it if needed.

    Args:
        config: Application configuration
    """
    from .core.backup_manager import is_backup_due, create_backup

    if not is_backup_due(config):
        return

    logger.info("Scheduled backup is due")

    # Import Qt components for dialogs
    from PySide6.QtWidgets import QMessageBox, QProgressDialog, QCheckBox
    from PySide6.QtCore import Qt

    silent = config.get("backup.silent", False)

    if not silent:
        # Prompt user before backup
        msg_box = QMessageBox()
        msg_box.setWindowTitle(_("Scheduled Backup"))
        msg_box.setText(_("A scheduled backup is due."))
        msg_box.setInformativeText(_("Would you like to create a backup now?"))
        msg_box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        msg_box.setDefaultButton(QMessageBox.StandardButton.Yes)

        # Add "don't ask again" checkbox
        dont_ask_checkbox = QCheckBox(_("Don't ask again (run backups silently)"))
        msg_box.setCheckBox(dont_ask_checkbox)

        result = msg_box.exec()

        # Save checkbox preference
        if dont_ask_checkbox.isChecked():
            config.set("backup.silent", True)
            config.save()

        if result != QMessageBox.StandardButton.Yes:
            logger.info("User declined scheduled backup")
            return

    # Show progress dialog
    progress = QProgressDialog(_("Creating backup..."), None, 0, 0)
    progress.setWindowTitle(_("Backup in Progress"))
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    progress.show()

    # Force UI update
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()

    # Run backup
    success, result, _assets = create_backup(config)

    progress.close()

    if success:
        logger.info(f"Startup backup completed: {result}")
        if not silent:
            QMessageBox.information(
                None,
                _("Backup Complete"),
                _("Backup saved successfully.\n\n{path}").format(path=result)
            )
    else:
        logger.error(f"Startup backup failed: {result}")
        QMessageBox.warning(
            None,
            _("Backup Failed"),
            _("Failed to create backup:\n\n{error}").format(error=result)
        )


def _check_directory_permissions(config: Config) -> None:
    """Warn user if data directories had loose permissions.

    Permissions are already tightened by Config.load() — this just
    shows a one-time informational dialog so the user knows.
    """
    warnings = getattr(config, "_permission_warnings", [])
    if not warnings:
        return

    # First run: fix silently (don't scare new users), just log it
    if config.get("app.first_run", True):
        logger.info("First run — tightened directory permissions silently")
        config._permission_warnings.clear()
        return

    from PySide6.QtWidgets import QMessageBox

    lines = []
    for path, _mode in warnings:
        lines.append(f"  {path}")

    QMessageBox.warning(
        None,
        _("{app} — Directory Permissions").format(app=APP_NAME),
        _("{app}'s configuration, databases, and caches were accessible "
          "to other users on this system:\n\n"
          "{dirs}\n\n"
          "Permissions have been secured so that only your login account "
          "and {app} itself have access.").format(
            app=APP_NAME, dirs="\n".join(lines)),
    )

    # Clear so it doesn't show again if run_gui is re-entered
    config._permission_warnings.clear()


def _check_startup_disk_health() -> None:
    """Check data and cache directory health at startup.

    Warns user if directories are not writable (red) or low on space (yellow).
    """
    from .core.config import get_data_dir, get_cache_dir
    from .core.directory_health import check_directory

    checks = [
        (_("Data directory"), get_data_dir()),
        (_("Cache directory"), get_cache_dir()),
    ]

    for label, path in checks:
        health = check_directory(path)
        if health.status == "red":
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                None,
                _("Directory Problem"),
                _("{label} is not usable:\n{path}\n\n{error}\n\n"
                  "The application may not work correctly.").format(
                    label=label, path=path,
                    error=health.error or _("not writable")),
            )
        elif health.status == "yellow":
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                None,
                _("Low Disk Space"),
                _("{label} has less than 1 GB free:\n{path}\n\n"
                  "Free space: {free_mb} MB\n\n"
                  "Consider freeing disk space to avoid problems.").format(
                    label=label, path=path, free_mb=health.free_mb),
            )


def run_gui(config: Config, database: Database, plugin_manager: PluginManager) -> int:
    """Launch the graphical user interface

    Args:
        config: Application configuration
        database: Database instance
        plugin_manager: Plugin manager instance

    Returns:
        Exit code
    """
    import os

    # Apply UI zoom via QT_SCALE_FACTOR BEFORE creating QApplication
    # This scales the entire interface (fonts, widgets, images, margins, icons)
    saved_zoom = config.get("appearance.ui_zoom", 100)
    if saved_zoom != 100:
        scale_factor = saved_zoom / 100.0
        os.environ["QT_SCALE_FACTOR"] = str(scale_factor)
        logger.info(f"Applied UI scale factor: {scale_factor} ({saved_zoom}%)")

    # Enable virtual keyboard on Steam Deck (must be set before QApplication)
    if sys.platform.startswith("linux") and "QT_IM_MODULE" not in os.environ:
        try:
            board_vendor = Path("/sys/devices/virtual/dmi/id/board_vendor").read_text().strip()
            if board_vendor == "Valve":
                os.environ["QT_IM_MODULE"] = "maliit"
                logger.info("Steam Deck detected — enabled Maliit virtual keyboard")
        except OSError:
            pass

    # setup application metadata

    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QIcon

    except ImportError:
        logger.error("PySide6 not installed. Install with: pip install PySide6")
        print("Error: PySide6 is required for the GUI.")
        print("Install with: pip install PySide6")
        return 1

    from .ui import MainWindow
    from .core.theme_manager import (
        ThemeManager, THEME_SYSTEM, install_bundled_themes
    )

    # Create application
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION_FULL)

    # Single-instance guard — prevent multiple GUI instances
    from PySide6.QtCore import QLockFile
    lock_file = QLockFile(str(get_config_dir() / "luducat.lock"))
    lock_file.setStaleLockTime(0)  # Always check PID, never time-based
    if not lock_file.tryLock(100):  # 100ms timeout
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            None,
            APP_NAME,
            _("Luducat is already running.\n\n"
              "Only one instance can run at a time."),
        )
        return 1

    # Patch QComboBox popup to suppress QSS-triggered check indicators.
    # QComboBox::item:hover/selected QSS rules activate indicator rendering
    # (checkmark on current item + left spacing). Using QItemDelegate (Qt's
    # default combo delegate type) preserves correct font resolution.
    from PySide6.QtWidgets import QComboBox, QItemDelegate

    class _NoCheckDelegate(QItemDelegate):
        def paint(self, painter, option, index):
            from PySide6.QtWidgets import QStyleOptionViewItem
            opt = QStyleOptionViewItem(option)
            opt.features &= ~opt.ViewItemFeature.HasCheckIndicator
            super().paint(painter, opt, index)

        def drawCheck(self, painter, option, rect, state):
            pass  # suppress checkmark rendering

    _original_showPopup = QComboBox.showPopup

    def _patched_showPopup(self):
        view = self.view()
        if not isinstance(view.itemDelegate(), _NoCheckDelegate):
            view.setItemDelegate(_NoCheckDelegate(view))
        _original_showPopup(self)

    QComboBox.showPopup = _patched_showPopup

    # Install Qt's own translations for standard button labels (Cancel, Yes, No…)
    # Must happen after QApplication is created. Uses the same language as gettext.
    from luducat.core.i18n import install_qt_translator
    install_qt_translator(app)

    # Preserve system font before any styling (cross-platform)
    # Qt platform plugins auto-detect: KDE, GNOME, Windows, macOS
    system_font = app.font()
    app.setFont(system_font)  # Lock in before Fusion style is applied

    # Reduce tooltip delay for snappier feel
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class _AppStyle(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
                return 580
            return super().styleHint(hint, option, widget, returnData)

    app.setStyle(_AppStyle(app.style()))

    # limit QT pixmap cache
    from PySide6.QtGui import QPixmapCache
    logger.info(f"QPixmapCache is currently {QPixmapCache.cacheLimit()}KB")
    QPixmapCache.setCacheLimit(4096)  # 4MB
    logger.info(f"QPixmapCache limited to {QPixmapCache.cacheLimit()}KB")


    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setDesktopFileName(APP_ID)

    # Install desktop icons on first run (or if not yet installed)
    # This must happen BEFORE icon selection so QIcon.fromTheme() can find them
    if sys.platform.startswith("linux"):
        install_desktop_icons(config)

    # load application icon
    from PySide6.QtCore import QSize

    base_dir = Path(__file__).resolve().parent
    icon_dir = base_dir / "assets" / "appicons"

    def select_icon_for_platform() -> Optional[QIcon]:
        """Return a QIcon appropriate to the current OS, or None."""
        platform = sys.platform

        # Windows: prefer ICO, fall back to PNG
        if platform.startswith("win"):
            ico = icon_dir / f"{APP_ICON_BASENAME}.ico"
            if ico.exists():
                return QIcon(str(ico))
            png = icon_dir / f"{APP_ICON_BASENAME}_256x256.png"
            if png.exists():
                return QIcon(str(png))

        # macOS: prefer ICNS, fall back to PNG
        elif platform == "darwin":
            icns = icon_dir / f"{APP_ICON_BASENAME}.icns"
            if icns.exists():
                return QIcon(str(icns))
            png = icon_dir / f"{APP_ICON_BASENAME}_256x256.png"
            if png.exists():
                return QIcon(str(png))

        # Linux / other Unix: try theme icon first (installed by luducat.sh),
        # then fall back to loading from bundled files
        else:
            # Try theme icon first (works best with KDE/GNOME)
            theme_icon = QIcon.fromTheme(APP_ID)
            if not theme_icon.isNull():
                logger.info(f"Using theme icon: {APP_ID}")
                return theme_icon

            # Fallback: load from bundled PNG files
            icon = QIcon()
            any_found = False
            for size in (512, 256, 128, 64, 48, 32, 24, 16):
                path = icon_dir / f"{APP_ICON_BASENAME}_{size}x{size}.png"
                if path.exists():
                    icon.addFile(str(path), QSize(size, size))
                    any_found = True
            if any_found:
                return icon

        return None

    # Set application icon BEFORE creating windows
    app_icon = select_icon_for_platform()
    logger.info(f"Icon selected: {app_icon}")
    if app_icon is not None:
        app.setWindowIcon(app_icon)
        logger.info(f"Application icon set for current platform {sys.platform}")
    else:
        logger.warning(f"No suitable application icon found for this platform {sys.platform}")

    # Apply platform-native style (Fusion is cross-platform consistent)
    app.setStyle("Fusion")

    # Install bundled themes (only copies if not already present)
    install_bundled_themes()

    # Initialize theme manager
    theme_manager = ThemeManager(app)

    # Default theme is system (uses desktop environment appearance)
    DEFAULT_THEME = "system"

    # Apply saved theme (defaults to variant:luducat)
    saved_theme = config.get("appearance.theme", DEFAULT_THEME)

    # Migrate old theme values
    if saved_theme in ("auto", "light", "dark"):
        saved_theme = DEFAULT_THEME
        config.set("appearance.theme", DEFAULT_THEME)
    elif saved_theme.startswith("custom:"):
        # Migrate from custom: to variant: format if variant exists
        theme_name = saved_theme[7:]  # Remove "custom:" prefix
        variant_name = theme_name.replace("_", "-").lower()
        variant_theme = f"variant:{variant_name}"
        # Check if variant file exists
        from .core.theme_manager import get_themes_dir
        variant_path = get_themes_dir() / "variants" / f"{variant_name}.json"
        if variant_path.exists():
            saved_theme = variant_theme
            config.set("appearance.theme", saved_theme)
            logger.info(f"Migrated theme from custom:{theme_name} to {variant_theme}")

    # Try to apply theme, fallback to system if it fails
    if not theme_manager.apply_theme(saved_theme):
        logger.warning(f"Failed to apply theme '{saved_theme}', falling back to system")
        theme_manager.apply_theme(THEME_SYSTEM)
        config.set("appearance.theme", THEME_SYSTEM)

    # Note: UI zoom is applied via QT_SCALE_FACTOR before QApplication creation
    # The theme_manager tracks the current zoom level for the settings dialog
    theme_manager.set_current_zoom(saved_zoom)

    # Check if scheduled backup is due
    _check_startup_backup(config)

    # Startup disk health check — warn user before problems snowball
    _check_startup_disk_health()

    # Warn if directory permissions were loose (already fixed by Config.load)
    _check_directory_permissions(config)

    # Enforce cache limits (respects offline mode)
    from .core.cache_manager import enforce_cache_limits
    enforce_cache_limits(config)

    # Initialize runtime manager for game execution
    from .core.runtime_manager import get_runtime_manager
    runtime_manager = get_runtime_manager()
    runtime_manager.set_config(config)
    runtime_manager.set_plugin_manager(plugin_manager)
    runtime_manager.set_database(database)

    # Initialize game manager for installation orchestration
    from .core.game_manager import get_game_manager
    game_manager = get_game_manager()
    game_manager.set_config(config)
    game_manager.set_database(database)
    game_manager.set_runtime_manager(runtime_manager)

    # Note: RuntimeManager.initialize() is async - defer to main window startup

    # Create and show main window (pass theme_manager for live updates)
    window = MainWindow(
        config, database, plugin_manager, theme_manager,
        runtime_manager=runtime_manager, game_manager=game_manager,
    )

    # Explicitly set window icon (in case app-level icon wasn't inherited)
    if app_icon is not None:
        window.setWindowIcon(app_icon)

    # Dark titlebars for all windows (main window + dialogs) on Windows
    _install_dark_titlebar_filter(app)
    if sys.platform == "win32" and theme_manager:
        # Reapply to all visible windows when theme changes (Windows resets them)
        def _reapply_dark_titlebars():
            for widget in app.topLevelWidgets():
                if widget.isVisible() and widget.isWindow():
                    set_dark_title_bar(widget)
        theme_manager.theme_changed.connect(_reapply_dark_titlebars)

    window.show()

    # Note: first_run flag is marked complete by the setup wizard when accepted
    # Do NOT mark it complete here - wizard handles this in _show_setup_wizard_first_run

    # Start Qt event loop
    return app.exec()


def _handle_run_subprocess() -> int:
    """Parse _run args manually and dispatch to runner subprocess.

    Kept out of argparse so it never appears in --help.
    """
    import argparse as _ap

    p = _ap.ArgumentParser(prog="luducat _run", add_help=False)
    p.add_argument("--session-id", type=int, required=True)
    p.add_argument("--db-path", type=str, required=True)
    p.add_argument("--env-json", type=str, default="")
    p.add_argument("--working-dir", type=str, default="")
    p.add_argument("game_command", nargs=_ap.REMAINDER)
    args = p.parse_args(sys.argv[2:])  # skip "luducat _run"

    from .core.runner_subprocess import run_game
    return run_game(args)


def main() -> int:
    """Main entry point"""
    # Internal runner subprocess — intercept before argparse to keep it invisible
    if len(sys.argv) >= 2 and sys.argv[1] == "_run":
        return _handle_run_subprocess()

    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Cross-platform game catalogue browser",
    )

    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"{APP_NAME} {APP_VERSION_FULL}",
    )

    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging",
    )

    parser.add_argument(
        "--log-file",
        type=Path,
        help="Write logs to file",
    )

    parser.add_argument(
        "--info",
        action="store_true",
        help="Print application info and exit",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # CLI subcommands disabled for initial release — kept for future use
    # subparsers.add_parser("plugins", help="List available plugins")
    # subparsers.add_parser("sync", help="Sync games from all stores")
    # repair_parser = subparsers.add_parser(
    #     "repair-assets",
    #     help="Repair library assets (covers) for games missing them"
    # )
    # repair_parser.add_argument(
    #     "--store",
    #     type=str,
    #     default="steam",
    #     help="Store plugin to repair (default: steam)"
    # )

    args = parser.parse_args()

    try:
        return run_cli(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
