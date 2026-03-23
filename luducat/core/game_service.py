# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_service.py

"""Game service for luducat

Bridges plugins and UI by:
- Aggregating games from all enabled plugins
- Managing game data in the main database
- Converting between formats
- Providing async data loading

Delegates to focused services for specific responsibilities:
- TagService: tag CRUD, assignment, store sync
- UserDataService: favorites, hidden, notes, playtime
- LazyMetadata: on-demand screenshots, covers, descriptions
- EnrichmentService: enrichment persistence, priority overrides
- SyncOrchestrator: sync pipeline, store fetch, metadata enrichment
"""

import gc
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

if TYPE_CHECKING:
    from .config import Config

from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from .config import get_cache_dir
from .database import (
    Database,
    Game as DbGame,
    StoreGame,
    find_or_create_game,
    repair_parent_dedup,
)
from .enrichment_service import EnrichmentService
from .game_entry import GameEntry, _intern
from .lazy_metadata import LazyMetadata
from .plugin_manager import PluginManager
from .metadata_resolver import (
    FIELD_SOURCE_CAPABILITIES,
    get_resolver,
)
from .sync_orchestrator import SyncOrchestrator, METADATA_JOB_BATCH_SIZE
from .constants import DEFAULT_TAG_COLOR
from .content_filter import adult_confidence, adult_confidence_from_sources
from .dt import parse_release_date
from .tag_service import TagService
from .user_data_service import UserDataService
from ..plugins.base import Game as PluginGame

logger = logging.getLogger(__name__)

# Strip "Company:" prefix from developer/publisher entries (data quality fix)
_COMPANY_PREFIX = re.compile(r'^Company:\s*', re.IGNORECASE)

# Detect demo/prologue/trial titles (normalized_title is lowercase, no punctuation)
_DEMO_TITLE = re.compile(
    r"[\s_-]*(?:demo|prologue|trial)\s*$",
    re.IGNORECASE,
)


def _strip_company_prefix(items: list) -> list:
    """Remove 'Company:' prefix from each entry in a developer/publisher list."""
    return [_COMPANY_PREFIX.sub('', s) for s in items if s]


# Fields that stay in _games_cache (views, filters, sorting, FTS, context menu)
LIST_FIELDS = frozenset({
    "id", "title", "normalized_title", "primary_store",
    "stores", "store_app_ids", "launch_urls",
    "is_favorite", "is_hidden", "is_family_shared", "family_license_count",
    "is_installed",
    "tags", "added_at", "last_launched", "launch_count", "playtime_minutes", "notes",
    "short_description", "header_image", "cover_image", "screenshots",
    "release_date", "developers", "publishers", "genres", "franchise", "game_modes", "themes",
    "is_free", "is_demo",
    "protondb_rating", "steam_deck_compat",
    "adult_confidence", "nsfw_override",
    "online_players", "local_players", "lan_players",
    "cover_source",
    "launch_config",
})

# Fields loaded on demand when a game is selected (MetadataPanel only)
DETAIL_FIELDS = frozenset({
    "background_image", "install_info",
    "series", "engine", "perspectives", "platforms", "age_ratings",
    "user_rating", "rating_positive", "rating_negative",
    "critic_rating", "critic_rating_url",
    "controller_support", "supported_languages", "full_audio_languages",
    "features", "controls",
    "pacing", "art_styles", "monetization", "microtransactions",
    "achievements", "estimated_owners", "recommendations", "peak_ccu",
    "average_playtime_forever",
    "links", "game_modes_detail", "videos", "storyline", "required_age",
    "protondb_score", "release_dates",
})


