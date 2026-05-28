# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Download manager — queue orchestration for the Luducat Downloader.

Singleton that manages download workers, persistence, game-level
grouping, bandwidth limiting, and archivist handoff. Layer 2 of the
downloader architecture.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from sqlalchemy import text

from luducat.core.archivist.types import ArchiveRequest, DownloadTarget
from luducat.core.json_compat import json

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


# ── Bandwidth limiting ──────────────────────────────────────────────


class _TokenBucket:
    """Thread-safe token bucket for bandwidth limiting.

    rate_bytes_per_sec=0 means unlimited.
    """

    def __init__(self, rate_bytes_per_sec: float) -> None:
        self._rate = rate_bytes_per_sec
        self._tokens = rate_bytes_per_sec if rate_bytes_per_sec > 0 else 0
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n_bytes: int) -> float:
        """Consume tokens. Returns seconds to sleep (0.0 if within budget)."""
        if self._rate <= 0:
            return 0.0

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= n_bytes:
                self._tokens -= n_bytes
                return 0.0

            deficit = n_bytes - self._tokens
            self._tokens = 0
            return deficit / self._rate

    def set_rate(self, rate_bytes_per_sec: float) -> None:
        """Update rate at runtime (e.g. settings change)."""
        with self._lock:
            self._rate = rate_bytes_per_sec
            if rate_bytes_per_sec <= 0:
                self._tokens = 0
            self._last_refill = time.monotonic()


# ── Download status constants ───────────────────────────────────────

class _Status:
    RESOLVING = "RESOLVING"
    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ── Datetime helpers ────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat()


# ── Download Worker ─────────────────────────────────────────────────


