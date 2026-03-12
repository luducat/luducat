# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sync_orchestrator.py

"""Sync orchestration service for luducat.

Manages the job-queue sync pipeline: building sync jobs, executing
store fetches, metadata enrichment batches, and cross-store propagation.
Extracted from GameService to reduce its responsibilities.
"""

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import load_only, selectinload
from sqlalchemy.orm.attributes import flag_modified

from .database import (
    Database,
    Game as DbGame,
    StoreGame,
    normalize_title,
)
from . import enrichment_state as es
from .enrichment_service import EnrichmentService
from .metadata_resolver import MetadataResolver
from .plugin_manager import PluginManager
from ..plugins.base import Game as PluginGame, RateLimitError

logger = logging.getLogger(__name__)


def _build_progressive_entry(
    game_uuid: str, game: PluginGame, store_name: str,
) -> dict:
    """Build a thin game dict for progressive UI loading.

    Contains only the fields available from the plugin's Game dataclass
    at save time — enough for display in the grid while sync continues.
    """
    return {
        "id": game_uuid,
        "title": game.title or "",
        "cover_image": game.cover_image_url or "",
        "header_image": game.header_image_url or "",
        "primary_store": store_name,
        "stores": [store_name],
        "store_app_ids": {store_name: game.store_app_id},
        "launch_urls": {store_name: game.launch_url or ""},
        "normalized_title": normalize_title(game.title or ""),
        "release_date": game.release_date or "",
        "developers": game.developers or [],
        "publishers": game.publishers or [],
        "genres": game.genres or [],
        "is_family_shared": bool(game.family_shared),
        "short_description": game.short_description or "",
    }

METADATA_JOB_BATCH_SIZE = 10

