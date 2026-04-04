# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# workers.py

"""Background workers for luducat

Provides QThread-based workers for:
- Async operations (coroutines)
- Sync store operations (sequential job queue)
- Image loading
"""

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QThread, Signal, QObject

from luducat.plugins.base import AuthenticationError

logger = logging.getLogger(__name__)


class DataLoaderWorker(QThread):
    """Worker thread for loading data from database

    Used to load games in background without freezing UI.

    Signals:
        finished: Emitted when load completes with result
        error: Emitted on error with exception message
    """

    finished = Signal(object)  # result (list of games)
    error = Signal(str)  # error message

    def __init__(
        self,
        load_func: Callable,
        parent: Optional[QObject] = None,
    ):
        """Initialize data loader worker

        Args:
            load_func: Synchronous function to call
            parent: Parent QObject
        """
        super().__init__(parent)
        self._load_func = load_func
        self._result = None

    def run(self) -> None:
        """Run the load function in background thread"""
        try:
            self._result = self._load_func()
            self.finished.emit(self._result)
        except Exception as e:
            logger.exception(f"DataLoaderWorker error: {e}")
            self.error.emit(str(e))

    def get_result(self) -> Any:
        """Get the result after completion"""
        return self._result


class AsyncWorker(QThread):
    """Worker thread for running async coroutines

    Signals:
        finished: Emitted when work completes with result
        error: Emitted on error with exception message
        progress: Emitted during work with (message, current, total)
    """

    finished = Signal(object)  # result
    error = Signal(str)  # error message
    progress = Signal(str, int, int)  # message, current, total

    def __init__(
        self,
        coro_func: Callable,
        *args,
        parent: Optional[QObject] = None,
        **kwargs
    ):
        """Initialize async worker

        Args:
            coro_func: Async function to run
            *args: Positional arguments for coro_func
            parent: Parent QObject
            **kwargs: Keyword arguments for coro_func
        """
        super().__init__(parent)

        self._coro_func = coro_func
        self._args = args
        self._kwargs = kwargs
        self._result = None

    def run(self) -> None:
        """Run the async function in a new event loop"""
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                # Run the coroutine
                self._result = loop.run_until_complete(
                    self._coro_func(*self._args, **self._kwargs)
                )
                self.finished.emit(self._result)

            finally:
                loop.close()

        except Exception as e:
            logger.exception(f"AsyncWorker error: {e}")
            self.error.emit(str(e))

    def get_result(self) -> Any:
        """Get the result after completion"""
        return self._result


