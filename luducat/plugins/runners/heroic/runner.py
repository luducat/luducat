# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Heroic Games Launcher Runner

Unifies GOG and Epic game launching through Heroic. Supports:
- Linux: system binary, AppImage, Flatpak
- Windows: installed binary, registry detection
- macOS: /Applications bundle

Detection logic consolidated from gog/launcher.py and epic/launcher_detector.py.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.base import (
    AbstractRunnerPlugin,
    RunnerLauncherInfo,
    LaunchIntent,
    LaunchMethod,
)

logger = logging.getLogger(__name__)


class HeroicRunner(AbstractRunnerPlugin):
    """Runner plugin for Heroic Games Launcher.

    Heroic is a cross-platform open-source launcher for GOG and Epic games.
    It handles Wine/Proton management on Linux, making it the primary runner
    for non-Steam games on Linux.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    @property
    def runner_name(self) -> str:
        return "heroic"

    @property
    def display_name(self) -> str:
        return "Heroic Games Launcher"

    @property
    def supported_stores(self) -> List[str]:
        return ["gog", "epic"]

    def get_launcher_priority(self) -> int:
        return 200

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect Heroic installation using centralized app_finder."""
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        from luducat.plugins.sdk.app_finder import (
            find_application,
            find_url_handler,
        )

        # Custom path takes top priority
        custom_path = self.get_setting("heroic_path")
        extra_dirs = []
        if custom_path:
            p = Path(custom_path).expanduser()
            if p.exists():
                extra_dirs.append(p if p.is_dir() else p.parent)

        results = find_application(
            ["heroic"],
            extra_search_dirs=extra_dirs or None,
            flatpak_ids=["com.heroicgameslauncher.hgl"],
            include_url_handler=True,
        )

        if not results:
            logger.info("Heroic Games Launcher not found")
            return None

        r = results[0]

        # Check URL handler registration
        url_handler = find_url_handler("heroic")
        has_url_handler = bool(url_handler)

        self._launcher_info = RunnerLauncherInfo(
            runner_name="heroic",
            path=r.path,
            install_type=r.install_type,
            virtualized=r.virtualized,
            url_scheme="heroic://" if has_url_handler else None,
            flatpak_id=r.flatpak_id,
            capabilities={
                "url_handler_registered": has_url_handler,
                "stores": ["gog", "epic"],
            },
        )

        logger.info(
            "Heroic detected: %s (%s), URL handler: %s",
            r.install_type,
            r.path or r.flatpak_id,
            has_url_handler,
        )
        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build launch intent for a GOG or Epic game via Heroic."""
        if store_name not in self.supported_stores:
            return None

        info = self.detect_launcher()
        if not info:
            return None

        url = f"heroic://launch/{store_name}/{app_id}"

        # Prefer URL scheme when handler is registered
        if info.capabilities.get("url_handler_registered"):
            return LaunchIntent(
                method=LaunchMethod.URL_SCHEME,
                runner_name="heroic",
                store_name=store_name,
                app_id=app_id,
                url=url,
            )

        # Flatpak: use flatpak run with the URL
        if info.install_type == "flatpak" and info.flatpak_id:
            return LaunchIntent(
                method=LaunchMethod.EXECUTABLE,
                runner_name="heroic",
                store_name=store_name,
                app_id=app_id,
                executable=Path("/usr/bin/flatpak"),
                arguments=["run", info.flatpak_id, url],
            )

        # Direct binary with URL argument
        if info.path:
            return LaunchIntent(
                method=LaunchMethod.EXECUTABLE,
                runner_name="heroic",
                store_name=store_name,
                app_id=app_id,
                executable=info.path,
                arguments=[url],
            )

        logger.warning("Heroic detected but no usable launch method")
        return None

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        if store_name not in self.supported_stores:
            return None
        return f"heroic://launch/{store_name}/{app_id}"

    def get_install_methods(self):
        """Return available installation methods for Heroic.

        Legacy method kept for backwards compatibility.
        """
        from luducat.plugins.sdk.app_finder import find_application

        methods = [{"value": "automatic", "label": _("Automatic"), "available": True}]

        results = find_application(
            ["heroic"],
            flatpak_ids=["com.heroicgameslauncher.hgl"],
        )
        for r in results:
            label = r.install_type.capitalize()
            if r.path:
                label += f" ({r.path})"
            elif r.flatpak_id:
                label += f" ({r.flatpak_id})"
            methods.append({
                "value": r.install_type,
                "label": label,
                "available": True,
            })

        methods.append({"value": "manual", "label": _("Manual"), "available": True})
        return methods

    def get_heroic_sources(self) -> List[Dict[str, str]]:
        """Return detected Heroic installations as source entries.

        Format matches Epic bridge's get_legendary_sources() for UI consistency.
        """
        from luducat.plugins.sdk.app_finder import find_application

        sources: List[Dict[str, str]] = []

        results = find_application(
            ["heroic"],
            flatpak_ids=["com.heroicgameslauncher.hgl"],
        )
        for r in results:
            source_type = r.install_type  # system, appimage, flatpak, etc.
            path = str(r.path) if r.path else ""
            if not path and r.flatpak_id:
                path = r.flatpak_id
            sources.append({
                "source": source_type,
                "path": path,
                "version": r.version or "",
            })

        return sources

    def get_heroic_sources_with_custom(self) -> List[Dict[str, str]]:
        """Get sources list for the config UI dropdown, including Custom entry."""
        sources = self.get_heroic_sources()
        custom_path = self.get_setting("heroic_path", "")
        sources.append({
            "source": "custom",
            "path": custom_path,
            "version": "",
        })
        return sources

    def clear_cache(self) -> None:
        """Force re-detection on next call."""
        self._launcher_info = None
        self._detection_done = False
