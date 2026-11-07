# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# miss_cache.py

"""Negative cache for own-store lookups.

A store plugin remembers ids its source answered "no such product"
for (delisted, removed, region-locked) so the lazy path does not ask
again on every scroll. Entries expire; a delisted product can come
back. Only a definite miss belongs here, never a failed request.
Disposable and rebuildable at any time, separate from the catalog.

Self-contained: zero imports from ``luducat.core``.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Miss TTL default from the resolver contract (D4); a settings surface
# replaces this constant in a later phase.
MISS_TTL_SECONDS = 14 * 86400


class LookupMissCache:
    """SQLite map of app id -> (reason, missed_at) with a TTL."""

    def __init__(
        self,
        db_path: Path,
        ttl_seconds: int = MISS_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else time.time
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS misses (
                appid INTEGER PRIMARY KEY,
                reason TEXT NOT NULL,
                missed_at INTEGER NOT NULL
            )
        """)
        self._conn.commit()

    def reason(self, appid: int) -> str | None:
        """The cached miss reason, or None when unknown or expired."""
        row = self._conn.execute(
            "SELECT reason, missed_at FROM misses WHERE appid = ?", (int(appid),)
        ).fetchone()
        if row is None:
            return None
        reason, missed_at = row
        if self._clock() - missed_at > self._ttl:
            self.forget(appid)
            return None
        return reason

    def remember(self, appid: int, reason: str) -> None:
        if not reason:
            raise ValueError("a miss needs a reason")
        self._conn.execute(
            "INSERT OR REPLACE INTO misses (appid, reason, missed_at) VALUES (?, ?, ?)",
            (int(appid), reason, int(self._clock())),
        )
        self._conn.commit()

    def remembered(self) -> set[int]:
        """Every app id with an unexpired miss; expired rows are left for
        reason() to drop on their next lookup."""
        oldest = int(self._clock()) - self._ttl
        rows = self._conn.execute(
            "SELECT appid FROM misses WHERE missed_at >= ?", (oldest,)
        ).fetchall()
        return {int(r[0]) for r in rows}

    def forget(self, appid: int) -> None:
        self._conn.execute("DELETE FROM misses WHERE appid = ?", (int(appid),))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
