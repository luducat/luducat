# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#
"""gameDetails response cache (game_details_cache).

Caches parsed GOG gameDetails API responses with a jittered TTL so
library-scale scans skip unchanged games. The audit scan hits
embed.gog.com/account/gameDetails/{id}.json for every owned game;
caching cuts a 4200-game scan from ~30 minutes to seconds for the
stable majority.

Follows the BuildCache pattern: load() pulls all rows up front,
freshness is judged against the persisted expiry horizon (never
recomputed from the TTL), and writes are batched via flush().
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Optional

from sqlalchemy import text

from luducat.core.dt import utc_now
from luducat.core.json_compat import json

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

_JITTER_FRACTION = 0.2


def details_cache_ttl_days(config) -> float:
    """Configured details-cache TTL in days, clamped to the 6-hour minimum."""
    if config is None:
        return 3.0
    try:
        ttl = float(config.get("downloads.details_cache_ttl_days", 3.0))
    except (TypeError, ValueError):
        return 3.0
    return max(0.25, ttl)


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class DetailsCache:
    """Read-through/batched-write cache for gameDetails responses.

    load() pulls the whole store's rows up front - the scan visits
    every game anyway. Freshness is judged against the row's persisted
    expiry horizon, so a TTL change applies to rows as they refresh.
    """

    FLUSH_EVERY = 15

    def __init__(
        self,
        engine: "Engine",
        store_name: str,
        ttl_days: float,
        now_fn: Callable[[], datetime] = utc_now,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if engine is None or not store_name:
            raise ValueError("DetailsCache needs an engine and a store name")
        if ttl_days < 0.25:
            raise ValueError(f"ttl_days must be >= 0.25, got {ttl_days}")
        self._engine = engine
        self._store = store_name
        self._ttl_days = ttl_days
        self._now_fn = now_fn
        self._jitter_fn = jitter_fn
        self._rows: dict[str, dict] = {}
        self._dirty: set[str] = set()

    def load(self) -> None:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT store_app_id, details_json, checked_at, "
                    "expires_at FROM game_details_cache "
                    "WHERE store_name = :store"
                ),
                {"store": self._store},
            ).mappings().all()
        self._rows = {r["store_app_id"]: dict(r) for r in rows}
        self._dirty.clear()

    def get(self, app_id: str) -> Optional[dict[str, Any]]:
        """Return cached details dict if fresh, None if stale or missing."""
        row = self._rows.get(app_id)
        if row is None:
            return None
        expires = _parse_dt(row.get("expires_at"))
        if expires is None or expires <= self._now_fn():
            return None
        raw = row.get("details_json")
        if not raw:
            return None
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None

    def put(self, app_id: str, details: dict[str, Any]) -> None:
        """Store a gameDetails response with jittered expiry."""
        now = self._now_fn()
        self._rows[app_id] = {
            "store_app_id": app_id,
            "details_json": json.dumps(details),
            "checked_at": now.isoformat(),
            "expires_at": self._horizon(now).isoformat(),
        }
        self._dirty.add(app_id)
        if len(self._dirty) >= self.FLUSH_EVERY:
            self.flush()

    def invalidate(self, app_id: str) -> None:
        """Mark one entry stale by clearing its expiry."""
        row = self._rows.get(app_id)
        if row is not None:
            row["expires_at"] = None
            self._dirty.add(app_id)

    def invalidate_all(self) -> None:
        """Clear all entries for this store from DB and memory."""
        with self._engine.connect() as conn:
            conn.execute(
                text(
                    "DELETE FROM game_details_cache "
                    "WHERE store_name = :store"
                ),
                {"store": self._store},
            )
            conn.commit()
        self._rows.clear()
        self._dirty.clear()

    def _horizon(self, now: datetime) -> datetime:
        ttl_hours = self._ttl_days * 24.0
        jitter = self._jitter_fn(-_JITTER_FRACTION, _JITTER_FRACTION)
        return now + timedelta(hours=ttl_hours * (1.0 + jitter))

    def flush(self) -> None:
        """Persist dirty rows via batched upserts."""
        if not self._dirty:
            return
        payload = []
        for app_id in sorted(self._dirty):
            row = self._rows[app_id]
            payload.append({
                "store": self._store,
                "app": app_id,
                "details_json": row.get("details_json"),
                "checked_at": row.get("checked_at"),
                "expires_at": row.get("expires_at"),
            })
        with self._engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO game_details_cache (
                        store_name, store_app_id,
                        details_json, checked_at, expires_at
                    ) VALUES (
                        :store, :app,
                        :details_json, :checked_at, :expires_at
                    )
                    ON CONFLICT (store_name, store_app_id) DO UPDATE SET
                        details_json = excluded.details_json,
                        checked_at = excluded.checked_at,
                        expires_at = excluded.expires_at
                """),
                payload,
            )
            conn.commit()
        logger.debug("Details cache: flushed %d row(s) for %s",
                     len(payload), self._store)
        self._dirty.clear()
