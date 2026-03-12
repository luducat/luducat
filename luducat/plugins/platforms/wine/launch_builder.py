# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# launch_builder.py

"""Wine Launch Command Builder

Constructs the final command line and environment for launching a game
through Wine or Proton. Three command paths: umu-run + Proton, direct
Proton, direct Wine.
"""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .wine_env import WineEnv

logger = logging.getLogger(__name__)


@dataclass
class WineLaunchCommand:
    """Complete launch command ready for subprocess execution."""
    command: List[str]
    environment: Dict[str, str]
    working_directory: Optional[Path] = None


def build_launch_command(
    game_exe: Path,
    runner,
    prefix,
    extra_env: Optional[Dict[str, str]] = None,
    runtime_settings: Optional[Dict] = None,
) -> WineLaunchCommand:
    """Build the Wine/Proton launch command.

    Args:
        game_exe: Path to the game executable inside the prefix
        runner: ResolvedRunner from RunnerResolver
        prefix: WinePrefix with prefix_path, arch, environment
        extra_env: User-supplied environment overrides
        runtime_settings: Plugin settings (esync, fsync, dxvk, mangohud, winedebug)

    Returns:
        WineLaunchCommand ready for execution
    """
    settings = runtime_settings or {}
    env = WineEnv(inherit_system=True)

    # 1. WINEPREFIX and WINEARCH
    env.add("WINEPREFIX", str(prefix.prefix_path))
    env.add("WINEARCH", prefix.arch)

    # 2. WINEDEBUG — from settings or default
    winedebug = settings.get("winedebug", "fixme-all")
    if winedebug:
        env.add("WINEDEBUG", winedebug)

    # 3. DLL overrides — disable Wine menu builder
    env.add_dll_override("winemenubuilder.exe", "d")

    # 3b. DXVK DLL overrides
    if settings.get("dxvk"):
        env.add_dll_override("d3d11", "n")
        env.add_dll_override("dxgi", "n")

    # 4. Sync primitives (Wine only — Proton handles its own)
    if not runner.is_proton:
        if settings.get("esync", True):
            env.add("WINEESYNC", "1")
        if settings.get("fsync", True):
            env.add("WINEFSYNC", "1")

    # 5. Source launcher env passthrough
    if hasattr(prefix, "environment") and prefix.environment:
        env.add_bundle(prefix.environment)

    # 6. Proton-specific variables
    if runner.is_proton:
        if runner.proton_path:
            env.add("PROTONPATH", str(runner.proton_path))
            env.add("STEAM_COMPAT_DATA_PATH", str(prefix.prefix_path.parent))
            env.add("STEAM_COMPAT_CLIENT_INSTALL_PATH",
                     str(Path.home() / ".steam" / "steam"))

        # umu-run needs GAMEID and STORE
        if runner.umu_run:
            store = getattr(prefix, "store_name", "unknown")
            app_id = getattr(prefix, "store_app_id", "0")
            if store == "steam":
                env.add("GAMEID", app_id)
            else:
                env.add("GAMEID", f"umu-{app_id}")
            env.add("STORE", store)

    # 7. User overrides (last — highest priority)
    if extra_env:
        env.add_bundle(extra_env, override=True)

    # Gamemode wrapper (before mangohud)
    if settings.get("gamemode") and shutil.which("gamemoderun"):
        env.add_command_prefix("gamemoderun")

    # MangoHud wrapper
    if settings.get("mangohud"):
        env.add_command_prefix("mangohud")

    # Virtual Desktop (Wine mode only — Proton handles its own windowing)
    vd_enabled = settings.get("virtual_desktop")
    vd_resolution = settings.get("virtual_desktop_resolution", "1920x1080")

    # Build command
    command = _build_command(
        game_exe, runner, env,
        virtual_desktop=vd_enabled,
        virtual_desktop_resolution=vd_resolution,
    )

    # Working directory defaults to the exe's parent
    working_dir = game_exe.parent if game_exe.parent.is_dir() else None

    return WineLaunchCommand(
        command=env.get_command_prefix() + command,
        environment=env.get_env(),
        working_directory=working_dir,
    )


def _build_command(
    game_exe: Path,
    runner,
    env: WineEnv,
    virtual_desktop: bool = False,
    virtual_desktop_resolution: str = "1920x1080",
) -> List[str]:
    """Build the core command list based on runner type."""
    exe_str = str(game_exe)

    # Path 1: umu-run + Proton
    if runner.is_proton and runner.umu_run:
        return [str(runner.umu_run), exe_str]

    # Path 2: Direct Proton
    if runner.is_proton and runner.proton_path:
        proton_script = runner.proton_path / "proton"
        return [str(proton_script), "waitforexitandrun", exe_str]

    # Path 3: Direct Wine (with optional virtual desktop)
    wine_bin = str(runner.wine_binary)
    if virtual_desktop:
        return [
            wine_bin, "explorer",
            f"/desktop=luducat,{virtual_desktop_resolution}",
            exe_str,
        ]
    return [wine_bin, exe_str]
