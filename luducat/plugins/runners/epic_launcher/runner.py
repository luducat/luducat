# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner.py

"""Epic Games Runner

Entry point for launching Epic games. Routes launch requests to the
configured backend:
- Heroic (default, all platforms)
- Native Epic Games Launcher (Windows/macOS)
- Wine runner or Native runner (stubs, in development)

The runner itself only handles the native Epic Launcher URL scheme
directly. All other backends are rerouted to their respective runners.
"""

import logging
import os
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

# Backend name → runner plugin name mapping
_BACKEND_RUNNER_MAP = {
    "heroic": "heroic",
    "wine": "wine",
    "native": "native",
}


class EpicLauncherRunner(AbstractRunnerPlugin):
    """Runner plugin for Epic Games.

    Acts as the user-facing entry point for Epic game launching.
    Detects the native Epic Games Launcher on Windows/macOS and
    reroutes to other runners (Heroic, Wine, Native) as configured.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._launcher_info: Optional[RunnerLauncherInfo] = None
        self._detection_done = False

    @property
    def runner_name(self) -> str:
        return "epic_launcher"

    @property
    def display_name(self) -> str:
        return "Epic Games Bridge"

    @property
    def supported_stores(self) -> List[str]:
        return ["epic"]

    def get_launcher_priority(self) -> int:
        # Higher than Heroic (200) so this runner is always selected
        # first for Epic games, then reroutes as configured.
        return 300

    def detect_launcher(self) -> Optional[RunnerLauncherInfo]:
        """Detect launch capabilities.

        Always reports as available — the runner reroutes to other
        runners and only needs the native launcher for the
        "epic_launcher" backend on Windows/macOS.
        """
        if self._detection_done:
            return self._launcher_info

        self._detection_done = True

        # Check for native Epic Games Launcher (Windows/macOS)
        native = self._find_epic_native()
        if native:
            path, install_type = native
            self._launcher_info = RunnerLauncherInfo(
                runner_name="epic_launcher",
                path=path,
                install_type=install_type,
                virtualized=False,
                url_scheme="com.epicgames.launcher://",
                capabilities={"stores": ["epic"]},
            )
            logger.info("Epic runner: native launcher at %s", path)
        else:
            # No native launcher, but runner is still usable via rerouting
            self._launcher_info = RunnerLauncherInfo(
                runner_name="epic_launcher",
                path=None,
                install_type="reroute",
                virtualized=False,
                capabilities={"stores": ["epic"]},
            )
            logger.info("Epic runner: no native launcher, reroute only")

        return self._launcher_info

    def build_launch_intent(
        self, store_name: str, app_id: str
    ) -> Optional[LaunchIntent]:
        """Build launch intent for an Epic game.

        Reads the configured backend and either handles the launch
        directly (native Epic Launcher) or reroutes to another runner.
        """
        if store_name != "epic":
            return None

        info = self.detect_launcher()
        if not info:
            return None

        backend = self.get_setting("launch_backend", "heroic")

        # Native Epic Launcher: handle directly via URL scheme
        if backend == "epic_launcher":
            if not info.url_scheme:
                logger.warning(
                    "Epic runner: native launcher backend selected but "
                    "not available on this platform"
                )
                return None
            url = f"com.epicgames.launcher://apps/{app_id}?action=launch"
            return LaunchIntent(
                method=LaunchMethod.URL_SCHEME,
                runner_name="epic_launcher",
                store_name="epic",
                app_id=app_id,
                url=url,
            )

        # Reroute to another runner
        target_runner = _BACKEND_RUNNER_MAP.get(backend)
        if not target_runner:
            logger.error("Epic runner: unknown backend '%s'", backend)
            return None

        return LaunchIntent(
            method=LaunchMethod.REROUTE,
            runner_name="epic_launcher",
            store_name="epic",
            app_id=app_id,
            reroute_target=target_runner,
        )

    def build_install_url(self, store_name: str, app_id: str) -> Optional[str]:
        if store_name != "epic":
            return None
        info = self.detect_launcher()
        # Native launcher: use Epic URL scheme
        if info and info.url_scheme:
            return f"com.epicgames.launcher://apps/{app_id}?action=install"
        # Reroute: Heroic handles install prompts via its launch URL
        backend = self.get_setting("launch_backend", "heroic")
        if backend == "heroic":
            return f"heroic://launch/epic/{app_id}"
        return None

    # === INSTALLATION STATUS ===

    def get_installed_games(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Get games installed via native Epic Games Launcher (Windows/macOS).

        Reads Epic's manifest files from the standard data directory.
        """
        manifest_dir = self._get_epic_manifest_dir()
        if not manifest_dir or not manifest_dir.is_dir():
            return None

        result = self._read_epic_manifests(manifest_dir)
        return result if result else None

    def _get_epic_manifest_dir(self) -> Optional[Path]:
        """Get Epic Games Launcher manifest directory."""
        if sys.platform == "win32":
            programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            return (
                Path(programdata) / "Epic" / "EpicGamesLauncher"
                / "Data" / "Manifests"
            )
        elif sys.platform == "darwin":
            return (
                Path.home() / "Library" / "Application Support"
                / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
            )
        return None

    def _read_epic_manifests(
        self, manifest_dir: Path
    ) -> Dict[str, Dict[str, Any]]:
        """Read .item manifest files from Epic's data directory."""
        from luducat.plugins.sdk.json import json

        result: Dict[str, Dict[str, Any]] = {}

        try:
            for manifest_file in manifest_dir.glob("*.item"):
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    app_name = data.get("AppName")
                    if not app_name:
                        continue
                    if data.get("bIsIncompleteInstall", False):
                        continue
                    result[app_name] = {
                        "installed": True,
                        "install_path": data.get("InstallLocation"),
                    }
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(
                        "Failed to read manifest %s: %s", manifest_file.name, e
                    )
        except OSError as e:
            logger.warning("Failed to scan Epic manifests: %s", e)

        if result:
            logger.info(
                "Epic native manifests: %d installed games detected", len(result)
            )
        return result

    # === LIFECYCLE ===

    def clear_cache(self) -> None:
        """Force re-detection on next call."""
        self._launcher_info = None
        self._detection_done = False

    # === PRIVATE HELPERS ===

    def _find_epic_native(self) -> Optional[tuple]:
        """Find native Epic Games Launcher via app_finder."""
        results = find_application(["EpicGamesLauncher", "Epic Games Launcher"])
        if results:
            r = results[0]
            return r.path, r.install_type

        # Windows: Epic-specific registry for data path (URL scheme only)
        if sys.platform == "win32":
            data_dir = self._get_epic_data_dir()
            if data_dir:
                return data_dir, "registry"

        return None

    def _get_epic_data_dir(self) -> Optional[Path]:
        """Get Epic Games Launcher data directory from registry (Windows)."""
        try:
            import winreg

            registry_paths = [
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\WOW6432Node\Epic Games\EpicGamesLauncher"),
                (winreg.HKEY_LOCAL_MACHINE,
                 r"SOFTWARE\Epic Games\EpicGamesLauncher"),
                (winreg.HKEY_CURRENT_USER,
                 r"SOFTWARE\Epic Games\EpicGamesLauncher"),
            ]

            for hkey, subkey in registry_paths:
                try:
                    key = winreg.OpenKey(hkey, subkey)
                    install_path, _ = winreg.QueryValueEx(key, "AppDataPath")
                    winreg.CloseKey(key)
                    if install_path:
                        path = Path(install_path)
                        if path.exists():
                            return path
                except (FileNotFoundError, OSError):
                    continue

        except ImportError:
            pass

        return None
