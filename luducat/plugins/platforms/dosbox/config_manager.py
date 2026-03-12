# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config_manager.py

"""DOSBox configuration priority management.

Whole-file priority system — no INI section merging (pre-release scope).

Config resolution order:
1. Per-game user config (highest priority)
2. Store-detected config (dosboxGOG*.conf, etc.)
3. Global user config (per variant)
4. Generated default config (lowest priority)
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Patterns for store-provided DOSBox config files (case-insensitive)
_STORE_CONFIG_PATTERNS = [
    "dosboxgog*.conf",
    "dosbox_*.conf",
    "dosbox*.conf",
]


class DOSBoxConfigManager:
    """Manages DOSBox configuration files with priority resolution.

    File locations:
        Global:    {config_dir}/global/{variant}.conf
        Per-game:  {config_dir}/games/{game_id}.conf
        Effective: {cache_dir}/configs/{game_id}.conf
    """

    def __init__(self, config_dir: Path, cache_dir: Path):
        self._config_dir = config_dir
        self._cache_dir = cache_dir

    def get_global_config(self, variant: str) -> str:
        """Get global config text for a variant.

        Returns empty string if no global config exists.
        """
        path = self._global_path(variant)
        if path.is_file():
            return path.read_text(errors="replace")
        return ""

    def set_global_config(self, variant: str, config_text: str) -> None:
        """Save global config for a variant."""
        path = self._global_path(variant)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config_text)
        logger.debug("Saved global DOSBox config: %s", path)

    def get_game_config(self, game_id: str) -> Optional[str]:
        """Get per-game config text.

        Returns None if no per-game config exists.
        """
        path = self._game_path(game_id)
        if path.is_file():
            return path.read_text(errors="replace")
        return None

    def set_game_config(self, game_id: str, config_text: str) -> None:
        """Save per-game config."""
        path = self._game_path(game_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config_text)
        logger.debug("Saved per-game DOSBox config: %s", path)

    def detect_store_config(self, game_path: Path) -> Optional[Path]:
        """Find store-provided DOSBox config in a game directory.

        Looks for: dosboxGOG*.conf, dosbox_*.conf, dosbox*.conf

        Args:
            game_path: Path to game installation directory

        Returns:
            Path to first matching config file, or None
        """
        if not game_path.is_dir():
            return None

        for pattern in _STORE_CONFIG_PATTERNS:
            # Case-insensitive glob
            matches = list(game_path.glob(pattern))
            if not matches:
                # Try uppercase
                matches = list(game_path.glob(pattern.upper()))
            if matches:
                # Return the first match sorted for determinism
                matches.sort(key=lambda p: p.name.lower())
                return matches[0]

        return None

    def resolve_config(
        self,
        game_id: str,
        variant: str,
        game_path: Optional[Path] = None,
    ) -> Path:
        """Resolve the effective config file for a game.

        Priority: per-game > store-detected > global > generated default.
        Writes effective config to cache_dir/configs/{game_id}.conf.

        Returns:
            Path to the effective config file
        """
        config_text = None

        # 1. Per-game user config
        game_config = self.get_game_config(game_id)
        if game_config:
            config_text = game_config

        # 2. Store-detected config
        if config_text is None and game_path:
            store_conf = self.detect_store_config(game_path)
            if store_conf:
                config_text = store_conf.read_text(errors="replace")

        # 3. Global user config
        if config_text is None:
            global_config = self.get_global_config(variant)
            if global_config:
                config_text = global_config

        # 4. Generated default
        if config_text is None:
            config_text = generate_default_config(game_path=game_path)

        # Write effective config
        effective_dir = self._cache_dir / "configs"
        effective_dir.mkdir(parents=True, exist_ok=True)
        effective_path = effective_dir / f"{game_id}.conf"
        effective_path.write_text(config_text)

        return effective_path

    def _global_path(self, variant: str) -> Path:
        return self._config_dir / "global" / f"{variant}.conf"

    def _game_path(self, game_id: str) -> Path:
        return self._config_dir / "games" / f"{game_id}.conf"


def generate_default_config(
    game_path: Optional[Path] = None,
    machine: str = "svga_s3",
    cycles: str = "auto",
) -> str:
    """Generate a default DOSBox configuration.

    Args:
        game_path: Optional game directory to mount
        machine: DOSBox machine type
        cycles: CPU cycles setting

    Returns:
        Config file content as string
    """
    mount_line = ""
    if game_path:
        mount_line = f'mount c "{game_path}"\nc:'

    return f"""\
[sdl]
fullscreen=false
output=opengl

[dosbox]
machine={machine}

[cpu]
core=auto
cycles={cycles}

[mixer]
rate=48000

[autoexec]
@echo off
{mount_line}
"""
