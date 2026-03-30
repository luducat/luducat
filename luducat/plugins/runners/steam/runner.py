# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Steam Client Runner

Detection-aware runner using app_finder for cross-platform Steam binary
detection. Primary launch method is URL_SCHEME (steam:// via Qt desktop
services). RuntimeManager handles fallback to binary execution when the
URI handler is not registered.

Supports:
- Linux: system binary, Flatpak, Snap (via PATH)
- Windows: registry, known install paths
- macOS: .app bundle
"""

import logging
from pathlib import Path
from typing import List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)

logger = logging.getLogger(__name__)


class SteamRunner(AbstractRunnerPlugin):
    """Runner plugin for Steam Client.

    Uses centralized app_finder for launcher detection. URL_SCHEME is the
    primary launch method (portable, Valve's documented API). Binary
    fallback is handled by RuntimeManager when the URI handler is broken.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    @property
    def runner_name(self) -> str:
        return "steam"

    @property
    def display_name(self) -> str:
        return "Steam Client"

    @property
    def supported_stores(self) -> List[str]:
        return ["steam"]

    def get_launcher_priority(self) -> int:
        return 300

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect Steam installation using centralized app_finder."""
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        from luducat.plugins.sdk.app_finder import (
            find_application,
            find_url_handler,
        )

        # Custom path takes priority
        custom_path = self.get_setting("steam_path")
        extra_dirs = []
        if custom_path:
            p = Path(custom_path).expanduser()
            if p.exists():
                extra_dirs.append(p if p.is_dir() else p.parent)

        results = find_application(
            ["steam"],
            extra_search_dirs=extra_dirs or None,
            flatpak_ids=["com.valvesoftware.Steam"],
            include_url_handler=True,
        )

        if not results:
            logger.info("Steam client not found")
            return None

        r = results[0]

        url_handler = find_url_handler("steam")
        has_url_handler = bool(url_handler)

        self._launcher_info = RunnerLauncherInfo(
            runner_name="steam",
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
            url_scheme="steam://" if has_url_handler else None,
            flatpak_id=r.flatpak_id,
            process_name="steam",
            capabilities={
                "url_handler_registered": has_url_handler,
            },
        )

        logger.info(
            "Steam detected: %s (%s), URL handler: %s",
            r.install_type,
            r.path or r.flatpak_id,
            has_url_handler,
        )
        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build steam:// URL scheme launch intent.

        Always returns URL_SCHEME — RuntimeManager handles fallback to
        binary execution if the URI handler is not registered.
        """
        if store_name != "steam":
            return None

        info = self.detect_launcher()
        if not info:
            return None

        return LaunchIntent(
            method=LaunchMethod.URL_SCHEME,
            runner_name="steam",
            store_name="steam",
            app_id=app_id,
            url=f"steam://rungameid/{app_id}",
        )

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        if store_name != "steam":
            return None
        return f"steam://install/{app_id}"

    def clear_cache(self) -> None:
        """Force re-detection on next call."""
        self._launcher_info = None
        self._detection_done = False
