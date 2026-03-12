# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runtime_base.py

"""Platform Provider Base Classes

Abstract base classes for platform providers that execute games.
Platform providers handle different execution methods:
- Native emulators (DOSBox, ScummVM)
- Compatibility layers (Wine, Proton) - future

Runner plugins (plugins/runners/) handle launcher delegation.
Each provider type is implemented as a plugin in plugins/platforms/.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from luducat.core.database import Game

logger = logging.getLogger(__name__)


class PlatformType(Enum):
    """Types of platform providers"""
    DOSBOX = "dosbox"  # DOSBox-Staging, DOSBox-X
    SCUMMVM = "scummvm"  # ScummVM
    WINE = "wine"  # Wine, Proton (future)
    NATIVE = "native"  # Direct execution


class LaunchMethod(Enum):
    """How to launch a game"""
    URL_SCHEME = "url_scheme"  # steam://, heroic://, etc.
    EXECUTABLE = "executable"  # Direct binary execution
    COMMAND = "command"  # Shell command
    IPC = "ipc"  # Inter-process communication (Playnite future)
    REROUTE = "reroute"  # Delegate to another runner plugin


@dataclass
class PlatformInfo:
    """Information about an installed platform"""
    platform_id: str  # Unique ID (e.g., "dosbox/0.81.0")
    platform_type: PlatformType
    name: str  # Display name
    version: str
    executable_path: Optional[Path] = None
    is_default: bool = False
    is_managed: bool = False  # Managed by luducat vs system-installed
    capabilities: List[str] = field(default_factory=list)  # e.g., ["mt32", "fluidsynth"]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "platform_type": self.platform_type.value,
            "name": self.name,
            "version": self.version,
            "executable_path": str(self.executable_path) if self.executable_path else None,
            "is_default": self.is_default,
            "is_managed": self.is_managed,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }


@dataclass
class LaunchConfig:
    """Configuration for launching a game"""
    game_id: str
    platform_id: str
    launch_method: LaunchMethod
    # For URL_SCHEME
    launch_url: Optional[str] = None
    # For EXECUTABLE/COMMAND
    executable: Optional[Path] = None
    arguments: List[str] = field(default_factory=list)
    working_directory: Optional[Path] = None
    environment: Dict[str, str] = field(default_factory=dict)
    # Additional options
    fullscreen: bool = False
    config_file: Optional[Path] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LaunchResult:
    """Result of a game launch attempt"""
    success: bool
    platform_id: str
    game_id: str
    error_message: Optional[str] = None
    process_id: Optional[int] = None
    launch_method: Optional[LaunchMethod] = None


# =============================================================================
# Runner Plugin Data Types
# =============================================================================

@dataclass
class RunnerLauncherInfo:
    """Information about a detected launcher for a runner plugin.

    Returned by ``AbstractRunnerPlugin.detect_launcher()`` to describe
    the launcher installation found on the system.
    """
    runner_name: str
    path: Optional[Path]            # None for Flatpak or URL-only runners
    install_type: str               # "system", "flatpak", "appimage", "registry", "bundle"
    virtualized: bool               # Flatpak or sandboxed AppImage
    version: Optional[str] = None
    url_scheme: Optional[str] = None
    flatpak_id: Optional[str] = None
    capabilities: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runner_name": self.runner_name,
            "path": str(self.path) if self.path else None,
            "install_type": self.install_type,
            "virtualized": self.virtualized,
            "version": self.version,
            "url_scheme": self.url_scheme,
            "flatpak_id": self.flatpak_id,
            "capabilities": self.capabilities,
        }


@dataclass
class LaunchIntent:
    """Structured launch intent built by a runner plugin.

    Separates *what to launch* (built by ``build_launch_intent()``) from
    *how to execute it* (handled by ``execute_launch()``). The RuntimeManager
    orchestrates both steps.
    """
    method: LaunchMethod
    runner_name: str
    store_name: str
    app_id: str
    url: Optional[str] = None
    executable: Optional[Path] = None
    arguments: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[Path] = None
    ipc_payload: Optional[dict] = None
    reroute_target: Optional[str] = None  # Target runner name for REROUTE


class PlatformProviderBase(ABC):
    """Abstract base class for platform providers

    Platform providers are responsible for:
    1. Detecting available platforms of their type
    2. Generating launch configurations
    3. Executing game launches
    4. Managing platform-specific settings

    Implementations should be stateless where possible,
    with configuration coming from the game or platform settings.

    Example implementation:
        class DOSBoxProvider(PlatformProviderBase):
            @property
            def platform_type(self) -> PlatformType:
                return PlatformType.DOSBOX

            def detect_platforms(self) -> List[PlatformInfo]:
                # Find DOSBox installations
                ...

            def can_run_game(self, game) -> bool:
                # Check if game has DOSBox metadata
                return game.has_tag("dosbox") or game.metadata.get("dosbox_conf")

            def create_launch_config(self, game, platform) -> LaunchConfig:
                # Generate DOSBox command line
                ...
    """

    def __init__(self):
        """Initialize platform provider"""
        self._config = None

    def set_config(self, config) -> None:
        """Set configuration manager

        Args:
            config: Config instance
        """
        self._config = config

    @property
    @abstractmethod
    def platform_type(self) -> PlatformType:
        """Get the platform type this provider handles

        Returns:
            PlatformType enum value
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Get human-readable provider name

        Returns:
            Provider name string
        """
        pass

    @abstractmethod
    def detect_platforms(self) -> List[PlatformInfo]:
        """Detect available platforms of this type

        Scans the system for installed platforms and returns
        information about each one found.

        Returns:
            List of PlatformInfo for detected platforms
        """
        pass

    @abstractmethod
    def can_run_game(self, game: "Game") -> bool:
        """Check if this provider can run the given game

        Args:
            game: Game to check

        Returns:
            True if this provider can handle the game
        """
        pass

    @abstractmethod
    def create_launch_config(
        self,
        game: "Game",
        platform: PlatformInfo,
        **kwargs
    ) -> LaunchConfig:
        """Create launch configuration for a game

        Args:
            game: Game to launch
            platform: Platform to use
            **kwargs: Additional launch options

        Returns:
            LaunchConfig with all launch parameters
        """
        pass

    def launch(self, config: LaunchConfig) -> LaunchResult:
        """Launch a game with the given configuration

        Default implementation handles common launch methods.
        Override for custom launch behavior.

        Args:
            config: Launch configuration

        Returns:
            LaunchResult with success status
        """
        try:
            if config.launch_method == LaunchMethod.URL_SCHEME:
                return self._launch_url_scheme(config)
            elif config.launch_method == LaunchMethod.EXECUTABLE:
                return self._launch_executable(config)
            elif config.launch_method == LaunchMethod.COMMAND:
                return self._launch_command(config)
            else:
                return LaunchResult(
                    success=False,
                    platform_id=config.platform_id,
                    game_id=config.game_id,
                    error_message=f"Unknown launch method: {config.launch_method}",
                )
        except Exception as e:
            logger.error(f"Launch failed: {e}")
            return LaunchResult(
                success=False,
                platform_id=config.platform_id,
                game_id=config.game_id,
                error_message=str(e),
            )

    def _launch_url_scheme(self, config: LaunchConfig) -> LaunchResult:
        """Launch via URL scheme (steam://, heroic://, etc.)

        Args:
            config: Launch configuration

        Returns:
            LaunchResult
        """
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
        """Launch via direct executable

        Args:
            config: Launch configuration

        Returns:
            LaunchResult with process ID
        """
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

    def _launch_command(self, config: LaunchConfig) -> LaunchResult:
        """Launch via shell command

        Args:
            config: Launch configuration

        Returns:
            LaunchResult
        """
        # For now, delegate to executable launch
        # Command launch might need shell=True in some cases
        return self._launch_executable(config)

    def get_platform_settings_schema(self) -> Dict[str, Any]:
        """Get JSON schema for platform-specific settings

        Override to provide custom settings UI.

        Returns:
            JSON schema dict or empty dict
        """
        return {}

    def get_game_settings_schema(self, game: "Game") -> Dict[str, Any]:
        """Get JSON schema for per-game platform settings

        Override to provide game-specific settings.

        Args:
            game: Game to get settings schema for

        Returns:
            JSON schema dict or empty dict
        """
        return {}

    def validate_platform(self, platform: PlatformInfo) -> bool:
        """Validate that a platform is working correctly

        Args:
            platform: Platform to validate

        Returns:
            True if platform is functional
        """
        if platform.executable_path and not platform.executable_path.exists():
            logger.warning(f"Platform executable missing: {platform.executable_path}")
            return False
        return True


class EmulatorProviderBase(PlatformProviderBase):
    """Base class for emulator providers (DOSBox, ScummVM, etc.)

    Emulators run games through compatibility/emulation layers
    and typically need configuration files and game data paths.
    """

    @abstractmethod
    def generate_config(
        self,
        game: "Game",
        platform: PlatformInfo,
        game_path: Path,
        **kwargs
    ) -> Path:
        """Generate configuration file for running a game

        Args:
            game: Game to configure
            platform: Platform to use
            game_path: Path to game files
            **kwargs: Additional options

        Returns:
            Path to generated config file
        """
        pass

    @abstractmethod
    def get_game_data_path(self, game: "Game") -> Optional[Path]:
        """Get path to game data files

        Args:
            game: Game to locate

        Returns:
            Path to game data or None if not found
        """
        pass

    def can_run_game(self, game: "Game") -> bool:
        """Check if this emulator can run the game

        Default implementation checks for platform-specific tags.

        Args:
            game: Game to check

        Returns:
            True if game appears compatible
        """
        # Check for tags indicating compatibility
        platform_name = self.platform_type.value.lower()
        if hasattr(game, 'tags') and game.tags:
            for tag in game.tags:
                if platform_name in tag.lower():
                    return True

        # Check metadata for platform-specific config
        if hasattr(game, 'metadata_json') and game.metadata_json:
            if f"{platform_name}_conf" in game.metadata_json:
                return True

        return False
