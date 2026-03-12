# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# metadata_resolver.py

"""Metadata priority resolution for multi-store games

When a game exists in multiple stores, this module resolves which
store's metadata to use for each field based on a priority list.

Also handles on-demand metadata fetching from plugins in priority order,
calling each enabled+available plugin until metadata is found.

Supports per-field configurable priorities via settings UI.

This is the SINGLE SOURCE OF TRUTH for all metadata operations.
GameService and other components must use MetadataResolver for ALL
metadata access - no direct plugin calls for metadata.
"""

import hashlib
from luducat.core.json_compat import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

if TYPE_CHECKING:
    from luducat.core.plugin_manager import PluginManager
    from luducat.plugins.base import EnrichmentData, Game as PluginGame

logger = logging.getLogger(__name__)


# Hardcoded fallback - used when no plugin declares provides_fields
_DEFAULT_FIELD_SOURCE_CAPABILITIES: Dict[str, List[str]] = {
    # General
    "title": ["steam", "gog", "epic", "igdb", "pcgamingwiki"],
    "description": ["steam", "gog", "igdb"],
    "short_description": ["steam", "gog", "epic", "igdb"],
    "developers": ["steam", "gog", "epic", "igdb", "pcgamingwiki"],
    "publishers": ["steam", "gog", "epic", "igdb", "pcgamingwiki"],
    "genres": ["steam", "gog", "epic", "igdb", "pcgamingwiki"],
    "release_date": ["steam", "gog", "igdb", "pcgamingwiki"],  # Merged field
    "game_modes_detail": ["pcgamingwiki", "steam", "gog", "igdb"],
    "crossplay": ["pcgamingwiki"],  # Only PCGW has crossplay data
    # Media
    "cover": ["steamgriddb", "steam", "gog", "epic", "igdb"],
    "hero": ["steamgriddb", "igdb", "steam", "epic", "gog"],
    "screenshots": ["steam", "gog", "epic", "igdb"],
    "header_url": ["steam", "gog", "epic"],  # Landscape banner — only store plugins
    "artworks": ["igdb"],
    "icon_url": ["steam", "gog"],
    "logo_url": ["steam", "gog", "epic"],
    "videos": ["igdb", "gog"],
    # Ratings
    "rating": ["steam", "gog", "igdb"],
    "rating_positive": ["steam"],
    "rating_negative": ["steam"],
    "total_rating": ["igdb"],
    "user_rating": ["igdb"],
    "age_ratings": ["igdb", "gog"],  # IGDB structured + GOG content_ratings converted
    "age_rating_esrb": ["igdb"],  # Virtual: extracted from age_ratings at display time
    "age_rating_pegi": ["igdb"],  # Virtual: extracted from age_ratings at display time
    "critic_rating": ["steam", "pcgamingwiki"],
    "critic_rating_url": ["steam"],
    "opencritic_score": ["pcgamingwiki"],
    "opencritic_id": ["pcgamingwiki"],
    # Extended
    "franchise": ["igdb", "gog", "pcgamingwiki"],
    "series": ["igdb", "gog", "pcgamingwiki"],
    "collections": ["igdb"],
    "tags": ["steam", "gog"],
    "themes": ["igdb", "pcgamingwiki"],
    "perspectives": ["igdb", "pcgamingwiki"],
    "platforms": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "links": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "storyline": ["igdb"],
    "pacing": ["pcgamingwiki"],
    "art_styles": ["pcgamingwiki"],
    "dlcs": ["steam", "gog"],
    "changelog": ["gog"],
    "content_ratings": ["gog"],  # Raw GOG content rating objects
    "editions": ["gog"],  # GOG game editions
    # Technical
    "engine": ["pcgamingwiki"],
    "controller_support": ["pcgamingwiki", "steam", "gog"],
    "full_controller_support": ["pcgamingwiki", "steam"],
    "controller_remapping": ["pcgamingwiki"],
    "controller_sensitivity": ["pcgamingwiki"],
    "controller_haptic_feedback": ["pcgamingwiki"],
    "key_remapping": ["pcgamingwiki"],
    "mouse_sensitivity": ["pcgamingwiki"],
    "mouse_acceleration": ["pcgamingwiki"],
    "mouse_input_in_menus": ["pcgamingwiki"],
    "touchscreen": ["pcgamingwiki"],
    "controls": ["pcgamingwiki"],
    "monetization": ["pcgamingwiki"],
    "microtransactions": ["pcgamingwiki"],
    # Cross-store data fields
    "supported_languages": ["steam", "gog"],
    "full_audio_languages": ["steam", "gog"],
    "features": ["steam", "gog", "epic"],
    # Game classification
    "category": ["igdb"],
    "status": ["igdb"],
    "game_modes": ["pcgamingwiki", "steam", "igdb"],
    "required_age": ["steam", "gog", "igdb"],
    "content_descriptors": ["steam"],  # Steam NSFW descriptors (content filter)
    # Commercial
    "price": ["steam", "gog", "epic"],
    "type": ["steam", "gog", "epic", "igdb"],
    "is_free": ["steam", "gog"],
    # Platform detail
    "windows": ["pcgamingwiki", "igdb"],
    "mac": ["pcgamingwiki", "igdb"],
    "linux": ["pcgamingwiki", "igdb"],
    "crossplay_platforms": ["pcgamingwiki"],
    # Statistics
    "achievements": ["steam", "gog"],
    "estimated_owners": ["steam"],
    "recommendations": ["steam"],
    "peak_ccu": ["steam"],
    "average_playtime": ["steam"],
    "average_playtime_forever": ["steam"],
    "average_playtime_2weeks": ["steam"],
    "playtime_minutes": ["steam"],
    # Compatibility (single-source)
    "protondb_rating": ["protondb"],
    "protondb_score": ["protondb"],
    "steam_deck_compat": ["steam"],
}

# Mutable alias - mutated in-place by build_from_plugins() so all importers see updates
FIELD_SOURCE_CAPABILITIES: Dict[str, List[str]] = dict(_DEFAULT_FIELD_SOURCE_CAPABILITIES)

# Field groupings for UI organization
FIELD_GROUPS: Dict[str, List[str]] = {
    "General": [
        "title", "description", "short_description", "developers", "publishers",
        "genres", "release_date", "game_modes_detail", "crossplay",
        "supported_languages", "full_audio_languages", "features",
        "type", "is_free", "required_age", "category", "status",
        "game_modes", "price",
    ],
    "Media": [
        "cover", "hero", "screenshots", "header_url",
        "artworks", "icon_url", "logo_url", "videos",
    ],
    "Ratings": [
        "rating", "rating_positive", "rating_negative", "total_rating", "user_rating",
        "age_rating_esrb", "age_rating_pegi", "content_ratings", "content_descriptors",
        "critic_rating", "critic_rating_url", "opencritic_score", "opencritic_id",
        "protondb_rating", "protondb_score", "steam_deck_compat",
    ],
    "Extended": [
        "franchise", "series", "collections", "themes", "perspectives",
        "platforms", "links", "pacing", "art_styles", "storyline",
        "crossplay_platforms", "editions",
    ],
    "Technical": [
        "engine", "controller_support", "full_controller_support",
        "controller_remapping", "controller_sensitivity", "controller_haptic_feedback",
        "key_remapping", "mouse_sensitivity", "mouse_acceleration", "mouse_input_in_menus",
        "touchscreen", "controls", "monetization", "microtransactions",
        "windows", "mac", "linux",
    ],
    "Statistics": [
        "achievements", "estimated_owners", "recommendations", "peak_ccu",
        "average_playtime", "average_playtime_forever", "average_playtime_2weeks",
        "playtime_minutes",
    ],
}

# Human-readable labels for fields (N_() marks for extraction, _() at use site)
FIELD_LABELS: Dict[str, str] = {
    "title": N_("Title"),
    "description": N_("Description"),
    "short_description": N_("Short Desc."),
    "developers": N_("Developer"),
    "publishers": N_("Publisher"),
    "genres": N_("Genre"),
    "release_date": N_("Release Date"),
    "game_modes_detail": N_("Multiplayer"),
    "crossplay": N_("Crossplay"),
    "crossplay_platforms": N_("Crossplay Plat."),
    "cover": N_("Cover"),
    "hero": N_("Hero Banner"),
    "screenshots": N_("Screenshots"),
    "header_url": N_("Header Image"),
    "artworks": N_("Artworks"),
    "icon_url": N_("Icon"),
    "logo_url": N_("Logo"),
    "videos": N_("Videos"),
    "rating": N_("Rating"),
    "rating_positive": N_("Positive Ratings"),
    "rating_negative": N_("Negative Ratings"),
    "total_rating": N_("Total Rating"),
    "user_rating": N_("User Rating"),
    "age_ratings": N_("Age Ratings"),
    "age_rating_esrb": N_("ESRB Rating"),
    "age_rating_pegi": N_("PEGI Rating"),
    "content_ratings": N_("Content Ratings"),
    "content_descriptors": N_("Descriptors"),
    "editions": N_("Editions"),
    "critic_rating": N_("Metacritic"),
    "critic_rating_url": N_("Metacritic URL"),
    "opencritic_score": N_("OpenCritic Score"),
    "opencritic_id": N_("OpenCritic ID"),
    "franchise": N_("Franchise"),
    "series": N_("Series"),
    "collections": N_("Collections"),
    "themes": N_("Themes"),
    "perspectives": N_("Perspectives"),
    "platforms": N_("Platforms"),
    "links": N_("Links"),
    "storyline": N_("Storyline"),
    "pacing": N_("Pacing"),
    "art_styles": N_("Art Style"),
    "engine": N_("Engine"),
    "controller_support": N_("Controller"),
    "full_controller_support": N_("Full Controller"),
    "controller_remapping": N_("Ctrl. Remapping"),
    "controller_sensitivity": N_("Ctrl. Sensitivity"),
    "controller_haptic_feedback": N_("Ctrl. Haptics"),
    "key_remapping": N_("Key Remapping"),
    "mouse_sensitivity": N_("Mouse Sensitivity"),
    "mouse_acceleration": N_("Mouse Accel."),
    "mouse_input_in_menus": N_("Mouse in Menus"),
    "touchscreen": N_("Touchscreen"),
    "controls": N_("Controls"),
    "monetization": N_("Monetization"),
    "microtransactions": N_("Microtransactions"),
    "supported_languages": N_("Languages"),
    "full_audio_languages": N_("Audio Languages"),
    "features": N_("Features"),
    "category": N_("Category"),
    "status": N_("Status"),
    "game_modes": N_("Game Modes"),
    "required_age": N_("Required Age"),
    "price": N_("Price"),
    "type": N_("Type"),
    "is_free": N_("Free to Play"),
    "windows": "Windows",
    "mac": "macOS",
    "linux": "Linux",
    "achievements": N_("Achievements"),
    "estimated_owners": N_("Est. Owners"),
    "recommendations": N_("Recommendations"),
    "peak_ccu": N_("Peak Players"),
    "average_playtime": N_("Avg. Playtime"),
    "average_playtime_forever": N_("Avg. Total Play"),
    "average_playtime_2weeks": N_("Avg. Recent Play"),
    "playtime_minutes": N_("Playtime"),
    "protondb_rating": N_("ProtonDB Rating"),
    "protondb_score": N_("ProtonDB Score"),
    "steam_deck_compat": N_("Steam Deck"),
}

