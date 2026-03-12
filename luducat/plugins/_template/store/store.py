# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# store.py

"""Template Store Plugin for luducat

Copy this directory to create a new store plugin:
1. Copy _template/ to your_store/
2. Rename this file's class to YourStore
3. Update plugin.json with your store's metadata
4. Implement all abstract methods marked with TODO

Required implementations:
- store_name, display_name (properties)
- is_available, is_authenticated
- fetch_user_games, fetch_game_metadata
- launch_game, get_database_path

Optional but recommended:
- get_store_page_url, get_game_metadata, get_games_metadata_bulk
- get_screenshots_for_app, refresh_game_description
- on_enable, on_disable, on_sync_complete, close
- get_auth_status (for plugin config UI)
"""

import logging
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.base import AbstractGameStore, Game, PluginError

logger = logging.getLogger(__name__)


class TemplateStore(AbstractGameStore):
    """Template store plugin implementation

    Replace 'Template' with your store name (e.g., EpicStore, ItchStore).
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize the store plugin

        Args:
            config_dir: Plugin config directory (~/.config/luducat/plugins/{name}/)
            cache_dir: Plugin cache directory (~/.cache/luducat/plugins/{name}/)
            data_dir: Plugin data directory (~/.local/share/luducat/plugins-data/{name}/)
        """
        super().__init__(config_dir, cache_dir, data_dir)

        # Initialize your database connection (lazy - created on first use)
        self._db = None

        # Initialize API client if needed (lazy - created on first use)
        self._api_client = None

        # Cached launcher detection result
        self._detected_launcher: Optional[Dict[str, Any]] = None

        logger.debug(f"TemplateStore initialized: data_dir={data_dir}")

    # =========================================================================
    # REQUIRED PROPERTIES
    # =========================================================================

    @property
    def store_name(self) -> str:
        """Return unique store identifier (lowercase, no spaces)

        Used as database key and in launch URLs.
        Example: "steam", "gog", "epic"
        """
        # TODO: Return your store's unique identifier
        return "template"

    @property
    def display_name(self) -> str:
        """Return human-readable store name for UI display

        Example: "Steam", "GOG", "Epic Games"
        """
        # TODO: Return your store's display name
        return "Template Store"

    # =========================================================================
    # REQUIRED METHODS - Authentication
    # =========================================================================

    def is_available(self) -> bool:
        """Check if store client/service is accessible

        Return True if the store can be used on this system.
        Check for required dependencies, client installation, etc.
        """
        # TODO: Check if your store is available
        # Example: check if client is installed, required packages exist
        return True

    def is_authenticated(self) -> bool:
        """Check if user is currently authenticated

        Return True if valid credentials exist and aren't expired.
        """
        # TODO: Check your authentication status
        # Example: check for valid API key, tokens, cookies
        api_key = self.get_credential("api_key")
        return bool(api_key)

    async def authenticate(self) -> bool:
        """Perform authentication flow

        This is called when authentication is needed.
        For API key-based auth, this might just verify the key.
        For OAuth, this should start the OAuth flow.

        Returns:
            True if authentication successful
        """
        # TODO: Implement your authentication flow
        # Most stores use the plugin config dialog for auth setup
        return self.is_authenticated()

    def get_auth_status(self) -> tuple:
        """Get detailed authentication status for UI display

        Returns:
            Tuple of (is_authenticated: bool, status_message: str)
        """
        # TODO: Return meaningful status for your store
        if not self.is_authenticated():
            return False, "Not configured"
        return True, "Connected"

    # =========================================================================
    # REQUIRED METHODS - Game Data
    # =========================================================================

    async def fetch_user_games(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """Fetch list of game IDs owned by user

        This should return app/game IDs as strings.
        Use status_callback to report progress during long operations.
        Check cancel_check() between pages/batches to support cancellation.

        Args:
            status_callback: Optional callback(message) for progress updates
            cancel_check: Optional callback returning True if cancelled

        Returns:
            List of store-specific app IDs (as strings)
        """
        # TODO: Fetch user's owned games from your store's API
        if status_callback:
            status_callback("Fetching game library...")

        # Example:
        # owned_games = await self._api.get_owned_games()
        # return [str(game_id) for game_id in owned_games]

        raise NotImplementedError("fetch_user_games must be implemented")

    async def fetch_game_metadata(
        self,
        app_ids: List[str],
        download_images: bool = False
    ) -> List[Game]:
        """Fetch detailed metadata for given app IDs

        Args:
            app_ids: List of app IDs to fetch metadata for
            download_images: If True, download and cache images locally

        Returns:
            List of Game objects with metadata
        """
        # TODO: Fetch metadata for the given app IDs
        # Return Game objects (from luducat.plugins.base)

        games = []
        for app_id in app_ids:
            # Example: fetch from API or database
            # metadata = await self._api.get_game_details(app_id)
            # game = Game(
            #     store_name=self.store_name,
            #     store_app_id=app_id,
            #     title=metadata["title"],
            #     ...
            # )
            # games.append(game)
            pass

        raise NotImplementedError("fetch_game_metadata must be implemented")

    def launch_game(self, app_id: str) -> bool:
        """Launch game via platform launcher

        Use the native launcher protocol (steam://, heroic://, etc.)
        or open the store page as fallback.

        Args:
            app_id: Store-specific game ID

        Returns:
            True if launch was initiated
        """
        # TODO: Launch the game
        # Example using Qt:
        # from PySide6.QtCore import QUrl
        # from PySide6.QtGui import QDesktopServices
        # url = f"yourstore://launch/{app_id}"
        # return QDesktopServices.openUrl(QUrl(url))

        raise NotImplementedError("launch_game must be implemented")

    def get_database_path(self) -> Path:
        """Return path to plugin's catalog database

        This is where the plugin stores its game catalog data.
        """
        # TODO: Return path to your database file
        return self.data_dir / "catalog.db"

    # =========================================================================
    # OPTIONAL METHODS - Enhanced Functionality
    # =========================================================================

    def get_store_page_url(self, app_id: str) -> str:
        """Get URL to game's store page

        Used for "View on Store" links in UI.
        """
        # TODO: Return your store's URL format
        return f"https://store.example.com/game/{app_id}"

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a single game from plugin's database

        Called by game_service to populate UI with game details.

        Returns:
            Metadata dict with keys: title, short_description, description,
            header_image_url, cover_image_url, screenshots, release_date,
            developers, publishers, genres
        """
        # TODO: Query your database for game metadata
        return None

    def get_games_metadata_bulk(
        self,
        app_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games efficiently

        Called during cache refresh with potentially thousands of IDs.
        Return a dict mapping app_id -> metadata dict.
        """
        # TODO: Efficient bulk query for metadata
        result = {}
        for app_id in app_ids:
            meta = self.get_game_metadata(app_id)
            if meta:
                result[app_id] = meta
        return result

    def get_screenshots_for_app(self, app_id: str) -> List[str]:
        """Get screenshot URLs for a single app

        Used for lazy loading screenshots in detail view.

        Returns:
            List of screenshot URLs
        """
        # TODO: Return screenshot URLs from your database
        return []

    def get_game_description(self, app_id: str) -> str:
        """Get description for a single game (lazy loading)

        Called when UI needs to display a game's description.
        Fetch from your local database, NOT from API (too slow).

        Returns:
            HTML description string, or empty string if not found
        """
        # TODO: Query your database for description
        # Example:
        # db = self._get_db()
        # game = db.get_game(app_id)
        # return game.description if game else ""
        return ""

    async def download_game_images(self, app_id: str) -> bool:
        """Download images for a single game (lazy loading)

        Called when UI needs to display a game's images but they're
        not cached locally. Download header, cover, background, and
        screenshot images to self.cache_dir.

        Returns:
            True if images were downloaded successfully
        """
        # TODO: Implement image downloading
        # Example:
        # import aiohttp
        # async with aiohttp.ClientSession() as session:
        #     metadata = self.get_game_metadata(app_id)
        #     if metadata and metadata.get("header_image_url"):
        #         # Download and save to cache_dir
        #         pass
        return False

    # =========================================================================
    # LIFECYCLE HOOKS
    # =========================================================================

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings

        Use this to initialize resources, create database tables, etc.
        """
        logger.info(f"{self.display_name} plugin enabled")

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings

        Use this to clean up resources, close connections, etc.
        """
        logger.info(f"{self.display_name} plugin disabled")
        self.close()

    def on_sync_complete(self, progress_callback=None) -> Dict[str, Any]:
        """Called after sync completes for this store

        Use this for post-sync cleanup, repairs, or statistics.

        Returns:
            Dict with any stats to merge into sync results
        """
        return {}

    def close(self) -> None:
        """Called when application is shutting down

        Clean up database connections, threads, etc.
        """
        if self._db:
            self._db.close()
            self._db = None
        self._api_client = None
        logger.debug(f"{self.display_name} plugin closed")

    # =========================================================================
    # HELPER METHODS (add your own as needed)
    # =========================================================================

    def _get_db(self):
        """Get or create database connection"""
        # TODO: Initialize your database
        # from .database import YourDatabase
        # if not self._db:
        #     self._db = YourDatabase(self.get_database_path())
        # return self._db
        pass

    def _get_api_client(self):
        """Get or create API client"""
        # TODO: Initialize your API client
        # if not self._api_client:
        #     self._api_client = YourApiClient(self)
        # return self._api_client
        pass

    # =========================================================================
    # LAUNCHER DETECTION (optional but recommended)
    # =========================================================================

    def _detect_launcher(self) -> Optional[Dict[str, Any]]:
        """Detect which launcher to use for this store

        This pattern is used by GOG plugin to detect Heroic vs Galaxy.
        Cache the result to avoid repeated filesystem checks.

        Returns:
            Dict with launcher info: {"type": str, "path": Path, "name": str}
            or None if no launcher found
        """
        if self._detected_launcher is not None:
            return self._detected_launcher

        # TODO: Implement launcher detection for your store
        # Example (GOG pattern):
        #
        # if sys.platform == "win32":
        #     launcher = self._detect_launcher_windows()
        # elif sys.platform == "darwin":
        #     launcher = self._detect_launcher_macos()
        # else:
        #     launcher = self._detect_launcher_linux()
        #
        # self._detected_launcher = launcher
        # return launcher

        return None

    def _detect_launcher_linux(self) -> Optional[Dict[str, Any]]:
        """Detect launcher on Linux

        Common patterns:
        - Flatpak: ~/.var/app/com.example.Launcher/
        - AppImage: ~/Applications/*.AppImage
        - Native: /usr/bin/launcher, ~/.local/bin/launcher
        """
        # TODO: Implement Linux launcher detection
        # Example:
        # flatpak_path = Path.home() / ".var" / "app" / "com.example.Launcher"
        # if flatpak_path.exists():
        #     return {"type": "launcher", "path": flatpak_path, "name": "Launcher (Flatpak)"}
        return None

    def _detect_launcher_windows(self) -> Optional[Dict[str, Any]]:
        """Detect launcher on Windows

        Common patterns:
        - Registry keys for install path
        - Program Files / Program Files (x86)
        - %LOCALAPPDATA% for user-installed apps
        """
        # TODO: Implement Windows launcher detection
        # Example:
        # import winreg
        # try:
        #     key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Example\Launcher")
        #     path, _ = winreg.QueryValueEx(key, "InstallPath")
        #     return {"type": "launcher", "path": Path(path), "name": "Example Launcher"}
        # except (FileNotFoundError, OSError):
        #     pass
        return None

    def _detect_launcher_macos(self) -> Optional[Dict[str, Any]]:
        """Detect launcher on macOS

        Common patterns:
        - /Applications/Launcher.app
        - ~/Applications/Launcher.app
        """
        # TODO: Implement macOS launcher detection
        # Example:
        # app_path = Path("/Applications/Launcher.app")
        # if app_path.exists():
        #     return {"type": "launcher", "path": app_path, "name": "Launcher"}
        return None
