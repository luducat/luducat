# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# constants.py

"""Application constants for luducat"""

# Bootstrap i18n markers — these must exist before any module uses N_().
# The real translations are installed later by init_i18n().
import builtins
if not hasattr(builtins, "N_"):
    builtins.N_ = lambda s: s
if not hasattr(builtins, "_"):
    builtins._ = lambda s: s
if not hasattr(builtins, "ngettext"):
    builtins.ngettext = lambda singular, plural, n: singular if n == 1 else plural

APP_NAME = "luducat"
APP_VERSION = "0.7.0"
APP_ID = "com.luducat.luducat"  # Desktop file name / GNOME App ID
APP_ICON_BASENAME = "app_icon"
APP_DESCRIPTION = N_("Cross-platform game catalogue browser")
APP_HOMEPAGE = "https://www.luducat.org"
APP_RELEASES_URL = "https://github.com/luducat/luducat/releases"
UPDATE_CHECK_URL = "https://luducat-api-proxy.luducat-cloudflare.workers.dev/version"
APP_AUTHOR = "luducat@trinity2k.net"
APP_LICENSE = "GPL-3.0-or-later"

# Dev build flag — set True for private tester builds, never in public release repo
IS_DEV_BUILD = False

# Full version string for display (e.g., "0.5.0" or "0.5.0.1 (dev)")
APP_VERSION_FULL = f"{APP_VERSION} (dev)" if IS_DEV_BUILD else APP_VERSION

# HTTP User-Agent strings (centralized — do NOT hardcode elsewhere)
# Default UA: browser-like for web requests (plugins + image cache + all HTTP)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
# Gateway UA: app-identifying, EXCLUSIVELY for luducat API proxy
USER_AGENT_GW = f"luducat/{APP_VERSION} (game catalog browser)"
# Backwards compatibility alias
USER_AGENT_BROWSER = USER_AGENT

# Configuration schema version (increment when config structure changes)
CONFIG_VERSION = 7

# Database schema version (managed by Alembic, but tracked here too)
DATABASE_VERSION = 1

# Distribution mode - set True during Nuitka build
# Disables user plugin directory scanning (incompatible with compiled binary)
COMPILED_BUILD = True

# Default backup location
DEFAULT_BACKUP_DIRNAME = "luducat_backups"

# Default UI settings
DEFAULT_WINDOW_WIDTH = 1200
DEFAULT_WINDOW_HEIGHT = 800
DEFAULT_LIST_PANEL_WIDTH = 196
DEFAULT_GRID_DENSITY = 250  # CSS --grid-min-size value in pixels
DEFAULT_RECENT_PLAYED_DAYS = 14  # Days for "Recently Played" filter

# Grid density bounds
GRID_DENSITY_MIN = 100   # 16 items per row
GRID_DENSITY_MAX = 500   # 2 items per row

# Image fade-in duration (ms) for grid views
DEFAULT_IMAGE_FADE_MS = 100
IMAGE_FADE_MIN_MS = 0      # 0 = disabled
IMAGE_FADE_MAX_MS = 500

# Cache settings
DEFAULT_THUMBNAIL_CACHE_MB = 500
DEFAULT_SCREENSHOT_CACHE_MB = 16384
DEFAULT_CACHE_CLEANUP_DAYS = 7200

# Rate limiting defaults (can be overridden per-plugin)
DEFAULT_RATE_LIMIT_CALLS = 200
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 300

# View modes
VIEW_MODE_LIST = "list"
VIEW_MODE_COVER = "cover"
VIEW_MODE_SCREENSHOT = "screenshot"

# Sort modes
SORT_MODE_NAME = "name"
SORT_MODE_RECENT = "recent"
SORT_MODE_ADDED = "added"  # Date added to catalog (added_at in main DB)
SORT_MODE_PUBLISHER = "publisher"
SORT_MODE_DEVELOPER = "developer"
SORT_MODE_RELEASE = "release"
SORT_MODE_FRANCHISE = "franchise"
SORT_MODE_FAMILY_LICENSES = "family_licenses"

# Base filter (radio - mutually exclusive)
FILTER_BASE_ALL = "all"
FILTER_BASE_RECENT = "recent"  # Recently played (last_launched not null)
FILTER_BASE_HIDDEN = "hidden"  # Show hidden games only

