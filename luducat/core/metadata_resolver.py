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

import dataclasses
import hashlib
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from luducat.core.json_compat import json
from luducat.core.network_manager import OfflineError
from luducat.plugins.base import (
    AuthenticationError,
    Lookup,
    LookupOutcome,
    NetworkError,
    RateLimitError,
)

if TYPE_CHECKING:
    from luducat.core.plugin_manager import PluginManager
    from luducat.plugins.base import EnrichmentData
    from luducat.plugins.base import Game as PluginGame

logger = logging.getLogger(__name__)


# Plugin exceptions that mean "could not ask", never "no record". Anything
# else raised by a hop is a plugin bug, logged and treated the same way.
_UNAVAILABLE_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (OfflineError, "offline"),
    (RateLimitError, "rate_limited"),
    (AuthenticationError, "auth"),
    (NetworkError, "network"),
)


# Unavailable reasons that clear by themselves; "auth" and "error: ..." do not.
TRANSIENT_REASONS = frozenset({"offline", "rate_limited", "network", "cancelled"})


@dataclasses.dataclass(frozen=True, slots=True)
class ChainHop:
    """One visited source in a chain walk and how it answered."""

    source: str
    outcome: LookupOutcome
    reason: str = ""

    @property
    def outcome_text(self) -> str:
        if self.reason:
            return f"{self.outcome.value} ({self.reason})"
        return self.outcome.value

    @property
    def transient(self) -> bool:
        """True when the same question may get another answer soon."""
        return self.reason.split(" ", 1)[0] in TRANSIENT_REASONS

    def describe(self) -> str:
        return f"{self.source} {self.outcome_text}"


@dataclasses.dataclass(slots=True)
class ChainWalk:
    """Result of one walk over a priority chain.

    Single-field walks fill value/source with the winner; gather walks
    keep every found record in by_source. trail lists every hop asked.
    """

    offline: bool
    value: Any = None
    source: str = ""
    trail: list[ChainHop] = dataclasses.field(default_factory=list)
    by_source: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.by_source)

    @property
    def retry_later(self) -> bool:
        """An empty walk that must not be remembered as a miss: offline, or a
        source was only paused or failed a request. A missing key or a plugin
        error does not go away by asking again, so those walks are final."""
        if self.found:
            return False
        return self.offline or any(
            hop.outcome is LookupOutcome.UNAVAILABLE and hop.transient
            for hop in self.trail
        )


