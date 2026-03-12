# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# platform_query.py

"""Cross-plugin read-only DB queries for platform detection.

Accesses other plugin DBs (GOG, IGDB, PCGamingWiki) to gather platform
signals. All queries are read-only raw SQL — no plugin model imports.
Results are cached in memory for the session.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PlatformDataQuery:
    """Read-only cross-plugin queries for platform detection.

    Args:
        data_dir: Base plugins-data directory
            (e.g. ~/.local/share/luducat/plugins-data/)
    """

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._cache: Dict[str, object] = {}
        self._connections: Dict[str, sqlite3.Connection] = {}

    def is_gog_dosbox_game(self, gog_app_id: str) -> Optional[bool]:
        """Check if a GOG game uses DOSBox.

        Args:
            gog_app_id: GOG product ID

        Returns:
            True/False if known, None if not found or DB missing
        """
        cache_key = f"gog_dosbox:{gog_app_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = None
        conn = self._get_connection("gog", "catalog.db")
        if conn:
            try:
                row = conn.execute(
                    "SELECT is_using_dosbox FROM gog_games WHERE gogid = ?",
                    (gog_app_id,),
                ).fetchone()
                if row and row[0] is not None:
                    result = bool(row[0])
            except sqlite3.Error as e:
                logger.debug("GOG dosbox query failed: %s", e)

        self._cache[cache_key] = result
        return result

    def get_igdb_platform_ids(
        self, store_name: str, store_app_id: str
    ) -> List[int]:
        """Get IGDB platform IDs for a game via store match.

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_app_id: Store-specific game ID

        Returns:
            List of IGDB platform IDs (e.g. 13 = DOS, 6 = PC)
        """
        cache_key = f"igdb_platforms:{store_name}:{store_app_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        result: List[int] = []
        conn = self._get_connection("igdb", "igdb.db")
        if conn:
            try:
                row = conn.execute(
                    "SELECT igdb_id FROM igdb_store_matches "
                    "WHERE store_name = ? AND store_app_id = ?",
                    (store_name, store_app_id),
                ).fetchone()
                if row:
                    igdb_id = row[0]
                    rows = conn.execute(
                        "SELECT platform_id FROM game_platforms "
                        "WHERE game_id = ?",
                        (igdb_id,),
                    ).fetchall()
                    result = [r[0] for r in rows]
            except sqlite3.Error as e:
                logger.debug("IGDB platform query failed: %s", e)

        self._cache[cache_key] = result
        return result

    def get_pcgw_engines(
        self, store_name: str, store_app_id: str
    ) -> List[str]:
        """Get PCGamingWiki engine names for a game.

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_app_id: Store-specific game ID

        Returns:
            List of engine name strings (e.g. ["DOSBox"], ["ScummVM"])
        """
        cache_key = f"pcgw_engines:{store_name}:{store_app_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        result: List[str] = []
        conn = self._get_connection("pcgamingwiki", "pcgamingwiki.db")
        if conn:
            try:
                row = conn.execute(
                    "SELECT g.engines FROM pcgw_store_matches sm "
                    "JOIN pcgw_games g ON sm.pcgw_page_id = g.page_id "
                    "WHERE sm.store_name = ? AND sm.store_app_id = ? "
                    "AND sm.pcgw_page_id IS NOT NULL",
                    (store_name, store_app_id),
                ).fetchone()
                if row and row[0]:
                    result = [
                        e.strip() for e in row[0].split(",") if e.strip()
                    ]
            except sqlite3.Error as e:
                logger.debug("PCGW engine query failed: %s", e)

        self._cache[cache_key] = result
        return result

    def close(self) -> None:
        """Close all database connections."""
        for conn in self._connections.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._connections.clear()
        self._cache.clear()

    def _get_connection(
        self, plugin_name: str, db_filename: str
    ) -> Optional[sqlite3.Connection]:
        """Get or create a read-only connection to a plugin database.

        Returns None if the database file doesn't exist.
        """
        key = f"{plugin_name}/{db_filename}"
        if key in self._connections:
            return self._connections[key]

        db_path = self._data_dir / plugin_name / db_filename
        if not db_path.is_file():
            return None

        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True
            )
            conn.execute("PRAGMA query_only = ON")
            self._connections[key] = conn
            return conn
        except sqlite3.Error as e:
            logger.debug("Failed to open %s: %s", db_path, e)
            return None
