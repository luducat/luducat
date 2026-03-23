# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# plugin_manager.py

"""Plugin discovery and management for luducat

Handles:
- Plugin discovery from config directory
- Bundled plugin installation on first run
- Plugin loading and instantiation
- Plugin lifecycle management
"""

import importlib.util
from luducat.core.json_compat import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from packaging import version

from ..plugins.base import (
    AbstractRunnerPlugin,
    AbstractGameStore,
    AbstractMetadataProvider,
    AbstractPlatformProvider,
    RunnerPlugin,
    MetadataPlugin,
    PluginError,
    PluginMetadata,
    PluginType,
    PlatformPlugin,
    StorePlugin,
)
from .config import Config, get_cache_dir, get_config_dir, get_data_dir
from .constants import APP_VERSION, COMPILED_BUILD
from .credentials import CredentialManager
from .directory_health import check_directory
from ..plugins.sdk import _registry as sdk_registry
from ..plugins.sdk.storage import PluginStorage
from ..plugins.sdk.network import PluginHttpClient
from .network_manager import get_network_manager

logger = logging.getLogger(__name__)

# Default brand colors for unknown stores (avoids dict allocation on every miss)
_DEFAULT_BRAND_COLORS: Dict[str, str] = {"bg": "#2a2a2a", "text": "#ffffff"}

# Fields already warned about (warn once per field per session)
_warned_non_canonical: set = set()

# Mapping from declarative store engine ruleset field names to standard metadata
# field names used by MetadataResolver (matches _to_plugin_game() in store.py).
_RULESET_TO_STANDARD: Dict[str, str] = {
    "title": "title",
    "short_description": "short_description",
    "description": "description",
    "developers": "developers",
    "publishers": "publishers",
    "genres": "genres",
    "release_date": "release_date",
    "cover_url": "cover",
    "cover_detail_url": "cover",
    "header_url": "header_url",
    "background_url": "hero",
    "screenshots": "screenshots",
    "videos": "videos",
    "game_modes": "game_modes",
    "languages": "supported_languages",
    "esrb_rating": "age_rating_esrb",
    "operating_systems": "platforms",
    "is_free": "is_free",
    "price": "price",
}


def validate_metadata_fields(metadata: dict, plugin_name: str) -> None:
    """Warn if a plugin produces non-canonical field names.

    Called during metadata merge to catch plugins using old storage names
    (cover_url, background_url, etc.) or unknown field names. Warnings are
    logged once per field per session.

    Args:
        metadata: Metadata dict produced by the plugin
        plugin_name: Name of the plugin for logging context
    """
    from ..plugins.base import CANONICAL_METADATA_FIELDS, PER_STORE_FIELDS
    from ..core.metadata_resolver import _INTERNAL_FIELDS

    for key in metadata:
        if key.startswith("_"):
            continue  # Internal tracking fields
        if key in CANONICAL_METADATA_FIELDS:
            continue
        if key in PER_STORE_FIELDS:
            continue
        if key in _INTERNAL_FIELDS:
            continue
        warn_key = f"{plugin_name}:{key}"
        if warn_key not in _warned_non_canonical:
            _warned_non_canonical.add(warn_key)
            logger.warning(
                f"Plugin '{plugin_name}' produced non-canonical field '{key}'"
            )


@dataclass
class LoadedPlugin:
    """Container for a loaded plugin instance

    Supports both store plugins (AbstractGameStore) and
    metadata plugins (AbstractMetadataProvider).
    """
    metadata: PluginMetadata
    instance: Any  # AbstractGameStore or AbstractMetadataProvider
    enabled: bool = True
    error: Optional[str] = None
    # Plugin integrity verification state (set by verify_plugins)
    trust_state: Optional[str] = None   # TrustState value
    trust_tier: Optional[str] = None    # TrustTier value
    fingerprint: Optional[str] = None   # SHA-256 Merkle hash

    def is_store_plugin(self) -> bool:
        """Check if this is a store plugin"""
        return "store" in self.metadata.plugin_types

    def is_metadata_plugin(self) -> bool:
        """Check if this is a metadata plugin"""
        return "metadata" in self.metadata.plugin_types