class _DownloadWorker(threading.Thread):
    """Worker thread for a single download.

    Calls download_file() from the engine, bridges progress to
    the manager via callback, checks cancel_event for pause/cancel.
    """

    def __init__(
        self,
        download_id: str,
        url: str,
        dest_path: Path,
        session,
        cancel_event: threading.Event,
        throttle_fn,
        num_connections: int,
        chunk_threshold: int,
        expected_checksum: Optional[str],
        extra_headers: Optional[dict],
        cookies: Optional[dict],
        on_progress=None,
        on_finished=None,
        chunk_state: Optional[dict] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.download_id = download_id
        self._url = url
        self._dest_path = dest_path
        self._session = session
        self._cancel = cancel_event
        self._throttle_fn = throttle_fn
        self._num_connections = num_connections
        self._chunk_threshold = chunk_threshold
        self._expected_checksum = expected_checksum
        self._extra_headers = extra_headers
        self._cookies = cookies
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._chunk_state = chunk_state
        self._last_time = time.monotonic()
        self._last_bytes = 0

    def run(self) -> None:
        from luducat.core.download_engine import download_file

        def progress_callback(bytes_written: int, total: int) -> None:
            # Bandwidth throttle
            chunk_size = bytes_written - self._last_bytes
            if chunk_size > 0:
                delay = self._throttle_fn(chunk_size)
                if delay > 0:
                    time.sleep(delay)

            # Speed calculation
            now = time.monotonic()
            elapsed = now - self._last_time
            speed = chunk_size / elapsed if elapsed > 0 else 0.0
            self._last_time = now
            self._last_bytes = bytes_written

            if self._on_progress:
                self._on_progress(self.download_id, bytes_written, total, speed)

        # Apply cookies to session if provided
        if self._cookies and self._session:
            for k, v in self._cookies.items():
                self._session.cookies.set(k, v)

        result = download_file(
            url=self._url,
            dest_path=self._dest_path,
            session=self._session,
            cancel_event=self._cancel,
            progress_callback=progress_callback,
            num_connections=self._num_connections,
            chunk_threshold=self._chunk_threshold,
            expected_checksum=self._expected_checksum,
            extra_headers=self._extra_headers,
            chunk_state=self._chunk_state,
        )

        if self._on_finished:
            self._on_finished(self.download_id, result)


# ── Download Manager ────────────────────────────────────────────────


class DownloadManager:
    """Queue orchestration for the Luducat Downloader.

    Manages download workers, persistence, game-level grouping,
    bandwidth limiting, and archivist handoff.
    """

    def __init__(self, engine: "Engine", config) -> None:
        self._engine = engine
        self._config = config
        self._max_concurrent = config.get("downloads.max_concurrent", 3)
        self._bandwidth_mbps = config.get("downloads.bandwidth_limit_mbps", 0)
        self._bucket = _TokenBucket(
            self._bandwidth_mbps * 1_000_000 / 8 if self._bandwidth_mbps else 0
        )
        self._workers: dict[str, Any] = {}  # download_id -> worker
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

        self._recover_state()

    def _recover_state(self) -> None:
        """On startup: DOWNLOADING -> PAUSED for all in-flight rows."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :paused, updated_at = :now "
                "WHERE status = :downloading"
            ), {
                "paused": _Status.PAUSED,
                "downloading": _Status.DOWNLOADING,
                "now": _now_iso(),
            })
            conn.execute(text(
                "UPDATE download_groups SET status = :paused, updated_at = :now "
                "WHERE status = :downloading"
            ), {
                "paused": _Status.PAUSED,
                "downloading": _Status.DOWNLOADING,
                "now": _now_iso(),
            })
            conn.commit()

    def submit(self, target: DownloadTarget) -> str:
        """Submit a game download. Returns group_id.

        Creates one download_group row and one download row per file.
        """
        group_id = uuid.uuid4().hex
        now = _now_iso()
        total_bytes = sum(f.expected_size or 0 for f in target.files)

        with self._engine.connect() as conn:
            max_prio = conn.execute(text(
                "SELECT COALESCE(MAX(priority), -1) FROM download_groups"
            )).scalar()
            next_prio = max_prio + 1

            conn.execute(text("""
                INSERT INTO download_groups (
                    id, game_title, store_name, store_app_id, icon_path,
                    status, total_bytes, downloaded_bytes,
                    file_count, files_completed, priority,
                    created_at, updated_at
                ) VALUES (
                    :id, :title, :store, :app_id, :icon,
                    :status, :total, 0,
                    :count, 0, :priority,
                    :now, :now
                )
            """), {
                "id": group_id,
                "title": target.game_title,
                "store": target.store_name,
                "app_id": target.store_app_id,
                "icon": None,
                "status": _Status.PENDING,
                "total": total_bytes if total_bytes > 0 else None,
                "count": len(target.files),
                "priority": next_prio,
                "now": now,
            })

            for priority, req in enumerate(target.files):
                dl_id = uuid.uuid4().hex
                checksum_str = ""
                if req.checksum_sha256:
                    checksum_str = f"sha256:{req.checksum_sha256}"

                conn.execute(text("""
                    INSERT INTO downloads (
                        id, group_id, url, destination_path, temp_path,
                        filename, status, bytes_downloaded, bytes_total,
                        checksum_expected, priority, retry_count,
                        headers_json, cookies_json, metadata_json,
                        resume_enabled, created_at, updated_at
                    ) VALUES (
                        :id, :gid, :url, :dest, :tmp,
                        :fname, :status, 0, :total,
                        :checksum, :priority, 0,
                        :headers, :cookies, :metadata,
                        1, :now, :now
                    )
                """), {
                    "id": dl_id,
                    "gid": group_id,
                    "url": req.url,
                    "dest": "",
                    "tmp": "",
                    "fname": req.filename,
                    "status": _Status.PENDING,
                    "total": req.expected_size,
                    "checksum": checksum_str or None,
                    "priority": priority,
                    "headers": json.dumps(req.headers) if req.headers else None,
                    "cookies": json.dumps(req.cookies) if req.cookies else None,
                    "metadata": json.dumps(req.metadata) if req.metadata else None,
                    "now": now,
                })

            conn.commit()

        logger.info(
            "Submitted download group %s: %s (%d files)",
            group_id, target.game_title, len(target.files),
        )
        self._schedule_next()
        return group_id

    def create_resolving_group(self, url: str, store_name: str) -> str:
        """Create a placeholder group in RESOLVING state.

        Returns group_id. The group has no download rows yet — those are
        added by finalize_resolving_group() after the resolve worker
        completes.
        """
        group_id = uuid.uuid4().hex
        now = _now_iso()

        with self._engine.connect() as conn:
            max_prio = conn.execute(text(
                "SELECT COALESCE(MAX(priority), -1) FROM download_groups"
            )).scalar()

            conn.execute(text("""
                INSERT INTO download_groups (
                    id, game_title, store_name, status,
                    total_bytes, file_count, files_completed, priority,
                    created_at, updated_at
                ) VALUES (
                    :id, :title, :store, :status,
                    NULL, 0, 0, :priority,
                    :now, :now
                )
            """), {
                "id": group_id,
                "title": url,
                "store": store_name,
                "status": _Status.RESOLVING,
                "priority": max_prio + 1,
                "now": now,
            })
            conn.commit()

        logger.debug("Created resolving group %s for %s (%s)",
                      group_id, url[:60], store_name)
        return group_id

    def finalize_resolving_group(
        self, group_id: str, target: "DownloadTarget",
    ) -> None:
        """Transition a RESOLVING group to PENDING with real download data."""
        now = _now_iso()
        total_bytes = sum(f.expected_size or 0 for f in target.files)

        with self._engine.connect() as conn:
            conn.execute(text("""
                UPDATE download_groups SET
                    game_title = :title,
                    store_app_id = :app_id,
                    status = :status,
                    total_bytes = :total,
                    file_count = :count,
                    updated_at = :now
                WHERE id = :id
            """), {
                "title": target.game_title,
                "app_id": target.store_app_id if hasattr(target, 'store_app_id') else None,
                "status": _Status.PENDING,
                "total": total_bytes if total_bytes > 0 else None,
                "count": len(target.files),
                "now": now,
                "id": group_id,
            })

            for priority, req in enumerate(target.files):
                dl_id = uuid.uuid4().hex
                checksum_str = ""
                if req.checksum_sha256:
                    checksum_str = f"sha256:{req.checksum_sha256}"

                conn.execute(text("""
                    INSERT INTO downloads (
                        id, group_id, url, destination_path, temp_path,
                        filename, status, bytes_downloaded, bytes_total,
                        checksum_expected, priority, retry_count,
                        headers_json, cookies_json, metadata_json,
                        resume_enabled, created_at, updated_at
                    ) VALUES (
                        :id, :gid, :url, :dest, :tmp,
                        :fname, :status, 0, :total,
                        :checksum, :priority, 0,
                        :headers, :cookies, :metadata,
                        1, :now, :now
                    )
                """), {
                    "id": dl_id,
                    "gid": group_id,
                    "url": req.url,
                    "dest": "",
                    "tmp": "",
                    "fname": req.filename,
                    "status": _Status.PENDING,
                    "total": req.expected_size,
                    "checksum": checksum_str or None,
                    "priority": priority,
                    "headers": json.dumps(req.headers) if req.headers else None,
                    "cookies": json.dumps(req.cookies) if req.cookies else None,
                    "metadata": json.dumps(req.metadata) if req.metadata else None,
                    "now": now,
                })

            conn.commit()

        logger.info("Finalized resolving group %s: %s (%d files)",
                     group_id, target.game_title, len(target.files))
        self._schedule_next()

    def fail_resolving_group(self, group_id: str, error: str) -> None:
        """Transition a RESOLVING group to FAILED with an error message."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE download_groups SET status = :status, last_error = :err, "
                "updated_at = :now WHERE id = :id"
            ), {
                "status": _Status.FAILED,
                "err": error,
                "id": group_id,
                "now": _now_iso(),
            })
            conn.commit()
        logger.debug("Resolving group %s failed: %s", group_id, error)

    def get_queue(self) -> list[dict]:
        """Group-level summaries for UI display."""
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT g.*, COALESCE(agg.live_bytes, 0) AS downloaded_bytes "
                "FROM download_groups g "
                "LEFT JOIN ("
                "  SELECT group_id, SUM(bytes_downloaded) AS live_bytes "
                "  FROM downloads GROUP BY group_id"
                ") agg ON agg.group_id = g.id "
                "ORDER BY g.priority ASC, g.created_at DESC"
            )).mappings().all()
        return [dict(r) for r in rows]

    def get_group_details(self, group_id: str) -> dict:
        """Group + child download details for expanded view."""
        with self._engine.connect() as conn:
            group = conn.execute(
                text("SELECT * FROM download_groups WHERE id = :id"),
                {"id": group_id},
            ).mappings().first()
            if group is None:
                return {}

            downloads = conn.execute(
                text(
                    "SELECT * FROM downloads WHERE group_id = :gid "
                    "ORDER BY priority"
                ),
                {"gid": group_id},
            ).mappings().all()

        return {
            "group": dict(group),
            "downloads": [dict(d) for d in downloads],
        }

    def _update_download_status(self, download_id: str, status: str) -> None:
        """Update a single download row's status."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :status, updated_at = :now WHERE id = :id"
            ), {"status": status, "id": download_id, "now": _now_iso()})
            conn.commit()

    def _update_group_status(self, group_id: str, status: str) -> None:
        """Update a group row's status."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE download_groups SET status = :status, updated_at = :now WHERE id = :id"
            ), {"status": status, "id": group_id, "now": _now_iso()})
            conn.commit()

    def _derive_group_status(self, group_id: str) -> str:
        """Derive group status from children statuses."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT status FROM downloads WHERE group_id = :gid"),
                {"gid": group_id},
            ).fetchall()

        statuses = {r[0] for r in rows}
        if not statuses:
            return _Status.PENDING
        if statuses == {_Status.COMPLETED}:
            return _Status.COMPLETED
        if _Status.DOWNLOADING in statuses:
            return _Status.DOWNLOADING
        if statuses == {_Status.CANCELLED} or (
            _Status.CANCELLED in statuses and statuses <= {_Status.CANCELLED, _Status.COMPLETED}
        ):
            return _Status.CANCELLED
        if _Status.FAILED in statuses and _Status.DOWNLOADING not in statuses:
            return _Status.FAILED
        if statuses <= {_Status.PAUSED, _Status.COMPLETED}:
            return _Status.PAUSED
        return _Status.PENDING

    def _stop_worker(self, download_id: str) -> None:
        """Signal a worker to stop (if running)."""
        with self._lock:
            cancel_ev = self._cancel_events.get(download_id)
            if cancel_ev:
                cancel_ev.set()

    def pause_download(self, download_id: str) -> None:
        """Pause a single download."""
        self._stop_worker(download_id)
        self._update_download_status(download_id, _Status.PAUSED)

    def resume_download(self, download_id: str) -> None:
        """Resume a paused download (sets to PENDING for worker pickup)."""
        self._update_download_status(download_id, _Status.PENDING)
        self._schedule_next()

    def cancel_download(self, download_id: str) -> None:
        """Cancel a single download."""
        self._stop_worker(download_id)
        self._update_download_status(download_id, _Status.CANCELLED)

    def pause_group(self, group_id: str) -> None:
        """Pause all non-completed children in a group."""
        logger.debug("Pausing group %s", group_id)
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :paused, updated_at = :now "
                "WHERE group_id = :gid AND status IN (:pending, :downloading)"
            ), {
                "paused": _Status.PAUSED, "pending": _Status.PENDING,
                "downloading": _Status.DOWNLOADING, "gid": group_id, "now": _now_iso(),
            })
            conn.commit()
        with self._engine.connect() as conn:
            dl_ids = [r[0] for r in conn.execute(
                text("SELECT id FROM downloads WHERE group_id = :gid"),
                {"gid": group_id},
            ).fetchall()]
        for dl_id in dl_ids:
            self._stop_worker(dl_id)
        self._update_group_status(group_id, _Status.PAUSED)

    def resume_group(self, group_id: str) -> None:
        """Resume paused/cancelled/failed children in a group.

        Already-completed files are left alone so only outstanding
        work is requeued.
        """
        logger.debug("Resuming group %s", group_id)
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :pending, updated_at = :now "
                "WHERE group_id = :gid "
                "AND status IN (:paused, :cancelled, :failed)"
            ), {"pending": _Status.PENDING, "paused": _Status.PAUSED,
                "cancelled": _Status.CANCELLED, "failed": _Status.FAILED,
                "gid": group_id, "now": _now_iso()})
            conn.commit()
        self._update_group_status(group_id, _Status.PENDING)
        self._schedule_next()

    def cancel_group(self, group_id: str) -> None:
        """Cancel all non-completed children in a group."""
        logger.debug("Cancelling group %s", group_id)
        with self._engine.connect() as conn:
            dl_ids = [r[0] for r in conn.execute(
                text("SELECT id FROM downloads WHERE group_id = :gid AND status NOT IN (:completed)"),
                {"gid": group_id, "completed": _Status.COMPLETED},
            ).fetchall()]
            conn.execute(text(
                "UPDATE downloads SET status = :cancelled, updated_at = :now "
                "WHERE group_id = :gid AND status NOT IN (:completed)"
            ), {"cancelled": _Status.CANCELLED, "completed": _Status.COMPLETED,
                "gid": group_id, "now": _now_iso()})
            conn.commit()
        for dl_id in dl_ids:
            self._stop_worker(dl_id)
        self._update_group_status(group_id, _Status.CANCELLED)

    def pause_all(self) -> None:
        """Pause all active/pending downloads."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :paused, updated_at = :now "
                "WHERE status IN (:pending, :downloading)"
            ), {"paused": _Status.PAUSED, "pending": _Status.PENDING,
                "downloading": _Status.DOWNLOADING, "now": _now_iso()})
            conn.execute(text(
                "UPDATE download_groups SET status = :paused, updated_at = :now "
                "WHERE status IN (:pending, :downloading)"
            ), {"paused": _Status.PAUSED, "pending": _Status.PENDING,
                "downloading": _Status.DOWNLOADING, "now": _now_iso()})
            conn.commit()
        with self._lock:
            for cancel_ev in self._cancel_events.values():
                cancel_ev.set()

    def resume_all(self) -> None:
        """Resume all paused/cancelled/failed downloads."""
        resumable = (_Status.PAUSED, _Status.CANCELLED, _Status.FAILED)
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :pending, updated_at = :now "
                "WHERE status IN (:s1, :s2, :s3)"
            ), {"pending": _Status.PENDING, "s1": resumable[0],
                "s2": resumable[1], "s3": resumable[2], "now": _now_iso()})
            conn.execute(text(
                "UPDATE download_groups SET status = :pending, updated_at = :now "
                "WHERE status IN (:s1, :s2, :s3)"
            ), {"pending": _Status.PENDING, "s1": resumable[0],
                "s2": resumable[1], "s3": resumable[2], "now": _now_iso()})
            conn.commit()
        self._schedule_next()

    def remove_group(self, group_id: str) -> None:
        """Cancel any active workers then delete the group and its downloads."""
        with self._engine.connect() as conn:
            dl_ids = [r[0] for r in conn.execute(
                text("SELECT id FROM downloads WHERE group_id = :gid"),
                {"gid": group_id},
            ).fetchall()]

        for dl_id in dl_ids:
            self._stop_worker(dl_id)

        with self._engine.connect() as conn:
            conn.execute(text(
                "DELETE FROM downloads WHERE group_id = :gid"
            ), {"gid": group_id})
            conn.execute(text(
                "DELETE FROM download_groups WHERE id = :gid"
            ), {"gid": group_id})
            conn.commit()

        logger.info("Removed download group %s", group_id)

    def clear_completed(self) -> int:
        """Remove all terminal groups (completed, cancelled, failed) + their downloads.

        Returns the number of groups removed.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id FROM download_groups "
                "WHERE status IN (:completed, :cancelled, :failed)"
            ), {
                "completed": _Status.COMPLETED,
                "cancelled": _Status.CANCELLED,
                "failed": _Status.FAILED,
            }).fetchall()

            group_ids = [r[0] for r in rows]
            if not group_ids:
                return 0

            for gid in group_ids:
                conn.execute(text(
                    "DELETE FROM downloads WHERE group_id = :gid"
                ), {"gid": gid})
                conn.execute(text(
                    "DELETE FROM download_groups WHERE id = :gid"
                ), {"gid": gid})

            conn.commit()

        logger.info("Cleared %d download group(s)", len(group_ids))
        return len(group_ids)

    def _schedule_next(self) -> None:
        """Start workers for pending downloads up to concurrency cap."""
        if self._max_concurrent <= 0:
            return  # 0 = don't auto-start (testing mode)

        with self._lock:
            active_count = len(self._workers)
            slots = self._max_concurrent - active_count
            if slots <= 0:
                return

        # Get next pending downloads, respecting group queue order
        with self._engine.connect() as conn:
            pending = conn.execute(text(
                "SELECT d.id, d.url, d.destination_path, d.temp_path, "
                "d.filename, d.checksum_expected, d.headers_json, d.cookies_json, "
                "d.group_id, d.chunks_json "
                "FROM downloads d "
                "JOIN download_groups g ON g.id = d.group_id "
                "WHERE d.status = :pending "
                "ORDER BY g.priority, d.priority, d.created_at "
                "LIMIT :limit"
            ), {"pending": _Status.PENDING, "limit": slots}).mappings().all()

        for row in pending:
            self._start_worker(row)

    def _start_worker(self, row) -> None:
        """Create and start a _DownloadWorker for a download row."""
        dl_id = row["id"]
        logger.debug("Starting worker for %s (%s)", dl_id, row.get("filename", "?"))
        cancel_ev = threading.Event()

        # Resolve paths via VolumeManager if not yet set
        dest_path = Path(row["destination_path"]) if row["destination_path"] else None

        if not dest_path:
            with self._engine.connect() as conn:
                group = conn.execute(
                    text("SELECT * FROM download_groups WHERE id = :gid"),
                    {"gid": row["group_id"]},
                ).mappings().first()

            if group:
                from luducat.core.archivist.volume import VolumeManager
                archive_path = self._config.get("downloads.archive_path", "")
                org = self._config.get("downloads.folder_organization", "store-slug")
                if not archive_path:
                    from luducat.core.config import get_default_archive_path
                    archive_path = str(get_default_archive_path())
                vm = VolumeManager(base_path=Path(archive_path), organization=org)
                dest_path = vm.resolve_path(
                    group["store_name"], group.get("store_app_id", ""),
                    group["game_title"], row["filename"],
                )

                with self._engine.connect() as conn:
                    conn.execute(text(
                        "UPDATE downloads SET destination_path = :dest, "
                        "updated_at = :now WHERE id = :id"
                    ), {"dest": str(dest_path), "id": dl_id, "now": _now_iso()})
                    conn.commit()

        # Parse stored headers/cookies/chunk state
        headers = json.loads(row["headers_json"]) if row["headers_json"] else None
        cookies = json.loads(row["cookies_json"]) if row["cookies_json"] else None
        chunk_state = json.loads(row["chunks_json"]) if row.get("chunks_json") else None
        if chunk_state:
            logger.debug("Loaded chunk state for %s: %d chunks",
                          dl_id, len(chunk_state.get("chunks", [])))

        # Create throttle function
        def throttle_fn(n_bytes: int) -> float:
            return self._bucket.consume(n_bytes)

        import requests as _req
        session = _req.Session()

        num_conn = self._config.get("downloads.max_connections_per_download", 4)
        chunk_mb = self._config.get("downloads.chunk_threshold_mb", 50)

        worker = _DownloadWorker(
            download_id=dl_id,
            url=row["url"],
            dest_path=dest_path,
            session=session,
            cancel_event=cancel_ev,
            throttle_fn=throttle_fn,
            num_connections=num_conn,
            chunk_threshold=chunk_mb * 1024 * 1024,
            expected_checksum=row.get("checksum_expected"),
            extra_headers=headers,
            cookies=cookies,
            on_progress=self._on_worker_progress,
            on_finished=self._on_worker_finished,
            chunk_state=chunk_state,
        )

        with self._lock:
            self._workers[dl_id] = worker
            self._cancel_events[dl_id] = cancel_ev

        # Update status to DOWNLOADING
        self._update_download_status(dl_id, _Status.DOWNLOADING)
        if row.get("group_id"):
            self._update_group_status(row["group_id"], _Status.DOWNLOADING)

        worker.start()

    def _on_worker_progress(self, download_id: str, bytes_dl: int,
                            total: int, speed: float) -> None:
        """Handle progress from a worker."""
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET bytes_downloaded = :bytes, updated_at = :now "
                "WHERE id = :id"
            ), {"bytes": bytes_dl, "id": download_id, "now": _now_iso()})
            conn.commit()

    def _on_worker_finished(self, download_id: str, result) -> None:
        """Handle worker completion — timestamp, move, archive, update status."""
        logger.debug("Worker finished: %s ok=%s reason=%r", download_id, result.ok, result.reason)
        # Clean up worker tracking
        with self._lock:
            self._workers.pop(download_id, None)
            self._cancel_events.pop(download_id, None)

        # Load download + group info from DB
        with self._engine.connect() as conn:
            dl_row = conn.execute(
                text("SELECT * FROM downloads WHERE id = :id"),
                {"id": download_id},
            ).mappings().first()

        if dl_row is None:
            logger.error("Worker finished for unknown download %s", download_id)
            return

        group_id = dl_row["group_id"]

        if not result.ok:
            current_status = dl_row["status"]
            if current_status in (_Status.PAUSED, _Status.CANCELLED):
                if result.chunk_state:
                    logger.debug("Persisting chunk state for %s", download_id)
                    with self._engine.connect() as conn:
                        conn.execute(text(
                            "UPDATE downloads SET chunks_json = :cj, "
                            "bytes_downloaded = :bytes, updated_at = :now "
                            "WHERE id = :id"
                        ), {
                            "cj": json.dumps(result.chunk_state),
                            "bytes": result.size,
                            "id": download_id,
                            "now": _now_iso(),
                        })
                        conn.commit()
                if group_id:
                    derived = self._derive_group_status(group_id)
                    self._update_group_status(group_id, derived)
                return
            self._update_download_status(download_id, _Status.FAILED)
            with self._engine.connect() as conn:
                conn.execute(text(
                    "UPDATE downloads SET last_error = :err, last_error_at = :now, "
                    "updated_at = :now WHERE id = :id"
                ), {"err": result.reason, "now": _now_iso(), "id": download_id})
                conn.commit()
            if group_id:
                derived = self._derive_group_status(group_id)
                self._update_group_status(group_id, derived)
            self._schedule_next()
            return

        # Success path: stamp → move → archive → update
        tmp_file = result.dest_path
        if tmp_file and tmp_file.exists():
            # 1. Stamp temp file with remote_mtime
            mtime = result.remote_mtime or time.time()
            os.utime(tmp_file, (mtime, mtime))

            # 2. Move to volume (preserves timestamp)
            try:
                archive_path = self._config.get("downloads.archive_path", "")
                org = self._config.get("downloads.folder_organization", "store-slug")
                if not archive_path:
                    from luducat.core.config import get_default_archive_path
                    archive_path = str(get_default_archive_path())

                from luducat.core.archivist.volume import VolumeManager
                vm = VolumeManager(base_path=Path(archive_path), organization=org)

                with self._engine.connect() as conn:
                    group = conn.execute(
                        text("SELECT * FROM download_groups WHERE id = :id"),
                        {"id": group_id},
                    ).mappings().first()

                if group:
                    final_path, rel_path = vm.move_to_volume(
                        tmp_file,
                        group["store_name"],
                        group.get("store_app_id", ""),
                        group["game_title"],
                        dl_row["filename"],
                    )

                    # 3. Register in archive manifest
                    from luducat.core.archivist.types import ArchiveEntry, ArchiveType
                    from luducat.core.archivist.manager import ArchivistManager

                    archivist = ArchivistManager(
                        engine=self._engine,
                        base_path=Path(archive_path),
                        organization=org,
                    )
                    entry = ArchiveEntry(
                        id=uuid.uuid4().hex,
                        archive_type=ArchiveType.GAME_INSTALLER,
                        filename=dl_row["filename"],
                        relative_path=rel_path,
                        size_bytes=result.size,
                        checksum_sha256=result.checksum_sha256,
                        downloaded_at=datetime.now(),
                        store_name=group["store_name"],
                        store_app_id=group.get("store_app_id"),
                        original_download_url=dl_row["url"],
                        remote_timestamp=datetime.fromtimestamp(mtime) if mtime else None,
                    )
                    archivist.add_entry(entry)

            except Exception as exc:
                logger.error("Archivist handoff failed for %s: %s", download_id, exc)

        # 4. Mark download COMPLETED, update group counters (single transaction)
        now = _now_iso()
        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE downloads SET status = :status, bytes_downloaded = :size, "
                "chunks_json = NULL, updated_at = :now "
                "WHERE id = :id"
            ), {"status": _Status.COMPLETED, "size": result.size,
                "now": now, "id": download_id})

            if group_id:
                conn.execute(text(
                    "UPDATE download_groups SET "
                    "files_completed = files_completed + 1, "
                    "downloaded_bytes = downloaded_bytes + :size, "
                    "updated_at = :now "
                    "WHERE id = :gid"
                ), {"size": result.size, "now": now, "gid": group_id})
            conn.commit()

        if group_id:
            derived = self._derive_group_status(group_id)
            self._update_group_status(group_id, derived)
            if derived == _Status.COMPLETED:
                self._set_group_verified(group_id, result)

        self._schedule_next()

    def _set_group_verified(self, group_id: str, last_result) -> None:
        """Determine verification status for a completed group."""
        has_store_checksum = bool(last_result.checksum_store)

        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT checksum_expected FROM downloads WHERE group_id = :gid"
            ), {"gid": group_id}).fetchall()

        any_store_checksum = has_store_checksum or any(
            r[0] for r in rows if r[0]
        )
        verified = "ok" if any_store_checksum else "size_only"

        with self._engine.connect() as conn:
            conn.execute(text(
                "UPDATE download_groups SET verified = :v, updated_at = :now "
                "WHERE id = :id"
            ), {"v": verified, "id": group_id, "now": _now_iso()})
            conn.commit()

    def move_group_up(self, group_id: str) -> None:
        """Swap a group with the one above it (lower priority number)."""
        with self._engine.connect() as conn:
            cur = conn.execute(text(
                "SELECT priority FROM download_groups WHERE id = :id"
            ), {"id": group_id}).scalar()
            if cur is None:
                return

            above = conn.execute(text(
                "SELECT id, priority FROM download_groups "
                "WHERE priority < :cur ORDER BY priority DESC LIMIT 1"
            ), {"cur": cur}).first()
            if above is None:
                return

            now = _now_iso()
            conn.execute(text(
                "UPDATE download_groups SET priority = :p, updated_at = :now "
                "WHERE id = :id"
            ), {"p": above[1], "id": group_id, "now": now})
            conn.execute(text(
                "UPDATE download_groups SET priority = :p, updated_at = :now "
                "WHERE id = :id"
            ), {"p": cur, "id": above[0], "now": now})
            conn.commit()

    def move_group_down(self, group_id: str) -> None:
        """Swap a group with the one below it (higher priority number)."""
        with self._engine.connect() as conn:
            cur = conn.execute(text(
                "SELECT priority FROM download_groups WHERE id = :id"
            ), {"id": group_id}).scalar()
            if cur is None:
                return

            below = conn.execute(text(
                "SELECT id, priority FROM download_groups "
                "WHERE priority > :cur ORDER BY priority ASC LIMIT 1"
            ), {"cur": cur}).first()
            if below is None:
                return

            now = _now_iso()
            conn.execute(text(
                "UPDATE download_groups SET priority = :p, updated_at = :now "
                "WHERE id = :id"
            ), {"p": below[1], "id": group_id, "now": now})
            conn.execute(text(
                "UPDATE download_groups SET priority = :p, updated_at = :now "
                "WHERE id = :id"
            ), {"p": cur, "id": below[0], "now": now})
            conn.commit()

    def move_group_to(self, group_id: str, target_index: int) -> None:
        """Move a group to an arbitrary position in the queue.

        Renumbers all group priorities to match the new ordering.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id FROM download_groups ORDER BY priority ASC"
            )).fetchall()
            ids = [r[0] for r in rows]
            if group_id not in ids:
                return
            ids.remove(group_id)
            target_index = max(0, min(target_index, len(ids)))
            ids.insert(target_index, group_id)
            now = _now_iso()
            for prio, gid in enumerate(ids):
                conn.execute(text(
                    "UPDATE download_groups SET priority = :p, updated_at = :now "
                    "WHERE id = :id"
                ), {"p": prio, "id": gid, "now": now})
            conn.commit()

    def shutdown(self) -> None:
        """Clean stop: cancel all workers, flush DB."""
        with self._lock:
            for cancel_ev in self._cancel_events.values():
                cancel_ev.set()
        logger.info("DownloadManager shutdown complete")


# ── Singleton ───────────────────────────────────────────────────────

_instance: Optional[DownloadManager] = None


def init_download_manager(engine: "Engine", config) -> DownloadManager:
    """Initialize the singleton. Called once during app startup."""
    global _instance
    _instance = DownloadManager(engine, config)
    return _instance


def get_download_manager() -> DownloadManager:
    """Get the singleton. Raises RuntimeError if not initialized."""
    if _instance is None:
        raise RuntimeError(
            "DownloadManager not initialized — call init_download_manager() first"
        )
    return _instance


def reset_download_manager() -> None:
    """Clear the singleton (for test teardown)."""
    global _instance
    if _instance is not None:
        _instance.shutdown()
    _instance = None
