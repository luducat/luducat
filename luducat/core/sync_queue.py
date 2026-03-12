# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# sync_queue.py

"""Sequential sync job queue for luducat

Replaces the parallel asyncio.gather() approach with a sequential queue.
One worker thread pulls jobs one at a time. Three phases run in order:
Store -> Metadata -> Assets.

This eliminates cross-plugin cancel races, makes progress reporting trivial,
and stops metadata plugins from "running in circles" by ensuring each plugin
runs once on only the games it hasn't already processed.
"""

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SyncPhase(Enum):
    """Sync phases in execution order."""

    STORE = "store"  # Fetch game lists + basic metadata from stores
    METADATA = "metadata"  # Enrich with IGDB/PCGW/SteamGridDB/ProtonDB
    ASSETS = "assets"  # Download missing images (deferred)


class JobPriority(Enum):
    """Job priority levels."""

    NORMAL = 0
    HIGH = 1  # User-requested enrichment (game click)


@dataclass
class SyncJob:
    """A single unit of sync work.

    Attributes:
        phase: Which sync phase this job belongs to
        plugin_name: Plugin responsible (e.g., "steam", "igdb", "_system")
        task_type: What to do:
            - "fetch_games": Get game ID list from a store
            - "fetch_metadata": Get store metadata for a batch of game IDs
            - "enrich_single": Priority enrichment for one game (all plugins)
            - "build_metadata_jobs": Sentinel — construct metadata jobs dynamically
        game_ids: Batch of store_app_ids to process
        store_name: Which store's games (for metadata jobs)
        batch_index: e.g., 3 (of batch_total)
        batch_total: Total batches for this plugin
        priority: NORMAL or HIGH
        extra: Arbitrary data (e.g., PluginGame objects, cross_store_ids)
    """

    phase: SyncPhase
    plugin_name: str
    task_type: str
    game_ids: List[str] = field(default_factory=list)
    store_name: str = ""
    batch_index: int = 0
    batch_total: int = 0
    priority: JobPriority = JobPriority.NORMAL
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """Human-readable description for progress bar."""
        if self.task_type == "fetch_games":
            return f"Fetching {self.plugin_name} game list"
        elif self.task_type == "fetch_metadata":
            count = len(self.game_ids)
            return f"{self.plugin_name}: metadata for {count} games"
        elif self.task_type == "enrich_plugin":
            total = sum(
                len(gl)
                for gl in self.extra.get("games_by_store", {}).values()
            )
            return f"{self.plugin_name} ({total} games)"
        elif self.task_type == "enrich_single":
            return f"{self.plugin_name}: priority enrichment"
        elif self.task_type == "tag_sync":
            return f"{self.plugin_name}: tag sync"
        elif self.task_type == "build_metadata_jobs":
            return "Building metadata jobs"
        return f"{self.plugin_name}: {self.task_type}"


