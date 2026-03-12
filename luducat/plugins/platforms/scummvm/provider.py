# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""ScummVM Platform Provider

Handles running classic adventure games using ScummVM. Uses multi-signal
confidence-scored game detection with a hardcoded game ID seed for
reliable launch without --auto-detect fallback.

Game detection:
- ScummVM game ID seed (~100 entries with store app IDs)
- PCGamingWiki engine data
- Tags, metadata, title heuristics
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


class ScummVMProvider(AbstractPlatformProvider):
    """Platform provider for ScummVM.

    Detects system-installed ScummVM via app_finder and uses a hardcoded
    game ID seed for high-confidence game detection and launch.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._platform_query = None

    @property
    def provider_name(self) -> str:
        return "scummvm"

    @property
    def display_name(self) -> str:
        return "ScummVM"

    @property
    def platform_type(self) -> str:
        return "scummvm"

    # ------------------------------------------------------------------ #
    # Binary detection
    # ------------------------------------------------------------------ #

    def detect_platforms(self) -> List[Dict[str, Any]]:
        """Detect installed ScummVM versions via app_finder."""
        runtimes = []
        seen_paths = set()

        # Custom path (highest priority)
        custom_path = self.get_setting("custom_path")
        if custom_path:
            path = Path(custom_path).expanduser()
            if path.exists() and path.is_file():
                version = self._get_scummvm_version(path)
                runtimes.append({
                    "platform_id": "scummvm/custom",
                    "name": "ScummVM (Custom)",
                    "version": version or "unknown",
                    "executable_path": str(path),
                    "is_default": True,
                    "is_managed": False,
                    "metadata": {},
                })
                seen_paths.add(str(path.resolve()))

        # System ScummVM via app_finder
        results = find_application(
            ["scummvm"],
            flatpak_ids=["org.scummvm.ScummVM"],
        )
        for r in results:
            if r.path:
                resolved = str(r.path.resolve())
                if resolved in seen_paths:
                    continue
                seen_paths.add(resolved)

            version = r.version
            if not version and r.path:
                version = self._get_scummvm_version(r.path)

            platform_id = "scummvm/system"
            if r.install_type == "flatpak":
                platform_id = "scummvm/flatpak"

            runtimes.append({
                "platform_id": platform_id,
                "name": f"ScummVM ({r.name_hint})"
                        if r.name_hint else "ScummVM",
                "version": version or "unknown",
                "executable_path": str(r.path) if r.path else None,
                "is_default": not runtimes,
                "is_managed": False,
                "metadata": {
                    "install_type": r.install_type,
                    "flatpak_id": r.flatpak_id,
                },
            })

        # Ensure at least one default
        if runtimes and not any(r["is_default"] for r in runtimes):
            runtimes[0]["is_default"] = True

        logger.info("Detected %d ScummVM installations", len(runtimes))
        return runtimes

    def _get_scummvm_version(self, exe_path: Path) -> Optional[str]:
        """Get ScummVM version from executable."""
        try:
            result = subprocess.run(
                [str(exe_path), "--version"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split("\n"):
                if "ScummVM" in line:
                    match = re.search(r"ScummVM (\d+\.\d+[\.\d]*)", line)
                    if match:
                        return match.group(1)
        except Exception:
            pass
        return None

    def _find_exe_in_dir(self, directory: Path) -> Optional[Path]:
        """Find ScummVM executable in a directory."""
        if not directory.exists():
            return None

        system = platform.system()
        exe_name = "scummvm.exe" if system == "Windows" else "scummvm"

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
        from .game_detector import detect_scummvm_game
        return detect_scummvm_game(
            game, platform_query=self._get_platform_query()
        )

    def _get_platform_query(self):
        """Lazy-init PlatformDataQuery for cross-plugin DB access."""
        if self._platform_query is None:
            from luducat.plugins.platforms.shared.platform_query import (
                PlatformDataQuery,
            )
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

        ScummVM ID resolution priority:
        1. Per-game launch_config override
        2. Seed match by store_app_id
        3. Game metadata scummvm_id field
        4. Seed match by normalized title
        5. None (falls back to --auto-detect)
        """
        executable = platform_info.get("executable_path")
        if not executable:
            raise ValueError("No ScummVM executable path in platform info")

        game_path = kwargs.get("game_path")
        fullscreen = kwargs.get("fullscreen", False)
        language = kwargs.get("language")
        subtitles = kwargs.get("subtitles", True)

        arguments = []

        if fullscreen:
            arguments.append("--fullscreen")

        if language:
            arguments.extend(["--language", language])

        if subtitles:
            arguments.append("--subtitles")

        # Resolve ScummVM game ID
        scummvm_id = self._resolve_scummvm_id(game)

        if scummvm_id:
            arguments.append(scummvm_id)
        elif game_path:
            arguments.extend(["--auto-detect", "--path", str(game_path)])

        return {
            "launch_method": "executable",
            "executable": executable,
            "arguments": arguments,
            "working_directory": str(game_path) if game_path else None,
            "game_id": str(getattr(game, "id",
                                    getattr(game, "store_app_id", "unknown"))),
            "platform_id": platform_info.get("platform_id", "scummvm/unknown"),
        }

    def _resolve_scummvm_id(self, game: "Game") -> Optional[str]:
        """Resolve ScummVM game ID with priority chain.

        1. Per-game launch_config override
        2. Seed match by store_app_id
        3. Game metadata scummvm_id field
        4. Seed match by normalized title
        """
        from .game_detector import SCUMMVM_GAME_IDS

        # 1. Launch config override
        import json
        launch_config = getattr(game, "launch_config", "")
        if launch_config:
            try:
                config = json.loads(launch_config) if isinstance(launch_config, str) else launch_config
                if isinstance(config, dict) and config.get("scummvm_id"):
                    return config["scummvm_id"]
            except (json.JSONDecodeError, TypeError):
                pass

        # 2. Seed match by store_app_id
        store_app_ids = getattr(game, "store_app_ids", {}) or {}
        for store_name, store_app_id in store_app_ids.items():
            for seed_data in SCUMMVM_GAME_IDS.values():
                if seed_data.get(store_name) and str(seed_data[store_name]) == str(store_app_id):
                    return seed_data["id"]

        # 3. Metadata scummvm_id
        extra_metadata = getattr(game, "extra_metadata", {}) or {}
        if "scummvm_id" in extra_metadata:
            return extra_metadata["scummvm_id"]

        # 4. Seed match by normalized title
        title = getattr(game, "title", "")
        if isinstance(title, str) and title:
            title_lower = title.lower()
            for seed_title, seed_data in SCUMMVM_GAME_IDS.items():
                if seed_title in title_lower:
                    return seed_data["id"]

        return None

    def get_scummvm_game_id(self, game: "Game") -> Optional[str]:
        """Get ScummVM game ID for a game if known.

        Deprecated — use _resolve_scummvm_id() instead.
        """
        return self._resolve_scummvm_id(game)

    # ------------------------------------------------------------------ #
    # Per-game settings
    # ------------------------------------------------------------------ #

    def get_game_settings_schema(self, game: "Game") -> Dict[str, Any]:
        """Get JSON schema for per-game ScummVM settings."""
        return {
            "type": "object",
            "properties": {
                "scummvm_id": {
                    "type": "string",
                    "title": "ScummVM Game ID",
                    "description": "Override ScummVM game ID for detection",
                },
                "fullscreen": {
                    "type": "boolean",
                    "title": "Start Fullscreen",
                    "default": False,
                },
                "language": {
                    "type": "string",
                    "title": "Language",
                    "description": "Game language override",
                },
                "subtitles": {
                    "type": "boolean",
                    "title": "Show Subtitles",
                    "default": True,
                },
                "music_volume": {
                    "type": "integer",
                    "title": "Music Volume",
                    "minimum": 0,
                    "maximum": 256,
                    "default": 192,
                },
                "sfx_volume": {
                    "type": "integer",
                    "title": "Sound Effects Volume",
                    "minimum": 0,
                    "maximum": 256,
                    "default": 192,
                },
            },
        }
