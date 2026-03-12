# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""ProtonDB Metadata Provider

Implements AbstractMetadataProvider to provide Linux compatibility ratings
from ProtonDB for Steam games.

No authentication required. Provides protondb_rating (tier) and protondb_score.
Only works for Steam games (ProtonDB indexes by Steam AppID).
"""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_now
from luducat.plugins.base import (
    AbstractMetadataProvider,
    EnrichmentData,
    Game,
    MetadataSearchResult,
    RateLimitError,
)

from .api import ProtonDbApi, ProtonDbApiError, ProtonDbRateLimitError
from .database import ProtonDbDatabase

logger = logging.getLogger(__name__)

# Cache TTL: re-check ratings after 14 days
CACHE_TTL_DAYS = 14


class ProtonDbProvider(AbstractMetadataProvider):
    """ProtonDB metadata provider

    Provides Linux/Proton compatibility ratings from ProtonDB.
    Only works for Steam games (ProtonDB uses Steam AppIDs).
    No authentication required - public API.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._api: Optional[ProtonDbApi] = None
        self._db: Optional[ProtonDbDatabase] = None

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def provider_name(self) -> str:
        return "protondb"

    @property
    def display_name(self) -> str:
        return "ProtonDB"

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def _get_api(self) -> ProtonDbApi:
        """Get or create API client"""
        if self._api is None:
            rate_limit = self.get_setting("rate_limit_delay", 0.1)
            self._api = ProtonDbApi(http_client=self.http, rate_limit_delay=rate_limit)
        return self._api

    def _get_db(self) -> ProtonDbDatabase:
        """Get or create database"""
        if self._db is None:
            self._db = ProtonDbDatabase(self.get_database_path())
        return self._db

    # =========================================================================
    # ABSTRACT METHOD IMPLEMENTATIONS
    # =========================================================================

    def on_enable(self) -> None:
        """Called when plugin is enabled.

        Ensures the ProtonDB ratings database exists so that on-demand
        lookups don't fail before the first sync.
        """
        logger.info("ProtonDB plugin enabled")
        try:
            self._get_db()
        except Exception as e:
            logger.debug(f"ProtonDB DB init in on_enable: {e}")

    def is_available(self) -> bool:
        """Always available - no authentication required"""
        return True

    async def authenticate(self) -> bool:
        """No-op - no authentication needed"""
        return True

    def is_authenticated(self) -> bool:
        """Always authenticated - public API"""
        return True

    def get_database_path(self) -> Path:
        return self.data_dir / "protondb.db"

    def get_ratings_bulk(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached ProtonDB ratings for bulk loading at startup.

        Returns:
            Dict mapping steam_app_id -> {"protondb_rating": tier, "protondb_score": score}
        """
        db = self._get_db()
        all_ratings = db.get_all_ratings()
        result = {}
        for app_id, rating in all_ratings.items():
            if rating.tier and rating.tier != "pending":
                result[app_id] = {
                    "protondb_rating": rating.tier,
                    "protondb_score": rating.score,
                }
        return result

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        """Look up ProtonDB data by store ID.

        Only works for Steam games. Returns the Steam AppID as the
        provider ID (ProtonDB is indexed by Steam AppID).
        """
        if store_name != "steam":
            return None
        # ProtonDB uses Steam AppIDs directly
        return store_id

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Not supported - ProtonDB has no title search API"""
        return []

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a game by Steam AppID"""
        db = self._get_db()
        rating = db.get_cached_rating(provider_id)

        if rating is None:
            # Try fetching from API
            try:
                api = self._get_api()
                data = api.get_summary(provider_id)
                if data:
                    db.save_rating(
                        steam_app_id=provider_id,
                        tier=data["tier"],
                        score=data.get("score", 0.0),
                        confidence=data.get("confidence", ""),
                        total_reports=data.get("total", 0),
                        trending_tier=data.get("trending_tier", ""),
                        best_reported_tier=data.get("best_reported_tier", ""),
                    )
                    return self._make_enrichment(
                        provider_id, data["tier"], data.get("score", 0.0)
                    )
                else:
                    db.save_no_match(provider_id)
                    return None
            except ProtonDbRateLimitError:
                logger.warning(f"ProtonDB: Rate limited fetching {provider_id}")
                return None
            except ProtonDbApiError as e:
                logger.warning(f"ProtonDB: Failed to fetch {provider_id}: {e}")
                return None

        return self._make_enrichment(provider_id, rating.tier, rating.score)

    # =========================================================================
    # ENRICHMENT (SYNC)
    # =========================================================================

    async def enrich_games(
        self,
        games: List[Game],
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        cross_store_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Enrich multiple games during store sync.

        Processes Steam games directly (ProtonDB uses Steam AppIDs).
        For non-Steam games, uses cross_store_ids mapping from
        MetadataResolver to look up ratings via their Steam counterpart.
        Skips games already cached within TTL.
        """
        if not games:
            return {}

        db = self._get_db()
        api = self._get_api()

        # Build Steam AppID -> Game mapping
        # For Steam games: direct mapping
        # For non-Steam games: use cross_store_ids from resolver
        steam_id_to_game: Dict[str, Game] = {}
        cross_store_map: Dict[str, str] = {}  # steam_app_id -> original store_app_id

        for game in games:
            if game.store_name == "steam":
                steam_id_to_game[game.store_app_id] = game
            elif cross_store_ids and game.store_app_id in cross_store_ids:
                steam_app_id = cross_store_ids[game.store_app_id]
                if steam_app_id not in steam_id_to_game:
                    steam_id_to_game[steam_app_id] = game
                    cross_store_map[steam_app_id] = game.store_app_id

        if not steam_id_to_game:
            logger.debug("ProtonDB: No Steam-resolvable games to enrich")
            return {}

        # Find which IDs need fetching (not cached or expired)
        all_steam_ids = list(steam_id_to_game.keys())
        cached = db.get_cached_ratings_bulk(all_steam_ids)
        no_match_ids = db.get_no_match_ids(all_steam_ids)
        cutoff = utc_now() - timedelta(days=CACHE_TTL_DAYS)

        uncached_ids = []
        for app_id in all_steam_ids:
            if app_id in no_match_ids:
                continue  # Skip known 404s
            if app_id in cached:
                rating = cached[app_id]
                if rating.fetched_at and rating.fetched_at > cutoff:
                    continue  # Still fresh
            uncached_ids.append(app_id)

        total = len(uncached_ids)
        if total > 0:
            logger.info(f"ProtonDB: Fetching ratings for {total} games")

        # Fetch uncached ratings
        # Cancel check is at the END of each iteration so that parallel
        # plugins always complete at least one unit of work before seeing
        # a cancel flag set by a sibling plugin (all metadata plugins run
        # concurrently via asyncio.gather).
        for i, app_id in enumerate(uncached_ids):
            if status_callback:
                status_callback(f"ProtonDB: {i + 1}/{total}", i + 1, total)

            try:
                data = api.get_summary(app_id)
                if data:
                    db.save_rating(
                        steam_app_id=app_id,
                        tier=data["tier"],
                        score=data.get("score", 0.0),
                        confidence=data.get("confidence", ""),
                        total_reports=data.get("total", 0),
                        trending_tier=data.get("trending_tier", ""),
                        best_reported_tier=data.get("best_reported_tier", ""),
                    )
                    cached[app_id] = type("Rating", (), {
                        "tier": data["tier"],
                        "score": data.get("score", 0.0),
                    })()
                else:
                    db.save_no_match(app_id)
            except ProtonDbRateLimitError as e:
                raise RateLimitError(str(e), wait_seconds=e.wait_seconds) from e
            except ProtonDbApiError as e:
                logger.warning(f"ProtonDB: Failed to fetch {app_id}: {e}")

            # Check cancel AFTER work — ensures partial results are saved
            if cancel_check and cancel_check():
                logger.info(f"ProtonDB: Enrichment cancelled after {i + 1}/{total}")
                break

        # Build enrichment results, keyed by original store_app_id
        all_cached = db.get_cached_ratings_bulk(all_steam_ids)
        results: Dict[str, EnrichmentData] = {}
        for steam_app_id, rating in all_cached.items():
            game = steam_id_to_game.get(steam_app_id)
            if not game:
                continue
            enrichment = self._make_enrichment(
                steam_app_id, rating.tier, rating.score
            )
            # Key by original store_app_id (not steam ID) for cross-store games
            results[game.store_app_id] = enrichment

        cross_count = sum(1 for sid in cross_store_map if cross_store_map[sid] in results)
        if cross_count:
            logger.info(
                f"ProtonDB: Enriched {len(results)} games "
                f"({cross_count} cross-store via resolver)"
            )
        else:
            logger.info(f"ProtonDB: Enriched {len(results)} games")
        return results

    # =========================================================================
    # MANUAL SYNC
    # =========================================================================

    def get_sync_stats(self) -> Dict[str, int]:
        """Get rating statistics for sync dialog

        Returns:
            Dict with counts: {"total": N, "matched": N, "failed": N}
        """
        db = self._get_db()
        all_ratings = db.get_all_ratings()
        all_no_match = db.get_all_no_match()
        matched = len(all_ratings)
        failed = len(all_no_match)
        return {"total": matched + failed, "matched": matched, "failed": failed}

    def sync_failed_matches(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> Dict[str, int]:
        """Retry all no-match entries against ProtonDB API

        Re-queries entries that previously returned 404. Games may have
        received ProtonDB reports since the last sync.

        Args:
            progress_callback: (message, current, total, success_count)

        Returns:
            {"total": N, "success": N, "failed": N}
        """
        db = self._get_db()
        api = self._get_api()

        no_match_entries = db.get_all_no_match()
        total = len(no_match_entries)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        success = 0
        failed = 0

        for i, entry in enumerate(no_match_entries):
            app_id = entry.steam_app_id
            if progress_callback:
                progress_callback(
                    _("ProtonDB: {}").format(app_id), i + 1, total, success
                )

            try:
                data = api.get_summary(app_id)
                if data:
                    db.save_rating(
                        steam_app_id=app_id,
                        tier=data["tier"],
                        score=data.get("score", 0.0),
                        confidence=data.get("confidence", ""),
                        total_reports=data.get("total", 0),
                        trending_tier=data.get("trending_tier", ""),
                        best_reported_tier=data.get("best_reported_tier", ""),
                    )
                    db.delete_no_match(app_id)
                    success += 1
                else:
                    db.save_no_match(app_id)  # Updates timestamp
                    failed += 1
            except ProtonDbRateLimitError as e:
                raise RateLimitError(str(e), wait_seconds=e.wait_seconds) from e
            except ProtonDbApiError as e:
                logger.warning(f"ProtonDB: Failed to fetch {app_id}: {e}")
                failed += 1

        still_failed = total - success
        logger.info(
            f"ProtonDB retry sync: {success} new ratings, {still_failed} still not found"
        )
        return {"total": total, "success": success, "failed": still_failed}

    def sync_refresh_all(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> Dict[str, int]:
        """Refresh all cached ProtonDB ratings

        Re-fetches data for all entries in protondb_ratings.
        Updates ratings and timestamps.

        Args:
            progress_callback: (message, current, total, success_count)

        Returns:
            {"total": N, "success": N, "failed": N}
        """
        db = self._get_db()
        api = self._get_api()

        all_ratings = db.get_all_ratings()
        entries = list(all_ratings.values())
        total = len(entries)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        success = 0
        failed = 0

        for i, rating in enumerate(entries):
            app_id = rating.steam_app_id
            if progress_callback:
                progress_callback(
                    _("ProtonDB: {}").format(app_id), i + 1, total, success
                )

            try:
                data = api.get_summary(app_id)
                if data:
                    db.save_rating(
                        steam_app_id=app_id,
                        tier=data["tier"],
                        score=data.get("score", 0.0),
                        confidence=data.get("confidence", ""),
                        total_reports=data.get("total", 0),
                        trending_tier=data.get("trending_tier", ""),
                        best_reported_tier=data.get("best_reported_tier", ""),
                    )
                    success += 1
                else:
                    # Game removed from ProtonDB (rare)
                    db.save_no_match(app_id)
                    db.delete_rating(app_id)
                    failed += 1
            except ProtonDbRateLimitError as e:
                raise RateLimitError(str(e), wait_seconds=e.wait_seconds) from e
            except ProtonDbApiError as e:
                logger.warning(f"ProtonDB: Failed to fetch {app_id}: {e}")
                failed += 1

        logger.info(
            f"ProtonDB refresh sync: {success} updated, {failed} failed of {total}"
        )
        return {"total": total, "success": success, "failed": failed}

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _make_enrichment(
        self, app_id: str, tier: str, score: float
    ) -> EnrichmentData:
        """Create EnrichmentData from ProtonDB rating"""
        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=app_id,
            extra={
                "protondb_rating": tier,
                "protondb_score": score,
            },
        )
