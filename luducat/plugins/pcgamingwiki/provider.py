# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""PCGamingWiki Metadata Provider

Implements AbstractMetadataProvider to provide game metadata enrichment
from PCGamingWiki via the public Cargo API.

No authentication required. Primary source for game modes.
Phase 1: Game modes (Singleplayer, Multiplayer, Co-op, Local)
Phase 2: Full metadata (genres, DRM, controller support, etc.)
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
)

from .api import PcgwApi, PcgwApiError, ProgressCallback
from .database import PcgwDatabase, PcgwGame, PcgwStoreMatch

logger = logging.getLogger(__name__)

# Re-fetch cached PCGW data after 30 days
CACHE_TTL_DAYS = 30


class PcgwProvider(AbstractMetadataProvider):
    """PCGamingWiki metadata provider

    Provides game metadata from PCGamingWiki via the public Cargo API.
    No authentication required - always available.

    Primary use: Game mode data (Singleplayer, Multiplayer, Co-op, Local).
    Future: Genres, DRM, controller support, engine info.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._api: Optional[PcgwApi] = None
        self._db: Optional[PcgwDatabase] = None

    # =========================================================================
    # PROPERTIES
    # =========================================================================

    @property
    def provider_name(self) -> str:
        return "pcgamingwiki"

    @property
    def display_name(self) -> str:
        return "PCGamingWiki"

    @property
    def store_match_table(self) -> str:
        return "pcgw_store_matches"

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def _get_api(self) -> PcgwApi:
        """Get or create API client"""
        if self._api is None:
            rate_limit = self.get_setting("rate_limit_delay", 0.5)
            self._api = PcgwApi(http_client=self.http, rate_limit_delay=rate_limit)
        return self._api

    def _get_db(self) -> PcgwDatabase:
        """Get or create database"""
        if self._db is None:
            self._db = PcgwDatabase(self.get_database_path())
        return self._db

    # =========================================================================
    # ABSTRACT METHOD IMPLEMENTATIONS
    # =========================================================================

    def is_available(self) -> bool:
        """Always available - no authentication required"""
        return True

    async def authenticate(self) -> bool:
        """No-op - no authentication needed for PCGamingWiki"""
        return True

    def is_authenticated(self) -> bool:
        """Always authenticated - public API"""
        return True

    def get_database_path(self) -> Path:
        return self.data_dir / "pcgamingwiki.db"

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        """Look up PCGW page ID using store ID

        Checks local cache first, falls back to API query.

        Returns:
            PCGW page_id as string, or None if not found
        """
        page_id = self._lookup_page_id(store_name, store_id)
        return str(page_id) if page_id else None

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Not supported - PCGamingWiki Cargo API has no title search

        Returns empty list. Use store ID lookup instead.
        """
        return []

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a game by PCGW page_id"""
        try:
            db = self._get_db()
            game = db.get_game(int(provider_id))
            if game:
                return self._game_to_enrichment(game)
        except Exception as e:
            logger.warning(f"Failed to get enrichment for page_id {provider_id}: {e}")
        return None

    # =========================================================================
    # GAME MODE SUPPORT (PRIMARY CAPABILITY)
    # =========================================================================

    def get_game_modes_bulk(
        self, store_app_ids: Dict[str, List[str]]
    ) -> Dict[str, Dict[str, List[str]]]:
        """Get game modes for multiple store games at once

        Queries local database only (no API calls). Called at startup
        to populate game mode badges.

        Returns normalized mode names matching GAME_MODE_LABELS keys:
        "Single player", "Multiplayer", "Co-operative", "Split screen"

        Args:
            store_app_ids: {"steam": ["440", "730"], "gog": ["1234"]}

        Returns:
            {"steam": {"440": ["Multiplayer"], "730": ["Multiplayer"]}}
        """
        result: Dict[str, Dict[str, List[str]]] = {}
        db = self._get_db()

        for store_name, app_ids in store_app_ids.items():
            if not app_ids:
                continue

            try:
                store_modes = db.get_game_modes_for_store_ids(store_name, app_ids)
                if store_modes:
                    result[store_name] = store_modes
            except Exception as e:
                logger.warning(
                    f"PCGamingWiki: Failed to get game modes for {store_name}: {e}"
                )

        return result

    # =========================================================================
    # ENRICHMENT (SYNC)
    # =========================================================================

    def resolve_steam_app_ids(
        self, store_name: str, app_ids: List[str]
    ) -> Dict[str, tuple]:
        """Resolve Steam AppIDs for non-Steam games using PCGW data.

        Pure local database query, no API calls.

        Args:
            store_name: Source store ("gog", "epic")
            app_ids: List of store app IDs

        Returns:
            Dict mapping store_app_id -> (steam_app_id, reference_title)
        """
        db = self._get_db()
        return db.get_steam_ids_for_store(store_name, app_ids)

    def resolve_cross_store_id(
        self,
        source_store: str,
        source_app_id: str,
        target_store: str,
        normalized_title: str = "",
    ) -> tuple:
        """Find target store's app_id using PCGW's cached cross-store fields.

        Only supports Steam and GOG targets (PCGW stores steam_app_id, gog_id).

        Args:
            source_store: Store the game is known in
            source_app_id: App ID in the source store
            target_store: Store to resolve to ("steam" or "gog")
            normalized_title: Unused (kept for interface consistency)

        Returns:
            Tuple of (target_app_id, reference_title) or (None, None)
        """
        if target_store not in ("steam", "gog"):
            return None, None

        db = self._get_db()
        match = db.get_store_match(source_store, source_app_id)
        if not match or not match.pcgw_page_id:
            return None, None

        game = db.get_game(match.pcgw_page_id)
        if not game:
            return None, None

        if target_store == "steam" and game.steam_app_id:
            return game.steam_app_id.split(",")[0].strip(), game.page_name
        elif target_store == "gog" and game.gog_id:
            return game.gog_id, game.page_name
        return None, None

    async def enrich_games(
        self,
        games: List[Game],
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        cross_store_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Enrich multiple games during store sync

        Groups games by store, batch-queries uncached games via
        PCGamingWiki Cargo API, stores results in local DB.

        Args:
            games: List of Game objects from store plugins
            status_callback: Progress callback (message, current, total)
            cancel_check: Optional callback that returns True if cancelled

        Returns:
            Dict mapping store_app_id -> EnrichmentData
        """
        if not games:
            return {}

        db = self._get_db()
        api = self._get_api()
        total = len(games)

        # Phase 1: Group games by store
        steam_games: Dict[str, Game] = {}
        gog_games: Dict[str, Game] = {}

        for game in games:
            if game.store_name == "steam":
                steam_games[game.store_app_id] = game
            elif game.store_name == "gog":
                gog_games[game.store_app_id] = game
            # Epic: no direct ID mapping in PCGamingWiki

        # Phase 2: Find uncached or stale IDs
        cutoff = utc_now() - timedelta(days=CACHE_TTL_DAYS)

        def _needs_fetch(store: str, app_id: str) -> bool:
            match = db.get_store_match(store, app_id)
            if match is None:
                return True
            if match.updated_at and match.updated_at < cutoff:
                return True  # Stale — older than TTL
            return False

        uncached_steam = [
            sid for sid in steam_games if _needs_fetch("steam", sid)
        ]
        uncached_gog = [
            gid for gid in gog_games if _needs_fetch("gog", gid)
        ]

        # Phase 3: Batch fetch uncached Steam games
        # Cancel is checked AFTER each phase so that parallel plugins
        # (running via asyncio.gather) always complete their current batch
        # before a sibling's cancel flag stops them.
        if uncached_steam:
            if status_callback:
                status_callback(
                    f"PCGamingWiki: Looking up {len(uncached_steam)} Steam games...",
                    0, total,
                )

            try:
                api_results = api.lookup_by_steam_ids_batch(
                    uncached_steam,
                    status_callback=status_callback,
                )
                self._store_api_results("steam", api_results, uncached_steam, db)
            except Exception as e:
                logger.warning(f"PCGamingWiki: Steam batch lookup failed: {e}")

        # Phase 4: Batch fetch uncached GOG games
        if uncached_gog and not (cancel_check and cancel_check()):
            if status_callback:
                status_callback(
                    f"PCGamingWiki: Looking up {len(uncached_gog)} GOG games...",
                    0, total,
                )

            try:
                api_results = api.lookup_by_gog_ids_batch(
                    uncached_gog,
                    status_callback=status_callback,
                )
                self._store_api_results("gog", api_results, uncached_gog, db)
            except Exception as e:
                logger.warning(f"PCGamingWiki: GOG batch lookup failed: {e}")

        # Phase 5: Build enrichment results from local DB
        results: Dict[str, EnrichmentData] = {}
        for game in games:
            match = db.get_store_match(game.store_name, game.store_app_id)
            if match and match.pcgw_page_id:
                pcgw_game = db.get_game(match.pcgw_page_id)
                if pcgw_game:
                    enrichment = self._game_to_enrichment(pcgw_game)
                    if enrichment:
                        results[game.store_app_id] = enrichment

        if status_callback:
            status_callback(
                f"PCGamingWiki: Enriched {len(results)} games",
                total, total,
            )

        logger.info(f"PCGamingWiki: Enriched {len(results)}/{total} games")
        return results

    def _store_api_results(
        self,
        store_name: str,
        api_results: Dict[str, Dict[str, Any]],
        all_ids: List[str],
        db: PcgwDatabase,
    ) -> None:
        """Store API lookup results in local database

        Saves both matched games and no_match entries to avoid
        repeated lookups for games not in PCGamingWiki.
        """
        session = db.get_session()
        try:
            for store_id in all_ids:
                data = api_results.get(store_id)
                if data:
                    page_id_raw = data.get("pageID")
                    if page_id_raw:
                        try:
                            page_id = int(page_id_raw)
                        except (ValueError, TypeError):
                            continue

                        # Save game + multiplayer data
                        db.save_game_from_api(data, session=session)

                        # Save store match
                        db.save_store_match(
                            store_name=store_name,
                            store_app_id=store_id,
                            pcgw_page_id=page_id,
                            pcgw_page_name=data.get("pageName"),
                            match_method="store_id",
                            session=session,
                        )
                else:
                    # No match found - cache to avoid re-querying
                    db.save_store_match(
                        store_name=store_name,
                        store_app_id=store_id,
                        pcgw_page_id=None,
                        match_method="no_match",
                        confidence=0.0,
                        session=session,
                    )

            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"PCGamingWiki: Failed to store API results: {e}")
        finally:
            session.close()

    # =========================================================================
    # ON-DEMAND METADATA
    # =========================================================================

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Get metadata for a store game on demand

        Checks local cache first, falls back to API if needed.
        """
        db = self._get_db()

        # Check cache
        match = db.get_store_match(store_name, store_id)
        if match is not None:
            if match.pcgw_page_id:
                game = db.get_game(match.pcgw_page_id)
                if game:
                    return self._game_to_metadata_dict(game)
            return None  # Cached no_match

        # Not cached - try API lookup
        page_id = self._lookup_page_id(store_name, store_id)
        if page_id:
            game = db.get_game(page_id)
            if game:
                return self._game_to_metadata_dict(game)

        return None

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _lookup_page_id(
        self, store_name: str, store_id: str
    ) -> Optional[int]:
        """Look up PCGW page_id for a store game

        Checks local DB first, then queries API.
        """
        db = self._get_db()

        # Check cache
        match = db.get_store_match(store_name, store_id)
        if match is not None:
            return match.pcgw_page_id

        # API lookup
        api = self._get_api()
        try:
            if store_name == "steam":
                data = api.lookup_by_steam_id(store_id)
            elif store_name == "gog":
                data = api.lookup_by_gog_id(store_id)
            else:
                return None

            if data:
                page_id_raw = data.get("pageID")
                if page_id_raw:
                    page_id = int(page_id_raw)
                    db.save_game_from_api(data)
                    db.save_store_match(
                        store_name=store_name,
                        store_app_id=store_id,
                        pcgw_page_id=page_id,
                        pcgw_page_name=data.get("pageName"),
                        match_method="store_id",
                    )
                    return page_id
            else:
                # Cache no_match
                db.save_store_match(
                    store_name=store_name,
                    store_app_id=store_id,
                    pcgw_page_id=None,
                    match_method="no_match",
                    confidence=0.0,
                )
        except PcgwApiError as e:
            logger.warning(f"PCGamingWiki API error looking up {store_name}/{store_id}: {e}")

        return None

    def _game_to_enrichment(self, game: PcgwGame) -> EnrichmentData:
        """Convert PcgwGame to EnrichmentData format"""
        from .database import _normalize_game_modes

        # Parse comma-delimited fields
        genres = _split_field(game.genres)
        developers = _split_field(game.developers)
        publishers = _split_field(game.publishers)

        # Build game modes from detailed multiplayer data
        mp = game.multiplayer
        game_modes = _normalize_game_modes(
            basic_modes=game.modes,
            local=mp.local if mp else None,
            local_modes=mp.local_modes if mp else None,
            lan=mp.lan if mp else None,
            lan_modes=mp.lan_modes if mp else None,
            online=mp.online if mp else None,
            online_modes=mp.online_modes if mp else None,
        )

        # Parse extended metadata fields
        themes = _split_field(game.themes)
        perspectives = _split_field(game.perspectives)
        platforms = _split_field(game.available_on)
        engines = _split_field(game.engines)
        engine = engines[0] if engines else None

        # Build extra data dict for provider-specific fields
        extra: Dict[str, Any] = {
            "game_modes": game_modes,
        }
        if mp:
            if mp.crossplay and mp.crossplay.lower() in ("true", "yes", "always on"):
                extra["crossplay"] = True
                if mp.crossplay_platforms:
                    extra["crossplay_platforms"] = mp.crossplay_platforms
            if mp.online_players:
                extra["online_players"] = mp.online_players
            if mp.local_players:
                extra["local_players"] = mp.local_players
            if mp.lan_players:
                extra["lan_players"] = mp.lan_players

        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=str(game.page_id),
            genres=genres,
            tags=[],  # PCGW themes are not tags in IGDB sense
            franchise=None,
            series=game.series,
            developers=developers,
            publishers=publishers,
            summary=None,  # PCGW doesn't have summaries
            storyline=None,
            release_date=game.released_windows,
            cover_url=game.cover_url,
            screenshots=[],
            themes=themes,
            platforms=platforms,
            perspectives=perspectives,
            engine=engine,
            extra=extra,
        )

    def _game_to_metadata_dict(self, game: PcgwGame) -> Dict[str, Any]:
        """Convert PcgwGame to standard metadata dict format"""
        from .database import _normalize_game_modes

        mp = game.multiplayer
        game_modes = _normalize_game_modes(
            basic_modes=game.modes,
            local=mp.local if mp else None,
            local_modes=mp.local_modes if mp else None,
            lan=mp.lan if mp else None,
            lan_modes=mp.lan_modes if mp else None,
            online=mp.online if mp else None,
            online_modes=mp.online_modes if mp else None,
        )

        engines = _split_field(game.engines)

        # Build multiplayer details dict
        multiplayer_details = {}
        if mp:
            multiplayer_details = {
                "local": mp.local,
                "local_players": mp.local_players,
                "lan": mp.lan,
                "lan_players": mp.lan_players,
                "online": mp.online,
                "online_players": mp.online_players,
                "asynchronous": getattr(mp, "asynchronous", None),
            }

        # Crossplay info
        crossplay = None
        crossplay_platforms = []
        if mp:
            crossplay = getattr(mp, "crossplay", None)
            crossplay_platforms = _split_field(getattr(mp, "crossplay_platforms", "")) if hasattr(mp, "crossplay_platforms") else []

        # Controller support (normalize true/false/limited/hackable/unknown)
        controller_support = getattr(game, "controller_support", None)
        full_controller_support = getattr(game, "full_controller_support", None)

        # Build per-platform release_date dict
        release_dates_dict: Dict[str, str] = {}
        if game.released_windows:
            release_dates_dict["windows"] = game.released_windows

        return {
            "title": game.page_name,  # PCGamingWiki page name as title
            "cover": game.cover_url,
            "release_date": release_dates_dict if release_dates_dict else (game.released_windows or ""),
            "developers": _split_field(game.developers),
            "publishers": _split_field(game.publishers),
            "genres": _split_field(game.genres),
            "game_modes": game_modes,
            "themes": _split_field(game.themes),
            "platforms": _split_field(game.available_on),
            "perspectives": _split_field(game.perspectives),
            "pacing": _split_field(getattr(game, "pacing", "")) if hasattr(game, "pacing") else [],
            "controls": _split_field(getattr(game, "controls", "")) if hasattr(game, "controls") else [],
            "art_styles": _split_field(getattr(game, "art_styles", "")) if hasattr(game, "art_styles") else [],
            "engine": engines[0] if engines else "",
            "engines": engines,
            "series": _split_field(getattr(game, "series", "")) if hasattr(game, "series") else [],
            "monetization": _split_field(getattr(game, "monetization", "")) if hasattr(game, "monetization") else [],
            "microtransactions": _split_field(getattr(game, "microtransactions", "")) if hasattr(game, "microtransactions") else [],
            "game_modes_detail": multiplayer_details if multiplayer_details else None,
            "crossplay": crossplay,
            "crossplay_platforms": crossplay_platforms,
            # Controller/Input support (field names match PCGW Cargo schema)
            "controller_support": controller_support,
            "full_controller_support": full_controller_support,
            "controller_remapping": getattr(game, "controller_remapping", None),
            "controller_sensitivity": getattr(game, "controller_sensitivity", None),
            "controller_haptic_feedback": getattr(game, "controller_haptic_feedback", None),
            "touchscreen": getattr(game, "touchscreen", None),
            "key_remapping": getattr(game, "key_remapping", None),
            "mouse_sensitivity": getattr(game, "mouse_sensitivity", None),
            "mouse_acceleration": getattr(game, "mouse_acceleration", None),
            "mouse_input_in_menus": getattr(game, "mouse_input_in_menus", None),
            # Note: Trackpad_support and Mouse_remapping don't exist in PCGW
            # External IDs and scores
            "metacritic_id": getattr(game, "metacritic_id", None),
            "critic_rating": getattr(game, "metacritic_score", None),
            "opencritic_id": getattr(game, "opencritic_id", None),
            "opencritic_score": getattr(game, "opencritic_score", None),
            "igdb_id": getattr(game, "igdb_id", None),
            "howlongtobeat_id": getattr(game, "howlongtobeat_id", None),
            "wikipedia_id": getattr(game, "wikipedia_id", None),
            "mobygames_id": getattr(game, "mobygames_id", None),
            "official_url": getattr(game, "official_url", None),
            "pcgw_page_id": game.page_id,
            "pcgw_page_name": game.page_name,
        }

    # =========================================================================
    # SYNC OPERATIONS
    # =========================================================================

    def get_sync_stats(self) -> Dict[str, int]:
        """Get match statistics for sync dialog

        Returns:
            Dict with counts: {"total": N, "matched": N, "failed": N}
        """
        db = self._get_db()
        return db.get_match_count()

    def sync_failed_matches(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> Dict[str, int]:
        """Retry all failed store matches

        Re-queries entries with match_method='no_match' against the
        PCGamingWiki API. Games may have been added to PCGamingWiki
        since the last sync.

        Args:
            progress_callback: (message, current, total, success_count)

        Returns:
            {"total": N, "success": N, "failed": N}
        """
        db = self._get_db()
        api = self._get_api()

        # Get all no_match entries
        session = db.get_session()
        try:
            failed_matches = (
                session.query(PcgwStoreMatch)
                .filter(PcgwStoreMatch.match_method == "no_match")
                .all()
            )
            for m in failed_matches:
                session.expunge(m)
        finally:
            session.close()

        total = len(failed_matches)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        # Group by store
        steam_ids = [m.store_app_id for m in failed_matches if m.store_name == "steam"]
        gog_ids = [m.store_app_id for m in failed_matches if m.store_name == "gog"]

        success = 0

        def _make_batch_progress(base_offset):
            """Adapter: API callback (msg, batch_current, batch_total) -> provider 4-arg callback"""
            def _cb(message, batch_current, batch_total):
                if progress_callback:
                    progress_callback(message, base_offset + batch_current, total, success)
            return _cb

        # Batch query Steam failed matches
        if steam_ids:
            try:
                results = api.lookup_by_steam_ids_batch(
                    steam_ids,
                    status_callback=_make_batch_progress(0),
                )
                success += self._process_retry_results("steam", steam_ids, results, db)
            except PcgwApiError as e:
                logger.warning(f"PCGamingWiki: Steam retry batch failed: {e}")

        # Batch query GOG failed matches
        if gog_ids:
            try:
                results = api.lookup_by_gog_ids_batch(
                    gog_ids,
                    status_callback=_make_batch_progress(len(steam_ids)),
                )
                success += self._process_retry_results("gog", gog_ids, results, db)
            except PcgwApiError as e:
                logger.warning(f"PCGamingWiki: GOG retry batch failed: {e}")

        if progress_callback:
            progress_callback(
                f"PCGamingWiki: Retry complete - {success} new matches",
                total, total, success,
            )

        still_failed = total - success
        logger.info(
            f"PCGamingWiki sync: {success} new matches, {still_failed} still failed"
        )
        return {"total": total, "success": success, "failed": still_failed}

    def sync_refresh_all(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
    ) -> Dict[str, int]:
        """Refresh metadata for all matched games

        Re-fetches PCGamingWiki data for all entries that have a
        pcgw_page_id. Updates game data and multiplayer info.

        Args:
            progress_callback: (message, current, total, success_count)

        Returns:
            {"total": N, "success": N, "failed": N}
        """
        db = self._get_db()
        api = self._get_api()

        # Get all matched entries
        session = db.get_session()
        try:
            matched = (
                session.query(PcgwStoreMatch)
                .filter(PcgwStoreMatch.pcgw_page_id.isnot(None))
                .all()
            )
            for m in matched:
                session.expunge(m)
        finally:
            session.close()

        steam_ids = [m.store_app_id for m in matched if m.store_name == "steam"]
        gog_ids = [m.store_app_id for m in matched if m.store_name == "gog"]
        total = len(steam_ids) + len(gog_ids)

        if total == 0:
            return {"total": 0, "success": 0, "failed": 0}

        success = 0

        def _make_batch_progress(base_offset):
            """Adapter: API callback (msg, batch_current, batch_total) -> provider 4-arg callback"""
            def _cb(message, batch_current, batch_total):
                if progress_callback:
                    progress_callback(message, base_offset + batch_current, total, success)
            return _cb

        # Refresh Steam games
        if steam_ids:
            try:
                results = api.lookup_by_steam_ids_batch(
                    steam_ids,
                    status_callback=_make_batch_progress(0),
                )
                self._store_api_results("steam", results, steam_ids, db)
                success += len(results)
            except PcgwApiError as e:
                logger.warning(f"PCGamingWiki: Steam refresh batch failed: {e}")

        # Refresh GOG games
        if gog_ids:
            try:
                results = api.lookup_by_gog_ids_batch(
                    gog_ids,
                    status_callback=_make_batch_progress(len(steam_ids)),
                )
                self._store_api_results("gog", results, gog_ids, db)
                success += len(results)
            except PcgwApiError as e:
                logger.warning(f"PCGamingWiki: GOG refresh batch failed: {e}")

        if progress_callback:
            progress_callback(
                f"PCGamingWiki: Refresh complete - {success} games updated",
                total, total, success,
            )

        logger.info(
            f"PCGamingWiki refresh: {success}/{total} games updated"
        )
        return {"total": total, "success": success, "failed": total - success}

    def _process_retry_results(
        self,
        store_name: str,
        all_ids: List[str],
        api_results: Dict[str, Any],
        db: PcgwDatabase,
    ) -> int:
        """Process retry results and update store matches

        Returns count of newly matched games.
        """
        success = 0
        session = db.get_session()
        try:
            for store_id in all_ids:
                data = api_results.get(store_id)
                if data:
                    page_id_raw = data.get("pageID")
                    if page_id_raw:
                        try:
                            page_id = int(page_id_raw)
                        except (ValueError, TypeError):
                            continue

                        db.save_game_from_api(data, session=session)
                        db.save_store_match(
                            store_name=store_name,
                            store_app_id=store_id,
                            pcgw_page_id=page_id,
                            pcgw_page_name=data.get("pageName"),
                            match_method="store_id",
                            session=session,
                        )
                        success += 1
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning(f"PCGamingWiki: Failed to store retry results: {e}")
        finally:
            session.close()
        return success

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def on_enable(self) -> None:
        """Initialize database and check service status on enable"""
        self._get_db()
        # Pre-check PCGW status page — trip breaker early if there's a known outage.
        # Skip the NetworkMonitor check (may not be initialized yet at startup).
        # The status page is on separate infrastructure and responds fast (~1s).
        try:
            self._get_api().check_status_page()
        except Exception as e:
            logger.debug("Status page pre-check skipped: %s", e)
        logger.info("PCGamingWiki provider enabled")

    def on_disable(self) -> None:
        """Cleanup on disable"""
        if self._db:
            self._db.close()
            self._db = None
        logger.info("PCGamingWiki provider disabled")

    def close(self) -> None:
        """Cleanup on shutdown"""
        if self._api:
            self._api.close()
            self._api = None
        if self._db:
            self._db.close()
            self._db = None


# =============================================================================
# Helpers
# =============================================================================

def _split_field(value: Optional[str]) -> List[str]:
    """Split a comma-delimited PCGW field into a list of strings"""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]
