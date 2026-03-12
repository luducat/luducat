"""PixelEngine -- Demo platform plugin for the luducat Plugin SDK.

This plugin demonstrates a platform provider for a fictional retro game engine.
Follows the DOSBox/ScummVM pattern: detect installed engines, check game
compatibility, build launch configurations.

Key concepts demonstrated:
- All required AbstractPlatformProvider methods
- Platform detection across multiple installation paths
- Game compatibility checking via tags and metadata
- Launch configuration with arguments and environment
- Per-game and global settings schemas
- Custom executable path from settings
"""

import logging
from pathlib import Path
from typing import Any, Dict, List

from luducat.plugins.base import AbstractPlatformProvider, Game

logger = logging.getLogger(__name__)


class PixelEnginePlatform(AbstractPlatformProvider):
    """PixelEngine platform provider.

    Demonstrates a platform plugin that provides a fictional retro game
    engine capable of running pixel-art games.
    """

    # ── Required Properties ──────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "pixelengine"

    @property
    def display_name(self) -> str:
        return "PixelEngine"

    @property
    def platform_type(self) -> str:
        return "pixelengine"

    # ── Required Methods ─────────────────────────────────────────────

    def detect_platforms(self) -> List[Dict[str, Any]]:
        """Detect installed PixelEngine versions.

        In a real plugin, this would scan common installation paths,
        check PATH, and read version info from executables.
        """
        platforms = []

        # Check common installation paths
        candidates = [
            ("/usr/bin/pixelengine", "PixelEngine"),
            ("/usr/local/bin/pixelengine", "PixelEngine"),
            ("/snap/bin/pixelengine", "PixelEngine (Snap)"),
        ]

        for exe_path, name in candidates:
            path = Path(exe_path)
            if path.exists():
                version = self._detect_version(path)
                platforms.append({
                    "platform_id": f"pixelengine/{version}",
                    "name": f"{name} {version}",
                    "version": version,
                    "executable_path": str(path),
                    "is_default": len(platforms) == 0,
                    "is_managed": False,
                })

        # Check user-configured custom path
        custom_path = self.get_setting("custom_path")
        if custom_path:
            path = Path(custom_path)
            if path.exists():
                platforms.append({
                    "platform_id": "pixelengine/custom",
                    "name": "PixelEngine (Custom)",
                    "version": "unknown",
                    "executable_path": str(path),
                    "is_default": len(platforms) == 0,
                    "is_managed": False,
                })

        if not platforms:
            logger.debug("PixelEngine: no installations found")

        return platforms

    def can_run_game(self, game: Game) -> bool:
        """Check if this game is PixelEngine-compatible.

        In a real plugin, this might check:
        - Game tags/categories for engine indicators
        - Game metadata for platform/engine info
        - A known-compatible game list
        - File inspection (looking for engine-specific files)
        """
        # Check tags for pixel/retro indicators
        tags = [t.lower() for t in (game.tags or [])]
        genres = [g.lower() for g in (game.genres or [])]

        pixel_indicators = {"pixel", "retro", "8-bit", "16-bit", "pixel art"}
        if pixel_indicators & set(tags):
            return True
        if pixel_indicators & set(genres):
            return True

        # Check extra metadata
        extra = game.extra_metadata or {}
        if extra.get("engine") == "pixelengine":
            return True
        if extra.get("platform") in ("retro", "pixel"):
            return True

        return False

    def create_launch_config(
        self,
        game: Game,
        platform_info: Dict[str, Any],
        **kwargs,
    ) -> Dict[str, Any]:
        """Build launch configuration for a PixelEngine game.

        Constructs the command line and environment to run the game
        through the PixelEngine platform.
        """
        game_path = kwargs.get("game_path", "")
        if not game_path:
            return {
                "launch_method": "error",
                "error": "No game path provided",
            }

        # Build arguments
        args = [str(game_path)]

        # Apply global settings
        if self.get_setting("fullscreen", False):
            args.append("--fullscreen")

        scale = self.get_setting("scale_factor", "Auto")
        if scale != "Auto":
            args.extend(["--scale", scale.replace("x", "")])

        # Apply per-game settings from kwargs
        if kwargs.get("sound_driver"):
            args.extend(["--sound", kwargs["sound_driver"]])

        return {
            "launch_method": "executable",
            "executable": platform_info["executable_path"],
            "arguments": args,
            "working_directory": game_path,
            "environment": {
                "PIXELENGINE_HOME": str(self.data_dir),
            },
        }

    # ── Optional Methods ─────────────────────────────────────────────

    def get_platform_settings_schema(self) -> Dict[str, Any]:
        """Global platform settings schema."""
        return {
            "audio_backend": {
                "type": "choice",
                "label": "Audio Backend",
                "choices": ["auto", "pulseaudio", "alsa", "sdl"],
                "default": "auto",
            },
            "vsync": {
                "type": "boolean",
                "label": "V-Sync",
                "default": True,
            },
        }

    def get_game_settings_schema(self, game: Game) -> Dict[str, Any]:
        """Per-game settings schema."""
        return {
            "sound_driver": {
                "type": "choice",
                "label": "Sound Driver",
                "choices": ["auto", "opl2", "opl3", "midi"],
                "default": "auto",
            },
            "custom_config": {
                "type": "path",
                "label": "Custom Config File",
                "path_type": "file",
            },
        }

    # ── Internal Helpers ─────────────────────────────────────────────

    @staticmethod
    def _detect_version(exe_path: Path) -> str:
        """Detect version from executable.

        In a real plugin, this would run `pixelengine --version` and
        parse the output.
        """
        # Demo: return a hardcoded version
        return "2.1.0"

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_enable(self) -> None:
        platforms = self.detect_platforms()
        logger.info(
            "PixelEngine enabled: %d installation(s) found",
            len(platforms),
        )

    def close(self) -> None:
        logger.info("PixelEngine platform shutting down")
