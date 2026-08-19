# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Build-number cache for the archive update check (store_build_cache).

The update scan compares the build number on disk against the build
number GOG currently serves; both are expensive enough to cache. Rows
carry a jittered freshness horizon baked in at persist time: TTL (days,
config downloads.build_cache_ttl_days, clamped >= 1) +/- 20% computed
in hours, so a library scanned in one sitting does not expire in one
sitting either (a 7-day TTL spreads per-game horizons over roughly
134-202 hours).

Writes are batched: the scan runs over thousands of games, and a
per-game commit would turn the cache into the bottleneck it exists to
remove. flush() runs automatically every FLUSH_EVERY dirty rows; the
scan calls it once more when it finishes or gets cancelled.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Optional

from sqlalchemy import text

from luducat.core.dt import utc_now

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

_JITTER_FRACTION = 0.2


def cache_ttl_days(config) -> int:
    """Configured build-cache TTL in days, clamped to the 1-day minimum."""
    if config is None:
        return 7
    try:
        ttl = int(config.get("downloads.build_cache_ttl_days", 7))
    except (TypeError, ValueError):
        return 7
    return max(1, ttl)


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class BuildCache:
    """Read-through/batched-write access to one store's build rows.

    load() pulls the whole store's rows up front -- the scan visits
    every game anyway, and one SELECT beats thousands. Freshness is
    judged against the row's persisted expiry horizon, never recomputed
    from the TTL, so a TTL change applies to rows as they refresh (the
    horizon was the persist-time promise).
    """

    FLUSH_EVERY = 15

    def __init__(
        self,
        engine: "Engine",
        store_name: str,
        ttl_days: int,
        now_fn: Callable[[], datetime] = utc_now,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if engine is None or not store_name:
            raise ValueError("BuildCache needs an engine and a store name")
        if ttl_days < 1:
            raise ValueError(f"ttl_days must be >= 1, got {ttl_days}")
        self._engine = engine
        self._store = store_name
        self._ttl_days = ttl_days
        self._now_fn = now_fn
        self._jitter_fn = jitter_fn
        self._rows: dict[str, dict] = {}
        self._dirty: set[str] = set()

    # -- Loading and freshness --

    def load(self) -> None:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT store_app_id, local_build, local_checked_at, "
                    "local_expires_at, online_build, online_checked_at, "
                    "online_expires_at FROM store_build_cache "
                    "WHERE store_name = :store"
                ),
                {"store": self._store},
            ).mappings().all()
        self._rows = {r["store_app_id"]: dict(r) for r in rows}
        self._dirty.clear()

    def _fresh(self, app_id: str, side: str) -> bool:
        row = self._rows.get(app_id)
        if row is None or row.get(f"{side}_checked_at") is None:
            return False
        expires = _parse_dt(row.get(f"{side}_expires_at"))
        return expires is not None and expires > self._now_fn()

    def local_build(self, app_id: str) -> Optional[int]:
        """Cached on-disk build, only while its horizon holds."""
        if not self._fresh(app_id, "local"):
            return None
        return self._rows[app_id].get("local_build")

    def online_build(self, app_id: str,
                     allow_stale: bool = False) -> Optional[int]:
        """Cached online build; allow_stale serves the offline path.

        Offline there is nothing better than the last known answer,
        however old -- a stale build still beats flagging nothing, and
        the spec pins that offline never reaches out.
        """
        if allow_stale:
            row = self._rows.get(app_id)
            return row.get("online_build") if row else None
        if not self._fresh(app_id, "online"):
            return None
        return self._rows[app_id].get("online_build")

    # -- Writing --

    def _horizon(self, now: datetime) -> datetime:
        ttl_hours = self._ttl_days * 24.0
        jitter = self._jitter_fn(-_JITTER_FRACTION, _JITTER_FRACTION)
        return now + timedelta(hours=ttl_hours * (1.0 + jitter))

    def _row(self, app_id: str) -> dict:
        return self._rows.setdefault(app_id, {
            "store_app_id": app_id,
            "local_build": None, "local_checked_at": None,
            "local_expires_at": None,
            "online_build": None, "online_checked_at": None,
            "online_expires_at": None,
        })

    def _set(self, app_id: str, side: str, build: Optional[int]) -> None:
        now = self._now_fn()
        row = self._row(app_id)
        row[f"{side}_build"] = build
        row[f"{side}_checked_at"] = now.isoformat()
        row[f"{side}_expires_at"] = self._horizon(now).isoformat()
        self._dirty.add(app_id)
        if len(self._dirty) >= self.FLUSH_EVERY:
            self.flush()

    def set_local(self, app_id: str, build: int) -> None:
        self._set(app_id, "local", int(build))

    def set_online(self, app_id: str, build: int) -> None:
        self._set(app_id, "online", int(build))

    def flush(self) -> None:
        """Persist dirty rows. Whole-row upserts: the in-memory rows are
        the merge of the loaded state and this scan's writes, so writing
        both sides never loses the one that did not change."""
        if not self._dirty:
            return
        payload = []
        for app_id in sorted(self._dirty):
            row = self._rows[app_id]
            payload.append({
                "store": self._store,
                "app": app_id,
                "local_build": row.get("local_build"),
                "local_checked_at": row.get("local_checked_at"),
                "local_expires_at": row.get("local_expires_at"),
                "online_build": row.get("online_build"),
                "online_checked_at": row.get("online_checked_at"),
                "online_expires_at": row.get("online_expires_at"),
            })
        with self._engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO store_build_cache (
                        store_name, store_app_id,
                        local_build, local_checked_at, local_expires_at,
                        online_build, online_checked_at, online_expires_at
                    ) VALUES (
                        :store, :app,
                        :local_build, :local_checked_at, :local_expires_at,
                        :online_build, :online_checked_at, :online_expires_at
                    )
                    ON CONFLICT (store_name, store_app_id) DO UPDATE SET
                        local_build = excluded.local_build,
                        local_checked_at = excluded.local_checked_at,
                        local_expires_at = excluded.local_expires_at,
                        online_build = excluded.online_build,
                        online_checked_at = excluded.online_checked_at,
                        online_expires_at = excluded.online_expires_at
                """),
                payload,
            )
            conn.commit()
        logger.debug("Build cache: flushed %d row(s) for %s",
                     len(payload), self._store)
        self._dirty.clear()


def load_builds(engine: "Engine",
                store_name: str) -> dict[str, tuple[Optional[int],
                                                    Optional[int]]]:
    """All cached (local, online) builds for a store, freshness ignored.

    Display helper for the review dialog: persisted audit results are
    reopened long after a scan, and a stale number that names the actual
    on-disk build is still the truth the columns promise. Comparison
    freshness is the scan's job, not the dialog's.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT store_app_id, local_build, online_build "
                "FROM store_build_cache WHERE store_name = :store"
            ),
            {"store": store_name},
        ).fetchall()
    return {app_id: (local, online) for app_id, local, online in rows}