# Hardcoded fallback - used when no plugin declares provides_fields
_DEFAULT_FIELD_SOURCE_CAPABILITIES: dict[str, list[str]] = {
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
    "is_demo": ["steam"],
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
FIELD_SOURCE_CAPABILITIES: dict[str, list[str]] = dict(_DEFAULT_FIELD_SOURCE_CAPABILITIES)

# Field groupings for UI organization
FIELD_GROUPS: dict[str, list[str]] = {
    "General": [
        "title", "description", "short_description", "developers", "publishers",
        "genres", "release_date", "game_modes_detail", "crossplay",
        "supported_languages", "full_audio_languages", "features",
        "type", "is_free", "is_demo", "required_age", "category", "status",
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
FIELD_LABELS: dict[str, str] = {
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
    "is_demo": N_("Demo"),
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
FIELD_TOOLTIPS: dict[str, str] = {
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
    "is_demo": N_("Whether this is a demo, trial, or prologue version."),
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
_DEFAULT_SOURCE_LABELS: dict[str, str] = {
    "steam": "Steam",
    "gog": "GOG",
    "epic": "Epic Games",
    "igdb": "IGDB",
    "pcgamingwiki": "PCGamingWiki",
    "steamgriddb": "SteamGridDB",
}

# Mutable alias - mutated in-place by build_from_plugins()
SOURCE_LABELS: dict[str, str] = dict(_DEFAULT_SOURCE_LABELS)


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
    # Identity fields (per-store keys, DB primary keys)
    "app_id",
    "id",
    "game_id",
    "translation_id",
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
    # Store engine internal fields
    "store_adult_baseline",  # Adult content baseline from store engine rulesets
    "store_adult_flag",  # Store-side per-game adult flag (JAST mature_content, MangaGamer r18)
    "mature_content",  # Raw store mature flag (feeds adult_flag ruleset condition)
    "goods_type",  # MangaGamer URL construction parameter
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

_SEED_FIELD_PRIORITIES: dict[str, list[str]] = {
    # General
    "title": ["steam", "gog", "epic", "amazon", "zoom", "jastusa", "mangagamer", "igdb", "pcgamingwiki"],
    "description": ["steam", "gog", "amazon", "igdb", "zoom", "jastusa", "mangagamer"],
    "short_description": ["steam", "gog", "epic", "amazon", "zoom", "jastusa", "igdb"],
    "developers": ["igdb", "pcgamingwiki", "steam", "gog", "epic", "amazon", "zoom", "jastusa", "mangagamer"],
    "publishers": ["igdb", "pcgamingwiki", "steam", "gog", "epic", "amazon", "zoom", "jastusa"],
    "genres": ["pcgamingwiki", "igdb", "steam", "gog", "epic", "amazon", "zoom", "mangagamer"],
    "release_date": ["steam", "gog", "igdb", "pcgamingwiki", "zoom", "jastusa", "mangagamer"],
    "game_modes_detail": ["pcgamingwiki", "steam", "gog", "igdb"],
    "crossplay": ["pcgamingwiki"],
    # Media
    "cover": ["igdb", "steamgriddb", "steam", "epic", "gog", "zoom", "jastusa", "mangagamer", "amazon"],
    "hero": ["steamgriddb", "igdb", "steam", "epic", "gog", "amazon"],
    "screenshots": ["steam", "gog", "epic", "igdb", "zoom", "jastusa", "mangagamer", "amazon"],
    "header_url": ["steam", "epic", "amazon", "gog"],
    "artworks": ["igdb"],
    "icon_url": ["steam", "gog", "amazon"],
    "logo_url": ["steam", "gog", "epic", "amazon"],
    "videos": ["igdb", "gog", "jastusa"],
    # Ratings
    "rating": ["steam", "gog", "igdb"],
    "user_rating": ["igdb"],
    "user_rating_count": ["igdb"],
    "rating_positive": ["steam"],
    "rating_negative": ["steam"],
    "total_rating": ["igdb"],
    "age_ratings": ["igdb", "gog", "amazon"],
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
    "platforms": ["igdb", "pcgamingwiki", "steam", "gog", "epic", "zoom", "amazon"],
    "links": ["igdb", "pcgamingwiki", "steam", "gog", "epic", "amazon"],
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
    "supported_languages": ["steam", "gog", "zoom"],
    "full_audio_languages": ["steam", "gog"],
    "features": ["gog", "steam", "epic", "amazon"],
    # Game classification
    "category": ["igdb"],
    "status": ["igdb"],
    "game_modes": ["pcgamingwiki", "steam", "igdb", "zoom"],
    "required_age": ["steam", "gog", "igdb", "amazon"],
    "content_descriptors": ["steam"],
    # Commercial
    "price": ["steam", "gog", "epic", "zoom"],
    "type": ["steam", "gog", "epic", "igdb", "amazon"],
    "is_free": ["steam", "gog", "zoom"],
    "is_demo": ["steam"],
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

# The priority name doubles as the _sources key enrichment_service writes;
# a name missing from the priority tables ranks every source 999.
ENRICHMENT_FIELD_MAPPINGS: tuple[tuple[str, str], ...] = (
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
    ("age_ratings", "age_ratings"),
    ("websites", "links"),
    ("user_rating", "rating"),
    ("engine", "engine"),
)


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
    _PRIORITY_FIELD_ALIASES: dict[str, str] = {
        "cover_url": "cover",
        "cover_detail_url": "cover",
        "background_url": "hero",
        "websites": "links",
        "categories": "features",
        "franchises": "franchise",
        "languages": "supported_languages",
        "operating_systems": "platforms",
        "esrb_rating": "age_rating_esrb",
    }

    def __init__(
        self,
        field_priorities: dict[str, list[str]],
    ):
        """Initialize the resolver.

        Args:
            field_priorities: Per-field priority lists loaded from user config.
                              REQUIRED — no hardcoded fallbacks.
        """
        self._plugin_manager: PluginManager | None = None

        # Track which fields user has explicitly customized
        self._user_overrides: set = set()
        # Track whether build_from_plugins has been called
        self._build_initialized: bool = False
        # Fields we've already warned about (warn once per field)
        self._missing_fields_warned: set = set()

        # Per-field priorities from config — the ONLY source of truth
        self._field_priorities: dict[str, list[str]] = {
            k: v.copy() for k, v in field_priorities.items()
        }

        # Callback fired when priorities change (for cache invalidation)
        self._on_priorities_changed: Callable | None = None

    def set_plugin_manager(self, plugin_manager: "PluginManager") -> None:
        """Set the plugin manager for on-demand metadata fetching

        Args:
            plugin_manager: The application's plugin manager instance
        """
        self._plugin_manager = plugin_manager

    def get_field_priority(self, field_name: str) -> list[str]:
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

    def _get_all_priority_sources(self) -> list[str]:
        """Get all unique sources from field priorities, order-preserved.

        Used when no specific field is requested (full metadata gather).
        """
        seen: set = set()
        result: list[str] = []
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

    def set_field_priorities(self, priorities: dict[str, list[str]]) -> None:
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

    def update_field_priorities(self, priorities: dict[str, list[str]]) -> None:
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

    def set_on_priorities_changed(self, callback: Callable | None) -> None:
        """Register a callback fired when priorities change.

        Args:
            callback: Callable with no arguments, or None to clear.
        """
        self._on_priorities_changed = callback

    def resolve_field_with_priorities(
        self,
        field_name: str,
        metadata_by_store: dict[str, dict[str, Any]],
        priorities: list[str],
    ) -> tuple[Any, str | None]:
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

    def get_all_field_priorities(self) -> dict[str, list[str]]:
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

    def get_effective_defaults(self) -> dict[str, list[str]]:
        """Get the factory-default field priorities (for "Reset to Defaults").

        Returns the seed defaults — the same values used to populate config
        on first run. Only used by the settings UI reset button.

        Returns:
            Dict mapping field_name -> list of sources in priority order
        """
        return {k: v.copy() for k, v in _SEED_FIELD_PRIORITIES.items()}

    def build_from_plugins(self, discovered_plugins: dict[str, Any]) -> None:
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
        field_declarations: dict[str, list[tuple[str, int]]] = {}
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
        new_labels: dict[str, str] = {}
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
        new_caps: dict[str, list[str]] = {}
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
        metadata_by_store: dict[str, dict[str, Any]]
    ) -> tuple[Any, str | None]:
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
        metadata_by_store: dict[str, dict[str, Any]]
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
            for source_metadata in metadata_by_store.values():
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
        metadata_by_store: dict[str, dict[str, Any]],
        fields: list[str] | None = None
    ) -> dict[str, Any]:
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
        metadata_by_store: dict[str, dict[str, Any]]
    ) -> dict[str, str]:
        """Merge release_date dicts from all sources, keeping oldest per platform.

        Uses user-configured release_date priority for source ordering.
        Sources NOT in the priority list are excluded entirely.
        Handles both new dict format and old string format for backward compat.

        Args:
            metadata_by_store: Dict mapping source_name -> metadata dict

        Returns:
            Dict mapping platform_name -> "YYYY-MM-DD" string, or empty dict
        """
        merged: dict[str, str] = {}
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
        metadata_by_store: dict[str, dict[str, Any]]
    ) -> str | None:
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
    _STORAGE_ALIASES: dict[str, str] = {
        "cover_url": "cover",
        "cover_detail_url": "cover",
        "background_url": "hero",
        "websites": "links",
        "categories": "features",
        "franchises": "franchise",
        "languages": "supported_languages",
        "operating_systems": "platforms",
        "esrb_rating": "age_rating_esrb",
    }
    # Reverse: canonical → old storage name (for metadata dicts that haven't been re-synced)
    _CANONICAL_TO_OLD: dict[str, str] = {v: k for k, v in _STORAGE_ALIASES.items()}

    @staticmethod
    def _get_field(
        metadata: dict[str, Any],
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
        """Check if a plugin is usable for resolve-time source selection
        (enabled + authenticated/available).

        NOT for the on-demand lazy-load chain; that uses
        _enabled_plugins_in_chain(), which gates on enabled-ness only.

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

        from luducat.plugins.base import StorePlugin
        if isinstance(plugin, StorePlugin):
            return plugin.is_authenticated()

        # Metadata/enrichment plugins: is_available() is the right check
        if not plugin.is_available():
            return False

        return True

    def _enabled_plugins_in_chain(self, priority: list[str]):
        """Yield (name, plugin) for every enabled plugin, in priority order.

        The lazy-load chain is gated on enabled-ness only. Credentials,
        API keys and connectivity are the plugin's business: a plugin that
        cannot answer returns None or raises, and the caller moves on.
        """
        for plugin_name in priority:
            plugin = self._plugin_manager.get_plugin(plugin_name)
            if plugin is not None:
                yield plugin_name, plugin

    @staticmethod
    def _unavailable_from_exception(plugin_name: str, exc: Exception) -> Lookup:
        for exc_type, reason in _UNAVAILABLE_REASONS:
            if isinstance(exc, exc_type):
                return Lookup.unavailable(reason)
        logger.error("lookup in %s raised %s: %s",
                     plugin_name, type(exc).__name__, exc)
        logger.debug("lookup in %s traceback", plugin_name, exc_info=exc)
        return Lookup.unavailable(f"error: {type(exc).__name__}")

    def _lookup_hop(
        self,
        plugin_name: str,
        plugin: Any,
        store_app_ids: dict[str, str],
        normalized_title: str,
    ) -> Lookup:
        """Ask one plugin about every store id and fold the answers into
        one outcome: found beats unavailable beats not_found, so an id
        that could not be looked up is never reported as a miss."""
        if not hasattr(plugin, "get_metadata_for_store_game"):
            return Lookup.not_found("no lookup interface")

        unavailable: Lookup | None = None
        missing: Lookup | None = None
        for store_name, app_id in store_app_ids.items():
            if not app_id:
                continue
            try:
                answer = Lookup.from_legacy(plugin.get_metadata_for_store_game(
                    store_name, app_id, normalized_title=normalized_title
                ))
            except Exception as e:  # plugin dispatch boundary
                answer = self._unavailable_from_exception(plugin_name, e)
            if answer.is_found:
                return answer
            if answer.outcome is LookupOutcome.UNAVAILABLE:
                unavailable = unavailable or answer
            else:
                missing = answer

        if unavailable is not None:
            return unavailable
        return missing if missing is not None else Lookup.not_found("no store id")

    @classmethod
    def _field_from_metadata(cls, metadata: dict[str, Any], field_name: str) -> Any:
        """Non-empty value for a field, under its canonical or any old
        storage name; None when the record has nothing usable."""
        canonical = cls._STORAGE_ALIASES.get(field_name, field_name)
        value = metadata.get(canonical)
        if cls._is_non_empty(value):
            return value
        for old_name, canon in cls._STORAGE_ALIASES.items():
            if canon == canonical:
                value = metadata.get(old_name)
                if cls._is_non_empty(value):
                    return value
        return None

    def _walk_chain(
        self,
        priority: list[str],
        store_app_ids: dict[str, str],
        normalized_title: str,
        *,
        field: str | None = None,
        offline: bool | None = None,
    ) -> ChainWalk:
        """Walk a priority chain once, one hop per enabled source.

        With a field, the walk stops at the first hop whose record carries
        a non-empty value for it. Without one, every hop is visited and
        each found record lands in by_source. A hop answering not_found or
        unavailable never ends the walk; credentials and connectivity are
        the plugin's business. Offline is detected once per walk.
        """
        walk = ChainWalk(offline=self._is_offline() if offline is None else offline)
        label = field or "metadata"

        for plugin_name, plugin in self._enabled_plugins_in_chain(priority):
            if any(hop.source == plugin_name for hop in walk.trail):
                continue

            answer = self._lookup_hop(plugin_name, plugin, store_app_ids,
                                      normalized_title)
            value = None
            if answer.is_found and field is not None:
                value = self._field_from_metadata(answer.data, field)
                if value is None:
                    answer = Lookup.not_found(f"no {field}")

            hop = ChainHop(plugin_name, answer.outcome, answer.reason)
            walk.trail.append(hop)
            logger.debug("%s <- %s: %s", label, plugin_name, hop.outcome_text)

            if answer.is_found:
                walk.by_source[plugin_name] = answer.data
                if field is not None:
                    walk.value = value
                    walk.source = plugin_name
                    return walk

        if not walk.found:
            self._log_exhausted(label, walk, store_app_ids, normalized_title)
        return walk

    @staticmethod
    def _log_exhausted(
        label: str,
        walk: ChainWalk,
        store_app_ids: dict[str, str],
        normalized_title: str,
    ) -> None:
        """One INFO line per empty walk so a miss is a grep, not a code read."""
        ids = ",".join(f"{s}:{i}" for s, i in store_app_ids.items() if i)
        hops = ", ".join(hop.describe() for hop in walk.trail) or "no enabled source"
        logger.info("%s chain exhausted for '%s' (%s)%s: %s",
                    label, normalized_title, ids or "no ids",
                    ", offline" if walk.offline else "", hops)

    # Key fields that should be present for metadata to be considered "complete"
    ESSENTIAL_FIELDS = ["cover", "release_date", "developers", "publishers"]

    def _is_metadata_complete(self, metadata: dict[str, Any]) -> bool:
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
        store_app_ids: dict[str, str],
        normalized_title: str,
    ) -> str | None:
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

        # Fallback: try the target store's own title-based resolution
        # (e.g., ZOOM derives slugs from titles and probes its public API)
        if normalized_title:
            target_plugin = self._plugin_manager.get_plugin(target_store)
            if target_plugin and hasattr(target_plugin, "resolve_by_title"):
                try:
                    result = target_plugin.resolve_by_title(normalized_title)
                    if result and result.get("app_id"):
                        logger.info(
                            f"Title-based resolution: resolved {target_store} "
                            f"for '{normalized_title}'"
                        )
                        return result["app_id"]
                except Exception as e:
                    logger.debug(
                        f"Title-based resolution failed for "
                        f"{target_store}/{normalized_title}: {e}"
                    )

        return None

    def fetch_metadata_for_game(
        self,
        store_app_ids: dict[str, str],
        normalized_title: str = "",
        field: str | None = None,
        _return_sources: bool = False,
    ) -> dict[str, Any] | None | tuple[dict[str, Any] | None, dict[str, str]]:
        """Fetch metadata on-demand from plugins in priority order.

        With a field, walks that field's priority and returns the winning
        source's whole record. Without one, gathers every enabled source
        and merges by per-field priority. See _walk_chain for the walk
        rules.

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

        priority = (
            self.get_field_priority(field) if field
            else self._get_all_priority_sources()
        )
        walk = self._walk_chain(priority, store_app_ids, normalized_title,
                                field=field)
        if not walk.found:
            return _none_result

        if field:
            record = walk.by_source[walk.source]
            return (record, {field: walk.source}) if _return_sources else record

        metadata_by_source = walk.by_source
        if _return_sources:
            # Use resolve_field_with_source for each field to build source map
            all_fields: set = set()
            for source_meta in metadata_by_source.values():
                all_fields.update(source_meta.keys())

            merged = {}
            source_map: dict[str, str] = {}
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

    def get_description_on_demand(
        self,
        store_app_ids: dict[str, str],
        normalized_title: str = ""
    ) -> str:
        """Get description on-demand from plugins in priority order

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Description string or empty string if not found
        """
        if not self._plugin_manager:
            return ""

        walk = self._walk_chain(self.get_field_priority("description"),
                                store_app_ids, normalized_title,
                                field="description")
        return walk.value if walk.found else ""

    def get_screenshots_on_demand(
        self,
        store_app_ids: dict[str, str],
        normalized_title: str = "",
        exclude_sources: list[str] | None = None,
    ) -> tuple[list[str], str]:
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

        skip = set(exclude_sources) if exclude_sources else set()
        priority = [
            name for name in self.get_field_priority("screenshots")
            if name not in skip
        ]
        walk = self._walk_chain(priority, store_app_ids, normalized_title,
                                field="screenshots")
        return (walk.value, walk.source) if walk.found else ([], "")

    def get_cover_on_demand(
        self,
        store_app_ids: dict[str, str],
        normalized_title: str = ""
    ) -> tuple[str, str]:
        """Get cover image URL on-demand from plugins in per-field priority order.

        Args:
            store_app_ids: Dict mapping store_name -> app_id for this game
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Tuple of (cover_url, source_name) or ("", "") if not found
        """
        walk = self.cover_walk(store_app_ids, normalized_title)
        if not walk.found:
            return "", ""
        return walk.value, walk.source

    def cover_walk(
        self,
        store_app_ids: dict[str, str],
        normalized_title: str = "",
    ) -> ChainWalk:
        """The cover chain walk itself, for callers that need to know
        whether an empty answer is final (ChainWalk.retry_later)."""
        if not self._plugin_manager:
            return ChainWalk(offline=False)

        walk = self._walk_chain(self.get_field_priority("cover"),
                                store_app_ids, normalized_title, field="cover")
        if walk.found:
            logger.info("get_cover_on_demand: Got cover from %s", walk.source)
        return walk

    def get_game_modes_bulk(
        self,
        store_app_ids: dict[str, list[str]]
    ) -> dict[str, dict[str, list[str]]]:
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

        result: dict[str, dict[str, list[str]]] = {}

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
        store_app_ids: dict[str, list[str]],
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, dict[str, dict[str, Any]]]:
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

        result: dict[str, dict[str, dict[str, Any]]] = {}

        # Each enrichment plugin has its own DB — safe to parallelize
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_enrichment(plugin_name, plugin):
            plugin_data = plugin.get_cache_metadata_bulk(store_app_ids)
            flat: dict[str, dict[str, Any]] = {}
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
        games: list["PluginGame"],
    ) -> dict[str, str]:
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
        app_id_titles: dict[str, str] = {}
        for game in games:
            app_id_titles[game.store_app_id] = normalize_title(game.title)

        app_ids = list(app_id_titles.keys())
        result: dict[str, str] = {}

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
        metadata: dict[str, Any],
        store_app_ids: dict[str, str],
        fields: list[str] | None = None,
    ) -> dict[str, tuple[str, str]]:
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
        non_conforming: dict[str, tuple[str, str]] = {}

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
        priority: list[str],
        store_app_ids: dict[str, str],
    ) -> str | None:
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
        store_app_ids: dict[str, str],
        normalized_title: str = "",
    ) -> tuple[Any, str | None]:
        """Fetch a field from the highest-priority source that has it.

        Walks the field's priority over the sources capable of providing
        it, under the same rules as the lazy-load chain: stored data is
        answered offline, and no source is skipped for missing
        credentials.

        Args:
            field_name: Name of the field to fetch
            store_app_ids: Dict mapping store_name -> app_id
            normalized_title: Normalized game title for fallback lookups

        Returns:
            Tuple of (value, source_name) or (None, None) if not found
        """
        if not self._plugin_manager:
            return None, None

        capable_sources = FIELD_SOURCE_CAPABILITIES.get(field_name, [])
        priority = [
            source for source in self.get_field_priority(field_name)
            if source in capable_sources
        ]
        walk = self._walk_chain(priority, store_app_ids, normalized_title,
                                field=field_name)
        return (walk.value, walk.source) if walk.found else (None, None)

    def refresh_non_conforming_fields(
        self,
        metadata: dict[str, Any],
        store_app_ids: dict[str, str],
        normalized_title: str = "",
        fields: list[str] | None = None,
    ) -> dict[str, tuple[Any, str]]:
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

        refreshed: dict[str, tuple[Any, str]] = {}

        for field_name, (current, _expected) in non_conforming.items():
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
        fields: list[str],
    ) -> dict[str, dict[str, dict[str, Any]]]:
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
        _SOURCE_ALIASES: dict[str, str] = {
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
        usable_cache: dict[str, bool] = {}

        def is_usable(provider: str) -> bool:
            if provider not in usable_cache:
                usable_cache[provider] = self._is_plugin_usable(provider)
            return usable_cache[provider]

        # Build result: game_id -> provider -> field -> value
        result: dict[str, dict[str, dict[str, Any]]] = {}

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
        app_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
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

        # Bulk metadata reads from local plugin DBs — no auth required.
        # Only need the plugin to be enabled (get_plugin checks that).
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
        games: list["PluginGame"],
        progress_callback: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
        metadata_started_callback: Callable[[str], None] | None = None,
        metadata_finished_callback: Callable[[str], None] | None = None,
        metadata_skipped_callback: Callable[[str], None] | None = None,
    ) -> dict[str, "EnrichmentData"]:
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
        cross_store_ids: dict[str, str] = {}
        if store_name != "steam":
            try:
                cross_store_ids = self.resolve_steam_app_ids(store_name, games)
            except Exception as e:
                logger.warning(f"Cross-store Steam ID resolution failed: {e}")

        async def run_plugin_enrichment(
            plugin_name: str, plugin
        ) -> tuple[str, dict[str, "EnrichmentData"]]:
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
        all_enrichments: dict[str, dict[str, Any]] = {}  # app_id -> {field: (value, source)}

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
        result_dict: dict[str, EnrichmentData] = {}
        for app_id, merged in all_enrichments.items():
            result_dict[app_id] = self._build_enrichment_data(merged)

        return result_dict

    async def enrich_games_single_plugin(
        self,
        plugin_name: str,
        store_name: str,
        games: list["PluginGame"],
        progress_callback: Callable[[str, int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> dict[str, "EnrichmentData"]:
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

        import asyncio

        from luducat.plugins.base import RateLimitError

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
        existing: dict[str, Any],
        enrichment: "EnrichmentData",
        source_name: str,
    ) -> None:
        """Merge an enrichment into existing data using priority

        Modifies existing dict in place. Only overwrites if new source has
        higher priority (lower rank) for each field.
        """
        for attr_name, field_name in ENRICHMENT_FIELD_MAPPINGS:
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

    def _build_enrichment_data(self, merged: dict[str, Any]) -> "EnrichmentData":
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
        store_ids: list[tuple[str, str]],
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
        games: list["PluginGame"],
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, "EnrichmentData"]:
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
_default_resolver: MetadataResolver | None = None


def init_resolver(field_priorities: dict[str, list[str]]) -> MetadataResolver:
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
