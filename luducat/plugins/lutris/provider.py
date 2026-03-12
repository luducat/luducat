# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""Lutris Metadata Provider.

Imports tags (categories), favourites, hidden status, and playtime
from Lutris's local pga.db into luducat. No network access — reads
only local SQLite database.

Supported data:
- Categories → luducat tags (source="lutris", skip reserved names)
- "favorite" category → luducat is_favorite flag
- ".hidden" category → luducat hidden flag
- playtime (hours) → playtime_minutes
- lastplayed (unix ts) → last_played ISO date

Delta sync:
- Caches Lutris state in plugin DB (lutris.db)
- Detects additions, changes, and removals between syncs
- Returns delta-aware entries so tag_service can add AND remove

Database location:
- ~/.local/share/lutris/pga.db (XDG_DATA_HOME)
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from luducat.plugins.base import (
    AbstractMetadataProvider,
    EnrichmentData,
    MetadataSearchResult,
)
from luducat.core.json_compat import json

logger = logging.getLogger(__name__)

# Lutris service names → luducat store names
_SERVICE_MAP = {
    "steam": "steam",
    "gog": "gog",
    "egs": "epic",
}

# Reserved Lutris category names (not imported as tags)
_RESERVED_CATEGORIES = {"favorite", ".hidden", ".uncategorized", "all"}

# Plugin DB schema version
_STATE_DB_VERSION = 1


