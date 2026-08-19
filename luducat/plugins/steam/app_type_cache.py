# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

"""Lightweight SQLite cache for Steam app type resolution.

Separate from the user's steamscraper database. Stores only the minimum
needed to classify app IDs as game/dlc/music/etc. Disposable and
rebuildable at any time.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


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
                is_free INTEGER DEFAULT 0
            )
        """)
        self._conn.commit()

    def store(self, appid: int, app_type: Optional[str],
              name: Optional[str], is_free: bool):
        """Store or update a single app's type info."""
        self._conn.execute(
            "INSERT OR REPLACE INTO apps (appid, type, name, is_free) "
            "VALUES (?, ?, ?, ?)",
            (appid, app_type, name, 1 if is_free else 0),
        )
        self._conn.commit()

    def store_batch(self, entries: List[Tuple[int, Optional[str],
                                              Optional[str], bool]]):
        """Store multiple entries at once.

        Each tuple: (appid, type, name, is_free).
        """
        if not entries:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO apps (appid, type, name, is_free) "
            "VALUES (?, ?, ?, ?)",
            [(a, t, n, 1 if f else 0) for a, t, n, f in entries],
        )
        self._conn.commit()

    def get_type(self, appid: int) -> Optional[str]:
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

    def get_cached_types(self, appids: Set[int]) -> Dict[int, Optional[str]]:
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

    def get_names(self, appids: Set[int]) -> Dict[int, Optional[str]]:
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

    def get_uncached(self, appids: Set[int]) -> Set[int]:
        """Return app IDs that are NOT in the cache."""
        cached = self.get_cached_types(appids)
        return appids - set(cached.keys())

    def close(self):
        """Close the database connection."""
        self._conn.close()
