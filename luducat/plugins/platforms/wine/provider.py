# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""Wine Platform Provider

Detects Wine/Proton installations and Wine prefixes on the system.
Launches Windows games through detected prefixes using Wine or Proton.

Prefix scanning is privacy-gated via local_data_consent. Launch pipeline:
detect prefix → find exe → resolve Wine binary → build command → execute.
"""

import logging
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from luducat.plugins.base import AbstractPlatformProvider

if TYPE_CHECKING:
    from luducat.plugins.base import Game

logger = logging.getLogger(__name__)


class WineProvider(AbstractPlatformProvider):
    """Wine/Proton platform provider.

    Detects Wine installations and prefixes. Launches games through
    existing Wine prefixes created by Heroic, Lutris, Bottles, Steam, etc.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._local_data_consent = False
        self._cached_prefixes: Optional[list] = None
        self._registry = None
        self._prefix_provider = None
        self._initialize_subplugins()

    def _initialize_subplugins(self) -> None:
        """Register all sub-plugins."""
        from .subplugins import SubPluginRegistry
        from .subplugins.external_prefix import ExternalPrefixProvider
        from .subplugins.stubs import (
            ManagedPrefixProvider,
            DXVKEnhancement,
            GamescopeOverlay,
            MangohudOverlay,
        )

        self._registry = SubPluginRegistry()
        self._prefix_provider = ExternalPrefixProvider(
            local_data_consent=self._local_data_consent
        )
        self._registry.register(self._prefix_provider)
        self._registry.register(ManagedPrefixProvider())
        self._registry.register(DXVKEnhancement())
        self._registry.register(GamescopeOverlay())
        self._registry.register(MangohudOverlay())

    def set_local_data_consent(self, consent: bool) -> None:
        """Set local data consent for prefix scanning."""
        self._local_data_consent = consent
        if self._prefix_provider:
            self._prefix_provider._consent = consent

    def has_local_data_consent(self) -> bool:
        return self._local_data_consent

    @property
    def provider_name(self) -> str:
        return "wine"

    @property
    def display_name(self) -> str:
        return "Wine/Proton"

    @property
    def platform_type(self) -> str:
        return "wine"

    def detect_platforms(self) -> List[Dict[str, Any]]:
        """Detect available Wine/Proton installations.

        Uses app_finder for Wine binary detection.

        Returns:
            List of platform info dicts.
        """
        from luducat.plugins.sdk.app_finder import find_wine_binary

        results = find_wine_binary()
        runtimes = []

        for i, r in enumerate(results):
            platform_id = f"wine/{r.name_hint.replace(' ', '_')}"
            runtimes.append({
                "platform_id": platform_id,
                "name": r.name_hint,
                "version": r.version or "",
                "executable_path": str(r.path) if r.path else None,
                "is_default": (i == 0),  # First found is default
                "is_managed": False,
                "capabilities": [],
                "metadata": {
                    "install_type": r.install_type,
                },
            })

        if runtimes:
            logger.info("Wine provider: detected %d Wine installation(s)", len(runtimes))
        else:
            logger.debug("Wine provider: no Wine installations found")

        return runtimes

    def can_run_game(self, game: "Game") -> bool:
        """Check if Wine can run this game via an existing prefix.

        Returns True if a matching prefix is found for any of the
        game's store identities. Linux-only.
        """
        if platform.system() != "Linux":
            return False

        if not self._local_data_consent:
            return False

        if not self._prefix_provider:
            return False

        # Try store_app_ids dict first (multi-store games)
        store_app_ids = getattr(game, "store_app_ids", None)
        if store_app_ids and isinstance(store_app_ids, dict):
            for store_name, app_id in store_app_ids.items():
                if self._prefix_provider.find_prefix_for_game(store_name, str(app_id)):
                    return True

        # Fall back to single store identity
        store_name = getattr(game, "store_name", None)
        app_id = getattr(game, "store_app_id", None)
        if store_name and app_id:
            if self._prefix_provider.find_prefix_for_game(store_name, str(app_id)):
                return True

        return False

    def create_launch_config(
        self,
        game: "Game",
        platform_info: Dict[str, Any],
        **kwargs
    ) -> Dict[str, Any]:
        """Create Wine launch configuration.

        Full pipeline: find prefix → detect exe → resolve Wine → build command.

        Kwargs:
            launch_config: Saved per-game config (may have wine_exe override)
            exe_selection_callback: Callable for ambiguous exe dialog
            save_launch_config: Callable to persist user's exe selection
        """
        from .exe_detector import detect_game_executables
        from .launch_builder import build_launch_command
        from .runner_resolver import RunnerResolver

        # Find matching prefix
        prefix = self._find_prefix_for_game(game)
        if not prefix:
            raise RuntimeError("No Wine prefix found for this game")

        game_title = getattr(game, "title", "Unknown")
        launch_config = kwargs.get("launch_config")

        # Detect game executables
        candidates = detect_game_executables(prefix, game_title, launch_config)
        if not candidates:
            raise RuntimeError(
                f"No game executables found in prefix: {prefix.prefix_path}"
            )

        # Select executable
        selected_exe = candidates[0]

        if selected_exe.score < 90:
            # Ambiguous — need user confirmation
            callback = kwargs.get("exe_selection_callback")
            if callback:
                result = callback(candidates, game_title, prefix)
                if result is None:
                    raise RuntimeError("User cancelled exe selection")
                selected_exe = result

                # Save selection if user chose to remember
                save_fn = kwargs.get("save_launch_config")
                if save_fn and getattr(result, "remember", False):
                    save_config = dict(launch_config or {})
                    save_config["wine_exe"] = str(result.path)
                    save_fn(save_config)

        # Read runtime settings from plugin config, merge per-game overrides
        settings = self._get_runtime_settings()
        if launch_config:
            for key in ("runtime_mode", "esync", "fsync", "dxvk", "mangohud",
                         "virtual_desktop", "virtual_desktop_resolution",
                         "gamemode", "winedebug"):
                lc_key = f"wine_{key}"
                if lc_key in launch_config:
                    settings[key] = launch_config[lc_key]
        runtime_mode = settings.get("runtime_mode", "auto")

        # Resolve Wine binary — use specific runtime if selected
        resolver = RunnerResolver()
        runner = None

        # Per-game runtime override, then global default_runtime
        runtime_id = None
        if launch_config:
            runtime_id = launch_config.get("wine_runtime")
        if not runtime_id:
            runtime_id = settings.get("default_runtime", "")

        if runtime_id:
            from .runtime_scanner import find_runtime_by_identifier
            selected_rt = find_runtime_by_identifier(runtime_id)
            if selected_rt:
                runner = resolver.resolve_specific(selected_rt)

        if not runner:
            user_wine = None
            if launch_config and launch_config.get("wine_binary"):
                user_wine = Path(launch_config["wine_binary"])
            elif settings.get("wine_binary"):
                user_wine = Path(settings["wine_binary"])

            user_proton_dir = None
            if settings.get("proton_directory"):
                user_proton_dir = Path(settings["proton_directory"])

            user_umu_bin = None
            if settings.get("umu_binary"):
                user_umu_bin = Path(settings["umu_binary"])

            runner = resolver.resolve(
                prefix,
                user_wine_binary=user_wine,
                runtime_mode=runtime_mode,
                user_proton_directory=user_proton_dir,
                user_umu_binary=user_umu_bin,
            )
        if not runner:
            raise RuntimeError(
                "No Wine or Proton binary found. "
                "Install Wine or a Proton version to launch this game."
            )

        # Build launch command
        extra_env = None
        if launch_config and launch_config.get("environment"):
            extra_env = launch_config["environment"]

        launch_cmd = build_launch_command(
            selected_exe.path, runner, prefix,
            extra_env=extra_env,
            runtime_settings=settings,
        )

        # Let sub-plugins contribute
        if self._registry:
            from .wine_env import WineEnv
            sub_env = WineEnv(inherit_system=False)
            self._registry.compose_env(sub_env, prefix=prefix)
            # Merge sub-plugin env (won't override existing keys)
            for key, value in sub_env.get_env().items():
                if key not in launch_cmd.environment:
                    launch_cmd.environment[key] = value

            launch_cmd.command = self._registry.compose_command(launch_cmd.command)

        return {
            "game_id": str(getattr(game, "id", "")),
            "platform_id": platform_info.get("platform_id", "wine/default"),
            "launch_method": "executable",
            "executable": launch_cmd.command[0] if launch_cmd.command else None,
            "arguments": launch_cmd.command[1:] if len(launch_cmd.command) > 1 else [],
            "working_directory": str(launch_cmd.working_directory) if launch_cmd.working_directory else None,
            "environment": launch_cmd.environment,
            "metadata": {
                "wine_prefix": str(prefix.prefix_path),
                "wine_source": runner.source,
                "is_proton": runner.is_proton,
            },
        }

    def _find_prefix_for_game(self, game: "Game"):
        """Find a matching prefix for a game across all store identities."""
        if not self._prefix_provider:
            return None

        # Try store_app_ids dict first
        store_app_ids = getattr(game, "store_app_ids", None)
        if store_app_ids and isinstance(store_app_ids, dict):
            for store_name, app_id in store_app_ids.items():
                prefix = self._prefix_provider.find_prefix_for_game(
                    store_name, str(app_id)
                )
                if prefix:
                    return prefix

        # Fall back to single store identity
        store_name = getattr(game, "store_name", None)
        app_id = getattr(game, "store_app_id", None)
        if store_name and app_id:
            return self._prefix_provider.find_prefix_for_game(
                store_name, str(app_id)
            )

        return None

    def _get_runtime_settings(self) -> Dict[str, Any]:
        """Read Wine runtime settings from plugin config.

        3-tier merge: global defaults → per-runtime overrides → (caller adds
        per-game on top). This method handles the first two tiers.
        """
        try:
            from luducat.plugins.sdk.config import get_config_value
            settings = {
                "runtime_mode": get_config_value("plugins.wine.runtime_mode", "auto"),
                "default_runtime": get_config_value("plugins.wine.default_runtime", ""),
                "wine_binary": get_config_value("plugins.wine.wine_binary", ""),
                "proton_directory": get_config_value("plugins.wine.proton_directory", ""),
                "umu_binary": get_config_value("plugins.wine.umu_binary", ""),
                "esync": get_config_value("plugins.wine.esync", True),
                "fsync": get_config_value("plugins.wine.fsync", True),
                "dxvk": get_config_value("plugins.wine.dxvk", False),
                "mangohud": get_config_value("plugins.wine.mangohud", False),
                "winedebug": get_config_value("plugins.wine.winedebug", "fixme-all"),
                "virtual_desktop": get_config_value("plugins.wine.virtual_desktop", False),
                "virtual_desktop_resolution": get_config_value("plugins.wine.virtual_desktop_resolution", "1920x1080"),
                "gamemode": get_config_value("plugins.wine.gamemode", False),
            }

            # Merge per-runtime overrides if a specific runtime is selected
            runtime_id = settings.get("default_runtime", "")
            if runtime_id:
                rt_prefix = f"plugins.wine.runtimes.{runtime_id}"
                for key in ("esync", "fsync", "dxvk", "mangohud", "gamemode",
                            "virtual_desktop", "virtual_desktop_resolution",
                            "winedebug"):
                    rt_val = get_config_value(f"{rt_prefix}.{key}", None)
                    if rt_val is not None:
                        settings[key] = rt_val

            return settings
        except Exception:
            return {}

    def scan_prefixes(self) -> list:
        """Scan for Wine prefixes using PrefixDetector.

        Privacy-gated via local_data_consent.

        Returns:
            List of WinePrefix objects.
        """
        if self._cached_prefixes is not None:
            return self._cached_prefixes

        from .prefix_detector import PrefixDetector

        detector = PrefixDetector(local_data_consent=self._local_data_consent)
        self._cached_prefixes = detector.scan_all()
        return self._cached_prefixes

    def clear_prefix_cache(self) -> None:
        """Force re-scan on next scan_prefixes() call."""
        self._cached_prefixes = None
        if self._prefix_provider:
            self._prefix_provider.clear_cache()
