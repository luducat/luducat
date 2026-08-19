# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""ArchivistManager — manifest CRUD for the archive volume.

Reads and writes to the `archives` table in the main database. Uses raw SQL
(no ORM model) consistent with other migration-only tables.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from sqlalchemy import text

from luducat.core.archivist.types import ArchiveEntry, ArchiveType
from luducat.core.json_compat import json

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    """Serialize datetime to ISO string for SQLite (avoids deprecated adapter)."""
    return dt.isoformat() if dt else None


def _str_to_dt(s) -> Optional[datetime]:
    """Parse ISO datetime string back to datetime object."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


class ArchivistManager:
    """Manages the archive manifest (archives table) and volume paths.

    Args:
        engine: SQLAlchemy engine connected to the main database.
        base_path: Root directory of the archive volume.
        organization: Folder organization mode (default: "store-slug").
    """

    def __init__(
        self,
        engine: "Engine",
        base_path: Path,
        organization: str = "store-slug",
        custom_layout: Optional[str] = None,
    ) -> None:
        from luducat.core.archivist.volume import VolumeManager

        self._engine = engine
        self.volume = VolumeManager(
            base_path=base_path, organization=organization,
            custom_layout=custom_layout)

    def add_entry(self, entry: ArchiveEntry) -> None:
        """Insert an archive entry into the manifest."""
        self.add_entries([entry])

    def add_entries(self, entries: list[ArchiveEntry]) -> None:
        """Insert archive entries in one transaction.

        Adoption commits tens of thousands of rows; per-row commits
        would turn that into minutes of fsync traffic.
        """
        if not entries:
            return
        with self._engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO archives (
                        id, archive_type, store_name, store_app_id,
                        filename, relative_path, size_bytes, checksum_sha256,
                        version, original_download_url, remote_timestamp,
                        downloaded_at, verified_at, metadata_json
                    ) VALUES (
                        :id, :archive_type, :store_name, :store_app_id,
                        :filename, :relative_path, :size_bytes, :checksum_sha256,
                        :version, :original_download_url, :remote_timestamp,
                        :downloaded_at, :verified_at, :metadata_json
                    )
                """),
                [
                    {
                        "id": entry.id,
                        "archive_type": entry.archive_type.value,
                        "store_name": entry.store_name,
                        "store_app_id": entry.store_app_id,
                        "filename": entry.filename,
                        "relative_path": entry.relative_path,
                        "size_bytes": entry.size_bytes,
                        "checksum_sha256": entry.checksum_sha256,
                        "version": entry.version,
                        "original_download_url": entry.original_download_url,
                        "remote_timestamp": _dt_to_str(entry.remote_timestamp),
                        "downloaded_at": _dt_to_str(entry.downloaded_at),
                        "verified_at": _dt_to_str(entry.verified_at),
                        "metadata_json": json.dumps(entry.metadata) if entry.metadata else None,
                    }
                    for entry in entries
                ],
            )
            conn.commit()

    def get_entry(self, entry_id: str) -> Optional[ArchiveEntry]:
        """Fetch a single archive entry by ID."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM archives WHERE id = :id"),
                {"id": entry_id},
            ).mappings().first()

        if row is None:
            return None
        return _row_to_entry(row)

    def get_entries_by_store(
        self,
        store_name: str,
        store_app_id: Optional[str] = None,
    ) -> list[ArchiveEntry]:
        """Fetch archive entries for a store, optionally filtered by app ID."""
        with self._engine.connect() as conn:
            if store_app_id is not None:
                rows = conn.execute(
                    text("SELECT * FROM archives WHERE store_name = :store AND store_app_id = :app"),
                    {"store": store_name, "app": store_app_id},
                ).mappings().all()
            else:
                rows = conn.execute(
                    text("SELECT * FROM archives WHERE store_name = :store"),
                    {"store": store_name},
                ).mappings().all()

        return [_row_to_entry(r) for r in rows]

    def get_all_entries(self) -> list[ArchiveEntry]:
        """Fetch all archive entries."""
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM archives")).mappings().all()
        return [_row_to_entry(r) for r in rows]

    def remove_entry(self, entry_id: str) -> bool:
        """Remove an archive entry. Returns True if a row was deleted."""
        with self._engine.connect() as conn:
            result = conn.execute(
                text("DELETE FROM archives WHERE id = :id"),
                {"id": entry_id},
            )
            conn.commit()
            return result.rowcount > 0

    def mark_verified(self, entry_id: str, verified_at: datetime) -> None:
        """Update the verified_at timestamp for an entry."""
        with self._engine.connect() as conn:
            conn.execute(
                text("UPDATE archives SET verified_at = :ts WHERE id = :id"),
                {"ts": _dt_to_str(verified_at), "id": entry_id},
            )
            conn.commit()

    def count(self, store_name: Optional[str] = None) -> int:
        """Count archive entries, optionally filtered by store."""
        with self._engine.connect() as conn:
            if store_name:
                row = conn.execute(
                    text("SELECT COUNT(*) FROM archives WHERE store_name = :store"),
                    {"store": store_name},
                ).first()
            else:
                row = conn.execute(text("SELECT COUNT(*) FROM archives")).first()
            return row[0]


def _row_to_entry(row) -> ArchiveEntry:
    """Convert a DB row (mapping) to an ArchiveEntry."""
    meta_raw = row["metadata_json"]
    metadata = json.loads(meta_raw) if meta_raw else None

    return ArchiveEntry(
        id=row["id"],
        archive_type=ArchiveType(row["archive_type"]),
        store_name=row["store_name"],
        store_app_id=row["store_app_id"],
        filename=row["filename"],
        relative_path=row["relative_path"],
        size_bytes=row["size_bytes"],
        checksum_sha256=row["checksum_sha256"],
        version=row["version"],
        original_download_url=row["original_download_url"],
        remote_timestamp=_str_to_dt(row["remote_timestamp"]),
        downloaded_at=_str_to_dt(row["downloaded_at"]),
        verified_at=_str_to_dt(row["verified_at"]),
        metadata=metadata,
    )