class SyncJobQueue:
    """Thread-safe sequential job queue with priority insertion.

    The SyncWorker pulls jobs via next_job() and processes them one at a time.
    The UI thread can insert priority jobs or pause/cancel from the main thread.

    Usage:
        queue = SyncJobQueue()
        queue.add_jobs(store_jobs)

        while True:
            job = queue.next_job()
            if job is None:
                break  # Empty, paused, or cancelled
            # ... execute job ...
            queue.job_completed()
    """

    def __init__(self):
        self._jobs: List[SyncJob] = []
        self._lock = threading.Lock()
        self._current_job: Optional[SyncJob] = None
        self._total_jobs: int = 0
        self._completed_jobs: int = 0
        self._cancelled = False
        self._paused = False
        self._skip_requested: Optional[str] = None  # plugin name to skip

    def add_jobs(self, jobs: List[SyncJob]) -> None:
        """Add jobs respecting phase ordering.

        STORE phase jobs are inserted before any METADATA phase jobs
        already in the queue, ensuring all store work completes before
        enrichment begins.  Non-STORE jobs are appended at the end.
        """
        if not jobs:
            return
        with self._lock:
            store_jobs = [j for j in jobs if j.phase == SyncPhase.STORE]
            other_jobs = [j for j in jobs if j.phase != SyncPhase.STORE]

            if store_jobs:
                # Find first METADATA/ASSETS phase job in the queue
                insert_idx = len(self._jobs)
                for i, existing in enumerate(self._jobs):
                    if existing.phase in (SyncPhase.METADATA, SyncPhase.ASSETS):
                        insert_idx = i
                        break
                # Insert store jobs before metadata jobs
                for offset, j in enumerate(store_jobs):
                    self._jobs.insert(insert_idx + offset, j)

            if other_jobs:
                self._jobs.extend(other_jobs)

            self._total_jobs += len(jobs)
            logger.debug(f"Added {len(jobs)} jobs, total now {self._total_jobs}")

    def insert_priority_job(self, job: SyncJob) -> None:
        """Insert a high-priority job at the front of the queue.

        Used when the user clicks a game during sync and wants
        its metadata enriched immediately.
        """
        job.priority = JobPriority.HIGH
        with self._lock:
            self._jobs.insert(0, job)
            self._total_jobs += 1
            logger.info(f"Priority job inserted: {job.description}")

    def next_job(self) -> Optional[SyncJob]:
        """Get the next job to execute.

        Returns None if the queue is empty, paused, or cancelled.
        The caller should call job_completed() after executing the job.
        """
        with self._lock:
            if self._cancelled:
                return None
            if self._paused:
                return None
            if not self._jobs:
                return None
            self._current_job = self._jobs.pop(0)
            return self._current_job

    def job_completed(self) -> None:
        """Mark the current job as completed."""
        with self._lock:
            self._completed_jobs += 1
            self._current_job = None

    def cancel(self) -> None:
        """Cancel the queue. next_job() will return None."""
        with self._lock:
            self._cancelled = True
            logger.info("Sync queue cancelled")

    def pause(self) -> None:
        """Pause the queue. next_job() will return None until resumed."""
        with self._lock:
            self._paused = True
            logger.info("Sync queue paused")

    def resume(self) -> None:
        """Resume a paused queue."""
        with self._lock:
            self._paused = False
            logger.info("Sync queue resumed")

    def skip_plugin(self, plugin_name: str) -> None:
        """Skip all remaining jobs for the given plugin.

        Sets a skip flag so the worker can break out of the current job,
        and removes all queued jobs for the plugin.

        Args:
            plugin_name: Plugin to skip (e.g. "steam", "igdb")
        """
        with self._lock:
            self._skip_requested = plugin_name
            # Remove queued jobs for this plugin
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.plugin_name != plugin_name]
            removed = before - len(self._jobs)
            self._total_jobs -= removed
            logger.info(
                f"Skipped {removed} queued jobs for plugin '{plugin_name}'"
            )

    @property
    def skip_requested_for(self) -> Optional[str]:
        """Plugin name that was requested to be skipped, or None."""
        with self._lock:
            return self._skip_requested

    def clear_skip(self) -> None:
        """Clear the skip request after the worker has acted on it."""
        with self._lock:
            self._skip_requested = None

    @property
    def total_jobs(self) -> int:
        """Total number of jobs added to the queue."""
        with self._lock:
            return self._total_jobs

    @property
    def completed_jobs(self) -> int:
        """Number of jobs completed so far."""
        with self._lock:
            return self._completed_jobs

    @property
    def progress_percent(self) -> float:
        """Completion percentage (0.0 to 100.0)."""
        with self._lock:
            if self._total_jobs == 0:
                return 0.0
            return (self._completed_jobs / self._total_jobs) * 100.0

    @property
    def current_job(self) -> Optional[SyncJob]:
        """The job currently being executed (or None)."""
        with self._lock:
            return self._current_job

    @property
    def remaining_jobs(self) -> List[SyncJob]:
        """Snapshot of remaining jobs (for the dropdown display)."""
        with self._lock:
            return list(self._jobs)

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return len(self._jobs) == 0 and self._current_job is None
