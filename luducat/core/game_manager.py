# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# game_manager.py

"""Game Manager - Orchestrates game installation and launching

The GameManager is the application-facing coordinator that brings together:
- RuntimeManager for game execution

It handles:
1. Installation status tracking
2. Launch orchestration through RuntimeManager
3. Per-game settings management

Architecture:
    GameManager (orchestrator)
    └── RuntimeManager (game execution)
        └── PlatformProviders (plugins)

Usage:
    manager = GameManager()
    manager.set_config(config)
    manager.set_runtime_manager(runtime_manager)
    await manager.initialize()

    # Check installation status
    status = manager.get_installation_status(game)

    # Launch a game
    result = await manager.launch_game(game)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from luducat.core.config import Config
    from luducat.core.runtime_manager import RuntimeManager
    from luducat.core.database import Game

logger = logging.getLogger(__name__)


class InstallationStatus(Enum):
    """Game installation status"""
    NOT_INSTALLED = "not_installed"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    INSTALLED = "installed"
    UPDATE_AVAILABLE = "update_available"
    VERIFYING = "verifying"
    VERIFY_FAILED = "verify_failed"
    CORRUPT = "corrupt"
    REPAIR_NEEDED = "repair_needed"


@dataclass
class InstallationInfo:
    """Information about a game installation"""
    game_id: str
    status: InstallationStatus
    install_path: Optional[Path] = None
    version: Optional[str] = None
    installed_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    last_played_at: Optional[datetime] = None
    launch_count: int = 0
    size_bytes: int = 0
    platform_id: Optional[str] = None
    settings: Dict[str, Any] = field(default_factory=dict)
    checksums: Dict[str, str] = field(default_factory=dict)  # filename -> sha256

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "status": self.status.value,
            "install_path": str(self.install_path) if self.install_path else None,
            "version": self.version,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "last_played_at": self.last_played_at.isoformat() if self.last_played_at else None,
            "launch_count": self.launch_count,
            "size_bytes": self.size_bytes,
            "platform_id": self.platform_id,
            "settings": self.settings,
            "checksums": self.checksums,
        }


@dataclass
class InstallResult:
    """Result of an installation operation"""
    success: bool
    game_id: str
    status: InstallationStatus
    install_path: Optional[Path] = None
    error_message: Optional[str] = None
    files_installed: int = 0
    bytes_installed: int = 0


@dataclass
class LaunchResult:
    """Result of a game launch"""
    success: bool
    game_id: str
    platform_id: Optional[str] = None
    error_message: Optional[str] = None
    process_id: Optional[int] = None


class GameManager:
    """Orchestrates game installation and launching

    The GameManager provides a unified interface for:
    - Tracking game installation status
    - Launching games through RuntimeManager
    - Managing per-game settings
    """

    def __init__(self):
        """Initialize game manager"""
        self._config: Optional["Config"] = None
        self._database = None
        self._runtime_manager: Optional["RuntimeManager"] = None

        # Installation cache (game_id -> InstallationInfo)
        self._installations: Dict[str, InstallationInfo] = {}

        # Default installation root
        self._install_root: Optional[Path] = None

        self._initialized = False

    def set_config(self, config: "Config") -> None:
        """Set configuration manager

        Args:
            config: Config instance
        """
        self._config = config
        logger.debug("GameManager: config set")

    def set_database(self, database) -> None:
        """Set database for persistence

        Args:
            database: Database instance
        """
        self._database = database
        logger.debug("GameManager: database set")

    def set_runtime_manager(self, manager: "RuntimeManager") -> None:
        """Set runtime manager

        Args:
            manager: RuntimeManager instance
        """
        self._runtime_manager = manager
        logger.debug("GameManager: runtime_manager set")

    async def initialize(self) -> None:
        """Initialize game manager

        Loads installation data from database.
        Call after setting all dependencies.
        """
        if self._initialized:
            return

        logger.info("Initializing GameManager")

        # Get install root from config
        if self._config:
            install_root = self._config.get("game_manager.install_root", "")
            if install_root:
                self._install_root = Path(install_root)

        # Load installations from database
        self._load_installations()

        self._initialized = True
        logger.info(f"GameManager initialized: {len(self._installations)} installations tracked")

    def _load_installations(self) -> None:
        """Load installation data from database"""
        if not self._database:
            return

        try:
            # TODO: Load from game_installations table when implemented
            pass
        except Exception as e:
            logger.error(f"Failed to load installations: {e}")

    def _save_installation(self, info: InstallationInfo) -> None:
        """Save installation info to database

        Args:
            info: InstallationInfo to save
        """
        if not self._database:
            return

        try:
            # TODO: Save to game_installations table when implemented
            pass
        except Exception as e:
            logger.error(f"Failed to save installation: {e}")

    # === Installation Status ===

    def get_installation_status(self, game: "Game") -> InstallationStatus:
        """Get installation status for a game

        Args:
            game: Game to check

        Returns:
            InstallationStatus enum value
        """
        game_id = self._get_game_id(game)
        info = self._installations.get(game_id)

        if not info:
            return InstallationStatus.NOT_INSTALLED

        return info.status

    def get_installation_info(self, game_or_id) -> Optional[InstallationInfo]:
        """Get full installation info for a game

        Args:
            game_or_id: Game object or game_id string

        Returns:
            InstallationInfo or None
        """
        if isinstance(game_or_id, str):
            game_id = game_or_id
        else:
            game_id = self._get_game_id(game_or_id)
        return self._installations.get(game_id)

    def save_game_settings(self, game_id: str, settings: Dict[str, Any]) -> None:
        """Save per-game settings

        Updates the settings in the installation info and persists to database.

        Args:
            game_id: Game identifier
            settings: Settings dictionary to save
        """
        info = self._installations.get(game_id)
        if info:
            info.settings.update(settings)
            if "platform_id" in settings:
                info.platform_id = settings["platform_id"]
            self._save_installation(info)
            logger.debug(f"Saved settings for game {game_id}")
        else:
            # Create minimal installation info for settings storage
            info = InstallationInfo(
                game_id=game_id,
                status=InstallationStatus.NOT_INSTALLED,
                settings=settings,
                platform_id=settings.get("platform_id"),
            )
            self._installations[game_id] = info
            self._save_installation(info)
            logger.debug(f"Created settings record for game {game_id}")

    def is_installed(self, game: "Game") -> bool:
        """Check if a game is installed

        Args:
            game: Game to check

        Returns:
            True if game is installed
        """
        status = self.get_installation_status(game)
        return status in (InstallationStatus.INSTALLED, InstallationStatus.UPDATE_AVAILABLE)

    # === Launching ===

    async def launch_game(
        self,
        game: "Game",
        platform_id: Optional[str] = None,
        **kwargs
    ) -> LaunchResult:
        """Launch a game

        Launch flow:
        1. Check installation status (for managed games)
        2. Select platform (or use specified)
        3. Delegate to RuntimeManager
        4. Update launch statistics

        Args:
            game: Game to launch
            platform_id: Specific platform to use
            **kwargs: Additional launch options

        Returns:
            LaunchResult with success status
        """
        game_id = self._get_game_id(game)
        title = getattr(game, "title", "Unknown")

        logger.info(f"Launching game: {title}")

        if not self._runtime_manager:
            return LaunchResult(
                success=False,
                game_id=game_id,
                error_message="Runtime manager not available",
            )

        # Get installation info if available
        info = self._installations.get(game_id)

        # Add install path to kwargs if available
        if info and info.install_path:
            kwargs.setdefault("game_path", info.install_path)

        # Use assigned platform if set
        if info and info.platform_id and not platform_id:
            platform_id = info.platform_id

        # Delegate to RuntimeManager
        result = await self._runtime_manager.launch_game(game, platform_id, **kwargs)

        # Update statistics on successful launch
        if result.success:
            self._update_launch_stats(game_id)

        return LaunchResult(
            success=result.success,
            game_id=game_id,
            platform_id=result.platform_id,
            error_message=result.error_message,
            process_id=result.process_id,
        )

    def _update_launch_stats(self, game_id: str) -> None:
        """Update launch statistics for a game

        Args:
            game_id: Game identifier
        """
        info = self._installations.get(game_id)
        if info:
            info.last_played_at = datetime.now()
            info.launch_count += 1
            self._save_installation(info)

    # === Settings ===

    def get_game_settings(self, game: "Game") -> Dict[str, Any]:
        """Get per-game settings

        Args:
            game: Game to get settings for

        Returns:
            Settings dict
        """
        game_id = self._get_game_id(game)
        info = self._installations.get(game_id)

        if info:
            return info.settings.copy()
        return {}

    def set_game_settings(self, game: "Game", settings: Dict[str, Any]) -> None:
        """Set per-game settings

        Args:
            game: Game to set settings for
            settings: Settings dict
        """
        game_id = self._get_game_id(game)
        info = self._installations.get(game_id)

        if not info:
            info = InstallationInfo(
                game_id=game_id,
                status=InstallationStatus.NOT_INSTALLED,
            )
            self._installations[game_id] = info

        info.settings = settings.copy()
        self._save_installation(info)

    def assign_platform(self, game: "Game", platform_id: str) -> bool:
        """Assign a platform to a game

        Args:
            game: Game to assign platform to
            platform_id: Platform identifier

        Returns:
            True if assignment succeeded
        """
        game_id = self._get_game_id(game)
        info = self._installations.get(game_id)

        if not info:
            info = InstallationInfo(
                game_id=game_id,
                status=InstallationStatus.NOT_INSTALLED,
            )
            self._installations[game_id] = info

        info.platform_id = platform_id
        self._save_installation(info)

        logger.debug(f"Assigned platform {platform_id} to game {game_id}")
        return True

    # === Helpers ===

    def _get_game_id(self, game: "Game") -> str:
        """Get unique identifier for a game

        Args:
            game: Game object

        Returns:
            Game ID string
        """
        if hasattr(game, "id") and game.id:
            return str(game.id)

        store_name = getattr(game, "store_name", "unknown")
        store_app_id = getattr(game, "store_app_id", "unknown")
        return f"{store_name}:{store_app_id}"

    def _update_installation_status(self, game_id: str, status: InstallationStatus) -> None:
        """Update installation status

        Args:
            game_id: Game identifier
            status: New status
        """
        info = self._installations.get(game_id)
        if info:
            info.status = status
            self._save_installation(info)
        else:
            self._installations[game_id] = InstallationInfo(
                game_id=game_id,
                status=status,
            )

    def get_stats(self) -> Dict[str, Any]:
        """Get game manager statistics

        Returns:
            Stats dict
        """
        status_counts = {}
        for info in self._installations.values():
            status = info.status.value
            status_counts[status] = status_counts.get(status, 0) + 1

        return {
            "total_tracked": len(self._installations),
            "by_status": status_counts,
            "total_size_bytes": sum(i.size_bytes for i in self._installations.values()),
        }


# Module-level singleton
_game_manager: Optional[GameManager] = None


def get_game_manager() -> GameManager:
    """Get or create the game manager singleton

    Returns:
        GameManager instance
    """
    global _game_manager
    if _game_manager is None:
        _game_manager = GameManager()
    return _game_manager
