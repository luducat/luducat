# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config.py

"""Configuration management for luducat

Uses TOML format and follows XDG Base Directory specification.
Handles configuration migration across versions.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import platformdirs

import tomllib

import tomli_w

from .constants import (
    APP_NAME,
    APP_VERSION,
    CONFIG_VERSION,
    DEFAULT_CACHE_CLEANUP_DAYS,
    DEFAULT_GRID_DENSITY,
    DEFAULT_LIST_PANEL_WIDTH,
    DEFAULT_SCREENSHOT_CACHE_MB,
    DEFAULT_THUMBNAIL_CACHE_MB,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    FILTER_ALL,
    SORT_MODE_NAME,
    VIEW_MODE_COVER,
)

logger = logging.getLogger(__name__)

# Directory overrides — set once during startup via apply_dir_overrides()
_data_dir_override: Optional[Path] = None
_cache_dir_override: Optional[Path] = None


def get_config_dir() -> Path:
    """Get XDG config directory for luducat"""
    return Path(platformdirs.user_config_dir(APP_NAME))


def get_data_dir() -> Path:
    """Get data directory for luducat (respects custom_data_dir override)"""
    if _data_dir_override:
        return _data_dir_override
    return Path(platformdirs.user_data_dir(APP_NAME))


def get_cache_dir() -> Path:
    """Get cache directory for luducat (respects custom_cache_dir override)"""
    if _cache_dir_override:
        return _cache_dir_override
    return Path(platformdirs.user_cache_dir(APP_NAME))


def apply_dir_overrides(config: "Config") -> None:
    """Set directory overrides from config. Called once during startup."""
    global _data_dir_override, _cache_dir_override
    from .directory_health import check_directory

    custom_data = config.get("app.custom_data_dir", "")
    if custom_data:
        health = check_directory(Path(custom_data))
        if health.reachable and health.writable:
            _data_dir_override = Path(custom_data)
            logger.info(f"Data directory override: {_data_dir_override}")
        else:
            logger.warning(f"Ignoring custom data dir (unhealthy): {health.tooltip}")

    custom_cache = config.get("app.custom_cache_dir", "")
    if custom_cache:
        health = check_directory(Path(custom_cache))
        if health.reachable and health.writable:
            _cache_dir_override = Path(custom_cache)
            logger.info(f"Cache directory override: {_cache_dir_override}")
        else:
            logger.warning(f"Ignoring custom cache dir (unhealthy): {health.tooltip}")


# Default configuration structure
DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "version": APP_VERSION,
        "config_version": CONFIG_VERSION,
        "first_run": True,
        "last_news_version": "",  # Track last viewed news version for auto-show
        "check_for_updates": False,  # Opt-in, default OFF
        "update_dismissed_version": "",  # Version user dismissed (don't nag again)
        "language": "",           # "" = auto-detect, or "en", "de", "pt_BR", etc.
        "custom_data_dir": "",   # Empty = use XDG default
        "custom_cache_dir": "",  # Empty = use XDG default
    },
    "ui": {
        "window_width": DEFAULT_WINDOW_WIDTH,
        "window_height": DEFAULT_WINDOW_HEIGHT,
        "window_maximized": False,
        "list_panel_width": DEFAULT_LIST_PANEL_WIDTH,
        "view_mode": VIEW_MODE_COVER,
        "sort_mode": SORT_MODE_NAME,
        "sort_reverse": False,
        "favorites_first": False,
    },
    "appearance": {
        "ui_zoom": 100,
        "theme_override": "auto",  # auto, light, dark
        "cover_grid_density": 150,
        "screenshot_grid_density": DEFAULT_GRID_DENSITY,
        "cover_scaling": "stretch",
        "quick_tag_count": 5,  # Max quick-access tags in crumb bar
    },
    "filters": {
        "quick_filter": FILTER_ALL,
        "active_stores": [],  # Empty = all stores
        "active_tags": [],
    },
    "sync": {
        "auto_sync_on_startup": False,
        "preferred_browser": "auto",  # auto, or browser key: firefox, chrome, etc.
    },
    "privacy": {
        "local_data_access_consent": False,  # Consent for browser cookies + local launcher data
    },
    "content_filter": {
        "enabled": True,             # Hide games that exceed adult confidence threshold
        "threshold": 0.60,           # 0.0–1.0; lower = stricter, higher = more permissive
    },
    "tags": {
        "default_sync_mode": "add_only",       # "add_only" | "full_sync" | "none"
        "source_colors": False,                 # Show brand color accent on imported tags
        "suppress_deleted_reimport": True,      # Deleted imported tags won't reappear on sync
        "plugin_overrides": {},                 # Per-plugin: {steam: {enabled, sync_mode}, ...}
        "suppressed_imports": [],               # List of {name, source} dicts for suppressed reimport
    },
    "cache": {
        "thumbnail_max_size_mb": DEFAULT_THUMBNAIL_CACHE_MB,
        "screenshot_max_size_mb": DEFAULT_SCREENSHOT_CACHE_MB,
        "cache_cleanup_days": DEFAULT_CACHE_CLEANUP_DAYS,
        "ram_cache_mode": "auto",       # "auto" | "manual"
        "ram_cache_manual_mb": 0,       # Manual budget in MB (0 = use auto)
    },
    "backup": {
        "schedule_enabled": False,
        "interval_days": 1,
        "check_on_startup": True,
        "silent": False,
        "location": "",
        "last_backup": "",
        "retention_daily": 7,
        "retention_weekly": 4,
        "retention_monthly": 12,
        "retention_yearly": 1,
        "retention_acknowledged": False,
    },
    "desktop": {
        "icons_installed": False,  # Set True after installing icons to hicolor theme
    },
    "game_manager": {
        "install_root": "",  # Default game installation directory
        "auto_install_on_launch": False,  # Prompt to install if not installed
        "verify_before_launch": False,  # Verify checksums before launch
        "show_first_run_dialog": True,  # Show config dialog on first launch
    },
    "runtime_manager": {
        "auto_detect_platforms": True,  # Auto-detect installed platforms
        "download_missing_platforms": True,  # Offer to download DOSBox/ScummVM
    },
    "plugins": {},  # Plugin-specific settings go here
    "metadata_priority": {
        # Per-field priorities — seeded from _SEED_FIELD_PRIORITIES on first run.
        # Each field maps to a list of sources in priority order (highest first).
        # At runtime, MetadataResolver loads ONLY from here — no fallbacks.
        # IMPORTANT: keep this in sync with _SEED_FIELD_PRIORITIES in metadata_resolver.py.
        # For new installs this is used directly; for upgrades _migrate_to_v2() seeds from there.
    },
}


class Config:
    """Configuration manager with TOML persistence

    Handles loading, saving, and migrating configuration files.
    Configuration is stored at ~/.config/luducat/config.toml

    Usage:
        config = Config()
        config.load()

        # Get values
        view_mode = config.get("ui.view_mode")
        zoom = config.get("appearance.ui_zoom", default=100)

        # Set values
        config.set("ui.view_mode", "cover")
        config.save()

        # Get nested dict
        ui_settings = config.get("ui")
    """

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize config manager

        Args:
            config_path: Custom config file path (for testing).
                        Defaults to XDG config directory.
        """
        if config_path is None:
            self.config_dir = get_config_dir()
            self.config_path = self.config_dir / "config.toml"
        else:
            self.config_path = config_path
            self.config_dir = config_path.parent

        self.data_dir = get_data_dir()
        self.cache_dir = get_cache_dir()

        self._config: Dict[str, Any] = {}
        self._dirty = False

    def load(self) -> None:
        """Load configuration from disk

        Creates default config if file doesn't exist.
        Migrates config if version is outdated.
        """
        # Ensure directories exist and tighten permissions (owner-only)
        import platform
        import stat
        self._permission_warnings: list[tuple[Path, int]] = []
        for d in (self.config_dir, self.data_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
            # Skip Unix permission check on Windows — stat().st_mode returns
            # synthetic 0o777 on NTFS, causing false positives every startup.
            # Windows %APPDATA% directories are already user-only via ACLs.
            if platform.system() != "Windows":
                try:
                    import oschmod
                    # Check current permissions before setting
                    current = d.stat().st_mode & 0o777
                    if current != 0o700:
                        if current & (stat.S_IRWXG | stat.S_IRWXO):
                            logger.warning(
                                "Directory %s has loose permissions (%04o) — "
                                "tightening to 0700 (owner-only)",
                                d, current,
                            )
                            self._permission_warnings.append((d, current))
                        oschmod.set_mode(d, 0o700)
                except Exception:
                    pass  # Best effort — don't block startup

        # Health warnings for data/cache directories
        from .directory_health import check_directory
        for name, path in [("data", self.data_dir), ("cache", self.cache_dir)]:
            health = check_directory(path)
            if not health.writable:
                logger.warning(f"{name} directory not writable: {health.error} ({path})")
            elif health.status == "yellow":
                logger.warning(f"{name} directory low on space: {health.free_mb} MB free ({path})")

        if not self.config_path.exists():
            logger.info("Creating default configuration")
            self._config = self._deep_copy(DEFAULT_CONFIG)
            self.save()
            return

        try:
            with open(self.config_path, "rb") as f:
                self._config = tomllib.load(f)
            logger.debug(f"Loaded config from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            logger.info("Using default configuration")
            self._config = self._deep_copy(DEFAULT_CONFIG)
            return

        # Check version and migrate if needed
        config_version = self._config.get("app", {}).get("config_version", 0)
        if config_version < CONFIG_VERSION:
            self._migrate(config_version)

        # Merge any new default keys
        self._merge_defaults()

    def save(self) -> None:
        """Save configuration to disk"""
        self.config_dir.mkdir(parents=True, exist_ok=True)

        try:
            with open(self.config_path, "wb") as f:
                tomli_w.dump(self._config, f)
            self._dirty = False
            logger.debug(f"Saved config to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by dot-separated key

        Args:
            key: Dot-separated key path (e.g., "ui.view_mode")
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        parts = key.split(".")
        value = self._config

        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default

        return value

    def set(self, key: str, value: Any) -> None:
        """Set configuration value by dot-separated key

        If value is None the key is deleted instead (TOML cannot
        represent None, so storing it would crash on save).

        Args:
            key: Dot-separated key path (e.g., "ui.view_mode")
            value: Value to set (None deletes the key)
        """
        parts = key.split(".")
        config = self._config

        # Navigate to parent dict
        for part in parts[:-1]:
            if part not in config:
                config[part] = {}
            config = config[part]

        # Delete key when value is None (TOML has no null type)
        if value is None:
            config.pop(parts[-1], None)
        else:
            config[parts[-1]] = value
        self._dirty = True

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get entire configuration section

        Args:
            section: Section name (e.g., "ui", "plugins")

        Returns:
            Configuration section dict (copy)
        """
        return self._deep_copy(self._config.get(section, {}))

    def set_section(self, section: str, values: Dict[str, Any]) -> None:
        """Set entire configuration section

        Args:
            section: Section name
            values: Section values dict
        """
        self._config[section] = self._deep_copy(values)
        self._dirty = True

    @property
    def is_first_run(self) -> bool:
        """Check if this is first run of application"""
        return self.get("app.first_run", True)

    def mark_first_run_complete(self) -> None:
        """Mark first run as complete"""
        self.set("app.first_run", False)
        self.save()

    def get_last_news_version(self) -> str:
        """Get the last version where user viewed the News tab.

        Returns:
            Version string or empty string if never viewed
        """
        return self.get("app.last_news_version", "")

    def set_last_news_version(self, version: str) -> None:
        """Set the last viewed news version.

        Args:
            version: Version string to store
        """
        self.set("app.last_news_version", version)
        self.save()

    def get_plugin_settings(self, plugin_name: str) -> Dict[str, Any]:
        """Get settings for a specific plugin

        Args:
            plugin_name: Plugin identifier

        Returns:
            Plugin settings dict
        """
        plugins = self._config.get("plugins", {})
        return self._deep_copy(plugins.get(plugin_name, {}))

    def set_plugin_settings(self, plugin_name: str, settings: Dict[str, Any]) -> None:
        """Set settings for a specific plugin

        Args:
            plugin_name: Plugin identifier
            settings: Plugin settings dict
        """
        if "plugins" not in self._config:
            self._config["plugins"] = {}
        cleaned = self._strip_none(self._deep_copy(settings))
        self._config["plugins"][plugin_name] = cleaned
        self._dirty = True

    def get_metadata_priorities(self) -> Dict[str, list]:
        """Get metadata priority settings for all fields

        Returns:
            Dict mapping field_name -> list of sources in priority order.
            Empty dict if using defaults.
        """
        priorities = self._config.get("metadata_priority", {})
        return self._deep_copy(priorities)

    def set_metadata_priorities(self, priorities: Dict[str, list]) -> None:
        """Set metadata priority settings for all fields

        Args:
            priorities: Dict mapping field_name -> list of sources in priority order.
                        Pass empty dict to clear all overrides (use defaults).
        """
        self._config["metadata_priority"] = self._deep_copy(priorities)
        self._dirty = True

    def set_metadata_field_priority(self, field_name: str, priority: list) -> None:
        """Set priority for a single metadata field

        Args:
            field_name: Name of the field (e.g., "cover", "description")
            priority: List of sources in priority order
        """
        if "metadata_priority" not in self._config:
            self._config["metadata_priority"] = {}
        self._config["metadata_priority"][field_name] = self._deep_copy(priority)
        self._dirty = True

    def get_metadata_field_priority(self, field_name: str) -> list:
        """Get priority for a single metadata field

        Args:
            field_name: Name of the field

        Returns:
            List of sources in priority order, or empty list if using default
        """
        priorities = self._config.get("metadata_priority", {})
        return self._deep_copy(priorities.get(field_name, []))

    def _migrate(self, from_version: int) -> None:
        """Migrate configuration from older version

        Args:
            from_version: Current config version
        """
        logger.info(f"Migrating config from v{from_version} to v{CONFIG_VERSION}")

        # Backup current config
        self._backup_config()

        # Apply migrations sequentially
        for version in range(from_version + 1, CONFIG_VERSION + 1):
            migrate_func = getattr(self, f"_migrate_to_v{version}", None)
            if migrate_func:
                logger.debug(f"Applying migration to v{version}")
                migrate_func()

        # Update version
        if "app" not in self._config:
            self._config["app"] = {}
        self._config["app"]["config_version"] = CONFIG_VERSION

        self.save()
        logger.info("Config migration complete")

    def _migrate_to_v1(self) -> None:
        """Migration to v1 - initial version, no changes needed"""
        pass

    def _migrate_to_v2(self) -> None:
        """Migration to v2 — seed metadata_priority with all field defaults.

        Existing users have an empty metadata_priority dict, which caused
        MetadataResolver to silently fall back to hardcoded class constants.
        After this migration, all ~43 fields are explicitly in config.
        """
        from luducat.core.metadata_resolver import _SEED_FIELD_PRIORITIES

        existing = self._config.get("metadata_priority", {})
        for field, sources in _SEED_FIELD_PRIORITIES.items():
            if field not in existing:
                existing[field] = list(sources)  # copy
        self._config["metadata_priority"] = existing
        logger.info(
            f"Seeded metadata_priority with {len(_SEED_FIELD_PRIORITIES)} fields "
            f"({len(existing)} total after preserving user overrides)"
        )

    def _migrate_to_v3(self) -> None:
        """Migration to v3 — seed new canonical metadata fields.

        Field name normalization added ~40 new fields to _SEED_FIELD_PRIORITIES
        (short_description, artworks, icon_url, logo_url, price, type, is_free,
        rating_positive, rating_negative, total_rating, game_modes, etc.).
        Also removed release_date from priority (now a merged field).
        """
        from luducat.core.metadata_resolver import _SEED_FIELD_PRIORITIES

        existing = self._config.get("metadata_priority", {})
        added = 0
        for field, sources in _SEED_FIELD_PRIORITIES.items():
            if field not in existing:
                existing[field] = list(sources)
                added += 1
        # Remove release_date from priority if present (now a merged field)
        if "release_date" in existing:
            del existing["release_date"]
        self._config["metadata_priority"] = existing
        logger.info(
            f"v3 migration: seeded {added} new metadata fields "
            f"({len(existing)} total)"
        )

    def _migrate_to_v4(self) -> None:
        """Migration to v4 — seed content_ratings, editions, GOG age_ratings.

        Adds new GOG catalog API fields (content_ratings, editions) to
        metadata_priority, ensures existing age_ratings includes "gog",
        and removes pcgamingwiki from cover priorities (unreliable aspect ratios).
        """
        from luducat.core.metadata_resolver import _SEED_FIELD_PRIORITIES

        existing = self._config.get("metadata_priority", {})
        added = 0
        for field, sources in _SEED_FIELD_PRIORITIES.items():
            if field not in existing:
                existing[field] = list(sources)
                added += 1
        # Ensure age_ratings includes "gog" for existing users
        if "age_ratings" in existing and "gog" not in existing["age_ratings"]:
            existing["age_ratings"].append("gog")
        # Remove pcgamingwiki from cover — images have unreliable aspect ratios
        if "cover" in existing and "pcgamingwiki" in existing["cover"]:
            existing["cover"].remove("pcgamingwiki")
        self._config["metadata_priority"] = existing
        logger.info(
            f"v4 migration: seeded {added} new metadata fields, "
            f"ensured GOG in age_ratings ({len(existing)} total)"
        )

    def _migrate_to_v5(self) -> None:
        """Migration to v5 — remove pcgamingwiki from media priorities, flag cleanup.

        PCGamingWiki cover images are often horizontal banners that get
        center-cropped by the CDN vertical suffix, producing poor covers.
        The v4 migration removed it from 'cover' only; this migration also
        removes it from 'hero' and 'screenshots' and flags the main DB for
        a one-time cleanup of already-resolved pcgamingwiki media fields.
        """
        media_fields = ["cover", "hero", "screenshots"]
        existing = self._config.get("metadata_priority", {})
        removed_from = []
        for field in media_fields:
            if field in existing and "pcgamingwiki" in existing[field]:
                existing[field].remove("pcgamingwiki")
                removed_from.append(field)
        self._config["metadata_priority"] = existing
        # Flag for one-time DB cleanup at next startup
        self._config["_pending_media_cleanup"] = True
        logger.info(
            f"v5 migration: removed pcgamingwiki from {removed_from or 'no'} "
            f"media priorities, flagged DB for media cleanup"
        )

    def _migrate_to_v6(self) -> None:
        """Migration to v6 — flag one-time content_descriptors repair.

        Games synced before commit 3f1a35a have content_descriptors = NULL
        in the steamscraper DB. The repair fetches fresh API data for
        adult games (required_age >= 18) that are missing this field.
        """
        self._config["_pending_content_descriptors_repair"] = True
        logger.info("v6 migration: flagged content_descriptors repair")

    def _migrate_to_v7(self) -> None:
        """Migration to v7 — centralize tag sync settings into [tags] section.

        Moves:
        - appearance.tag_source_colors → tags.source_colors
        - Steam plugin tag_sync_mode → tags.plugin_overrides.steam.sync_mode
        - GOG plugin tag_sync_mode → tags.plugin_overrides.gog.sync_mode
        - Heroic plugin tag_sync_enabled → tags.plugin_overrides.heroic.enabled
        - Heroic plugin tag_sync_mode → tags.plugin_overrides.heroic.sync_mode
        - Heroic plugin import_favourites → tags.plugin_overrides.heroic.import_favourites
        """
        # Ensure tags section exists
        if "tags" not in self._config:
            self._config["tags"] = {}
        tags = self._config["tags"]

        # Move appearance.tag_source_colors → tags.source_colors
        appearance = self._config.get("appearance", {})
        if "tag_source_colors" in appearance:
            tags["source_colors"] = appearance.pop("tag_source_colors")
            self._config["appearance"] = appearance

        # Migrate plugin tag_sync settings → tags.plugin_overrides
        overrides = tags.get("plugin_overrides", {})
        plugins = self._config.get("plugins", {})

        # Steam: tag_sync_mode
        steam_settings = plugins.get("steam", {})
        if "tag_sync_mode" in steam_settings:
            mode = steam_settings.pop("tag_sync_mode")
            steam_override = overrides.get("steam", {})
            steam_override["sync_mode"] = mode
            steam_override.setdefault("enabled", mode != "none")
            overrides["steam"] = steam_override

        # GOG: tag_sync_mode
        gog_settings = plugins.get("gog", {})
        if "tag_sync_mode" in gog_settings:
            mode = gog_settings.pop("tag_sync_mode")
            gog_override = overrides.get("gog", {})
            gog_override["sync_mode"] = mode
            gog_override.setdefault("enabled", mode != "none")
            overrides["gog"] = gog_override

        # Heroic: tag_sync_enabled, tag_sync_mode, import_favourites
        heroic_settings = plugins.get("heroic", {})
        heroic_override = overrides.get("heroic", {})
        if "tag_sync_enabled" in heroic_settings:
            heroic_override["enabled"] = heroic_settings.pop("tag_sync_enabled")
        if "tag_sync_mode" in heroic_settings:
            heroic_override["sync_mode"] = heroic_settings.pop("tag_sync_mode")
        if "import_favourites" in heroic_settings:
            heroic_override["import_favourites"] = heroic_settings.pop("import_favourites")
        if heroic_override:
            overrides["heroic"] = heroic_override

        tags["plugin_overrides"] = overrides
        self._config["tags"] = tags

        migrated = []
        if "steam" in overrides:
            migrated.append("steam")
        if "gog" in overrides:
            migrated.append("gog")
        if "heroic" in overrides:
            migrated.append("heroic")
        logger.info(
            f"v7 migration: centralized tag sync settings "
            f"(migrated: {', '.join(migrated) or 'none'})"
        )

    def _merge_defaults(self) -> None:
        """Merge missing default keys into config

        This ensures new config keys are added when upgrading
        without overwriting existing user values.
        """
        def merge(target: Dict, source: Dict) -> None:
            for key, value in source.items():
                if key not in target:
                    target[key] = self._deep_copy(value)
                elif isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)

        merge(self._config, DEFAULT_CONFIG)

    def _backup_config(self) -> Path:
        """Create timestamped backup of config file

        Returns:
            Path to backup file
        """
        backup_dir = self.config_dir / "backups"
        backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"config.toml.{timestamp}.backup"

        if self.config_path.exists():
            shutil.copy(self.config_path, backup_path)
            logger.info(f"Config backed up to {backup_path}")

        # Keep only last 10 backups
        backups = sorted(backup_dir.glob("config.toml.*.backup"))
        for old_backup in backups[:-10]:
            old_backup.unlink()
            logger.debug(f"Removed old backup: {old_backup}")

        return backup_path

    @staticmethod
    def _deep_copy(obj: Any) -> Any:
        """Create deep copy of nested dict/list structure"""
        if isinstance(obj, dict):
            return {k: Config._deep_copy(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [Config._deep_copy(v) for v in obj]
        else:
            return obj

    @staticmethod
    def _strip_none(obj: Any) -> Any:
        """Recursively remove None values from dicts (TOML has no null)."""
        if isinstance(obj, dict):
            return {k: Config._strip_none(v) for k, v in obj.items() if v is not None}
        elif isinstance(obj, list):
            return [Config._strip_none(v) for v in obj]
        else:
            return obj
