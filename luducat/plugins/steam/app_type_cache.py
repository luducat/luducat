# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

"""Lightweight SQLite cache for Steam app type resolution.

Separate from the user's steamscraper database. Stores only the minimum
needed to classify app IDs as game/dlc/music/etc. Disposable and
rebuildable at any time.
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Unknown (type=NULL) rows are re-queried after this long. Steam's
# soft throttle can hide apps from a response; unknown is a retryable
# observation, not a permanent fact. Typed rows never expire.
NULL_RETRY_TTL_SECONDS = 7 * 86400

_SCHEMA_VERSION = 2


class AppTypeCache:
    """SQLite cache mapping Steam app IDs to their store type."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS apps (
                appid INTEGER PRIMARY KEY,
                type TEXT,
                name TEXT,
                is_free INTEGER DEFAULT 0,
                resolved_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < _SCHEMA_VERSION:
            self._migrate(version)
        self._conn.commit()

    def _migrate(self, from_version: int):
        """Upgrade a pre-v2 database in place.

        v1 had no resolved_at column. Typed rows are stamped now
        (permanent); NULL rows get 0 so they are retried immediately -
        this heals caches poisoned by soft-throttled empty responses.
        """
        columns = {
            row[1] for row in
            self._conn.execute("PRAGMA table_info(apps)").fetchall()
        }
        if "resolved_at" not in columns:
            self._conn.execute(
                "ALTER TABLE apps ADD COLUMN "
                "resolved_at INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            "UPDATE apps SET resolved_at = ? "
            "WHERE type IS NOT NULL AND resolved_at = 0",
            (int(time.time()),),
        )
        self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        logger.info(
            "App type cache migrated from schema v%d to v%d",
            from_version, _SCHEMA_VERSION,
        )

    def store(self, appid: int, app_type: str | None,
              name: str | None, is_free: bool):
        """Store or update a single app's type info."""
        self._conn.execute(
            "INSERT OR REPLACE INTO apps "
            "(appid, type, name, is_free, resolved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (appid, app_type, name, 1 if is_free else 0, int(time.time())),
        )
        self._conn.commit()

    def store_batch(self, entries: list[tuple[int, str | None,
                                              str | None, bool]]):
        """Store multiple entries at once.

        Each tuple: (appid, type, name, is_free).
        """
        if not entries:
            return
        now = int(time.time())
        self._conn.executemany(
            "INSERT OR REPLACE INTO apps "
            "(appid, type, name, is_free, resolved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(a, t, n, 1 if f else 0, now) for a, t, n, f in entries],
        )
        self._conn.commit()

    def get_type(self, appid: int) -> str | None:
        """Get cached type for an app, or None if not cached."""
        row = self._conn.execute(
            "SELECT type FROM apps WHERE appid = ?", (appid,)
        ).fetchone()
        return row[0] if row else None

    def is_cached(self, appid: int) -> bool:
        """Check if an app ID has been resolved (even if type is None)."""
        row = self._conn.execute(
            "SELECT 1 FROM apps WHERE appid = ?", (appid,)
        ).fetchone()
        return row is not None

    def get_cached_types(self, appids: set[int]) -> dict[int, str | None]:
        """Bulk lookup: return {appid: type} for all cached entries."""
        if not appids:
            return {}
        results = {}
        batch = sorted(appids)
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT appid, type FROM apps WHERE appid IN ({placeholders})",
                chunk,
            ).fetchall()
            for appid, app_type in rows:
                results[appid] = app_type
        return results

    def get_names(self, appids: set[int]) -> dict[int, str | None]:
        """Bulk lookup: return {appid: name} for cached entries that have a name."""
        if not appids:
            return {}
        results = {}
        batch = sorted(appids)
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT appid, name FROM apps WHERE appid IN ({placeholders})"
                " AND name IS NOT NULL",
                chunk,
            ).fetchall()
            for appid, name in rows:
                results[appid] = name
        return results

    def get_uncached(self, appids: set[int]) -> set[int]:
        """Return app IDs that need (re-)resolution.

        Not present at all, or cached as unknown longer ago than
        NULL_RETRY_TTL_SECONDS.
        """
        if not appids:
            return set()
        cutoff = int(time.time()) - NULL_RETRY_TTL_SECONDS
        settled = set()
        batch = sorted(appids)
        for i in range(0, len(batch), 500):
            chunk = batch[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT appid FROM apps WHERE appid IN ({placeholders}) "
                "AND (type IS NOT NULL OR resolved_at >= ?)",
                (*chunk, cutoff),
            ).fetchall()
            settled.update(row[0] for row in rows)
        return appids - settled

    def close(self):
        """Close the database connection."""
        self._conn.close()