# Plain-English tooltips for field labels in the priority settings UI
# N_() marks for extraction, _() at use site
FIELD_TOOLTIPS: Dict[str, str] = {
    # General
    "title": N_("The game's display name."),
    "description": N_("Full game description shown in the detail view."),
    "short_description": N_("Brief summary shown in tooltips and previews."),
    "developers": N_("Studio or person who made the game."),
    "publishers": N_("Company that published the game."),
    "genres": N_("Game genres like Action, RPG, or Strategy."),
    "release_date": N_("When the game was released, per platform."),
    "game_modes_detail": N_("Multiplayer mode details (co-op, split screen, etc.)."),
    "crossplay": N_("Whether the game supports cross-platform play."),
    "supported_languages": N_("Languages the game supports for text."),
    "full_audio_languages": N_("Languages with full voice acting."),
    "features": N_("Store features like cloud saves, achievements, overlay."),
    "type": N_("Whether this is a game, DLC, demo, or mod."),
    "is_free": N_("Whether the game is free to play."),
    "required_age": N_("Minimum age required by the store."),
    "category": N_("IGDB game category (main game, expansion, bundle, etc.)."),
    "status": N_("Release status (released, early access, cancelled, etc.)."),
    "game_modes": N_("Basic game mode tags (singleplayer, multiplayer)."),
    "price": N_("Current store price."),
    # Media
    "cover": N_("Vertical box art shown in grid view."),
    "hero": N_("Wide banner image shown in the detail view header."),
    "screenshots": N_("In-game screenshots gallery."),
    "header_url": N_("Landscape banner image used by stores."),
    "artworks": N_("Promotional artwork and key art from IGDB."),
    "icon_url": N_("Small square icon used in compact views."),
    "logo_url": N_("Game logo image (transparent background)."),
    "videos": N_("Trailers and gameplay video links."),
    # Ratings
    "rating": N_("Store user review score."),
    "rating_positive": N_("Number of positive user reviews."),
    "rating_negative": N_("Number of negative user reviews."),
    "total_rating": N_("IGDB combined critic and user score."),
    "user_rating": N_("IGDB user-submitted rating."),
    "age_ratings": N_("Age rating board classifications."),
    "age_rating_esrb": N_("ESRB rating (North America)."),
    "age_rating_pegi": N_("PEGI rating (Europe)."),
    "content_ratings": N_("Raw content rating objects from the store."),
    "content_descriptors": N_("Content warnings (violence, language, etc.)."),
    "editions": N_("Available game editions (standard, deluxe, etc.)."),
    "critic_rating": N_("Metacritic aggregate score."),
    "critic_rating_url": N_("Link to the Metacritic page."),
    "opencritic_score": N_("OpenCritic aggregate score."),
    "opencritic_id": N_("OpenCritic database identifier."),
    "protondb_rating": N_("Community Linux compatibility tier."),
    "protondb_score": N_("Numeric ProtonDB compatibility score."),
    "steam_deck_compat": N_("Valve's Steam Deck compatibility verdict."),
    # Extended
    "franchise": N_("Franchise the game belongs to."),
    "series": N_("Game series or sub-franchise."),
    "collections": N_("IGDB collections grouping related games."),
    "themes": N_("Thematic tags like horror, fantasy, sci-fi."),
    "perspectives": N_("Camera perspective (first-person, top-down, etc.)."),
    "platforms": N_("Platforms the game is available on."),
    "links": N_("External links (official site, wiki, social media)."),
    "storyline": N_("Plot synopsis from IGDB."),
    "pacing": N_("Game pacing style (real-time, turn-based)."),
    "art_styles": N_("Visual art style classification."),
    "crossplay_platforms": N_("Which platforms support crossplay."),
    # Technical
    "engine": N_("Game engine used (Unity, Unreal, etc.)."),
    "controller_support": N_("Whether controllers are supported."),
    "full_controller_support": N_("Whether the game is fully playable with a controller."),
    "controller_remapping": N_("Whether controller buttons can be remapped."),
    "controller_sensitivity": N_("Whether controller sensitivity is adjustable."),
    "controller_haptic_feedback": N_("Whether the controller supports haptic feedback."),
    "key_remapping": N_("Whether keyboard keys can be rebound."),
    "mouse_sensitivity": N_("Whether mouse sensitivity is adjustable."),
    "mouse_acceleration": N_("Whether mouse acceleration can be toggled."),
    "mouse_input_in_menus": N_("Whether menus can be navigated with a mouse."),
    "touchscreen": N_("Whether touchscreen input is supported."),
    "controls": N_("General control scheme information."),
    "monetization": N_("Monetization model (one-time, subscription, etc.)."),
    "microtransactions": N_("Whether the game has microtransactions."),
    "windows": N_("Windows platform support details."),
    "mac": N_("macOS platform support details."),
    "linux": N_("Linux platform support details."),
    # Statistics
    "achievements": N_("Total number of achievements."),
    "estimated_owners": N_("Estimated number of owners (SteamSpy)."),
    "recommendations": N_("Number of user recommendations."),
    "peak_ccu": N_("Peak concurrent players ever recorded."),
    "average_playtime": N_("Average playtime across all players."),
    "average_playtime_forever": N_("Average total playtime since launch."),
    "average_playtime_2weeks": N_("Average playtime in the last two weeks."),
    "playtime_minutes": N_("Your personal playtime in minutes."),
}

# Human-readable labels for sources (hardcoded fallback)
_DEFAULT_SOURCE_LABELS: Dict[str, str] = {
    "steam": "Steam",
    "gog": "GOG",
    "epic": "Epic Games",
    "igdb": "IGDB",
    "pcgamingwiki": "PCGamingWiki",
    "steamgriddb": "SteamGridDB",
}

# Mutable alias - mutated in-place by build_from_plugins()
SOURCE_LABELS: Dict[str, str] = dict(_DEFAULT_SOURCE_LABELS)