class PluginManager:
    """Manages plugin discovery, loading, and lifecycle

    Plugin Discovery:
        Plugins are discovered from ~/.config/luducat/plugins/
        Each plugin directory must contain a plugin.json file.

    Bundled Plugins:
        On first run, bundled plugins from the application package
        are copied to the user's config directory.

    Usage:
        manager = PluginManager(config)
        manager.discover_plugins()
        manager.load_enabled_plugins()

        # Get a plugin
        steam = manager.get_plugin("steam")

        # Use plugin
        games = await steam.fetch_user_games()

        # Cleanup
        manager.close()
    """

    # Class-level registry populated during discover_plugins()
    # Maps plugin name → display_name (e.g., "gog" → "GOG")
    _display_names: Dict[str, str] = {}
    # Maps plugin name → {"bg": "#hex", "text": "#hex"}
    _brand_colors: Dict[str, Dict[str, str]] = {}
    # Maps plugin name → list of plugin types (e.g., ["store"])
    _plugin_types: Dict[str, List[str]] = {}
    # Maps plugin name → badge label (e.g., "steam" → "STM")
    _badge_labels: Dict[str, str] = {}
    # Maps plugin name → capabilities dict from plugin.json
    _plugin_capabilities: Dict[str, Dict[str, bool]] = {}
    # Set of hidden plugin names (excluded from UI store lists)
    _hidden_plugins: set = set()

    @classmethod
    def get_store_display_name(cls, store: str) -> str:
        """Get the properly-spelled display name for a store/plugin.

        Uses plugin metadata populated during discover_plugins().
        Falls back to capitalize() for unknown plugins.
        """
        return cls._display_names.get(store.lower(), store.capitalize())

    @classmethod
    def get_store_brand_colors(cls, store: str) -> Dict[str, str]:
        """Get brand colors for a store/plugin.

        Returns {"bg": "#hex", "text": "#hex"} or a default.
        """
        return cls._brand_colors.get(store.lower(), _DEFAULT_BRAND_COLORS)

    @classmethod
    def get_store_plugin_names(cls) -> List[str]:
        """Get names of all discovered visible store-type plugins."""
        return [
            name for name, types in cls._plugin_types.items()
            if "store" in types and name not in cls._hidden_plugins
        ]

    @classmethod
    def get_metadata_plugin_names(cls) -> List[str]:
        """Get names of all plugins that provide metadata (store + enrichment).

        Store plugins are metadata providers too — they provide title,
        description, screenshots, etc. for games in their store.
        """
        return [
            name for name, types in cls._plugin_types.items()
            if "store" in types or "metadata" in types
        ]

    @classmethod
    def get_enrichment_plugin_names(cls) -> List[str]:
        """Get names of enrichment-capable metadata plugins.

        Returns only metadata-typed plugins with enrich_metadata=true.
        Tag-sync-only plugins (Lutris, Heroic) are excluded.
        Does NOT include store plugins (which enrich via store sync).
        """
        return [
            name for name, types in cls._plugin_types.items()
            if "metadata" in types
            and cls._plugin_capabilities.get(name, {}).get("enrich_metadata", False)
        ]

    @classmethod
    def get_tag_sync_plugin_names(cls) -> List[str]:
        """Get names of metadata plugins with tag_sync capability.

        These are plugins like Lutris, Heroic that import tags/favourites
        from local sources. Does NOT include enrichment plugins.
        """
        return [
            name for name, types in cls._plugin_types.items()
            if "metadata" in types
            and cls._plugin_capabilities.get(name, {}).get("tag_sync", False)
            and not cls._plugin_capabilities.get(name, {}).get("enrich_metadata", False)
        ]

    @classmethod
    def is_store_plugin(cls, name: str) -> bool:
        """Check if a plugin is a store-type plugin."""
        return "store" in cls._plugin_types.get(name, [])

    @classmethod
    def get_store_badge_label(cls, store: str) -> str:
        """Get 3-char badge abbreviation for a store/plugin.

        Uses plugin metadata populated during discover_plugins().
        Falls back to first 3 chars of uppercase store name.
        """
        return cls._badge_labels.get(store.lower(), store.upper()[:3])

    def __init__(self, config: Config):
        """Initialize plugin manager

        Args:
            config: Application configuration
        """
        self.config = config

        # Directories
        self.config_dir = get_config_dir()
        self.data_dir = get_data_dir()
        self.cache_dir = get_cache_dir()

        # In compiled builds, use bundled plugins directly (no user plugins)
        # User plugins require .py source files which aren't available in compiled binary
        if COMPILED_BUILD:
            self.plugins_dir = Path(__file__).parent.parent / "plugins"
            logger.info("Compiled build: using bundled plugins only (user plugins disabled)")
        else:
            self.plugins_dir = self.config_dir / "plugins"
            # Ensure plugins directory exists
            self.plugins_dir.mkdir(parents=True, exist_ok=True)

        # Initialize credential manager with config dir for file fallback
        self.credential_manager = CredentialManager(config_dir=self.config_dir)

        # Plugin storage
        self._discovered: Dict[str, PluginMetadata] = {}
        self._loaded: Dict[str, LoadedPlugin] = {}

        # Plugin integrity verification
        self._verifier = None  # Initialized lazily in verify_plugins()
        self._verification_results: Dict[str, Any] = {}

        # Initialize SDK registry (one-time, populates shim modules)
        self._init_sdk()

    def _init_sdk(self) -> None:
        """Populate the SDK registry with core implementations.

        Called once from __init__.  SDK shim modules (sdk.config,
        sdk.cookies, etc.) delegate to these injected functions.
        """
        sdk_registry.register_config(
            get_data_dir=get_data_dir,
            get_cache_dir=get_cache_dir,
            get_config_value=self.config.get,
            set_config_value=self.config.set,
        )

        # Browser cookies — lazy import to avoid pulling Qt at startup
        def _get_cookie_manager():
            from .browser_cookies import get_browser_cookie_manager
            return get_browser_cookie_manager(self.config)

        sdk_registry.register_cookies(get_browser_cookies=_get_cookie_manager)

        # Dialogs — lazy import to avoid pulling Qt at startup
        def _register_dialog_helpers():
            try:
                from ..ui.dialogs.oauth_dialog import (
                    BrowserLoginConfig, get_login_status,
                )
                sdk_registry.register_dialogs(
                    browser_login_config_class=BrowserLoginConfig,
                    get_login_status=get_login_status,
                )
            except ImportError:
                pass  # headless / test environment

        _register_dialog_helpers()

        # NetworkManager — central hub for all plugin HTTP activity
        self._network_manager = get_network_manager()
        sdk_registry.register_network_manager(self._network_manager)

        # ProxyManager — authenticated proxy access for plugins
        from .proxy_manager import get_proxy_manager
        sdk_registry.register_proxy_manager(get_proxy_manager())

        # Dialog helpers — reset_plugin_data (lazy, needs Qt)
        def _register_reset():
            try:
                from ..ui.dialogs.plugin_config import reset_plugin_data
                sdk_registry.register_reset_plugin_data(reset_plugin_data)
            except ImportError:
                pass  # headless / test environment
        _register_reset()

        # Icon tinting — lazy import to avoid pulling Qt at startup
        def _register_icons():
            try:
                from ..utils.icons import load_tinted_icon
                sdk_registry.register_load_tinted_icon(load_tinted_icon)
            except ImportError:
                pass  # headless / test environment
        _register_icons()

        # URL opener — lazy import to avoid pulling Qt at startup
        def _register_open_url():
            try:
                from ..utils.browser import open_url
                sdk_registry.register_open_url(open_url)
            except ImportError:
                pass  # headless / test environment
        _register_open_url()

        # NetworkMonitor integration happens later when MainWindow creates
        # the monitor and calls set_network_monitor() on this manager.
        # At __init__ time, the Qt event loop (and thus NetworkMonitor)
        # doesn't exist yet.

        logger.debug("SDK registry initialized")

    def cleanup_legacy_directories(self) -> None:
        """Remove legacy plugin data directories

        Previously, plugin data was stored in ~/.local/share/luducat/plugins/
        but this has been moved to ~/.local/share/luducat/plugins-data/
        to avoid confusion with plugin code in config/plugins/.

        This method removes the old stale directory if it exists.
        """
        legacy_plugins_dir = self.data_dir / "plugins"

        if legacy_plugins_dir.exists():
            logger.info(f"Removing legacy plugin data directory: {legacy_plugins_dir}")
            try:
                shutil.rmtree(legacy_plugins_dir)
                logger.info("Legacy plugin data directory removed")
            except Exception as e:
                logger.warning(f"Failed to remove legacy plugin data directory: {e}")

    def _cleanup_user_plugin_dirs(self) -> None:
        """Remove bundled plugin directories from user config dir.

        In compiled builds, plugins load from the application directory.
        Any plugin dirs in ~/.config/luducat/plugins/ are stale leftovers
        from previous source-mode runs or manually copied — remove them.
        Only removes directories matching known bundled plugin names.
        """
        user_plugins_dir = self.config_dir / "plugins"
        if not user_plugins_dir.exists():
            return

        # Build set of bundled plugin names
        bundled_dir = Path(__file__).parent.parent / "plugins"
        bundled_names = set()
        if bundled_dir.exists():
            for d in bundled_dir.iterdir():
                if d.is_dir() and not d.name.startswith((".", "_")):
                    if d.name == "platforms":
                        for rd in d.iterdir():
                            if rd.is_dir() and not rd.name.startswith((".", "_")):
                                bundled_names.add(rd.name)
                    else:
                        bundled_names.add(d.name)

        try:
            for item in list(user_plugins_dir.iterdir()):
                if not item.is_dir():
                    continue
                if item.name == "platforms":
                    for rt in list(item.iterdir()):
                        if rt.is_dir() and rt.name in bundled_names:
                            shutil.rmtree(rt)
                            logger.info(f"Removed stale user plugin dir: {rt}")
                    # Remove platforms/ if now empty
                    if item.exists() and not any(item.iterdir()):
                        item.rmdir()
                elif item.name in bundled_names:
                    shutil.rmtree(item)
                    logger.info(f"Removed stale user plugin dir: {item}")

            # Remove plugins/ dir itself if empty
            if user_plugins_dir.exists() and not any(user_plugins_dir.iterdir()):
                user_plugins_dir.rmdir()
                logger.info("Removed empty user plugins directory")
        except Exception as e:
            logger.warning(f"Failed to clean up user plugin dirs: {e}")

    def install_bundled_plugins(self) -> int:
        """Install bundled plugins from application package

        Copies plugins from luducat/plugins/ to user config.
        Only installs new plugins or updates outdated ones.

        Returns:
            Number of plugins installed/updated
        """

        # Clean up legacy directories first
        self.cleanup_legacy_directories()

        # In compiled builds, plugins are loaded directly from bundled dir
        # No installation/copying needed — also clean up any stale user dirs
        if COMPILED_BUILD:
            logger.debug("Compiled build: skipping bundled plugin installation")
            self._cleanup_user_plugin_dirs()
            return 0

        # Find bundled plugins directory
        bundled_dir = Path(__file__).parent.parent / "plugins"
        if not bundled_dir.exists():
            logger.debug("No bundled plugins directory found")
            return 0

        installed_count = 0

        # Install direct plugins (store, metadata)
        installed_count += self._install_plugins_from_dir(bundled_dir, self.plugins_dir)

        # Install platform and runner plugins from nested subdirectories
        for subdir_name in ("platforms", "runners"):
            bundled_subdir = bundled_dir / subdir_name
            if bundled_subdir.exists():
                user_subdir = self.plugins_dir / subdir_name
                user_subdir.mkdir(parents=True, exist_ok=True)
                installed_count += self._install_plugins_from_dir(bundled_subdir, user_subdir)

        if installed_count > 0:
            logger.info(f"Installed/updated {installed_count} bundled plugin(s)")

        return installed_count

    def _install_plugins_from_dir(self, source_dir: Path, dest_dir: Path) -> int:
        """Install plugins from a source directory to destination

        Args:
            source_dir: Directory containing plugin subdirectories
            dest_dir: Target directory for installed plugins

        Returns:
            Number of plugins installed/updated
        """
        # Pre-flight: verify destination is writable
        health = check_directory(dest_dir)
        if not health.writable:
            logger.error(f"Cannot install plugins: {dest_dir} not writable: {health.error}")
            return 0

        installed_count = 0

        for plugin_source in source_dir.iterdir():
            if not plugin_source.is_dir():
                continue

            # Skip __pycache__, hidden dirs, and container directories
            if plugin_source.name.startswith("_") or plugin_source.name.startswith("."):
                continue
            if plugin_source.name in ("platforms", "runners", "sdk"):
                continue

            plugin_json = plugin_source / "plugin.json"
            if not plugin_json.exists():
                # Not a plugin directory (might be base.py, __init__.py, etc.)
                continue

            try:
                with open(plugin_json) as f:
                    bundled_meta = json.load(f)

                plugin_name = bundled_meta.get("name", plugin_source.name)
                plugin_dest = dest_dir / plugin_name

                should_install = False

                if not plugin_dest.exists():
                    # New plugin
                    should_install = True
                    logger.info(f"Installing bundled plugin: {plugin_name}")
                else:
                    # Check version
                    existing_json = plugin_dest / "plugin.json"
                    if existing_json.exists():
                        with open(existing_json) as f:
                            existing_meta = json.load(f)

                        bundled_ver = bundled_meta.get("version", "0.0.0")
                        existing_ver = existing_meta.get("version", "0.0.0")

                        if version.parse(bundled_ver) > version.parse(existing_ver):
                            logger.info(
                                f"Updating bundled plugin: {plugin_name} "
                                f"({existing_ver} -> {bundled_ver})"
                            )
                            # Backup existing
                            self._backup_plugin(plugin_dest)
                            shutil.rmtree(plugin_dest)
                            should_install = True

                if should_install:
                    shutil.copytree(plugin_source, plugin_dest)
                    installed_count += 1

            except Exception as e:
                logger.error(f"Failed to install bundled plugin {plugin_source.name}: {e}")

        return installed_count

    def _backup_plugin(self, plugin_dir: Path) -> Optional[Path]:
        """Backup a plugin directory before updating

        Args:
            plugin_dir: Plugin directory to backup

        Returns:
            Path to backup or None if failed
        """
        backup_dir = self.plugins_dir / ".backups"
        backup_dir.mkdir(exist_ok=True)

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{plugin_dir.name}.{timestamp}.backup"
        backup_path = backup_dir / backup_name

        try:
            shutil.copytree(plugin_dir, backup_path)
            logger.debug(f"Backed up plugin to {backup_path}")

            # Keep only last 3 backups per plugin
            pattern = f"{plugin_dir.name}.*.backup"
            backups = sorted(backup_dir.glob(pattern))
            for old_backup in backups[:-3]:
                shutil.rmtree(old_backup)

            return backup_path
        except Exception as e:
            logger.error(f"Failed to backup plugin {plugin_dir.name}: {e}")
            return None

    def discover_plugins(self) -> Dict[str, PluginMetadata]:
        """Discover all available plugins

        Scans plugins directory for valid plugin.json files.
        Also scans plugins/platforms/ and plugins/runners/ subdirectories.

        Returns:
            Dict mapping plugin name to metadata
        """
        self._discovered.clear()

        if not self.plugins_dir.exists():
            return {}

        # Subdirectories that contain nested plugin packages
        _PLUGIN_SUBDIRS = {"platforms", "runners"}

        # Scan direct children (store, metadata plugins)
        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            # Skip hidden/special directories
            if plugin_dir.name.startswith(".") or plugin_dir.name.startswith("_"):
                continue

            # Skip nested plugin subdirectories (handled separately below)
            if plugin_dir.name in _PLUGIN_SUBDIRS:
                continue

            plugin_json = plugin_dir / "plugin.json"
            if not plugin_json.exists():
                logger.debug(f"Skipping {plugin_dir.name}: no plugin.json")
                continue

            self._discover_single_plugin(plugin_dir, plugin_json)

        # Scan nested subdirectories for platform and runner plugins
        for subdir_name in _PLUGIN_SUBDIRS:
            subdir = self.plugins_dir / subdir_name
            if not subdir.exists():
                continue
            for nested_dir in subdir.iterdir():
                if not nested_dir.is_dir():
                    continue

                if nested_dir.name.startswith(".") or nested_dir.name.startswith("_"):
                    continue

                plugin_json = nested_dir / "plugin.json"
                if not plugin_json.exists():
                    logger.debug(f"Skipping {subdir_name}/{nested_dir.name}: no plugin.json")
                    continue

                self._discover_single_plugin(nested_dir, plugin_json)

        logger.info(f"Discovered {len(self._discovered)} plugin(s)")
        return self._discovered

    def verify_plugins(self) -> Dict[str, Any]:
        """Verify integrity of all discovered plugins.

        Computes Merkle fingerprints and compares against the keyring
        trust store.  Plugins that fail verification are flagged (but
        not yet disabled — that happens in load_plugin()).

        Must be called after discover_plugins() and before
        load_enabled_plugins().

        Returns:
            Dict of plugin_name -> VerificationResult
        """
        from .plugin_verifier import (
            PluginVerifier, TrustStore,
            detect_distribution_format, log_trust_state,
        )

        # Initialize trust store (keyring-backed)
        trust_store = TrustStore(self.credential_manager, data_dir=self.data_dir)

        # Determine source tree dir for dev mode detection
        source_tree_dir = None
        if not COMPILED_BUILD:
            source_tree_dir = Path(__file__).parent.parent / "plugins"

        # Run verification
        self._verifier = PluginVerifier(trust_store)
        self._verification_results = self._verifier.verify_all(
            self._discovered, source_tree_dir=source_tree_dir,
        )

        # Save trust store (persists any seeds or updates)
        trust_store.save()

        # Log trust state block
        distribution = detect_distribution_format()
        log_trust_state(
            self._verification_results,
            trust_store.get_trust_source(),
            distribution,
        )

        # Store distribution info for shutdown logging
        self._trust_store = trust_store
        self._distribution = distribution

        return self._verification_results

    def log_trust_state_shutdown(self) -> None:
        """Log the trust state block at shutdown."""
        if not self._verification_results:
            return
        from .plugin_verifier import log_trust_state
        log_trust_state(
            self._verification_results,
            self._trust_store.get_trust_source() if hasattr(self, '_trust_store') else "unknown",
            self._distribution if hasattr(self, '_distribution') else ("unknown", None),
            event="shutdown",
        )

    def _get_bundled_names(self) -> set:
        """Get set of bundled plugin names from the application source tree.

        Reads the 'name' field from each plugin.json to handle cases where
        the plugin name differs from its directory name (e.g., runner plugins).
        """
        import json as _json

        bundled_dir = Path(__file__).parent.parent / "plugins"
        _NESTED_SUBDIRS = {"platforms", "runners"}
        names = set()
        if bundled_dir.exists():
            for d in bundled_dir.iterdir():
                if d.is_dir() and not d.name.startswith((".", "_")):
                    if d.name in _NESTED_SUBDIRS:
                        for rd in d.iterdir():
                            if rd.is_dir() and not rd.name.startswith((".", "_")):
                                pj = rd / "plugin.json"
                                if pj.exists():
                                    try:
                                        data = _json.loads(pj.read_text(encoding="utf-8"))
                                        names.add(data.get("name", rd.name))
                                    except Exception:
                                        names.add(rd.name)
                                else:
                                    names.add(rd.name)
                    elif d.name != "sdk":
                        pj = d / "plugin.json"
                        if pj.exists():
                            try:
                                data = _json.loads(pj.read_text(encoding="utf-8"))
                                names.add(data.get("name", d.name))
                            except Exception:
                                names.add(d.name)
                        else:
                            names.add(d.name)
        return names

    def _discover_single_plugin(self, plugin_dir: Path, plugin_json: Path) -> None:
        """Discover a single plugin from its directory

        Args:
            plugin_dir: Path to plugin directory
            plugin_json: Path to plugin.json file
        """
        try:
            metadata = self._load_metadata(plugin_json)

            # Validate compatibility
            if not self._check_compatibility(metadata):
                logger.warning(
                    f"Plugin {metadata.name} v{metadata.version} is not compatible "
                    f"with luducat v{APP_VERSION}"
                )
                return

            # Store the plugin directory path for loading
            metadata.plugin_dir = plugin_dir

            # Mark bundled status
            metadata.is_bundled = metadata.name in self._get_bundled_names()

            self._discovered[metadata.name] = metadata

            # Populate class-level registries for UI lookups
            PluginManager._display_names[metadata.name] = metadata.display_name
            PluginManager._plugin_types[metadata.name] = metadata.plugin_types
            PluginManager._plugin_capabilities[metadata.name] = dict(metadata.capabilities)
            if metadata.brand_colors:
                PluginManager._brand_colors[metadata.name] = metadata.brand_colors
            if metadata.badge_label:
                PluginManager._badge_labels[metadata.name] = metadata.badge_label
            else:
                PluginManager._badge_labels[metadata.name] = metadata.display_name.upper()[:3]
            if metadata.hidden:
                PluginManager._hidden_plugins.add(metadata.name)
            logger.debug(f"Discovered plugin: {metadata.name} v{metadata.version}")

        except Exception as e:
            logger.error(f"Failed to load plugin metadata from {plugin_dir}: {e}")

    def _load_metadata(self, plugin_json: Path) -> PluginMetadata:
        """Load and parse plugin.json

        Args:
            plugin_json: Path to plugin.json file

        Returns:
            PluginMetadata object

        Raises:
            ValueError: If metadata is invalid
        """
        with open(plugin_json) as f:
            data = json.load(f)

        # Required fields (store_class OR provider_class depending on type)
        required_base = ["name", "version", "author", "description"]
        for fld in required_base:
            if fld not in data:
                raise ValueError(f"Missing required field: {fld}")

        # Check for class path based on plugin type
        plugin_types = data.get("plugin_types", ["store"])
        has_store_class = "store_class" in data
        has_provider_class = "provider_class" in data
        has_entry_point = "entry_point" in data

        if "store" in plugin_types and not has_store_class:
            raise ValueError("Store plugin missing required field: store_class")
        if "metadata" in plugin_types and not has_store_class and not has_provider_class:
            raise ValueError("Metadata plugin missing required field: provider_class")
        if "platform" in plugin_types and not has_entry_point:
            raise ValueError("Platform plugin missing required field: entry_point")
        if "runner" in plugin_types and not has_entry_point:
            raise ValueError("Runner plugin missing required field: entry_point")

        # Build metadata object
        metadata = PluginMetadata(
            name=data["name"],
            display_name=data.get("display_name", data["name"]),
            version=data["version"],
            author=data["author"],
            description=data["description"],
            min_luducat_version=data.get("min_luducat_version", "0.1.0"),
            max_luducat_version=data.get("max_luducat_version"),
            store_class=data.get("store_class"),
            provider_class=data.get("provider_class"),
            entry_point=data.get("entry_point"),
            author_email=data.get("author_email"),
            homepage=data.get("homepage"),
            icon=data.get("icon"),
        )

        # Dependencies
        deps = data.get("dependencies", {})
        metadata.python_version = deps.get("python", ">=3.10")
        metadata.packages = deps.get("packages", [])

        # Platforms
        if "platforms" in data:
            metadata.platforms = data["platforms"]

        # Capabilities
        if "capabilities" in data:
            metadata.capabilities = data["capabilities"]

        # Settings schema
        if "settings_schema" in data:
            metadata.settings_schema = data["settings_schema"]

        # Per-field metadata declarations
        if "provides_fields" in data:
            metadata.provides_fields = data["provides_fields"]

        # Auth declaration
        if "auth" in data:
            metadata.auth = data["auth"]

        # Plugin types (defaults to ["store"] for backwards compatibility)
        metadata.plugin_types = data.get("plugin_types", ["store"])

        # Brand colors
        if "brand_colors" in data:
            metadata.brand_colors = data["brand_colors"]

        # Badge label (3-char abbreviation for UI badges)
        if "badge_label" in data:
            metadata.badge_label = data["badge_label"]

        # Network configuration
        if "network" in data:
            metadata.network = data["network"]

        # Privacy declaration
        if "privacy" in data:
            metadata.privacy = data["privacy"]

        # Runner plugin configuration
        if "runner" in data:
            metadata.runner_config = data["runner"]

        # Custom config dialog class
        if "config_dialog_class" in data:
            metadata.config_dialog_class = data["config_dialog_class"]

        # Declarative config actions
        if "config_actions" in data:
            metadata.config_actions = data["config_actions"]

        # Custom settings group title/description
        if "settings_title" in data:
            metadata.settings_title = data["settings_title"]
        if "settings_description" in data:
            metadata.settings_description = data["settings_description"]

        # Hidden flag (plugin not shown in Settings plugin list)
        if data.get("hidden"):
            metadata.hidden = True

        # Multi-store flag (engine spawns multiple virtual stores)
        if data.get("multi_store"):
            metadata.multi_store = True

        # Credentials
        creds = data.get("credentials", {})
        metadata.use_system_keyring = creds.get("use_system_keyring", True)
        metadata.keyring_service = creds.get("keyring_service")

        return metadata

    def _check_compatibility(self, metadata: PluginMetadata) -> bool:
        """Check if plugin is compatible with current app version

        Args:
            metadata: Plugin metadata

        Returns:
            True if compatible
        """
        app_ver = version.parse(APP_VERSION)
        min_ver = version.parse(metadata.min_luducat_version)

        if app_ver < min_ver:
            return False

        if metadata.max_luducat_version:
            max_ver = version.parse(metadata.max_luducat_version)
            if app_ver > max_ver:
                return False

        return True

    # ── Import Audit ──────────────────────────────────────────────

    # Patterns that indicate SDK boundary violations
    _AUDIT_CORE_RE = __import__("re").compile(
        r"^\s*(?:from\s+luducat\.(?:core|utils|ui)\b|import\s+luducat\.(?:core|utils|ui)\b)"
    )
    _AUDIT_TELEMETRY_LIBS = frozenset({
        "sentry_sdk", "sentry", "raven", "mixpanel", "amplitude",
        "posthog", "datadog", "ddtrace", "newrelic", "bugsnag",
        "rollbar", "airbrake",
    })
    _AUDIT_TELEMETRY_RE = __import__("re").compile(
        r"^\s*(?:import|from)\s+("
        + "|".join(_AUDIT_TELEMETRY_LIBS)
        + r")\b"
    )

    def _audit_plugin_imports(self, plugin_dir: Path, metadata: PluginMetadata) -> bool:
        """Audit a plugin's source files for import violations.

        Telemetry imports are always blocked (even bundled).
        Core imports: blocked for third-party, warn-only for bundled.

        Returns:
            True if plugin is safe to load, False if blocked.
        """
        telemetry_violations = []
        core_violations = []

        for py_file in plugin_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            rel_path = str(py_file.relative_to(plugin_dir))
            for line_num, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # Check for telemetry imports (always blocked)
                if self._AUDIT_TELEMETRY_RE.match(stripped):
                    telemetry_violations.append(
                        f"  {rel_path}:{line_num}: Telemetry library: {stripped}"
                    )

                # Check for core imports (blocked for third-party)
                elif self._AUDIT_CORE_RE.match(stripped):
                    # Allow TYPE_CHECKING imports
                    lines = content.splitlines()
                    in_type_checking = False
                    for i in range(line_num - 2, -1, -1):
                        s = lines[i].strip()
                        if s.startswith("if TYPE_CHECKING"):
                            in_type_checking = True
                            break
                        if s and not s.startswith(("#", "from", "import")):
                            break
                    if not in_type_checking:
                        core_violations.append(
                            f"  {rel_path}:{line_num}: Core import: {stripped}"
                        )

        # Telemetry is always blocked, no exceptions
        if telemetry_violations:
            logger.warning(
                "Plugin %s blocked — telemetry imports:\n%s",
                metadata.name, "\n".join(telemetry_violations),
            )
            return False

        if not core_violations:
            return True

        if metadata.is_bundled:
            # Bundled plugins: warn only for core imports
            logger.debug(
                "Import audit warnings for bundled plugin %s:\n%s",
                metadata.name, "\n".join(core_violations),
            )
            return True
        else:
            # Third-party: block loading
            logger.warning(
                "Plugin %s blocked — import violations:\n%s",
                metadata.name, "\n".join(core_violations),
            )
            return False

    def load_plugin(self, plugin_name: str) -> LoadedPlugin:
        """Load and instantiate a plugin

        Supports both store plugins and metadata plugins.

        Args:
            plugin_name: Plugin identifier

        Returns:
            LoadedPlugin container

        Raises:
            PluginError: If plugin cannot be loaded
        """
        if plugin_name in self._loaded:
            return self._loaded[plugin_name]

        if plugin_name not in self._discovered:
            raise PluginError(f"Plugin not found: {plugin_name}")

        metadata = self._discovered[plugin_name]
        # Use stored plugin_dir from discovery (handles platforms/ subdirectory)
        plugin_dir = metadata.plugin_dir or self.plugins_dir / plugin_name

        # Import audit — blocks untrusted third-party plugins with violations
        if not self._audit_plugin_imports(plugin_dir, metadata):
            raise PluginError(
                f"Plugin {plugin_name} failed import audit — "
                f"contains forbidden imports. See log for details."
            )

        # Integrity verification — check if plugin passed fingerprint check
        verification = self._verification_results.get(plugin_name)
        if verification:
            from .plugin_verifier import TrustState
            if verification.trust_state == TrustState.MISMATCH:
                # Disable the plugin — possible tampering
                logger.warning(
                    "Plugin '%s' DISABLED — fingerprint mismatch (possible tampering)",
                    plugin_name,
                )
                if hasattr(self, '_trust_store'):
                    self._trust_store.disable_plugin(plugin_name, "fingerprint_mismatch")
                    self._trust_store.save()
                loaded = LoadedPlugin(
                    metadata=metadata,
                    instance=None,
                    enabled=False,
                    error="Integrity check failed — plugin disabled for safety",
                    trust_state=verification.trust_state,
                    trust_tier=verification.trust_tier,
                    fingerprint=verification.fingerprint,
                )
                self._loaded[plugin_name] = loaded
                return loaded

        try:
            # Determine which class to import based on plugin type
            is_metadata_plugin = "metadata" in metadata.plugin_types
            is_store_plugin = "store" in metadata.plugin_types
            is_platform_plugin = "platform" in metadata.plugin_types
            is_runner_plugin = "runner" in metadata.plugin_types

            if is_runner_plugin and metadata.entry_point:
                # Runner plugin - use entry_point
                class_path = metadata.entry_point
                plugin_class = self._import_plugin_class(
                    plugin_dir, class_path, AbstractRunnerPlugin
                )
            elif is_platform_plugin and metadata.entry_point:
                # Platform plugin - use entry_point
                class_path = metadata.entry_point
                plugin_class = self._import_plugin_class(
                    plugin_dir, class_path, AbstractPlatformProvider
                )
            elif is_metadata_plugin and metadata.provider_class:
                # Metadata plugin - use provider_class
                class_path = metadata.provider_class
                plugin_class = self._import_plugin_class(
                    plugin_dir, class_path, AbstractMetadataProvider
                )
            elif is_store_plugin and metadata.store_class:
                # Store plugin - use store_class
                class_path = metadata.store_class
                plugin_class = self._import_plugin_class(
                    plugin_dir, class_path, AbstractGameStore
                )
            else:
                raise PluginError(
                    f"Plugin {plugin_name} has no valid class path for its type"
                )

            # Create plugin directories
            # Plugin data (databases) is in data_dir/plugins-data/
            # Plugin cache (images) is in cache_dir/plugins/
            plugin_data_dir = self.data_dir / "plugins-data" / plugin_name
            plugin_cache_dir = self.cache_dir / "plugins" / plugin_name
            if COMPILED_BUILD:
                # Compiled builds: no separate user config dir for plugins
                # (plugin code lives in app dir, data/cache in user XDG dirs)
                plugin_config_dir = plugin_data_dir
            else:
                # Dev/source: plugin config in config_dir/plugins/
                plugin_config_dir = self.config_dir / "plugins" / plugin_name

            # Instantiate plugin
            instance = plugin_class(
                config_dir=plugin_config_dir,
                cache_dir=plugin_cache_dir,
                data_dir=plugin_data_dir,
            )

            # Inject dependencies
            # Platform plugins don't need credential manager
            if hasattr(instance, 'set_credential_manager'):
                instance.set_credential_manager(self.credential_manager)
            if hasattr(instance, 'set_settings'):
                instance.set_settings(self.config.get_plugin_settings(plugin_name))
            if hasattr(instance, 'set_local_data_consent'):
                consent = self.config.get("privacy.local_data_access_consent", False)
                instance.set_local_data_consent(consent)
            if hasattr(instance, 'set_storage'):
                instance.set_storage(PluginStorage(
                    plugin_name=plugin_name,
                    config_dir=plugin_config_dir,
                    cache_dir=plugin_cache_dir,
                    data_dir=plugin_data_dir,
                ))

            # Register plugin with NetworkManager and inject PluginHttpClient
            if hasattr(instance, 'set_http_client'):
                network_cfg = metadata.network
                allowed_domains = network_cfg.get("allowed_domains", [])
                rate_limits = network_cfg.get("rate_limits", None)
                self._network_manager.register_plugin(
                    plugin_name,
                    allowed_domains=allowed_domains,
                    rate_limits=rate_limits,
                )
                instance.set_http_client(PluginHttpClient(plugin_name))

            # Check if enabled in config
            plugin_settings = self.config.get_plugin_settings(plugin_name)
            enabled = plugin_settings.get("enabled", True)

            # Multi-store: engine spawns virtual store instances
            if metadata.multi_store and hasattr(instance, 'get_store_instances'):
                loaded = LoadedPlugin(
                    metadata=metadata,
                    instance=instance,
                    enabled=enabled,
                    trust_state=verification.trust_state if verification else None,
                    trust_tier=verification.trust_tier if verification else None,
                    fingerprint=verification.fingerprint if verification else None,
                )
                self._loaded[plugin_name] = loaded

                if enabled:
                    virtual_stores = instance.get_store_instances()
                    for vs in virtual_stores:
                        self._register_virtual_store(
                            vs, metadata, verification, plugin_name,
                        )
                    logger.info(
                        "Loaded multi-store engine: %s (%d virtual stores)",
                        plugin_name, len(virtual_stores),
                    )

                return loaded

            loaded = LoadedPlugin(
                metadata=metadata,
                instance=instance,
                enabled=enabled,
                trust_state=verification.trust_state if verification else None,
                trust_tier=verification.trust_tier if verification else None,
                fingerprint=verification.fingerprint if verification else None,
            )

            self._loaded[plugin_name] = loaded

            if enabled:
                instance.on_enable()
                if is_runner_plugin:
                    plugin_type = "runner"
                elif is_platform_plugin:
                    plugin_type = "platform"
                elif is_metadata_plugin:
                    plugin_type = "metadata"
                else:
                    plugin_type = "store"
                logger.info(
                    f"Loaded {plugin_type} plugin: {plugin_name} v{metadata.version}"
                )

            return loaded

        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}")
            loaded = LoadedPlugin(
                metadata=metadata,
                instance=None,  # type: ignore
                enabled=False,
                error=str(e),
            )
            self._loaded[plugin_name] = loaded
            raise PluginError(f"Failed to load plugin {plugin_name}: {e}") from e

    def _register_virtual_store(
        self,
        vs,
        parent_metadata: PluginMetadata,
        verification,
        engine_name: str,
    ) -> None:
        """Register a virtual store spawned by a multi_store engine.

        Each virtual store gets its own entry in _loaded, _brand_colors,
        _badge_labels, _display_names, etc. — making it indistinguishable
        from a regular store plugin in the UI and sync pipeline.
        """
        vs_name = vs.store_name

        # Inject dependencies into the virtual store
        if hasattr(vs, 'set_credential_manager'):
            vs.set_credential_manager(self.credential_manager)
        if hasattr(vs, 'set_settings'):
            vs.set_settings(self.config.get_plugin_settings(vs_name))
        if hasattr(vs, 'set_local_data_consent'):
            consent = self.config.get("privacy.local_data_access_consent", False)
            vs.set_local_data_consent(consent)
        if hasattr(vs, 'set_storage'):
            vs.set_storage(PluginStorage(
                plugin_name=vs_name,
                config_dir=vs.config_dir,
                cache_dir=vs.cache_dir,
                data_dir=vs.data_dir,
            ))

        # Register with NetworkManager — derive domains from ruleset
        if hasattr(vs, 'set_http_client') and hasattr(vs, '_ruleset'):
            domains = vs._ruleset.domains
            rate = vs._ruleset.rate_limit
            rate_limits = None
            if rate:
                rate_limits = {
                    d: {"requests": rate.get("calls", 60),
                        "window": rate.get("window_seconds", 60)}
                    for d in domains
                }
            self._network_manager.register_plugin(
                vs_name,
                allowed_domains=domains,
                rate_limits=rate_limits,
            )
            vs.set_http_client(PluginHttpClient(vs_name))

        # Populate class-level registries so UI can find this store
        ruleset = vs._ruleset
        brand = ruleset.brand_colors
        if brand:
            colors = {
                "bg": brand.get("badge_bg", brand.get("bg", "#2a2a2a")),
                "text": brand.get("badge_text", brand.get("text", "#ffffff")),
            }
            if brand.get("badge_heart_color"):
                colors["heart"] = brand["badge_heart_color"]
            PluginManager._brand_colors[vs_name] = colors
        PluginManager._display_names[vs_name] = ruleset.display_name
        PluginManager._plugin_types[vs_name] = ["store"]
        PluginManager._badge_labels[vs_name] = ruleset.badge_label
        PluginManager._plugin_capabilities[vs_name] = dict(parent_metadata.capabilities)

        # Build auth metadata and settings_schema from ruleset auth config
        vs_auth = dict(ruleset.auth) if ruleset.auth else {"type": "none"}
        vs_settings_schema = {}
        auth_type = vs_auth.get("type", "none")

        if auth_type == "bearer_redirect":
            # Login handled by VirtualStore.get_config_actions() (native dialog)
            # No settings_schema needed — token stored via login flow
            pass
        elif auth_type == "api_token":
            vs_settings_schema["api_token"] = {
                "type": "string",
                "label": "API Token",
                "description": "Your API token for this store.",
                "secret": True,
            }

        # Derive provides_fields from ruleset field declarations so
        # build_from_plugins() adds this store to FIELD_SOURCE_CAPABILITIES
        vs_provides_fields: Dict[str, Dict[str, Any]] = {}
        lib_fields = set(ruleset.library.get("fields", {}).keys())
        det_fields = set(ruleset.detail.get("fields", {}).keys()) if ruleset.detail else set()
        for rf in lib_fields | det_fields:
            standard = _RULESET_TO_STANDARD.get(rf)
            if standard and standard not in vs_provides_fields:
                vs_provides_fields[standard] = {"priority": 50}

        # Create a virtual PluginMetadata for this store
        vs_metadata = PluginMetadata(
            name=vs_name,
            display_name=ruleset.display_name,
            version=parent_metadata.version,
            author=parent_metadata.author,
            description=f"Virtual store: {ruleset.display_name}",
            min_luducat_version=parent_metadata.min_luducat_version,
            plugin_types=["store"],
            brand_colors=PluginManager._brand_colors.get(vs_name, {}),
            badge_label=ruleset.badge_label,
            capabilities=dict(parent_metadata.capabilities),
            is_bundled=parent_metadata.is_bundled,
            auth=vs_auth,
            settings_schema=vs_settings_schema,
            provides_fields=vs_provides_fields,
        )

        # Seed config.toml enabled flag (so sync-all and sync menu find this store)
        config_key = f"plugins.{vs_name}.enabled"
        if self.config.get(config_key) is None:
            self.config.set(config_key, True)
            self.config.save()

        vs_enabled = self.config.get(config_key, True)

        # Register as loaded plugin
        vs_loaded = LoadedPlugin(
            metadata=vs_metadata,
            instance=vs,
            enabled=vs_enabled,
            trust_state=verification.trust_state if verification else None,
            trust_tier=verification.trust_tier if verification else None,
            fingerprint=verification.fingerprint if verification else None,
        )
        self._loaded[vs_name] = vs_loaded

        # Also add to _discovered so get_store_plugin_names() finds it
        self._discovered[vs_name] = vs_metadata

        if vs_enabled:
            vs.on_enable()
        logger.info("Registered virtual store: %s (%s, enabled=%s)", vs_name, ruleset.display_name, vs_enabled)

    def _import_plugin_class(
        self, plugin_dir: Path, class_path: str, expected_base: type
    ) -> type:
        """Import plugin class from plugin directory

        Args:
            plugin_dir: Plugin directory
            class_path: Dot-path to class (e.g., "store.SteamStore" or "provider.IgdbProvider")
            expected_base: Expected base class (AbstractGameStore or AbstractMetadataProvider)

        Returns:
            Plugin class (not instantiated)
        """
        parts = class_path.rsplit(".", 1)
        if len(parts) == 2:
            module_name, class_name = parts
        else:
            module_name = class_path
            class_name = class_path

        plugin_name = plugin_dir.name

        # Add parent of plugin dir to path so plugin is importable as package
        plugins_dir_str = str(plugin_dir.parent)
        plugin_dir_str = str(plugin_dir)

        if plugins_dir_str not in sys.path:
            sys.path.insert(0, plugins_dir_str)

        try:
            # First, set up the plugin as a package by importing __init__.py
            init_file = plugin_dir / "__init__.py"
            if init_file.exists():
                init_spec = importlib.util.spec_from_file_location(
                    plugin_name,
                    init_file,
                    submodule_search_locations=[plugin_dir_str]
                )
                if init_spec and init_spec.loader:
                    init_module = importlib.util.module_from_spec(init_spec)
                    init_module.__package__ = plugin_name
                    init_module.__path__ = [plugin_dir_str]
                    sys.modules[plugin_name] = init_module
                    init_spec.loader.exec_module(init_module)

            # Now import the plugin module as part of the package
            full_module_name = f"{plugin_name}.{module_name}"
            module_file = plugin_dir / f"{module_name}.py"

            spec = importlib.util.spec_from_file_location(
                full_module_name,
                module_file,
                submodule_search_locations=[plugin_dir_str]
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot find module: {module_name}")

            module = importlib.util.module_from_spec(spec)
            module.__package__ = plugin_name
            sys.modules[full_module_name] = module
            spec.loader.exec_module(module)

            # Get the class
            plugin_class = getattr(module, class_name)

            if not issubclass(plugin_class, expected_base):
                raise TypeError(
                    f"{class_name} must inherit from {expected_base.__name__}"
                )

            return plugin_class

        finally:
            # Don't remove from path - other imports may need it
            pass

    # Keep old method name for backwards compatibility
    def _import_store_class(self, plugin_dir: Path, class_path: str) -> Type[AbstractGameStore]:
        """Import store class from plugin (backwards compatibility)"""
        return self._import_plugin_class(plugin_dir, class_path, AbstractGameStore)

    def load_plugin_class(self, plugin_name: str, class_path: str) -> type:
        """Load an arbitrary class from a plugin directory.

        Used for dynamic loading of plugin-provided UI classes (e.g.,
        config dialogs) without hardcoding imports in core/UI code.

        Args:
            plugin_name: Plugin identifier (must be discovered)
            class_path: Dot-path to class (e.g., "config_dialog.EpicConfigDialog")

        Returns:
            The class object (not instantiated)

        Raises:
            PluginError: If plugin not found or class loading fails
        """
        metadata = self._discovered.get(plugin_name)
        if not metadata or not metadata.plugin_dir:
            raise PluginError(f"Plugin '{plugin_name}' not discovered")

        parts = class_path.rsplit(".", 1)
        if len(parts) != 2:
            raise PluginError(f"Invalid class path: {class_path}")

        module_name, class_name = parts
        plugin_dir = metadata.plugin_dir

        plugins_dir_str = str(plugin_dir.parent)
        plugin_dir_str = str(plugin_dir)

        if plugins_dir_str not in sys.path:
            sys.path.insert(0, plugins_dir_str)

        full_module_name = f"{plugin_dir.name}.{module_name}"
        module_file = plugin_dir / f"{module_name}.py"

        spec = importlib.util.spec_from_file_location(
            full_module_name,
            module_file,
            submodule_search_locations=[plugin_dir_str],
        )
        if spec is None or spec.loader is None:
            raise PluginError(f"Cannot find module: {module_name}")

        module = importlib.util.module_from_spec(spec)
        module.__package__ = plugin_dir.name
        sys.modules[full_module_name] = module
        spec.loader.exec_module(module)

        return getattr(module, class_name)

    def load_enabled_plugins(self) -> Dict[str, LoadedPlugin]:
        """Load all enabled plugins

        Returns:
            Dict of loaded plugins
        """
        for name, metadata in list(self._discovered.items()):
            plugin_settings = self.config.get_plugin_settings(name)
            enabled = plugin_settings.get("enabled", True)

            if enabled:
                try:
                    self.load_plugin(name)
                except PluginError as e:
                    logger.error(f"Failed to load enabled plugin {name}: {e}")

        return self._loaded

    def get_loaded_plugin(self, plugin_name: str) -> Optional["LoadedPlugin"]:
        """Get a LoadedPlugin container by name.

        Unlike get_plugin() which returns just the instance, this returns
        the full LoadedPlugin with metadata, enabled status, etc.

        Args:
            plugin_name: Plugin identifier

        Returns:
            LoadedPlugin or None if not loaded
        """
        return self._loaded.get(plugin_name)

    def get_plugin(self, plugin_name: str) -> Optional[AbstractGameStore]:
        """Get a loaded plugin instance

        Args:
            plugin_name: Plugin identifier

        Returns:
            Plugin instance or None if not loaded
        """
        loaded = self._loaded.get(plugin_name)
        if loaded and loaded.enabled and loaded.instance:
            return loaded.instance
        return None

    def is_plugin_enabled(self, plugin_name: str) -> bool:
        """Check if a plugin is loaded and enabled."""
        loaded = self._loaded.get(plugin_name)
        return loaded is not None and loaded.enabled

    def get_all_plugins(self) -> Dict[str, AbstractGameStore]:
        """Get all loaded and enabled plugin instances

        Returns:
            Dict mapping plugin name to instance
        """
        return {
            name: loaded.instance
            for name, loaded in self._loaded.items()
            if loaded.enabled and loaded.instance
        }

    def get_plugins_by_type(self, plugin_type: PluginType) -> Dict[str, AbstractGameStore]:
        """Get all loaded plugins that implement a specific type

        Args:
            plugin_type: The plugin type to filter by (e.g., PluginType.STORE)

        Returns:
            Dict mapping plugin name to instance for plugins of that type
        """
        result = {}
        type_value = plugin_type.value

        for name, loaded in self._loaded.items():
            if not loaded.enabled or not loaded.instance:
                continue

            # Check if plugin implements this type
            if type_value in loaded.metadata.plugin_types:
                result[name] = loaded.instance

        return result

    def get_store_plugins(self) -> Dict[str, StorePlugin]:
        """Get all loaded store plugins (convenience method)

        Returns:
            Dict mapping plugin name to StorePlugin instance
        """
        return self.get_plugins_by_type(PluginType.STORE)

    def get_metadata_plugins(self) -> Dict[str, MetadataPlugin]:
        """Get all loaded metadata plugins (convenience method)

        Returns:
            Dict mapping plugin name to MetadataPlugin instance
        """
        return self.get_plugins_by_type(PluginType.METADATA)

    def get_platform_plugins(self) -> Dict[str, PlatformPlugin]:
        """Get all loaded platform plugins (convenience method)

        Returns:
            Dict mapping plugin name to PlatformPlugin instance
        """
        return self.get_plugins_by_type(PluginType.PLATFORM)

    def get_runner_plugins(self) -> Dict[str, RunnerPlugin]:
        """Get all loaded runner plugins (convenience method).

        Returns:
            Dict mapping plugin name to RunnerPlugin instance
        """
        return self.get_plugins_by_type(PluginType.RUNNER)

    def refresh_plugin_settings(self, plugin_name: str) -> None:
        """Refresh settings for a plugin from config file.

        Call this before sync to pick up any settings changes made
        since the plugin was loaded.

        Args:
            plugin_name: Plugin identifier
        """
        loaded = self._loaded.get(plugin_name)
        if loaded and loaded.instance:
            fresh_settings = self.config.get_plugin_settings(plugin_name)
            loaded.instance.set_settings(fresh_settings)
            # Also refresh consent flag
            if hasattr(loaded.instance, 'set_local_data_consent'):
                consent = self.config.get("privacy.local_data_access_consent", False)
                loaded.instance.set_local_data_consent(consent)
            logger.debug(f"Refreshed settings for plugin {plugin_name}")

    def refresh_all_consent(self) -> None:
        """Refresh privacy consent flag on all loaded plugins.

        Call this after the user changes the consent setting in
        Settings -> Privacy so plugins pick it up immediately.
        """
        consent = self.config.get("privacy.local_data_access_consent", False)
        for loaded in self._loaded.values():
            if loaded.instance and hasattr(loaded.instance, 'set_local_data_consent'):
                loaded.instance.set_local_data_consent(consent)

    def persist_plugin_settings(self, plugin_name: str) -> None:
        """Persist plugin's in-memory settings back to config file.

        Call this after sync to save any settings changes made by the plugin
        (e.g., cached family group info, author hit counters).

        Uses MERGE rather than replace — the plugin's _settings is a deep copy
        taken at load time, so it may be missing keys that UI dialogs added
        to the config section later (e.g., steam_id_resolved_at, asset_counts_cache).
        Merging preserves those keys while picking up runtime changes from the plugin.

        Args:
            plugin_name: Plugin identifier
        """
        loaded = self._loaded.get(plugin_name)
        if loaded and loaded.instance:
            runtime_settings = loaded.instance._settings
            # Start from the current config (includes dialog-written keys)
            merged = self.config.get_plugin_settings(plugin_name)
            # Layer runtime changes on top (hit counters, etc.)
            merged.update(runtime_settings)
            self.config.set_plugin_settings(plugin_name, merged)
            logger.debug(f"Persisted settings for plugin {plugin_name}")

    def get_discovered_plugins(self) -> Dict[str, PluginMetadata]:
        """Get all discovered plugin metadata

        Returns:
            Dict mapping plugin name to metadata
        """
        return dict(self._discovered)

    def enable_plugin(self, plugin_name: str) -> bool:
        """Enable a plugin

        Args:
            plugin_name: Plugin identifier

        Returns:
            True if enabled successfully
        """
        if plugin_name not in self._discovered:
            return False

        # Update config
        settings = self.config.get_plugin_settings(plugin_name)
        settings["enabled"] = True
        self.config.set_plugin_settings(plugin_name, settings)

        # Load if not loaded
        if plugin_name not in self._loaded:
            try:
                self.load_plugin(plugin_name)
            except PluginError:
                return False
        else:
            loaded = self._loaded[plugin_name]
            loaded.enabled = True
            if loaded.instance:
                loaded.instance.on_enable()

        return True

    def disable_plugin(self, plugin_name: str) -> bool:
        """Disable a plugin

        Args:
            plugin_name: Plugin identifier

        Returns:
            True if disabled successfully
        """
        if plugin_name not in self._loaded:
            return False

        loaded = self._loaded[plugin_name]

        if loaded.instance:
            loaded.instance.on_disable()

        loaded.enabled = False

        # Update config
        settings = self.config.get_plugin_settings(plugin_name)
        settings["enabled"] = False
        self.config.set_plugin_settings(plugin_name, settings)

        return True

    def inject_main_db_accessors(self, game_service) -> None:
        """Inject MainDbAccessor into all store plugins.

        Called by MainWindow after GameService is created.
        Must be called before any sync or DB access operations.
        """
        from .db_accessor import MainDbAccessor

        for name, loaded in self._loaded.items():
            instance = loaded.instance
            if isinstance(instance, AbstractGameStore) and hasattr(instance, 'set_main_db_accessor'):
                accessor = MainDbAccessor(game_service, instance.store_name)
                instance.set_main_db_accessor(accessor)
                logger.debug("Injected MainDbAccessor for plugin %s", name)

    def close(self) -> None:
        """Close all plugins and clean up.

        Persists in-memory settings (e.g. author hit counters) before
        closing instances, so runtime-accumulated data survives restart.
        """
        # Log trust state at shutdown (forensic trail)
        self.log_trust_state_shutdown()

        for name, loaded in self._loaded.items():
            if loaded.instance:
                try:
                    self.persist_plugin_settings(name)
                except Exception as e:
                    logger.debug(f"Failed to persist settings for {name}: {e}")
                try:
                    loaded.instance.close()
                except Exception as e:
                    logger.error(f"Error closing plugin {name}: {e}")

        self._loaded.clear()

        # Close NetworkManager (closes all plugin sessions)
        if hasattr(self, '_network_manager') and self._network_manager:
            self._network_manager.close()

        logger.debug("Plugin manager closed")
