# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""DOSBox Platform Provider

Handles running DOS games using DOSBox-Staging or vanilla DOSBox.
Uses multi-signal confidence-scored game detection and config priority
management. Binary detection via sdk.app_finder.

Supported DOSBox variants (pre-release):
- DOSBox-Staging (recommended): Modern fork with improved features
- DOSBox (vanilla): Original DOSBox

Game detection:
- GOG is_using_dosbox flag (highest confidence)
- IGDB DOS platform data
- PCGamingWiki engine data
- Tags, genres, metadata, title heuristics
"""

import logging
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from luducat.plugins.base import AbstractPlatformProvider
from luducat.plugins.sdk.app_finder import find_application

if TYPE_CHECKING:
    from luducat.plugins.base import Game

logger = logging.getLogger(__name__)


class DOSBoxProvider(AbstractPlatformProvider):
    """Platform provider for DOSBox emulation.

    Detects system-installed DOSBox variants via app_finder and uses
    multi-signal confidence scoring for game detection.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._platform_query = None
        self._config_manager = None

    @property
    def provider_name(self) -> str:
        return "dosbox"

    @property
    def display_name(self) -> str:
        return "DOSBox"

    @property
    def platform_type(self) -> str:
        return "dosbox"

    # ------------------------------------------------------------------ #
    # Binary detection
    # ------------------------------------------------------------------ #

    def detect_platforms(self) -> List[Dict[str, Any]]:
        """Detect installed DOSBox variants via app_finder."""
        runtimes = []
        seen_paths = set()

        # Custom path (highest priority)
        custom_path = self.get_setting("custom_path")
        if custom_path:
            path = Path(custom_path).expanduser()
            if path.exists() and path.is_file():
                version = self._get_dosbox_version(path)
                runtimes.append({
                    "platform_id": "dosbox/custom",
                    "name": "DOSBox (Custom)",
                    "version": version or "unknown",
                    "executable_path": str(path),
                    "is_default": True,
                    "is_managed": False,
                    "metadata": {"variant": "custom"},
                })
                seen_paths.add(str(path.resolve()))

        # DOSBox-Staging via app_finder (priority 1)
        staging_results = find_application(
            ["dosbox-staging"],
            flatpak_ids=["io.github.dosbox-staging"],
        )
        for r in staging_results:
            self._add_app_result(
                r, "dosbox-staging", "DOSBox-Staging",
                runtimes, seen_paths,
            )

        # Vanilla DOSBox (priority 2, skip duplicates)
        vanilla_results = find_application(
            ["dosbox"],
            flatpak_ids=["com.dosbox.DOSBox"],
        )
        for r in vanilla_results:
            self._add_app_result(
                r, "dosbox", "DOSBox",
                runtimes, seen_paths,
            )

        # Set default: prefer staging unless setting says otherwise
        prefer_staging = self.get_setting("prefer_staging", True)
        if runtimes and not any(r["is_default"] for r in runtimes):
            if prefer_staging:
                # Find first staging variant
                for r in runtimes:
                    if r.get("metadata", {}).get("variant") == "dosbox-staging":
                        r["is_default"] = True
                        break
            if not any(r["is_default"] for r in runtimes):
                runtimes[0]["is_default"] = True

        logger.info("Detected %d DOSBox installations", len(runtimes))
        return runtimes

    def _add_app_result(
        self, result, variant: str, display_name: str,
        runtimes: list, seen_paths: set,
    ) -> None:
        """Convert AppSearchResult to platform info dict, deduplicating."""
        if result.path:
            resolved = str(result.path.resolve())
            if resolved in seen_paths:
                return
            seen_paths.add(resolved)

        version = result.version
        if not version and result.path:
            version = self._get_dosbox_version(result.path)

        platform_id = f"dosbox/{variant}"
        if result.install_type == "flatpak":
            platform_id = f"dosbox/{variant}-flatpak"

        runtimes.append({
            "platform_id": platform_id,
            "name": f"{display_name} ({result.name_hint})"
                    if result.name_hint else display_name,
            "version": version or "unknown",
            "executable_path": str(result.path) if result.path else None,
            "is_default": False,
            "is_managed": False,
            "metadata": {
                "variant": variant,
                "install_type": result.install_type,
                "flatpak_id": result.flatpak_id,
            },
        })

    def _get_dosbox_version(self, exe_path: Path) -> Optional[str]:
        """Get DOSBox version from executable."""
        try:
            result = subprocess.run(
                [str(exe_path), "--version"],
                capture_output=True, text=True, timeout=5,
            )
            output = result.stdout + result.stderr
            for line in output.split("\n"):
                if "version" in line.lower():
                    match = re.search(r"(\d+\.\d+[\.\d]*)", line)
                    if match:
                        return match.group(1)
        except Exception:
            pass
        return None

    def _find_exe_in_dir(self, directory: Path) -> Optional[Path]:
        """Find DOSBox executable in a directory."""
        if not directory.exists():
            return None

        system = platform.system()
        exe_names = (
            ["dosbox-staging.exe", "dosbox.exe"]
            if system == "Windows"
            else ["dosbox-staging", "dosbox"]
        )
        for exe_name in exe_names:
            for candidate in (directory / exe_name, directory / "bin" / exe_name):
                if candidate.exists():
                    return candidate
        return None

    # ------------------------------------------------------------------ #
    # Game detection
    # ------------------------------------------------------------------ #

    def can_run_game(self, game: "Game") -> bool:
        """Check if this provider can run the given game."""
        result = self.get_detection_result(game)
        return result is not None and result.score >= 50

    def get_detection_result(self, game: "Game"):
        """Get detection result with confidence score.

        Returns:
            PlatformCandidate or None
        """
        from .game_detector import detect_dosbox_game
        return detect_dosbox_game(
            game, platform_query=self._get_platform_query()
        )

    def _get_platform_query(self):
        """Lazy-init PlatformDataQuery for cross-plugin DB access."""
        if self._platform_query is None:
            from luducat.plugins.platforms.shared.platform_query import (
                PlatformDataQuery,
            )
            # data_dir is plugins-data/{plugin_name}/, go up to plugins-data/
            self._platform_query = PlatformDataQuery(self.data_dir.parent)
        return self._platform_query

    # ------------------------------------------------------------------ #
    # Launch configuration
    # ------------------------------------------------------------------ #

    def create_launch_config(
        self,
        game: "Game",
        platform_info: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """Create launch configuration for a game.

        Pipeline:
        1. Resolve variant binary from platform_info
        2. Detect store-provided config (dosboxGOG.conf, etc.)
        3. Resolve effective config: per-game > store > global > default
        4. Build command: dosbox -conf <config> [game.exe]
        """
        executable = platform_info.get("executable_path")
        if not executable:
            raise ValueError("No DOSBox executable path in platform info")

        game_id = str(getattr(game, "id",
                               getattr(game, "store_app_id", "unknown")))
        game_path = kwargs.get("game_path")
        fullscreen = kwargs.get("fullscreen", False)
        config_file = kwargs.get("config_file")
        variant = platform_info.get("metadata", {}).get("variant", "dosbox")

        arguments = []

        # Resolve config
        if config_file:
            arguments.extend(["-conf", str(config_file)])
        else:
            mgr = self._get_config_manager()
            config_path = mgr.resolve_config(
                game_id, variant,
                game_path=Path(game_path) if game_path else None,
            )
            arguments.extend(["-conf", str(config_path)])

        if fullscreen:
            arguments.append("-fullscreen")

        # Add game path or executable
        if game_path:
            path = Path(game_path)
            if path.is_dir():
                for exe_name in [
                    "GAME.EXE", "GAME.COM", "PLAY.EXE", "START.EXE",
                ]:
                    exe_file = path / exe_name
                    if exe_file.exists():
                        arguments.append(str(exe_file))
                        break
                else:
                    arguments.extend(["-c", f'MOUNT C "{path}"'])
                    arguments.extend(["-c", "C:"])
            else:
                arguments.append(str(path))

        return {
            "launch_method": "executable",
            "executable": executable,
            "arguments": arguments,
            "working_directory": str(game_path) if game_path else None,
            "game_id": game_id,
            "platform_id": platform_info.get("platform_id", "dosbox/unknown"),
        }

    def generate_config(
        self, game: "Game", game_path: Path, **kwargs
    ) -> Path:
        """Generate DOSBox configuration file for a game."""
        from .config_manager import generate_default_config

        config_dir = self.cache_dir / "configs"
        config_dir.mkdir(parents=True, exist_ok=True)

        game_id = str(getattr(game, "id",
                               getattr(game, "store_app_id", "unknown")))
        config_path = config_dir / f"{game_id}.conf"
        config_path.write_text(generate_default_config(game_path=game_path))

        logger.debug("Generated DOSBox config at %s", config_path)
        return config_path

    def _get_config_manager(self):
        """Lazy-init DOSBoxConfigManager."""
        if self._config_manager is None:
            from .config_manager import DOSBoxConfigManager
            self._config_manager = DOSBoxConfigManager(
                self.config_dir, self.cache_dir
            )
        return self._config_manager

    # ------------------------------------------------------------------ #
    # Per-game settings
    # ------------------------------------------------------------------ #

    def get_game_settings_schema(self, game: "Game") -> Dict[str, Any]:
        """Get JSON schema for per-game DOSBox settings."""
        return {
            "type": "object",
            "properties": {
                "cycles": {
                    "type": "string",
                    "title": "CPU Cycles",
                    "description": "CPU speed setting (auto, max, or fixed number)",
                    "default": "auto",
                },
                "machine": {
                    "type": "string",
                    "title": "Machine Type",
                    "description": "Emulated machine type",
                    "enum": ["svga_s3", "vgaonly", "cga", "tandy", "hercules"],
                    "default": "svga_s3",
                },
                "fullscreen": {
                    "type": "boolean",
                    "title": "Start Fullscreen",
                    "default": False,
                },
                "custom_conf": {
                    "type": "string",
                    "title": "Custom Config",
                    "description": "Path to custom dosbox.conf file",
                },
            },
        }