class GameService:
    """Service for loading and managing game data

    Provides a unified interface for:
    - Loading games from database
    - Syncing games from plugins
    - Converting between formats
    - Managing user data (favorites, tags)

    Usage:
        service = GameService(database, plugin_manager)

        # Load all games
        games = service.get_all_games()

        # Sync from plugins (via job-queue system)
        jobs = service.build_sync_jobs()

        # Update user data
        service.set_favorite(game_id, True)
    """

    # Expose batch size constant for backwards compatibility
    METADATA_JOB_BATCH_SIZE = METADATA_JOB_BATCH_SIZE

    def __init__(
        self,
        database: Database,
        plugin_manager: PluginManager,
        config: Optional["Config"] = None,
    ):
        """Initialize game service

        Args:
            database: Main database instance
            plugin_manager: Plugin manager instance
            config: Application config (for metadata priorities)
        """
        self.database = database
        self.plugin_manager = plugin_manager
        self._config = config

        # Use the global MetadataResolver singleton (initialized in main.py)
        self._resolver = get_resolver()
        self._resolver.set_plugin_manager(plugin_manager)
        self._resolver.build_from_plugins(plugin_manager.get_discovered_plugins())

        # Validate priorities against freshly built capabilities
        if self._resolver.prune_stale_priorities() and self._config:
            self._config.set_metadata_priorities(
                self._resolver.get_all_field_priorities()
            )
            self._config.save()
            logger.info("Persisted pruned metadata priorities to config")

        # Cache for UI data (GameEntry objects with __slots__ for memory efficiency)
        self._games_cache: Dict[str, GameEntry] = {}
        self._cache_valid = False

        # Cached per-store game counts (invalidated on cache rebuild)
        self._store_counts_cache: Optional[Dict[str, int]] = None

        # LRU cache for descriptions (lazy loaded, max 20 items)
        self._description_cache: OrderedDict[str, str] = OrderedDict()
        self._description_cache_max = 20

        # LRU cache for detail fields (lazy loaded per game, max 50 items)
        self._detail_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._detail_cache_max = 50

        # --- Wire up extracted services ---
        self._tags = TagService(database, self._games_cache, config=self._config)
        self._user_data = UserDataService(database, self._games_cache)
        self._enrichment = EnrichmentService(
            database, self._resolver, self._games_cache,
        )
        self._lazy_metadata = LazyMetadata(
            database, self._games_cache, self._description_cache,
            self._description_cache_max, self._resolver, plugin_manager,
            self._detail_cache, self._detail_cache_max,
        )
        self._sync = SyncOrchestrator(
            database, plugin_manager, self._resolver,
            self._enrichment, config,
        )
        self._sync.set_game_service_callbacks(
            save_plugin_game=self._save_plugin_game,
            get_synced_app_ids=self._get_synced_app_ids,
            remove_family_shared=self.remove_family_shared_games,
            invalidate_cache=self.invalidate_cache,
            remove_store_data=self.remove_store_data,
            reconcile_stale=self.reconcile_stale_games,
            save_account_id=self._save_last_account_id,
        )

        # Check if cache directory was deleted and invalidate stale paths
        self._validate_cache_on_startup()

        # One-time dedup repair for parent-title matching improvements
        self._repair_dedup_if_needed()

    def _validate_cache_on_startup(self) -> None:
        """Check if cache directory exists, invalidate stale paths if deleted.

        If the user deleted the cache directory (e.g., ~/.cache/luducat),
        we need to clear all local file paths from the database metadata
        so that lazy loading will refetch them.

        Also handles migration from old app name (gamelauncher -> luducat).
        """
        cache_dir = get_cache_dir()

        if not cache_dir.exists():
            logger.info(
                f"Cache directory {cache_dir} does not exist, "
                "will invalidate stale local paths in database"
            )
            self._invalidate_local_screenshot_paths()
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._invalidate_local_screenshot_paths()

    def _invalidate_local_screenshot_paths(self) -> None:
        """Clear invalid local file paths from database metadata.

        Scans all StoreGame entries and clears screenshot paths that point
        to non-existent local files. This allows lazy loading to refetch them.
        Also detects paths from old app names (e.g., gamelauncher -> luducat).
        """
        session = self.database.get_session()
        try:
            store_games = session.query(StoreGame).filter(
                StoreGame.metadata_json.isnot(None)
            ).all()

            updated_count = 0
            for sg in store_games:
                metadata = sg.metadata_json
                if not metadata:
                    continue

                screenshots = metadata.get("screenshots", [])
                if not screenshots:
                    continue

                has_invalid = False

                for ss in screenshots:
                    if not ss:
                        continue
                    if ss.startswith("http://") or ss.startswith("https://"):
                        continue
                    elif ss.startswith("file://"):
                        from PySide6.QtCore import QUrl
                        path = QUrl(ss).toLocalFile()
                        if "gamelauncher" in path.lower() or not Path(path).exists():
                            has_invalid = True
                            logger.debug(f"Invalidating stale screenshot: {ss}")
                            break
                    elif Path(ss).is_absolute():
                        if "gamelauncher" in ss.lower() or not Path(ss).exists():
                            has_invalid = True
                            logger.debug(f"Invalidating stale screenshot: {ss}")
                            break

                if has_invalid:
                    metadata["screenshots"] = []
                    sg.metadata_json = metadata
                    flag_modified(sg, "metadata_json")
                    updated_count += 1

            if updated_count > 0:
                session.commit()
                logger.info(
                    f"Invalidated screenshot paths in {updated_count} games "
                    "(will be refetched via lazy loading)"
                )

        except Exception as e:
            logger.error(f"Failed to invalidate local screenshot paths: {e}")
            session.rollback()

    def _repair_dedup_if_needed(self) -> None:
        """Run one-time dedup repair for parent-title matching improvements.

        Uses config flag '_dedup_repair_v1' to run exactly once.
        """
        config = self._config
        if config is None:
            return
        flag_key = "_dedup_repair_v1"
        if config.get(flag_key, default=False):
            return  # Already ran

        session = self.database.get_session()
        try:
            merged = repair_parent_dedup(session)
            if merged > 0:
                session.commit()
                logger.info("Dedup repair v1: merged %d duplicate game(s)", merged)
            else:
                logger.debug("Dedup repair v1: no duplicates found")
            config.set(flag_key, True)
        except Exception as e:
            logger.error("Dedup repair failed: %s", e)
            session.rollback()

    # ── Cache / Query / Conversion ─────────────────────────────────────

    def get_all_games(
        self, progress_callback: Optional[Callable[[str, str, int], None]] = None
    ) -> List[GameEntry]:
        """Get all games formatted for UI

        Args:
            progress_callback: Optional callback(message, detail, progress) for progress updates.
                              Called periodically to allow UI to remain responsive.
                              progress is 0-100 percentage.

        Returns:
            List of GameEntry objects with UI-ready format
        """
        if self._cache_valid:
            return list(self._games_cache.values())

        self._refresh_cache(progress_callback)
        return list(self._games_cache.values())

    def get_game(self, game_id: str) -> Optional[GameEntry]:
        """Get a single game by ID

        Args:
            game_id: Game UUID

        Returns:
            GameEntry or None
        """
        if not self._cache_valid:
            self._refresh_cache()

        return self._games_cache.get(game_id)

    def get_game_details(self, game_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed game info including store game metadata

        This method queries the database directly to get the raw store_game
        metadata with source tracking, which is useful for the preview dialog.

        Args:
            game_id: Game UUID

        Returns:
            Dict with game info and store_games list containing metadata,
            or None if game not found
        """
        session = self.database.get_session()
        try:
            game = (
                session.query(DbGame)
                .options(selectinload(DbGame.store_games))
                .filter_by(id=game_id)
                .first()
            )
            if not game:
                return None

            store_games = []
            for sg in game.store_games:
                store_games.append({
                    "store_name": sg.store_name,
                    "store_app_id": sg.store_app_id,
                    "metadata": sg.metadata_json or {},
                })

            return {
                "id": game.id,
                "title": game.title,
                "primary_store": game.primary_store,
                "store_games": store_games,
            }
        except Exception as e:
            logger.error(f"Failed to get game details: {e}")
            return None

    def _refresh_cache(
        self, progress_callback: Optional[Callable[[str, str, int], None]] = None
    ) -> None:
        """Refresh games cache from database

        Queries main DB for game records, then batch-fetches metadata
        from each plugin's database. This eliminates metadata_json duplication.

        Args:
            progress_callback: Optional callback(message, detail, progress_percent)
                              for progress updates
        """
        def report(message: str, detail: str = "", progress: int = 0) -> None:
            if progress_callback:
                progress_callback(message, detail, progress)

        def _rss_mb() -> int:
            """Current RSS in MB (0 if psutil unavailable)."""
            try:
                import psutil
                return psutil.Process().memory_info().rss // (1024 * 1024)
            except Exception:
                return 0

        session = self.database.get_session()

        try:
            # Expire all cached objects so the query fetches fresh data.
            # Critical after sync (which writes from a worker thread session):
            # without this, the identity map returns stale Game/StoreGame
            # objects and new store_game entries (e.g. second store badge)
            # won't appear until restart.
            session.expire_all()

            report(_("Loading games..."), _("Querying database..."), 5)

            games = (
                session.query(DbGame)
                .options(
                    selectinload(DbGame.store_games).load_only(
                        StoreGame.id,
                        StoreGame.game_id,
                        StoreGame.store_name,
                        StoreGame.store_app_id,
                        StoreGame.launch_url,
                        StoreGame.family_shared,
                        StoreGame.family_shared_owner,
                        StoreGame.is_installed,
                        StoreGame.install_path,
                        # metadata_json excluded — fetched separately for steam_deck_compat
                    ),
                    selectinload(DbGame.tags),
                    selectinload(DbGame.user_data),
                )
                .all()
            )

            total_games = len(games)
            loading_msg = _("Loading games...")
            report(loading_msg, _("Found %d games") % total_games, 10)

            store_app_ids: Dict[str, List[str]] = {}
            game_store_map: Dict[str, List[tuple]] = {}

            for game in games:
                game_stores: List[tuple] = []
                for sg in game.store_games:
                    store_app_ids.setdefault(sg.store_name, []).append(sg.store_app_id)
                    game_stores.append((sg.store_name, sg.store_app_id))
                if game_stores:
                    game_store_map[game.id] = game_stores

            from luducat.core.plugin_manager import PluginManager

            # Metadata fetch reads from local plugin DBs — no auth needed.
            # Include any enabled plugin that has cached data on disk.
            metadata_stores = {}
            for store_name, app_ids in store_app_ids.items():
                plugin = self.plugin_manager.get_plugin(store_name)
                if plugin:
                    try:
                        db_path = plugin.get_database_path()
                        if db_path.exists():
                            metadata_stores[store_name] = (plugin, app_ids)
                        else:
                            logger.debug(f"Skipping {store_name}: no plugin DB")
                    except Exception:
                        logger.debug(f"Skipping {store_name}: DB path check failed")
                else:
                    logger.debug(f"Skipping {store_name}: plugin not found or disabled")

            # Progress layout:
            # 0-10%   DB query
            # 10-55%  Store metadata fetch
            # 55-65%  Enrichment + game modes + ProtonDB
            # 65-95%  Game loop (per-game metadata resolution)
            # 95-100% FTS rebuild
            num_stores = len(metadata_stores)

            all_metadata: Dict[str, Dict[str, Dict]] = {}

            if num_stores == 0 and store_app_ids:
                logger.info("No authenticated plugins - skipping metadata fetch")
                report(loading_msg, _("No configured stores (using cached data)"), 40)

            # Parallel store metadata fetch — each plugin has its own DB
            total_games = sum(len(ids) for _, ids in metadata_stores.values())
            report(
                loading_msg,
                _("Fetching metadata (%d games, %d stores)...") % (
                    total_games, num_stores,
                ),
                10,
            )

            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _fetch_store(store_name_ids):
                sn, (_, aids) = store_name_ids
                logger.info(f"Getting metadata for {len(aids)} {sn} games")
                meta = self._resolver.get_metadata_bulk(sn, aids)
                logger.info(f"Fetched metadata for {len(meta)} {sn} games")
                return sn, meta

            with ThreadPoolExecutor(max_workers=max(1, num_stores)) as executor:
                futures = {
                    executor.submit(_fetch_store, item): item[0]
                    for item in metadata_stores.items()
                }
                for future in as_completed(futures):
                    store_name = futures[future]
                    try:
                        sn, metadata = future.result()
                        all_metadata[sn] = metadata
                        if metadata:
                            sample_id = next(iter(metadata))
                            sample = metadata[sample_id]
                            cover_val = (
                                sample.get('cover')
                                or sample.get('cover_url', 'NONE')
                            )
                            cov = cover_val[:50] if cover_val else 'NONE'
                            logger.info(
                                f"Sample metadata for {sample_id}: "
                                f"cover={cov}"
                            )
                    except Exception as e:
                        logger.warning(f"Store metadata fetch failed for {store_name}: {e}")

            report(loading_msg, _("Loading enrichment data..."), 55)

            # Batch-fetch steam_deck_compat from metadata_json (only for Steam games)
            # This avoids loading metadata_json for ALL store games (most are non-Steam)
            steam_deck_compat_map: Dict[str, str] = {}
            try:
                from sqlalchemy import text as sa_text
                compat_rows = session.execute(
                    sa_text(
                        "SELECT game_id, json_extract(metadata_json, '$.steam_deck_compat') "
                        "FROM store_games "
                        "WHERE store_name = 'steam' AND metadata_json IS NOT NULL "
                        "AND json_extract(metadata_json, '$.steam_deck_compat') IS NOT NULL"
                    )
                ).fetchall()
                for row in compat_rows:
                    steam_deck_compat_map[row[0]] = row[1]
                if steam_deck_compat_map:
                    logger.debug(f"Loaded steam_deck_compat for {len(steam_deck_compat_map)} games")
            except Exception as e:
                logger.debug(f"Could not batch-fetch steam_deck_compat: {e}")

            # Batch-fetch family_license_count from metadata_json (Steam games only)
            family_license_map: Dict[str, int] = {}
            try:
                from sqlalchemy import text as sa_text
                flc_rows = session.execute(
                    sa_text(
                        "SELECT game_id, json_extract(metadata_json, '$._family_license_count') "
                        "FROM store_games "
                        "WHERE store_name = 'steam' AND metadata_json IS NOT NULL "
                        "AND json_extract(metadata_json, '$._family_license_count') IS NOT NULL "
                        "AND json_extract(metadata_json, '$._family_license_count') > 0"
                    )
                ).fetchall()
                for row in flc_rows:
                    family_license_map[row[0]] = int(row[1])
                if family_license_map:
                    logger.debug(f"Loaded family_license_count for {len(family_license_map)} games")
            except Exception as e:
                logger.debug(f"Could not batch-fetch family_license_count: {e}")

            # Get enrichment data through MetadataResolver (respects priorities + enabled state)
            # Include ALL fields that have at least one non-store source
            store_names = set(PluginManager.get_store_plugin_names())
            cache_fields = [
                field for field, sources in FIELD_SOURCE_CAPABILITIES.items()
                if any(s not in store_names for s in sources)
            ]
            enrichment_by_game = self._resolver.get_enrichment_for_cache(
                session, cache_fields
            )
            if enrichment_by_game:
                logger.info(f"Loaded enrichment data for {len(enrichment_by_game)} games")

            # Query enrichment plugin local DBs (IGDB, SteamGridDB, etc.)
            # This is the key addition: plugins with local DBs have thousands
            # of cached entries that were never surfaced during cache build.
            enrichment_plugin_data: Dict[str, Dict[str, Dict[str, Any]]] = {}
            if store_app_ids:
                def _enrichment_progress(display_name: str) -> None:
                    report(
                        loading_msg,
                        _("Loading %s data...") % display_name,
                        57,
                    )

                try:
                    enrichment_plugin_data = (
                        self._resolver.get_enrichment_bulk_for_cache(
                            store_app_ids,
                            progress_callback=_enrichment_progress,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to load enrichment plugin data: {e}")

            report(loading_msg, _("Loading game modes..."), 60)

            enabled_stores = set(self.plugin_manager.get_store_plugins().keys())

            game_modes_available = self._resolver.has_game_modes_support()
            all_game_modes: Dict[str, Dict[str, List[str]]] = {}

            if game_modes_available and store_app_ids:
                try:
                    all_game_modes = self._resolver.get_game_modes_bulk(store_app_ids)
                    modes_count = sum(len(m) for m in all_game_modes.values())
                    logger.info(f"Loaded game modes for {modes_count} games from metadata plugins")
                except Exception as e:
                    logger.warning(f"Failed to load game modes: {e}")

            protondb_ratings: Dict[str, Dict[str, Any]] = {}
            try:
                protondb_plugin = self.plugin_manager.get_plugin("protondb")
                if protondb_plugin and hasattr(protondb_plugin, "get_ratings_bulk"):
                    protondb_ratings = protondb_plugin.get_ratings_bulk()
                    if protondb_ratings:
                        logger.info(
                            f"Loaded {len(protondb_ratings)} "
                            "ProtonDB ratings from plugin DB"
                        )
            except Exception as e:
                logger.debug(f"Could not load ProtonDB ratings: {e}")

            report(loading_msg, _("Building game list..."), 65)
            logger.info(f"RSS before cache build: {_rss_mb()}MB (ORM objects: {total_games})")

            self._games_cache.clear()
            self._detail_cache.clear()
            self._store_counts_cache = None

            # Report progress every 200 games during the main loop
            report_interval = 200
            game_loop_start = 65
            game_loop_range = 30  # 65% -> 95%

            for game_idx, game in enumerate(games):
                if game_idx % report_interval == 0 and game_idx > 0:
                    loop_pct = game_loop_start + int(
                        game_loop_range * game_idx
                        / max(total_games, 1)
                    )
                    msg = _("Processing games (%d/%d)...") % (
                        game_idx, total_games
                    )
                    report(loading_msg, msg, loop_pct)

                metadata_by_store = {}

                if game.id in game_store_map:
                    for store_name, app_id in game_store_map[game.id]:
                        if store_name in all_metadata:
                            store_meta = all_metadata[store_name].get(app_id, {})
                            if store_meta:
                                metadata_by_store[store_name] = store_meta

                # Merge enrichment data from metadata_json._sources
                if game.id in enrichment_by_game:
                    for provider, fields in enrichment_by_game[game.id].items():
                        if provider not in metadata_by_store:
                            metadata_by_store[provider] = fields
                        else:
                            for k, v in fields.items():
                                metadata_by_store[provider].setdefault(k, v)

                # Merge enrichment plugin DB data (IGDB, SteamGridDB, etc.)
                if game.id in game_store_map and enrichment_plugin_data:
                    for store_name, app_id in game_store_map[game.id]:
                        for provider_name, provider_data in enrichment_plugin_data.items():
                            if app_id in provider_data:
                                if provider_name not in metadata_by_store:
                                    metadata_by_store[provider_name] = provider_data[app_id]
                                else:
                                    for k, v in provider_data[app_id].items():
                                        metadata_by_store[provider_name].setdefault(k, v)

                if metadata_by_store:
                    metadata = self._resolver.resolve_game_metadata(metadata_by_store)
                else:
                    metadata = {}

                if game.id in game_store_map:
                    for store_name, app_id in game_store_map[game.id]:
                        if store_name in all_game_modes:
                            game_modes = all_game_modes[store_name].get(app_id)
                            if game_modes:
                                metadata["game_modes"] = game_modes
                                break

                if protondb_ratings:
                    for sg in game.store_games:
                        if sg.store_name == "steam" and sg.store_app_id in protondb_ratings:
                            rating_data = protondb_ratings[sg.store_app_id]
                            if not metadata.get("protondb_rating"):
                                metadata["protondb_rating"] = rating_data["protondb_rating"]
                            if not metadata.get("protondb_score"):
                                metadata["protondb_score"] = rating_data["protondb_score"]
                            break

                # Apply steam_deck_compat from batch-fetched data
                if not metadata.get("steam_deck_compat") and game.id in steam_deck_compat_map:
                    metadata["steam_deck_compat"] = steam_deck_compat_map[game.id]

                ui_game = self._db_game_to_ui(
                    game, metadata, enabled_stores=enabled_stores,
                    game_modes_available=game_modes_available,
                    include_detail_fields=False,
                    metadata_by_store=metadata_by_store,
                    family_license_map=family_license_map,
                )
                if ui_game is not None:
                    self._games_cache[game.id] = ui_game

            self._cache_valid = True
            logger.info(
                f"Refreshed cache with {len(self._games_cache)} games — "
                f"RSS after loop (ORM+cache): {_rss_mb()}MB"
            )

            # Release bulk metadata dicts before detaching ORM objects
            del all_metadata, enrichment_by_game, enrichment_plugin_data
            del all_game_modes
            del protondb_ratings, steam_deck_compat_map, family_license_map
            del game_store_map, games

            # Detach ALL ORM objects from session identity map — expire_all()
            # only marks attributes as expired but keeps objects in the map.
            # expunge_all() actually removes them, allowing GC.
            session.expunge_all()
            gc.collect()

            logger.info(f"RSS after expunge+gc: {_rss_mb()}MB")

            report(loading_msg, _("Building search index..."), 95)
            self._rebuild_fts_index()

        except Exception as e:
            logger.error(f"Failed to refresh cache: {e}")
            raise

    def _rebuild_fts_index(self) -> None:
        """Rebuild the FTS5 full-text search index from cached game data."""
        session = self.database.get_session()
        try:
            from sqlalchemy import text

            result = session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='games_fts'")
            )
            if not result.fetchone():
                logger.debug("games_fts table not found, skipping FTS index rebuild")
                return

            session.execute(text("DELETE FROM games_fts"))

            count = 0
            for game_id, game in self._games_cache.items():
                title = game.get("title", "")
                short_desc = game.get("short_description", "")
                developers = " ".join(game.get("developers") or [])
                publishers = " ".join(game.get("publishers") or [])
                genres = " ".join(game.get("genres") or [])
                themes = " ".join(game.get("themes") or [])

                session.execute(
                    text(
                        "INSERT INTO games_fts(game_id, title, short_description, "
                        "developers, publishers, genres, themes) "
                        "VALUES (:game_id, :title, :short_desc, :devs, :pubs, :genres, :themes)"
                    ),
                    {
                        "game_id": game_id,
                        "title": title,
                        "short_desc": short_desc,
                        "devs": developers,
                        "pubs": publishers,
                        "genres": genres,
                        "themes": themes,
                    },
                )
                count += 1

            session.commit()
            logger.debug(f"Rebuilt FTS5 index with {count} games")

        except Exception as e:
            logger.warning(f"Failed to rebuild FTS5 index: {e}")
            try:
                session.rollback()
            except Exception:
                pass

    def search_fts(self, query: str) -> Optional[set]:
        """Search games using FTS5 full-text search.

        Args:
            query: Search query string. Supports FTS5 syntax.

        Returns:
            Set of matching game IDs, or None if FTS5 is unavailable.
        """
        if not query or not query.strip():
            return None

        session = self.database.get_session()
        try:
            from sqlalchemy import text

            result = session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='games_fts'")
            )
            if not result.fetchone():
                return None

            # Strip FTS5 syntax characters to prevent query parse errors
            clean_query = query.strip()
            for ch in '",;(){}[]^~\\':
                clean_query = clean_query.replace(ch, ' ')
            clean_query = ' '.join(clean_query.split())  # collapse whitespace
            special = ['*', ':', 'OR', 'AND', 'NOT', '-', '+']
            if clean_query and not any(
                c in clean_query for c in special
            ):
                terms = clean_query.split()
                terms[-1] = terms[-1] + '*'
                clean_query = ' '.join(terms)

            result = session.execute(
                text("SELECT game_id FROM games_fts WHERE games_fts MATCH :query"),
                {"query": clean_query},
            )
            return {row[0] for row in result.fetchall()}

        except Exception as e:
            logger.debug(f"FTS5 search failed for '{query}': {e}")
            return None

    def _db_game_to_ui(
        self, game: DbGame, metadata: Optional[Dict] = None,
        enabled_stores: Optional[set] = None,
        game_modes_available: bool = False,
        include_detail_fields: bool = True,
        metadata_by_store: Optional[Dict[str, Dict]] = None,
        family_license_map: Optional[Dict[str, int]] = None,
    ) -> Optional[Union[GameEntry, Dict[str, Any]]]:
        """Convert database Game to UI format

        Args:
            game: Database Game object with relationships loaded
            metadata: Optional metadata dict from plugin
            enabled_stores: Optional set of enabled store names
            game_modes_available: Whether game modes data is available
            metadata_by_store: Optional per-source metadata dict for content scoring

        Returns:
            GameEntry (list cache) or Dict (detail), or None if game should be hidden
        """
        all_stores = sorted(dict.fromkeys(sg.store_name for sg in game.store_games))
        if enabled_stores is not None:
            stores = [s for s in all_stores if s in enabled_stores]
            if not stores:
                return None
        else:
            stores = all_stores

        user_data = game.user_data
        is_favorite = user_data.is_favorite if user_data else False
        is_hidden = user_data.is_hidden if user_data else False
        last_launched = user_data.last_launched if user_data else None
        launch_count = user_data.launch_count if user_data else 0
        playtime_minutes = user_data.playtime_minutes if user_data else 0
        custom_notes = user_data.custom_notes if user_data else ""
        game_nsfw_override = user_data.nsfw_override if user_data else 0
        launch_config_raw = user_data.launch_config if user_data else None

        tags = [
            {
                "name": _intern(tag.name),
                "color": _intern(tag.color) if tag.color else "",
                "source": _intern(tag.source) if tag.source else "",
                "nsfw_override": tag.nsfw_override,
            }
            for tag in game.tags
        ]

        if metadata is None:
            metadata_by_store = {}
            for sg in game.store_games:
                if sg.metadata_json:
                    metadata_by_store[sg.store_name] = sg.metadata_json
            metadata = self._resolver.resolve_game_metadata(metadata_by_store)

        raw_screenshots = metadata.get("screenshots", [])
        screenshots = [
            ss for ss in raw_screenshots
            if self._lazy_metadata._is_valid_screenshot_path(ss)
        ]

        active_store_games = (
            [sg for sg in game.store_games if sg.store_name in enabled_stores]
            if enabled_stores is not None else game.store_games
        )
        store_app_ids = {sg.store_name: sg.store_app_id for sg in active_store_games}

        launch_urls = {sg.store_name: sg.launch_url for sg in active_store_games}

        is_family_shared = any(sg.family_shared == 1 for sg in active_store_games)

        # Family license count from batch-fetched data or metadata_json fallback
        family_license_count = 0
        if family_license_map is not None:
            family_license_count = family_license_map.get(game.id, 0)
        else:
            # Fallback for non-cache callers (include_detail_fields=True path)
            for sg in active_store_games:
                if sg.store_name == "steam" and sg.metadata_json:
                    flc = sg.metadata_json.get("_family_license_count", 0)
                    if flc > family_license_count:
                        family_license_count = flc

        is_installed = any(sg.is_installed for sg in active_store_games)

        # Player counts from game_modes_detail (promoted to list cache for badges)
        # Note: game_modes_detail may arrive as a JSON string from SQL json_extract()
        _gmd = metadata.get("game_modes_detail") or {}
        if isinstance(_gmd, str):
            from .json_compat import json as _json
            try:
                _gmd = _json.loads(_gmd)
            except (ValueError, TypeError):
                _gmd = {}
        if not isinstance(_gmd, dict):
            _gmd = {}

        # Intern repeated strings and list contents for memory savings
        _norm_title = game.normalized_title
        _primary = game.primary_store
        _developers = [_intern(d) for d in _strip_company_prefix(metadata.get("developers", []))]
        _publishers = [_intern(p) for p in _strip_company_prefix(metadata.get("publishers", []))]
        _genres = [_intern(g) for g in (metadata.get("genres") or [])]
        _themes = [_intern(t) for t in (metadata.get("themes") or [])]
        _game_modes = (
            [_intern(m) for m in (metadata.get("game_modes") or [])]
            if game_modes_available else []
        )
        _franchise = self._normalize_franchise(metadata.get("franchise"))
        _protondb = metadata.get("protondb_rating", "")
        _deck_compat = metadata.get("steam_deck_compat", "")
        _cover_source = (
            (metadata.get("_sources") or {}).get("cover", "")
            or (metadata.get("_sources") or {}).get("cover_url", "")
        )

        # Build GameEntry with interned strings
        result = GameEntry(
            id=game.id,
            title=game.title,
            normalized_title=_intern(_norm_title) if _norm_title else "",
            primary_store=_intern(_primary) if _primary else "",
            stores=[_intern(s) for s in stores],
            store_app_ids=store_app_ids,
            launch_urls=launch_urls,
            is_favorite=is_favorite,
            is_hidden=is_hidden,
            is_family_shared=is_family_shared,
            family_license_count=family_license_count,
            is_installed=is_installed,
            tags=tags,
            added_at=game.added_at.isoformat() if game.added_at else None,
            last_launched=last_launched.isoformat() if last_launched else None,
            launch_count=launch_count,
            playtime_minutes=playtime_minutes,
            notes=custom_notes or "",
            short_description=metadata.get("short_description", ""),
            header_image=metadata.get("header_url", ""),
            cover_image=metadata.get("cover") or metadata.get("cover_url", ""),
            screenshots=screenshots,
            release_date=self._extract_display_date(metadata.get("release_date", "")),
            developers=_developers,
            publishers=_publishers,
            genres=_genres,
            franchise=_intern(_franchise) if _franchise else "",
            game_modes=_game_modes,
            themes=_themes,
            is_demo=(
                metadata.get("is_demo", False)
                or bool(_DEMO_TITLE.search(game.normalized_title))
            ),
            is_free=(
                metadata.get("is_free", False)
                and not metadata.get("is_demo", False)
                and not bool(_DEMO_TITLE.search(game.normalized_title))
            ),
            protondb_rating=_intern(_protondb) if _protondb else "",
            steam_deck_compat=_intern(_deck_compat) if _deck_compat else "",
            adult_confidence=(
                adult_confidence_from_sources(metadata_by_store, tags, game_nsfw_override)
                if metadata_by_store
                else adult_confidence(metadata)
            ),
            nsfw_override=game_nsfw_override,
            online_players=_gmd.get("online_players", ""),
            local_players=_gmd.get("local_players", ""),
            lan_players=_gmd.get("lan_players", ""),
            cover_source=_intern(_cover_source) if _cover_source else "",
            launch_config=launch_config_raw or "",
        )

        # Detail fields — only included when needed (MetadataPanel, detail view)
        # Returns a plain dict when detail fields are requested (rare path)
        if include_detail_fields:
            # Build install_info: {store_name: install_path} for installed stores
            install_info = {}
            for sg in active_store_games:
                if sg.is_installed:
                    install_info[sg.store_name] = sg.install_path

            detail_dict = dict(result.items())
            detail_dict.update({
                "install_info": install_info,
                "background_image": metadata.get("hero") or metadata.get("background_url", ""),
                "franchise": metadata.get("franchise", ""),
                "series": metadata.get("series", ""),
                "engine": metadata.get("engine", ""),
                "perspectives": metadata.get("perspectives", []),
                "platforms": metadata.get("platforms", []),
                "age_ratings": metadata.get("age_ratings", []),
                "user_rating": metadata.get("user_rating"),
                "rating_positive": metadata.get("rating_positive"),
                "rating_negative": metadata.get("rating_negative"),
                "critic_rating": metadata.get("critic_rating"),
                "critic_rating_url": metadata.get("critic_rating_url", ""),
                "controller_support": metadata.get("controller_support", ""),
                "supported_languages": metadata.get("supported_languages", []),
                "full_audio_languages": metadata.get("full_audio_languages", ""),
                "features": metadata.get("features") or metadata.get("categories", []),
                "controls": metadata.get("controls", []),
                "pacing": metadata.get("pacing", []),
                "art_styles": metadata.get("art_styles", []),
                "monetization": metadata.get("monetization", []),
                "microtransactions": metadata.get("microtransactions", []),
                "achievements": metadata.get("achievements"),
                "estimated_owners": metadata.get("estimated_owners", ""),
                "recommendations": metadata.get("recommendations"),
                "peak_ccu": metadata.get("peak_ccu"),
                "average_playtime_forever": metadata.get("average_playtime_forever"),
                "links": metadata.get("links") or metadata.get("websites", []),
                "game_modes_detail": metadata.get("game_modes_detail", {}),
                "videos": metadata.get("videos", []),
                "storyline": metadata.get("storyline", ""),
                "required_age": metadata.get("required_age"),
                "protondb_score": metadata.get("protondb_score"),
                "release_dates": (
                    metadata.get("release_date")
                    if isinstance(metadata.get("release_date"), dict)
                    else {}
                ),
            })
            return detail_dict

        return result

    @staticmethod
    def _normalize_franchise(value) -> str:
        """Normalize franchise to a display string.

        IGDB returns franchise as a list of names or a single string.
        Converts to a comma-separated string for display and sorting.
        """
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) if value else ""
        return value if value else ""

    @staticmethod
    def _extract_display_date(release_date_value) -> str:
        """Extract a display date string from release_date (dict or string).

        For dict format (per-platform dates), parses all values to ISO and
        returns the oldest.  For string format, parses via parse_release_date.

        Args:
            release_date_value: Dict[str, str] or str or None

        Returns:
            ISO date string "YYYY-MM-DD" or empty string
        """
        if isinstance(release_date_value, dict):
            if not release_date_value:
                return ""
            # Parse each platform date to ISO, drop unparseable values
            parsed = [
                iso for v in release_date_value.values()
                if (iso := parse_release_date(str(v) if v else ""))
            ]
            return min(parsed) if parsed else ""
        return parse_release_date(str(release_date_value) if release_date_value else "") or ""

    def reset_media_fields(
        self,
        fields: List[str],
        only_sources: Optional[set] = None,
        affected_game_ids: Optional[set] = None,
    ) -> int:
        """Clear resolved media fields from metadata_json so they re-resolve.

        For each store_game, removes the specified field values and their
        ``_sources`` entries from metadata_json.  Also clears
        ``_priority_hash`` so the enrichment pipeline re-runs.

        Args:
            fields: Field names to clear (e.g. ``["cover", "hero", "screenshots"]``).
            only_sources: If provided, only clear a field when its ``_sources``
                entry matches one of these source names.  ``None`` = clear
                unconditionally.
            affected_game_ids: If provided, populated with the game_id of each
                modified StoreGame so the caller can batch re-enrich them.

        Returns:
            Number of store_game records modified.
        """
        from .enrichment_state import _FIELD_TO_KEY, SOURCES_KEY

        # Build the set of metadata_json keys to remove per field
        field_keys: Dict[str, List[str]] = {}
        for f in fields:
            keys = [f]
            mapped = _FIELD_TO_KEY.get(f)
            if mapped:
                keys.append(mapped)
            # Secondary keys
            if f == "hero":
                keys.extend(["background_provider", "background_url"])
            elif f == "cover":
                keys.append("cover_url")
            field_keys[f] = keys

        session = self.database.get_session()
        modified = 0
        try:
            store_games = session.query(StoreGame).filter(
                StoreGame.metadata_json.isnot(None)
            ).all()

            for idx, sg in enumerate(store_games):
                meta = sg.metadata_json
                if not meta:
                    continue

                sources = meta.get(SOURCES_KEY, {})
                changed = False

                for field_name in fields:
                    if only_sources:
                        # Check both canonical and alias key in _sources
                        src = sources.get(field_name, "")
                        alias = _FIELD_TO_KEY.get(field_name)
                        if not src and alias:
                            src = sources.get(alias, "")
                        if src not in only_sources:
                            continue

                    # Clear field value keys
                    for key in field_keys[field_name]:
                        if key in meta:
                            del meta[key]
                            changed = True

                    # Clear _sources entry (both canonical and alias)
                    if field_name in sources:
                        del sources[field_name]
                    alias = _FIELD_TO_KEY.get(field_name)
                    if alias and alias in sources:
                        del sources[alias]
                        changed = True

                if changed:
                    # Clear priority hash so enrichment re-runs
                    sources.pop("_priority_hash", None)
                    sg.metadata_json = meta
                    flag_modified(sg, "metadata_json")
                    modified += 1
                    if affected_game_ids is not None:
                        affected_game_ids.add(sg.game_id)

                # Batch commit every 500 records
                if modified > 0 and modified % 500 == 0:
                    session.commit()

            if modified > 0:
                session.commit()

            logger.info(
                f"reset_media_fields: cleared {fields} from {modified} store_games"
                + (f" (sources={only_sources})" if only_sources else "")
            )
        except Exception as e:
            logger.error(f"Failed to reset media fields: {e}")
            session.rollback()
            raise
        finally:
            if modified > 0:
                self.invalidate_cache()

        return modified

    def reselect_media_from_plugin(
        self, source_name: str, plugin
    ) -> Dict[str, Dict[str, str]]:
        """Re-evaluate cover/hero for games sourced from a metadata plugin.

        Locally re-queries the plugin's cached assets with current settings
        (e.g. updated author scores). No API calls — purely local.

        Uses SQL-level json_extract filtering to only load StoreGames whose
        cover/hero is actually sourced from the given plugin, and updates
        _games_cache entries in-place so callers can do a targeted view
        refresh instead of a full cache rebuild.

        Args:
            source_name: Plugin name (e.g. "steamgriddb")
            plugin: Metadata plugin instance with reselect_cached_assets()

        Returns:
            Dict mapping game_id to changed fields, e.g.
            ``{"uuid": {"cover": "https://..."}}``.  Empty if nothing changed.
        """
        from .enrichment_state import _FIELD_TO_KEY, SOURCES_KEY

        if not hasattr(plugin, 'reselect_cached_assets'):
            return {}

        session = self.database.get_session()
        modified = 0
        # Track which game_ids changed and what their new URLs are
        modified_game_ids: Dict[str, Dict[str, str]] = {}
        try:
            from sqlalchemy import or_, text

            store_games = session.query(StoreGame).filter(
                or_(
                    text("json_extract(metadata_json, '$._sources.cover') = :src"),
                    text("json_extract(metadata_json, '$._sources.cover_url') = :src"),
                    text("json_extract(metadata_json, '$._sources.hero') = :src"),
                    text("json_extract(metadata_json, '$._sources.background_url') = :src"),
                )
            ).params(src=source_name).all()

            for sg in store_games:
                meta = sg.metadata_json
                if not meta:
                    continue

                sources = meta.get(SOURCES_KEY, {})
                cover_key = _FIELD_TO_KEY.get("cover", "cover_url")
                hero_key = _FIELD_TO_KEY.get("hero", "background_url")

                cover_sourced = (
                    sources.get("cover") == source_name
                    or sources.get(cover_key) == source_name
                )
                hero_sourced = (
                    sources.get("hero") == source_name
                    or sources.get(hero_key) == source_name
                )
                if not (cover_sourced or hero_sourced):
                    continue

                result = plugin.reselect_cached_assets(sg.store_name, sg.store_app_id)

                changed = False
                updates: Dict[str, str] = {}
                if cover_sourced:
                    new_cover = (result or {}).get("cover")
                    old_cover = meta.get(cover_key) or meta.get("cover")
                    if new_cover != old_cover:
                        if new_cover:
                            meta["cover"] = new_cover
                            meta[cover_key] = new_cover
                        else:
                            # All assets blocked — clear field + hash for lazy re-resolve
                            meta.pop("cover", None)
                            meta.pop(cover_key, None)
                            sources.pop("cover", None)
                            sources.pop(cover_key, None)
                            sources.pop("_priority_hash", None)
                        changed = True
                        updates["cover"] = new_cover or ""

                if hero_sourced:
                    new_hero = (result or {}).get("hero")
                    old_hero = meta.get(hero_key) or meta.get("hero")
                    if new_hero != old_hero:
                        if new_hero:
                            meta["hero"] = new_hero
                            meta[hero_key] = new_hero
                        else:
                            meta.pop("hero", None)
                            meta.pop(hero_key, None)
                            meta.pop("background_provider", None)
                            sources.pop("hero", None)
                            sources.pop(hero_key, None)
                            sources.pop("_priority_hash", None)
                        changed = True
                        updates["hero"] = new_hero or ""

                if changed:
                    sg.metadata_json = meta
                    flag_modified(sg, "metadata_json")
                    modified += 1
                    if updates:
                        modified_game_ids[sg.game_id] = updates

                if modified > 0 and modified % 500 == 0:
                    session.commit()

            if modified > 0:
                session.commit()

                # For cleared fields, re-resolve from next priority source
                # so the UI shows the fallback cover instead of "no cover".
                needs_resolve = {
                    gid: upd for gid, upd in modified_game_ids.items()
                    if any(v == "" for v in upd.values())
                }
                if needs_resolve:
                    self._reresolve_cleared_fields(
                        session, needs_resolve, cover_key, hero_key,
                        modified_game_ids,
                    )

                # In-place cache update instead of invalidate_cache() — avoids
                # full 10-second rebuild. Only affected entries get new URLs.
                for game_id, updates in modified_game_ids.items():
                    if game_id in self._games_cache:
                        entry = self._games_cache[game_id]
                        if "cover" in updates:
                            entry.cover_image = updates["cover"]
                    # Clear detail cache entry (hero lives there)
                    self._detail_cache.pop(game_id, None)

            logger.info(
                "reselect_media_from_plugin: updated %d records (source=%s)",
                modified, source_name,
            )
            return modified_game_ids
        except Exception as e:
            logger.error(f"reselect_media_from_plugin failed: {e}")
            session.rollback()
            return {}

    def _reresolve_cleared_fields(
        self,
        session,
        needs_resolve: Dict[str, Dict[str, str]],
        cover_key: str,
        hero_key: str,
        modified_game_ids: Dict[str, Dict[str, str]],
    ) -> None:
        """Re-resolve cleared cover/hero from sibling StoreGames.

        When a metadata plugin's assets are all blocked, the field is cleared.
        This method runs the priority resolver on the remaining StoreGames
        so the next-best source provides the cover/hero immediately, rather
        than leaving "no cover" until the next detail-view access.
        """
        from sqlalchemy.orm import selectinload

        game_ids = list(needs_resolve.keys())
        games = (
            session.query(DbGame)
            .options(selectinload(DbGame.store_games))
            .filter(DbGame.id.in_(game_ids))
            .all()
        )
        for game in games:
            metadata_by_store = {}
            for sg in game.store_games:
                if sg.metadata_json:
                    metadata_by_store[sg.store_name] = sg.metadata_json
            if not metadata_by_store:
                continue
            resolved = self._resolver.resolve_game_metadata(metadata_by_store)
            updates = needs_resolve[game.id]
            if updates.get("cover") == "":
                fallback = resolved.get(cover_key) or resolved.get("cover") or ""
                if fallback:
                    updates["cover"] = fallback
                    modified_game_ids[game.id]["cover"] = fallback
            if updates.get("hero") == "":
                fallback = resolved.get(hero_key) or resolved.get("hero") or ""
                if fallback:
                    updates["hero"] = fallback
                    modified_game_ids[game.id]["hero"] = fallback

    def invalidate_cache(self) -> None:
        """Invalidate the games cache and lazy metadata."""
        self._cache_valid = False
        self._detail_cache.clear()
        self._lazy_metadata.clear_cache()

    # ── Store data management ──────────────────────────────────────────

    def _get_synced_app_ids(self, store_name: str) -> set:
        """Get set of app IDs already synced for a store."""
        session = self.database.get_session()
        try:
            existing = session.query(StoreGame.store_app_id).filter_by(
                store_name=store_name
            ).all()
            return {row[0] for row in existing}
        finally:
            pass

    def remove_family_shared_games(self, store_name: str) -> int:
        """Remove family shared games for a store from the database.

        Args:
            store_name: Store name (e.g., "steam")

        Returns:
            Number of games removed
        """
        session = self.database.get_session()
        try:
            family_shared_games = session.query(StoreGame).filter_by(
                store_name=store_name,
                family_shared=1,
            ).all()

            if not family_shared_games:
                return 0

            removed_count = 0
            orphaned_game_ids = []

            for store_game in family_shared_games:
                game_id = store_game.game_id

                session.delete(store_game)
                removed_count += 1

                remaining = session.query(StoreGame).filter(
                    StoreGame.game_id == game_id,
                    StoreGame.id != store_game.id,
                ).count()

                if remaining == 0:
                    orphaned_game_ids.append(game_id)

            if orphaned_game_ids:
                session.query(DbGame).filter(
                    DbGame.id.in_(orphaned_game_ids)
                ).delete(synchronize_session='fetch')
                logger.info(
                    f"Removed {len(orphaned_game_ids)} orphaned games "
                    f"after removing family shared entries"
                )

            session.commit()
            self.invalidate_cache()

            logger.info(
                f"Removed {removed_count} family shared games for {store_name}"
            )
            return removed_count

        except Exception as e:
            logger.error(f"Failed to remove family shared games: {e}")
            session.rollback()
            raise

    def count_store_exclusive_games(self, store_name: str) -> int:
        """Count games that exist only in the given store."""
        enabled_stores = set(self.plugin_manager.get_store_plugins().keys())
        enabled_stores.discard(store_name)

        session = self.database.get_session()
        games_in_store = (
            session.query(DbGame)
            .join(StoreGame)
            .filter(StoreGame.store_name == store_name)
            .options(selectinload(DbGame.store_games))
            .all()
        )

        count = 0
        for game in games_in_store:
            other_stores = {
                sg.store_name for sg in game.store_games
                if sg.store_name != store_name
            }
            if not other_stores.intersection(enabled_stores):
                count += 1
        return count

    def remove_store_data(self, store_name: str) -> Dict[str, int]:
        """Remove all StoreGame entries for a store and clean up orphans.

        Orphan cleanup rules:
        - Game entries with no remaining StoreGame entries are candidates
        - Games with user data (favorites, tags, play sessions) are preserved
        - Only truly empty orphans are removed

        Args:
            store_name: Store name to remove data for

        Returns:
            Dict with removal stats
        """

        session = self.database.get_session()
        stats = {
            "store_games_removed": 0,
            "orphans_removed": 0,
            "orphans_preserved": 0,
        }

        try:
            store_games = session.query(StoreGame).filter_by(
                store_name=store_name,
            ).all()

            if not store_games:
                return stats

            affected_game_ids = {sg.game_id for sg in store_games}

            for sg in store_games:
                session.delete(sg)
            stats["store_games_removed"] = len(store_games)
            session.flush()

            for game_id in affected_game_ids:
                remaining = session.query(StoreGame).filter_by(
                    game_id=game_id,
                ).count()
                if remaining > 0:
                    continue

                game = session.query(DbGame).filter_by(id=game_id).options(
                    selectinload(DbGame.user_data),
                    selectinload(DbGame.tags),
                    selectinload(DbGame.play_sessions),
                ).first()
                if not game:
                    continue

                has_user_data = False
                if game.user_data:
                    ud = game.user_data
                    if ud.is_favorite or ud.is_hidden or ud.custom_notes or ud.launch_count > 0:
                        has_user_data = True
                if game.tags:
                    has_user_data = True
                if game.play_sessions:
                    has_user_data = True

                if has_user_data:
                    stats["orphans_preserved"] += 1
                    logger.info(f"Preserving orphaned game '{game.title}' (has user data)")
                else:
                    session.delete(game)
                    stats["orphans_removed"] += 1

            session.commit()
            self.invalidate_cache()

            logger.info(
                f"Reset {store_name}: {stats['store_games_removed']} store entries removed, "
                f"{stats['orphans_removed']} orphans removed, "
                f"{stats['orphans_preserved']} orphans preserved (have user data)"
            )

        except Exception as e:
            logger.error(f"Failed to remove store data for {store_name}: {e}")
            session.rollback()
            raise

        return stats

    def reconcile_stale_games(self, store_name: str, stale_ids: set) -> int:
        """Remove StoreGame entries for games no longer in the owned list.

        Runs orphan cleanup on affected Games using the same pattern as
        remove_store_data but targeting specific app_ids, not the entire store.
        Family shared games are excluded from removal.

        Args:
            store_name: Store identifier
            stale_ids: Set of store_app_ids no longer in the owned list

        Returns:
            Number of StoreGame entries removed
        """
        session = self.database.get_session()
        removed = 0
        try:
            for app_id in stale_ids:
                sg = session.query(StoreGame).filter_by(
                    store_name=store_name,
                    store_app_id=app_id,
                ).first()
                if not sg:
                    continue
                # Don't touch family shared games
                if sg.family_shared:
                    continue

                game_id = sg.game_id
                session.delete(sg)
                removed += 1

                # Orphan check: if no remaining store_games, check user data
                remaining = session.query(StoreGame).filter(
                    StoreGame.game_id == game_id,
                    StoreGame.id != sg.id,
                ).count()

                if remaining == 0:
                    game = session.query(DbGame).filter_by(id=game_id).options(
                        selectinload(DbGame.user_data),
                        selectinload(DbGame.tags),
                        selectinload(DbGame.play_sessions),
                    ).first()
                    if game:
                        has_user_data = False
                        if game.user_data:
                            ud = game.user_data
                            if (ud.is_favorite or ud.is_hidden
                                    or ud.custom_notes or ud.launch_count > 0):
                                has_user_data = True
                        if game.tags:
                            has_user_data = True
                        if game.play_sessions:
                            has_user_data = True

                        if not has_user_data:
                            session.delete(game)

            session.commit()
            if removed:
                self.invalidate_cache()
        except Exception:
            session.rollback()
            raise
        return removed

    def _save_last_account_id(
        self, store_name: str, account_id: Optional[str]
    ) -> None:
        """Persist the last-known account ID for a store.

        Args:
            store_name: Store identifier
            account_id: Account identifier string, or None to clear
        """
        ids = (self._config.get("sync.last_account_ids", {}) or {}).copy()
        if account_id:
            ids[store_name] = account_id
        else:
            ids.pop(store_name, None)
        self._config.set("sync.last_account_ids", ids)
        self._config.save()

    def _save_plugin_game(self, plugin_game: PluginGame) -> tuple:
        """Save a plugin game to the database

        Args:
            plugin_game: Game from plugin

        Returns:
            Tuple of (game_uuid: str, is_new: bool)
        """
        session = self.database.get_session()

        existing = session.query(StoreGame).filter_by(
            store_name=plugin_game.store_name,
            store_app_id=plugin_game.store_app_id,
        ).first()

        is_new = existing is None

        metadata = {
            "short_description": plugin_game.short_description,
            "description": plugin_game.description,
            "header_url": plugin_game.header_image_url,
            "cover_url": plugin_game.cover_image_url,
            "background_url": plugin_game.background_image_url,
            "screenshots": plugin_game.screenshots,
            "release_date": plugin_game.release_date,
            "developers": plugin_game.developers,
            "publishers": plugin_game.publishers,
            "genres": plugin_game.genres,
            "categories": plugin_game.categories,
            "tags": plugin_game.tags,
            "playtime_minutes": plugin_game.playtime_minutes,
        }

        game = find_or_create_game(
            session,
            title=plugin_game.title,
            store_name=plugin_game.store_name,
            store_app_id=plugin_game.store_app_id,
            launch_url=plugin_game.launch_url,
            metadata=metadata,
            family_shared=plugin_game.family_shared,
            family_shared_owner=plugin_game.family_shared_owner,
        )

        session.commit()
        return (game.id, is_new)

    def get_store_info(self, enabled_only: bool = True) -> List[tuple]:
        """Get info about store plugins (not metadata providers).

        Args:
            enabled_only: If True, only return enabled plugins (default True)

        Returns:
            List of (store_name, display_name, is_authenticated) tuples
        """
        result = []
        discovered = self.plugin_manager.get_discovered_plugins()

        for name, metadata in discovered.items():
            if "store" not in metadata.plugin_types:
                continue
            if metadata.hidden:
                continue

            if enabled_only:
                loaded = self.plugin_manager.get_loaded_plugin(name)
                if not loaded or not loaded.enabled:
                    continue

            plugin = self.plugin_manager.get_plugin(name)
            is_auth = plugin.is_authenticated() if plugin else False
            result.append((name, metadata.display_name, is_auth))

        return result

    def get_game_counts(self) -> tuple:
        """Get game counts for display.

        Returns:
            Tuple of (unique_games_count, total_store_games_count)
        """
        session = self.database.get_session()
        try:
            games_count = session.query(DbGame).count()
            store_games_count = session.query(StoreGame).count()
            return (games_count, store_games_count)
        except Exception as e:
            logger.error(f"Failed to get game counts: {e}")
            return (0, 0)

    def get_per_store_counts(self) -> Dict[str, int]:
        """Get game counts per store (cached, invalidated on cache rebuild).

        Returns:
            Dict mapping store_name -> count of store games
        """
        if self._store_counts_cache is not None:
            return self._store_counts_cache

        from sqlalchemy import func

        session = self.database.get_session()
        try:
            rows = (
                session.query(StoreGame.store_name, func.count())
                .group_by(StoreGame.store_name)
                .all()
            )
            self._store_counts_cache = {name: count for name, count in rows}
            return self._store_counts_cache
        except Exception as e:
            logger.error(f"Failed to get per-store counts: {e}")
            return {}

    # ── Delegation: TagService ─────────────────────────────────────────

    def _apply_tag_sync_data(self, store_name, tag_data):
        return self._tags._apply_tag_sync_data(store_name, tag_data)

    def _apply_metadata_tag_sync(self, source, mode, entries, removals=None):
        return self._tags._apply_metadata_tag_sync(source, mode, entries, removals)

    def _apply_install_sync_data(
        self, store_name: str, install_data: Dict[str, Any]
    ) -> Dict[str, int]:
        """Apply installation status from a store plugin to the main DB.

        Updates StoreGame.is_installed and install_path for all games belonging
        to the given store. Games in the mapping are marked installed; games NOT
        in the mapping are marked not installed (full sync).

        Args:
            store_name: Store identifier (e.g. "steam", "epic", "gog")
            install_data: Dict mapping store_app_id ->
                          {"installed": bool, "install_path": str|None}

        Returns:
            Dict with stats: {"installed": N, "cleared": N}
        """
        session = self.database.get_session()
        stats = {"installed": 0, "cleared": 0}

        try:
            store_games = session.query(StoreGame).filter_by(
                store_name=store_name
            ).all()

            for sg in store_games:
                info = install_data.get(sg.store_app_id)
                if info and info.get("installed"):
                    if not sg.is_installed or sg.install_path != info.get("install_path"):
                        sg.is_installed = True
                        sg.install_path = info.get("install_path")
                        stats["installed"] += 1
                else:
                    if sg.is_installed:
                        sg.is_installed = False
                        sg.install_path = None
                        stats["cleared"] += 1

            session.commit()
            total = stats["installed"] + stats["cleared"]
            if total > 0:
                logger.info(
                    f"Install sync for {store_name}: "
                    f"{stats['installed']} installed, {stats['cleared']} cleared"
                )
        except Exception as e:
            logger.error(f"Failed to apply install sync for {store_name}: {e}")
            session.rollback()

        return stats

    def _apply_family_license_data(
        self, store_name: str, counts: Dict[str, int]
    ) -> None:
        """Write per-game family license counts into StoreGame.metadata_json.

        Args:
            store_name: Store identifier (e.g. "steam")
            counts: Dict mapping store_app_id -> number of family owners
        """
        from sqlalchemy.orm.attributes import flag_modified

        session = self.database.get_session()
        updated = 0
        try:
            store_games = session.query(StoreGame).filter_by(
                store_name=store_name
            ).all()

            for sg in store_games:
                new_count = counts.get(sg.store_app_id, 0)
                if sg.metadata_json is None:
                    sg.metadata_json = {}
                old_count = sg.metadata_json.get("_family_license_count", 0)
                if old_count != new_count:
                    sg.metadata_json["_family_license_count"] = new_count
                    flag_modified(sg, "metadata_json")
                    updated += 1

            if updated:
                session.commit()
                logger.info(
                    f"Family license sync: updated {updated} "
                    f"{store_name} games ({len(counts)} apps in pool)"
                )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _apply_playtime_sync_data(
        self, store_name: str, playtime_data: Dict[str, Any]
    ) -> Dict[str, int]:
        """Apply playtime data from a store plugin to the main DB.

        For each entry: find StoreGame by store_name + store_app_id, then call
        import_playtime() which handles UserGameData + PlaySession creation.

        Args:
            store_name: Store identifier (e.g. "steam", "gog", "epic")
            playtime_data: Dict mapping store_app_id ->
                           {"minutes": int, "last_played": str|None}

        Returns:
            Dict with stats: {"imported": N, "skipped": N}
        """
        from datetime import datetime

        session = self.database.get_session()
        stats = {"imported": 0, "skipped": 0}
        source = f"{store_name}_import"

        try:
            for store_app_id, data in playtime_data.items():
                minutes = data.get("minutes", 0)
                last_played_str = data.get("last_played")

                # Parse last_played if provided
                last_played = None
                if last_played_str:
                    try:
                        last_played = datetime.fromisoformat(last_played_str)
                    except (ValueError, TypeError):
                        # Try common datetime formats
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                            try:
                                last_played = datetime.strptime(last_played_str, fmt)
                                break
                            except ValueError:
                                continue

                # Find the StoreGame to get the game_id
                store_game = session.query(StoreGame).filter_by(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                ).first()

                if not store_game:
                    stats["skipped"] += 1
                    continue

                if minutes > 0 or last_played:
                    self.import_playtime(
                        store_game.game_id,
                        store_name,
                        minutes,
                        last_played=last_played,
                        source=source,
                    )
                    stats["imported"] += 1

            total = stats["imported"]
            if total > 0:
                logger.info(
                    f"Playtime sync for {store_name}: "
                    f"{total} games imported, {stats['skipped']} skipped"
                )
        except Exception as e:
            logger.error(f"Failed to apply playtime sync for {store_name}: {e}")

        return stats

    def add_tag(self, game_id, tag_name):
        return self._tags.add_tag(game_id, tag_name)

    def remove_tag(self, game_id, tag_name):
        return self._tags.remove_tag(game_id, tag_name)

    def get_all_tags(self):
        return self._tags.get_all_tags()

    def get_tag_game_counts(self):
        return self._tags.get_tag_game_counts()

    def get_game_tags(self, game_id):
        return self._tags.get_game_tags(game_id)

    def create_tag(self, name, color=DEFAULT_TAG_COLOR, **kwargs):
        return self._tags.create_tag(name, color, **kwargs)

    def update_tag(self, tag_id, name=None, color=None, **kwargs):
        return self._tags.update_tag(tag_id, name, color, **kwargs)

    def delete_tag(self, tag_id):
        return self._tags.delete_tag(tag_id)

    def set_game_tags(self, game_id, tag_names):
        return self._tags.set_game_tags(game_id, tag_names)

    def get_tags_by_source(self, source):
        return self._tags.get_tags_by_source(source)

    def get_tags_by_type(self, tag_type):
        return self._tags.get_tags_by_type(tag_type)

    def merge_tags(self, keep_id, absorb_id):
        return self._tags.merge_tags(keep_id, absorb_id)

    def set_tag_score(self, tag_id, score):
        return self._tags.set_tag_score(tag_id, score)

    def get_scored_tags(self, min_score=1):
        return self._tags.get_scored_tags(min_score)

    def get_quick_access_tags(self, max_count=5):
        return self._tags.get_quick_access_tags(max_count)

    def get_tag_usage_counts(self):
        return self._tags.get_tag_usage_counts()

    def export_tags(self):
        return self._tags.export_tags()

    def import_tags(self, tag_data):
        return self._tags.import_tags(tag_data)

    # ── Delegation: UserDataService ────────────────────────────────────

    def set_favorite(self, game_id, is_favorite):
        return self._user_data.set_favorite(game_id, is_favorite)

    def set_hidden(self, game_id, is_hidden):
        return self._user_data.set_hidden(game_id, is_hidden)

    def set_game_notes(self, game_id, notes):
        return self._user_data.set_game_notes(game_id, notes)

    def set_nsfw_override(self, game_id, nsfw_override):
        return self._user_data.set_nsfw_override(game_id, nsfw_override)

    def record_launch(self, game_id, store_name=""):
        return self._user_data.record_launch(game_id, store_name)

    def get_launch_config(self, game_id):
        return self._user_data.get_launch_config(game_id)

    def set_launch_config(self, game_id, config):
        return self._user_data.set_launch_config(game_id, config)

    def end_play_session(self, session_id):
        return self._user_data.end_play_session(session_id)

    def import_playtime(
        self, game_id, store_name, playtime_minutes,
        last_played=None, source="import",
    ):
        return self._user_data.import_playtime(
            game_id, store_name, playtime_minutes,
            last_played, source,
        )

    def get_play_sessions_summary(self, game_id):
        return self._user_data.get_play_sessions_summary(game_id)

    # ── Delegation: LazyMetadata ───────────────────────────────────────

    def _is_valid_screenshot_path(self, path):
        return self._lazy_metadata._is_valid_screenshot_path(path)

    def _has_valid_screenshots(self, screenshots):
        return self._lazy_metadata._has_valid_screenshots(screenshots)

    def get_screenshots(self, game_id):
        return self._lazy_metadata.get_screenshots(game_id)

    def invalidate_screenshots(self, game_id, failed_urls):
        return self._lazy_metadata.invalidate_screenshots(game_id, failed_urls)

    def get_cover(self, game_id):
        return self._lazy_metadata.get_cover(game_id)

    def update_game_description(self, game_id, description):
        return self._lazy_metadata.update_game_description(game_id, description)

    def get_description(self, game_id):
        return self._lazy_metadata.get_description(game_id)

    def ensure_metadata_complete(self, game_id):
        return self._lazy_metadata.ensure_metadata_complete(game_id)

    def get_detail_fields(self, game_id):
        return self._lazy_metadata.get_detail_fields(game_id)

    def _persist_metadata_updates(self, game_id, updates):
        return self._lazy_metadata._persist_metadata_updates(game_id, updates)

    # ── Delegation: EnrichmentService ──────────────────────────────────

    def _should_override(self, field_name, provider_name, metadata, metadata_key=None):
        return self._enrichment._should_override(field_name, provider_name, metadata, metadata_key)

    def _apply_enrichments(self, store_name, enrichments, provider_name):
        return self._enrichment._apply_enrichments(store_name, enrichments, provider_name)

    async def force_rescan_game(self, game_id, progress_callback=None):
        def refresh():
            self._cache_valid = False
            self._refresh_cache()
        return await self._enrichment.force_rescan_game(
            game_id, progress_callback,
            refresh_cache_fn=refresh,
        )

    # ── Delegation: SyncOrchestrator ───────────────────────────────────

    def build_sync_jobs(self, stores=None, full_resync=False):
        return self._sync.build_sync_jobs(stores, full_resync)

    async def execute_store_fetch(
        self, store_name, full_resync=False,
        status_callback=None, cancel_check=None,
    ):
        return await self._sync.execute_store_fetch(
            store_name, full_resync,
            status_callback, cancel_check,
        )

    def create_skeleton_games(self, store_name, new_ids, **kwargs):
        return self._sync.create_skeleton_games(store_name, new_ids, **kwargs)

    async def execute_store_metadata_batch(
        self, store_name, app_ids,
        progress_callback=None, cancel_check=None,
        status_callback=None, new_games_callback=None,
        countdown_callback=None, budget_threshold=0,
    ):
        return await self._sync.execute_store_metadata_batch(
            store_name, app_ids, progress_callback,
            cancel_check, status_callback,
            new_games_callback, countdown_callback,
            budget_threshold,
        )

    def build_metadata_jobs(self, full_resync=False):
        return self._sync.build_metadata_jobs(full_resync)

    async def execute_metadata_batch(
        self, plugin_name, games, store_name,
        progress_callback=None, cancel_check=None,
        status_callback=None,
    ):
        return await self._sync.execute_metadata_batch(
            plugin_name, games, store_name,
            progress_callback, cancel_check,
            status_callback,
        )

    async def execute_metadata_plugin_run(
        self, plugin_name, games_by_store,
        batch_progress_callback=None,
        cancel_check=None, status_callback=None,
    ):
        return await self._sync.execute_metadata_plugin_run(
            plugin_name, games_by_store,
            batch_progress_callback, cancel_check,
            status_callback,
        )

    async def execute_single_enrichment(self, store_app_id, store_name, cancel_check=None):
        return await self._sync.execute_single_enrichment(store_app_id, store_name, cancel_check)

    async def re_enrich_games_for_plugin(
        self, game_ids: set, plugin_name: str
    ) -> None:
        """Re-enrich specific games with a single metadata plugin.

        Queries StoreGames for the given game_ids, picks one representative
        per parent game (preferring steam > gog > epic), and runs the
        metadata plugin enrichment pipeline on them.

        Used after ``reset_media_fields`` to batch-refetch covers/heroes
        from the next-best source (e.g. after blocking a SteamGridDB author).

        Args:
            game_ids: Set of parent Game UUIDs to re-enrich.
            plugin_name: Metadata plugin to run (e.g. "steamgriddb").
        """
        from .sync_orchestrator import _STORE_PREFERENCE

        session = self.database.get_session()
        try:
            store_games = (
                session.query(StoreGame)
                .filter(StoreGame.game_id.in_(game_ids))
                .options(
                    selectinload(StoreGame.game).load_only(
                        DbGame.id, DbGame.title,
                    ),
                )
                .all()
            )

            # Group by parent game, pick one representative per game
            by_game_id: Dict[str, list] = {}
            for sg in store_games:
                if sg.game:
                    by_game_id.setdefault(sg.game_id, []).append(sg)

            games_by_store: Dict[str, list] = {}
            for game_id, sgs in by_game_id.items():
                sgs_sorted = sorted(
                    sgs,
                    key=lambda s: _STORE_PREFERENCE.get(s.store_name, 99),
                )
                best = sgs_sorted[0]

                pg = PluginGame(
                    store_app_id=best.store_app_id,
                    store_name=best.store_name,
                    title=best.game.title if best.game else "",
                    launch_url=best.launch_url or "",
                )
                pg.siblings = [
                    (sg.store_name, sg.store_app_id)
                    for sg in sgs if sg is not best
                ]
                games_by_store.setdefault(best.store_name, []).append(pg)

            if games_by_store:
                total = sum(len(v) for v in games_by_store.values())
                logger.info(
                    "Batch re-enrichment: %d games via %s",
                    total, plugin_name,
                )
                await self._sync.execute_metadata_plugin_run(
                    plugin_name, games_by_store
                )
        except Exception:
            logger.error(
                "Batch re-enrichment failed for %s", plugin_name,
                exc_info=True,
            )
