# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runtime_manager.py

"""Runtime Manager - Coordinates platform providers and runner plugins

The RuntimeManager is responsible for:
1. Discovering and registering platform providers (DOSBox, ScummVM, Wine)
2. Discovering and detecting runner plugins (Heroic, Steam, Galaxy)
3. Detecting available platforms and launchers
4. Assigning platforms/runners to games
5. Orchestrating game launches through a three-tier selection:
   a) Explicit assignment (per-game config)
   b) Compatible runner (highest priority for game's store)
   c) Compatible platform (DOSBox, ScummVM, Wine prefix)

Architecture:
    RuntimeManager (coordinator)
    ├── PlatformProviders (plugins/platforms/)
    │   ├── DOSBoxProvider
    │   ├── ScummVMProvider
    │   └── WineProvider
    └── RunnerPlugins (plugins/runners/)
        ├── HeroicRunner (GOG, Epic)
        ├── SteamRunner (Steam)
        ├── GalaxyRunner (GOG, Windows)
        ├── EpicLauncherRunner (Epic, Windows/macOS)
        └── NativeRunner (manual assignment)

Usage:
    manager = RuntimeManager()
    manager.set_config(config)
    manager.set_plugin_manager(plugin_manager)
    await manager.initialize()

    # Launch a game (three-tier auto-selection)
    result = await manager.launch_game(game)
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .runtime_base import (
    PlatformType,
    PlatformInfo,
    PlatformProviderBase,
    RunnerLauncherInfo,
    LaunchConfig,
    LaunchIntent,
    LaunchResult,
    LaunchMethod,
)

if TYPE_CHECKING:
    from luducat.core.config import Config
    from luducat.core.database import Game
    from luducat.core.plugin_manager import PluginManager
    from luducat.plugins.base import AbstractRunnerPlugin

logger = logging.getLogger(__name__)


class RuntimeManager:
    """Coordinates platform providers for game execution

    Manages the lifecycle of platform providers, platform detection,
    game-platform assignment, and launch orchestration.

    The manager discovers platform providers from the plugin system
    and provides a unified interface for launching games.
    """

    def __init__(self):
        """Initialize runtime manager"""
        self._config: Optional["Config"] = None
        self._plugin_manager: Optional["PluginManager"] = None
        self._database = None
        self._game_service = None

        # Registered providers by type
        self._providers: Dict[PlatformType, List[PlatformProviderBase]] = {}

        # Cached platform info
        self._platforms: Dict[str, PlatformInfo] = {}  # platform_id -> PlatformInfo
        self._default_platforms: Dict[PlatformType, str] = {}  # type -> platform_id

        # Runner plugins
        self._runners: Dict[str, "AbstractRunnerPlugin"] = {}  # runner_name -> plugin
        self._available_runners: Dict[str, RunnerLauncherInfo] = {}  # runner_name -> info

        # Game-platform/runner assignments (game_id -> platform_id or "runner/<name>")
        self._assignments: Dict[str, str] = {}

        self._initialized = False

    def set_config(self, config: "Config") -> None:
        """Set configuration manager

        Args:
            config: Config instance
        """
        self._config = config
        logger.debug("RuntimeManager: config set")

    def set_plugin_manager(self, plugin_manager: "PluginManager") -> None:
        """Set plugin manager for provider discovery

        Args:
            plugin_manager: PluginManager instance
        """
        self._plugin_manager = plugin_manager
        logger.debug("RuntimeManager: plugin_manager set")

    def set_database(self, database) -> None:
        """Set database for persistence

        Args:
            database: Database instance
        """
        self._database = database
        logger.debug("RuntimeManager: database set")

    def set_game_service(self, game_service) -> None:
        """Set game service for per-game launch config lookup.

        Args:
            game_service: GameService instance
        """
        self._game_service = game_service
        logger.debug("RuntimeManager: game_service set")

    async def initialize(self) -> None:
        """Initialize runtime manager

        Discovers providers/runners and detects available platforms/launchers.
        Call after setting config and plugin_manager.
        """
        if self._initialized:
            return

        logger.info("Initializing RuntimeManager")

        # Discover platform providers from plugins
        self._discover_providers()

        # Discover runner plugins
        self._discover_runners()

        # Detect available platforms
        await self._detect_platforms()

        # Detect available launchers (runners)
        self._detect_launchers()

        # Load saved assignments from database
        self._load_assignments()

        self._initialized = True
        logger.info(
            "RuntimeManager initialized: %d provider types, "
            "%d platforms, %d runners (%d available)",
            len(self._providers), len(self._platforms),
            len(self._runners), len(self._available_runners),
        )

    def _discover_providers(self) -> None:
        """Discover platform providers from plugins"""
        if not self._plugin_manager:
            logger.warning("No plugin manager, skipping provider discovery")
            return

        # Get platform provider plugins
        # Platform providers are a special plugin type
        try:
            from luducat.plugins.base import PluginType
            platform_plugins = self._plugin_manager.get_plugins_by_type(PluginType.PLATFORM)

            for plugin_name, plugin in platform_plugins.items():
                provider = plugin.get_platform_provider()
                if provider:
                    self.register_provider(provider)
                    logger.debug(f"Registered platform provider: {provider.provider_name}")

        except Exception as e:
            logger.error(f"Failed to discover platform providers: {e}")

    def _discover_runners(self) -> None:
        """Discover runner plugins from the plugin manager."""
        if not self._plugin_manager:
            logger.warning("No plugin manager, skipping runner discovery")
            return

        try:
            from luducat.plugins.base import PluginType
            runner_plugins = self._plugin_manager.get_plugins_by_type(PluginType.RUNNER)

            for plugin_name, runner in runner_plugins.items():
                runner_name = getattr(runner, 'runner_name', plugin_name)
                self._runners[runner_name] = runner
                logger.debug("Registered runner plugin: %s", runner_name)

            logger.info("Discovered %d runner plugin(s)", len(self._runners))

        except Exception as e:
            logger.error("Failed to discover runner plugins: %s", e)

    def _detect_launchers(self) -> None:
        """Call detect_launcher() on each runner and cache results."""
        self._available_runners.clear()

        for runner_name, runner in self._runners.items():
            try:
                info = runner.detect_launcher()
                if info is not None:
                    self._available_runners[runner_name] = info
                    logger.debug("Runner available: %s (%s)", runner_name, info.install_type)
                else:
                    logger.debug("Runner not available: %s", runner_name)
            except Exception as e:
                logger.error("Runner %s detection failed: %s", runner_name, e)

        logger.info(
            "Runner detection: %d/%d available",
            len(self._available_runners), len(self._runners),
        )

    def _select_runner(
        self,
        store_name: str,
        app_id: str,
    ) -> Optional["AbstractRunnerPlugin"]:
        """Select the best runner plugin for a game.

        Filters by: supported_stores, availability, can_launch_game.
        Sorts by get_launcher_priority() descending.

        Args:
            store_name: Store plugin name (e.g. "steam", "gog", "epic")
            app_id: Store-specific app ID

        Returns:
            Best matching runner plugin, or None
        """
        candidates = []

        for runner_name, runner in self._runners.items():
            # Must be detected/available
            if runner_name not in self._available_runners:
                continue

            # Must support this store
            if store_name not in runner.supported_stores:
                continue

            # Deeper validation
            try:
                if not runner.can_launch_game(store_name, app_id):
                    continue
            except Exception:
                continue

            priority = runner.get_launcher_priority()
            candidates.append((priority, runner_name, runner))

        if not candidates:
            return None

        # Sort by priority descending, then name for stability
        candidates.sort(key=lambda x: (-x[0], x[1]))
        selected = candidates[0]
        logger.debug(
            "Selected runner for %s/%s: %s (priority %d)",
            store_name, app_id, selected[1], selected[0],
        )
        return selected[2]

    def _launch_via_runner(
        self,
        runner: "AbstractRunnerPlugin",
        store_name: str,
        app_id: str,
        game_id: str,
        _visited: Optional[set] = None,
        **kwargs,
    ) -> LaunchResult:
        """Build intent and execute launch via a runner plugin.

        Supports rerouting: if a runner returns a REROUTE intent, the
        request is forwarded to the target runner. Circular reroutes
        (A -> B -> A) are detected and rejected.

        Args:
            runner: Runner plugin to use
            store_name: Store name
            app_id: Store-specific app ID
            game_id: Game identifier for result tracking
            _visited: Set of runner names already visited (circular detection)

        Returns:
            LaunchResult
        """
        runner_name = runner.runner_name

        try:
            intent = runner.build_launch_intent(store_name, app_id)
            if not intent and runner_name == "native":
                # Check per-game config for a saved executable
                per_game = self._get_per_game_launch_config(game_id)
                saved_exe = (per_game or {}).get("executable", "")
                if saved_exe:
                    exe = Path(saved_exe)
                    if exe.exists():
                        intent = runner.build_launch_intent_with_executable(
                            store_name, app_id, exe,
                        )
                if not intent:
                    # No saved exe — prompt user to pick one
                    native_cb = kwargs.get("native_exe_callback")
                    save_cb = kwargs.get("save_launch_config")
                    if native_cb:
                        exe_path = native_cb()
                        if exe_path:
                            exe = Path(exe_path)
                            intent = runner.build_launch_intent_with_executable(
                                store_name, app_id, exe,
                            )
                            if save_cb:
                                save_cb({"runner": "native", "executable": str(exe)})
                        else:
                            return LaunchResult(
                                success=False,
                                platform_id=f"runner/{runner_name}",
                                game_id=game_id,
                                error_message="No executable selected",
                            )
            if not intent:
                return LaunchResult(
                    success=False,
                    platform_id=f"runner/{runner_name}",
                    game_id=game_id,
                    error_message=f"Runner {runner_name} returned no launch intent",
                )

            # Handle reroute to another runner
            if intent.method == LaunchMethod.REROUTE:
                visited = _visited or {runner_name}
                target = intent.reroute_target
                if not target:
                    return LaunchResult(
                        success=False,
                        platform_id=f"runner/{runner_name}",
                        game_id=game_id,
                        error_message="REROUTE intent has no target",
                    )
                if target in visited:
                    chain = " -> ".join(visited) + f" -> {target}"
                    return LaunchResult(
                        success=False,
                        platform_id=f"runner/{runner_name}",
                        game_id=game_id,
                        error_message=f"Circular reroute detected: {chain}",
                    )
                visited.add(target)
                target_runner = self._runners.get(target)
                if not target_runner or target not in self._available_runners:
                    return LaunchResult(
                        success=False,
                        platform_id=f"runner/{runner_name}",
                        game_id=game_id,
                        error_message=f"Reroute target '{target}' not available",
                    )
                logger.info(
                    "Runner %s rerouting %s/%s to %s",
                    runner_name, store_name, app_id, target,
                )
                return self._launch_via_runner(
                    target_runner, store_name, app_id, game_id, _visited=visited,
                    **kwargs,
                )

            # Apply per-game overrides to intent
            self._apply_user_overrides_to_intent(intent, game_id)

            logger.info(
                "Launching %s/%s via runner %s (method: %s)",
                store_name, app_id, runner_name, intent.method.value,
            )
            result = runner.execute_launch(intent)

            # Multi-store fallback: if IPC launch failed (game not found in
            # remote library), try alternative store entries for the same game.
            if (
                not result.success
                and intent.method == LaunchMethod.IPC
                and "No game with" in (result.error_message or "")
            ):
                alt_stores = kwargs.get("_store_app_ids", {})
                for alt_store, alt_ids in alt_stores.items():
                    if alt_store == store_name or alt_store not in runner.supported_stores:
                        continue
                    for alt_id in (alt_ids if isinstance(alt_ids, list) else [alt_ids]):
                        logger.info(
                            "Retrying %s via %s/%s (fallback)",
                            runner_name, alt_store, alt_id,
                        )
                        alt_intent = runner.build_launch_intent(alt_store, alt_id)
                        if alt_intent:
                            self._apply_user_overrides_to_intent(alt_intent, game_id)
                            alt_result = runner.execute_launch(alt_intent)
                            if alt_result.success:
                                return alt_result

            return result

        except Exception as e:
            logger.error("Runner %s launch failed: %s", runner_name, e)
            return LaunchResult(
                success=False,
                platform_id=f"runner/{runner_name}",
                game_id=game_id,
                error_message=str(e),
            )

    def register_provider(self, provider: PlatformProviderBase) -> None:
        """Register a platform provider

        Args:
            provider: PlatformProviderBase implementation (or plugin provider)
        """
        # Get platform type - may be enum or string
        rt = provider.platform_type
        if isinstance(rt, str):
            try:
                platform_type = PlatformType(rt)
            except ValueError:
                logger.warning(f"Unknown platform type: {rt}")
                platform_type = PlatformType.NATIVE
        else:
            platform_type = rt

        if platform_type not in self._providers:
            self._providers[platform_type] = []

        # Inject dependencies (if provider supports them)
        if self._config and hasattr(provider, 'set_config'):
            provider.set_config(self._config)
        self._providers[platform_type].append(provider)
        logger.debug(f"Registered provider: {provider.provider_name} ({platform_type.value})")

    async def _detect_platforms(self) -> None:
        """Detect available platforms from all providers"""
        self._platforms.clear()
        self._default_platforms.clear()

        for platform_type, providers in self._providers.items():
            type_platforms: List[PlatformInfo] = []

            for provider in providers:
                try:
                    detected = provider.detect_platforms()
                    for platform_data in detected:
                        # Convert dict to PlatformInfo if needed
                        platform = self._ensure_platform_info(platform_data, platform_type)
                        if platform is None:
                            continue

                        # Validate platform
                        if self._validate_platform(provider, platform):
                            self._platforms[platform.platform_id] = platform
                            type_platforms.append(platform)
                            logger.debug(f"Detected platform: {platform.platform_id}")
                except Exception as e:
                    logger.error(f"Provider {provider.provider_name} detection failed: {e}")

            # Set default for this type
            if type_platforms:
                # Prefer explicitly marked defaults, then first detected
                default = next(
                    (r for r in type_platforms if r.is_default),
                    type_platforms[0]
                )
                self._default_platforms[platform_type] = default.platform_id

        logger.info(f"Detected {len(self._platforms)} platforms across {len(self._providers)} types")

    def _ensure_platform_info(
        self,
        platform_data: Any,
        platform_type: PlatformType
    ) -> Optional[PlatformInfo]:
        """Convert platform data to PlatformInfo if needed

        Handles both PlatformInfo objects and dicts from plugin providers.

        Args:
            platform_data: PlatformInfo or dict from provider
            platform_type: Platform type for this provider

        Returns:
            PlatformInfo object or None if invalid
        """
        if isinstance(platform_data, PlatformInfo):
            return platform_data

        if isinstance(platform_data, dict):
            try:
                executable_path = platform_data.get("executable_path")
                if executable_path and not isinstance(executable_path, Path):
                    executable_path = Path(executable_path)

                return PlatformInfo(
                    platform_id=platform_data.get("platform_id", "unknown"),
                    platform_type=platform_type,
                    name=platform_data.get("name", "Unknown"),
                    version=platform_data.get("version", ""),
                    executable_path=executable_path,
                    is_default=platform_data.get("is_default", False),
                    is_managed=platform_data.get("is_managed", False),
                    capabilities=platform_data.get("capabilities", []),
                    metadata=platform_data.get("metadata", {}),
                )
            except Exception as e:
                logger.warning(f"Failed to convert platform data: {e}")
                return None

        logger.warning(f"Unknown platform data type: {type(platform_data)}")
        return None

    def _validate_platform(
        self,
        provider: PlatformProviderBase,
        platform: PlatformInfo
    ) -> bool:
        """Validate a platform is functional

        Args:
            provider: Provider that detected this platform
            platform: Platform to validate

        Returns:
            True if platform is valid
        """
        # Use provider's validate method if available
        if hasattr(provider, 'validate_platform'):
            return provider.validate_platform(platform)

        # Basic validation: check executable exists if specified
        if platform.executable_path and not platform.executable_path.exists():
            logger.warning(f"Platform executable missing: {platform.executable_path}")
            return False

        return True

    def _ensure_launch_config(
        self,
        config_data: Any,
        platform: PlatformInfo,
        game_id: str
    ) -> Optional[LaunchConfig]:
        """Convert launch config data to LaunchConfig if needed

        Handles both LaunchConfig objects and dicts from plugin providers.

        Args:
            config_data: LaunchConfig or dict from provider
            platform: Platform being used
            game_id: Game identifier

        Returns:
            LaunchConfig object or None if invalid
        """
        if isinstance(config_data, LaunchConfig):
            return config_data

        if isinstance(config_data, dict):
            try:
                # Get launch method
                method_str = config_data.get("launch_method", "url_scheme")
                if isinstance(method_str, LaunchMethod):
                    launch_method = method_str
                else:
                    launch_method = LaunchMethod(method_str)

                # Handle paths
                executable = config_data.get("executable")
                if executable and not isinstance(executable, Path):
                    executable = Path(executable)

                working_dir = config_data.get("working_directory")
                if working_dir and not isinstance(working_dir, Path):
                    working_dir = Path(working_dir)

                config_file = config_data.get("config_file")
                if config_file and not isinstance(config_file, Path):
                    config_file = Path(config_file)

                # Build metadata, including store_name and app_id for delegation
                metadata = dict(config_data.get("metadata", {}))
                if "store_name" in config_data:
                    metadata["store_name"] = config_data["store_name"]
                if "app_id" in config_data:
                    metadata["app_id"] = config_data["app_id"]

                return LaunchConfig(
                    game_id=config_data.get("game_id", game_id),
                    platform_id=config_data.get("platform_id", platform.platform_id),
                    launch_method=launch_method,
                    launch_url=config_data.get("launch_url"),
                    executable=executable,
                    arguments=config_data.get("arguments", []),
                    working_directory=working_dir,
                    environment=config_data.get("environment", {}),
                    fullscreen=config_data.get("fullscreen", False),
                    config_file=config_file,
                    metadata=metadata,
                )
            except Exception as e:
                logger.warning(f"Failed to convert launch config: {e}")
                return None

        logger.warning(f"Unknown launch config type: {type(config_data)}")
        return None

    def _launch_with_config(
        self,
        provider: PlatformProviderBase,
        config: LaunchConfig
    ) -> LaunchResult:
        """Launch a game with the given configuration

        For EXECUTABLE launches with a valid game_id, routes through
        the runner subprocess for session tracking and sleep inhibition.
        For URL_SCHEME and other methods, delegates to the provider.

        Args:
            provider: Provider handling the launch
            config: Launch configuration

        Returns:
            LaunchResult with success status
        """
        # EXECUTABLE launches go through runner subprocess for session tracking
        if (config.launch_method == LaunchMethod.EXECUTABLE
                and config.executable
                and self._game_service):
            return self._launch_via_runner_subprocess(config)

        # Try provider's launch method first
        if hasattr(provider, 'launch'):
            try:
                result = provider.launch(config)
                if isinstance(result, LaunchResult):
                    return result
                # Handle dict result from plugins
                if isinstance(result, dict):
                    return LaunchResult(
                        success=result.get("success", False),
                        platform_id=result.get("platform_id", config.platform_id),
                        game_id=result.get("game_id", config.game_id),
                        error_message=result.get("error_message"),
                        process_id=result.get("process_id"),
                        launch_method=config.launch_method,
                    )
                # Assume bool means success
                if isinstance(result, bool):
                    return LaunchResult(
                        success=result,
                        platform_id=config.platform_id,
                        game_id=config.game_id,
                        launch_method=config.launch_method,
                    )
            except Exception as e:
                logger.error(f"Provider launch failed: {e}")
                return LaunchResult(
                    success=False,
                    platform_id=config.platform_id,
                    game_id=config.game_id,
                    error_message=str(e),
                )

        # Fall back to built-in launch methods
        return self._builtin_launch(config)

    def _launch_via_runner_subprocess(self, config: LaunchConfig) -> LaunchResult:
        """Spawn game via runner subprocess for session tracking.

        The runner subprocess is a lightweight process that:
        1. Inhibits system sleep
        2. Launches the game
        3. Waits for exit
        4. Records session duration in the DB

        Args:
            config: Launch configuration (must have executable set)

        Returns:
            LaunchResult with runner subprocess PID
        """
        import json
        import subprocess
        import sys

        # Determine store_name from config metadata
        store_name = config.metadata.get("store_name", "unknown")

        # Record launch session (creates PlaySession, returns session_id)
        session_id = self._game_service.record_launch(config.game_id, store_name)

        # Build runner subprocess command
        db_path = str(self._database.db_path) if self._database else ""
        if not db_path:
            logger.warning("No database path for runner subprocess")

        runner_cmd = [
            sys.executable, "-m", "luducat", "_run",
            "--session-id", str(session_id),
            "--db-path", db_path,
        ]
        if config.environment:
            runner_cmd.extend(["--env-json", json.dumps(config.environment)])
        if config.working_directory:
            runner_cmd.extend(["--working-dir", str(config.working_directory)])
        runner_cmd.append("--")
        runner_cmd.extend([str(config.executable)] + config.arguments)

        try:
            if sys.platform == "win32":
                process = subprocess.Popen(
                    runner_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
            else:
                process = subprocess.Popen(
                    runner_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            logger.info(
                "Runner subprocess spawned (pid %d) for game %s, session %d",
                process.pid, config.game_id[:8], session_id,
            )

            return LaunchResult(
                success=True,
                platform_id=config.platform_id,
                game_id=config.game_id,
                process_id=process.pid,
                launch_method=LaunchMethod.EXECUTABLE,
            )

        except Exception as e:
            logger.error("Failed to spawn runner subprocess: %s", e)
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message=f"Runner subprocess failed: {e}",
            )

    def _builtin_launch(self, config: LaunchConfig) -> LaunchResult:
        """Built-in launch implementation

        Args:
            config: Launch configuration

        Returns:
            LaunchResult
        """
        try:
            if config.launch_method == LaunchMethod.URL_SCHEME:
                return self._launch_url_scheme(config)
            elif config.launch_method == LaunchMethod.EXECUTABLE:
                return self._launch_executable(config)
            else:
                return LaunchResult(
                    success=False,
                    platform_id=config.platform_id,
                    game_id=config.game_id,
                    error_message=f"Unsupported launch method: {config.launch_method}",
                )
        except Exception as e:
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message=str(e),
            )

    def _launch_url_scheme(self, config: LaunchConfig) -> LaunchResult:
        """Launch via URL scheme"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if not config.launch_url:
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message="No launch URL provided",
            )

        logger.info(f"Launching via URL scheme: {config.launch_url}")
        success = QDesktopServices.openUrl(QUrl(config.launch_url))

        return LaunchResult(
            success=success,
            platform_id=config.platform_id,
            game_id=config.game_id,
            launch_method=LaunchMethod.URL_SCHEME,
            error_message=None if success else "Failed to open URL",
        )

    def _launch_executable(self, config: LaunchConfig) -> LaunchResult:
        """Launch via direct executable"""
        import subprocess
        import sys

        if not config.executable or not config.executable.exists():
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message=f"Executable not found: {config.executable}",
            )

        args = [str(config.executable)] + config.arguments
        logger.info(f"Launching executable: {' '.join(args)}")

        try:
            # Prepare environment
            env = None
            if config.environment:
                import os
                env = os.environ.copy()
                env.update(config.environment)

            # Launch detached process
            if sys.platform == "win32":
                process = subprocess.Popen(
                    args,
                    cwd=config.working_directory,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
            else:
                process = subprocess.Popen(
                    args,
                    cwd=config.working_directory,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

            return LaunchResult(
                success=True,
                platform_id=config.platform_id,
                game_id=config.game_id,
                process_id=process.pid,
                launch_method=LaunchMethod.EXECUTABLE,
            )

        except Exception as e:
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message=str(e),
            )

    def _load_assignments(self) -> None:
        """Load game-platform assignments from database"""
        if not self._database:
            return

        try:
            # TODO: Load from game_installations table when implemented
            pass
        except Exception as e:
            logger.error(f"Failed to load platform assignments: {e}")

    def get_available_platforms(
        self,
        platform_type: Optional[PlatformType] = None
    ) -> List[PlatformInfo]:
        """Get list of available platforms

        Args:
            platform_type: Filter by type, or None for all

        Returns:
            List of PlatformInfo objects
        """
        if platform_type:
            return [
                r for r in self._platforms.values()
                if r.platform_type == platform_type
            ]
        return list(self._platforms.values())

    def get_platform(self, platform_id: str) -> Optional[PlatformInfo]:
        """Get platform by ID

        Args:
            platform_id: Platform identifier

        Returns:
            PlatformInfo or None
        """
        return self._platforms.get(platform_id)

    def get_default_platform(self, platform_type: PlatformType) -> Optional[PlatformInfo]:
        """Get default platform for a type

        Args:
            platform_type: Platform type

        Returns:
            Default PlatformInfo or None
        """
        platform_id = self._default_platforms.get(platform_type)
        if platform_id:
            return self._platforms.get(platform_id)
        return None

    def set_default_platform(self, platform_id: str) -> bool:
        """Set a platform as default for its type

        Args:
            platform_id: Platform to set as default

        Returns:
            True if successful
        """
        platform = self._platforms.get(platform_id)
        if not platform:
            logger.warning(f"Platform not found: {platform_id}")
            return False

        # Update default
        self._default_platforms[platform.platform_type] = platform_id

        # Update platform info
        for r in self._platforms.values():
            if r.platform_type == platform.platform_type:
                r.is_default = (r.platform_id == platform_id)

        logger.info(f"Set default platform: {platform_id}")
        return True

    def get_assigned_platform(self, game_id: str) -> Optional[PlatformInfo]:
        """Get platform assigned to a game

        Args:
            game_id: Game identifier

        Returns:
            Assigned PlatformInfo or None
        """
        platform_id = self._assignments.get(game_id)
        if platform_id:
            return self._platforms.get(platform_id)
        return None

    def assign_platform(self, game_id: str, platform_id: str) -> bool:
        """Assign a platform to a game

        Args:
            game_id: Game identifier
            platform_id: Platform to assign

        Returns:
            True if successful
        """
        if platform_id not in self._platforms:
            logger.warning(f"Platform not found: {platform_id}")
            return False

        self._assignments[game_id] = platform_id
        logger.debug(f"Assigned platform {platform_id} to game {game_id}")

        # TODO: Persist to database
        return True

    def get_compatible_platforms(self, game: "Game") -> List[PlatformInfo]:
        """Get platforms that can run a game

        Args:
            game: Game to check compatibility for

        Returns:
            List of compatible PlatformInfo objects
        """
        compatible = []

        for platform_type, providers in self._providers.items():
            for provider in providers:
                try:
                    if provider.can_run_game(game):
                        # Add all platforms of this type
                        for platform in self._platforms.values():
                            # Handle both enum and string comparisons
                            rt = platform.platform_type
                            if rt == platform_type or (
                                hasattr(rt, 'value') and rt.value == platform_type.value
                            ):
                                compatible.append(platform)
                        break  # Only need one provider per type to confirm
                except Exception as e:
                    logger.debug(f"Compatibility check failed for {provider.provider_name}: {e}")

        return compatible

    def select_platform_for_game(self, game: "Game") -> Optional[PlatformInfo]:
        """Select best platform for a game

        Selection order:
        1. Explicitly assigned platform
        2. Compatible platform (type default)
        3. External launcher fallback

        Args:
            game: Game to select platform for

        Returns:
            Selected PlatformInfo or None
        """
        game_id = str(game.id) if hasattr(game, 'id') else getattr(game, 'store_app_id', None)

        # 1. Check explicit assignment
        if game_id:
            assigned = self.get_assigned_platform(game_id)
            if assigned and assigned.platform_id in self._platforms:
                return assigned

        # 2. Find compatible platform
        compatible = self.get_compatible_platforms(game)
        if compatible:
            # Prefer default of compatible type
            for platform in compatible:
                if platform.is_default:
                    return platform
            return compatible[0]

        return None

    def get_provider_for_platform(self, platform: PlatformInfo) -> Optional[PlatformProviderBase]:
        """Get the provider that handles a platform

        Args:
            platform: Platform to find provider for

        Returns:
            PlatformProviderBase or None
        """
        providers = self._providers.get(platform.platform_type, [])
        return providers[0] if providers else None

    async def launch_game(
        self,
        game: "Game",
        platform_id: Optional[str] = None,
        runner_name: Optional[str] = None,
        **kwargs
    ) -> LaunchResult:
        """Launch a game using three-tier selection.

        Selection order:
        1. Explicit assignment (platform_id or runner_name parameter)
        2. Compatible runner (highest priority for game's store)
        3. Compatible platform (DOSBox, ScummVM, Wine prefix)

        Args:
            game: Game to launch
            platform_id: Specific platform to use
            runner_name: Specific runner to use
            **kwargs: Additional launch options

        Returns:
            LaunchResult with success status
        """
        game_id = str(game.id) if hasattr(game, 'id') else getattr(game, 'store_app_id', 'unknown')
        store_name = getattr(game, 'store_name', None) or ''
        app_id = getattr(game, 'store_app_id', None) or ''

        # Collect all store entries for multi-store fallback (e.g. Playnite bridge)
        store_app_ids = getattr(game, 'store_app_ids', None) or {}
        if store_app_ids:
            kwargs["_store_app_ids"] = store_app_ids

        # --- Tier 0: Per-game launch config from database ---
        if not runner_name and not platform_id:
            per_game_config = self._get_per_game_launch_config(game_id)
            if per_game_config:
                cfg_runner = per_game_config.get("runner")
                cfg_platform = per_game_config.get("platform")
                if cfg_runner:
                    runner_name = cfg_runner
                    logger.debug(
                        "Per-game config: using runner '%s' for %s",
                        cfg_runner, game_id[:8],
                    )
                elif cfg_platform:
                    platform_id = cfg_platform
                    logger.debug(
                        "Per-game config: using platform '%s' for %s",
                        cfg_platform, game_id[:8],
                    )

        # --- Tier 1: Explicit runner assignment ---
        if runner_name:
            runner = self._runners.get(runner_name)
            if not runner:
                return LaunchResult(
                    success=False,
                    platform_id=f"runner/{runner_name}",
                    game_id=game_id,
                    error_message=f"Runner not found: {runner_name}",
                )
            return self._launch_via_runner(runner, store_name, app_id, game_id, **kwargs)

        # --- Tier 1: Explicit platform assignment ---
        if platform_id:
            # Check if it's a runner reference (runner/<name>)
            if platform_id.startswith("runner/"):
                rname = platform_id[7:]
                runner = self._runners.get(rname)
                if runner:
                    return self._launch_via_runner(runner, store_name, app_id, game_id, **kwargs)
                return LaunchResult(
                    success=False,
                    platform_id=platform_id,
                    game_id=game_id,
                    error_message=f"Runner not found: {rname}",
                )

            platform = self.get_platform(platform_id)
            if not platform:
                return LaunchResult(
                    success=False,
                    platform_id=platform_id,
                    game_id=game_id,
                    error_message=f"Platform not found: {platform_id}",
                )
            return await self._launch_via_platform(game, platform, game_id, **kwargs)

        # --- Tier 2: Compatible runner (highest priority for store) ---
        if store_name and app_id:
            runner = self._select_runner(store_name, app_id)
            if runner:
                return self._launch_via_runner(runner, store_name, app_id, game_id, **kwargs)

        # --- Tier 3: Compatible platform ---
        compatible = self.get_compatible_platforms(game)
        if compatible:
            # Prefer default of compatible type
            platform = next(
                (r for r in compatible if r.is_default),
                compatible[0],
            )
            return await self._launch_via_platform(game, platform, game_id, **kwargs)

        return LaunchResult(
            success=False,
            platform_id="none",
            game_id=game_id,
            error_message=(
                f"No launch method available for {store_name}/{app_id}. "
                "Install a compatible launcher (Heroic, Steam, etc.)."
            ),
        )

    async def _launch_via_platform(
        self,
        game: "Game",
        platform: PlatformInfo,
        game_id: str,
        **kwargs,
    ) -> LaunchResult:
        """Launch via a platform provider."""
        provider = self.get_provider_for_platform(platform)
        if not provider:
            return LaunchResult(
                success=False,
                platform_id=platform.platform_id,
                game_id=game_id,
                error_message=f"No provider for platform type: {platform.platform_type}",
            )

        # Inject per-game launch config if not already provided
        if "launch_config" not in kwargs and self._game_service:
            try:
                kwargs["launch_config"] = self._game_service.get_launch_config(game_id)
            except Exception:
                pass

        try:
            config_data = provider.create_launch_config(game, platform, **kwargs)
            config = self._ensure_launch_config(config_data, platform, game_id)
            if config is None:
                return LaunchResult(
                    success=False,
                    platform_id=platform.platform_id,
                    game_id=game_id,
                    error_message="Failed to create valid launch config",
                )
        except Exception as e:
            logger.error("Failed to create launch config: %s", e)
            return LaunchResult(
                success=False,
                platform_id=platform.platform_id,
                game_id=game_id,
                error_message=f"Failed to create launch config: {e}",
            )

        # Apply per-game user overrides (launch_args, working_dir, environment)
        self._apply_user_overrides(config, game_id)

        logger.info("Launching game %s with platform %s", game_id, platform.platform_id)
        return self._launch_with_config(provider, config)

    def _apply_user_overrides(self, config: LaunchConfig, game_id: str) -> None:
        """Apply per-game user settings on top of provider config.

        Merges launch_args, working_dir, and environment from the user's
        per-game launch configuration (Settings tab).
        """
        import shlex

        per_game = self._get_per_game_launch_config(game_id)
        if not per_game:
            return

        user_args = per_game.get("launch_args", "")
        if user_args:
            try:
                config.arguments.extend(shlex.split(user_args))
            except ValueError:
                config.arguments.append(user_args)

        user_wd = per_game.get("working_dir")
        if user_wd:
            config.working_directory = Path(user_wd)

        user_env = per_game.get("environment", {})
        if user_env:
            config.environment.update(user_env)

    def _apply_user_overrides_to_intent(
        self, intent: "LaunchIntent", game_id: str
    ) -> None:
        """Apply per-game user settings to a runner launch intent.

        Merges launch_args, working_dir, and environment onto
        URL_SCHEME/EXECUTABLE intents. IPC intents (Playnite bridge) are
        skipped — the bridge is a strict game-ID relay and never applies
        per-game overrides.
        """
        import shlex

        # IPC intents are pure game-ID relays; overrides don't apply
        if intent.ipc_payload is not None:
            return

        per_game = self._get_per_game_launch_config(game_id)
        if not per_game:
            return

        user_args = per_game.get("launch_args", "")
        user_wd = per_game.get("working_dir")
        user_env = per_game.get("environment", {})

        if not user_args and not user_wd and not user_env:
            return

        if user_args:
            try:
                intent.arguments.extend(shlex.split(user_args))
            except ValueError:
                intent.arguments.append(user_args)
        if user_wd:
            intent.working_directory = Path(user_wd)
        if user_env:
            intent.environment.update(user_env)

    def _get_per_game_launch_config(self, game_id: str) -> Optional[Dict[str, Any]]:
        """Read per-game launch config from UserGameData.

        Returns:
            Parsed config dict with keys: runner, platform, launch_args.
            None if no per-game config set.
        """
        game_service = getattr(self, "_game_service", None)
        if not game_service:
            return None
        try:
            return game_service.get_launch_config(game_id)
        except Exception as e:
            logger.debug("Failed to read per-game launch config for %s: %s", game_id[:8], e)
        return None

    async def refresh_platforms(self) -> int:
        """Refresh platform and runner detection.

        Re-scans for available platforms and launchers.

        Returns:
            Total number of platforms + available runners
        """
        await self._detect_platforms()
        self._detect_launchers()
        return len(self._platforms) + len(self._available_runners)

    # === RUNNER QUERY METHODS ===

    def get_available_runners(self) -> Dict[str, RunnerLauncherInfo]:
        """Get all detected/available runner launchers.

        Returns:
            Dict mapping runner_name -> RunnerLauncherInfo
        """
        return dict(self._available_runners)

    def get_runner(self, runner_name: str) -> Optional["AbstractRunnerPlugin"]:
        """Get a runner plugin by name.

        Args:
            runner_name: Runner identifier

        Returns:
            AbstractRunnerPlugin or None
        """
        return self._runners.get(runner_name)

    def get_runners_for_store(self, store_name: str) -> List[str]:
        """Get available runner names that support a store.

        Args:
            store_name: Store plugin name

        Returns:
            List of runner names, sorted by priority (highest first)
        """
        candidates = []
        for runner_name, runner in self._runners.items():
            if runner_name not in self._available_runners:
                continue
            if store_name in runner.supported_stores:
                priority = runner.get_launcher_priority()
                candidates.append((priority, runner_name))

        candidates.sort(key=lambda x: (-x[0], x[1]))
        return [name for _, name in candidates]

    def get_runtime_stats(self) -> Dict[str, Any]:
        """Get platform and runner statistics.

        Returns:
            Dict with platform/runner counts
        """
        stats = {
            "total_platforms": len(self._platforms),
            "provider_types": len(self._providers),
            "total_runners": len(self._runners),
            "available_runners": len(self._available_runners),
            "by_type": {},
            "runners": {},
        }

        for platform_type in PlatformType:
            count = len([
                r for r in self._platforms.values()
                if r.platform_type == platform_type
            ])
            if count > 0:
                stats["by_type"][platform_type.value] = count

        for runner_name, info in self._available_runners.items():
            runner = self._runners.get(runner_name)
            stats["runners"][runner_name] = {
                "install_type": info.install_type,
                "stores": runner.supported_stores if runner else [],
                "priority": runner.get_launcher_priority() if runner else 0,
            }

        return stats


# Module-level singleton
_runtime_manager: Optional[RuntimeManager] = None


def get_runtime_manager() -> RuntimeManager:
    """Get or create the runtime manager singleton

    Returns:
        RuntimeManager instance
    """
    global _runtime_manager
    if _runtime_manager is None:
        _runtime_manager = RuntimeManager()
    return _runtime_manager
