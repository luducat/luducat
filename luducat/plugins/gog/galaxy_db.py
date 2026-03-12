# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# galaxy_db.py

"""GOG Galaxy 2.0 Database Access

Reads playtime and installation data from the GOG Galaxy 2.0 local database
(Windows only). These are pure DB readers with no launcher dependencies.

The Galaxy 2.0 DB lives at:
  %ProgramData%/GOG.com/Galaxy/storage/galaxy-2.0.db
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_galaxy_db_path() -> Optional[Path]:
    """Get path to GOG Galaxy's local database (Windows only)."""
    if sys.platform != "win32":
        return None

    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    db_path = Path(program_data) / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db"

    if db_path.exists():
        return db_path
    return None


def get_playtime_data() -> Optional[Dict[str, Dict[str, Any]]]:
    """Query GOG Galaxy database for playtime data (Windows only).

    Reads GameTimes (minutes) and LastPlayedDates (last played timestamp)
    from the Galaxy 2.0 database. Only returns data for GOG games
    (releaseKey LIKE 'gog_%').

    Returns:
        Dict mapping gogid -> {"minutes": int, "last_played": str|None}
        or None if Galaxy DB not available.
    """
    db_path = get_galaxy_db_path()
    if not db_path:
        return None

    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Validate GameTimes table exists
        cursor.execute("PRAGMA table_info(GameTimes)")
        columns = {row[1] for row in cursor.fetchall()}
        if not {"releaseKey", "minutesInGame"}.issubset(columns):
            logger.warning("Galaxy DB: GameTimes table has unexpected schema")
            conn.close()
            return None

        # Query playtime — only GOG games
        cursor.execute("""
            SELECT releaseKey, minutesInGame FROM GameTimes
            WHERE releaseKey LIKE 'gog_%' AND minutesInGame > 0
        """)

        result: Dict[str, Dict[str, Any]] = {}
        for row in cursor.fetchall():
            release_key = row[0]
            minutes = row[1]
            gogid = release_key[4:]  # Strip "gog_" prefix
            result[gogid] = {"minutes": minutes, "last_played": None}

        # Merge LastPlayedDates (sparse — not all games have entries)
        cursor.execute("PRAGMA table_info(LastPlayedDates)")
        lpd_columns = {row[1] for row in cursor.fetchall()}
        if {"gameReleaseKey", "lastPlayedDate"}.issubset(lpd_columns):
            cursor.execute("""
                SELECT gameReleaseKey, lastPlayedDate FROM LastPlayedDates
                WHERE gameReleaseKey LIKE 'gog_%'
            """)
            for row in cursor.fetchall():
                gogid = row[0][4:]  # Strip "gog_" prefix
                last_played = row[1]
                if gogid in result:
                    result[gogid]["last_played"] = last_played
                elif last_played:
                    # Game has last_played but no playtime — include with 0 minutes
                    result[gogid] = {"minutes": 0, "last_played": last_played}

        conn.close()
        logger.info(f"GOG Galaxy: found playtime for {len(result)} games")
        return result if result else None

    except sqlite3.Error as e:
        logger.warning(f"Failed to query Galaxy database for playtime: {e}")
        return None


def get_installed_games_batch() -> Optional[Dict[str, Dict[str, Any]]]:
    """Query GOG Galaxy database for all installed GOG games (Windows only).

    Single SQL query against Galaxy DB, returning all installed GOG games
    with their executable paths.

    Returns:
        Dict mapping gogid -> {"installed": True, "install_path": str|None}
        or None if Galaxy DB not available (Linux, or Galaxy not installed).
    """
    db_path = get_galaxy_db_path()
    if not db_path:
        return None

    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Query all installed GOG games via PlayTasks + PlayTaskLaunchParameters
        cursor.execute("""
            SELECT pt.gameReleaseKey, ptlp.executablePath
            FROM PlayTasks pt
            LEFT JOIN PlayTaskLaunchParameters ptlp ON ptlp.playTaskId = pt.id
            WHERE pt.isPrimary = 1
              AND pt.gameReleaseKey LIKE 'gog_%'
        """)

        result = {}
        for row in cursor.fetchall():
            release_key = row[0]  # e.g. "gog_1234567890"
            executable_path = row[1]

            # Extract GOG ID from release key
            if release_key and release_key.startswith("gog_"):
                gogid = release_key[4:]  # Strip "gog_" prefix
                # Derive install directory from executable path
                install_dir = None
                if executable_path:
                    exe_path = Path(executable_path)
                    # The install directory is typically the parent of the executable
                    install_dir = str(exe_path.parent) if exe_path.parent != exe_path else None

                result[gogid] = {
                    "installed": True,
                    "install_path": install_dir,
                }

        conn.close()
        logger.info(f"GOG Galaxy: found {len(result)} installed games")
        return result if result else None

    except sqlite3.Error as e:
        logger.warning(f"Failed to batch-query Galaxy database: {e}")
        return None