def _title_similarity(title_a: str, title_b: str) -> float:
    """Calculate Jaccard word-overlap similarity between two normalized titles.

    Args:
        title_a: First normalized title
        title_b: Second normalized title

    Returns:
        Similarity score 0.0-1.0
    """
    if title_a == title_b:
        return 1.0
    words_a = set(title_a.split())
    words_b = set(title_b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# Internal/bookkeeping fields — bypass priority resolution entirely.
# These are set by the enrichment system, not user-configurable metadata.
_INTERNAL_FIELDS: frozenset = frozenset({
    "_sources",
    "data_source",
    "enriched",
    "background_provider",
    # Cross-store IDs (single-source, used for cross-referencing only)
    "igdb_id",
    "igdb_url",
    "gogid",
    "pcgw_page_id",
    "pcgw_page_name",
    "steamgriddb_id",
    "howlongtobeat_id",  # Exposed as-is for future HLTB integration
    "wikipedia_id",
    "mobygames_id",
    "metacritic_id",
    # Per-store fields: not priority-resolved, each store keeps its own
    "slug",
    "dlcs",
    "is_available",
    "changelog",
    "downloads_json",
    # Legacy store tags — not priority-resolved, native tag system used
    "tags",
    "keywords",
    "user_tags",
    # GOG-specific description source fields
    # (used by plugin to derive description/short_description)
    "description_lead",
    "description_cool",
    # Single-source URL fields (feed into merged `links`)
    "website",
    "official_url",
    # Legacy fields — computed at runtime or aliased
    "release_year",  # Computed from release_date dict
    # Plural variants of aliased fields
    "engines",  # PCGamingWiki plural → engine
})

# Merged fields — collected from ALL sources (not priority-resolved).
# MetadataResolver applies special merge logic instead of pick-first-by-priority.
_MERGED_FIELDS: frozenset = frozenset({
    "release_date",  # Per-platform dict, oldest date per platform wins
    # "links" — follow-up: URL normalization + dedup from all plugins
})

# --- Seed defaults ---
# These exist ONLY for config seeding (first run / migration).
# At runtime, MetadataResolver reads ONLY from user config.
# Do NOT reference these from MetadataResolver or any runtime code path.

_SEED_STORE_PRIORITY = ["steam", "gog", "epic"]
_SEED_METADATA_PRIORITY = ["pcgamingwiki", "steamgriddb", "igdb"]
_SEED_HERO_PRIORITY = ["steamgriddb", "igdb", "steam", "epic"]
_SEED_GAME_MODES_PRIORITY = ["pcgamingwiki", "igdb"]

_SEED_FIELD_PRIORITIES: Dict[str, List[str]] = {
    # General
    "title": ["steam", "gog", "epic", "igdb", "pcgamingwiki"],
    "description": ["steam", "gog", "igdb"],
    "short_description": ["steam", "gog", "epic", "igdb"],
    "developers": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "publishers": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "genres": ["pcgamingwiki", "igdb", "steam", "gog", "epic"],
    "release_date": ["steam", "gog", "igdb", "pcgamingwiki"],  # Merged field
    "game_modes_detail": ["pcgamingwiki", "steam", "gog", "igdb"],
    "crossplay": ["pcgamingwiki"],
    # Media
    "cover": ["igdb", "steamgriddb", "steam", "epic", "gog"],
    "hero": ["steamgriddb", "igdb", "steam", "epic", "gog"],
    "screenshots": ["steam", "gog", "epic", "igdb"],
    "header_url": ["steam", "epic", "gog"],
    "artworks": ["igdb"],
    "icon_url": ["steam", "gog"],
    "logo_url": ["steam", "gog", "epic"],
    "videos": ["igdb", "gog"],
    # Ratings
    "rating": ["steam", "gog", "igdb"],
    "user_rating": ["igdb"],
    "user_rating_count": ["igdb"],
    "rating_positive": ["steam"],
    "rating_negative": ["steam"],
    "total_rating": ["igdb"],
    "age_ratings": ["igdb", "gog"],
    "age_rating_esrb": ["igdb"],  # Virtual: extracted from age_ratings at display time
    "age_rating_pegi": ["igdb"],  # Virtual: extracted from age_ratings at display time
    "critic_rating": ["steam", "pcgamingwiki"],
    "critic_rating_url": ["steam"],
    "opencritic_score": ["pcgamingwiki"],
    "opencritic_id": ["pcgamingwiki"],
    # Extended
    "franchise": ["igdb", "gog", "pcgamingwiki"],
    "series": ["igdb", "gog", "pcgamingwiki"],
    "collections": ["igdb"],
    "tags": ["steam", "gog"],
    "themes": ["igdb", "pcgamingwiki"],
    "perspectives": ["igdb", "pcgamingwiki"],
    "platforms": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "links": ["igdb", "pcgamingwiki", "steam", "gog", "epic"],
    "storyline": ["igdb"],
    "pacing": ["pcgamingwiki"],
    "art_styles": ["pcgamingwiki"],
    "dlcs": ["steam", "gog"],
    "changelog": ["gog"],
    "content_ratings": ["gog"],  # Raw GOG content rating objects
    "editions": ["gog"],  # GOG game editions
    # Technical
    "engine": ["pcgamingwiki"],
    "controller_support": ["pcgamingwiki", "steam", "gog"],
    "full_controller_support": ["pcgamingwiki", "steam"],
    "controller_remapping": ["pcgamingwiki"],
    "controller_sensitivity": ["pcgamingwiki"],
    "controller_haptic_feedback": ["pcgamingwiki"],
    "key_remapping": ["pcgamingwiki"],
    "mouse_sensitivity": ["pcgamingwiki"],
    "mouse_acceleration": ["pcgamingwiki"],
    "mouse_input_in_menus": ["pcgamingwiki"],
    "touchscreen": ["pcgamingwiki"],
    "controls": ["pcgamingwiki"],
    "monetization": ["pcgamingwiki"],
    "microtransactions": ["pcgamingwiki"],
    # Cross-store data fields
    "supported_languages": ["steam", "gog"],
    "full_audio_languages": ["steam", "gog"],
    "features": ["gog", "steam", "epic"],
    # Game classification
    "category": ["igdb"],
    "status": ["igdb"],
    "game_modes": ["pcgamingwiki", "steam", "igdb"],
    "required_age": ["steam", "gog", "igdb"],
    "content_descriptors": ["steam"],
    # Commercial
    "price": ["steam", "gog", "epic"],
    "type": ["steam", "gog", "epic", "igdb"],
    "is_free": ["steam", "gog"],
    # Platform detail
    "windows": ["pcgamingwiki", "igdb"],
    "mac": ["pcgamingwiki", "igdb"],
    "linux": ["pcgamingwiki", "igdb"],
    "crossplay_platforms": ["pcgamingwiki"],
    # Statistics
    "achievements": ["steam", "gog"],
    "estimated_owners": ["steam"],
    "recommendations": ["steam"],
    "peak_ccu": ["steam"],
    "average_playtime": ["steam"],
    "average_playtime_forever": ["steam"],
    "average_playtime_2weeks": ["steam"],
    "playtime_minutes": ["steam"],
    # Compatibility (single-source)
    "protondb_rating": ["protondb"],
    "protondb_score": ["protondb"],
    "steam_deck_compat": ["steam"],
}


class MetadataResolver:
    """Resolves metadata from multiple stores based on priority order.

    At runtime, ALL priorities come from user config (seeded on first run).
    No hardcoded fallbacks — if a field has no config entry, that's a
    migration bug. Use get_field_priority() for all priority lookups.

    Usage:
        # Singleton — initialized once at app startup via init_resolver()
        resolver = get_resolver()
        resolver.set_plugin_manager(plugin_manager)
        resolved = resolver.resolve_game_metadata(metadata_by_store)
    """

    # Backward-compat aliases: old stored metadata_json keys → canonical names.
    # New plugin output uses canonical names directly. These shrink over time
    # as users re-sync and old metadata_json entries are overwritten.
    _PRIORITY_FIELD_ALIASES: Dict[str, str] = {
        "cover_url": "cover",
        "background_url": "hero",
        "websites": "links",
        "categories": "features",
        "franchises": "franchise",
        "languages": "supported_languages",
    }

    def __init__(
        self,
        field_priorities: Dict[str, List[str]],
    ):
        """Initialize the resolver.

        Args:
            field_priorities: Per-field priority lists loaded from user config.
                              REQUIRED — no hardcoded fallbacks.
        """
        self._plugin_manager: Optional["PluginManager"] = None

        # Track which fields user has explicitly customized
        self._user_overrides: set = set()
        # Track whether build_from_plugins has been called
        self._build_initialized: bool = False
        # Fields we've already warned about (warn once per field)
        self._missing_fields_warned: set = set()

        # Per-field priorities from config — the ONLY source of truth
        self._field_priorities: Dict[str, List[str]] = {
            k: v.copy() for k, v in field_priorities.items()
        }

        # Callback fired when priorities change (for cache invalidation)
        self._on_priorities_changed: Optional[Callable] = None

    def set_plugin_manager(self, plugin_manager: "PluginManager") -> None:
        """Set the plugin manager for on-demand metadata fetching

        Args:
            plugin_manager: The application's plugin manager instance
        """
        self._plugin_manager = plugin_manager

    def get_field_priority(self, field_name: str) -> List[str]:
        """Get the priority order for a specific field

        Args:
            field_name: Name of the metadata field

        Returns:
            List of source names in priority order (highest first).
            Returns [] for internal/tracking fields (silent bypass).
            Returns [] with warning for truly unknown fields.
        """
        # Canonicalize storage keys to field names (e.g. "cover_url" → "cover")
        canonical = self._PRIORITY_FIELD_ALIASES.get(field_name, field_name)
        if canonical in self._field_priorities:
            return self._field_priorities[canonical].copy()
        if field_name != canonical and field_name in self._field_priorities:
            return self._field_priorities[field_name].copy()
        # Internal/tracking fields — bypass priority silently
        if field_name in _INTERNAL_FIELDS or field_name.startswith("_"):
            return []
        # Merged fields — handled by special merge logic, not priority
        if field_name in _MERGED_FIELDS:
            return []
        # Truly unknown field — log once at warning level
        if field_name not in self._missing_fields_warned:
            self._missing_fields_warned.add(field_name)
            logger.warning(
                f"No priority config for field '{field_name}' — "
                f"add to _SEED_FIELD_PRIORITIES or _INTERNAL_FIELDS"
            )
        return []

    def _is_store_plugin(self, plugin_name: str) -> bool:
        """Check if a plugin is a store plugin via PluginManager.

        Delegates to PluginManager class-level registry.
        """
        from luducat.core.plugin_manager import PluginManager
        return PluginManager.is_store_plugin(plugin_name)

    def _get_all_priority_sources(self) -> List[str]:
        """Get all unique sources from field priorities, order-preserved.

        Used when no specific field is requested (full metadata gather).
        """
        seen: set = set()
        result: List[str] = []
        for sources in self._field_priorities.values():
            for s in sources:
                if s not in seen:
                    seen.add(s)
                    result.append(s)
        return result

    def get_field_priority_rank(self, field_name: str, source_name: str) -> int:
        """Get the priority rank of a source for a specific field

        Lower rank = higher priority. Returns a high number if not in list.

        Args:
            field_name: Name of the metadata field
            source_name: Name of the source/provider

        Returns:
            Priority rank (0 = highest priority)
        """
        priority = self.get_field_priority(field_name)
        try:
            return priority.index(source_name)
        except ValueError:
            return 999  # Not in priority list, lowest priority

    def set_field_priorities(self, priorities: Dict[str, List[str]]) -> None:
        """Update per-field priority settings

        Marks each field as user-overridden so build_from_plugins() won't
        clobber it.

        Args:
            priorities: Dict mapping field_name -> list of sources in priority order
        """
        for field_name, priority_list in priorities.items():
            if field_name in self._field_priorities:
                self._field_priorities[field_name] = priority_list.copy()
                self._user_overrides.add(field_name)
        logger.debug(f"Field priorities updated for {len(priorities)} fields")

    def update_field_priorities(self, priorities: Dict[str, List[str]]) -> None:
        """Update priorities from settings save and fire change callback.

        Called by the settings dialog after the user saves. Both updates
        internal state and triggers cache invalidation via the registered
        callback.

        Args:
            priorities: Dict mapping field_name -> list of sources in priority order
        """
        self.set_field_priorities(priorities)
        if self._on_priorities_changed:
            self._on_priorities_changed()

    def set_on_priorities_changed(self, callback: Optional[Callable]) -> None:
        """Register a callback fired when priorities change.

        Args:
            callback: Callable with no arguments, or None to clear.
        """
        self._on_priorities_changed = callback

    def resolve_field_with_priorities(
        self,
        field_name: str,
        metadata_by_store: Dict[str, Dict[str, Any]],
        priorities: List[str],
    ) -> Tuple[Any, Optional[str]]:
        """Resolve using explicit priority list (for preview dialog).

        The singleton's internal priorities are NOT modified. This allows
        the preview dialog to show what-if results without side effects.

        Args:
            field_name: Name of the metadata field
            metadata_by_store: Dict mapping source_name -> metadata dict
            priorities: Explicit priority list to use instead of config

        Returns:
            Tuple of (resolved_value, source_name) or (None, None)
        """
        for source_name in priorities:
            if source_name not in metadata_by_store:
                continue
            value = self._get_field(metadata_by_store[source_name], field_name)
            if self._is_non_empty(value):
                return value, source_name
        return None, None

    def get_all_field_priorities(self) -> Dict[str, List[str]]:
        """Get all per-field priorities

        Returns:
            Dict mapping field_name -> list of sources in priority order
        """
        return {k: v.copy() for k, v in self._field_priorities.items()}

    def compute_priority_hash(self) -> str:
        """Compute a short hash of the current field priorities.

        Used to detect when the user changes priority settings so that
        on-demand enrichment can re-fetch with updated priorities.

        Returns:
            8-character hex hash of the priority configuration
        """
        # Deterministic serialization: sort keys for consistency
        serialized = json.dumps(self._field_priorities, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest()[:8]

    def reset_field_to_default(self, field_name: str) -> None:
        """Reset a single field to its default priority

        Uses plugin-built priorities if available, otherwise hardcoded fallback.

        Args:
            field_name: Name of the field to reset
        """
        defaults = self.get_effective_defaults()
        if field_name in defaults:
            self._field_priorities[field_name] = defaults[field_name].copy()
            self._user_overrides.discard(field_name)
            logger.debug(f"Reset field '{field_name}' to default priority")

    def reset_all_to_defaults(self) -> None:
        """Reset all field priorities to defaults

        Uses plugin-built priorities if available, otherwise hardcoded fallback.
        """
        defaults = self.get_effective_defaults()
        for field_name, default_priority in defaults.items():
            self._field_priorities[field_name] = default_priority.copy()
        self._user_overrides.clear()
        logger.debug("Reset all field priorities to defaults")

    def get_effective_defaults(self) -> Dict[str, List[str]]:
        """Get the factory-default field priorities (for "Reset to Defaults").

        Returns the seed defaults — the same values used to populate config
        on first run. Only used by the settings UI reset button.

        Returns:
            Dict mapping field_name -> list of sources in priority order
        """
        return {k: v.copy() for k, v in _SEED_FIELD_PRIORITIES.items()}

    def build_from_plugins(self, discovered_plugins: Dict[str, Any]) -> None:
        """Build dynamic capability data from plugin declarations.

        Reads ``provides_fields`` from each plugin's metadata and rebuilds
        the module-level ``FIELD_SOURCE_CAPABILITIES`` and ``SOURCE_LABELS``
        dicts in-place.  Does NOT modify priorities — those come exclusively
        from user config.

        Args:
            discovered_plugins: Dict mapping plugin_name -> PluginMetadata,
                                as returned by PluginManager.get_discovered_plugins()
        """
        if self._build_initialized:
            logger.warning(
                "build_from_plugins() called more than once — "
                "module-level FIELD_SOURCE_CAPABILITIES and SOURCE_LABELS "
                "will be re-initialized"
            )
        self._build_initialized = True

        # Collect all provides_fields declarations
        # field_name -> [(plugin_name, priority_number), ...]
        field_declarations: Dict[str, List[Tuple[str, int]]] = {}
        any_declared = False

        for plugin_name, pmeta in discovered_plugins.items():
            pf = getattr(pmeta, "provides_fields", None)
            if not pf:
                continue
            any_declared = True
            for field_name, field_info in pf.items():
                priority_num = field_info.get("priority", 50)
                if field_name not in field_declarations:
                    field_declarations[field_name] = []
                field_declarations[field_name].append((plugin_name, priority_num))

        if not any_declared:
            logger.debug("No plugins declare provides_fields, keeping fallbacks")
            return

        # --- Update SOURCE_LABELS from display_name ---
        new_labels: Dict[str, str] = {}
        for plugin_name, pmeta in discovered_plugins.items():
            display = getattr(pmeta, "display_name", None)
            if display:
                new_labels[plugin_name] = display
        # Merge: plugin declarations override defaults, keep defaults for the rest
        merged_labels = dict(_DEFAULT_SOURCE_LABELS)
        merged_labels.update(new_labels)
        SOURCE_LABELS.clear()
        SOURCE_LABELS.update(merged_labels)

        # --- Build FIELD_SOURCE_CAPABILITIES ---
        new_caps: Dict[str, List[str]] = {}
        for field_name, decls in field_declarations.items():
            # Sort by priority number ascending (lower = higher priority)
            decls.sort(key=lambda x: x[1])
            new_caps[field_name] = [name for name, _ in decls]
        # Merge with fallback for fields not declared by any plugin
        merged_caps = dict(_DEFAULT_FIELD_SOURCE_CAPABILITIES)
        merged_caps.update(new_caps)
        FIELD_SOURCE_CAPABILITIES.clear()
        FIELD_SOURCE_CAPABILITIES.update(merged_caps)

        logger.info(
            f"Built capabilities from {len(discovered_plugins)} plugins: "
            f"{len(field_declarations)} fields declared"
        )

    def prune_stale_priorities(self) -> bool:
        """Validate priorities against FIELD_SOURCE_CAPABILITIES.

        - Removes sources not in capabilities (plugin removed/renamed)
        - Adds sources in capabilities but missing from config (plugin added)
        - Returns True if any changes were made (caller should persist)
        """
        dirty = False
        for field_name, priority in list(self._field_priorities.items()):
            valid = set(FIELD_SOURCE_CAPABILITIES.get(field_name, []))
            if not valid:
                continue
            # Remove stale sources
            pruned = [s for s in priority if s in valid]
            # Add new sources (append at end, preserving user order)
            for src in FIELD_SOURCE_CAPABILITIES.get(field_name, []):
                if src not in pruned:
                    pruned.append(src)
            if pruned != priority:
                removed = set(priority) - set(pruned)
                added = set(pruned) - set(priority)
                if removed:
                    logger.info(
                        "Pruned stale sources from %s: %s", field_name, removed
                    )
                if added:
                    logger.info(
                        "Added new sources to %s: %s", field_name, added
                    )
                self._field_priorities[field_name] = pruned
                dirty = True
        return dirty

    def resolve_field_with_source(
        self,
        field_name: str,
        metadata_by_store: Dict[str, Dict[str, Any]]
    ) -> Tuple[Any, Optional[str]]:
        """Resolve a metadata field and return the source that provided it

        Uses per-field priority order.

        Args:
            field_name: Name of the metadata field
            metadata_by_store: Dict mapping source_name -> metadata dict

        Returns:
            Tuple of (resolved_value, source_name) or (None, None) if not found
        """
        priority = self.get_field_priority(field_name)

        for source_name in priority:
            if source_name not in metadata_by_store:
                continue

            source_metadata = metadata_by_store[source_name]
            value = self._get_field(source_metadata, field_name)

            if self._is_non_empty(value):
                return value, source_name

        # Empty priority (internal/merged fields): try all sources in dict order
        if not priority:
            for source_name, source_metadata in metadata_by_store.items():
                value = self._get_field(source_metadata, field_name)
                if self._is_non_empty(value):
                    return value, source_name
            return None, None

        # Check sources not in priority list (defensive — truly unknown only)
        # Skip known plugins (stores + metadata providers) — if they're not
        # in the field's priority list, the user deliberately excluded them.
        from luducat.core.plugin_manager import PluginManager
        known_sources = set(FIELD_SOURCE_CAPABILITIES.get(field_name, []))
        known_plugins = set(PluginManager.get_metadata_plugin_names())
        for source_name, source_metadata in metadata_by_store.items():
            if source_name in priority:
                continue
            if source_name in known_sources:
                continue  # Known source deliberately excluded from priority
            if source_name in known_plugins:
                continue  # Known plugin not declared for this field — skip
            value = self._get_field(source_metadata, field_name)
            if self._is_non_empty(value):
                logger.warning(
                    f"Using metadata from unknown source '{source_name}' for "
                    f"field '{field_name}' - add to field priority list"
                )
                return value, source_name

        return None, None

    def resolve_field(
        self,
        field_name: str,
        metadata_by_store: Dict[str, Dict[str, Any]]
    ) -> Any:
        """Resolve a metadata field from multiple stores

        Returns the first non-empty value following per-field priority order.

        Args:
            field_name: Name of the metadata field (e.g., "description", "series")
            metadata_by_store: Dict mapping store_name -> metadata dict

        Returns:
            The resolved value, or None if no store has this field
        """
        priority = self.get_field_priority(field_name)

        for source_name in priority:
            if source_name not in metadata_by_store:
                continue

            source_metadata = metadata_by_store[source_name]
            value = self._get_field(source_metadata, field_name)

            if self._is_non_empty(value):
                return value

        # Empty priority (internal/merged fields): try all sources in dict order
        if not priority:
            for source_name, source_metadata in metadata_by_store.items():
                value = self._get_field(source_metadata, field_name)
                if self._is_non_empty(value):
                    return value
            return None

        # Check sources not in priority list (defensive — truly unknown only)
        # Skip known plugins (stores + metadata providers) — if they're not
        # in the field's priority list, the user deliberately excluded them.
        from luducat.core.plugin_manager import PluginManager
        known_sources = set(FIELD_SOURCE_CAPABILITIES.get(field_name, []))
        known_plugins = set(PluginManager.get_metadata_plugin_names())
        for source_name, source_metadata in metadata_by_store.items():
            if source_name in priority:
                continue
            if source_name in known_sources:
                continue  # Known source deliberately excluded from priority
            if source_name in known_plugins:
                continue  # Known plugin not declared for this field — skip
            value = self._get_field(source_metadata, field_name)
            if self._is_non_empty(value):
                logger.warning(
                    f"Using metadata from unknown source '{source_name}' for "
                    f"field '{field_name}' - add to field priority list"
                )
                return value

        return None

    def resolve_game_metadata(
        self,
        metadata_by_store: Dict[str, Dict[str, Any]],
        fields: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Resolve all metadata fields for a game

        Args:
            metadata_by_store: Dict mapping store_name -> metadata dict
            fields: List of field names to resolve. If None, resolves all
                    fields found in any store's metadata.

        Returns:
            Dict with resolved values for each field
        """
        if not metadata_by_store:
            return {}

        # Determine which fields to resolve
        if fields is None:
            fields = set()
            for store_metadata in metadata_by_store.values():
                fields.update(store_metadata.keys())
            fields = list(fields)

        # Resolve each field
        result = {}
        for field_name in fields:
            # Merged fields use special merge logic, not priority resolution
            if field_name == "release_date":
                merged_dates = self._merge_release_dates(metadata_by_store)
                if merged_dates:
                    result["release_date"] = merged_dates
                continue
            value = self.resolve_field(field_name, metadata_by_store)
            if value is not None:
                result[field_name] = value

        return result

    def _merge_release_dates(
        self,
        metadata_by_store: Dict[str, Dict[str, Any]]
    ) -> Dict[str, str]:
        """Merge release_date dicts from all sources, keeping oldest per platform.

        Uses user-configured release_date priority for source ordering.
        Sources NOT in the priority list are excluded entirely.
        Handles both new dict format and old string format for backward compat.

        Args:
            metadata_by_store: Dict mapping source_name -> metadata dict

        Returns:
            Dict mapping platform_name -> "YYYY-MM-DD" string, or empty dict
        """
        merged: Dict[str, str] = {}
        # Use user-configured priority (read directly — get_field_priority()
        # returns [] for merged fields by design)
        canonical = self._PRIORITY_FIELD_ALIASES.get(
            "release_date", "release_date"
        )
        source_order = self._field_priorities.get(canonical, [])
        for source in source_order:
            if source not in metadata_by_store:
                continue
            dates = metadata_by_store[source].get("release_date")
            if isinstance(dates, dict):
                for platform, date_str in dates.items():
                    if not date_str or not isinstance(date_str, str) or len(date_str) < 4:
                        continue
                    if platform not in merged or date_str < merged[platform]:
                        merged[platform] = date_str
            elif isinstance(dates, str) and dates:
                # Backward compat: old string format — use as "windows" fallback
                if "windows" not in merged:
                    merged["windows"] = dates
        return merged

    def get_field_source(
        self,
        field_name: str,
        metadata_by_store: Dict[str, Dict[str, Any]]
    ) -> Optional[str]:
        """Get which source provided the value for a field

        Uses per-field priority order.
        Useful for debugging and UI display ("Description from: Steam")

        Args:
            field_name: Name of the metadata field
            metadata_by_store: Dict mapping source_name -> metadata dict

        Returns:
            Source name that provided the value, or None if not found
        """
        priority = self.get_field_priority(field_name)

        for source_name in priority:
            if source_name not in metadata_by_store:
                continue

            source_metadata = metadata_by_store[source_name]
            value = self._get_field(source_metadata, field_name)

            if self._is_non_empty(value):
                return source_name

        return None

    @staticmethod
    def _is_non_empty(value: Any) -> bool:
        """Check if a value is non-empty

        Handles None, empty strings, empty lists, etc.
        """
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, (list, dict)) and len(value) == 0:
            return False
        return True

    # Old storage names → canonical names for _get_field backward compat
    _STORAGE_ALIASES: Dict[str, str] = {
        "cover_url": "cover",
        "background_url": "hero",
        "websites": "links",
        "categories": "features",
        "franchises": "franchise",
        "languages": "supported_languages",
    }
    # Reverse: canonical → old storage name (for metadata dicts that haven't been re-synced)
    _CANONICAL_TO_OLD: Dict[str, str] = {v: k for k, v in _STORAGE_ALIASES.items()}

    @staticmethod
    def _get_field(
        metadata: Dict[str, Any],
        field_name: str,
    ) -> Any:
        """Get field value from metadata dict.

        Checks canonical name first, then falls back to old storage name
        for backward compatibility with pre-normalization metadata_json.

        Args:
            metadata: Metadata dict to search
            field_name: Canonical field name to look for

        Returns:
            Field value or None if not found
        """
        value = metadata.get(field_name)
        if value is not None:
            return value
        # Try old storage name for backward compat
        old_name = MetadataResolver._CANONICAL_TO_OLD.get(field_name)
        if old_name:
            return metadata.get(old_name)
        # Try canonical name if we were given an old storage name
        canonical = MetadataResolver._STORAGE_ALIASES.get(field_name)
        if canonical:
            return metadata.get(canonical)
        return None

    def _is_plugin_usable(self, plugin_name: str) -> bool:
        """Check if a plugin is usable (enabled + available/authenticated)

        Args:
            plugin_name: Name of the plugin to check

        Returns:
            True if plugin is enabled and has valid authentication
        """
        if not self._plugin_manager:
            return False

        # get_plugin returns None if plugin is disabled
        plugin = self._plugin_manager.get_plugin(plugin_name)
        if not plugin:
            return False

        # Store plugins: authentication means plugin DB has data (from sync).
        # Client installation is irrelevant for reading the local plugin DB.
        from luducat.plugins.base import StorePlugin
        if isinstance(plugin, StorePlugin):
            return plugin.is_authenticated()

        # Metadata/enrichment plugins: is_available() is the right check
        if not plugin.is_available():
            return False

        return True

    def _query_plugin_for_game(
        self,
        plugin,
        store_app_ids: Dict[str, str],
        normalized_title: str,
    ) -> Optional[Dict[str, Any]]:
        """Query a plugin for game metadata using the uniform interface.

        Iterates store_app_ids, calls get_metadata_for_store_game() for each.
        Returns first non-None result.  Works identically for store plugins
        and enrichment plugins — the resolver is plugin-type-agnostic.

        Args:
            plugin: Plugin instance (store or enrichment)
            store_app_ids: Dict mapping store_name -> app_id
            normalized_title: Normalized game title for fallback search

        Returns:
            Metadata dict or None
        """
        if not hasattr(plugin, "get_metadata_for_store_game"):
            return None
        for store_name, app_id in store_app_ids.items():
            if not app_id:
                continue
            metadata = plugin.get_metadata_for_store_game(
                store_name, app_id, normalized_title=normalized_title
            )
            if metadata:
                return metadata
        return None

    # Key fields that should be present for metadata to be considered "complete"
    ESSENTIAL_FIELDS = ["cover", "release_date", "developers", "publishers"]

    def _is_metadata_complete(self, metadata: Dict[str, Any]) -> bool:
        """Check if metadata has all essential fields populated.

        Args:
            metadata: Metadata dict to check

        Returns:
            True if all essential fields are present and non-empty
        """
        for field in self.ESSENTIAL_FIELDS:
            value = metadata.get(field)
            # Backward compat: check old storage name too
            if not self._is_non_empty(value):
                old_name = {"cover": "cover_url"}.get(field)
                if old_name:
                    value = metadata.get(old_name)
                if not self._is_non_empty(value):
                    return False
        return True

    def _is_offline(self) -> bool:
        """Check if the app is in offline mode."""
        try:
            from .network_monitor import get_network_monitor
            return not get_network_monitor().is_online
        except RuntimeError:
            return False  # Monitor not initialized, assume online

    def _resolve_cross_store_app_id(
        self,
        target_store: str,
        store_app_ids: Dict[str, str],
        normalized_title: str,
    ) -> Optional[str]:
        """Resolve the app_id for target_store via cross-reference plugins.

        Plugin databases are license-agnostic caches. When a game is owned on
        one store (e.g., Epic) but a higher-priority store (e.g., Steam) exists
        in the priority list, this method uses IGDB/PCGW cross-references to
        find the target store's app_id so its plugin DB can be queried.

        Tries IGDB external_ids first (bidirectional, all stores), then PCGW
        cross-store fields (Steam/GOG targets only). Validates matches with
        title similarity (>=0.6 threshold).

        Args:
            target_store: Store to resolve to (e.g., "steam")
            store_app_ids: Dict of source_store -> app_id for this game
            normalized_title: Normalized game title for validation

        Returns:
            Resolved app_id for target_store, or None
        """
        if not self._plugin_manager:
            return None

        cross_ref_plugins = ["igdb", "pcgamingwiki"]

        for xref_name in cross_ref_plugins:
            if not self._is_plugin_usable(xref_name):
                continue
            plugin = self._plugin_manager.get_plugin(xref_name)
            if not plugin or not hasattr(plugin, "resolve_cross_store_id"):
                continue

            for source_store, source_app_id in store_app_ids.items():
                try:
                    target_id, ref_title = plugin.resolve_cross_store_id(
                        source_store, source_app_id, target_store, normalized_title
                    )
                except Exception as e:
                    logger.debug(
                        f"Cross-ref {xref_name} failed for "
                        f"{source_store}:{source_app_id} → {target_store}: {e}"
                    )
                    continue

                if not target_id:
                    continue

                # Validate with title similarity
                if normalized_title and ref_title:
                    from .database import normalize_title
                    sim = _title_similarity(
                        normalized_title, normalize_title(ref_title)
                    )
                    if sim < 0.6:
                        logger.debug(
                            f"Cross-ref {xref_name}: "
                            f"{source_store}:{source_app_id} → "
                            f"{target_store}:{target_id} rejected "
                            f"(similarity {sim:.2f})"
                        )
                        continue

                logger.info(
                    f"Cross-ref {xref_name}: resolved {target_store} "
                    f"app_id {target_id} for '{normalized_title}'"
                )
                return target_id

        return None

    def fetch_metadata_for_game(
        self,
        store_app_ids: Dict[str, str],
        normalized_title: str = "",
        field: Optional[str] = None,
        _return_sources: bool = False,
    ) -> Union[Optional[Dict[str, Any]], Tuple[Optional[Dict[str, Any]], Dict[str, str]]]:
        """Fetch metadata on-demand from plugins in priority order.

        Uses the combined priority order so per-field priorities are respected
        during the final resolve_game_metadata() merge.  Each plugin must be
        enabled and authenticated to be used.

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups
            field: Optional specific field to check (returns early if found)
            _return_sources: If True, return (merged_metadata, source_map) where
                source_map maps field_name -> source_name. Private kwarg for
                on-demand enrichment.

        Returns:
            When _return_sources=False (default):
                Dict with merged metadata from all sources,
                or None if no usable plugin has metadata
            When _return_sources=True:
                Tuple of (merged_metadata_or_None, field_source_map)
        """
        _none_result = (None, {}) if _return_sources else None

        if not self._plugin_manager:
            logger.debug("No plugin manager set, cannot fetch metadata on-demand")
            return _none_result

        if self._is_offline():
            logger.debug("Skipping fetch_metadata_for_game — offline")
            return _none_result

        # Use field-specific priority when looking for a single field,
        # otherwise all unique sources for a full gather.
        priority = (
            self.get_field_priority(field) if field
            else self._get_all_priority_sources()
        )

        # Collect metadata from all sources
        metadata_by_source: Dict[str, Dict[str, Any]] = {}

        for plugin_name in priority:
            if plugin_name in metadata_by_source:
                continue  # Already gathered from this source
            if not self._is_plugin_usable(plugin_name):
                continue

            try:
                plugin = self._plugin_manager.get_plugin(plugin_name)
                if not plugin:
                    continue

                metadata = self._query_plugin_for_game(
                    plugin, store_app_ids, normalized_title
                )

                if metadata:
                    # Single-field mode: return immediately if the field is found
                    if field and self._is_non_empty(metadata.get(field)):
                        logger.debug(f"Got {field} from {plugin_name}")
                        if _return_sources:
                            return metadata, {field: plugin_name}
                        return metadata

                    metadata_by_source[plugin_name] = metadata
                    logger.debug(f"Got metadata from {plugin_name}")

            except Exception as e:
                logger.debug(f"Failed to get metadata from {plugin_name}: {e}")
                continue

        # Merge metadata from all sources using priority resolution
        if metadata_by_source:
            if _return_sources:
                # Use resolve_field_with_source for each field to build source map
                all_fields: set = set()
                for source_meta in metadata_by_source.values():
                    all_fields.update(source_meta.keys())

                merged = {}
                source_map: Dict[str, str] = {}
                for field_name in all_fields:
                    value, source = self.resolve_field_with_source(
                        field_name, metadata_by_source
                    )
                    if value is not None:
                        merged[field_name] = value
                    if source:
                        source_map[field_name] = source

                return (merged if merged else None, source_map)

            merged = self.resolve_game_metadata(metadata_by_source)
            return merged if merged else None

        return _none_result

    def get_description_on_demand(
        self,
        store_app_ids: Dict[str, str],
        normalized_title: str = ""
    ) -> str:
        """Get description on-demand from plugins in priority order

        Convenience method that fetches just the description field.

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Description string or empty string if not found
        """
        if not self._plugin_manager:
            return ""

        if self._is_offline():
            logger.debug("Skipping get_description_on_demand — offline")
            return ""

        # Use per-field priority instead of hardcoded store/metadata order
        priority = self.get_field_priority("description")

        for plugin_name in priority:
            if not self._is_plugin_usable(plugin_name):
                continue

            plugin = self._plugin_manager.get_plugin(plugin_name)
            if not plugin:
                continue

            try:
                metadata = self._query_plugin_for_game(
                    plugin, store_app_ids, normalized_title
                )
                if metadata:
                    desc = metadata.get("description", "")
                    if desc and desc.strip():
                        logger.debug(f"Got description from {plugin_name}")
                        return desc
            except Exception as e:
                logger.debug(f"Failed to get description from {plugin_name}: {e}")
                continue

        return ""

    def get_screenshots_on_demand(
        self,
        store_app_ids: Dict[str, str],
        normalized_title: str = "",
        exclude_sources: Optional[List[str]] = None,
    ) -> Tuple[List[str], str]:
        """Get screenshots on-demand from plugins in per-field priority order.

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups
            exclude_sources: Sources to skip (e.g. source that returned 404)

        Returns:
            Tuple of (screenshot URLs, source_name) or ([], "") if not found
        """
        if not self._plugin_manager:
            return [], ""

        if self._is_offline():
            logger.debug("Skipping get_screenshots_on_demand — offline")
            return [], ""

        skip = set(exclude_sources) if exclude_sources else set()
        priority = self.get_field_priority("screenshots")

        for plugin_name in priority:
            if plugin_name in skip:
                continue
            if not self._is_plugin_usable(plugin_name):
                continue
            try:
                plugin = self._plugin_manager.get_plugin(plugin_name)
                if not plugin:
                    continue

                metadata = self._query_plugin_for_game(
                    plugin, store_app_ids, normalized_title
                )
                if metadata:
                    screenshots = metadata.get("screenshots", [])
                    if screenshots:
                        logger.debug(f"Got screenshots from {plugin_name}")
                        return screenshots, plugin_name
            except Exception as e:
                logger.debug(f"get_screenshots_on_demand: Failed from {plugin_name}: {e}")
                continue

        return [], ""

    def get_cover_on_demand(
        self,
        store_app_ids: Dict[str, str],
        normalized_title: str = ""
    ) -> Tuple[str, str]:
        """Get cover image URL on-demand from plugins in per-field priority order.

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Tuple of (cover_url, source_name) or ("", "") if not found
        """
        if not self._plugin_manager:
            return "", ""

        if self._is_offline():
            logger.debug("Skipping get_cover_on_demand — offline")
            return "", ""

        priority = self.get_field_priority("cover")

        for plugin_name in priority:
            if not self._is_plugin_usable(plugin_name):
                continue
            try:
                plugin = self._plugin_manager.get_plugin(plugin_name)
                if not plugin:
                    continue

                metadata = self._query_plugin_for_game(
                    plugin, store_app_ids, normalized_title
                )
                if metadata:
                    cover = metadata.get("cover") or metadata.get("cover_url")
                    if cover and cover.strip():
                        logger.info(f"get_cover_on_demand: Got cover from {plugin_name}")
                        return cover, plugin_name
            except Exception as e:
                logger.debug(f"get_cover_on_demand: Failed from {plugin_name}: {e}")
                continue

        return "", ""

    def get_game_modes_bulk(
        self,
        store_app_ids: Dict[str, List[str]]
    ) -> Dict[str, Dict[str, List[str]]]:
        """Get game modes for multiple games from all enabled metadata plugins

        Queries each enabled metadata plugin that supports game modes and
        aggregates results. This is used at startup to populate game mode
        badges without making per-game API calls.

        Args:
            store_app_ids: Dict mapping store_name -> list of app_ids
                          e.g. {"steam": ["440", "730"], "gog": ["1234"]}

        Returns:
            Nested dict: {store_name: {app_id: [game_mode_names]}}
            e.g. {"steam": {"440": ["Single player", "Multiplayer"]}}
            Returns empty dict if no metadata plugins are available.
        """
        if not self._plugin_manager:
            logger.debug("No plugin manager set, cannot fetch game modes")
            return {}

        result: Dict[str, Dict[str, List[str]]] = {}

        # Query each metadata plugin that supports bulk game modes
        # Uses per-field priority for game_modes_detail
        for plugin_name in self.get_field_priority("game_modes_detail"):
            if not self._is_plugin_usable(plugin_name):
                continue

            try:
                plugin = self._plugin_manager.get_plugin(plugin_name)

                # Check if plugin supports bulk game modes query
                if not hasattr(plugin, "get_game_modes_bulk"):
                    continue

                plugin_modes = plugin.get_game_modes_bulk(store_app_ids)

                # Merge results (first plugin wins for each game)
                for store_name, app_modes in plugin_modes.items():
                    if store_name not in result:
                        result[store_name] = {}
                    for app_id, modes in app_modes.items():
                        if app_id not in result[store_name]:
                            result[store_name][app_id] = modes

                logger.debug(
                    f"Got game modes from metadata plugin: {plugin_name}"
                )
            except Exception as e:
                logger.warning(f"Failed to get game modes from {plugin_name}: {e}")
                continue

        return result

    def has_game_modes_support(self) -> bool:
        """Check if any enabled metadata plugin supports game modes

        Returns:
            True if at least one metadata plugin is usable and supports game modes
        """
        if not self._plugin_manager:
            return False

        for plugin_name in self.get_field_priority("game_modes_detail"):
            if not self._is_plugin_usable(plugin_name):
                continue

            plugin = self._plugin_manager.get_plugin(plugin_name)
            if plugin and hasattr(plugin, "get_game_modes_bulk"):
                return True

        return False

    def get_enrichment_bulk_for_cache(
        self,
        store_app_ids: Dict[str, List[str]],
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Query enrichment plugins' local DBs for cache build.

        Iterates all metadata-typed plugins (not stores) that implement
        get_cache_metadata_bulk(). Each plugin is guarded by
        _is_plugin_usable(). Returns data keyed by provider name, then
        app_id, then metadata dict.

        The store_app_ids structure flows through to plugins so they can
        filter to only the user's owned games.

        Args:
            store_app_ids: Dict mapping store_name -> list of app_ids
            progress_callback: Optional callback(display_name) called
                before each plugin query for UI progress updates

        Returns:
            {provider_name: {app_id: metadata_dict}}
            app_ids are flattened across stores (same app_id from different
            stores gets merged, last write wins — unlikely in practice).
        """
        if not self._plugin_manager:
            return {}

        from luducat.core.plugin_manager import PluginManager
        enrichment_names = PluginManager.get_enrichment_plugin_names()

        # Collect usable enrichment plugins
        usable_plugins = []
        for plugin_name in enrichment_names:
            if not self._is_plugin_usable(plugin_name):
                continue
            plugin = self._plugin_manager.get_plugin(plugin_name)
            if plugin and hasattr(plugin, "get_cache_metadata_bulk"):
                usable_plugins.append((plugin_name, plugin))

        if not usable_plugins:
            return {}

        if progress_callback:
            names = ", ".join(
                PluginManager.get_store_display_name(n)
                for n, _ in usable_plugins
            )
            progress_callback(names)

        result: Dict[str, Dict[str, Dict[str, Any]]] = {}

        # Each enrichment plugin has its own DB — safe to parallelize
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_enrichment(plugin_name, plugin):
            plugin_data = plugin.get_cache_metadata_bulk(store_app_ids)
            flat: Dict[str, Dict[str, Any]] = {}
            for store_entries in plugin_data.values():
                for app_id, meta in store_entries.items():
                    if app_id not in flat:
                        flat[app_id] = meta
                    else:
                        for k, v in meta.items():
                            flat[app_id].setdefault(k, v)
            return plugin_name, flat

        with ThreadPoolExecutor(max_workers=max(1, len(usable_plugins))) as executor:
            futures = {
                executor.submit(_fetch_enrichment, pn, p): pn
                for pn, p in usable_plugins
            }
            for future in as_completed(futures):
                pn = futures[future]
                try:
                    plugin_name, flat = future.result()
                    if flat:
                        result[plugin_name] = flat
                        logger.info(
                            f"Enrichment bulk from {plugin_name}: "
                            f"{len(flat)} games"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to get bulk cache metadata from {pn}: {e}"
                    )

        return result

    # =========================================================================
    # CROSS-STORE ID RESOLUTION
    # =========================================================================

    def resolve_steam_app_ids(
        self,
        store_name: str,
        games: List["PluginGame"],
    ) -> Dict[str, str]:
        """Resolve Steam AppIDs for non-Steam games via metadata plugins.

        Queries PCGW first (no auth, always available), then IGDB for
        remaining unresolved games. Performs title similarity verification
        to filter out false matches.

        This is a pure local operation — no API calls, just database lookups.

        Args:
            store_name: Source store ("gog", "epic")
            games: List of PluginGame objects to resolve

        Returns:
            Dict mapping store_app_id -> steam_app_id
            Only includes verified matches.
        """
        from .database import normalize_title

        if store_name == "steam" or not games:
            return {}

        # Build lookup: app_id -> normalized title
        app_id_titles: Dict[str, str] = {}
        for game in games:
            app_id_titles[game.store_app_id] = normalize_title(game.title)

        app_ids = list(app_id_titles.keys())
        result: Dict[str, str] = {}

        # Try PCGW first (no auth needed, always available)
        pcgw_count = 0
        if self._is_plugin_usable("pcgamingwiki"):
            try:
                pcgw_plugin = self._plugin_manager.get_plugin("pcgamingwiki")
                if pcgw_plugin and hasattr(pcgw_plugin, "resolve_steam_app_ids"):
                    raw = pcgw_plugin.resolve_steam_app_ids(store_name, app_ids)
                    for store_id, (steam_id, ref_title) in raw.items():
                        game_title = app_id_titles.get(store_id, "")
                        ref_normalized = normalize_title(ref_title) if ref_title else ""
                        if game_title and ref_normalized:
                            if _title_similarity(game_title, ref_normalized) >= 0.6:
                                result[store_id] = steam_id
                                pcgw_count += 1
                            else:
                                logger.debug(
                                    f"PCGW cross-store match rejected: "
                                    f"'{game_title}' vs '{ref_normalized}'"
                                )
                        else:
                            # No title to verify — accept ID-based match
                            result[store_id] = steam_id
                            pcgw_count += 1
            except Exception as e:
                logger.warning(f"PCGW cross-store resolution failed: {e}")

        # Try IGDB for remaining (requires auth)
        remaining_ids = [aid for aid in app_ids if aid not in result]
        igdb_count = 0
        if remaining_ids and self._is_plugin_usable("igdb"):
            try:
                igdb_plugin = self._plugin_manager.get_plugin("igdb")
                if igdb_plugin and hasattr(igdb_plugin, "resolve_steam_app_ids"):
                    raw = igdb_plugin.resolve_steam_app_ids(store_name, remaining_ids)
                    for store_id, (steam_id, ref_title) in raw.items():
                        game_title = app_id_titles.get(store_id, "")
                        ref_normalized = normalize_title(ref_title) if ref_title else ""
                        if game_title and ref_normalized:
                            if _title_similarity(game_title, ref_normalized) >= 0.6:
                                result[store_id] = steam_id
                                igdb_count += 1
                            else:
                                logger.debug(
                                    f"IGDB cross-store match rejected: "
                                    f"'{game_title}' vs '{ref_normalized}'"
                                )
                        else:
                            result[store_id] = steam_id
                            igdb_count += 1
            except Exception as e:
                logger.warning(f"IGDB cross-store resolution failed: {e}")

        if result:
            logger.info(
                f"Cross-store Steam ID resolution for {store_name}: "
                f"{len(result)}/{len(app_ids)} resolved "
                f"(PCGW: {pcgw_count}, IGDB: {igdb_count})"
            )

        return result

    # =========================================================================
    # PRIORITY CONFORMANCE CHECKING
    # =========================================================================

    # Fields that should be checked for priority conformance (media fields)
    MEDIA_FIELDS = ["cover", "hero", "screenshots"]

    def check_priority_conformance(
        self,
        metadata: Dict[str, Any],
        store_app_ids: Dict[str, str],
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Tuple[str, str]]:
        """Check if cached metadata sources match current priority settings

        For each field, verifies that the current source (from _sources dict)
        is the highest-priority source that CAN provide data for this game.

        Args:
            metadata: Current metadata dict (must have _sources for tracking)
            store_app_ids: Dict mapping store_name -> app_id for this game
            fields: Fields to check. Defaults to MEDIA_FIELDS (cover, hero, screenshots)

        Returns:
            Dict mapping field_name -> (current_source, expected_source) for
            fields that don't conform. Empty dict if all fields conform.
        """
        if not self._plugin_manager:
            return {}

        from .enrichment_state import get_sources as _get_sources

        fields = fields or self.MEDIA_FIELDS
        sources = _get_sources(metadata)
        non_conforming: Dict[str, Tuple[str, str]] = {}

        for field_name in fields:
            current_source = sources.get(field_name, "")
            if not current_source:
                # No tracked source - can't check conformance
                continue

            # Get priority list for this field
            priority = self.get_field_priority(field_name)

            # Find the highest-priority usable source that can provide this field
            expected_source = self._find_highest_priority_source(
                field_name, priority, store_app_ids
            )

            if expected_source and expected_source != current_source:
                # Current source doesn't match expected highest priority
                current_rank = self.get_field_priority_rank(field_name, current_source)
                expected_rank = self.get_field_priority_rank(field_name, expected_source)

                if expected_rank < current_rank:
                    # A higher priority source is available
                    non_conforming[field_name] = (current_source, expected_source)
                    logger.debug(
                        f"Field '{field_name}' non-conforming: "
                        f"current={current_source} (rank {current_rank}), "
                        f"expected={expected_source} (rank {expected_rank})"
                    )

        return non_conforming

    def _find_highest_priority_source(
        self,
        field_name: str,
        priority: List[str],
        store_app_ids: Dict[str, str],
    ) -> Optional[str]:
        """Find the highest-priority usable source for a field

        Args:
            field_name: Name of the metadata field
            priority: Priority list for this field
            store_app_ids: Dict mapping store_name -> app_id

        Returns:
            Name of highest-priority usable source, or None if none available
        """
        # Get sources that can provide this field
        capable_sources = FIELD_SOURCE_CAPABILITIES.get(field_name, [])

        for source_name in priority:
            # Must be in capability list
            if source_name not in capable_sources:
                continue

            # Must be usable (enabled + authenticated)
            if not self._is_plugin_usable(source_name):
                continue

            # This source is usable for this field
            return source_name

        return None

    def fetch_field_from_priority_source(
        self,
        field_name: str,
        store_app_ids: Dict[str, str],
        normalized_title: str = "",
    ) -> Tuple[Any, Optional[str]]:
        """Fetch a field from the highest-priority source

        Queries sources in priority order until data is found.

        Args:
            field_name: Name of the field to fetch
            store_app_ids: Dict mapping store_name -> app_id
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Tuple of (value, source_name) or (None, None) if not found
        """
        if not self._plugin_manager:
            return None, None

        if self._is_offline():
            logger.debug("Skipping fetch_field_from_priority_source — offline")
            return None, None

        priority = self.get_field_priority(field_name)
        capable_sources = FIELD_SOURCE_CAPABILITIES.get(field_name, [])

        # Canonical field names — try canonical first, then old storage name
        _FIELD_FALLBACKS = {
            "cover": "cover_url",
            "hero": "background_url",
        }
        metadata_key = field_name
        fallback_key = _FIELD_FALLBACKS.get(field_name)

        for source_name in priority:
            if source_name not in capable_sources:
                continue

            if not self._is_plugin_usable(source_name):
                continue

            try:
                plugin = self._plugin_manager.get_plugin(source_name)
                if not plugin:
                    continue

                metadata = self._query_plugin_for_game(
                    plugin, store_app_ids, normalized_title
                )

                if metadata:
                    value = metadata.get(metadata_key)
                    # Backward compat: try old storage name if canonical not found
                    if not self._is_non_empty(value) and fallback_key:
                        value = metadata.get(fallback_key)

                    if self._is_non_empty(value):
                        logger.debug(
                            f"Fetched '{field_name}' from priority source: {source_name}"
                        )
                        return value, source_name

            except Exception as e:
                logger.debug(
                    f"Failed to fetch '{field_name}' from {source_name}: {e}"
                )
                continue

        return None, None

    def refresh_non_conforming_fields(
        self,
        metadata: Dict[str, Any],
        store_app_ids: Dict[str, str],
        normalized_title: str = "",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Tuple[Any, str]]:
        """Refresh fields that don't conform to current priority settings

        Checks each field for priority conformance and fetches fresh data
        from the correct priority source if needed.

        Args:
            metadata: Current metadata dict with _sources tracking
            store_app_ids: Dict mapping store_name -> app_id
            normalized_title: Normalized game title for fallback lookups
            fields: Fields to check/refresh. Defaults to MEDIA_FIELDS

        Returns:
            Dict mapping field_name -> (new_value, new_source) for refreshed fields
        """
        non_conforming = self.check_priority_conformance(
            metadata, store_app_ids, fields
        )

        refreshed: Dict[str, Tuple[Any, str]] = {}

        for field_name, (current, expected) in non_conforming.items():
            value, source = self.fetch_field_from_priority_source(
                field_name, store_app_ids, normalized_title
            )
            if value is not None and source:
                refreshed[field_name] = (value, source)
                logger.info(
                    f"Refreshed '{field_name}': {current} -> {source}"
                )

        return refreshed

    # =========================================================================
    # BATCH OPERATIONS (for startup and sync)
    # =========================================================================

    def get_enrichment_for_cache(
        self,
        session,
        fields: List[str],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Bulk-extract enrichment-sourced fields from metadata_json.

        Reads _sources markers to attribute fields to their enrichment
        provider (igdb, pcgamingwiki, etc.).  Only includes providers
        that are currently enabled and usable.

        Uses SQL json_extract() to efficiently pull requested fields
        without loading full metadata_json blobs.

        Args:
            session: SQLAlchemy session for database queries
            fields: List of field names to extract (e.g. ["release_date", "genres"])

        Returns:
            Dict[game_id, Dict[provider_name, Dict[field_name, value]]]
        """
        if not fields:
            return {}

        # Validate field names to prevent SQL injection via json_extract paths.
        # Callers pass FIELD_SOURCE_CAPABILITIES keys (internal constants), but
        # defense-in-depth ensures only safe identifiers reach the SQL string.
        _SAFE_FIELD_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
        for field_name in fields:
            if not _SAFE_FIELD_RE.match(field_name):
                raise ValueError(f"Invalid field name for SQL query: {field_name!r}")

        from sqlalchemy import text as sa_text

        # Mapping: some fields have different names for data vs _sources marker.
        # EnrichmentService marks cover as _sources.cover, but data lives in
        # metadata.cover_url. Same for hero/background_url. On-demand enrichment
        # marks as _sources.cover_url. Use COALESCE to check both conventions.
        _SOURCE_ALIASES: Dict[str, str] = {
            "cover_url": "cover",
            "cover": "cover_url",
            "background_url": "hero",
            "hero": "background_url",
        }

        # Build SQL json_extract expressions for each requested field + its source
        select_parts = ["game_id"]
        for field_name in fields:
            alias = _SOURCE_ALIASES.get(field_name)
            if alias:
                # Data: check both canonical name and alias storage key
                # (EnrichmentService stores cover data as cover_url, hero as
                # background_url, but priority fields use cover/hero)
                select_parts.append(
                    f"COALESCE("
                    f"json_extract(metadata_json, '$.{field_name}'), "
                    f"json_extract(metadata_json, '$.{alias}')"
                    f") AS f_{field_name}"
                )
                # Source: check both conventions (already correct)
                select_parts.append(
                    f"COALESCE("
                    f"json_extract(metadata_json, '$._sources.{field_name}'), "
                    f"json_extract(metadata_json, '$._sources.{alias}')"
                    f") AS s_{field_name}"
                )
            else:
                select_parts.append(
                    f"json_extract(metadata_json, '$.{field_name}') AS f_{field_name}"
                )
                select_parts.append(
                    f"json_extract(metadata_json, '$._sources.{field_name}') AS s_{field_name}"
                )

        # We only care about rows where at least one source is tracked
        where_clauses = []
        for field_name in fields:
            alias = _SOURCE_ALIASES.get(field_name)
            if alias:
                where_clauses.append(
                    f"(json_extract(metadata_json, '$._sources.{field_name}') IS NOT NULL"
                    f" OR json_extract(metadata_json, '$._sources.{alias}') IS NOT NULL)"
                )
            else:
                where_clauses.append(
                    f"json_extract(metadata_json, '$._sources.{field_name}') IS NOT NULL"
                )

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM store_games "
            f"WHERE metadata_json IS NOT NULL "
            f"AND ({' OR '.join(where_clauses)})"
        )

        try:
            rows = session.execute(sa_text(sql)).fetchall()
        except Exception as e:
            logger.warning(f"Failed to query enrichment data from metadata_json: {e}")
            return {}

        # Determine which providers are currently usable
        usable_cache: Dict[str, bool] = {}

        def is_usable(provider: str) -> bool:
            if provider not in usable_cache:
                usable_cache[provider] = self._is_plugin_usable(provider)
            return usable_cache[provider]

        # Build result: game_id -> provider -> field -> value
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for row in rows:
            game_id = row[0]
            col_idx = 1  # skip game_id

            for field_name in fields:
                value = row[col_idx]
                source = row[col_idx + 1]
                col_idx += 2

                if source is None or value is None:
                    continue

                # Only include enrichment providers (not store plugins)
                if self._is_store_plugin(source):
                    continue

                if not is_usable(source):
                    continue

                # Parse JSON arrays/objects returned as strings by json_extract
                if isinstance(value, str) and value[:1] in ('[', '{'):
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, ValueError):
                        pass

                if not self._is_non_empty(value):
                    continue

                if game_id not in result:
                    result[game_id] = {}
                if source not in result[game_id]:
                    result[game_id][source] = {}
                result[game_id][source][field_name] = value

        return result

    def get_metadata_bulk(
        self,
        store_name: str,
        app_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games from a store plugin

        This is the ONLY way GameService should get bulk metadata from stores.

        Args:
            store_name: Name of the store plugin
            app_ids: List of app IDs to fetch

        Returns:
            Dict mapping app_id -> metadata dict
        """
        if not self._plugin_manager:
            return {}

        if not self._is_plugin_usable(store_name):
            logger.debug(f"Store plugin {store_name} not usable for bulk fetch")
            return {}

        plugin = self._plugin_manager.get_plugin(store_name)
        if not plugin:
            return {}

        try:
            if hasattr(plugin, "get_games_metadata_bulk"):
                return plugin.get_games_metadata_bulk(app_ids)
            else:
                # Fallback to individual fetches
                result = {}
                for app_id in app_ids:
                    metadata = plugin.get_game_metadata(app_id)
                    if metadata:
                        result[app_id] = metadata
                return result
        except Exception as e:
            logger.warning(f"Bulk metadata fetch failed for {store_name}: {e}")
            return {}

    async def enrich_games_batch(
        self,
        store_name: str,
        games: List["PluginGame"],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        metadata_started_callback: Optional[Callable[[str], None]] = None,
        metadata_finished_callback: Optional[Callable[[str], None]] = None,
        metadata_skipped_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, "EnrichmentData"]:
        """Run batch enrichment through all metadata plugins IN PARALLEL

        This is the ONLY way to run batch enrichment. GameService must use this
        method instead of calling metadata plugins directly.

        Plugins run in parallel for better performance. Results are merged
        using priority order after all plugins complete.

        Args:
            store_name: Name of the store being enriched
            games: List of PluginGame objects to enrich
            progress_callback: Optional callback(message, current, total)
            cancel_check: Optional function that returns True if cancelled
            status_callback: Optional callback(message) for status messages (rate limit, etc)
            metadata_started_callback: Optional callback(plugin_name) when plugin starts
            metadata_finished_callback: Optional callback(plugin_name) when plugin finishes
            metadata_skipped_callback: Optional callback(plugin_name) when plugin skipped

        Returns:
            Dict mapping store_app_id -> EnrichmentData (merged from all sources)
        """
        import asyncio

        if not self._plugin_manager or not games:
            return {}

        if self._is_offline():
            logger.debug("Skipping enrich_games_batch — offline")
            return {}

        # Import here to avoid circular imports
        from luducat.plugins.base import EnrichmentData

        # Get metadata plugins in priority order
        metadata_plugins = self._plugin_manager.get_metadata_plugins()
        if not metadata_plugins:
            return {}

        from luducat.core.plugin_manager import PluginManager
        ordered_names = PluginManager.get_enrichment_plugin_names()
        # Include any plugins not in enrichment list at the end
        for name in metadata_plugins:
            if name not in ordered_names:
                ordered_names.append(name)

        # Determine which plugins to run and which to skip
        plugins_to_run = []
        for plugin_name in ordered_names:
            plugin = metadata_plugins.get(plugin_name)
            if plugin is None:
                continue

            # Check skip_on_sync setting
            skip_on_sync = plugin.get_setting("skip_on_sync", False)
            if skip_on_sync:
                logger.debug(f"Metadata plugin {plugin_name} marked as skip_on_sync")
                if metadata_skipped_callback:
                    metadata_skipped_callback(plugin_name)
                continue

            if not plugin.is_available():
                logger.debug(f"Metadata plugin {plugin_name} not available")
                if metadata_skipped_callback:
                    metadata_skipped_callback(plugin_name)
                continue

            auto_enrich = plugin.get_setting("auto_enrich", True)
            if not auto_enrich:
                logger.debug(f"Auto-enrich disabled for {plugin_name}")
                if metadata_skipped_callback:
                    metadata_skipped_callback(plugin_name)
                continue

            plugins_to_run.append((plugin_name, plugin))

        if not plugins_to_run:
            return {}

        # Resolve cross-store Steam AppIDs for non-Steam games
        # This enables ProtonDB (Steam-only) to rate GOG/Epic games
        cross_store_ids: Dict[str, str] = {}
        if store_name != "steam":
            try:
                cross_store_ids = self.resolve_steam_app_ids(store_name, games)
            except Exception as e:
                logger.warning(f"Cross-store Steam ID resolution failed: {e}")

        async def run_plugin_enrichment(
            plugin_name: str, plugin
        ) -> tuple[str, Dict[str, "EnrichmentData"]]:
            """Run enrichment for a single plugin.

            Handles RateLimitError with retry and UI notification via
            status_callback. Long waits (e.g. ProtonDB 5min) are managed
            here rather than inside the plugin.
            """
            from luducat.plugins.base import RateLimitError

            max_retries = 3

            try:
                # Notify started
                if metadata_started_callback:
                    metadata_started_callback(plugin_name)

                # Authenticate if needed
                if not plugin.is_authenticated():
                    await plugin.authenticate()

                if progress_callback:
                    progress_callback(
                        f"Enriching with {plugin.display_name}...", 0, len(games)
                    )

                # Run batch enrichment with rate limit retry
                enrichments: dict = {}
                for attempt in range(max_retries + 1):
                    try:
                        enrichments = await plugin.enrich_games(
                            games,
                            status_callback=progress_callback,
                            cancel_check=cancel_check,
                            cross_store_ids=cross_store_ids,
                        )
                        break  # Success
                    except RateLimitError as e:
                        if attempt >= max_retries:
                            logger.error(
                                f"{plugin_name}: Rate limit exceeded after "
                                f"{max_retries} retries"
                            )
                            enrichments = {}
                            break
                        wait = e.wait_seconds
                        logger.warning(
                            f"Rate limit hit ({plugin_name}, attempt "
                            f"{attempt + 1}/{max_retries + 1}). "
                            f"Waiting {wait // 60} minutes..."
                        )
                        remaining = wait
                        while remaining > 0:
                            mins_left = (remaining + 59) // 60
                            if status_callback:
                                status_callback(
                                    f"Rate limit ({plugin_name}): "
                                    f"{mins_left} min remaining "
                                    f"(attempt {attempt + 1}/{max_retries + 1})"
                                )
                            chunk = min(60, remaining)
                            await asyncio.sleep(chunk)
                            remaining -= chunk
                            if cancel_check and cancel_check():
                                break
                        if status_callback:
                            status_callback("")
                        if cancel_check and cancel_check():
                            enrichments = {}
                            break

                # Notify finished
                if metadata_finished_callback:
                    metadata_finished_callback(plugin_name)

                return (plugin_name, enrichments)

            except Exception as e:
                logger.warning(f"Enrichment failed for {plugin_name}: {e}")
                # Still notify finished even on error
                if metadata_finished_callback:
                    metadata_finished_callback(plugin_name)
                return (plugin_name, {})

        # Run all plugins in PARALLEL
        tasks = [
            run_plugin_enrichment(name, plugin) for name, plugin in plugins_to_run
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect enrichments by app_id, tracking sources
        all_enrichments: Dict[str, Dict[str, Any]] = {}  # app_id -> {field: (value, source)}

        # Merge results in priority order
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Plugin enrichment raised exception: {result}")
                continue

            plugin_name, enrichments = result
            if not enrichments:
                continue

            # Merge enrichments using priority (lower rank wins)
            for app_id, enrichment in enrichments.items():
                if app_id not in all_enrichments:
                    all_enrichments[app_id] = {}

                existing = all_enrichments[app_id]
                self._merge_enrichment(existing, enrichment, plugin_name)

        # Convert merged data back to EnrichmentData objects
        result_dict: Dict[str, EnrichmentData] = {}
        for app_id, merged in all_enrichments.items():
            result_dict[app_id] = self._build_enrichment_data(merged)

        return result_dict

    async def enrich_games_single_plugin(
        self,
        plugin_name: str,
        store_name: str,
        games: List["PluginGame"],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, "EnrichmentData"]:
        """Run enrichment through a SINGLE metadata plugin.

        Unlike enrich_games_batch() which runs ALL plugins in parallel,
        this runs only the specified plugin. Used by the sequential job
        queue for predictable, one-at-a-time enrichment.

        Handles cross-store ID resolution for ProtonDB automatically.

        Args:
            plugin_name: Name of the metadata plugin to run
            store_name: Store that owns these games
            games: List of PluginGame objects to enrich
            progress_callback: Optional callback(message, current, total)
            cancel_check: Optional function that returns True if cancelled
            status_callback: Optional callback(message) for status messages

        Returns:
            Dict mapping store_app_id -> EnrichmentData
        """
        if not self._plugin_manager or not games:
            return {}

        if self._is_offline():
            logger.debug("Skipping single-plugin enrich — offline")
            return {}

        from luducat.plugins.base import RateLimitError
        import asyncio

        metadata_plugins = self._plugin_manager.get_metadata_plugins()
        plugin = metadata_plugins.get(plugin_name)
        if not plugin:
            logger.warning(f"Metadata plugin not found: {plugin_name}")
            return {}

        if not plugin.is_available():
            logger.info(f"Skipping {plugin_name}: not available")
            return {}

        # Cross-store ID resolution (needed for ProtonDB on non-Steam games)
        cross_store_ids = {}
        if store_name != "steam":
            cross_store_ids = self.resolve_steam_app_ids(store_name, games)

        # Authenticate if needed
        if not plugin.is_authenticated():
            try:
                await plugin.authenticate()
            except Exception as e:
                logger.error(f"Authentication failed for {plugin_name}: {e}")
                return {}

        # Run enrichment with rate limit retry
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                enrichments = await plugin.enrich_games(
                    games,
                    status_callback=progress_callback,
                    cancel_check=cancel_check,
                    cross_store_ids=cross_store_ids,
                )
                return enrichments or {}
            except RateLimitError as e:
                if attempt >= max_retries:
                    logger.error(
                        f"{plugin_name}: rate limit exceeded after {max_retries} retries"
                    )
                    return {}
                wait = e.wait_seconds
                logger.warning(
                    f"{plugin_name}: rate limit, waiting {wait}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                remaining = wait
                while remaining > 0:
                    mins_left = (remaining + 59) // 60
                    if status_callback:
                        status_callback(
                            f"Rate limit ({plugin_name}): "
                            f"{mins_left} min remaining "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                    chunk = min(60, remaining)
                    await asyncio.sleep(chunk)
                    remaining -= chunk
                    if cancel_check and cancel_check():
                        break
                if status_callback:
                    status_callback("")
                if cancel_check and cancel_check():
                    return {}
            except Exception as e:
                logger.error(f"{plugin_name} enrichment failed: {e}")
                return {}

        return {}

    def _merge_enrichment(
        self,
        existing: Dict[str, Any],
        enrichment: "EnrichmentData",
        source_name: str,
    ) -> None:
        """Merge an enrichment into existing data using priority

        Modifies existing dict in place. Only overwrites if new source has
        higher priority (lower rank) for each field.
        """
        # Define field mappings: enrichment attr -> (field_name, priority_field)
        field_mappings = [
            ("genres", "genres"),
            ("developers", "developers"),
            ("publishers", "publishers"),
            ("cover_url", "cover"),
            ("screenshots", "screenshots"),
            ("background_url", "hero"),
            ("franchise", "franchise"),
            ("series", "series"),
            ("themes", "themes"),
            ("perspectives", "perspectives"),
            ("platforms", "platforms"),
            ("age_ratings", "age_rating"),
            ("websites", "links"),
            ("user_rating", "rating"),
            ("engine", "engine"),
        ]

        for attr_name, field_name in field_mappings:
            value = getattr(enrichment, attr_name, None)
            if not self._is_non_empty(value):
                continue

            current_source = existing.get(f"_source_{field_name}", "")
            if current_source:
                # Check priority
                new_rank = self.get_field_priority_rank(field_name, source_name)
                current_rank = self.get_field_priority_rank(field_name, current_source)
                if new_rank >= current_rank:
                    continue  # Current source has equal or higher priority

            # Set new value
            existing[attr_name] = value
            existing[f"_source_{field_name}"] = source_name

        # Handle extra dict (for multiplayer, crossplay, etc.)
        if enrichment.extra:
            if "extra" not in existing:
                existing["extra"] = {}
            for key, value in enrichment.extra.items():
                if self._is_non_empty(value):
                    existing["extra"][key] = value

    def _build_enrichment_data(self, merged: Dict[str, Any]) -> "EnrichmentData":
        """Build EnrichmentData from merged dict"""
        from luducat.plugins.base import EnrichmentData

        # Extract per-field source map from _source_* keys
        source_map = {}
        for key, value in merged.items():
            if key.startswith("_source_") and isinstance(value, str):
                source_map[key[8:]] = value  # strip "_source_" prefix

        # Pick a representative provider_name (for backward compat)
        provider_name = (
            source_map.get("cover")
            or source_map.get("hero")
            or next(iter(source_map.values()), "merged")
        )

        return EnrichmentData(
            provider_name=provider_name,
            provider_id="",  # Not tracked in merge
            genres=merged.get("genres"),
            developers=merged.get("developers"),
            publishers=merged.get("publishers"),
            cover_url=merged.get("cover_url"),
            screenshots=merged.get("screenshots"),
            background_url=merged.get("background_url"),
            franchise=merged.get("franchise"),
            series=merged.get("series"),
            themes=merged.get("themes"),
            perspectives=merged.get("perspectives"),
            platforms=merged.get("platforms"),
            age_ratings=merged.get("age_ratings"),
            websites=merged.get("websites"),
            user_rating=merged.get("user_rating"),
            engine=merged.get("engine"),
            extra=merged.get("extra"),
            source_map=source_map,
        )

    def clear_plugin_cache_for_game(
        self,
        store_ids: List[Tuple[str, str]],
    ) -> None:
        """Clear cached matches in metadata plugins for a game

        Used before force-rescan to ensure fresh data is fetched.

        Args:
            store_ids: List of (store_name, store_app_id) tuples
        """
        if not self._plugin_manager:
            return

        metadata_plugins = self._plugin_manager.get_metadata_plugins()

        for plugin_name, plugin in metadata_plugins.items():
            try:
                # table_name is a hardcoded string from each plugin's
                # store_match_table property (e.g. "igdb_store_matches"),
                # not user input. SQL parameterization doesn't support
                # table names, so string interpolation is acceptable here.
                table_name = getattr(plugin, "store_match_table", None)
                if not table_name:
                    continue
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
                    logger.warning(f"Invalid table name from plugin: {table_name!r}")
                    continue

                db = getattr(plugin, "db", None) or getattr(plugin, "_db", None)
                if db is None:
                    continue

                plugin_session = db.get_session()
                for store_name, store_app_id in store_ids:
                    from sqlalchemy import text
                    plugin_session.execute(
                        text(
                            f"DELETE FROM {table_name} "
                            "WHERE store_name = :sn "
                            "AND store_app_id = :sid"
                        ),
                        {"sn": store_name, "sid": store_app_id},
                    )
                plugin_session.commit()
                logger.debug(f"Cleared store matches in {plugin_name}")
            except Exception as e:
                logger.debug(f"Could not clear matches in {plugin_name}: {e}")

    async def force_rescan_game(
        self,
        games: List["PluginGame"],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> Dict[str, "EnrichmentData"]:
        """Force rescan metadata for games, bypassing cache

        Clears plugin caches and re-fetches from all sources.

        Args:
            games: List of PluginGame objects to rescan
            progress_callback: Optional callback(message, current, total)

        Returns:
            Dict mapping store_app_id -> EnrichmentData
        """
        if not self._plugin_manager or not games:
            return {}

        # Clear plugin caches first
        store_ids = [(g.store_name, g.store_app_id) for g in games]
        self.clear_plugin_cache_for_game(store_ids)

        # Now run enrichment (will fetch fresh data)
        return await self.enrich_games_batch(
            games[0].store_name if games else "",
            games,
            progress_callback=progress_callback,
        )


# ---------------------------------------------------------------------------
# Global singleton — ONE MetadataResolver instance for the entire process.
# Production code MUST use get_resolver().  Tests use init_resolver() /
# reset_resolver() for isolation.
# ---------------------------------------------------------------------------
_default_resolver: Optional[MetadataResolver] = None


def init_resolver(field_priorities: Dict[str, List[str]]) -> MetadataResolver:
    """Initialize the global singleton.  Called ONCE at app startup.

    Args:
        field_priorities: Per-field priority lists from user config.

    Returns:
        The newly created MetadataResolver.
    """
    global _default_resolver
    _default_resolver = MetadataResolver(field_priorities=field_priorities)
    return _default_resolver


def get_resolver() -> MetadataResolver:
    """Get the global MetadataResolver singleton.

    Raises RuntimeError if init_resolver() has not been called yet.
    """
    if _default_resolver is None:
        raise RuntimeError(
            "MetadataResolver not initialized — call init_resolver() first"
        )
    return _default_resolver


def reset_resolver() -> None:
    """Clear the global singleton (for test teardown only)."""
    global _default_resolver
    _default_resolver = None
