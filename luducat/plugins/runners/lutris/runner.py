# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Lutris Runner

Launches games via Lutris using the lutris:rungame/{slug} URI scheme.
Resolves game slugs from Lutris pga.db using service + service_id matching.

Supports:
- Linux: system binary, Flatpak (net.lutris.Lutris)
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)

logger = logging.getLogger(__name__)

# Lutris service names → luducat store names (reverse lookup)
_STORE_TO_SERVICE = {
    "steam": "steam",
    "gog": "gog",
    "epic": "egs",
}


class LutrisRunner(AbstractRunnerPlugin):
    """Runner plugin for Lutris.

    Launches games via the lutris:rungame/{slug} URI scheme.
    Slug resolution uses Lutris pga.db (service + service_id matching).
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False
        self._slug_cache: Dict[str, str] = {}  # "store:app_id" → slug

    @property
    def runner_name(self) -> str:
        return "lutris"

    @property
    def display_name(self) -> str:
        return "Lutris"

    @property
    def supported_stores(self) -> List[str]:
        return ["steam", "gog", "epic"]

    def get_launcher_priority(self) -> int:
        return 150

    # ── Detection ─────────────────────────────────────────────────────

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect Lutris installation using centralized app_finder."""
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        from luducat.plugins.sdk.app_finder import (
            find_application,
            find_url_handler,
        )

        results = find_application(
            ["lutris"],
            flatpak_ids=["net.lutris.Lutris"],
            include_url_handler=True,
        )

        if not results:
            logger.info("Lutris not found")
            return None

        r = results[0]

        url_handler = find_url_handler("lutris")
        has_url_handler = bool(url_handler)

        self._launcher_info = RunnerLauncherInfo(
            runner_name="lutris",
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
            url_scheme="lutris://" if has_url_handler else None,
            flatpak_id=r.flatpak_id,
            capabilities={
                "url_handler_registered": has_url_handler,
                "stores": list(_STORE_TO_SERVICE.keys()),
            },
        )

        logger.info(
            "Lutris detected: %s (%s), URL handler: %s",
            r.install_type,
            r.path or r.flatpak_id,
            has_url_handler,
        )
        return self._launcher_info

    # ── Launch ────────────────────────────────────────────────────────

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build launch intent for a game via Lutris."""
        if store_name not in self.supported_stores:
            return None

        info = self.detect_launcher()
        if not info:
            return None

        slug = self._resolve_slug(store_name, app_id)
        if not slug:
            logger.debug(
                "No Lutris slug found for %s:%s", store_name, app_id
            )
            return None

        url = f"lutris:rungame/{slug}"

        # Prefer URL scheme when handler is registered
        if info.capabilities.get("url_handler_registered"):
            return LaunchIntent(
                method=LaunchMethod.URL_SCHEME,
                runner_name="lutris",
                store_name=store_name,
                app_id=app_id,
                url=url,
            )

        # Flatpak: use flatpak run with the URL
        if info.install_type == "flatpak" and info.flatpak_id:
            return LaunchIntent(
                method=LaunchMethod.EXECUTABLE,
                runner_name="lutris",
                store_name=store_name,
                app_id=app_id,
                executable=Path("/usr/bin/flatpak"),
                arguments=["run", info.flatpak_id, url],
            )

        # Direct binary with URL argument
        if info.path:
            return LaunchIntent(
                method=LaunchMethod.EXECUTABLE,
                runner_name="lutris",
                store_name=store_name,
                app_id=app_id,
                executable=info.path,
                arguments=[url],
            )

        logger.warning("Lutris detected but no usable launch method")
        return None

    def build_install_url(
        self, store_name: str, app_id: str
    ) -> Optional[str]:
        if store_name not in self.supported_stores:
            return None
        slug = self._resolve_slug(store_name, app_id)
        if not slug:
            return None
        return f"lutris:install/{slug}"

    # ── Slug resolution ───────────────────────────────────────────────

    def _resolve_slug(self, store_name: str, app_id: str) -> Optional[str]:
        """Resolve a luducat store game to a Lutris slug via pga.db.

        Checks the games table using service + service_id matching.
        Results are cached in memory.
        """
        cache_key = f"{store_name}:{app_id}"
        if cache_key in self._slug_cache:
            return self._slug_cache[cache_key]

        service = _STORE_TO_SERVICE.get(store_name)
        if not service:
            return None

        slug = self._query_slug(service, app_id)
        if slug:
            self._slug_cache[cache_key] = slug
        return slug

    def _query_slug(self, service: str, service_id: str) -> Optional[str]:
        """Query Lutris pga.db for a game slug."""
        db_path = self._find_pga_db()
        if not db_path:
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cursor = conn.execute(
                    "SELECT slug FROM games "
                    "WHERE service = ? AND service_id = ? LIMIT 1",
                    (service, service_id),
                )
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.debug(f"Lutris slug lookup failed: {e}")
            return None

    @staticmethod
    def _find_pga_db() -> Optional[Path]:
        """Locate Lutris pga.db."""
        candidates = [
            Path.home() / ".local" / "share" / "lutris" / "pga.db",
            Path.home() / ".cache" / "lutris" / "pga.db",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def clear_cache(self) -> None:
        """Force re-detection on next call."""
        self._launcher_info = None
        self._detection_done = False
        self._slug_cache.clear()