class SyncWorker(QThread):
    """Worker that processes SyncJobs sequentially from a SyncJobQueue.

    Replaces the old parallel sync approach. One worker thread pulls jobs
    one at a time, executing them via GameService methods. Progress is
    reported per-phase with per-game granularity.

    Signals:
        phase_started(plugin, description, total_count)
        phase_progress(plugin, current, total)
        phase_finished(plugin)
        sync_finished(stats)
        sync_error(message)
        rate_limit(message)
        cancelled(partial_stats)
    """

    phase_started = Signal(str, str, int)  # plugin, description, total_count
    phase_progress = Signal(str, int, int)  # plugin, current, total
    phase_finished = Signal(str)  # plugin
    sync_finished = Signal(dict)  # final stats
    sync_error = Signal(str)  # error message
    rate_limit = Signal(str)  # rate limit status message
    rate_limit_countdown = Signal(str, int)  # plugin, wait_seconds
    cancelled = Signal(dict)  # partial stats
    games_batch_ready = Signal(list)  # progressive loading: list of game dicts
    cache_refresh_requested = Signal()  # enrichment interleave done
    sync_warning = Signal(str, str)  # title, message

    def __init__(
        self,
        game_service,
        queue,
        full_resync: bool = False,
        parent: Optional[QObject] = None,
    ):
        """Initialize sync worker.

        Args:
            game_service: GameService instance
            queue: SyncJobQueue instance
            full_resync: If True, re-sync all games
            parent: Parent QObject
        """
        super().__init__(parent)
        self._game_service = game_service
        self._queue = queue
        self._full_resync = full_resync
        self._stats: Dict[str, Any] = {}
        # Track cumulative progress across metadata batches per store
        self._store_metadata_progress: Dict[str, Dict[str, int]] = {}
        self._sync_warnings: list[tuple[str, str]] = []  # (title, message)

    @property
    def queue(self):
        """Expose queue for priority job insertion from UI thread."""
        return self._queue

    def run(self) -> None:
        """Process jobs from the queue sequentially."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while True:
                job = self._queue.next_job()
                if job is None:
                    break

                # Check if a plugin was skipped between jobs
                skip_for = self._queue.skip_requested_for
                if skip_for:
                    if job.plugin_name == skip_for:
                        # This job is for the skipped plugin — drop it
                        self._queue.job_completed()
                        self._queue.clear_skip()
                        self._store_metadata_progress.pop(skip_for, None)
                        self.phase_finished.emit(skip_for)
                        logger.info(f"Skipped job: {job.description}")
                        continue
                    else:
                        # Stale skip flag for a different plugin — the
                        # previous job's handler already emitted
                        # phase_finished via cancel_or_skip(). Just clear.
                        self._queue.clear_skip()
                        self._store_metadata_progress.pop(skip_for, None)

                logger.info(f"Sync job: {job.description}")

                try:
                    loop.run_until_complete(self._execute_job(job))
                except AuthenticationError as e:
                    logger.warning(f"Auth failed ({job.description}): {e}")
                    title = _("{store}: Authentication Failed").format(
                        store=job.plugin_name
                    )
                    msg = _(
                        "Could not fetch games — your login session has "
                        "expired.\n\nPlease re-connect in Settings → Plugins."
                    )
                    self._sync_warnings.append((title, msg))
                except Exception as e:
                    logger.error(f"Job failed ({job.description}): {e}")
                    self._stats.setdefault("errors", []).append(
                        {"job": job.description, "error": str(e)}
                    )

                self._queue.job_completed()

                # If skip was requested during this job's execution, the
                # handler already emitted phase_finished. Clear the flag
                # now to prevent a duplicate emit on the next iteration.
                skip_for = self._queue.skip_requested_for
                if skip_for == job.plugin_name:
                    self._queue.clear_skip()
                    self._store_metadata_progress.pop(skip_for, None)

        except Exception as e:
            logger.exception(f"SyncWorker fatal error: {e}")
            self.sync_error.emit(str(e))
            return
        finally:
            # Clean up pending async tasks
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            loop.close()

        if self._queue.is_cancelled:
            self.cancelled.emit(self._stats)
        else:
            # Emit any collected warnings before finishing
            for title, msg in self._sync_warnings:
                self.sync_warning.emit(title, msg)
            self.sync_finished.emit(self._stats)

    async def _execute_job(self, job) -> None:
        """Dispatch a job to the appropriate GameService method."""
        from ..core.sync_queue import SyncPhase

        if job.phase == SyncPhase.STORE:
            if job.task_type == "fetch_games":
                await self._exec_store_fetch(job)
            elif job.task_type == "fetch_metadata":
                await self._exec_store_metadata(job)

        elif job.phase == SyncPhase.METADATA:
            if job.task_type == "build_metadata_jobs":
                await self._exec_build_metadata_jobs(job)
            elif job.task_type == "enrich_plugin":
                await self._exec_metadata_plugin(job)
            elif job.task_type == "enrich_single":
                await self._exec_single_enrichment(job)
            elif job.task_type == "tag_sync":
                await self._exec_tag_sync(job)

    async def _exec_store_fetch(self, job) -> None:
        """Execute a store fetch_games job.

        Fetches game IDs, then dynamically creates fetch_metadata jobs
        for new (unsynced) games and adds them to the queue.
        """
        from ..core.sync_queue import SyncJob, SyncPhase

        store_name = job.plugin_name

        def cancel_or_skip():
            if self._queue.is_cancelled:
                return True
            skip_for = self._queue.skip_requested_for
            return skip_for == store_name if skip_for else False

        self.phase_started.emit(store_name, "loading game list...", 0)

        import re
        _page_re = re.compile(r"page\s+(\d+)/(\d+)", re.IGNORECASE)
        _last_structured_total = [None]  # mutable for closure

        def _status_with_page_progress(
            message: str, current=None, total=None,
        ) -> None:
            """Route progress to the progress bar.

            Handles two call signatures used by store plugins:
            - 1-arg: text message, e.g. "page 3/42" (parsed via regex)
            - 3-arg: structured progress (message, current, total)
            """
            if current is not None and total is not None:
                # Structured progress (enrichment, catalog fetch).
                # Emit phase_started when total changes (new sub-phase).
                if total != _last_structured_total[0]:
                    _last_structured_total[0] = total
                    self.phase_started.emit(store_name, message, total)
                self.phase_progress.emit(store_name, current, total)
                return
            m = _page_re.search(message)
            if m:
                page, total_pages = int(m.group(1)), int(m.group(2))
                self.phase_progress.emit(store_name, page, total_pages)
                return  # Page progress shown in progress bar, not status bar
            self._on_status(message)

        all_ids, new_ids, refetch_ids = await self._game_service.execute_store_fetch(
            store_name,
            full_resync=self._full_resync,
            status_callback=_status_with_page_progress,
            cancel_check=cancel_or_skip,
        )

        self._stats.setdefault(store_name, {})
        self._stats[store_name]["games_found"] = len(all_ids)
        self._stats[store_name]["games_new"] = len(new_ids)
        self._stats[store_name]["games_refetch"] = len(refetch_ids)

        # Don't create metadata jobs if cancelled during fetch
        if cancel_or_skip():
            self._game_service.invalidate_cache()
            self.phase_finished.emit(store_name)
            return

        # Apply local data syncs (tags, install status, playtime)
        self.phase_started.emit(store_name, "syncing local data...", 0)
        self._apply_store_tag_sync(store_name)
        self._apply_family_shared_tag_sync(store_name)
        self._apply_store_install_sync(store_name)
        self._apply_store_playtime_sync(store_name)
        self._apply_family_license_sync(store_name)

        # Collect family sharing warnings (Steam only)
        self._collect_family_sharing_warning(store_name)

        # Bulk preparation hook (e.g. GOG catalog scan for covers)
        # Runs BEFORE skeleton creation so plugin DB has enriched data
        # (vertical covers, screenshots, descriptions) when skeletons read it.
        if not cancel_or_skip():
            from ..plugins.base import StorePlugin
            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if (
                isinstance(plugin, StorePlugin)
                and type(plugin).prepare_metadata
                is not StorePlugin.prepare_metadata
            ):
                display_name = (
                    self._game_service.plugin_manager
                    .get_store_display_name(store_name)
                )
                self.phase_started.emit(
                    store_name,
                    f"scanning {display_name} catalog...",
                    0,
                )
                await plugin.prepare_metadata(
                    status_callback=_status_with_page_progress,
                    cancel_check=cancel_or_skip,
                )

        # Create skeleton games from plugin DB (local, no HTTP)
        # so games appear in the UI immediately
        if new_ids:
            self.phase_started.emit(
                store_name, "adding games...", len(new_ids),
            )

            def skeleton_cb(batch):
                self.games_batch_ready.emit(batch)

            self._game_service.create_skeleton_games(
                store_name, new_ids,
                new_games_callback=skeleton_cb,
                progress_callback=lambda cur, tot: (
                    self.phase_progress.emit(store_name, cur, tot)
                ),
                cancel_check=cancel_or_skip,
            )

        # Invalidate cache so tag/install/playtime/skeleton changes are
        # visible in the final _load_games() call
        self._game_service.invalidate_cache()

        metadata_ids = new_ids + refetch_ids
        if metadata_ids:
            # Don't queue metadata jobs if cancelled during skeleton creation
            if cancel_or_skip():
                self.phase_finished.emit(store_name)
                return

            # Create metadata fetch jobs for new + refetch games (batch of 50)
            batch_size = 50
            batches = [
                metadata_ids[i : i + batch_size]
                for i in range(0, len(metadata_ids), batch_size)
            ]
            metadata_jobs = []
            for idx, batch in enumerate(batches):
                metadata_jobs.append(
                    SyncJob(
                        phase=SyncPhase.STORE,
                        plugin_name=store_name,
                        task_type="fetch_metadata",
                        game_ids=batch,
                        store_name=store_name,
                        batch_index=idx + 1,
                        batch_total=len(batches),
                    )
                )
            self._queue.add_jobs(metadata_jobs)

            # Set up cumulative progress tracking for this store
            self._store_metadata_progress[store_name] = {
                "done": 0, "total": len(metadata_ids),
            }
            # Emit phase_started for metadata fetching (per-game total)
            self.phase_started.emit(
                store_name, "fetching metadata", len(metadata_ids),
            )

            parts = []
            if new_ids:
                parts.append(f"{len(new_ids)} new")
            if refetch_ids:
                parts.append(f"{len(refetch_ids)} refetch")
            logger.info(
                f"{store_name}: {' + '.join(parts)} games, "
                f"{len(batches)} metadata jobs queued"
            )
        else:
            logger.info(f"{store_name}: no new games")
            self.phase_finished.emit(store_name)

    async def _exec_store_metadata(self, job) -> None:
        """Execute a store metadata fetch job for a batch of game IDs."""
        store_name = job.store_name
        progress_tracker = self._store_metadata_progress.get(store_name)

        def cancel_or_skip():
            if self._queue.is_cancelled:
                return True
            skip_for = self._queue.skip_requested_for
            return skip_for == store_name if skip_for else False

        def progress_cb(game_name, current, total):
            if progress_tracker:
                progress_tracker["done"] += 1
                done = progress_tracker["done"]
                total_games = progress_tracker["total"]
                self.phase_progress.emit(store_name, done, total_games)

        def new_games_cb(batch):
            self.games_batch_ready.emit(batch)

        def countdown_cb(wait_seconds):
            self.rate_limit_countdown.emit(store_name, wait_seconds)

        budget_threshold = self._get_budget_threshold(store_name)

        stats = await self._game_service.execute_store_metadata_batch(
            store_name,
            job.game_ids,
            progress_callback=progress_cb,
            cancel_check=cancel_or_skip,
            status_callback=self._on_status,
            new_games_callback=new_games_cb,
            countdown_callback=countdown_cb,
            budget_threshold=budget_threshold,
        )

        # Budget pause: wait cooldown, then re-queue remaining.
        # Enrichment is deferred to the post-sync METADATA phase to avoid
        # proxy rate-limit contention between IGDB and store fetches.
        if stats.get("_budget_paused") and not cancel_or_skip():
            remaining_ids = stats.get("_remaining_app_ids", [])
            await self._wait_budget_cooldown(
                store_name, cancel_or_skip, countdown_cb, progress_tracker,
            )
            if remaining_ids and not cancel_or_skip():
                self._requeue_remaining(
                    store_name, remaining_ids, job, progress_tracker,
                )

        # Periodic UI refresh so covers populate incrementally.
        if progress_tracker:
            done = progress_tracker["done"]
            last_refresh = progress_tracker.get("_last_refresh", 0)
            if done - last_refresh >= 20:
                progress_tracker["_last_refresh"] = done
                self.cache_refresh_requested.emit()

        # Merge stats
        store_stats = self._stats.setdefault(store_name, {})
        store_stats["games_added"] = store_stats.get("games_added", 0) + stats.get("games_added", 0)
        store_stats["games_updated"] = store_stats.get("games_updated", 0) + stats.get("games_updated", 0)

        # Emit phase_finished when last batch completes or skip/cancel aborted
        # (but not if budget_paused — remaining batches were re-queued)
        if stats.get("_budget_paused"):
            pass  # Re-queued batches will emit phase_finished
        elif job.batch_index == job.batch_total or cancel_or_skip():
            self.phase_finished.emit(store_name)
            # Clean up tracker
            self._store_metadata_progress.pop(store_name, None)

    async def _exec_build_metadata_jobs(self, job) -> None:
        """Sentinel job: build metadata enrichment jobs dynamically.

        Called after all store phases complete. Queries the DB for
        unenriched games and creates per-plugin batch jobs.
        Also runs metadata plugin tag sync (e.g. Heroic tag import).
        """
        self.phase_started.emit("_system", "building metadata jobs...", 0)

        # Run metadata plugin tag sync (e.g. Heroic categories/favourites)
        self._apply_metadata_tag_sync()

        # Invalidate cache so metadata tag changes are visible
        self._game_service.invalidate_cache()

        full_resync = job.extra.get("full_resync", False)
        metadata_jobs = self._game_service.build_metadata_jobs(
            full_resync=full_resync,
        )
        if metadata_jobs:
            self._queue.add_jobs(metadata_jobs)
            logger.info(f"Built {len(metadata_jobs)} metadata enrichment jobs")
        else:
            logger.info("No metadata enrichment jobs needed")

        self.phase_finished.emit("_system")

    async def _exec_metadata_plugin(self, job) -> None:
        """Execute a consolidated metadata plugin run (all stores, all batches).

        The plugin processes all stores sequentially, batching internally.
        Progress is reported per-game via phase_progress.
        """
        plugin_name = job.plugin_name
        total_games = sum(
            len(gl)
            for gl in job.extra.get("games_by_store", {}).values()
        )

        self.phase_started.emit(plugin_name, "enriching games", total_games)

        def batch_progress_cb(completed, total):
            self.phase_progress.emit(plugin_name, completed, total)

        def cancel_or_skip():
            if self._queue.is_cancelled:
                return True
            skip_for = self._queue.skip_requested_for
            return skip_for == plugin_name if skip_for else False

        def status_cb(message):
            self.rate_limit.emit(message)

        try:
            stats = await self._game_service.execute_metadata_plugin_run(
                plugin_name,
                job.extra.get("games_by_store", {}),
                batch_progress_callback=batch_progress_cb,
                cancel_check=cancel_or_skip,
                status_callback=status_cb,
            )

            # Track enrichment stats
            meta_stats = self._stats.setdefault("_metadata", {})
            plugin_stats = meta_stats.setdefault(plugin_name, {})
            plugin_stats["enriched"] = (
                plugin_stats.get("enriched", 0) + stats.get("games_enriched", 0)
            )
        finally:
            self.phase_finished.emit(plugin_name)

    async def _exec_single_enrichment(self, job) -> None:
        """Execute priority enrichment for a single game (all plugins)."""
        await self._game_service.execute_single_enrichment(
            job.game_ids[0] if job.game_ids else "",
            job.store_name,
            cancel_check=lambda: self._queue.is_cancelled,
        )

    async def _exec_tag_sync(self, job) -> None:
        """Execute tag sync for a single metadata plugin (e.g. Lutris, Heroic).

        Runs the plugin's get_tag_sync_data() and applies via tag_service,
        reporting progress via phase signals.
        """
        plugin_name = job.plugin_name

        self.phase_started.emit(plugin_name, "syncing tags...", 0)

        try:
            self._apply_metadata_tag_sync_single(plugin_name)

            # Invalidate cache so changes are visible
            self._game_service.invalidate_cache()
        finally:
            self.phase_finished.emit(plugin_name)

    def _apply_metadata_tag_sync_single(self, plugin_name: str) -> None:
        """Run tag sync for a single metadata plugin.

        Args:
            plugin_name: Name of the plugin to sync
        """
        config = self._game_service._config
        overrides = config.get("tags.plugin_overrides", {}) if config else {}

        metadata_plugins = self._game_service.plugin_manager.get_metadata_plugins()
        plugin = metadata_plugins.get(plugin_name)
        if not plugin or not hasattr(plugin, "get_tag_sync_data"):
            return

        plugin_override = overrides.get(plugin_name, {})

        sync_mode = plugin_override.get("sync_mode", "default")
        if sync_mode == "default":
            sync_mode = config.get("tags.default_sync_mode", "add_only") if config else "add_only"

        try:
            kwargs = {}
            if "import_favourites" in plugin_override:
                kwargs["import_favourites"] = plugin_override["import_favourites"]

            tag_data = plugin.get_tag_sync_data(**kwargs)
            if not tag_data:
                return
            effective_mode = tag_data.get("mode", sync_mode)
            stats = self._game_service._apply_metadata_tag_sync(
                tag_data.get("source", plugin_name),
                effective_mode,
                tag_data.get("entries", []),
                removals=tag_data.get("removals"),
            )
            if stats:
                meta_stats = self._stats.setdefault("_metadata", {})
                meta_stats[plugin_name] = {"tag_sync": stats}
        except Exception as e:
            logger.warning(
                f"Tag sync failed for metadata plugin {plugin_name}: {e}"
            )

    def _apply_store_tag_sync(self, store_name: str) -> None:
        """Apply tag/favorite/hidden sync if the store plugin supports it.

        Reads sync mode from centralized tags config (tags.plugin_overrides),
        falling back to tags.default_sync_mode.
        """
        try:
            config = self._game_service._config
            overrides = config.get("tags.plugin_overrides", {}) if config else {}
            plugin_override = overrides.get(store_name, {})

            # Check if tag sync is enabled for this plugin
            if not plugin_override.get("enabled", True):
                return

            # Determine sync mode: plugin override → global default
            sync_mode = plugin_override.get("sync_mode", "default")
            if sync_mode == "default":
                sync_mode = config.get("tags.default_sync_mode", "add_only") if config else "add_only"
            if sync_mode == "none":
                return

            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if plugin and hasattr(plugin, "get_tag_sync_data"):
                tag_data = plugin.get_tag_sync_data()
                if tag_data:
                    # Override mode from centralized config
                    tag_data["mode"] = sync_mode
                    stats = self._game_service._apply_tag_sync_data(
                        store_name, tag_data
                    )
                    if stats:
                        store_stats = self._stats.setdefault(store_name, {})
                        store_stats["tag_sync"] = stats
        except Exception as e:
            logger.warning(f"Tag sync failed for {store_name}: {e}")

    def _apply_family_shared_tag_sync(self, store_name: str) -> None:
        """Sync the 'Family Shared' system tag after store sync."""
        try:
            from ..core.database import StoreGame, Game as DbGame
            session = self._game_service.database.get_session()
            try:
                # Find all games with at least one family_shared StoreGame
                family_game_ids = set()
                all_game_ids = set()

                # Get all games for this store
                store_games = session.query(StoreGame).filter_by(
                    store_name=store_name
                ).all()

                for sg in store_games:
                    all_game_ids.add(sg.game_id)
                    if sg.family_shared:
                        family_game_ids.add(sg.game_id)

                if family_game_ids or all_game_ids:
                    self._game_service._tags.sync_family_shared_tags(
                        family_game_ids, all_game_ids
                    )
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"Family shared tag sync failed for {store_name}: {e}")

    def _apply_store_install_sync(self, store_name: str) -> None:
        """Apply installation status sync if the store plugin supports it."""
        try:
            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if plugin and hasattr(plugin, "get_install_sync_data"):
                install_data = plugin.get_install_sync_data()
                if install_data:
                    stats = self._game_service._apply_install_sync_data(
                        store_name, install_data
                    )
                    if stats:
                        store_stats = self._stats.setdefault(store_name, {})
                        store_stats["install_sync"] = stats
        except Exception as e:
            logger.warning(f"Install sync failed for {store_name}: {e}")

    def _apply_store_playtime_sync(self, store_name: str) -> None:
        """Apply playtime sync if the store plugin supports it."""
        try:
            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if plugin and hasattr(plugin, "get_playtime_sync_data"):
                playtime_data = plugin.get_playtime_sync_data()
                if playtime_data:
                    stats = self._game_service._apply_playtime_sync_data(
                        store_name, playtime_data
                    )
                    if stats:
                        store_stats = self._stats.setdefault(store_name, {})
                        store_stats["playtime_sync"] = stats
        except Exception as e:
            logger.warning(f"Playtime sync failed for {store_name}: {e}")

    def _apply_family_license_sync(self, store_name: str) -> None:
        """Sync per-game family license counts into metadata_json.

        Only applies for Steam — reads license_counts from the store plugin
        and writes _family_license_count into each StoreGame's metadata_json.
        """
        if store_name != "steam":
            return
        try:
            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if plugin and hasattr(plugin, "get_family_license_data"):
                counts = plugin.get_family_license_data()
                if counts:
                    self._game_service._apply_family_license_data(store_name, counts)
        except Exception as e:
            logger.warning(f"Family license sync failed for {store_name}: {e}")

    def _collect_family_sharing_warning(self, store_name: str) -> None:
        """Check if the Steam plugin encountered family sharing issues."""
        if store_name != "steam":
            return
        try:
            plugin = self._game_service.plugin_manager.get_plugin(store_name)
            if not plugin:
                return
            warning = getattr(plugin, "_family_sharing_warning", None)
            if not warning:
                return
            from luducat.core.i18n import _
            title = _("Steam Family Sharing")
            if warning == "vdf_fallback":
                msg = _(
                    "The Steam family sharing API was not available. "
                    "Family data was detected from local files instead.\n\n"
                    "Retry sync later for complete data."
                )
            else:
                msg = _(
                    "Steam family data could not be retrieved. "
                    "Retry sync later.\n\n"
                    "If this persists, please open an issue on GitHub."
                )
            self._sync_warnings.append((title, msg))
            plugin._family_sharing_warning = None
        except Exception as e:
            logger.debug(f"Family sharing warning check failed: {e}")

    def _apply_metadata_tag_sync(self) -> None:
        """Apply tag sync from metadata plugins that support it (e.g. Heroic).

        Reads sync mode from centralized tags config (tags.plugin_overrides),
        falling back to tags.default_sync_mode.
        """
        config = self._game_service._config
        overrides = config.get("tags.plugin_overrides", {}) if config else {}

        metadata_plugins = self._game_service.plugin_manager.get_metadata_plugins()
        for plugin_name, plugin in metadata_plugins.items():
            if not hasattr(plugin, "get_tag_sync_data"):
                continue

            # Read centralized config for this plugin
            plugin_override = overrides.get(plugin_name, {})

            # Check if tag sync is enabled for this plugin
            if not plugin_override.get("enabled", True):
                continue

            # Determine sync mode: plugin override → global default
            sync_mode = plugin_override.get("sync_mode", "default")
            if sync_mode == "default":
                sync_mode = config.get("tags.default_sync_mode", "add_only") if config else "add_only"
            if sync_mode == "none":
                continue

            try:
                # Pass plugin-specific overrides as kwargs
                kwargs = {}
                if "import_favourites" in plugin_override:
                    kwargs["import_favourites"] = plugin_override["import_favourites"]

                tag_data = plugin.get_tag_sync_data(**kwargs)
                if not tag_data:
                    continue
                # Use plugin's own mode if delta, otherwise use configured sync_mode
                effective_mode = tag_data.get("mode", sync_mode)
                stats = self._game_service._apply_metadata_tag_sync(
                    tag_data.get("source", plugin_name),
                    effective_mode,
                    tag_data.get("entries", []),
                    removals=tag_data.get("removals"),
                )
                if stats:
                    meta_stats = self._stats.setdefault("_metadata", {})
                    meta_stats[plugin_name] = {"tag_sync": stats}
            except Exception as e:
                logger.warning(
                    f"Tag sync failed for metadata plugin {plugin_name}: {e}"
                )

    def _get_budget_threshold(self, store_name: str) -> int:
        """Return proactive budget threshold for a store, 0 to disable."""
        if store_name == "steam":
            from ..plugins.steam.steamscraper.config import (
                PROACTIVE_BUDGET_THRESHOLD,
            )
            return PROACTIVE_BUDGET_THRESHOLD
        return 0

    async def _wait_budget_cooldown(
        self, store_name, cancel_or_skip, countdown_cb, progress_tracker,
    ) -> None:
        """Wait for API budget cooldown before resuming store metadata fetch.

        Enrichment is deferred to the post-sync METADATA phase to avoid
        proxy rate-limit contention between IGDB and store fetches.
        """
        logger.info(f"{store_name}: budget paused, waiting cooldown")

        plugin = self._game_service.plugin_manager.get_plugin(store_name)
        if plugin and hasattr(plugin, "get_api_budget_status"):
            budget = plugin.get_api_budget_status()
            if budget and budget.get("in_cooldown"):
                remaining = budget.get("cooldown_remaining", 0)
                if remaining > 0:
                    if progress_tracker:
                        self.phase_started.emit(
                            store_name,
                            "fetching metadata",
                            progress_tracker["total"],
                        )
                        self.phase_progress.emit(
                            store_name,
                            progress_tracker["done"],
                            progress_tracker["total"],
                        )
                    countdown_cb(remaining)
                    while remaining > 0 and not cancel_or_skip():
                        chunk = min(10, remaining)
                        await asyncio.sleep(chunk)
                        remaining -= chunk
                    countdown_cb(0)

    def _requeue_remaining(
        self, store_name, remaining_ids, job, progress_tracker,
    ) -> None:
        """Re-queue remaining metadata fetch jobs after budget cooldown."""
        from ..core.sync_queue import SyncJob, SyncPhase

        batch_size = 50
        batches = [
            remaining_ids[i : i + batch_size]
            for i in range(0, len(remaining_ids), batch_size)
        ]
        new_jobs = []
        for idx, batch in enumerate(batches):
            new_jobs.append(
                SyncJob(
                    phase=SyncPhase.STORE,
                    plugin_name=store_name,
                    task_type="fetch_metadata",
                    game_ids=batch,
                    store_name=store_name,
                    batch_index=idx + 1,
                    batch_total=len(batches),
                )
            )
        if new_jobs:
            self._queue.add_jobs(new_jobs)
            # Restore phase display for continued fetching
            if progress_tracker:
                self.phase_started.emit(
                    store_name,
                    "fetching metadata",
                    progress_tracker["total"],
                )
            logger.info(
                f"{store_name}: re-queued {len(remaining_ids)} games "
                f"in {len(batches)} batches after budget cooldown"
            )

    def _on_status(self, message: str) -> None:
        """Status callback for store fetch operations."""
        self.rate_limit.emit(message)

    def cancel(self) -> None:
        """Request cancellation via the queue."""
        logger.info("Sync cancellation requested")
        self._queue.cancel()

    def skip_current_plugin(self) -> None:
        """Skip remaining jobs for the current plugin."""
        current = self._queue.current_job
        if current:
            logger.info(f"Skipping plugin: {current.plugin_name}")
            self._queue.skip_plugin(current.plugin_name)


