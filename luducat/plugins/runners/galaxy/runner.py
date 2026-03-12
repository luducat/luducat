# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""GOG Galaxy Runner

Launches GOG games via GOG Galaxy client on Windows. Uses the Galaxy 2.0
database for launch parameters and installation status. On non-Windows
platforms, falls back to the ``goggalaxy://`` URL scheme (opens game page).

Galaxy DB access is privacy-gated (requires local_data_consent).

Extracted from gog/launcher.py Galaxy methods + DB queries.
"""

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)
from luducat.plugins.sdk.app_finder import find_application

logger = logging.getLogger(__name__)


class GalaxyRunner(AbstractRunnerPlugin):
    """Runner plugin for GOG Galaxy.

    Windows-only for full functionality (DB-based launch).
    Non-Windows: URL scheme opens game page (not full launch).
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    @property
    def runner_name(self) -> str:
        return "galaxy"

    @property
    def display_name(self) -> str:
        return "GOG Galaxy"

    @property
    def supported_stores(self) -> List[str]:
        return ["gog"]

    def get_launcher_priority(self) -> int:
        return 150

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect GOG Galaxy installation (Windows only)."""
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        if sys.platform != "win32":
            logger.debug("Galaxy Runner: Windows-only, skipping on %s", sys.platform)
            return None

        extra_dirs: List[Path] = []

        # User-configured path takes priority
        configured = self.get_setting("galaxy_path", "")
        if configured:
            p = Path(configured)
            if p.exists():
                extra_dirs.append(p if p.is_dir() else p.parent)

        # GOG-specific registry: install directory
        registry_dir = self._get_galaxy_registry_dir()
        if registry_dir:
            extra_dirs.append(registry_dir)

        # Default install locations
        for env_var, fallback in [
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            ("ProgramFiles", r"C:\Program Files"),
        ]:
            default_dir = Path(os.environ.get(env_var, fallback)) / "GOG Galaxy"
            if default_dir.is_dir():
                extra_dirs.append(default_dir)

        results = find_application(
            ["GalaxyClient"],
            extra_search_dirs=extra_dirs or None,
        )

        if not results:
            logger.info("GOG Galaxy not found")
            return None

        r = results[0]

        self._launcher_info = RunnerLauncherInfo(
            runner_name="galaxy",
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
            url_scheme="goggalaxy://",
            capabilities={
                "db_available": self._get_galaxy_db_path() is not None,
                "stores": ["gog"],
            },
        )

        logger.info("GOG Galaxy detected: %s (%s)", r.install_type, r.path)
        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build launch intent for a GOG game via Galaxy."""
        if store_name != "gog":
            return None

        info = self.detect_launcher()
        if not info:
            return None

        # On Windows with DB access: use GalaxyClient.exe /command=runGame
        if sys.platform == "win32" and info.path and self.has_local_data_consent():
            params = self._get_galaxy_launch_params(app_id)
            if params and params.get("executable_path"):
                return LaunchIntent(
                    method=LaunchMethod.EXECUTABLE,
                    runner_name="galaxy",
                    store_name="gog",
                    app_id=app_id,
                    executable=info.path,
                    arguments=[
                        "/command=runGame",
                        f"/gameId={app_id}",
                        f'/path="{params["executable_path"]}"',
                    ],
                )

        # Fallback: URL scheme (opens game page, not direct launch)
        return LaunchIntent(
            method=LaunchMethod.URL_SCHEME,
            runner_name="galaxy",
            store_name="gog",
            app_id=app_id,
            url=f"goggalaxy://openGameView/{app_id}",
        )

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        if store_name != "gog":
            return None
        return f"goggalaxy://installationScreen/{app_id}"

    def can_launch_game(self, store_name: str, app_id: str) -> bool:
        """Check if Galaxy can launch a specific game.

        On Windows with DB access, checks if game is installed in Galaxy.
        """
        if store_name != "gog":
            return False

        info = self.detect_launcher()
        if not info:
            return False

        # Windows with DB: check if game is actually installed
        if sys.platform == "win32" and self.has_local_data_consent():
            params = self._get_galaxy_launch_params(app_id)
            return params is not None

        # Non-Windows or no consent: URL scheme always works
        return True

    def get_installed_games(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Query Galaxy DB for all installed GOG games.

        Privacy-gated via local_data_consent.

        Returns:
            Dict mapping gogid -> {"installed": True, "install_path": str|None}
        """
        if not self.has_local_data_consent():
            return None

        db_path = self._get_galaxy_db_path()
        if not db_path:
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT pt.gameReleaseKey, ptlp.executablePath
                FROM PlayTasks pt
                LEFT JOIN PlayTaskLaunchParameters ptlp ON ptlp.playTaskId = pt.id
                WHERE pt.isPrimary = 1
                  AND pt.gameReleaseKey LIKE 'gog_%'
            """)

            result = {}
            for row in cursor.fetchall():
                release_key = row[0]
                executable_path = row[1]

                if release_key and release_key.startswith("gog_"):
                    gogid = release_key[4:]
                    install_dir = None
                    if executable_path:
                        exe_path = Path(executable_path)
                        install_dir = str(exe_path.parent) if exe_path.parent != exe_path else None

                    result[gogid] = {
                        "installed": True,
                        "install_path": install_dir,
                    }

            conn.close()
            logger.info("GOG Galaxy: found %d installed games", len(result))
            return result if result else None

        except sqlite3.Error as e:
            logger.warning("Failed to batch-query Galaxy database: %s", e)
            return None

    def get_playtime_data(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Query Galaxy DB for playtime data (Windows only).

        Privacy-gated via local_data_consent.

        Returns:
            Dict mapping gogid -> {"minutes": int, "last_played": str|None}
        """
        if not self.has_local_data_consent():
            return None

        db_path = self._get_galaxy_db_path()
        if not db_path:
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            # Validate schema
            cursor.execute("PRAGMA table_info(GameTimes)")
            columns = {row[1] for row in cursor.fetchall()}
            if not {"releaseKey", "minutesInGame"}.issubset(columns):
                logger.warning("Galaxy DB: GameTimes table has unexpected schema")
                conn.close()
                return None

            cursor.execute("""
                SELECT releaseKey, minutesInGame FROM GameTimes
                WHERE releaseKey LIKE 'gog_%' AND minutesInGame > 0
            """)

            result: Dict[str, Dict[str, Any]] = {}
            for row in cursor.fetchall():
                gogid = row[0][4:]  # Strip "gog_" prefix
                result[gogid] = {"minutes": row[1], "last_played": None}

            # Merge LastPlayedDates
            cursor.execute("PRAGMA table_info(LastPlayedDates)")
            lpd_columns = {row[1] for row in cursor.fetchall()}
            if {"gameReleaseKey", "lastPlayedDate"}.issubset(lpd_columns):
                cursor.execute("""
                    SELECT gameReleaseKey, lastPlayedDate FROM LastPlayedDates
                    WHERE gameReleaseKey LIKE 'gog_%'
                """)
                for row in cursor.fetchall():
                    gogid = row[0][4:]
                    last_played = row[1]
                    if gogid in result:
                        result[gogid]["last_played"] = last_played
                    elif last_played:
                        result[gogid] = {"minutes": 0, "last_played": last_played}

            conn.close()
            logger.info("GOG Galaxy: found playtime for %d games", len(result))
            return result if result else None

        except sqlite3.Error as e:
            logger.warning("Failed to query Galaxy database for playtime: %s", e)
            return None

    def clear_cache(self) -> None:
        """Force re-detection on next call."""
        self._launcher_info = None
        self._detection_done = False

    # === PRIVATE HELPERS ===

    def _get_galaxy_registry_dir(self) -> Optional[Path]:
        """Get GOG Galaxy install directory from GOG-specific registry keys."""
        try:
            import winreg

            subkeys = [
                r"SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths",
                r"SOFTWARE\GOG.com\GalaxyClient\paths",
            ]

            for subkey in subkeys:
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(hive, subkey) as key:
                            client_path, _ = winreg.QueryValueEx(key, "client")
                            p = Path(client_path)
                            if p.is_dir():
                                return p
                    except OSError:
                        continue
        except ImportError:
            pass
        return None

    def _get_galaxy_db_path(self) -> Optional[Path]:
        """Get path to GOG Galaxy's local database (Windows only)."""
        if sys.platform != "win32":
            return None

        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        db_path = Path(program_data) / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db"

        if db_path.exists():
            return db_path
        return None

    def _get_galaxy_launch_params(self, gogid: str) -> Optional[Dict[str, str]]:
        """Query Galaxy DB for game launch parameters."""
        db_path = self._get_galaxy_db_path()
        if not db_path:
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.cursor()

            game_key = f"gog_{gogid}"
            cursor.execute("""
                SELECT id FROM PlayTasks
                WHERE gameReleaseKey = ? AND isPrimary = 1
            """, (game_key,))

            row = cursor.fetchone()
            if not row:
                conn.close()
                return None

            task_id = row[0]

            cursor.execute("""
                SELECT executablePath FROM PlayTaskLaunchParameters
                WHERE playTaskId = ?
            """, (task_id,))

            row = cursor.fetchone()
            conn.close()

            if row and row[0]:
                return {"executable_path": row[0]}
            return None

        except sqlite3.Error as e:
            logger.warning("Failed to query Galaxy database: %s", e)
            return None