# Enrichment field mappings: (metadata_key, priority_field_name)
# Used for propagating enrichment data from primary to sibling store_games
ENRICHMENT_FIELD_MAPPINGS = [
    ("cover_url", "cover"),
    ("background_url", "hero"),
    ("screenshots", "screenshots"),
    ("genres", "genres"),
    ("developers", "developers"),
    ("publishers", "publishers"),
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

# Store preference order for dedup representative selection
_STORE_PREFERENCE = {"steam": 0, "gog": 1, "epic": 2}


class SyncOrchestrator:
    """Orchestrates store sync and metadata enrichment pipelines."""

    def __init__(
        self,
        database: Database,
        plugin_manager: PluginManager,
        resolver: MetadataResolver,
        enrichment_service: EnrichmentService,
        config=None,
    ):
        self.database = database
        self.plugin_manager = plugin_manager
        self._resolver = resolver
        self._enrichment = enrichment_service
        self._config = config

        # Callbacks injected by GameService for operations that stay there
        self._save_plugin_game_fn: Optional[Callable] = None
        self._get_synced_app_ids_fn: Optional[Callable] = None
        self._remove_family_shared_fn: Optional[Callable] = None
        self._invalidate_cache_fn: Optional[Callable] = None
        self._remove_store_data_fn: Optional[Callable] = None
        self._reconcile_stale_fn: Optional[Callable] = None
        self._save_account_id_fn: Optional[Callable] = None

    def set_game_service_callbacks(
        self,
        save_plugin_game: Callable,
        get_synced_app_ids: Callable,
        remove_family_shared: Callable,
        invalidate_cache: Callable,
        remove_store_data: Optional[Callable] = None,
        reconcile_stale: Optional[Callable] = None,
        save_account_id: Optional[Callable] = None,
    ) -> None:
        """Inject callbacks to GameService methods that stay on GameService.

        Args:
            save_plugin_game: GameService._save_plugin_game
            get_synced_app_ids: GameService._get_synced_app_ids
            remove_family_shared: GameService.remove_family_shared_games
            invalidate_cache: GameService.invalidate_cache
            remove_store_data: GameService.remove_store_data
            reconcile_stale: GameService.reconcile_stale_games
            save_account_id: GameService._save_last_account_id
        """
        self._save_plugin_game_fn = save_plugin_game
        self._get_synced_app_ids_fn = get_synced_app_ids
        self._remove_family_shared_fn = remove_family_shared
        self._invalidate_cache_fn = invalidate_cache
        self._remove_store_data_fn = remove_store_data
        self._reconcile_stale_fn = reconcile_stale
        self._save_account_id_fn = save_account_id

    def build_sync_jobs(
        self,
        stores: Optional[List[str]] = None,
        full_resync: bool = False,
    ) -> list:
        """Build the initial SyncJob list for the queue.

        Creates store fetch_games jobs for each authenticated store,
        then a sentinel job to trigger metadata job construction.

        Args:
            stores: Specific stores to sync, or None for all authenticated
            full_resync: If True, re-sync all games

        Returns:
            List of SyncJob objects
        """
        from .sync_queue import SyncJob, SyncPhase

        jobs = []
        plugins = self.plugin_manager.get_store_plugins()

        if stores:
            store_list = [s for s in stores if s in plugins]
        else:
            store_list = list(plugins.keys())

        last_account_ids = (
            (self._config.get("sync.last_account_ids", {}) or {})
            if self._config else {}
        )

        for store_name in store_list:
            plugin = plugins.get(store_name)
            if not plugin:
                continue

            if not plugin.is_authenticated():
                # Include unauthenticated stores that had a previous account
                # so execute_store_fetch() can reconcile (remove stale data)
                if store_name in last_account_ids:
                    logger.info(
                        f"{store_name}: not authenticated but has previous "
                        "account data — scheduling for reconciliation"
                    )
                else:
                    logger.info(f"Skipping {store_name}: not authenticated")
                    continue

            jobs.append(
                SyncJob(
                    phase=SyncPhase.STORE,
                    plugin_name=store_name,
                    task_type="fetch_games",
                    extra={"full_resync": full_resync},
                )
            )

        # Sentinel: after all stores, build metadata jobs dynamically
        jobs.append(
            SyncJob(
                phase=SyncPhase.METADATA,
                plugin_name="_system",
                task_type="build_metadata_jobs",
                extra={"full_resync": full_resync},
            )
        )

        logger.info(f"Built {len(jobs)} initial sync jobs ({len(jobs) - 1} stores)")
        return jobs

    async def execute_store_fetch(
        self,
        store_name: str,
        full_resync: bool = False,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple:
        """Fetch game IDs from a store.

        Returns (all_ids, new_ids) where new_ids are games not yet synced.

        Args:
            store_name: Store to fetch from
            full_resync: If True, treat all IDs as new
            status_callback: Optional callback for status messages
            cancel_check: Optional callback returning True if cancelled

        Returns:
            Tuple of (all_ids: List[str], new_ids: List[str])
        """
        plugin = self.plugin_manager.get_plugin(store_name)
        if not plugin:
            raise ValueError(f"Plugin not found: {store_name}")

        # Refresh settings to pick up changes
        self.plugin_manager.refresh_plugin_settings(store_name)

        # --- Account change detection ---
        current_account = (
            plugin.get_account_identifier() if plugin.is_authenticated() else None
        )
        last_account_ids = (
            (self._config.get("sync.last_account_ids", {}) or {})
            if self._config else {}
        )
        last_account = last_account_ids.get(store_name)

        if not plugin.is_authenticated():
            # Credentials removed → purge store data, skip fetch
            if last_account and self._remove_store_data_fn:
                logger.warning(
                    f"{store_name}: credentials removed, removing store data"
                )
                self._remove_store_data_fn(store_name)
                if self._save_account_id_fn:
                    self._save_account_id_fn(store_name, None)
            return [], []

        if (
            last_account
            and current_account
            and current_account != last_account
            and self._remove_store_data_fn
        ):
            # Different account → purge old data, force full resync
            logger.warning(
                f"{store_name}: account changed, reconciling ownership"
            )
            self._remove_store_data_fn(store_name)
            full_resync = True

        # Handle family sharing cleanup
        include_family_shared = plugin.get_setting("include_family_shared", False)
        logger.debug("Family sharing setting for %s: %s", store_name, include_family_shared)
        if not include_family_shared and self._remove_family_shared_fn:
            removed = self._remove_family_shared_fn(store_name)
            if removed > 0:
                logger.info(f"Removed {removed} family shared games")

        def fetch_status(message, *_args):
            if status_callback:
                status_callback(message)

        all_ids = await plugin.fetch_user_games(
            status_callback=fetch_status,
            cancel_check=cancel_check,
        )

        # --- Stale game removal (same account, games no longer owned) ---
        if all_ids and self._reconcile_stale_fn:
            synced_ids = (
                self._get_synced_app_ids_fn(store_name)
                if self._get_synced_app_ids_fn else set()
            )
            stale_ids = synced_ids - set(all_ids)
            if stale_ids:
                removed = self._reconcile_stale_fn(store_name, stale_ids)
                if removed:
                    logger.info(
                        f"{store_name}: removed {removed} stale games "
                        "no longer owned"
                    )

        # Save current account identifier
        if current_account and self._save_account_id_fn:
            self._save_account_id_fn(store_name, current_account)

        if full_resync:
            new_ids = all_ids
        else:
            synced_ids = (
                self._get_synced_app_ids_fn(store_name)
                if self._get_synced_app_ids_fn else set()
            )
            new_ids = [app_id for app_id in all_ids if app_id not in synced_ids]

        logger.info(
            f"{store_name}: {len(all_ids)} total, {len(new_ids)} new"
        )
        return all_ids, new_ids

    def create_skeleton_games(
        self,
        store_name: str,
        new_ids: List[str],
        new_games_callback: Optional[Callable[[list], None]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Create skeleton Game+StoreGame entries from plugin DB data.

        Uses the store plugin's local DB (no HTTP) to populate the main DB
        immediately after store fetch.  Games appear in the UI within seconds;
        metadata enrichment later updates these skeletons with richer data.

        Args:
            store_name: Store plugin name
            new_ids: App IDs to create skeletons for
            new_games_callback: Emits batches of progressive-entry dicts
            progress_callback: Reports (current, total) progress
            cancel_check: Returns True if cancelled

        Returns:
            Number of skeleton games created
        """
        plugin = self.plugin_manager.get_plugin(store_name)
        if not plugin:
            return 0

        # Bulk query plugin DB — single efficient query, no HTTP
        try:
            bulk_meta = plugin.get_games_metadata_bulk(new_ids)
        except Exception as e:
            logger.error(f"Skeleton bulk metadata failed for {store_name}: {e}")
            return 0

        if not bulk_meta:
            return 0

        total = len(bulk_meta)
        created = 0
        batch = []
        BATCH_SIZE = 10

        for idx, (app_id, meta) in enumerate(bulk_meta.items(), 1):
            if cancel_check and cancel_check():
                break

            title = meta.get("title") or f"App {app_id}"

            # Build launch URL from store page URL helper
            launch_url = ""
            try:
                launch_url = plugin.get_store_page_url(str(app_id))
            except Exception:
                pass

            plugin_game = PluginGame(
                store_app_id=str(app_id),
                store_name=store_name,
                title=title,
                launch_url=launch_url,
                short_description=meta.get("short_description"),
                cover_image_url=meta.get("cover") or meta.get("cover_url") or "",
                header_image_url=meta.get("header_url") or "",
                background_image_url=meta.get("background_url") or "",
                release_date=meta.get("release_date"),
                developers=meta.get("developers", []),
                publishers=meta.get("publishers", []),
                genres=meta.get("genres", []),
                screenshots=meta.get("screenshots", []),
            )

            if self._save_plugin_game_fn:
                game_uuid, is_new = self._save_plugin_game_fn(plugin_game)
                if is_new:
                    created += 1
                    if new_games_callback:
                        batch.append(
                            _build_progressive_entry(
                                game_uuid, plugin_game, store_name,
                            )
                        )
                        if len(batch) >= BATCH_SIZE:
                            new_games_callback(batch)
                            batch = []

            if progress_callback:
                progress_callback(idx, total)

        # Flush remaining batch
        if batch and new_games_callback:
            new_games_callback(batch)

        logger.info(
            f"{store_name}: created {created} skeleton games "
            f"from {total} plugin DB entries"
        )
        return created

    async def execute_store_metadata_batch(
        self,
        store_name: str,
        app_ids: List[str],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        new_games_callback: Optional[Callable[[list], None]] = None,
        countdown_callback: Optional[Callable[[int], None]] = None,
        budget_threshold: int = 0,
    ) -> Dict[str, Any]:
        """Fetch and save store metadata for a batch of game IDs.

        Args:
            store_name: Store name
            app_ids: Game IDs to fetch metadata for
            progress_callback: Optional callback(game_name, current, total)
            cancel_check: Optional callback returning True if cancelled
            status_callback: Optional callback(message) for rate limit status
            new_games_callback: Optional callback(batch) for progressive UI loading
            countdown_callback: Optional callback(wait_seconds) for countdown display
            budget_threshold: If > 0, pause at this request count for interleave

        Returns:
            Stats dict with games_added, games_updated, errors.
            May include _budget_paused=True and _remaining_app_ids if paused.
        """
        plugin = self.plugin_manager.get_plugin(store_name)
        if not plugin:
            return {"error": f"Plugin not found: {store_name}"}

        stats = {"games_added": 0, "games_updated": 0, "errors": 0}
        total = len(app_ids)
        max_rate_limit_retries = 3

        new_games_batch = []

        for idx, app_id in enumerate(app_ids, 1):
            if cancel_check and cancel_check():
                break

            # Budget check: pause before hitting proactive rate limit
            if budget_threshold > 0 and hasattr(plugin, "get_api_budget_status"):
                budget = plugin.get_api_budget_status()
                if (
                    budget
                    and not budget.get("in_cooldown")
                    and budget["request_count"] >= budget_threshold
                ):
                    stats["_budget_paused"] = True
                    stats["_remaining_app_ids"] = list(app_ids[idx - 1:])
                    logger.info(
                        f"{store_name}: budget threshold reached "
                        f"({budget['request_count']}/{budget['budget_limit']}), "
                        f"{len(stats['_remaining_app_ids'])} games remaining"
                    )
                    break

            games = None
            rate_retry = 0
            while rate_retry <= max_rate_limit_retries:
                try:
                    games = await plugin.fetch_game_metadata([app_id])
                    break
                except RateLimitError as e:
                    rate_retry += 1
                    if rate_retry > max_rate_limit_retries:
                        stats["errors"] += 1
                        if countdown_callback:
                            countdown_callback(0)
                        if status_callback:
                            status_callback("")
                        break
                    reason = getattr(e, "reason", "rate limit")
                    remaining = e.wait_seconds
                    # Signal countdown start
                    if countdown_callback:
                        countdown_callback(remaining)
                    while remaining > 0:
                        if remaining >= 60:
                            time_str = f"{(remaining + 59) // 60} min"
                        else:
                            time_str = f"{remaining}s"
                        if status_callback:
                            status_callback(
                                f"Steam rate limited ({reason}): "
                                f"{time_str} remaining "
                                f"(attempt {rate_retry}/{max_rate_limit_retries})"
                            )
                        chunk = min(10, remaining)
                        await asyncio.sleep(chunk)
                        remaining -= chunk
                        if cancel_check and cancel_check():
                            break
                    # Signal countdown end
                    if countdown_callback:
                        countdown_callback(0)
                    if status_callback:
                        status_callback("")
                    if cancel_check and cancel_check():
                        break
                except Exception as e:
                    logger.error(f"Failed to fetch metadata for {app_id}: {e}")
                    stats["errors"] += 1
                    break

            if games and self._save_plugin_game_fn:
                game = games[0]
                if progress_callback:
                    progress_callback(game.title or f"App {app_id}", idx, total)
                game_uuid, is_new = self._save_plugin_game_fn(game)
                if is_new:
                    stats["games_added"] += 1
                    # Accumulate for progressive UI loading
                    if new_games_callback:
                        new_games_batch.append(
                            _build_progressive_entry(
                                game_uuid, game, store_name
                            )
                        )
                        if len(new_games_batch) >= 100:
                            new_games_callback(new_games_batch)
                            new_games_batch = []
                else:
                    stats["games_updated"] += 1

        # Flush remaining progressive batch
        if new_games_batch and new_games_callback:
            new_games_callback(new_games_batch)

        if self._invalidate_cache_fn:
            self._invalidate_cache_fn()
        return stats

    def build_metadata_jobs(self, full_resync: bool = False) -> list:
        """Build SyncJob list for the metadata enrichment phase.

        Creates ONE consolidated SyncJob per metadata plugin (in priority
        order).  Each job carries all unenriched games across all stores,
        deduped by parent Game — the worker handles internal batching.

        Args:
            full_resync: If True, retry games previously attempted with
                         no match (ignores ``_attempted_by`` markers).

        Returns:
            List of SyncJob objects for metadata enrichment (one per plugin)
        """
        from .sync_queue import SyncJob, SyncPhase

        jobs = []
        metadata_plugins = self.plugin_manager.get_metadata_plugins()
        from luducat.core.plugin_manager import PluginManager
        ordered_names = PluginManager.get_enrichment_plugin_names()

        for plugin_name in ordered_names:
            plugin = metadata_plugins.get(plugin_name)
            if not plugin:
                continue
            if not plugin.is_available():
                logger.info(f"Skipping {plugin_name}: not available")
                continue
            if plugin.get_setting("skip_on_sync", False):
                logger.info(f"Skipping {plugin_name}: skip_on_sync is set")
                continue

            unenriched_by_store = self._get_unenriched_games_for_plugin(
                plugin_name, full_resync=full_resync,
            )
            total_games = sum(len(gl) for gl in unenriched_by_store.values())
            if total_games == 0:
                logger.info(f"{plugin_name}: all games already enriched")
                continue

            jobs.append(
                SyncJob(
                    phase=SyncPhase.METADATA,
                    plugin_name=plugin_name,
                    task_type="enrich_plugin",
                    game_ids=[],
                    store_name="",
                    batch_index=1,
                    batch_total=1,
                    extra={"games_by_store": unenriched_by_store},
                )
            )

            stores = ", ".join(
                f"{s}({len(gl)})" for s, gl in unenriched_by_store.items()
            )
            logger.info(
                f"{plugin_name}: {total_games} games across "
                f"{len(unenriched_by_store)} stores -> 1 consolidated job "
                f"[{stores}]"
            )

        logger.info(f"Built {len(jobs)} metadata enrichment jobs total")
        return jobs

    def _get_unenriched_games_for_plugin(
        self,
        plugin_name: str,
        full_resync: bool = False,
    ) -> Dict[str, list]:
        """Find games that need enrichment from a specific plugin.

        Uses SQL-level json_extract filtering to avoid loading already-enriched
        rows into memory. Groups remaining store_games by parent Game to
        deduplicate across stores. For multi-store games, picks ONE
        representative (preferring steam > gog > epic) and attaches sibling
        info for post-enrichment propagation.

        Args:
            plugin_name: Metadata plugin name (e.g., "igdb", "protondb")
            full_resync: If True, ignore _attempted_by markers

        Returns:
            Dict mapping store_name -> List[PluginGame]
            Each PluginGame has a ``siblings`` field containing
            ``[(store_name, store_app_id), ...]`` for propagation.
        """
        session = self.database.get_session()

        # Step 1: Find game_ids that are ALREADY enriched/attempted in SQL.
        # A game_id is "done" if ANY of its store_games is enriched or attempted.
        # We collect these IDs, then exclude them from the main query.
        #
        # Enrichment state lives in metadata_json._sources:
        #   _sources._enriched_via = "plugin"           (sibling dedup)
        #   _sources._attempted_by = ["plugin", ...]    (tried, no match)
        #   _sources.<field> = "plugin"                  (actual enrichment)
        #
        # For is_enriched_by: any value in _sources equals plugin_name.
        # We use json_each() to iterate _sources values efficiently.

        enriched_via_path = "$._sources._enriched_via"
        attempted_path = "$._sources._attempted_by"
        sources_path = "$._sources"

        # Build SQL conditions for "this store_game is done for this plugin"
        # Condition 1: enriched via sibling
        cond_via = text(
            "json_extract(store_games.metadata_json, :via_path) = :plugin"
        )
        # Condition 2: attempted by plugin (not in full_resync mode)
        # json_each on the _attempted_by array, check if any value = plugin
        cond_attempted_subquery = text(
            "EXISTS (SELECT 1 FROM json_each("
            "  json_extract(store_games.metadata_json, :attempted_path)"
            ") WHERE json_each.value = :plugin)"
        )
        # Condition 3: enriched by plugin (any field source = plugin)
        # json_each on _sources dict, check values (excluding internal keys)
        cond_enriched_subquery = text(
            "EXISTS (SELECT 1 FROM json_each("
            "  json_extract(store_games.metadata_json, :sources_path)"
            ") WHERE json_each.value = :plugin"
            "  AND json_each.key NOT IN ('_attempted_by', '_enriched_via'))"
        )

        params = dict(
            via_path=enriched_via_path,
            attempted_path=attempted_path,
            sources_path=sources_path,
            plugin=plugin_name,
        )

        if full_resync:
            # Only skip truly enriched games, ignore attempted markers
            done_condition = or_(cond_via, cond_enriched_subquery)
        else:
            done_condition = or_(cond_via, cond_attempted_subquery, cond_enriched_subquery)

        # Get game_ids where ANY store_game is "done"
        done_game_ids_q = (
            session.query(StoreGame.game_id)
            .filter(done_condition)
            .params(**params)
            .distinct()
        )

        # Step 2: Load only unenriched store_games with minimal columns
        query = (
            session.query(StoreGame)
            .filter(~StoreGame.game_id.in_(done_game_ids_q))
            .options(
                load_only(
                    StoreGame.game_id,
                    StoreGame.store_name,
                    StoreGame.store_app_id,
                    StoreGame.launch_url,
                ),
                selectinload(StoreGame.game).load_only(
                    DbGame.id, DbGame.title,
                ),
            )
        )
        store_games = query.all()

        # Step 3: Group by parent Game.id for cross-store dedup
        by_game_id: Dict[str, list] = {}
        for sg in store_games:
            if sg.game:
                by_game_id.setdefault(sg.game.id, []).append(sg)

        by_store: Dict[str, list] = {}
        for game_id, sgs in by_game_id.items():
            # Pick the best representative (prefer steam > gog > epic)
            sgs_sorted = sorted(
                sgs,
                key=lambda s: _STORE_PREFERENCE.get(s.store_name, 99),
            )
            best = sgs_sorted[0]

            game = PluginGame(
                store_app_id=best.store_app_id,
                store_name=best.store_name,
                title=best.game.title if best.game else "",
                launch_url=best.launch_url or "",
            )
            # Track sibling store_games for enrichment propagation
            game.siblings = [
                (sg.store_name, sg.store_app_id)
                for sg in sgs if sg is not best
            ]

            by_store.setdefault(best.store_name, []).append(game)

        return by_store

    @staticmethod
    def _is_enriched_or_attempted(sg, plugin_name: str, full_resync: bool) -> bool:
        """Check if a store_game should be skipped for a plugin.

        Returns True if the game has real enrichment data from the plugin,
        was enriched by proxy (cross-store dedup), or was previously
        attempted with no match (unless full_resync overrides).
        """
        metadata = sg.metadata_json or {}

        if es.is_enriched_by(metadata, plugin_name):
            return True

        if es.is_enriched_via_sibling(metadata, plugin_name):
            return True

        if not full_resync and es.is_attempted_by(metadata, plugin_name):
            return True

        return False

    async def execute_metadata_batch(
        self,
        plugin_name: str,
        games: list,
        store_name: str,
        progress_callback: Optional[Callable] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute one metadata enrichment batch for a single plugin.

        Calls the plugin's enrich_games() with the batch, then applies
        enrichments to the DB immediately (checkpoint).

        Args:
            plugin_name: Metadata plugin name
            games: List of PluginGame objects to enrich
            store_name: Which store's games
            progress_callback: Optional callback(game_name, current, total)
            cancel_check: Optional callback returning True if cancelled
            status_callback: Optional callback(message) for rate limits

        Returns:
            Stats dict with games_enriched count
        """
        stats = {"games_enriched": 0, "errors": 0}

        if not games:
            return stats

        enrichments = None
        try:
            enrichments = await self._resolver.enrich_games_single_plugin(
                plugin_name,
                store_name,
                games,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                status_callback=status_callback,
            )

            if enrichments:
                enriched_count = self._enrichment._apply_enrichments(
                    store_name, enrichments, plugin_name,
                )
                stats["games_enriched"] = enriched_count
        except Exception as e:
            logger.error(f"Metadata batch failed ({plugin_name}): {e}")
            stats["errors"] += 1

        # Only mark attempted if batch completed normally (not cancelled)
        if not (cancel_check and cancel_check()):
            self._mark_attempted(store_name, games, enrichments or {}, plugin_name)

        return stats

    async def execute_metadata_plugin_run(
        self,
        plugin_name: str,
        games_by_store: Dict[str, list],
        batch_progress_callback: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute a consolidated metadata enrichment run for one plugin.

        Processes all stores and batches internally, calling
        execute_metadata_batch() per batch.  Reports game-level progress
        via batch_progress_callback(games_processed, total_games).

        Args:
            plugin_name: Metadata plugin name
            games_by_store: Dict mapping store_name -> List[PluginGame]
            batch_progress_callback: Optional callback(games_done, total_games)
            cancel_check: Optional callback returning True if cancelled
            status_callback: Optional callback(message) for status messages

        Returns:
            Stats dict with games_enriched and errors counts
        """
        stats = {"games_enriched": 0, "errors": 0}

        # Check cancellation BEFORE starting any work for this plugin
        if cancel_check and cancel_check():
            return stats

        batch_size = METADATA_JOB_BATCH_SIZE

        total_games = sum(len(gl) for gl in games_by_store.values())
        games_processed = 0

        for store_name, game_list in games_by_store.items():
            if not game_list:
                continue

            batches = [
                game_list[i : i + batch_size]
                for i in range(0, len(game_list), batch_size)
            ]

            for batch in batches:
                if cancel_check and cancel_check():
                    return stats

                # Capture current offset for the closure
                batch_offset = games_processed
                batch_high_water = [0]  # mutable for closure

                def per_game_progress(message, current, total):
                    """Forward per-game progress from plugin to UI.

                    Uses a high-water mark so progress only advances forward.
                    Plugins may report phase-local counters that reset between
                    phases (e.g. lookup → heroes → covers), but the UI should
                    show smooth linear progress.
                    """
                    if current > batch_high_water[0] and batch_progress_callback:
                        batch_high_water[0] = current
                        cumulative = batch_offset + current
                        batch_progress_callback(
                            min(cumulative, total_games), total_games,
                        )

                batch_stats = await self.execute_metadata_batch(
                    plugin_name,
                    batch,
                    store_name,
                    progress_callback=per_game_progress,
                    cancel_check=cancel_check,
                    status_callback=status_callback,
                )
                stats["games_enriched"] += batch_stats.get("games_enriched", 0)
                stats["errors"] += batch_stats.get("errors", 0)

                self._propagate_enrichment_to_siblings(
                    plugin_name, batch, store_name,
                )

                games_processed += len(batch)
                if batch_progress_callback:
                    batch_progress_callback(games_processed, total_games)

        return stats

    async def execute_single_enrichment(
        self,
        store_app_id: str,
        store_name: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Priority enrichment for a single game using all plugins.

        Called when a user clicks a game during sync and it lacks metadata.

        Args:
            store_app_id: Game's store app ID
            store_name: Game's store name
            cancel_check: Optional callback returning True if cancelled

        Returns:
            Stats dict
        """
        session = self.database.get_session()
        sg = session.query(StoreGame).filter_by(
            store_name=store_name,
            store_app_id=store_app_id,
        ).first()

        if not sg:
            return {"enriched": False, "reason": "not_found"}

        game = PluginGame(
            store_app_id=sg.store_app_id,
            store_name=sg.store_name,
            title=sg.game.title if sg.game else "",
            launch_url=sg.launch_url or "",
        )

        try:
            enrichments = await self._resolver.enrich_games_batch(
                store_name,
                [game],
                cancel_check=cancel_check,
            )
            if enrichments:
                self._enrichment._apply_enrichments(store_name, enrichments, "merged")
                return {"enriched": True}
        except Exception as e:
            logger.error(f"Single enrichment failed for {store_app_id}: {e}")

        return {"enriched": False}

    def _propagate_enrichment_to_siblings(
        self,
        plugin_name: str,
        batch: list,
        store_name: str,
    ) -> None:
        """Propagate enrichment data and markers to sibling store_games.

        When a representative store_game is enriched, its siblings on
        other stores receive:
        1. Enriched field values (respecting priority — only overwrites
           when the enrichment source has higher priority than the
           sibling's current source for that field)
        2. An ``_enriched_via`` marker so they are not re-processed

        Args:
            plugin_name: Metadata plugin that ran
            batch: List of PluginGame objects that were just enriched
            store_name: Store of the representative game
        """
        session = self.database.get_session()
        any_changed = False

        for game in batch:
            siblings = game.siblings or []
            if not siblings:
                continue

            primary_sg = session.query(StoreGame).filter_by(
                store_name=store_name,
                store_app_id=game.store_app_id,
            ).first()
            if not primary_sg:
                continue

            primary_meta = primary_sg.metadata_json or {}
            if not es.is_enriched_by(primary_meta, plugin_name):
                continue

            for sib_store, sib_app_id in siblings:
                sib_sg = session.query(StoreGame).filter_by(
                    store_name=sib_store,
                    store_app_id=sib_app_id,
                ).first()
                if not sib_sg:
                    continue
                sib_meta = sib_sg.metadata_json or {}

                # Copy enrichment field values using priority
                for attr_name, field_name in ENRICHMENT_FIELD_MAPPINGS:
                    source = es.get_field_source(primary_meta, field_name)
                    if not source:
                        continue
                    value = primary_meta.get(attr_name)
                    if not self._resolver._is_non_empty(value):
                        continue
                    # Check if enrichment source has higher priority
                    sib_source = es.get_field_source(sib_meta, field_name) or sib_store
                    new_rank = self._resolver.get_field_priority_rank(field_name, source)
                    sib_rank = self._resolver.get_field_priority_rank(field_name, sib_source)
                    if new_rank < sib_rank:
                        sib_meta[attr_name] = value
                        es.mark_field_source(sib_meta, field_name, source)

                # Mark enriched via sibling
                if not es.is_enriched_via_sibling(sib_meta, plugin_name):
                    es.mark_enriched_via_sibling(sib_meta, plugin_name)
                sib_sg.metadata_json = sib_meta
                any_changed = True

        if any_changed:
            session.commit()

    def _mark_attempted(
        self,
        store_name: str,
        games: list,
        enrichments: Dict[str, Any],
        plugin_name: str,
    ) -> None:
        """Mark games as attempted by a plugin even when no data returned.

        Games that DID get enrichment data already have ``_sources`` set by
        ``_apply_enrichments``.  This handles the remainder — games the
        plugin processed but couldn't find a match for.

        Args:
            store_name: Store name
            games: List of PluginGame objects sent for enrichment
            enrichments: Enrichment results (keyed by store_app_id)
            plugin_name: Metadata plugin that ran
        """
        attempted_ids = {g.store_app_id for g in games} - set(enrichments.keys())
        if not attempted_ids:
            return

        session = self.database.get_session()
        for app_id in attempted_ids:
            sg = session.query(StoreGame).filter_by(
                store_name=store_name,
                store_app_id=app_id,
            ).first()
            if not sg:
                continue

            meta = sg.metadata_json or {}

            if es.is_enriched_by(meta, plugin_name):
                continue
            if es.is_enriched_via_sibling(meta, plugin_name):
                continue

            if es.mark_attempted(meta, plugin_name):
                sg.metadata_json = meta
                flag_modified(sg, "metadata_json")

        session.commit()
