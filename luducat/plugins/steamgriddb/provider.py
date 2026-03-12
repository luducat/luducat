# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""SteamGridDB Metadata Provider

Implements AbstractMetadataProvider to provide community-sourced
game images from SteamGridDB (heroes, grids, logos).

Primary use: Hero banner images as background artwork.
Auth: API key (free from steamgriddb.com profile).
"""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_from_timestamp, utc_now
from luducat.plugins.base import (
    AbstractMetadataProvider,
    EnrichmentData,
    Game,
    MetadataSearchResult,
)

from .api import SgdbApi, SgdbApiError, SgdbAuthError, SgdbCancelledError, ProgressCallback
from .database import SgdbDatabase, SgdbStoreMatch

logger = logging.getLogger(__name__)

# Settings choice → API parameter mappings
NSFW_FILTER_MAP = {"Exclude": "false", "Include": "any", "Only NSFW": "true"}
HUMOR_FILTER_MAP = {"Exclude": "false", "Include": "any", "Only Humor": "true"}


def _style_to_api(choice: str) -> Optional[str]:
    """Map a style choice label to API value (or None for 'Any')"""
    if not choice or choice == "Any":
        return None
    return choice.lower().replace(" ", "_")


class SgdbProvider(AbstractMetadataProvider):
    """SteamGridDB metadata provider

    Provides community-sourced game images from SteamGridDB:
    - Hero banners (primary use — non-dimmed background images)
    - Grid covers (library capsules)
    - Logos (transparent game logos)

    Requires a free API key from https://www.steamgriddb.com/profile/preferences/api
    """

    FAILED_MATCH_TTL = timedelta(days=30)

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._api: Optional[SgdbApi] = None
        self._db: Optional[SgdbDatabase] = None
        self._auth_validated: Optional[bool] = None  # Cache auth validation result

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def provider_name(self) -> str:
        return "steamgriddb"

    @property
    def display_name(self) -> str:
        return "SteamGridDB"

    @property
    def store_match_table(self) -> str:
        return "sgdb_store_matches"

    # =========================================================================
    # PUBLIC API (called by core via plugin instance)
    # =========================================================================

    def fetch_user_stats(
        self, steam64: str, timeout: int = 5,
    ) -> Optional[Dict[str, int]]:
        """Fetch author upload stats from SteamGridDB.

        Core calls this instead of importing api.py directly.

        Returns:
            {"grid": N, "hero": N, "logo": N, "icon": N} or None
        """
        from .api import fetch_sgdb_user_stats
        return fetch_sgdb_user_stats(steam64, timeout, http_client=self.http)

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def _get_api(self) -> SgdbApi:
        """Get or create API client (lazy init)"""
        if self._api is None:
            api_key = self.get_credential("api_key") or self.get_setting("api_key")
            if not api_key:
                raise SgdbAuthError("SteamGridDB API key not configured")
            self._api = SgdbApi(api_key=api_key, http_client=self.http)
        return self._api

    def _get_db(self) -> SgdbDatabase:
        """Get or create database (lazy init)"""
        if self._db is None:
            self._db = SgdbDatabase(self.get_database_path())
        return self._db

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Override to invalidate cached API when settings change"""
        super().set_settings(settings)
        if self._api:
            self._api.close()
            self._api = None
        # Reset auth validation so new credentials can be tested
        self._auth_validated = None

    # =========================================================================
    # ABSTRACT METHOD IMPLEMENTATIONS
    # =========================================================================

    def is_available(self) -> bool:
        """Check if API key is configured"""
        api_key = self.get_credential("api_key") or self.get_setting("api_key")
        return bool(api_key)

    async def authenticate(self) -> bool:
        """Validate API key

        SteamGridDB uses a simple API key (no OAuth flow).
        We validate by making a test request.
        """
        try:
            api = self._get_api()
            return api.validate_key()
        except SgdbAuthError:
            return False
        except Exception as e:
            logger.warning(f"SteamGridDB authentication check failed: {e}")
            return False

    def is_authenticated(self) -> bool:
        """Check if we have a valid API key

        Validates once per session and caches the result.
        """
        # Return cached result if available
        if self._auth_validated is not None:
            return self._auth_validated

        try:
            api = self._get_api()
            self._auth_validated = api.validate_key()
            logger.debug(f"SteamGridDB auth validation: {self._auth_validated}")
            return self._auth_validated
        except Exception as e:
            logger.debug(f"SteamGridDB auth validation failed: {e}")
            self._auth_validated = False
            return False

    def get_database_path(self) -> Path:
        return self.data_dir / "steamgriddb.db"

    # =========================================================================
    # STORE ID LOOKUP
    # =========================================================================

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        """Look up SteamGridDB game ID using store ID

        SteamGridDB has native platform endpoints:
        - /games/steam/{appid}
        - /games/gog/{gogid}
        - /games/egs/{egsid}

        Returns:
            SteamGridDB game ID as string, or None
        """
        sgdb_id = self._lookup_sgdb_id(store_name, store_id)
        return str(sgdb_id) if sgdb_id else None

    def _lookup_sgdb_id(
        self,
        store_name: str,
        store_app_id: str,
        normalized_title: Optional[str] = None,
        status_callback: ProgressCallback = None,
    ) -> Optional[int]:
        """Internal lookup with caching

        Algorithm:
        1. Check local cache (SgdbStoreMatch table)
        2. Try platform lookup (direct store ID → SGDB ID)
        3. If no match + title available, try title search
        4. Cache result (even no_match)
        """
        db = self._get_db()

        # Check cache first
        match = db.get_store_match(store_name, str(store_app_id))
        if match is not None:
            if match.sgdb_game_id:
                return match.sgdb_game_id
            # Cached no_match — check TTL
            if match.match_method == "no_match":
                age = (
                    utc_now() - match.updated_at
                    if match.updated_at
                    else timedelta(days=999)
                )
                if age <= self.FAILED_MATCH_TTL:
                    return None  # Within TTL, don't retry

        # Cache miss or expired — try platform lookup
        api = self._get_api()
        game_data = api.get_game_by_platform_id(
            store_name, store_app_id, status_callback
        )

        sgdb_id = None
        match_method = "no_match"
        confidence = 0.0

        if game_data:
            sgdb_id = game_data.get("id")
            if sgdb_id:
                match_method = "platform_lookup"
                confidence = 1.0
                db.save_game(
                    sgdb_id=sgdb_id,
                    name=game_data.get("name", ""),
                    release_date=game_data.get("release_date"),
                    verified=game_data.get("verified", False),
                )

        # Fallback: title search
        if sgdb_id is None and normalized_title:
            results = api.search_game(normalized_title, status_callback)
            if results:
                best = results[0]
                sgdb_id = best.get("id")
                if sgdb_id:
                    match_method = "title_search"
                    confidence = 0.8
                    db.save_game(
                        sgdb_id=sgdb_id,
                        name=best.get("name", ""),
                        release_date=best.get("release_date"),
                        verified=best.get("verified", False),
                    )

        # Cache result (even no_match)
        db.save_store_match(
            store_name=store_name,
            store_app_id=str(store_app_id),
            sgdb_game_id=sgdb_id,
            match_method=match_method,
            confidence=confidence,
        )

        return sgdb_id

    # =========================================================================
    # ASSET ATTRIBUTION (AbstractMetadataProvider interface)
    # =========================================================================

    def get_asset_attribution(self, asset_url: str) -> Optional[Dict[str, Any]]:
        """Get attribution info for a SteamGridDB asset by URL.

        Looks up the author name and steam_id from the local asset database.
        """
        if not asset_url:
            return None
        try:
            db = self._get_db()
            info = db.get_author_info_by_url(asset_url)
            if info:
                result = {"author": info[0]}
                if info[1]:
                    result["steam_id"] = info[1]
                return result
            return None
        except Exception:
            logger.debug("Failed to look up asset attribution", exc_info=True)
            return None

    def adjust_author_score(self, author_name: str, delta: int) -> bool:
        """Adjust score for a SteamGridDB asset author.

        Updates or creates the author entry in plugin settings.
        """
        try:
            author_data = self._get_author_data()
            name_lower = author_name.lower()

            if name_lower in author_data:
                author_data[name_lower]["score"] += delta
            else:
                author_data[name_lower] = {
                    "score": delta,
                    "steam_id": "",
                    "hits": 0,
                }

            self._settings["author_scores"] = author_data
            return True
        except Exception:
            logger.error("Failed to adjust author score", exc_info=True)
            return False

    # =========================================================================
    # AUTHOR SCORES
    # =========================================================================

    def _get_author_scores(self) -> dict:
        """Get author scores dict from settings. Keys are lowercase.

        Returns {name_lower: score_int} for the DB layer (unchanged interface).
        Also handles migration from old formats.
        """
        data = self._get_author_data()
        return {name: entry["score"] for name, entry in data.items()}

    def _get_author_data(self) -> dict:
        """Get full author data dict from settings. Keys are lowercase.

        Returns:
            {name_lower: {"score": int, "steam_id": str, "hits": int}}

        Handles migration from:
        1. Old author_blacklist/author_preferred lists
        2. Old flat {name: score_int} format
        3. New {name: {score, steam_id, hits}} format
        """
        raw = self.get_setting("author_scores", None)

        # Migration path 1: no author_scores → check old lists
        if raw is None:
            old_blacklist = self.get_setting("author_blacklist", [])
            old_preferred = self.get_setting("author_preferred", [])
            if old_blacklist or old_preferred:
                data = {}
                if isinstance(old_blacklist, list):
                    for name in old_blacklist:
                        data[str(name).lower()] = {
                            "score": -10, "steam_id": "", "hits": 0,
                        }
                if isinstance(old_preferred, list):
                    for name in old_preferred:
                        data[str(name).lower()] = {
                            "score": 10, "steam_id": "", "hits": 0,
                        }
                logger.info(
                    f"SteamGridDB: migrated {len(old_blacklist or [])} blacklisted + "
                    f"{len(old_preferred or [])} preferred authors to author_scores"
                )
                return data
            return {}

        if not isinstance(raw, dict):
            return {}

        result = {}
        for k, v in raw.items():
            name = str(k).lower()
            if isinstance(v, dict):
                # New format: {score, steam_id, hits}
                score = v.get("score", 0)
                if not isinstance(score, (int, float)):
                    continue
                result[name] = {
                    "score": int(score),
                    "steam_id": str(v.get("steam_id", "") or ""),
                    "hits": int(v.get("hits", 0)),
                }
            elif isinstance(v, (int, float)):
                # Migration path 2: old flat {name: score_int}
                result[name] = {
                    "score": int(v),
                    "steam_id": "",
                    "hits": 0,
                }
            # else: skip invalid entries
        return result

    def _record_author_hit(self, author_name: str, steam_id: str = "") -> None:
        """Record a hit for an author whose rule fired.

        Increments the hits counter. Opportunistically fills in steam_id
        if the entry had an empty one and we got one from the API.

        Args:
            author_name: Author name (case-insensitive matching)
            steam_id: Optional Steam64 ID from the API response
        """
        raw = self.get_setting("author_scores", None)
        if not isinstance(raw, dict):
            return

        name_lower = author_name.lower()

        # Find matching key (case-insensitive)
        match_key = None
        for k in raw:
            if str(k).lower() == name_lower:
                match_key = k
                break

        if match_key is None:
            return

        entry = raw[match_key]
        if isinstance(entry, dict):
            entry["hits"] = entry.get("hits", 0) + 1
            # Always update steam_id from SGDB API — it's authoritative
            # (vanity URL resolution may have set a wrong one)
            if steam_id:
                entry["steam_id"] = steam_id
        elif isinstance(entry, (int, float)):
            # Migrate flat entry in-place
            raw[match_key] = {
                "score": int(entry),
                "steam_id": steam_id or "",
                "hits": 1,
            }

    # =========================================================================
    # ASSET FETCHING
    # =========================================================================

    def _fetch_and_cache_heroes(
        self,
        sgdb_game_id: int,
        status_callback: ProgressCallback = None,
    ) -> int:
        """Fetch heroes from API and cache top 5

        Returns:
            Number of heroes cached
        """
        api = self._get_api()
        db = self._get_db()

        # Build filter params from user choice settings
        style_api = _style_to_api(self.get_setting("hero_style", "Alternate"))
        nsfw_api = NSFW_FILTER_MAP.get(
            self.get_setting("nsfw_filter", "Exclude"), "false"
        )
        humor_api = HUMOR_FILTER_MAP.get(
            self.get_setting("humor_filter", "Exclude"), "false"
        )
        allow_animated = self.get_setting("allow_animated", False)
        types_filter = None if allow_animated else ["static"]

        heroes = api.get_heroes(
            sgdb_game_id,
            styles=[style_api] if style_api else None,
            nsfw=nsfw_api,
            humor=humor_api,
            types=types_filter,
            status_callback=status_callback,
        )

        if heroes:
            return db.save_assets(sgdb_game_id, "hero", heroes)

        # If style filter gave no results, try without style
        if style_api:
            heroes = api.get_heroes(
                sgdb_game_id,
                nsfw=nsfw_api,
                humor=humor_api,
                types=types_filter,
                status_callback=status_callback,
            )
            if heroes:
                return db.save_assets(sgdb_game_id, "hero", heroes)

        return 0

    def _get_best_hero_url(self, sgdb_game_id: int) -> Optional[str]:
        """Get best hero URL from cache, respecting user preferences"""
        db = self._get_db()
        style_api = _style_to_api(self.get_setting("hero_style", "Alternate"))
        nsfw_setting = self.get_setting("nsfw_filter", "Exclude")
        humor_setting = self.get_setting("humor_filter", "Exclude")
        allow_animated = self.get_setting("allow_animated", False)

        # Map choice values to allow_* booleans for DB filtering
        allow_nsfw = nsfw_setting != "Exclude"
        allow_humor = humor_setting != "Exclude"

        author_scores = self._get_author_scores()
        blocked_hits: Dict[str, int] = {}
        asset = db.get_best_asset(
            sgdb_game_id,
            "hero",
            style=style_api,
            allow_nsfw=allow_nsfw,
            allow_humor=allow_humor,
            allow_animated=allow_animated,
            author_scores=author_scores,
            blocked_hits=blocked_hits,
        )
        # Record hit for selected author (positive/neutral rule fired)
        if asset and asset.author_name and author_scores:
            if asset.author_name.lower() in author_scores:
                self._record_author_hit(
                    asset.author_name,
                    steam_id=asset.author_steam_id or "",
                )
        # Record hits for blocked authors whose assets were filtered out
        for name in blocked_hits:
            self._record_author_hit(name)
        return asset.url if asset else None

    def _fetch_and_cache_grids(
        self,
        sgdb_game_id: int,
        status_callback: ProgressCallback = None,
    ) -> int:
        """Fetch grids (covers) from API and cache top 5

        Returns:
            Number of grids cached
        """
        api = self._get_api()
        db = self._get_db()

        # Build filter params from user settings
        style_api = _style_to_api(self.get_setting("grid_style", "Alternate"))
        nsfw_api = NSFW_FILTER_MAP.get(
            self.get_setting("nsfw_filter", "Exclude"), "false"
        )
        humor_api = HUMOR_FILTER_MAP.get(
            self.get_setting("humor_filter", "Exclude"), "false"
        )
        allow_animated = self.get_setting("allow_animated", False)
        types_filter = None if allow_animated else ["static"]

        # Prefer vertical cover dimensions (600x900, 342x482, 660x930)
        dimensions = ["600x900", "660x930", "342x482"]

        grids = api.get_grids(
            sgdb_game_id,
            styles=[style_api] if style_api else None,
            dimensions=dimensions,
            nsfw=nsfw_api,
            humor=humor_api,
            types=types_filter,
            status_callback=status_callback,
        )

        if grids:
            return db.save_assets(sgdb_game_id, "grid", grids)

        # If style filter gave no results, try without style
        if style_api:
            grids = api.get_grids(
                sgdb_game_id,
                dimensions=dimensions,
                nsfw=nsfw_api,
                humor=humor_api,
                types=types_filter,
                status_callback=status_callback,
            )
            if grids:
                return db.save_assets(sgdb_game_id, "grid", grids)

        # If dimension filter gave no results, try any dimension
        grids = api.get_grids(
            sgdb_game_id,
            nsfw=nsfw_api,
            humor=humor_api,
            types=types_filter,
            status_callback=status_callback,
        )
        if grids:
            return db.save_assets(sgdb_game_id, "grid", grids)

        return 0

    def _get_best_grid_url(self, sgdb_game_id: int) -> Optional[str]:
        """Get best grid (cover) URL from cache, respecting user preferences"""
        db = self._get_db()
        style_api = _style_to_api(self.get_setting("grid_style", "Alternate"))
        nsfw_setting = self.get_setting("nsfw_filter", "Exclude")
        humor_setting = self.get_setting("humor_filter", "Exclude")
        allow_animated = self.get_setting("allow_animated", False)

        # Map choice values to allow_* booleans for DB filtering
        allow_nsfw = nsfw_setting != "Exclude"
        allow_humor = humor_setting != "Exclude"

        author_scores = self._get_author_scores()
        blocked_hits: Dict[str, int] = {}
        asset = db.get_best_asset(
            sgdb_game_id,
            "grid",
            style=style_api,
            allow_nsfw=allow_nsfw,
            allow_humor=allow_humor,
            allow_animated=allow_animated,
            author_scores=author_scores,
            blocked_hits=blocked_hits,
        )
        # Record hit for selected author (positive/neutral rule fired)
        if asset and asset.author_name and author_scores:
            if asset.author_name.lower() in author_scores:
                self._record_author_hit(
                    asset.author_name,
                    steam_id=asset.author_steam_id or "",
                )
        # Record hits for blocked authors whose assets were filtered out
        for name in blocked_hits:
            self._record_author_hit(name)
        return asset.url if asset else None

    # =========================================================================
    # LOCAL RESELECTION (after author score changes)
    # =========================================================================

    def get_blocked_author_asset_urls(self) -> set:
        """Get all cached asset URLs from currently blocked authors.

        Blocked = author score < 0.
        Returns set of URLs (both full-size and thumbnails).
        """
        scores = self._get_author_scores()
        blocked = [name for name, score in scores.items() if score < 0]
        if not blocked:
            return set()
        db = self._get_db()
        return set(db.get_asset_urls_by_authors(blocked))

    def reselect_cached_assets(
        self, store_name: str, store_app_id: str
    ) -> Optional[Dict[str, Any]]:
        """Re-evaluate cached SGDB assets with current author scores.

        Purely local — queries the SGDB DB only, zero API calls.
        Used after author score changes to re-select best cover/hero.
        """
        db = self._get_db()

        # Local cache lookup only — no API call
        match = db.get_store_match(store_name, str(store_app_id))
        if not match or not match.sgdb_game_id:
            return None

        sgdb_id = match.sgdb_game_id
        hero_url = self._get_best_hero_url(sgdb_id)
        cover_url = self._get_best_grid_url(sgdb_id)

        result = {}
        if hero_url:
            result["hero"] = hero_url
        if cover_url:
            result["cover"] = cover_url

        return result if result else None

    # =========================================================================
    # SEARCH (AbstractMetadataProvider interface)
    # =========================================================================

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Search for games by title

        Uses the SteamGridDB autocomplete search endpoint.
        """
        try:
            api = self._get_api()
        except SgdbAuthError:
            return []

        results = api.search_game(title)

        search_results = []
        for game in results:
            game_id = game.get("id")
            if not game_id:
                continue

            # Parse release_date (Unix timestamp) to year
            release_year = None
            release_ts = game.get("release_date")
            if release_ts and isinstance(release_ts, (int, float)):
                try:
                    release_year = utc_from_timestamp(release_ts).year
                except (ValueError, OSError):
                    pass

            search_results.append(
                MetadataSearchResult(
                    provider_id=str(game_id),
                    title=game.get("name", ""),
                    release_year=release_year,
                    platforms=[],
                    cover_url=None,  # SteamGridDB search doesn't return covers
                    confidence=0.8,
                )
            )

        return search_results

    # =========================================================================
    # ENRICHMENT (main entry point during sync)
    # =========================================================================

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a game — hero banner and cover URLs"""
        try:
            sgdb_id = int(provider_id)
        except (ValueError, TypeError):
            return None

        db = self._get_db()

        # Try hero cache first
        hero_url = self._get_best_hero_url(sgdb_id)
        if not hero_url:
            # Check if we've already tried fetching (empty result cached)
            if not db.has_assets(sgdb_id, "hero"):
                self._fetch_and_cache_heroes(sgdb_id)
                hero_url = self._get_best_hero_url(sgdb_id)

        # Try grid (cover) cache
        cover_url = self._get_best_grid_url(sgdb_id)
        if not cover_url:
            if not db.has_assets(sgdb_id, "grid"):
                self._fetch_and_cache_grids(sgdb_id)
                cover_url = self._get_best_grid_url(sgdb_id)

        if not hero_url and not cover_url:
            return None

        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=str(sgdb_id),
            background_url=hero_url,
            cover_url=cover_url,
        )

    async def enrich_games(
        self,
        games: List[Game],
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        cross_store_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Batch enrichment during store sync

        Three-phase process:
        1. Platform lookup for all games (grouped by store)
        2. Title search fallback for unmatched (respect TTL)
        3. Fetch heroes for all matched games

        Cancellation: Partial results are returned with whatever was processed.
        Data is persisted to steamgriddb.db incrementally, so cancelled syncs
        can be resumed without re-fetching already cached data.
        """
        if not games:
            return {}

        try:
            api = self._get_api()
        except SgdbAuthError:
            logger.warning("SteamGridDB: API key not configured, skipping enrichment")
            return {}

        # Reset API cancel state for this enrichment run
        api.reset_cancel()

        db = self._get_db()
        total = len(games)

        def _check_cancelled() -> bool:
            if cancel_check and cancel_check():
                api.cancel()  # Wake any sleeping API waits
                return True
            return False

        # Helper to build results from current game_to_sgdb mapping
        def build_results_for_matched(
            game_list: List[Game],
            sgdb_mapping: Dict[str, int],
        ) -> Dict[str, EnrichmentData]:
            """Build EnrichmentData for games that have been matched and cached."""
            partial_results: Dict[str, EnrichmentData] = {}
            for g in game_list:
                sgdb_id = sgdb_mapping.get(g.store_app_id)
                if sgdb_id:
                    hero_url = self._get_best_hero_url(sgdb_id)
                    cover_url = self._get_best_grid_url(sgdb_id)
                    if hero_url or cover_url:
                        partial_results[g.store_app_id] = EnrichmentData(
                            provider_name=self.provider_name,
                            provider_id=str(sgdb_id),
                            background_url=hero_url,
                            cover_url=cover_url,
                        )
            return partial_results

        # Phase 1: Platform lookups (grouped by store)
        game_to_sgdb: Dict[str, int] = {}  # store_app_id → sgdb_game_id
        unmatched: List[Game] = []
        try:
            return self._do_enrich_games(
                games, api, db, total, game_to_sgdb, unmatched,
                build_results_for_matched, _check_cancelled,
                status_callback, cancel_check,
            )
        except SgdbCancelledError:
            logger.info("SteamGridDB enrichment interrupted by cancellation")
            return build_results_for_matched(games, game_to_sgdb)

    def _do_enrich_games(
        self,
        games,
        api,
        db,
        total,
        game_to_sgdb,
        unmatched,
        build_results_for_matched,
        _check_cancelled,
        status_callback,
        cancel_check,
    ):
        """Inner enrichment logic, separated for SgdbCancelledError handling."""
        processed = 0

        if status_callback:
            status_callback("SteamGridDB: Looking up games...", 0, total)

        for game in games:
            if _check_cancelled():
                # Build results from whatever was matched so far
                logger.info(f"SteamGridDB: Cancelled during lookup, returning {len(game_to_sgdb)} matches")
                return build_results_for_matched(games, game_to_sgdb)

            # Check cache first (no API call)
            match = db.get_store_match(game.store_name, game.store_app_id)
            if match is not None:
                if match.sgdb_game_id:
                    game_to_sgdb[game.store_app_id] = match.sgdb_game_id
                else:
                    # Cached no_match — check if expired
                    age = (
                        utc_now() - match.updated_at
                        if match.updated_at
                        else timedelta(days=999)
                    )
                    if age > self.FAILED_MATCH_TTL:
                        unmatched.append(game)
                    # else: within TTL, skip
                processed += 1
                continue

            # Cache miss — do platform lookup
            game_data = api.get_game_by_platform_id(game.store_name, game.store_app_id)

            if game_data and game_data.get("id"):
                sgdb_id = game_data["id"]
                game_to_sgdb[game.store_app_id] = sgdb_id
                db.save_game(
                    sgdb_id=sgdb_id,
                    name=game_data.get("name", ""),
                    release_date=game_data.get("release_date"),
                    verified=game_data.get("verified", False),
                )
                db.save_store_match(
                    store_name=game.store_name,
                    store_app_id=game.store_app_id,
                    sgdb_game_id=sgdb_id,
                    match_method="platform_lookup",
                    confidence=1.0,
                )
            else:
                # No platform match — queue for title search
                unmatched.append(game)
                db.save_store_match(
                    store_name=game.store_name,
                    store_app_id=game.store_app_id,
                    sgdb_game_id=None,
                    match_method="no_match",
                    confidence=0.0,
                )

            processed += 1
            if status_callback and processed % 5 == 0:
                status_callback(
                    f"SteamGridDB: Lookup ({processed}/{total}, "
                    f"{len(game_to_sgdb)} matched)...",
                    processed, total,
                )

        # Phase 2: Title search fallback for unmatched
        if unmatched and not _check_cancelled():
            if status_callback:
                status_callback(
                    f"SteamGridDB: Title search for {len(unmatched)} unmatched...",
                    processed, total,
                )

            for i, game in enumerate(unmatched):
                if _check_cancelled():
                    break

                title = getattr(game, "title", "")
                if not title:
                    continue

                if status_callback and i % 5 == 0:
                    status_callback(
                        f"SteamGridDB: Title search ({i + 1}/{len(unmatched)}): "
                        f"{title[:40]}...",
                        processed + i, total,
                    )

                search_results = api.search_game(title)
                if search_results:
                    best = search_results[0]
                    sgdb_id = best.get("id")
                    if sgdb_id:
                        game_to_sgdb[game.store_app_id] = sgdb_id
                        db.save_game(
                            sgdb_id=sgdb_id,
                            name=best.get("name", ""),
                            release_date=best.get("release_date"),
                            verified=best.get("verified", False),
                        )
                        db.save_store_match(
                            store_name=game.store_name,
                            store_app_id=game.store_app_id,
                            sgdb_game_id=sgdb_id,
                            match_method="title_search",
                            confidence=0.8,
                        )

        # Phase 3: Fetch heroes and grids for all matched games
        if _check_cancelled():
            # Build results from whatever was matched so far
            logger.info(f"SteamGridDB: Cancelled before asset fetch, returning {len(game_to_sgdb)} matches")
            return build_results_for_matched(games, game_to_sgdb)

        unique_sgdb_ids = set(game_to_sgdb.values())

        # Filter to only IDs that don't already have cached heroes
        ids_needing_heroes = [
            sid for sid in unique_sgdb_ids
            if not db.has_assets(sid, "hero")
        ]

        # Filter to only IDs that don't already have cached grids (covers)
        ids_needing_grids = [
            sid for sid in unique_sgdb_ids
            if not db.has_assets(sid, "grid")
        ]

        # Fetch heroes
        if ids_needing_heroes:
            if status_callback:
                status_callback(
                    f"SteamGridDB: Fetching heroes for {len(ids_needing_heroes)} games...",
                    0, len(ids_needing_heroes),
                )

            for i, sgdb_id in enumerate(ids_needing_heroes):
                if _check_cancelled():
                    break

                self._fetch_and_cache_heroes(sgdb_id)

                if status_callback and i % 5 == 0:
                    status_callback(
                        f"SteamGridDB: Fetching heroes ({i + 1}/{len(ids_needing_heroes)})...",
                        i + 1, len(ids_needing_heroes),
                    )

        # Fetch grids (covers)
        if ids_needing_grids and not _check_cancelled():
            if status_callback:
                status_callback(
                    f"SteamGridDB: Fetching covers for {len(ids_needing_grids)} games...",
                    0, len(ids_needing_grids),
                )

            for i, sgdb_id in enumerate(ids_needing_grids):
                if _check_cancelled():
                    break

                self._fetch_and_cache_grids(sgdb_id)

                if status_callback and i % 5 == 0:
                    status_callback(
                        f"SteamGridDB: Fetching covers ({i + 1}/{len(ids_needing_grids)})...",
                        i + 1, len(ids_needing_grids),
                    )

        # Build enrichment results (include both hero and cover)
        results = build_results_for_matched(games, game_to_sgdb)

        if status_callback:
            status_callback(
                f"SteamGridDB: Enriched {len(results)}/{total} games",
                total, total,
            )

        logger.info(
            f"SteamGridDB: Enriched {len(results)}/{total} games "
            f"({len(game_to_sgdb)} matched, {len(ids_needing_heroes)} heroes, "
            f"{len(ids_needing_grids)} covers fetched)"
        )
        return results

    # =========================================================================
    # ON-DEMAND METADATA (for MetadataResolver)
    # =========================================================================

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Get metadata for a store game on demand

        Checks local cache first, falls back to API if needed.
        Returns hero as background_url and cover as cover_url.
        """
        try:
            sgdb_id = self._lookup_sgdb_id(
                store_name, store_id, normalized_title=normalized_title
            )
            if not sgdb_id:
                return None

            db = self._get_db()

            # Get hero (background)
            hero_url = self._get_best_hero_url(sgdb_id)
            if not hero_url:
                if not db.has_assets(sgdb_id, "hero"):
                    self._fetch_and_cache_heroes(sgdb_id)
                    hero_url = self._get_best_hero_url(sgdb_id)

            # Get grid (cover)
            cover_url = self._get_best_grid_url(sgdb_id)
            if not cover_url:
                if not db.has_assets(sgdb_id, "grid"):
                    self._fetch_and_cache_grids(sgdb_id)
                    cover_url = self._get_best_grid_url(sgdb_id)

            if hero_url or cover_url:
                result = {"steamgriddb_id": sgdb_id}
                if hero_url:
                    result["hero"] = hero_url
                if cover_url:
                    result["cover"] = cover_url
                return result
            return None
        except Exception as e:
            logger.debug(
                f"SteamGridDB on-demand lookup failed for {store_name}/{store_id}: {e}"
            )
            return None

    def get_cache_metadata_bulk(
        self, store_app_ids: Dict[str, List[str]]
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Bulk local-DB query for covers + heroes. No API calls.

        Uses raw SQL to avoid ORM overhead on potentially 100k+ asset rows.
        Queries sgdb_store_matches → sgdb_assets, picks best-scoring
        grid (cover) and hero per game, respecting author scores and
        user filter preferences.

        Args:
            store_app_ids: Dict mapping store_name -> list of app_ids

        Returns:
            Nested dict: {store_name: {app_id: {"cover": url, "hero": url}}}
        """
        from sqlalchemy import text as sa_text

        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        db = self._get_db()

        try:
            store_app_sets = {
                store: set(ids) for store, ids in store_app_ids.items()
            }

            # Load user preferences once
            author_scores = self._get_author_scores()
            nsfw_setting = self.get_setting("nsfw_filter", "Exclude")
            humor_setting = self.get_setting("humor_filter", "Exclude")
            allow_animated = self.get_setting("allow_animated", False)
            allow_nsfw = nsfw_setting != "Exclude"
            allow_humor = humor_setting != "Exclude"

            hero_style = _style_to_api(self.get_setting("hero_style", "Alternate"))
            grid_style = _style_to_api(self.get_setting("grid_style", "Alternate"))

            scores_lower = (
                {k.lower(): v for k, v in author_scores.items()}
                if author_scores else {}
            )

            with db.get_session() as session:
                # Raw SQL: get only matched store entries for owned games
                match_rows = session.execute(sa_text(
                    "SELECT store_name, store_app_id, sgdb_game_id "
                    "FROM sgdb_store_matches "
                    "WHERE sgdb_game_id IS NOT NULL"
                )).fetchall()

                # Filter to owned games in Python (IN clause with 13k+ IDs
                # is slower than full scan + set lookup)
                sgdb_to_stores: Dict[int, List[tuple]] = {}
                for store_name, app_id, sgdb_id in match_rows:
                    if store_name not in store_app_sets:
                        continue
                    if app_id not in store_app_sets[store_name]:
                        continue
                    sgdb_to_stores.setdefault(sgdb_id, []).append(
                        (store_name, app_id)
                    )

                if not sgdb_to_stores:
                    return result

                sgdb_ids = list(sgdb_to_stores.keys())

                # Raw SQL: fetch only needed columns for hero + grid assets
                # Avoids ORM object creation for potentially 100k+ rows.
                # Use json_each for large IN-clause (SQLite can't bind tuples).
                from luducat.core.json_compat import json as _json
                ids_json = _json.dumps(sgdb_ids)
                asset_rows = session.execute(sa_text(
                    "SELECT a.game_id, a.asset_type, a.url, a.score, a.style, "
                    "       a.is_nsfw, a.is_humor, a.is_animated, a.author_name "
                    "FROM sgdb_assets a "
                    "WHERE a.asset_type IN ('hero', 'grid') "
                    "  AND a.game_id IN (SELECT value FROM json_each(:ids))"
                ), {"ids": ids_json}).fetchall()

                # Group by (game_id, asset_type) as lightweight tuples
                grouped: Dict[tuple, list] = {}
                for row in asset_rows:
                    key = (row[0], row[1])  # game_id, asset_type
                    grouped.setdefault(key, []).append(row)

                def _pick_best_raw(
                    candidates: list,
                    style_pref: Optional[str],
                ) -> Optional[str]:
                    """Filter and pick best-scoring asset URL from raw rows.

                    Row format: (game_id, asset_type, url, score, style,
                                 is_nsfw, is_humor, is_animated, author_name)
                    """
                    filtered = []
                    for r in candidates:
                        url, score, style = r[2], r[3] or 0, r[4]
                        is_nsfw, is_humor, is_animated = r[5], r[6], r[7]
                        author = r[8]
                        if not allow_nsfw and is_nsfw:
                            continue
                        if not allow_humor and is_humor:
                            continue
                        if not allow_animated and is_animated:
                            continue
                        if (scores_lower and author
                                and scores_lower.get(author.lower(), 0) < 0):
                            continue
                        filtered.append((url, score, style, author))

                    if not filtered:
                        return None

                    if style_pref:
                        styled = [f for f in filtered if f[2] == style_pref]
                        if styled:
                            filtered = styled

                    def effective_score(item):
                        base = item[1]
                        if scores_lower and item[3]:
                            boost = scores_lower.get(item[3].lower(), 0)
                            if boost > 0:
                                return base + boost
                        return base

                    best = max(filtered, key=effective_score)
                    return best[0]  # url

                for sgdb_id, store_entries in sgdb_to_stores.items():
                    hero_url = _pick_best_raw(
                        grouped.get((sgdb_id, "hero"), []),
                        hero_style,
                    )
                    cover_url = _pick_best_raw(
                        grouped.get((sgdb_id, "grid"), []),
                        grid_style,
                    )

                    if not hero_url and not cover_url:
                        continue

                    meta: Dict[str, Any] = {}
                    if hero_url:
                        meta["hero"] = hero_url
                    if cover_url:
                        meta["cover"] = cover_url

                    for store_name, app_id in store_entries:
                        if store_name not in result:
                            result[store_name] = {}
                        result[store_name][app_id] = meta

                total = sum(len(v) for v in result.values())
                logger.info(
                    f"SteamGridDB cache bulk: {total} store entries from "
                    f"{len(sgdb_to_stores)} SGDB games"
                )

        except Exception as e:
            logger.warning(f"Failed to get bulk cache metadata: {e}")

        return result

    # =========================================================================
    # SYNC OPERATIONS
    # =========================================================================

    def get_sync_stats(self) -> Dict[str, int]:
        """Get match statistics for sync dialog"""
        db = self._get_db()
        return db.get_match_count()

    def sync_failed_matches(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> Dict[str, int]:
        """Retry all failed store matches

        Re-queries entries with match_method='no_match' that have
        expired past FAILED_MATCH_TTL.
        """
        db = self._get_db()
        api = self._get_api()

        # Get expired no_match entries
        session = db.get_session()
        try:
            failed_matches = (
                session.query(SgdbStoreMatch)
                .filter(SgdbStoreMatch.match_method == "no_match")
                .all()
            )
            for m in failed_matches:
                session.expunge(m)
        finally:
            session.close()

        total = len(failed_matches)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        success = 0
        for i, match in enumerate(failed_matches):
            if progress_callback:
                progress_callback(
                    f"SteamGridDB: Retrying {i + 1}/{total}...",
                    i, total, success,
                )

            # Try platform lookup
            game_data = api.get_game_by_platform_id(
                match.store_name, match.store_app_id
            )
            if game_data and game_data.get("id"):
                sgdb_id = game_data["id"]
                db.save_game(
                    sgdb_id=sgdb_id,
                    name=game_data.get("name", ""),
                    release_date=game_data.get("release_date"),
                    verified=game_data.get("verified", False),
                )
                db.save_store_match(
                    store_name=match.store_name,
                    store_app_id=match.store_app_id,
                    sgdb_game_id=sgdb_id,
                    match_method="platform_lookup",
                    confidence=1.0,
                )
                success += 1

        if progress_callback:
            progress_callback(
                f"SteamGridDB: Retry complete — {success} new matches",
                total, total, success,
            )

        logger.info(
            f"SteamGridDB sync: {success} new matches, {total - success} still failed"
        )
        return {"total": total, "success": success, "failed": total - success}

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_enable(self) -> None:
        """Initialize database on enable"""
        self._get_db()
        logger.info("SteamGridDB provider enabled")

    def on_disable(self) -> None:
        """Cleanup on disable"""
        if self._db:
            self._db.close()
            self._db = None
        logger.info("SteamGridDB provider disabled")

    def close(self) -> None:
        """Cleanup on shutdown"""
        if self._api:
            self._api.close()
            self._api = None
        if self._db:
            self._db.close()
            self._db = None