class LutrisProvider(AbstractMetadataProvider):
    """Import tags, favourites, hidden status and playtime from Lutris."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "lutris"

    @property
    def display_name(self) -> str:
        return "Lutris"

    # ── Availability ──────────────────────────────────────────────────

    def _find_pga_db(self) -> Optional[Path]:
        """Locate Lutris pga.db.

        Returns:
            Path to pga.db, or None if not found.
        """
        candidates = [
            Path.home() / ".local" / "share" / "lutris" / "pga.db",
            Path.home() / ".cache" / "lutris" / "pga.db",  # legacy
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def is_available(self) -> bool:
        return self._find_pga_db() is not None

    # ── Auth (not needed) ─────────────────────────────────────────────

    async def authenticate(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    # ── Plugin state DB ───────────────────────────────────────────────

    def _init_state_db(self, conn: sqlite3.Connection) -> None:
        """Create the lutris_state table if it doesn't exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lutris_state (
                service TEXT NOT NULL,
                service_id TEXT NOT NULL,
                title TEXT,
                installed INTEGER DEFAULT 0,
                slug TEXT,
                tags TEXT,
                is_favorite INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                playtime_hours REAL DEFAULT 0,
                last_played TEXT,
                PRIMARY KEY (service, service_id)
            )
        """)

    def _get_state_db(self) -> sqlite3.Connection:
        """Open (and init) the plugin state database."""
        db_path = self.get_database_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        self._init_state_db(conn)
        return conn

    def _load_cached_state(
        self, conn: sqlite3.Connection
    ) -> Dict[str, Dict[str, Any]]:
        """Load all cached entries from plugin DB.

        Returns:
            Dict keyed by "store:app_id" → cached state dict
        """
        cached: Dict[str, Dict[str, Any]] = {}
        cursor = conn.execute(
            "SELECT service, service_id, title, tags, "
            "is_favorite, is_hidden, playtime_hours, last_played "
            "FROM lutris_state"
        )
        for row in cursor:
            store = _SERVICE_MAP.get(row["service"])
            if not store:
                continue
            key = f"{store}:{row['service_id']}"
            tags_json = row["tags"]
            tags = json.loads(tags_json) if tags_json else []
            cached[key] = {
                "store": store,
                "app_id": row["service_id"],
                "title": row["title"] or "",
                "tags": tags,
                "is_favorite": bool(row["is_favorite"]),
                "is_hidden": bool(row["is_hidden"]),
                "playtime_hours": row["playtime_hours"] or 0,
                "last_played": row["last_played"] or "",
            }
        return cached

    def _save_state(
        self, conn: sqlite3.Connection, current: Dict[str, Dict[str, Any]]
    ) -> None:
        """Replace cached state with current pga.db snapshot.

        Args:
            conn: Plugin state DB connection
            current: Current pga.db data keyed by "store:app_id"
        """
        conn.execute("DELETE FROM lutris_state")

        reverse_service = {v: k for k, v in _SERVICE_MAP.items()}
        for entry in current.values():
            service = reverse_service.get(entry["store"])
            if not service:
                continue
            conn.execute(
                "INSERT INTO lutris_state "
                "(service, service_id, title, tags, is_favorite, is_hidden, "
                "playtime_hours, last_played) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    service,
                    entry["app_id"],
                    entry.get("title", ""),
                    json.dumps(entry.get("tags", [])),
                    int(entry.get("is_favorite", False)),
                    int(entry.get("is_hidden", False)),
                    entry.get("playtime_hours", 0),
                    entry.get("last_played", ""),
                ),
            )
        conn.commit()

    # ── Tag sync ──────────────────────────────────────────────────────

    def get_tag_sync_data(self, **kwargs) -> Optional[Dict[str, Any]]:
        """Read Lutris pga.db and return delta-aware tag sync data.

        Compares current pga.db state against cached state in plugin DB.
        Returns additions and removals so tag_service can do full delta sync.

        Keyword Args:
            import_favourites: Override for import_favourites setting
                (from centralized tags config).

        Returns:
            Dict with "source", "mode", "entries", "removals" or None.
        """
        if not self.has_local_data_consent():
            logger.debug("Skipping Lutris tag sync: local data consent not granted")
            return None

        db_path = self._find_pga_db()
        if not db_path:
            return None

        try:
            current = self._read_sync_data(db_path, **kwargs)
        except Exception as e:
            logger.warning(f"Failed to read Lutris pga.db: {e}")
            return None

        # Compute delta against cached state
        try:
            entries, removals = self._compute_delta(current, **kwargs)
        except Exception as e:
            logger.warning(f"Failed to compute Lutris delta: {e}")
            # Fall back to full current data
            entries = list(current.values())
            removals = []

        if not entries and not removals:
            return None

        return {
            "source": "lutris",
            "mode": "delta",
            "entries": entries,
            "removals": removals,
        }

    def _compute_delta(
        self, current: Dict[str, Dict[str, Any]], **kwargs
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Compare current pga.db state against cached state.

        Returns:
            (entries_to_sync, removals) — entries include new + changed,
            removals include entries that disappeared from pga.db.
        """
        import_favs = kwargs.get(
            "import_favourites",
            self.get_setting("import_favourites", True),
        )

        state_conn = self._get_state_db()
        try:
            cached = self._load_cached_state(state_conn)

            entries: List[Dict[str, Any]] = []
            removals: List[Dict[str, Any]] = []

            # New + changed entries
            for key, cur in current.items():
                prev = cached.get(key)

                entry: Dict[str, Any] = {
                    "store": cur["store"],
                    "app_id": cur["app_id"],
                    "title": cur["title"],
                    "tags": cur["tags"],
                }

                if import_favs:
                    entry["favorite"] = cur.get("is_favorite", False)
                if cur.get("is_hidden"):
                    entry["hidden"] = True

                if cur.get("playtime_hours", 0) > 0:
                    entry["playtime_hours"] = round(cur["playtime_hours"], 2)
                if cur.get("last_played"):
                    entry["last_played"] = cur["last_played"]

                if prev is None:
                    # New entry — always include if it has data
                    if entry.get("tags") or entry.get("favorite") or entry.get("hidden") or entry.get("playtime_hours"):
                        entries.append(entry)
                else:
                    # Changed entry — include if anything changed
                    changed = (
                        set(cur.get("tags", [])) != set(prev.get("tags", []))
                        or cur.get("is_favorite", False) != prev.get("is_favorite", False)
                        or cur.get("is_hidden", False) != prev.get("is_hidden", False)
                        or cur.get("playtime_hours", 0) != prev.get("playtime_hours", 0)
                    )
                    if changed:
                        # Include removed tags info for delta processing
                        removed_tags = list(set(prev.get("tags", [])) - set(cur.get("tags", [])))
                        if removed_tags:
                            entry["removed_tags"] = removed_tags
                        # Track unfavorite
                        if import_favs and prev.get("is_favorite") and not cur.get("is_favorite"):
                            entry["unfavorite"] = True
                        # Track unhide
                        if prev.get("is_hidden") and not cur.get("is_hidden"):
                            entry["unhidden"] = True
                        entries.append(entry)

            # Removed entries (in cache but not in current pga.db)
            for key, prev in cached.items():
                if key not in current:
                    removal: Dict[str, Any] = {
                        "store": prev["store"],
                        "app_id": prev["app_id"],
                        "title": prev.get("title", ""),
                        "tags": prev.get("tags", []),
                    }
                    if import_favs and prev.get("is_favorite"):
                        removal["unfavorite"] = True
                    if prev.get("is_hidden"):
                        removal["unhidden"] = True
                    removals.append(removal)

            # Update cached state
            self._save_state(state_conn, current)

            return entries, removals
        finally:
            state_conn.close()

    def _read_sync_data(
        self, db_path: Path, **kwargs
    ) -> Dict[str, Dict[str, Any]]:
        """Read pga.db and build sync entries.

        Args:
            db_path: Path to Lutris pga.db

        Returns:
            Dict keyed by "store:app_id" → entry dict with raw state
        """
        entries: Dict[str, Dict[str, Any]] = {}

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # 1) Build game_id → game info mapping (only games with service IDs)
            games = self._load_games_with_services(conn)

            # 2) Load categories and their game assignments
            cat_map, game_cats = self._load_categories(conn)

            # 3) Build entries from game data + categories
            for game_id, game_info in games.items():
                key = f"{game_info['store']}:{game_info['app_id']}"

                tags = []
                is_favorite = False
                is_hidden = False

                for cat_id in game_cats.get(game_id, []):
                    cat_name = cat_map.get(cat_id, "")
                    if not cat_name:
                        continue
                    if cat_name == "favorite":
                        is_favorite = True
                    elif cat_name == ".hidden":
                        is_hidden = True
                    elif cat_name not in _RESERVED_CATEGORIES and not cat_name.startswith("."):
                        tags.append(cat_name)

                entry: Dict[str, Any] = {
                    "store": game_info["store"],
                    "app_id": game_info["app_id"],
                    "title": game_info["name"],
                    "tags": tags,
                    "is_favorite": is_favorite,
                    "is_hidden": is_hidden,
                }

                # Playtime
                playtime_hours = game_info.get("playtime")
                if playtime_hours and playtime_hours > 0:
                    entry["playtime_hours"] = round(playtime_hours, 2)

                lastplayed = game_info.get("lastplayed")
                if lastplayed and lastplayed > 0:
                    try:
                        dt = datetime.fromtimestamp(lastplayed, tz=timezone.utc)
                        entry["last_played"] = dt.strftime("%Y-%m-%d")
                    except (OSError, ValueError):
                        pass

                entries[key] = entry

        finally:
            conn.close()

        return entries

    def _load_games_with_services(
        self, conn: sqlite3.Connection
    ) -> Dict[int, Dict[str, Any]]:
        """Load games that have a known service mapping.

        Returns:
            Dict of game_id → {store, app_id, name, playtime, lastplayed}
        """
        games: Dict[int, Dict[str, Any]] = {}

        cursor = conn.execute(
            "SELECT id, name, service, service_id, playtime, lastplayed "
            "FROM games WHERE service IS NOT NULL AND service_id IS NOT NULL"
        )
        for row in cursor:
            service = row["service"]
            store = _SERVICE_MAP.get(service)
            if not store:
                continue

            service_id = row["service_id"]
            if not service_id:
                continue

            games[row["id"]] = {
                "store": store,
                "app_id": str(service_id),
                "name": row["name"] or "",
                "playtime": row["playtime"],
                "lastplayed": row["lastplayed"],
            }

        return games

    def _load_categories(
        self, conn: sqlite3.Connection
    ) -> tuple:
        """Load categories and game-category assignments.

        Returns:
            (cat_id → cat_name, game_id → [cat_ids])
        """
        # Category id → name
        cat_map: Dict[int, str] = {}
        try:
            cursor = conn.execute("SELECT id, name FROM categories")
            for row in cursor:
                cat_map[row["id"]] = row["name"]
        except sqlite3.OperationalError:
            # categories table might not exist
            return {}, {}

        # Game id → list of category ids
        game_cats: Dict[int, List[int]] = {}
        try:
            cursor = conn.execute("SELECT game_id, category_id FROM games_categories")
            for row in cursor:
                gid = row["game_id"]
                if gid not in game_cats:
                    game_cats[gid] = []
                game_cats[gid].append(row["category_id"])
        except sqlite3.OperationalError:
            pass

        return cat_map, game_cats

    # ── Stub enrichment methods (not used by this plugin) ─────────────

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        return None

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        return []

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        return None

    def get_database_path(self) -> Path:
        return self.data_dir / "lutris.db"