# Type filters (checkboxes - can combine, OR'd)
FILTER_TYPE_FREE = "free"            # is_free = true in store DB
FILTER_TYPE_FAVORITES = "favorites"  # is_favorite = true
FILTER_TYPE_INSTALLED = "installed"  # is_installed = true (at least one store)
FILTER_TYPE_DEMOS = "demos"          # demo/prologue/trial suffix in title

# Legacy constants for backwards compatibility
FILTER_ALL = FILTER_BASE_ALL
FILTER_FAVORITES = FILTER_TYPE_FAVORITES
FILTER_RECENT = FILTER_BASE_RECENT

# Corner triangle badges for free/demo games (top-left of covers)
CORNER_TRIANGLE_SIZE = 30
CORNER_FREE_COLORS = {"bg": "#4caf50", "text": "#ffffff"}
CORNER_DEMO_COLORS = {"bg": "#1976d2", "text": "#ffffff"}

# Game mode badge configuration
# Maps game mode names to short display labels for badges
# "Single player" is intentionally omitted from badges (implicit default — 97% of games have it)
# Sources: IGDB provides generic modes (Multiplayer, Co-operative, Split screen, MMO, BR).
#          PCGamingWiki provides granular local subtypes (Local Co-op, Local Versus).
#          "Split screen" is the IGDB fallback when PCGW detail is unavailable.
GAME_MODE_LABELS = {
    "PVP": N_("PVP"),
    "Multiplayer": N_("MP"),
    "Co-operative": N_("CO-OP"),
    "Split screen": N_("LOCAL"),
    "Local Co-op": N_("L-COOP"),
    "Local Versus": N_("L-VS"),
    "Online Versus": N_("O-VS"),
    "LAN": N_("LAN"),
    "Massively Multiplayer Online (MMO)": N_("MMO"),
    "Battle Royale": N_("BR"),
}

# Game modes available for filtering (includes Single player)
# Maps game mode names to display labels for filter menu
GAME_MODE_FILTERS = {
    "Single player": N_("Single Player"),
    "PVP": N_("PVP"),
    "Multiplayer": N_("Multiplayer"),
    "Co-operative": N_("Co-op"),
    "Split screen": N_("Local/Split Screen"),
    "Local Co-op": N_("Local Co-op"),
    "Local Versus": N_("Local Versus"),
    "Online Versus": N_("Online Versus"),
    "LAN": N_("LAN"),
    "Massively Multiplayer Online (MMO)": N_("MMO"),
    "Battle Royale": N_("Battle Royale"),
}

# Installed badge colors and label
INSTALLED_BADGE_COLOR = {"bg": "#2e7d32", "text": "#ffffff"}
INSTALLED_BADGE_LABEL = N_("INST")

# ProtonDB tier badge labels and colors (brand colors - OK to hardcode for overlays)
# Keys are lowercase to match ProtonDB API response format
PROTONDB_TIER_LABELS = {
    "platinum": N_("PLAT"),
    "gold": N_("GOLD"),
    "silver": N_("SILVER"),
    "bronze": N_("BRONZE"),
    "borked": N_("BORKED"),
}
PROTONDB_TIER_COLORS = {
    "platinum": {"bg": "#b4c7dc", "text": "#1a1a1a"},
    "gold": {"bg": "#cfb53b", "text": "#1a1a1a"},
    "silver": {"bg": "#a6a6a6", "text": "#1a1a1a"},
    "bronze": {"bg": "#cd7f32", "text": "#ffffff"},
    "borked": {"bg": "#ff0000", "text": "#ffffff"},
}

# Steam Deck compatibility badge labels and colors
STEAM_DECK_LABELS = {
    "verified": N_("DECK OK"),
    "playable": N_("DECK"),
    "unsupported": N_("DECK N/A"),
}
STEAM_DECK_COLORS = {
    "verified": {"bg": "#59bf40", "text": "#ffffff"},
    "playable": {"bg": "#ffc82c", "text": "#1a1a1a"},
    "unsupported": {"bg": "#8b0000", "text": "#ffffff"},
}

# Default color for user-created tags (KDE Breeze accent blue)
DEFAULT_TAG_COLOR = "#3daee9"

# Tag source colors (brand colors for source color mode)
TAG_SOURCE_COLORS = {
    "native": None,  # Uses theme accent color
    "steam": "#1b2838",
    "gog": "#a259ff",
    "epic": "#2a2a2a",
    "heroic": "#aa00ff",
    "lutris": "#ff9800",
    "playnite": "#00b4d8",
    "imported": "#e67e22",
    "zoom": "#1a1a2e",
    "jastusa": "#1a237e",
    "mangagamer": "#e91e63",
}
