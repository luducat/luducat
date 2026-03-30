# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# base.py

"""Base classes for luducat plugins

All game store plugins must inherit from StorePlugin (or AbstractGameStore)
and implement the required abstract methods. See the plugin development guide.

Plugin Type System:
    Plugins declare their types in plugin.json and inherit from typed base classes:
    - "store" -> StorePlugin: Game store integration (Steam, GOG, Epic)
    - "metadata" -> MetadataPlugin: Metadata providers (IGDB, PCGamingWiki)
    - "platform" -> PlatformPlugin: Platform providers (DOSBox, ScummVM, Wine)
    - "runner" -> RunnerPlugin: Runner plugins (Steam, Heroic, Playnite)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union


# =============================================================================
# CANONICAL METADATA FIELD VOCABULARY
# =============================================================================
# Plugins MUST use these exact keys when producing metadata dicts.
# The MetadataResolver only recognizes these names (plus backward-compat
# aliases for old stored metadata_json). Non-canonical names are logged
# as warnings during validation.

CANONICAL_METADATA_FIELDS: frozenset = frozenset({
    # General
    "title", "description", "short_description", "developers", "publishers",
    "genres", "release_date", "type", "is_free",
    # Media
    "cover", "hero", "header_url", "screenshots", "artworks", "videos",
    "icon_url", "logo_url",
    # Taxonomy (NO tags/keywords — store tags are marketing junk, native tag system used)
    "collections",
    "features", "game_modes", "game_modes_detail", "crossplay", "crossplay_platforms",
    # Ratings
    "rating", "user_rating", "rating_positive", "rating_negative",
    "total_rating",
    "critic_rating", "critic_rating_url", "age_ratings", "age_rating_esrb", "age_rating_pegi",
    "opencritic_score", "opencritic_id",
    "protondb_rating", "protondb_score", "steam_deck_compat",
    # Extended
    "franchise", "series", "themes", "perspectives", "platforms", "links",
    "storyline", "pacing", "art_styles",
    # Technical
    "engine", "engines", "controller_support", "full_controller_support",
    "controller_remapping", "controller_sensitivity", "controller_haptic_feedback",
    "key_remapping", "mouse_sensitivity", "mouse_acceleration", "mouse_input_in_menus",
    "touchscreen", "controls", "monetization", "microtransactions",
    # Statistics
    "achievements", "estimated_owners", "recommendations", "peak_ccu",
    "average_playtime", "average_playtime_forever", "average_playtime_2weeks",
    "playtime_minutes",
    # Languages
    "supported_languages", "full_audio_languages",
    # Commercial
    "price", "required_age", "category", "status",
    # Platform detail
    "windows", "mac", "linux",
    # GOG-specific description fields (used by GOG plugin to derive description/short_description)
    "description_lead", "description_cool",
    # Single-source URL fields (feed into merged `links`)
    "website", "official_url",
})

# Per-store dict fields — not priority-resolved, stored per-store alongside store data.
# Plugins provide these and they're exposed in the metadata, but MetadataResolver does
# NOT resolve them via priority — each store's value is kept separately.
PER_STORE_FIELDS: frozenset = frozenset({
    "slug",           # URL slug per store
    "dlcs",           # DLC list per store
    "is_available",   # Delisted flag per store (boolean)
    "changelog",      # Changelog per store
    "downloads_json", # Download info per store
})


# =============================================================================
# PLATFORM NORMALIZATION (for release_date dict keys)
# =============================================================================

# IGDB platform IDs → canonical platform name
IGDB_PLATFORM_NORMALIZATION: Dict[int, str] = {
    # PC
    6: "windows",       # PC (Microsoft Windows)
    13: "dos",          # DOS (includes MSDOS, Tandy, IBM PC/PCjr variants)
    3: "linux",         # Linux
    14: "macos",        # Mac (covers macOS, OS X, Mac OS)
    # Japanese PCs — NOT merged to "dos" per design
    149: "pc98",        # PC-9800 Series (NEC)
    # Consoles (common ones)
    48: "ps4",
    167: "ps5",
    9: "ps3",
    8: "ps2",
    7: "ps1",
    49: "xboxone",
    169: "xboxseriesx",
    12: "xbox360",
    11: "xbox",
    130: "switch",
    41: "wiiu",
    5: "wii",
    4: "n64",
    19: "snes",
    18: "nes",
    20: "ds",
    37: "3ds",
    33: "gameboy",
    24: "gba",
    # Portables
    46: "psvita",
    38: "psp",
    # Mobile
    34: "android",
    39: "ios",
    # Other
    170: "stadia",
}

# Text-based platform name normalization (for non-IGDB sources or IGDB slug fallback)
PLATFORM_NAME_NORMALIZATION: Dict[str, str] = {
    # Windows variants
    "pc (microsoft windows)": "windows", "windows": "windows",
    "windows nt": "windows", "windows 7": "windows", "windows 8": "windows",
    "windows 10": "windows", "windows 11": "windows", "windows vista": "windows",
    "windows xp": "windows",
    # Win9x variants → separate platform
    "windows 95": "win9x", "windows 98": "win9x", "windows me": "win9x",
    "win9x": "win9x", "win95": "win9x", "win98": "win9x",
    # DOS variants
    "dos": "dos", "msdos": "dos", "ms-dos": "dos",
    "tandy": "dos", "ibm pc": "dos", "ibm pcjr": "dos", "ibm-pc": "dos",
    # Linux variants
    "linux": "linux", "steamos": "linux", "ubuntu": "linux",
    # macOS variants
    "macos": "macos", "mac os": "macos", "os x": "macos", "osx": "macos",
    "mac os x": "macos", "macintosh": "macos",
    # Japanese PCs — NOT merged to dos
    "pc-9800 series": "pc98", "pc-98": "pc98", "pc98": "pc98",
    "sharp x68000": "x68000", "x68000": "x68000",
    "fm towns": "fmtowns", "fm-towns": "fmtowns",
    "msx": "msx", "msx2": "msx",
}


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def generate_short_description(description: str, max_paragraphs: int = 2) -> str:
    """Extract first N paragraphs from a full description for tooltip use.

    Args:
        description: Full description text (plain text or light HTML)
        max_paragraphs: Maximum number of paragraphs to extract

    Returns:
        Truncated description string, or empty string if input is empty
    """
    if not description:
        return ""
    paragraphs = [p.strip() for p in description.split("\n\n") if p.strip()]
    return "\n\n".join(paragraphs[:max_paragraphs])


def compute_release_year(release_dates: Dict[str, str]) -> Optional[int]:
    """Compute release year from oldest date across all platforms.

    Args:
        release_dates: Dict mapping platform name → "YYYY-MM-DD" string

    Returns:
        Year as integer, or None if no valid dates
    """
    if not release_dates or not isinstance(release_dates, dict):
        return None
    valid_dates = [d for d in release_dates.values() if d and len(d) >= 4]
    if not valid_dates:
        return None
    oldest = min(valid_dates)
    try:
        return int(oldest[:4])
    except (ValueError, IndexError):
        return None


class PluginType(Enum):
    """Plugin type classification

    Plugins can implement multiple types, each requiring specific methods.
    Use plugin_manager.get_plugins_by_type() to enumerate by type.
    """
    STORE = "store"                    # Game store (Steam, GOG, Epic)
    METADATA = "metadata"              # Metadata-only providers (IGDB, HLTB)
    PLATFORM = "platform"              # Platform providers (DOSBox, ScummVM, Wine)
    RUNNER = "runner"                  # Runner plugins (Heroic, Steam, Playnite)


@dataclass
class ConfigAction:
    """Declarative action for plugin config dialogs.

    Actions are rendered as buttons in the generic PluginConfigDialog.
    Plugins declare actions via plugin.json ``config_actions`` (static)
    or ``get_config_actions()`` (dynamic, runtime state).

    Attributes:
        id: Unique action identifier (e.g., "test_connection", "sync_data")
        label: Button text (wrapped with ``_()`` at render time)
        callback: Method name (str) or callable to invoke on click
        group: Layout group — "auth" (top row), "data" (second row),
               "general" (below settings)
        icon: Optional SVG filename from assets/icons/ for the button
        enabled: Whether the button is initially enabled
        tooltip: Optional tooltip text (wrapped with ``_()`` at render time)
        requires_auth: If True, button is disabled when plugin is not authenticated
        dialog_class: Optional module path to a dialog class to open on click
                      (relative to plugin directory, e.g., "ui.author_dialog.AuthorDialog")
    """
    id: str
    label: str
    callback: Union[str, Callable] = ""
    group: str = "general"
    icon: Optional[str] = None
    enabled: bool = True
    tooltip: Optional[str] = None
    requires_auth: bool = False
    dialog_class: Optional[str] = None


class PluginError(Exception):
    """Base exception for plugin errors"""
    pass


class AuthenticationError(PluginError):
    """Raised when authentication fails or is required"""
    pass


class RateLimitError(PluginError):
    """Raised when API rate limit is exceeded.

    Plugins should raise this on 429/403 responses or proactive cooldown
    instead of sleeping internally.
    The caller (game_service) handles retry, wait, and UI notification.
    """
    def __init__(self, message: str = "Rate limit exceeded", wait_seconds: int = 300, reason: str = "429"):
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.reason = reason  # "429", "403", "proactive"


class NetworkError(PluginError):
    """Raised when network request fails"""
    pass


@dataclass
class Game:
    """Standardized game data from any store

    This is the common format that all plugins must convert their
    platform-specific data into. The main application uses this
    format for display and storage.

    Attributes:
        store_app_id: Platform-specific unique identifier (as string)
        store_name: Name of the source store (e.g., "steam", "gog")
        title: Game title
        launch_url: URL scheme to launch the game (e.g., "steam://rungameid/440")

    Optional attributes have sensible defaults for games with incomplete metadata.
    """
    # Required fields
    store_app_id: str
    store_name: str
    title: str
    launch_url: str

    # Optional metadata
    short_description: Optional[str] = None
    description: Optional[str] = None
    header_image_url: Optional[str] = None
    cover_image_url: Optional[str] = None  # 2:3 portrait cover (library_capsule)
    background_image_url: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)

    # Release info
    release_date: Optional[str] = None
    publishers: List[str] = field(default_factory=list)
    developers: List[str] = field(default_factory=list)

    # Categorization
    genres: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)  # Store-provided tags (not user tags)

    # Play statistics (if available from store)
    playtime_minutes: Optional[int] = None
    last_played: Optional[datetime] = None

    # Achievement data (if available)
    achievements_total: Optional[int] = None
    achievements_unlocked: Optional[int] = None

    # Licensing status
    # 0 = Owned (or shared to others), 1 = Borrowed via Family Sharing
    family_shared: int = 0
    # For borrowed games (family_shared=1), the SteamID of the owner
    family_shared_owner: Optional[str] = None

    # Steam status flags
    # 0 = normal, 1 = marked private on user's Steam profile
    is_private_app: int = 0
    # 0 = still listed, 1 = not in public Steam store catalog
    is_delisted: int = 0

    # Additional metadata (store-specific, preserved as-is)
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    # Sibling store_games for cross-store enrichment propagation.
    # Set by sync_orchestrator when building deduped enrichment batches.
    # List of (store_name, store_app_id) tuples for other stores.
    siblings: Optional[List[tuple]] = field(default_factory=list)


@dataclass
class MetadataSearchResult:
    """Result from searching a metadata provider

    Returned by AbstractMetadataProvider.search_game() to allow
    the caller to select the best match.
    """
    provider_id: str         # Provider-specific ID (e.g., IGDB game ID)
    title: str               # Game title from provider
    release_year: Optional[int] = None
    platforms: List[str] = field(default_factory=list)
    cover_url: Optional[str] = None
    confidence: float = 0.0  # Match confidence 0.0-1.0


@dataclass
class EnrichmentData:
    """Enrichment data from a metadata provider

    Contains metadata that enhances game data from store plugins.
    All fields are optional - providers may not have all data.
    """
    provider_name: str       # Source provider (e.g., "igdb")
    provider_id: str         # Provider-specific ID

    # Categorization (primary enrichment targets)
    genres: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)      # Themes in IGDB
    franchise: Optional[str] = None
    series: Optional[str] = None

    # Additional metadata (may override store data if higher quality)
    developers: List[str] = field(default_factory=list)
    publishers: List[str] = field(default_factory=list)
    summary: Optional[str] = None
    storyline: Optional[str] = None
    release_date: Optional[str] = None

    # Images (fallback if store has none)
    cover_url: Optional[str] = None
    background_url: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)

    # Ratings
    user_rating: Optional[float] = None
    user_rating_count: Optional[int] = None

    # Extended metadata (from IGDB, PCGamingWiki, etc.)
    themes: List[str] = field(default_factory=list)
    platforms: List[str] = field(default_factory=list)
    perspectives: List[str] = field(default_factory=list)
    age_ratings: List[Dict[str, str]] = field(default_factory=list)
    engine: Optional[str] = None
    websites: List[Dict[str, str]] = field(default_factory=list)

    # Extra provider-specific data
    extra: Dict[str, Any] = field(default_factory=dict)

    # Per-field source map (field_name -> provider_name)
    # Populated when enrichment comes from merged multi-plugin results
    source_map: Dict[str, str] = field(default_factory=dict)


@dataclass
class PluginMetadata:
    """Plugin metadata loaded from plugin.json

    This dataclass represents the parsed plugin.json file that
    every plugin must include.
    """
    # Required fields (no defaults)
    name: str                    # Unique identifier (lowercase, alphanumeric + underscore)
    display_name: str            # Human-readable name
    version: str                 # Semantic version (MAJOR.MINOR.PATCH)
    author: str
    description: str
    min_luducat_version: str

    # Class paths - one required depending on plugin type
    store_class: Optional[str] = None      # For store plugins (e.g., "store.SteamStore")
    provider_class: Optional[str] = None   # For metadata plugins (e.g., "provider.IgdbProvider")
    entry_point: Optional[str] = None      # For runtime plugins (e.g., "provider.DOSBoxProvider")

    # Plugin directory path (set during discovery)
    plugin_dir: Optional[Path] = None

    # Optional fields (with defaults)
    max_luducat_version: Optional[str] = None

    # Optional fields
    author_email: Optional[str] = None
    homepage: Optional[str] = None
    icon: Optional[str] = None   # Relative path to icon file

    # Dependencies
    python_version: str = ">=3.10"
    packages: List[str] = field(default_factory=list)

    # Platform support
    platforms: Dict[str, bool] = field(default_factory=lambda: {
        "linux": True, "windows": True, "macos": True
    })

    # Plugin types (required for type-based enumeration)
    # Valid types: "store", "metadata", "runtime", "launch_provider"
    plugin_types: List[str] = field(default_factory=lambda: ["store"])

    # Capabilities
    capabilities: Dict[str, bool] = field(default_factory=lambda: {
        "fetch_library": True,
        "fetch_metadata": True,
        "launch_games": True,
        "track_playtime": False,
        "achievements": False,
        "cloud_saves": False,
    })

    # Settings schema for plugin configuration UI
    settings_schema: Dict[str, Any] = field(default_factory=dict)

    # Per-field metadata declarations from plugin.json "provides_fields" section.
    # Maps field_name -> {"priority": int, ...}. Lower priority = higher preference.
    # If empty, this plugin uses fallback defaults in MetadataResolver.
    provides_fields: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Auth declaration from plugin.json "auth" block.
    # type: "none", "api_key", "oauth", "browser_cookies", "external_tool"
    auth: Dict[str, Any] = field(default_factory=dict)

    # Brand colors for UI badges/labels (from plugin.json "brand_colors")
    brand_colors: Dict[str, str] = field(default_factory=dict)

    # Badge label for UI store badges (3-char abbreviation, from plugin.json)
    badge_label: str = ""

    # Network configuration (from plugin.json "network" section)
    network: Dict[str, Any] = field(default_factory=dict)

    # Privacy declaration (from plugin.json "privacy" section)
    privacy: Dict[str, Any] = field(default_factory=dict)

    # Whether this plugin is bundled with the application (set by PluginManager)
    is_bundled: bool = False

    # Custom config dialog class path (e.g., "config_dialog.EpicConfigDialog")
    config_dialog_class: Optional[str] = None

    # Declarative config dialog actions (from plugin.json "config_actions")
    config_actions: List[Dict[str, Any]] = field(default_factory=list)

    # Bridge plugin configuration (from plugin.json "bridge" section)
    bridge_config: Dict[str, Any] = field(default_factory=dict)

    # Custom settings group title and intro text (from plugin.json)
    settings_title: Optional[str] = None
    settings_description: Optional[str] = None

    # Hidden: plugin not shown in Settings plugin list (e.g., multi_store engines
    # that only exist to spawn virtual stores)
    hidden: bool = False

    # Multi-store: plugin spawns multiple virtual store instances
    multi_store: bool = False

    # Credential storage settings
    use_system_keyring: bool = True
    keyring_service: Optional[str] = None  # Defaults to "luducat.{name}"


class AbstractGameStore(ABC):
    """Base class for all game store plugins

    Plugin developers must inherit from this class and implement all
    abstract methods. The plugin manager will instantiate this class
    and call its methods to sync game libraries.

    Lifecycle:
        1. Plugin is discovered via plugin.json
        2. Plugin class is instantiated with config
        3. authenticate() is called if needed
        4. fetch_user_games() retrieves owned game IDs
        5. fetch_game_metadata() retrieves game details
        6. Plugin is kept alive for launch_game() calls
        7. on_disable() called when plugin is disabled

    Thread Safety:
        All async methods may be called from background threads.
        Plugins should be thread-safe or document their limitations.

    Example:
        class MyStore(AbstractGameStore):
            @property
            def store_name(self) -> str:
                return "my_store"

            async def fetch_user_games(self) -> List[str]:
                return ["123", "456", "789"]

            # ... implement other abstract methods
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize plugin

        Args:
            config_dir: Plugin's config directory
                        (~/.config/luducat/plugins/{name}/)
            cache_dir: Plugin's cache directory
                       (~/.cache/luducat/plugins/{name}/)
            data_dir: Plugin's data directory
                      (~/.local/share/luducat/plugins-data/{name}/)
        """
        self.config_dir = config_dir
        self.cache_dir = cache_dir
        self.data_dir = data_dir

        # Ensure directories exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Will be set by plugin manager after initialization
        self._credential_manager = None
        self._settings: Dict[str, Any] = {}
        self._local_data_consent = False
        self._storage = None  # PluginStorage, injected by plugin manager
        self._http_client = None  # PluginHttpClient, injected by plugin manager
        self._main_db_accessor = None  # MainDbAccessor, injected by plugin manager

    def set_main_db_accessor(self, accessor) -> None:
        """Called by plugin manager to inject MainDbAccessor.

        Provides controlled, dict-based access to the main database
        without requiring direct ORM model imports.
        """
        self._main_db_accessor = accessor

    @property
    def main_db(self):
        """Main database accessor (MainDbAccessor).

        Returns None if not yet injected.
        """
        return self._main_db_accessor

    def set_credential_manager(self, credential_manager) -> None:
        """Called by plugin manager to inject credential manager

        Plugins should NOT create their own credential storage.
        Always use self.get_credential() and self.set_credential().
        """
        self._credential_manager = credential_manager

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Called by plugin manager to inject plugin settings"""
        self._settings = settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a plugin setting value"""
        return self._settings.get(key, default)

    def set_local_data_consent(self, consent: bool) -> None:
        """Called by plugin manager to inject privacy consent flag"""
        self._local_data_consent = consent

    def has_local_data_consent(self) -> bool:
        """Check if user granted consent for reading local data.

        Gates access to browser cookies, VDF files, Galaxy DB,
        Heroic config, and other local launcher data.
        """
        return self._local_data_consent

    def set_storage(self, storage) -> None:
        """Called by plugin manager to inject PluginStorage instance.

        Provides path-confined filesystem access.  Plugins can use
        ``self.storage`` to read/write files safely within their
        designated directories.
        """
        self._storage = storage

    @property
    def storage(self):
        """Path-confined filesystem access (PluginStorage).

        Returns None if not yet injected.
        """
        return self._storage

    def set_http_client(self, client) -> None:
        """Called by plugin manager to inject PluginHttpClient.

        Provides rate-limited, domain-checked HTTP access.
        Plugins should use ``self.http.get()`` / ``self.http.post()``
        instead of creating their own ``requests.Session()``.
        """
        self._http_client = client

    @property
    def http(self):
        """HTTP client (PluginHttpClient) for network requests.

        Returns None if not yet injected.
        """
        return self._http_client

    # === REQUIRED ABSTRACT METHODS ===

    @property
    @abstractmethod
    def store_name(self) -> str:
        """Return unique store identifier

        Must be lowercase, alphanumeric + underscores only.
        This is used as database prefix and config key.

        Examples: "steam", "gog", "epic", "itch_io"
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return human-readable store name for UI display

        Examples: "Steam", "GOG Galaxy", "Epic Games Store"
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if store client/service is accessible

        Returns:
            True if the store can be used (client installed,
            service reachable, etc.)

        Example: Check if Steam client is installed on Linux:
            return Path("~/.steam/steam.sh").expanduser().exists()
        """
        pass

    @abstractmethod
    async def authenticate(self) -> bool:
        """Perform authentication flow

        This may involve OAuth, OpenID, API key validation, or other
        authentication mechanisms. Use self.set_credential() to store
        tokens/keys securely.

        Returns:
            True if authentication successful

        Raises:
            AuthenticationError: If authentication fails
        """
        pass

    @abstractmethod
    def is_authenticated(self) -> bool:
        """Check if user is currently authenticated

        Returns:
            True if valid credentials exist and are not expired
        """
        pass

    @abstractmethod
    async def fetch_user_games(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """Fetch list of game IDs owned by user

        This should be a relatively fast operation that just returns
        identifiers. Detailed metadata is fetched separately via
        fetch_game_metadata().

        Args:
            status_callback: Optional callback(message) for progress updates
                           during long-running fetches (e.g., paginated APIs)
            cancel_check: Optional callback returning True if cancelled

        Returns:
            List of store-specific app IDs (as strings)

        Raises:
            AuthenticationError: If not authenticated
            NetworkError: If network request fails
        """
        pass

    @abstractmethod
    async def fetch_game_metadata(
        self, app_ids: List[str], download_images: bool = False
    ) -> List[Game]:
        """Fetch detailed metadata for given app IDs

        Args:
            app_ids: List of store-specific app IDs
            download_images: If True, download images to cache. Default False
                for fast sync - images are loaded lazily on demand.

        Returns:
            List of Game objects with metadata. Games that fail to
            fetch should be omitted (with logging), not raise exceptions.

        Note:
            - Implement rate limiting internally
            - Use caching to avoid redundant API calls
            - Return partial results if some games fail
            - Image URLs should be stored even when download_images=False
        """
        pass

    async def prepare_metadata(
        self,
        status_callback=None,
        cancel_check=None,
    ) -> None:
        """Hook for bulk preparation before per-game metadata fetch.

        Called after skeleton games are created (games visible in UI).
        Use for bulk API scans that populate the plugin DB before
        per-game fetch_game_metadata() reads from it.
        """
        pass

    def launch_game(self, app_id: str) -> bool:
        """Launch game via platform launcher.

        .. deprecated::
            Store plugins should no longer own launch logic. Game launching
            is now handled by bridge plugins (plugins/bridges/). This method
            is kept for backward compatibility during the transition.

        Args:
            app_id: Store-specific app ID

        Returns:
            True if launch command was successful
        """
        import warnings
        warnings.warn(
            f"{type(self).__name__}.launch_game() is deprecated. "
            "Use bridge plugins for game launching.",
            DeprecationWarning,
            stacklevel=2,
        )
        return False

    @abstractmethod
    def get_database_path(self) -> Path:
        """Return path to plugin's catalog database

        Each plugin maintains its own SQLite database for caching
        game metadata. This keeps plugin data isolated.

        Returns:
            Path to plugin's database file

        Convention:
            self.data_dir / "catalog.db"
        """
        pass

    # === OPTIONAL METHODS (override if supported) ===

    def get_config_actions(self) -> List[ConfigAction]:
        """Return dynamic config dialog actions.

        Override for actions that need runtime state (conditional enables,
        progress feedback, multi-step flows). Called by generic dialog
        during construction.

        Actions returned here are merged with static ``config_actions``
        from plugin.json. Same-id actions from this method replace
        JSON-declared ones.

        Returns:
            List of ConfigAction descriptors
        """
        return []

    def get_account_identifier(self) -> Optional[str]:
        """Return a string uniquely identifying the current account.

        Core uses this to detect account changes between syncs and
        reconcile ownership data (remove stale StoreGame entries).
        Returns None if account identity cannot be determined.

        Override in subclasses to return a stable account identifier
        (e.g., Steam64 ID, Epic account name, GOG auth cookie).
        """
        return None

    async def fetch_playtime(self, app_ids: List[str]) -> Dict[str, int]:
        """Fetch playtime data for games (optional)

        Returns:
            Dict mapping app_id -> playtime in minutes
        """
        return {}

    async def fetch_achievements(self, app_id: str) -> Dict[str, Any]:
        """Fetch achievement data for a game (optional)

        Returns:
            Dict with achievement data (format is plugin-specific)
        """
        return {}

    def get_store_page_url(self, app_id: str) -> str:
        """Get URL to game's store page

        Returns:
            URL string, or empty string if not applicable
        """
        return ""

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a single game from plugin's database

        This is the canonical source for game metadata. The main database
        should NOT store a copy - query the plugin directly.

        Args:
            app_id: Store-specific app ID

        Returns:
            Dict with metadata or None if not found:
            {
                "short_description": str,
                "description": str,
                "header_image_url": str,
                "cover_image_url": str,
                "screenshots": List[str],
                "release_date": str,
                "developers": List[str],
                "publishers": List[str],
                "genres": List[str],
            }
        """
        return None

    def get_games_metadata_bulk(self, app_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games efficiently

        For performance with large libraries (15k+ games), plugins should
        implement this to batch database queries.

        Args:
            app_ids: List of store-specific app IDs

        Returns:
            Dict mapping app_id -> metadata dict (same format as get_game_metadata)
        """
        # Default implementation: call get_game_metadata for each
        result = {}
        for app_id in app_ids:
            metadata = self.get_game_metadata(app_id)
            if metadata:
                result[app_id] = metadata
        return result

    def get_ids_needing_refetch(self) -> List[str]:
        """Return IDs of existing games that need metadata re-fetched.

        Called after fetch_user_games() during sync. Allows plugins to signal
        that previously-fetched games have stale metadata (e.g., detail ruleset
        changed). The sync pipeline will call fetch_game_metadata() for these
        but will NOT create skeleton games (they already exist in main DB).
        """
        return []

    def get_game_description(self, app_id: str) -> str:
        """Get description for a single game (lazy loading).

        Called when UI needs to display a game's description.
        Plugins should fetch from their local database, not API.

        Args:
            app_id: Store-specific app ID

        Returns:
            HTML description string, or empty string if not found
        """
        # Default: try to get from full metadata
        metadata = self.get_game_metadata(app_id)
        if metadata:
            return metadata.get("description", "")
        return ""

    async def download_game_images(self, app_id: str) -> bool:
        """Download images for a single game (lazy loading)

        Called when UI needs to display a game's images but they're
        not cached locally. Plugins should download header, cover,
        background, and screenshot images.

        Args:
            app_id: Store-specific app ID

        Returns:
            True if images were downloaded successfully
        """
        return False

    def get_cached_image_path(self, app_id: str, image_type: str) -> Optional[Path]:
        """Get path to cached image if it exists

        Args:
            app_id: Store-specific app ID
            image_type: Type of image (e.g., 'header', 'cover', 'background')

        Returns:
            Path to cached image file, or None if not cached
        """
        return None

    # === UNIFORM METADATA INTERFACE ===

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Resolve a game in this plugin's catalog and return standardized metadata.

        The plugin DB is a license-agnostic cache — contains ALL games ever
        fetched, not just user-owned ones.  Resolution strategy:
        1. If store_name matches our store → direct ID lookup (fast path)
        2. Otherwise → title-based search in our catalog

        Subclasses should override for API-on-cache-miss or richer matching.

        Args:
            store_name: Store identifier ("steam", "gog", "epic")
            store_id: Store's app ID
            normalized_title: Optional normalized title for fallback search

        Returns:
            Dict with standardized metadata, or None if game not found
        """
        if store_name == self.store_name:
            return self.get_game_metadata(store_id)
        if normalized_title:
            return self._find_game_by_title(normalized_title)
        return None

    def _find_game_by_title(self, normalized_title: str) -> Optional[Dict[str, Any]]:
        """Search own catalog by normalized title. Override in subclasses.

        Args:
            normalized_title: Normalized game title

        Returns:
            Standardized metadata dict, or None if not found
        """
        return None

    # === LIFECYCLE HOOKS ===

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings

        Use this to initialize resources, create database tables, etc.
        """
        pass

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings

        Use this to clean up resources, close connections, etc.
        """
        pass

    def close(self) -> None:
        """Called when application is shutting down

        Clean up any resources: close database connections,
        HTTP sessions, etc.
        """
        pass

    def get_install_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return installation status data for syncing to main DB.

        Store plugins override this to report which games are installed locally.
        Called during sync, right after tag sync.

        Returns:
            Dict mapping store_app_id -> {"installed": bool, "install_path": str|None}
            for installed games (absence = not installed), or None if not supported.
        """
        return None

    def get_playtime_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return playtime data for syncing to main DB.

        Store plugins override this to report playtime from local or API sources.
        Called during sync, after install sync.

        Returns:
            Dict mapping store_app_id -> {"minutes": int, "last_played": str|None}
            where last_played is ISO datetime string or None, or None if not supported.
        """
        return None

    def on_sync_complete(self, progress_callback=None) -> Dict[str, Any]:
        """Called after sync completes for this store

        Use this for post-sync tasks like repairing assets, cleanup, etc.
        All plugin-specific logic should be in the plugin, not game_service.

        Args:
            progress_callback: Optional callback(message, current, total)

        Returns:
            Dict with any stats to report (e.g., {"assets_repaired": 5})
        """
        return {}

    async def get_download_info(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get download information for a game

        Override this method to provide download URLs, checksums, etc.
        for game installers, patches, and extras.

        Args:
            app_id: Store-specific app ID

        Returns:
            Dict with download information:
            {
                "installers": [...],  # List of installer info
                "patches": [...],     # List of patch info
                "extras": [...],      # List of extra info (soundtracks, etc.)
            }
            Or None if downloads not supported.
        """
        return None

    # === CREDENTIAL HELPERS ===

    def get_credential(self, key: str) -> Optional[str]:
        """Retrieve credential from secure storage

        Args:
            key: Credential key (e.g., "api_key", "auth_token")

        Returns:
            Credential value or None if not found

        Note:
            Uses system keyring (libsecret on Linux, Keychain on macOS,
            Credential Manager on Windows).
        """
        if self._credential_manager is None:
            return None
        return self._credential_manager.get(self.store_name, key)

    def set_credential(self, key: str, value: str) -> None:
        """Store credential in secure storage

        Args:
            key: Credential key
            value: Credential value
        """
        if self._credential_manager is None:
            raise PluginError("Credential manager not initialized")
        self._credential_manager.store(self.store_name, key, value)

    def delete_credential(self, key: str) -> None:
        """Delete credential from secure storage

        Args:
            key: Credential key to delete
        """
        if self._credential_manager is None:
            return
        self._credential_manager.delete(self.store_name, key)


# Alias for new naming convention
# Use StorePlugin in new code; AbstractGameStore kept for backwards compatibility
StorePlugin = AbstractGameStore


class AbstractMetadataProvider(ABC):
    """Base class for metadata-only plugins (IGDB, HLTB, etc.)

    Metadata providers enrich game data from store plugins with additional
    information like genres, tags, franchises, ratings, etc. They do NOT
    provide game ownership or launch capabilities.

    Key differences from StorePlugin:
    - No fetch_user_games() - doesn't track ownership
    - No launch_game() - can't launch games
    - Provides enrich_game() and search_game() instead

    Lifecycle:
        1. Plugin is discovered via plugin.json (plugin_types: ["metadata"])
        2. Plugin class is instantiated with config dirs
        3. authenticate() is called if needed (e.g., API keys)
        4. During store sync, enrich_games() is called with store games
        5. Plugin caches enrichment data in its local database
        6. on_disable() called when plugin is disabled

    Thread Safety:
        All async methods may be called from background threads.
        Plugins should be thread-safe or document their limitations.

    Example:
        class IgdbProvider(AbstractMetadataProvider):
            @property
            def provider_name(self) -> str:
                return "igdb"

            async def lookup_by_store_id(self, store_name, store_id) -> Optional[str]:
                # Use external_games API to find IGDB game
                return igdb_id

            async def get_enrichment(self, provider_id) -> Optional[EnrichmentData]:
                # Fetch genres, tags, franchise from IGDB
                return enrichment_data
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize plugin

        Args:
            config_dir: Plugin's config directory
                        (~/.config/luducat/plugins/{name}/)
            cache_dir: Plugin's cache directory
                       (~/.cache/luducat/plugins/{name}/)
            data_dir: Plugin's data directory
                      (~/.local/share/luducat/plugins-data/{name}/)
        """
        self.config_dir = config_dir
        self.cache_dir = cache_dir
        self.data_dir = data_dir

        # Ensure directories exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Will be set by plugin manager after initialization
        self._credential_manager = None
        self._settings: Dict[str, Any] = {}
        self._local_data_consent = False
        self._storage = None  # PluginStorage, injected by plugin manager
        self._http_client = None  # PluginHttpClient, injected by plugin manager

    def set_credential_manager(self, credential_manager) -> None:
        """Called by plugin manager to inject credential manager"""
        self._credential_manager = credential_manager

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Called by plugin manager to inject plugin settings"""
        self._settings = settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a plugin setting value"""
        return self._settings.get(key, default)

    def set_local_data_consent(self, consent: bool) -> None:
        """Called by plugin manager to inject privacy consent flag"""
        self._local_data_consent = consent

    def has_local_data_consent(self) -> bool:
        """Check if user granted consent for reading local data.

        Gates access to local launcher config files and databases.
        """
        return self._local_data_consent

    def set_storage(self, storage) -> None:
        """Called by plugin manager to inject PluginStorage instance."""
        self._storage = storage

    @property
    def storage(self):
        """Path-confined filesystem access (PluginStorage)."""
        return self._storage

    def set_http_client(self, client) -> None:
        """Called by plugin manager to inject PluginHttpClient."""
        self._http_client = client

    @property
    def http(self):
        """HTTP client (PluginHttpClient) for network requests."""
        return self._http_client

    # === OPTIONAL METHODS ===

    def get_config_actions(self) -> List[ConfigAction]:
        """Return dynamic config dialog actions.

        Override for actions that need runtime state. See
        AbstractGameStore.get_config_actions() for details.
        """
        return []

    def get_asset_attribution(self, asset_url: str) -> Optional[Dict[str, Any]]:
        """Get attribution info for an asset this plugin provided.

        Plugins that track asset authorship (e.g. community image sources)
        can override this to return author information for a given URL.

        Args:
            asset_url: The URL of the asset (cover, hero, etc.)

        Returns:
            Dict with at minimum {"author": str}, or None if unknown.
            May also include "author_id", "score", etc.
        """
        return None

    def adjust_author_score(self, author_name: str, delta: int) -> bool:
        """Adjust the score for an asset author.

        Plugins that support per-author quality scoring can override this
        to update an author's score in plugin settings.

        Args:
            author_name: The author's display name.
            delta: Score change (+1 for boost, -1 for block, etc.)

        Returns:
            True if the score was updated, False if not supported.
        """
        return False

    # === REQUIRED ABSTRACT METHODS ===

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return unique provider identifier

        Must be lowercase, alphanumeric + underscores only.
        This is used as database prefix and config key.

        Examples: "igdb", "hltb", "cooptimus"
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return human-readable provider name for UI display

        Examples: "IGDB", "HowLongToBeat", "Co-Optimus"
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider service is accessible

        Returns:
            True if the provider can be used (API reachable,
            credentials configured, etc.)
        """
        pass

    @abstractmethod
    async def authenticate(self) -> bool:
        """Perform authentication flow

        For IGDB: Twitch OAuth with client_id/client_secret
        For others: API key validation

        Returns:
            True if authentication successful

        Raises:
            AuthenticationError: If authentication fails
        """
        pass

    @abstractmethod
    def is_authenticated(self) -> bool:
        """Check if provider credentials are valid

        Returns:
            True if valid credentials exist and are not expired
        """
        pass

    @abstractmethod
    async def lookup_by_store_id(
        self,
        store_name: str,
        store_id: str
    ) -> Optional[str]:
        """Look up provider's game ID using store ID

        This is the primary matching method. Uses external_games
        database (IGDB) or similar to find games by their store IDs.

        Args:
            store_name: Store identifier ("steam", "gog", "epic")
            store_id: Store's app ID (Steam appid, GOG gogid, Epic app_name)

        Returns:
            Provider's game ID if found, None otherwise
        """
        pass

    @abstractmethod
    async def search_game(
        self,
        title: str,
        year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Search for games by title (fallback when store ID lookup fails)

        Args:
            title: Game title to search for
            year: Optional release year to narrow results

        Returns:
            List of search results sorted by relevance
        """
        pass

    @abstractmethod
    async def get_enrichment(
        self,
        provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a game

        Args:
            provider_id: This provider's game ID

        Returns:
            EnrichmentData with genres, tags, franchise, etc.
            None if game not found
        """
        pass

    @abstractmethod
    def get_database_path(self) -> Path:
        """Return path to plugin's enrichment database

        Each metadata plugin maintains its own SQLite database for caching
        game enrichment data. This reduces API calls on subsequent syncs.

        Returns:
            Path to plugin's database file

        Convention:
            self.data_dir / "enrichment.db"
        """
        pass

    # === CACHE MANAGEMENT ===

    @property
    def store_match_table(self) -> Optional[str]:
        """SQLAlchemy table name for store-to-plugin ID mapping.

        Override in subclasses to enable cache clearing during force-rescan.
        Return None if plugin doesn't use a match table.
        """
        return None

    # === BATCH OPERATIONS (for efficiency during sync) ===

    async def enrich_games(
        self,
        games: List[Game],
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        cross_store_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Enrich multiple games during sync

        This is the main entry point called during store sync.
        Default implementation calls lookup_by_store_id + get_enrichment
        for each game. Override for batch API optimizations.

        Args:
            games: List of Game objects from store plugins
            status_callback: Optional callback(message, current, total)
            cancel_check: Optional callback that returns True if cancelled
            cross_store_ids: Optional mapping of store_app_id -> steam_app_id
                for non-Steam games resolved by MetadataResolver

        Returns:
            Dict mapping store_app_id -> EnrichmentData for enriched games
        """
        results = {}
        total = len(games)

        for i, game in enumerate(games):
            if cancel_check and cancel_check():
                break

            if status_callback:
                status_callback(f"Enriching: {game.title}", i + 1, total)

            try:
                # Try store ID lookup first
                provider_id = await self.lookup_by_store_id(
                    game.store_name, game.store_app_id
                )

                if not provider_id:
                    # Fallback to title search
                    search_results = await self.search_game(game.title)
                    if search_results and search_results[0].confidence >= 0.8:
                        provider_id = search_results[0].provider_id

                if provider_id:
                    enrichment = await self.get_enrichment(provider_id)
                    if enrichment:
                        results[game.store_app_id] = enrichment

            except Exception as e:
                # Log but don't fail entire batch
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to enrich {game.title}: {e}"
                )

        return results

    def get_cached_enrichment(self, store_name: str, store_id: str) -> Optional[EnrichmentData]:
        """Get cached enrichment data from local database

        Use this to avoid API calls for already-enriched games.

        Args:
            store_name: Store identifier
            store_id: Store's app ID

        Returns:
            Cached EnrichmentData or None if not cached
        """
        return None

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Get metadata for a game on-demand using store info (sync)

        This is the generic interface for on-demand metadata fetching.
        Called by MetadataResolver when store plugins don't have the data.

        The plugin handles:
        1. Looking up its internal ID from the store info
        2. Fetching and returning metadata in standard format

        Args:
            store_name: Store identifier ("steam", "gog", "epic")
            store_id: Store's app ID
            normalized_title: Optional normalized title for fallback search

        Returns:
            Dict with metadata (description, cover_image_url, genres, etc.)
            or None if game not found
        """
        # Default implementation returns None
        # Subclasses should override to provide actual implementation
        return None

    # === LIFECYCLE HOOKS ===

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings"""
        pass

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings"""
        pass

    def close(self) -> None:
        """Called when application is shutting down"""
        pass

    # === CREDENTIAL HELPERS ===

    def get_credential(self, key: str) -> Optional[str]:
        """Retrieve credential from secure storage"""
        if self._credential_manager is None:
            return None
        return self._credential_manager.get(self.provider_name, key)

    def set_credential(self, key: str, value: str) -> None:
        """Store credential in secure storage"""
        if self._credential_manager is None:
            raise PluginError("Credential manager not initialized")
        self._credential_manager.store(self.provider_name, key, value)

    def delete_credential(self, key: str) -> None:
        """Delete credential from secure storage"""
        if self._credential_manager is None:
            return
        self._credential_manager.delete(self.provider_name, key)


# Alias for consistency
MetadataPlugin = AbstractMetadataProvider


class AbstractPlatformProvider(ABC):
    """Base class for platform provider plugins (DOSBox, ScummVM, Wine, etc.)

    Platform providers handle game execution through various methods:
    - Emulators (DOSBox, ScummVM)
    - Compatibility layers (Wine, Proton)

    Each platform provider is a plugin that can detect available platforms,
    check game compatibility, and generate launch configurations.

    Lifecycle:
        1. Plugin is discovered via plugin.json (plugin_types: ["platform"])
        2. Plugin class is instantiated with config dirs
        3. detect_platforms() is called to find available platforms
        4. can_run_game() checks if this provider handles a game
        5. create_launch_config() generates launch parameters
        6. launch() executes the game

    Example:
        class DOSBoxProvider(AbstractPlatformProvider):
            @property
            def provider_name(self) -> str:
                return "dosbox"

            def detect_platforms(self):
                # Find DOSBox installations
                ...

            def can_run_game(self, game):
                return "dosbox" in game.tags
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize platform provider plugin

        Args:
            config_dir: Plugin's config directory
            cache_dir: Plugin's cache directory
            data_dir: Plugin's data directory
        """
        self.config_dir = config_dir
        self.cache_dir = cache_dir
        self.data_dir = data_dir

        # Ensure directories exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Will be set by plugin manager after initialization
        self._settings: Dict[str, Any] = {}
        self._storage = None  # PluginStorage, injected by plugin manager
        self._http_client = None  # PluginHttpClient, injected by plugin manager

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Called by plugin manager to inject plugin settings"""
        self._settings = settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get plugin setting value"""
        return self._settings.get(key, default)

    def set_storage(self, storage) -> None:
        """Called by plugin manager to inject PluginStorage instance."""
        self._storage = storage

    @property
    def storage(self):
        """Path-confined filesystem access (PluginStorage)."""
        return self._storage

    def set_http_client(self, client) -> None:
        """Called by plugin manager to inject PluginHttpClient."""
        self._http_client = client

    @property
    def http(self):
        """HTTP client (PluginHttpClient) for network requests."""
        return self._http_client

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique identifier for this platform provider

        Returns:
            Provider name (e.g., "dosbox", "scummvm", "wine")
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for this provider

        Returns:
            Display name (e.g., "DOSBox-Staging", "ScummVM")
        """
        pass

    @property
    @abstractmethod
    def platform_type(self) -> str:
        """Type of platform this provider handles

        Returns:
            Platform type string ("dosbox", "scummvm", "wine", "native")
        """
        pass

    @abstractmethod
    def detect_platforms(self) -> List[Dict[str, Any]]:
        """Detect available platforms of this type

        Scans the system for installed platforms and returns
        information about each one found.

        Returns:
            List of platform info dicts:
            [
                {
                    "platform_id": "dosbox/0.81.0",
                    "name": "DOSBox-Staging 0.81.0",
                    "version": "0.81.0",
                    "executable_path": "/usr/bin/dosbox",
                    "is_default": True,
                    "is_managed": False,  # True if managed by luducat
                },
                ...
            ]
        """
        pass

    @abstractmethod
    def can_run_game(self, game: "Game") -> bool:
        """Check if this provider can run the given game

        Args:
            game: Game to check (from plugins.base.Game or database model)

        Returns:
            True if this provider can handle the game
        """
        pass

    @abstractmethod
    def create_launch_config(
        self,
        game: "Game",
        platform_info: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """Create launch configuration for a game

        Args:
            game: Game to launch
            platform_info: Platform info dict from detect_platforms()
            **kwargs: Additional launch options

        Returns:
            Launch config dict:
            {
                "launch_method": "url_scheme" | "executable" | "command",
                "launch_url": "steam://...",  # For url_scheme
                "executable": "/path/to/exe",  # For executable/command
                "arguments": ["--arg1", "--arg2"],
                "working_directory": "/path/to/game",
                "environment": {"VAR": "value"},
            }
        """
        pass

    def launch(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Launch a game with the given configuration

        Default implementation handles common launch methods.
        Override for custom launch behavior.

        Args:
            config: Launch configuration from create_launch_config()

        Returns:
            Result dict: {"success": bool, "error": str | None, "pid": int | None}
        """
        launch_method = config.get("launch_method", "executable")

        try:
            if launch_method == "url_scheme":
                return self._launch_url_scheme(config)
            elif launch_method in ("executable", "command"):
                return self._launch_executable(config)
            else:
                return {"success": False, "error": f"Unknown launch method: {launch_method}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _launch_url_scheme(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Launch via URL scheme"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = config.get("launch_url")
        if not url:
            return {"success": False, "error": "No launch URL provided"}

        success = QDesktopServices.openUrl(QUrl(url))
        return {"success": success, "error": None if success else "Failed to open URL"}

    def _launch_executable(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Launch via direct executable"""
        import subprocess
        import sys

        executable = config.get("executable")
        if not executable:
            return {"success": False, "error": "No executable provided"}

        exe_path = Path(executable)
        if not exe_path.exists():
            return {"success": False, "error": f"Executable not found: {executable}"}

        args = [str(exe_path)] + config.get("arguments", [])
        cwd = config.get("working_directory")
        env = None

        if config.get("environment"):
            import os
            env = os.environ.copy()
            env.update(config["environment"])

        try:
            if sys.platform == "win32":
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
            else:
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            return {"success": True, "error": None, "pid": process.pid}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_platform_settings_schema(self) -> Dict[str, Any]:
        """Get JSON schema for platform-specific settings

        Override to provide custom settings UI.

        Returns:
            JSON schema dict or empty dict
        """
        return {}

    def get_game_settings_schema(self, game: "Game") -> Dict[str, Any]:
        """Get JSON schema for per-game platform settings

        Override to provide game-specific settings.

        Args:
            game: Game to get settings schema for

        Returns:
            JSON schema dict or empty dict
        """
        return {}

    def get_platform_provider(self):
        """Get the PlatformProviderBase instance for RuntimeManager

        This allows the plugin to return its core PlatformProviderBase
        implementation for use by RuntimeManager.

        Returns:
            PlatformProviderBase subclass instance, or self if compatible
        """
        return self

    # === LIFECYCLE HOOKS ===

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings"""
        pass

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings"""
        pass

    def close(self) -> None:
        """Called when application is shutting down"""
        pass


# Alias
PlatformPlugin = AbstractPlatformProvider


class AbstractRunnerPlugin(ABC):
    """Base class for runner plugins.

    Runner plugins delegate game launching to external launcher applications:
    - Store launchers (Steam Client, GOG Galaxy, Epic Games Launcher)
    - Third-party launchers (Heroic, Lutris, Minigalaxy, Bottles)
    - Platform runners (Playnite via IPC)

    Stores own *what you own* (library, purchases, licenses).
    Runners own *how to launch it* (launcher detection, URL schemes, IPC).
    Platforms own *how to run it directly* (engine configuration, compatibility layers).

    Lifecycle:
        1. Plugin is discovered via plugin.json (plugin_types: ["runner"])
        2. Plugin class is instantiated with config dirs
        3. detect_launcher() probes system and returns RunnerLauncherInfo or None
        4. build_launch_intent() produces a structured LaunchIntent
        5. execute_launch() carries out the intent (default handles URL/exec)
        6. RuntimeManager orchestrates steps 3-5

    Example:
        class HeroicRunner(AbstractRunnerPlugin):
            @property
            def runner_name(self) -> str:
                return "heroic"

            @property
            def display_name(self) -> str:
                return "Heroic Runner"

            @property
            def supported_stores(self) -> List[str]:
                return ["gog", "epic"]

            def detect_launcher(self):
                from luducat.plugins.sdk.app_finder import find_application
                results = find_application(
                    ["heroic"],
                    flatpak_ids=["com.heroicgameslauncher.hgl"],
                )
                if results:
                    r = results[0]
                    return RunnerLauncherInfo(
                        runner_name="heroic", path=r.path,
                        install_type=r.install_type, virtualized=r.virtualized,
                        url_scheme="heroic://", flatpak_id=r.flatpak_id,
                    )
                return None
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize runner plugin.

        Args:
            config_dir: Plugin's config directory
            cache_dir: Plugin's cache directory
            data_dir: Plugin's data directory
        """
        self.config_dir = config_dir
        self.cache_dir = cache_dir
        self.data_dir = data_dir

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._settings: Dict[str, Any] = {}
        self._storage = None  # PluginStorage, injected by plugin manager
        self._http_client = None  # PluginHttpClient, injected by plugin manager
        self._local_data_consent: bool = False

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Called by plugin manager to inject plugin settings."""
        self._settings = settings

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get plugin setting value."""
        return self._settings.get(key, default)

    def set_storage(self, storage) -> None:
        """Called by plugin manager to inject PluginStorage instance."""
        self._storage = storage

    @property
    def storage(self):
        """Path-confined filesystem access (PluginStorage)."""
        return self._storage

    def set_http_client(self, client) -> None:
        """Called by plugin manager to inject PluginHttpClient."""
        self._http_client = client

    @property
    def http(self):
        """HTTP client (PluginHttpClient) for network requests."""
        return self._http_client

    def set_local_data_consent(self, consent: bool) -> None:
        """Set whether plugin has user consent for local data access."""
        self._local_data_consent = consent

    def has_local_data_consent(self) -> bool:
        """Check if plugin has user consent for local data access."""
        return self._local_data_consent

    # === STATUS METHODS ===

    @property
    def has_bridge_pairing(self) -> bool:
        """Whether this runner uses bridge-based pairing (IPC over TLS).

        Bridge runners don't have a local binary path — they connect to a
        remote host via TCP. The settings UI shows host/port fields and
        Pair/Unpair buttons instead of a path selector.

        Override to return True in bridge-based runners (e.g., Playnite).
        """
        return False

    def is_available(self) -> bool:
        """Check if this runner's launcher is detected on the system.

        Used by PluginsSettingsTab to show "Configured" vs "Not configured".
        Default implementation calls detect_launcher() and checks the result.

        Returns:
            True if the launcher is detected.
        """
        return self.detect_launcher() is not None

    # === ABSTRACT PROPERTIES ===

    @property
    @abstractmethod
    def runner_name(self) -> str:
        """Unique identifier for this runner plugin.

        Must be lowercase, alphanumeric + underscores only.

        Examples: "heroic", "lutris", "steam_client"
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI display.

        Examples: "Heroic Runner", "Lutris Runner"
        """
        pass

    @property
    @abstractmethod
    def supported_stores(self) -> List[str]:
        """List of store plugin names this provider can launch games for.

        Examples: ["gog", "epic"] for Heroic, ["steam"] for Steam native
        """
        pass

    # === CORE INTERFACE (new structured approach) ===

    @abstractmethod
    def detect_launcher(self) -> Optional["RunnerLauncherInfo"]:
        """Detect the launcher application on this system.

        Returns:
            RunnerLauncherInfo describing the found launcher, or None
            if the launcher is not installed.
        """
        pass

    @abstractmethod
    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional["LaunchIntent"]:
        """Build a structured launch intent for a game.

        Args:
            store_name: Store identifier ("steam", "gog", "epic")
            app_id: Store-specific app ID

        Returns:
            LaunchIntent describing how to launch the game, or None
            if this runner cannot launch the given game.
        """
        pass

    def execute_launch(self, intent: "LaunchIntent") -> "LaunchResult":
        """Execute a launch intent.

        Default implementation handles URL_SCHEME (via QDesktopServices)
        and EXECUTABLE (via subprocess.Popen with detached process flags).
        Override for custom launch behavior.

        Args:
            intent: LaunchIntent from build_launch_intent()

        Returns:
            LaunchResult with success status
        """
        from luducat.core.runtime_base import LaunchMethod as _LM, LaunchResult as _LR
        import logging as _logging
        _logger = _logging.getLogger(__name__)

        try:
            if intent.method == _LM.URL_SCHEME:
                return self._execute_url_launch(intent)
            elif intent.method == _LM.EXECUTABLE:
                return self._execute_binary_launch(intent)
            else:
                return _LR(
                    success=False,
                    platform_id=f"runner/{intent.runner_name}",
                    game_id=intent.app_id,
                    error_message=f"Unsupported launch method: {intent.method}",
                )
        except Exception as e:
            _logger.error("Runner launch failed: %s", e)
            return _LR(
                success=False,
                platform_id=f"runner/{intent.runner_name}",
                game_id=intent.app_id,
                error_message=str(e),
            )

    def can_launch_game(self, store_name: str, app_id: str) -> bool:
        """Check if this runner can launch a specific game.

        More thorough than checking ``supported_stores`` alone — can verify
        the game is installed in the launcher, for example.

        Default: returns True if store_name is in supported_stores.

        Args:
            store_name: Store identifier
            app_id: Store-specific app ID

        Returns:
            True if bridge can handle this game.
        """
        return store_name in self.supported_stores

    def get_launcher_priority(self) -> int:
        """Priority for multi-runner selection (higher = preferred).

        When multiple runners can launch a game, RuntimeManager picks
        the one with the highest priority. Default: 100.

        Returns:
            Integer priority value
        """
        return 100

    def get_install_methods(self) -> List[Dict[str, Any]]:
        """Return available installation methods for the launcher.

        Used by the runner config dialog to populate the Installation dropdown.
        Each entry describes a detected installation or the "Automatic" default.

        Returns:
            List of dicts with keys:
            - value: internal identifier (e.g., "automatic", "system", "flatpak")
            - label: display text (e.g., "System (/usr/bin/heroic)")
            - available: whether this method is currently usable
        """
        from luducat.plugins.sdk.app_finder import find_application

        methods = [{"value": "automatic", "label": _("Automatic"), "available": True}]

        # Get binary hints from runner config
        name_hints = [self.runner_name]
        flatpak_ids = []

        # Check plugin.json bridge config for flatpak_id
        # (subclasses can override for more specific detection)
        try:
            results = find_application(
                name_hints,
                flatpak_ids=flatpak_ids or None,
            )
            for r in results:
                label = f"{r.install_type.capitalize()}"
                if r.path:
                    label += f" ({r.path})"
                elif r.flatpak_id:
                    label += f" ({r.flatpak_id})"
                methods.append({
                    "value": r.install_type,
                    "label": label,
                    "available": True,
                })
        except Exception:
            pass

        methods.append({"value": "manual", "label": _("Manual"), "available": True})
        return methods

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        """Build URL to install a game via this runner.

        Returns a URL string that, when opened, triggers installation of
        the specified game in this runner's launcher application.

        Args:
            store_name: Store the game belongs to (e.g. "steam", "gog", "epic").
            app_id: Store-specific application identifier.

        Returns:
            Install URL string, or None if this runner cannot install games.
        """
        return None

    def get_installed_games(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Get games installed in this launcher.

        Optional method for runner plugins that can report installation
        status (e.g. Galaxy DB, Heroic config). Used to enrich
        ``is_installed`` / ``install_path`` in the main database.

        Returns:
            Dict mapping app_id -> {"installed": True, "install_path": str|None}
            or None if not supported.
        """
        return None

    # === DEPRECATED METHODS (backward compat) ===

    def get_launch_uri(self, store_name: str, app_id: str) -> Optional[str]:
        """Build a launch URI for a specific game.

        .. deprecated::
            Use :meth:`build_launch_intent` instead. This wrapper builds
            an intent and extracts the URL.
        """
        import warnings
        warnings.warn(
            "get_launch_uri() is deprecated, use build_launch_intent()",
            DeprecationWarning,
            stacklevel=2,
        )
        intent = self.build_launch_intent(store_name, app_id)
        return intent.url if intent else None

    def launch_game(self, store_name: str, app_id: str) -> bool:
        """Execute game launch via this provider.

        .. deprecated::
            Use :meth:`build_launch_intent` + :meth:`execute_launch` instead.
        """
        import warnings
        warnings.warn(
            "launch_game() is deprecated, use build_launch_intent() + execute_launch()",
            DeprecationWarning,
            stacklevel=2,
        )
        intent = self.build_launch_intent(store_name, app_id)
        if not intent:
            return False
        result = self.execute_launch(intent)
        return result.success

    # === DEFAULT LAUNCH IMPLEMENTATIONS ===

    def _execute_url_launch(self, intent: "LaunchIntent") -> "LaunchResult":
        """Launch via URL scheme using QDesktopServices."""
        from luducat.core.runtime_base import LaunchMethod as _LM, LaunchResult as _LR
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if not intent.url:
            return _LR(
                success=False,
                platform_id=f"runner/{intent.runner_name}",
                game_id=intent.app_id,
                error_message="No launch URL in intent",
            )

        import logging as _logging
        _logging.getLogger(__name__).info(
            "Runner %s: launching via URL %s", intent.runner_name, intent.url
        )
        success = QDesktopServices.openUrl(QUrl(intent.url))

        return _LR(
            success=success,
            platform_id=f"runner/{intent.runner_name}",
            game_id=intent.app_id,
            launch_method=_LM.URL_SCHEME,
            error_message=None if success else "Failed to open URL",
        )

    def _execute_binary_launch(self, intent: "LaunchIntent") -> "LaunchResult":
        """Launch via direct binary execution with detached process."""
        from luducat.core.runtime_base import LaunchMethod as _LM, LaunchResult as _LR
        import subprocess
        import sys

        if not intent.executable:
            return _LR(
                success=False,
                platform_id=f"runner/{intent.runner_name}",
                game_id=intent.app_id,
                error_message="No executable path in intent",
            )

        args = [str(intent.executable)] + intent.arguments

        import logging as _logging
        _logging.getLogger(__name__).info(
            "Runner %s: launching binary %s",
            intent.runner_name, " ".join(args),
        )

        env = None
        if intent.environment:
            import os
            env = os.environ.copy()
            env.update(intent.environment)

        if sys.platform == "win32":
            process = subprocess.Popen(
                args,
                cwd=intent.working_directory,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.DETACHED_PROCESS,
            )
        else:
            process = subprocess.Popen(
                args,
                cwd=intent.working_directory,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        return _LR(
            success=True,
            platform_id=f"runner/{intent.runner_name}",
            game_id=intent.app_id,
            process_id=process.pid,
            launch_method=_LM.EXECUTABLE,
        )

    # === LIFECYCLE HOOKS ===

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings."""
        pass

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings."""
        pass

    def close(self) -> None:
        """Called when application is shutting down."""
        pass


# Alias
RunnerPlugin = AbstractRunnerPlugin

# Re-export launch types so runner plugins import from base, not core
from luducat.core.runtime_base import (  # noqa: E402
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
    LaunchResult,
)
