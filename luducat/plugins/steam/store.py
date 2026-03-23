# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# store.py

"""Steam store plugin for luducat

Wraps the steamscraper module to implement AbstractGameStore interface.
"""

import asyncio
from luducat.plugins.sdk.json import json
import logging
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread

from luducat.plugins.base import (
    AbstractGameStore,
    AuthenticationError,
    Game,
    NetworkError,
    PluginError,
    RateLimitError,
)
from luducat.plugins.sdk.network import RequestException, RequestTimeout

# Import family sharing API
from .family_api import SharedApp, FamilyGroup, fetch_family_shared_games
from .family_sharing import find_steam_path, get_family_shared_games
from .vdf_scanner import parse_login_users, parse_user_config, scan_installed_games

# Import the steamscraper module
from .steamscraper import SteamGameManager
from .steamscraper.database import Database as ScraperDatabase, Game as ScraperGame
from .steamscraper.exceptions import (
    RateLimitExceededError,
    SteamScraperException,
)
logger = logging.getLogger(__name__)


class SteamStore(AbstractGameStore):
    """Steam store integration using steamscraper module

    This plugin wraps the steamscraper module to provide Steam library
    access through the luducat plugin interface.

    Authentication:
        Requires a Steam Web API key from https://steamcommunity.com/dev/apikey
        The API key is stored securely in the system keyring.

    Usage:
        The plugin uses the steamscraper.SteamGameManager for all operations.
        Game metadata is cached in the plugin's SQLite database.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize Steam store plugin

        Args:
            config_dir: Plugin config directory
            cache_dir: Plugin cache directory (for images)
            data_dir: Plugin data directory (for database)
        """
        super().__init__(config_dir, cache_dir, data_dir)

        self._manager: Optional[SteamGameManager] = None
        self._steam_id: Optional[str] = None
        # Track which app IDs are family shared and their owners
        # Maps app_id -> owner_steamid (or None if owner unknown)
        self._family_shared_apps: Dict[str, Optional[str]] = {}
        self._family_sharing_warning: Optional[str] = None
        self._title_index: Optional[Dict[str, int]] = None
        # Playtime data from last fetch_user_games() call, consumed by get_playtime_sync_data()
        self._playtime_cache: Optional[Dict[str, Dict[str, Any]]] = None

    @property
    def store_name(self) -> str:
        return "steam"

    @property
    def display_name(self) -> str:
        return "Steam"

    def is_available(self) -> bool:
        """Check if Steam client is installed.

        Uses centralized app_finder for cross-platform detection (PATH,
        registry, Flatpak, AppImage, macOS bundles), plus Steam-specific
        data directory checks on Linux.

        Returns:
            True if Steam appears to be installed
        """
        from luducat.plugins.sdk.app_finder import find_application

        results = find_application(
            ["steam"],
            flatpak_ids=["com.valvesoftware.Steam"],
        )
        if results:
            return True

        # Steam-specific: data directories exist even if binary isn't in PATH
        # (e.g. Steam installed via .deb but binary at /usr/games/steam)
        if platform.system() == "Linux":
            for data_dir in (
                Path.home() / ".steam" / "steam",
                Path.home() / ".local" / "share" / "Steam",
            ):
                if data_dir.exists():
                    return True

        return False

    def _get_steam_path_setting(self) -> str:
        """Get steam_path from settings, checking bridge config as fallback.

        The steam_path setting was moved from the Steam store plugin to the
        Steam Bridge plugin. This method checks both locations for backward
        compatibility and migration.

        Returns:
            Steam path string, or empty string if not set
        """
        # Check own settings first (legacy / migrated)
        path = self.get_setting("steam_path", "")
        if path:
            return path
        # Check Steam Bridge settings via config
        try:
            from luducat.plugins.sdk.config import get_config_value
            path = get_config_value("plugins.steam_bridge.steam_path", "")
            if path:
                return path
        except Exception:
            pass
        return ""

    def _get_manager(self) -> SteamGameManager:
        """Get or create the SteamGameManager instance

        Returns:
            Initialized SteamGameManager

        Raises:
            PluginError: If API key is not configured
        """
        if self._manager is not None:
            return self._manager

        api_key = self.get_credential("api_key")
        if not api_key:
            raise PluginError(
                "Steam API key not configured. "
                "Get your key from https://steamcommunity.com/dev/apikey"
            )

        # Initialize manager with plugin directories
        self._manager = SteamGameManager(
            db_path=str(self.data_dir / "catalog.db"),
            cache_dir=str(self.cache_dir),
            api_key=api_key,
            http_client=self.http,
        )

        return self._manager

    async def authenticate(self) -> bool:
        """Verify Steam API key is valid

        For Steam, authentication is just validating the API key works.
        The actual authentication with Steam's servers is handled by
        the Steam client.

        Returns:
            True if API key is valid
        """
        api_key = self.get_credential("api_key")

        if not api_key:
            # No API key stored - cannot authenticate
            raise AuthenticationError(
                "Steam API key not configured. "
                "Please set your API key in Settings > Plugins > Steam"
            )

        # Validate the API key by calling an endpoint that requires it
        # We use ISteamUser/GetPlayerSummaries which requires a valid API key
        try:
            # Use a well-known public Steam ID (Valve's Robin Walker)
            test_steam_id = "76561197960435530"
            url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
            params = {
                "key": api_key,
                "steamids": test_steam_id
            }

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.http.get(url, params=params, timeout=10)
            )

            if response.status_code == 403:
                raise AuthenticationError(
                    "Invalid Steam API key. "
                    "Get a valid key from https://steamcommunity.com/dev/apikey"
                )
            elif response.status_code == 401:
                raise AuthenticationError("Steam API key is unauthorized")
            elif response.status_code != 200:
                raise AuthenticationError(
                    f"Steam API error: HTTP {response.status_code}"
                )

            # Check response has expected structure
            data = response.json()
            if "response" not in data:
                raise AuthenticationError("Invalid API response - key may be invalid")

            logger.info("Steam API key validated successfully")
            return True

        except RequestTimeout as e:
            raise AuthenticationError("Steam API request timed out") from e
        except RequestException as e:
            raise AuthenticationError(f"Network error: {e}") from e
        except AuthenticationError:
            raise
        except Exception as e:
            raise AuthenticationError(f"Authentication failed: {e}") from e

    def is_authenticated(self) -> bool:
        """Check if we have both API key and Steam ID.

        Returns:
            True if API key exists in keyring AND steam_id is configured
        """
        api_key = self.get_credential("api_key")
        if not api_key:
            return False
        steam_id = self.get_setting("steam_id") or self.get_credential("steam_id")
        return bool(steam_id)

    def get_account_identifier(self) -> Optional[str]:
        """Return the Steam64 ID identifying the current account."""
        return self.get_setting("steam_id") or self.get_credential("steam_id")

    def get_auth_status(self) -> tuple:
        """Get authentication status with details

        Returns:
            Tuple of (is_authenticated: bool, status_message: str)
        """
        api_key = self.get_credential("api_key")
        steam_id = self.get_setting("steam_id") or self.get_credential("steam_id")

        if not api_key:
            return False, _("Not connected")

        if not steam_id:
            return False, _("Steam ID not configured")

        # Try to resolve persona name (cached to avoid repeated lookups)
        if not hasattr(self, '_cached_persona_name'):
            self._cached_persona_name = None

        if self._cached_persona_name is None:
            # 1. Try VDF (local file, needs consent)
            if self.has_local_data_consent():
                try:
                    steam_path = find_steam_path(self.get_setting("steam_path"))
                    if steam_path:
                        users = parse_login_users(steam_path)
                        for user in users:
                            if user.get("steam_id") == steam_id:
                                name = user.get("persona_name", "")
                                if name:
                                    self._cached_persona_name = name
                                    break
                except Exception:
                    pass

            # 2. Try API if VDF didn't work
            if self._cached_persona_name is None:
                try:
                    name_map = self._resolve_steam_names([steam_id])
                    name = name_map.get(steam_id, "")
                    if name:
                        self._cached_persona_name = name
                except Exception:
                    pass

        if self._cached_persona_name:
            return True, _("Connected as {name}").format(name=self._cached_persona_name)
        return True, _("Connected")

    def steam_login_action(self) -> Optional[str]:
        """Field action callback: detect Steam ID from VDF or browser cookies.

        Called by the inline [Login...] button in the settings dialog.
        Returns a Steam64 ID string if found, or None (dialog opens browser).
        """
        # 1. Try VDF for logged-in user
        if self.has_local_data_consent():
            try:
                steam_path = find_steam_path(self.get_setting("steam_path"))
                if steam_path:
                    users = parse_login_users(steam_path)
                    if users:
                        # Return the most recently logged-in user's Steam ID
                        return users[0].get("steam_id")
            except Exception as e:
                logger.debug("VDF Steam ID detection failed: %s", e)

        # 2. Try browser cookies (check if Steam community session exists)
        if self.has_local_data_consent():
            try:
                from luducat.plugins.sdk.cookies import get_browser_cookie_manager
                cookie_mgr = get_browser_cookie_manager()
                if not cookie_mgr:
                    raise RuntimeError("Browser cookie manager not available")
                cookies = cookie_mgr.get_cookies("steamcommunity.com")
                if cookies:
                    steam_login = cookies.get("steamLoginSecure", "")
                    if steam_login and "%" in steam_login:
                        # steamLoginSecure = steamid%7C%7Ctoken
                        steam_id = steam_login.split("%")[0]
                        if steam_id.isdigit() and len(steam_id) == 17:
                            return steam_id
            except Exception as e:
                logger.debug("Browser cookie Steam ID detection failed: %s", e)

        return None

    def resolve_vanity_url(self, vanity_name: str) -> Optional[str]:
        """Resolve a Steam vanity URL name to a Steam64 ID.

        Delegates to the steamscraper API client.

        Args:
            vanity_name: The vanity URL part (e.g., "gabelogannewell")

        Returns:
            Steam64 ID string on success, None otherwise
        """
        try:
            manager = self._get_manager()
            return manager.api_client.resolve_vanity_url(vanity_name)
        except Exception as e:
            logger.debug(f"Vanity URL resolution failed for '{vanity_name}': {e}")
            return None

    async def fetch_user_games(
        self,
        status_callback: Optional[Any] = None,
        cancel_check: Optional[Any] = None,
    ) -> List[str]:
        """Fetch list of owned game IDs from Steam API

        This fetches all owned games in a single API call. Steam's API
        returns all games at once (no pagination), which works fine even
        for large libraries (10k+ games).

        Args:
            status_callback: Optional callback (unused - Steam returns all at once)
            cancel_check: Optional callback returning True if cancelled

        Returns:
            List of Steam app IDs (as strings)

        Raises:
            AuthenticationError: If not authenticated
            NetworkError: If API request fails
        """
        if not self.is_authenticated():
            raise AuthenticationError("Not authenticated")

        steam_id = self.get_setting("steam_id") or self.get_credential("steam_id")
        if not steam_id:
            raise PluginError(
                "Steam ID not configured. "
                "Please set your Steam ID in Settings > Plugins > Steam"
            )

        api_key = self.get_credential("api_key")

        try:
            # Fetch owned games from Steam API
            # This returns ALL games in one response (tested with 15k+ libraries)
            url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
            params = {
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": 1,  # Include name so we have basic info
                "skip_unvetted_apps": 0,
                "include_played_free_games": 1,
            }

            logger.info(f"Fetching owned games for Steam ID {steam_id}...")

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.http.get(url, params=params, timeout=60)
            )

            if response.status_code == 401:
                raise AuthenticationError("Steam API key is invalid")
            elif response.status_code == 403:
                raise PluginError(
                    "Cannot access game library. Make sure your Steam profile "
                    "game details are set to Public in Privacy Settings."
                )
            elif response.status_code != 200:
                raise NetworkError(f"Steam API error: HTTP {response.status_code}")

            data = response.json()
            games = data.get("response", {}).get("games", [])

            if not games:
                logger.warning(
                    "No games found. Make sure your Steam profile game details "
                    "are set to Public, or check your Steam ID is correct."
                )
                return []

            # Store basic game info in our database for immediate display
            # Full metadata will be fetched incrementally
            self._store_basic_game_info(games)

            # Cache playtime data for get_playtime_sync_data() (called by sync pipeline)
            self._build_playtime_cache(games)

            app_ids = [str(game["appid"]) for game in games]
            owned_app_ids = set(app_ids)
            logger.info(f"Found {len(app_ids)} owned games from Steam API")

            # Clear family shared tracking from previous syncs
            self._family_shared_apps = {}

            # Check if family sharing is enabled (skip if cancelled)
            include_family_shared = self.get_setting("include_family_shared")
            if cancel_check and cancel_check():
                return app_ids
            if include_family_shared:
                logger.debug("Family sharing enabled, fetching borrowed games for %s", steam_id)
                # Get borrowed games (excludes owned games) - API first, legacy fallback
                borrowed_apps = self._get_family_shared_games(steam_id, owned_app_ids)
                if borrowed_apps:
                    self._family_shared_apps = borrowed_apps
                    app_ids.extend(borrowed_apps.keys())
                    logger.info(f"Added {len(borrowed_apps)} borrowed family shared games")
                else:
                    logger.debug("Family sharing: no borrowed games found")
            else:
                logger.debug(
                    "Family sharing disabled "
                    "(include_family_shared=%s)",
                    include_family_shared,
                )

            return app_ids

        except RequestTimeout as e:
            raise NetworkError("Steam API request timed out") from e
        except RequestException as e:
            raise NetworkError(f"Network error: {e}") from e
        except (AuthenticationError, PluginError, NetworkError):
            raise
        except Exception as e:
            raise NetworkError(f"Failed to fetch user games: {e}") from e

    def _store_basic_game_info(self, games: List[dict]) -> None:
        """Store basic game info from GetOwnedGames response

        This gives us immediate access to game names and playtime
        without needing to fetch full metadata for each game.

        Args:
            games: List of game dicts from Steam API
        """
        try:
            manager = self._get_manager()
            session = manager.database.get_session()
            from .steamscraper.database import Game as ScraperGame

            count = 0
            for game_data in games:
                appid = game_data.get("appid")
                if not appid:
                    continue

                # Check if game already exists
                existing = session.query(ScraperGame).filter_by(appid=appid).first()
                if existing:
                    # Update playtime if we have it
                    if "playtime_forever" in game_data:
                        existing.average_playtime_forever = game_data["playtime_forever"]
                    continue

                # Create new game entry with basic info
                game = ScraperGame(
                    appid=appid,
                    name=game_data.get("name", f"App {appid}"),
                    average_playtime_forever=game_data.get("playtime_forever", 0),
                )
                session.add(game)
                count += 1

                # Commit in batches to avoid memory issues
                if count % 500 == 0:
                    session.commit()
                    logger.debug(f"Stored {count} games...")

            session.commit()
            logger.info(f"Stored basic info for {count} new games")

        except Exception as e:
            logger.error(f"Failed to store basic game info: {e}")
            # Don't raise - this is optional enhancement

    def _build_playtime_cache(self, games: List[dict]) -> None:
        """Build playtime cache from Steam API GetOwnedGames response.

        Stores playtime data for consumption by get_playtime_sync_data().
        Called during fetch_user_games() after receiving API response.

        Args:
            games: List of game dicts from Steam API GetOwnedGames
        """
        from datetime import datetime

        self._playtime_cache = {}
        for game_data in games:
            appid = game_data.get("appid")
            if not appid:
                continue

            playtime_minutes = game_data.get("playtime_forever", 0)
            rtime_last_played = game_data.get("rtime_last_played", 0)

            if playtime_minutes <= 0 and rtime_last_played <= 0:
                continue

            last_played = None
            if rtime_last_played > 0:
                try:
                    last_played = datetime.fromtimestamp(rtime_last_played).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except (OSError, ValueError):
                    pass

            self._playtime_cache[str(appid)] = {
                "minutes": playtime_minutes,
                "last_played": last_played,
            }

        logger.debug(f"Built playtime cache: {len(self._playtime_cache)} games with playtime")

    def get_playtime_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return playtime data from the most recent Steam API fetch.

        Returns cached data from _build_playtime_cache() (populated during
        fetch_user_games). Cache is consumed once and cleared.

        Returns:
            Dict mapping appid -> {"minutes": int, "last_played": str|None}
            or None if no playtime data available.
        """
        data = self._playtime_cache
        self._playtime_cache = None  # Consume once
        if data:
            logger.info(f"Steam playtime sync: {len(data)} games with playtime")
        return data

    def _get_family_shared_games(
        self, steam_id: str, owned_app_ids: set,
    ) -> Dict[str, Optional[str]]:
        """Get borrowed games via family sharing with owner info.

        Tries the Steam API first (provides owner info), falls back to
        legacy VDF parsing (no owner info) if API fails.

        Args:
            steam_id: Steam64 ID
            owned_app_ids: Set of app IDs the user owns (to exclude)

        Returns:
            Dict mapping app_id -> owner_steamid (or None if owner unknown)
        """
        self._family_sharing_warning = None
        logger.debug("Family sharing: querying API for steam_id=%s (%d owned games excluded)",
                     steam_id, len(owned_app_ids))
        # Try Steam API first (provides rich data including owner info)
        try:
            borrowed_apps, family_group, license_counts = fetch_family_shared_games(
                steam_id, http_client=self.http,
            )
            self._family_license_counts = license_counts
            if license_counts:
                logger.debug(
                    "Family license counts: %d apps, max owners=%d",
                    len(license_counts),
                    max(license_counts.values()) if license_counts else 0,
                )
            if borrowed_apps:
                # Cache family group info in plugin settings for UI display
                if family_group:
                    self._cache_family_group(family_group)

                # Build dict mapping app_id -> comma-separated owner SteamIDs
                result = {}
                for app in borrowed_apps:
                    app_id = str(app.appid)
                    # Skip if user owns this game
                    if app_id in owned_app_ids:
                        continue
                    # Store all owners as comma-separated string
                    owners = ",".join(app.owner_steamids) if app.owner_steamids else None
                    result[app_id] = owners

                    # Store basic info for borrowed games (they need names too)
                    self._store_borrowed_game_info(app)

                if result:
                    logger.info(f"API: Found {len(result)} borrowed games with owner info")
                    return result
        except Exception as e:
            logger.info("Family sharing API failed: %s", e)

        # Fall back to legacy VDF parsing (no owner info)
        logger.info("Falling back to legacy VDF method for family sharing")
        try:
            steam_path = self._get_steam_path_setting()
            app_ids, fetch_time = get_family_shared_games(
                steam_id,
                owned_app_ids=owned_app_ids,
                steam_path=steam_path,
            )
            # Legacy method doesn't provide owner info
            result = {app_id: None for app_id in app_ids}
            logger.info("VDF fallback: found %d family shared games", len(result))
            self._family_sharing_warning = "vdf_fallback"
            return result
        except Exception as e:
            logger.warning(f"Legacy family sharing also failed: {e}")
            self._family_sharing_warning = "both_failed"
            return {}

    def get_family_license_data(self) -> Dict[str, int]:
        """Return per-app license counts from the last family sharing fetch.

        Returns:
            Dict mapping app_id (str) -> number of family members who own it.
            Empty dict if family sharing was not fetched or failed.
        """
        return getattr(self, "_family_license_counts", {})

    def _resolve_steam_names(self, steamids: List[str]) -> Dict[str, str]:
        """Resolve Steam IDs to display names via ISteamUser/GetPlayerSummaries.

        Args:
            steamids: List of Steam64 IDs to resolve

        Returns:
            Dict mapping steamid -> personaname (display name)
        """
        if not steamids:
            return {}

        logger.debug(f"Trying to resolve SteamID(s) {steamids} to display names")

        api_key = self.get_credential("api_key")
        if not api_key:
            logger.debug("No API key available for name resolution")
            return {}

        try:
            # API supports up to 100 IDs per request
            url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
            params = {
                "key": api_key,
                "steamids": ",".join(steamids)
            }

            response = self.http.get(url, params=params, timeout=10)

            if response.status_code != 200:
                logger.debug(f"GetPlayerSummaries failed: {response.status_code}")
                return {}

            data = response.json()
            players = data.get("response", {}).get("players", [])

            result = {}
            for player in players:
                sid = player.get("steamid")
                name = player.get("personaname")
                if sid and name:
                    result[sid] = name
                    logger.debug(f"Resolved {sid} -> {name}")

            logger.info(f"Resolved {len(result)}/{len(steamids)} Steam names")
            return result

        except Exception as e:
            logger.debug(f"Failed to resolve Steam names: {e}")
            return {}

    def _cache_family_group(self, family_group: FamilyGroup) -> None:
        """Cache family group info in plugin settings.

        Args:
            family_group: FamilyGroup object from API
        """
        try:
            # Resolve Steam IDs to display names
            steamids = [m.steamid for m in family_group.members]
            name_map = self._resolve_steam_names(steamids)
            logger.debug(f"Family group IDs: {steamids}")

            # Build member list with resolved names
            members = []
            for member in family_group.members:
                # Use resolved name, fall back to API nickname or steamid
                nickname = name_map.get(member.steamid) or member.nickname
                members.append({
                    "steamid": member.steamid,
                    "role": member.role,
                    "nickname": nickname,
                })

            group_data = {
                "groupid": family_group.groupid,
                "name": family_group.name,
                "members": members,
                "free_spots": family_group.free_spots,
            }

            logger.debug(f"Family group data: {group_data}")
            for m in family_group.members:
                nick = name_map.get(m.steamid, m.steamid)
                logger.debug(
                    "  Member %s (%s): role=%d, joined=%d, cooldown=%d",
                    nick, m.steamid, m.role, m.time_joined,
                    m.cooldown_seconds_remaining,
                )

            # Update settings in-place to preserve plugin manager reference
            self._settings["family_group"] = group_data

            logger.info(f"Cached family group '{family_group.name}' with {len(members)} members")
        except Exception as e:
            logger.debug(f"Failed to cache family group: {e}")

    def _store_borrowed_game_info(self, app: SharedApp) -> None:
        """Store basic info for a borrowed game from API response.

        Args:
            app: SharedApp object from API with name and other info
        """
        manager = self._get_manager()
        session = manager.database.get_session()
        try:
            from .steamscraper.database import Game as ScraperGame

            # Check if game already exists
            existing = session.query(ScraperGame).filter_by(appid=app.appid).first()
            if existing:
                return

            # Create new game entry with info from API
            game = ScraperGame(
                appid=app.appid,
                name=app.name,
            )
            session.add(game)
            session.commit()
            logger.debug(f"Stored borrowed game: {app.name} ({app.appid})")
        except Exception as e:
            session.rollback()
            logger.debug(f"Failed to store borrowed game {app.appid}: {e}")
        finally:
            session.close()

    def get_family_member_name(self, steamid: str) -> str:
        """Get display name for a family member by their steamid.

        Args:
            steamid: Steam64 ID of the family member

        Returns:
            Member's nickname if known, otherwise the steamid
        """
        family_group = self._settings.get("family_group", {})
        members = family_group.get("members", [])
        for member in members:
            if member.get("steamid") == steamid:
                nickname = member.get("nickname")
                if nickname:
                    return nickname
        return steamid

    async def fetch_game_metadata(
        self, app_ids: List[str], download_images: bool = False
    ) -> List[Game]:
        """Fetch detailed metadata for games

        Args:
            app_ids: List of Steam app IDs
            download_images: If True, download images (default False for fast sync)

        Returns:
            List of Game objects with metadata
        """
        if not self.is_authenticated():
            raise AuthenticationError("Not authenticated")

        try:
            manager = self._get_manager()
            loop = asyncio.get_event_loop()

            # Convert to ints for steamscraper
            int_ids = [int(aid) for aid in app_ids]

            # Fetch games using bulk method - no image download during sync
            scraper_games = await loop.run_in_executor(
                None,
                lambda: manager.get_games_bulk(int_ids, download_images=download_images),
            )

            # Convert to plugin Game format
            games = []
            for appid, sg in scraper_games.items():
                # Check if this game is family shared and get owner
                app_id_str = str(appid)
                if app_id_str in self._family_shared_apps:
                    owner = self._family_shared_apps[app_id_str]
                    game = self._convert_game(sg, family_shared=1, family_shared_owner=owner)
                else:
                    game = self._convert_game(sg, family_shared=0, family_shared_owner=None)
                if game:
                    games.append(game)

            return games

        except RateLimitExceededError as e:
            raise RateLimitError(str(e), wait_seconds=e.wait_seconds, reason=e.reason) from e
        except SteamScraperException as e:
            logger.error(f"Failed to fetch game metadata: {e}")
            raise NetworkError(f"Failed to fetch game metadata: {e}") from e

    @staticmethod
    def _safe_json_field(value, field_name: str, default=None):
        """Parse JSON field if it's somehow still a string.

        Column(JSON) should handle deserialization automatically, so the
        isinstance(str) guard should never trigger.  If it does, a debug
        message is logged to help track down the root cause.
        """
        if default is None:
            default = []
        if not value:
            return default
        if isinstance(value, str):
            logger.debug("Unexpected string in JSON column '%s' — parsing", field_name)
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return [value] if isinstance(default, list) and value else default
        return value

    def _convert_game(
        self,
        scraper_game,
        family_shared: int = 0,
        family_shared_owner: Optional[str] = None
    ) -> Optional[Game]:
        """Convert steamscraper Game to plugin Game format

        Args:
            scraper_game: Game object from steamscraper
            family_shared: Family sharing status (0=owned, 1=borrowed)
            family_shared_owner: SteamID of owner for borrowed games

        Returns:
            Plugin Game object or None if conversion fails
        """
        try:
            # Parse JSON fields (Column(JSON) should auto-deserialize, helper
            # provides defensive fallback with diagnostic logging)
            screenshots = self._safe_json_field(scraper_game.screenshots, "screenshots")
            developers = self._safe_json_field(scraper_game.developers, "developers")
            publishers = self._safe_json_field(scraper_game.publishers, "publishers")
            genres = self._safe_json_field(scraper_game.genres, "genres")
            categories = self._safe_json_field(scraper_game.categories, "categories")

            return Game(
                store_app_id=str(scraper_game.appid),
                store_name="steam",
                title=scraper_game.name or f"App {scraper_game.appid}",
                launch_url=f"steam://rungameid/{scraper_game.appid}",
                short_description=scraper_game.short_description,
                description=scraper_game.detailed_description or scraper_game.about_the_game,
                header_image_url=scraper_game.header_image,
                # Cover image: prefer 2x (1200x1800) over 1x (600x900)
                cover_image_url=(
                    scraper_game.library_capsule_2x
                    or scraper_game.library_capsule
                    or ""
                ),
                background_image_url=(
                    scraper_game.library_hero
                    or scraper_game.background_image
                    or ""
                ),
                screenshots=screenshots,
                release_date=scraper_game.release_date,
                publishers=publishers,
                developers=developers,
                genres=genres,
                categories=categories,
                playtime_minutes=scraper_game.average_playtime_forever,
                family_shared=family_shared,
                family_shared_owner=family_shared_owner,
            )

        except Exception as e:
            logger.error(f"Failed to convert game {scraper_game.appid}: {e}")
            return None

    def _probe_missing_library_assets(self, scraper_game) -> None:
        """Probe and store library assets for games missing them.

        Called for games synced before library asset probing was added.
        Updates the scraper database and refreshes the scraper_game object.

        Args:
            scraper_game: Game object from steamscraper that needs asset probing
        """
        try:
            db_path = self.data_dir / "catalog.db"
            manager = SteamGameManager(
                db_path=str(db_path),
                api_key=self.get_credential("api_key"),
            )

            appid = scraper_game.appid

            # Get the game from manager's own session
            session = manager.database.get_session()
            from .steamscraper.database import Game as ScraperGame
            db_game = session.query(ScraperGame).filter_by(appid=appid).first()

            if not db_game:
                logger.warning(f"Game {appid} not found in scraper database")
                return

            # Probe and store library assets
            logger.info(f"Probing library assets for game {appid} (missing library_capsule)")
            found_assets = manager.probe_and_store_library_assets(
                appid,
                game=db_game,
                session=session
            )

            # Commit changes to database
            session.commit()

            # Copy probed URLs back to the original scraper_game object
            scraper_game.library_capsule = found_assets.get('library_capsule')
            scraper_game.library_capsule_2x = found_assets.get('library_capsule_2x')
            scraper_game.library_hero = found_assets.get('library_hero')
            scraper_game.main_capsule = found_assets.get('main_capsule')

            logger.debug(
                f"Library assets probed for {appid}: "
                f"capsule={scraper_game.library_capsule is not None}, "
                f"hero={scraper_game.library_hero is not None}"
            )

        except Exception as e:
            logger.warning(f"Failed to probe library assets for {scraper_game.appid}: {e}")

    def repair_library_assets(self, progress_callback=None) -> dict:
        """Probe and update library assets for all games missing them.

        This repairs games that were synced before library asset probing was added.
        Should be called after sync or as a manual repair operation.

        Args:
            progress_callback: Optional callback(game_name, current, total)

        Returns:
            Dict with repair stats: {probed: int, updated: int, failed: int}
        """
        stats = {"probed": 0, "updated": 0, "failed": 0}

        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return stats

        try:
            manager = SteamGameManager(
                db_path=str(db_path),
                api_key=self.get_credential("api_key"),
            )
            session = manager.database.get_session()

            from .steamscraper.database import Game as ScraperGame

            # Find all games with header_image but no library_capsule
            games_needing_repair = session.query(ScraperGame).filter(
                ScraperGame.header_image.isnot(None),
                ScraperGame.library_capsule.is_(None),
            ).all()

            total = len(games_needing_repair)
            if total == 0:
                logger.info("No games need library asset repair")
                return stats

            logger.info(f"Repairing library assets for {total} games")

            BATCH_SIZE = 10  # Commit every N games

            for idx, game in enumerate(games_needing_repair, 1):
                try:
                    if progress_callback:
                        progress_callback(game.name or f"App {game.appid}", idx, total)

                    # Probe library assets and update database directly
                    found_assets = manager.probe_and_store_library_assets(
                        game.appid,
                        game=game,
                        session=session
                    )
                    stats["probed"] += 1

                    if found_assets.get('library_capsule'):
                        stats["updated"] += 1

                    # Commit in batches to ensure progress is saved
                    if idx % BATCH_SIZE == 0:
                        session.commit()
                        logger.debug(f"Committed batch at {idx}/{total}")

                except Exception as e:
                    stats["failed"] += 1
                    logger.warning(f"Failed to repair library assets for {game.appid}: {e}")

            # Final commit for remaining games
            session.commit()
            logger.debug("Final commit to database")

            # Now update the main database with new cover URLs
            self._update_main_db_cover_urls(games_needing_repair)

            logger.info(
                f"Library asset repair complete: {stats['probed']} probed, "
                f"{stats['updated']} updated, {stats['failed']} failed"
            )

        except Exception as e:
            logger.error(f"Library asset repair failed: {e}")

        return stats

    def _update_main_db_cover_urls(self, repaired_games) -> None:
        """Update main database with new cover URLs from repaired games.

        Args:
            repaired_games: List of steamscraper Game objects with updated library assets
        """
        if not self.main_db:
            logger.warning("MainDbAccessor not available, skipping cover URL update")
            return

        try:
            patches_by_id = {}
            for game in repaired_games:
                if not game.library_capsule:
                    continue
                new_cover_url = game.library_capsule_2x or game.library_capsule or ""
                if new_cover_url:
                    patches_by_id[str(game.appid)] = {"cover": new_cover_url}

            if patches_by_id:
                updated = self.main_db.batch_patch_metadata_json(patches_by_id)
                logger.info(f"Updated {updated} cover URLs in main database")

        except Exception as e:
            logger.error(f"Failed to update main database cover URLs: {e}")

    def _is_main_thread(self) -> bool:
        """Check if we're running on the main (GUI) thread.

        Returns:
            True if on main thread, False if on background thread
        """
        app = QApplication.instance()
        if app is None:
            return False
        return QThread.currentThread() == app.thread()

    def get_all_screenshot_urls(self) -> dict:
        """Get screenshot URLs for all games with Image records.

        Returns:
            Dict mapping app_id (str) -> list of screenshot URLs
        """
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return {}

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()

            try:
                from .steamscraper.database import Image as ScraperImage

                # Get all images with URLs, grouped by appid
                images = session.query(ScraperImage).filter(
                    ScraperImage.url.isnot(None)
                ).order_by(
                    ScraperImage.appid,
                    ScraperImage.image_order
                ).all()

                result = {}
                for img in images:
                    app_id = str(img.appid)
                    if app_id not in result:
                        result[app_id] = []
                    result[app_id].append(img.url)

                logger.debug(f"Found screenshot URLs for {len(result)} games")
                return result

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Failed to get screenshot URLs: {e}")
            return {}

    def get_screenshots_for_app(self, app_id: str) -> list:
        """Get screenshot URLs for a single app, with lazy population.

        Priority order:
        1. Check catalog.db for existing Image records
        2. Fetch from Steam API and store in catalog

        Args:
            app_id: Steam app ID

        Returns:
            List of screenshot URLs, or empty list if not found
        """
        logger.info(f"get_screenshots_for_app called for app_id={app_id}")
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            logger.warning(f"catalog.db does not exist at {db_path}")
            return []

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()

            try:
                from .steamscraper.database import Image as ScraperImage

                appid_int = int(app_id)

                # 1. Check for existing Image records with URLs
                images = session.query(ScraperImage).filter(
                    ScraperImage.appid == appid_int,
                    ScraperImage.url.isnot(None)
                ).order_by(
                    ScraperImage.image_order
                ).all()

                logger.info(f"Found {len(images)} cached Image records for app {app_id}")

                if images:
                    urls = [img.url for img in images]
                    logger.info(f"Returning {len(urls)} cached URLs for app {app_id}")
                    return urls

                # 2. Try Steam API scrape
                logger.info(f"No cached images, fetching from Steam API for app {app_id}")
                return self._fetch_screenshots_from_api(appid_int, session, ScraperImage)

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Failed to get screenshots for {app_id}: {e}", exc_info=True)
            return []

    def _fetch_screenshots_from_api(self, appid: int, session, ImageClass) -> list:
        """Fetch screenshots from Steam API and store in catalog.

        Args:
            appid: Steam app ID
            session: Database session
            ImageClass: Image model class

        Returns:
            List of screenshot URLs, or empty list if failed
        """
        try:
            manager = self._get_manager()

            # Use the API client to get screenshot URLs
            logger.info(f"Calling Steam API for app {appid} screenshots")
            app_details = manager.api_client.get_app_details(appid)
            if not app_details:
                logger.warning(f"No app details returned from API for {appid}")
                return []

            screenshots_data = app_details.get('screenshots', [])
            logger.info(f"API returned {len(screenshots_data)} screenshots for {appid}")
            if not screenshots_data:
                return []

            urls = []
            for idx, ss in enumerate(screenshots_data):
                # Get full-size URL (path_full) or thumbnail (path_thumbnail)
                url = ss.get('path_full') or ss.get('path_thumbnail')
                if not url:
                    continue

                # Determine extension
                ext = '.jpg'
                if '.png' in url.lower():
                    ext = '.png'
                elif '.gif' in url.lower():
                    ext = '.gif'

                filename = f"{appid}_{idx}{ext}"

                image = ImageClass(
                    appid=appid,
                    filename=filename,
                    image_order=idx,
                    url=url,
                    scraped_date=None
                )
                session.add(image)
                urls.append(url)

            if urls:
                session.commit()
                logger.debug(f"Fetched {len(urls)} screenshots from API for app {appid}")

            return urls

        except Exception as e:
            logger.debug(f"API fetch failed for {appid}: {e}")
            return []

    def refresh_game_description(self, app_id: str) -> Optional[str]:
        """Refresh game description from Steam API.

        Use this to fetch the full HTML description from Steam.

        Args:
            app_id: Steam app ID

        Returns:
            Updated HTML description, or None if refresh failed
        """
        try:
            manager = self._get_manager()
            appid_int = int(app_id)

            # Fetch fresh details from Steam API
            app_details = manager.api_client.get_app_details(appid_int)
            if not app_details:
                logger.debug(f"No API data for app {app_id}")
                return None

            # Get the HTML description
            html_description = (
                app_details.get('detailed_description')
                or app_details.get('about_the_game')
            )
            if not html_description:
                logger.debug(f"No description in API response for app {app_id}")
                return None

            # Update the database
            session = manager.database.get_session()
            try:
                from .steamscraper.database import Game as ScraperGame

                game = session.query(ScraperGame).filter_by(appid=appid_int).first()
                if game:
                    game.detailed_description = html_description
                    # Also update about_the_game if present in API
                    if app_details.get('about_the_game'):
                        game.about_the_game = app_details['about_the_game']
                    session.commit()
                    logger.info(f"Updated description for app {app_id} from Steam API")
            finally:
                session.close()

            return html_description

        except RateLimitExceededError as e:
            logger.warning(f"Rate limited refreshing description for {app_id}")
            raise RateLimitError(str(e), wait_seconds=e.wait_seconds, reason=e.reason) from e
        except Exception as e:
            logger.warning(f"Failed to refresh description for {app_id}: {e}")
            return None

    def get_database_path(self) -> Path:
        """Get path to plugin's catalog database

        Returns:
            Path to SQLite database
        """
        return self.data_dir / "catalog.db"

    def get_store_page_url(self, app_id: str) -> str:
        """Get URL to Steam store page

        Args:
            app_id: Steam app ID

        Returns:
            Store page URL
        """
        return f"https://store.steampowered.com/app/{app_id}"

    def get_api_budget_status(self) -> Optional[dict]:
        """Return current API rate limit budget status.

        Returns:
            Dict with request_count, budget_limit, in_cooldown,
            cooldown_remaining — or None if manager not initialized.
        """
        if self._manager is None:
            return None
        return self._manager.api_client.get_budget_status()

    async def fetch_playtime(self, app_ids: List[str]) -> Dict[str, int]:
        """Fetch playtime data from database

        Args:
            app_ids: List of Steam app IDs

        Returns:
            Dict mapping app_id -> playtime in minutes
        """
        try:
            manager = self._get_manager()
            session = manager.database.get_session()
            from .steamscraper.database import Game as ScraperGame

            result = {}
            for app_id in app_ids:
                game = session.query(ScraperGame).filter_by(appid=int(app_id)).first()
                if game and game.average_playtime_forever:
                    result[app_id] = game.average_playtime_forever

            return result

        except Exception as e:
            logger.error(f"Failed to fetch playtime: {e}")
            return {}

    async def download_game_images(self, app_id: str) -> bool:
        """Download images for a single game (lazy loading)

        Called when UI needs to display a game's images but they're not cached.

        Args:
            app_id: Steam app ID

        Returns:
            True if images were downloaded successfully
        """
        try:
            manager = self._get_manager()
            import asyncio

            loop = asyncio.get_event_loop()

            # Use manager's refresh_images which downloads missing images
            result = await loop.run_in_executor(
                None, manager.refresh_images, int(app_id)
            )

            downloaded = result.get("downloaded", 0)
            logger.info(f"Downloaded {downloaded} images for game {app_id}")
            return downloaded > 0

        except Exception as e:
            logger.error(f"Failed to download images for {app_id}: {e}")
            return False

    def get_cached_image_path(self, app_id: str, image_type: str) -> Optional[Path]:
        """Get path to cached image if it exists

        Args:
            app_id: Steam app ID
            image_type: One of 'header', 'capsule', 'background', 'library_600x900'

        Returns:
            Path to cached image file, or None if not cached
        """
        image_dir = self.cache_dir / app_id
        if not image_dir.exists():
            return None

        # Map image types to filenames
        filenames = {
            "header": "header.jpg",
            "capsule": "capsule.jpg",
            "background": "background.jpg",
            "library_600x900": "library_600x900.jpg",
            "cover": "library_600x900.jpg",  # Alias
        }

        filename = filenames.get(image_type)
        if not filename:
            return None

        image_path = image_dir / filename
        return image_path if image_path.exists() else None

    def on_enable(self) -> None:
        """Called when plugin is enabled.

        Ensures the steamscraper catalog database exists so that
        on-demand metadata lookups don't fail before the first sync.
        """
        logger.info("Steam plugin enabled")
        try:
            ScraperDatabase(str(self.data_dir / "catalog.db"))
        except Exception as e:
            logger.debug(f"Steam DB init in on_enable: {e}")

    def on_disable(self) -> None:
        """Called when plugin is disabled"""
        logger.info("Steam plugin disabled")
        self.close()

    def on_sync_complete(self, progress_callback=None) -> dict:
        """Called after sync completes."""
        return {}

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a single game from steamscraper database

        This is the canonical source for game metadata. The main database
        should NOT store a copy - query the plugin directly.

        Args:
            app_id: Steam app ID

        Returns:
            Dict with metadata or None if not found
        """
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return None

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()

            try:
                from .steamscraper.database import Game as ScraperGame

                game = session.query(ScraperGame).filter_by(appid=int(app_id)).first()
                if game:
                    return self._scraper_game_to_metadata(game, include_description=True)
                return None

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Failed to get metadata for {app_id}: {e}")
            return None

    # === UNIFORM METADATA INTERFACE ===

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Resolve a game in Steam's catalog and return standardized metadata.

        Resolution strategy:
        1. If store_name == "steam" → direct ID lookup
        2. Otherwise → title-based search in Steam catalog
        3. Enrich screenshots via API-on-cache-miss if empty

        Args:
            store_name: Store identifier
            store_id: Store's app ID
            normalized_title: Optional normalized title for cross-store search

        Returns:
            Standardized metadata dict, or None
        """
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return None

        appid = None
        if store_name == self.store_name:
            appid = store_id
        elif normalized_title:
            appid = self._find_appid_by_title(normalized_title)

        if not appid:
            return None

        metadata = self.get_game_metadata(str(appid))
        if not metadata:
            return None

        # Enrich screenshots if missing (API-on-cache-miss)
        if not metadata.get("screenshots"):
            screenshots = self.get_screenshots_for_app(str(appid))
            if screenshots:
                metadata["screenshots"] = screenshots

        return metadata

    def _find_game_by_title(self, normalized_title: str) -> Optional[Dict[str, Any]]:
        """Search Steam catalog by normalized title."""
        appid = self._find_appid_by_title(normalized_title)
        if appid is None:
            return None
        metadata = self.get_game_metadata(str(appid))
        if metadata and not metadata.get("screenshots"):
            screenshots = self.get_screenshots_for_app(str(appid))
            if screenshots:
                metadata["screenshots"] = screenshots
        return metadata

    def _find_appid_by_title(self, normalized_title: str) -> Optional[int]:
        """Find a Steam appid by normalized title using lazy index."""
        if self._title_index is None:
            self._build_title_index()
        return self._title_index.get(normalized_title)

    def _build_title_index(self) -> None:
        """Build lazy normalized_title → appid index from all catalog games."""
        from luducat.plugins.sdk.text import normalize_title

        self._title_index = {}
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()
            try:
                games = session.query(
                    ScraperGame.appid, ScraperGame.name
                ).filter(
                    ScraperGame.type == "game"
                ).all()
                for appid, name in games:
                    if name:
                        nt = normalize_title(name)
                        if nt:
                            self._title_index[nt] = appid
                logger.debug(
                    f"Built Steam title index: {len(self._title_index)} entries"
                )
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Failed to build Steam title index: {e}")

    def get_games_metadata_bulk(self, app_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games efficiently

        Uses a single database query for performance with large libraries.
        Defers heavy text columns (descriptions, reviews) that are not needed
        for the startup cache — descriptions are lazy-loaded on demand.

        Args:
            app_ids: List of Steam app IDs

        Returns:
            Dict mapping app_id -> metadata dict
        """
        if not app_ids:
            return {}

        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return {}

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()

            try:
                from sqlalchemy.orm import defer
                from .steamscraper.database import Game as ScraperGame

                # Convert to ints and query in bulk
                # Defer heavy text columns not needed for cache build
                # (descriptions are lazy-loaded on demand via get_game_description)
                appid_ints = [int(aid) for aid in app_ids]
                games = (
                    session.query(ScraperGame)
                    .filter(ScraperGame.appid.in_(appid_ints))
                    .options(
                        defer(ScraperGame.detailed_description),
                        defer(ScraperGame.about_the_game),
                        defer(ScraperGame.reviews),
                        defer(ScraperGame.ext_user_account_notice),
                        defer(ScraperGame.notes),
                        defer(ScraperGame.package_groups),
                        defer(ScraperGame.packages),
                    )
                    .all()
                )

                result = {}
                for game in games:
                    metadata = self._scraper_game_to_metadata(game)
                    if metadata:
                        result[str(game.appid)] = metadata

                return result

            finally:
                session.close()

        except Exception as e:
            logger.error(f"Failed to get bulk metadata: {e}")
            return {}

    def _scraper_game_to_metadata(self, game, include_description: bool = False) -> Dict[str, Any]:
        """Convert steamscraper Game to metadata dict

        Args:
            game: ScraperGame object
            include_description: If True, include full description text.
                False by default for bulk loads (descriptions lazy-loaded on demand).

        Returns:
            Metadata dict in standardized format
        """
        # Parse JSON fields (Column(JSON) should auto-deserialize, helper
        # provides defensive fallback with diagnostic logging)
        screenshots = self._safe_json_field(game.screenshots, "screenshots")
        developers = self._safe_json_field(game.developers, "developers")
        publishers = self._safe_json_field(game.publishers, "publishers")
        genres = self._safe_json_field(game.genres, "genres")

        # Cover image: prefer 2x (1200x1800) over 1x (600x900)
        cover_image_url = (
            game.library_capsule_2x
            or game.library_capsule
            or ""
        )

        # Background image priority: library_hero > background_image
        background_image_url = game.library_hero or game.background_image or ""

        # Platform support
        platforms = []
        if getattr(game, "windows", False):
            platforms.append("Windows")
        if getattr(game, "linux", False):
            platforms.append("Linux")
        if getattr(game, "mac", False):
            platforms.append("macOS")

        # Categories (e.g., Single-player, Co-op, Steam Workshop)
        categories = self._safe_json_field(game.categories, "categories")

        # Tags (user-generated)
        tags = self._safe_json_field(game.tags, "tags")

        # Supported languages — normalize to plain list
        # Column is JSON: may be a list, HTML string, or None
        raw_langs = getattr(game, "supported_languages", None)
        if isinstance(raw_langs, list):
            lang_list = [str(lang).strip() for lang in raw_langs if lang]
        elif isinstance(raw_langs, str) and raw_langs.strip():
            import re
            # Strip HTML tags (Steam uses <strong>*</strong> for full audio markers)
            clean = re.sub(r"<[^>]+>", "", raw_langs)
            # Take only the language list (before <br> footnotes)
            if "<br>" in raw_langs:
                clean = clean.split("\n")[0]
            lang_list = [
                lang.strip().rstrip("*").strip()
                for lang in clean.split(",") if lang.strip()
            ]
        else:
            lang_list = []
        full_audio_languages = getattr(game, "full_audio_languages", "") or ""

        # Calculate Steam user rating percentage from positive/negative reviews
        # Use explicit None checks because 0 is a valid value (0 is falsy in Python)
        rating = None
        positive = getattr(game, "positive", None)
        negative = getattr(game, "negative", None)
        if positive is not None and negative is not None:
            total = positive + negative
            if total > 0:
                # Simple percentage: positive / total * 100
                rating = round((positive / total) * 100, 1)
        elif getattr(game, "user_score", None):
            rating = game.user_score

        # Build per-platform release_date dict
        release_date_str = game.release_date or ""
        release_dates_dict: Dict[str, str] = {}
        if release_date_str:
            from luducat.plugins.sdk.datetime import parse_release_date
            parsed = parse_release_date(release_date_str)
            if parsed:
                if getattr(game, "windows", False):
                    release_dates_dict["windows"] = parsed
                if getattr(game, "linux", False):
                    release_dates_dict["linux"] = parsed
                if getattr(game, "mac", False):
                    release_dates_dict["macos"] = parsed

        metadata = {
            "title": game.name or "",
            "short_description": game.short_description or "",
            "header_url": game.header_image or "",
            "cover": cover_image_url or "",
            "hero": background_image_url,
            "screenshots": screenshots,
            "release_date": release_dates_dict if release_dates_dict else release_date_str,
            "developers": developers,
            "publishers": publishers,
            "genres": genres,
            "critic_rating": getattr(game, "metacritic_score", None),
            "critic_rating_url": getattr(game, "metacritic_url", "") or "",
            "rating": rating,
            "rating_positive": positive,
            "rating_negative": negative,
            "controller_support": getattr(game, "controller_support", "") or "",
            "platforms": platforms,
            "website": getattr(game, "website", "") or "",
            "features": categories,
            "supported_languages": lang_list,
            "full_audio_languages": full_audio_languages,
            "type": getattr(game, "type", "") or "",
            "required_age": getattr(game, "required_age", None),
            "is_free": getattr(game, "is_free", False),
            "is_demo": (
                (getattr(game, "type", "") or "").lower() == "demo"
            ),
            "achievements": getattr(game, "achievements", None),
            "recommendations": getattr(game, "recommendations", None),
            "estimated_owners": getattr(game, "estimated_owners", "") or "",
            "average_playtime_forever": getattr(game, "average_playtime_forever", None),
            "average_playtime_2weeks": getattr(game, "average_playtime_2weeks", None),
            "peak_ccu": getattr(game, "peak_ccu", None),
            "content_descriptors": self._safe_json_field(
                game.content_descriptors, "content_descriptors",
            ),
            "tags": tags,
        }

        # Only include description when explicitly requested (e.g. single-game fetch)
        # Bulk loads skip this — descriptions are lazy-loaded on demand
        if include_description:
            metadata["description"] = (
                game.detailed_description or game.about_the_game or ""
            )

        return metadata

    def get_game_description(self, app_id: str) -> str:
        """Get description for a single game (optimized query).

        Only fetches description fields, not full metadata.

        Args:
            app_id: Steam app ID

        Returns:
            HTML description string, or empty string if not found
        """
        db_path = self.data_dir / "catalog.db"
        if not db_path.exists():
            return ""

        try:
            database = ScraperDatabase(str(db_path))
            session = database.get_session()

            try:
                from .steamscraper.database import Game as ScraperGame

                game = session.query(ScraperGame).filter_by(appid=int(app_id)).first()
                if game:
                    return game.detailed_description or game.about_the_game or ""
            finally:
                session.close()

            # Not in local cache — fetch from Steam store API (public endpoint)
            return self.refresh_game_description(app_id) or ""

        except Exception as e:
            logger.debug(f"Failed to get description for {app_id}: {e}")
            return ""

    def fetch_deck_compat(self, app_id: str) -> Optional[str]:
        """Fetch Steam Deck compatibility rating for a game.

        Uses the Steam Deck compatibility report API endpoint.
        Returns: "verified", "playable", "unsupported", or None if unknown.
        """
        # Deck compat categories: 3=Verified, 2=Playable, 1=Unsupported, 0=Unknown
        DECK_COMPAT_MAP = {3: "verified", 2: "playable", 1: "unsupported"}

        try:
            url = "https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
            resp = self.http.get(url, params={"nAppID": app_id}, timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json()
            # Response: {"success": 2, "results": {"resolved_category": 3, ...}}
            results = data.get("results", {})
            category = results.get("resolved_category", 0)
            return DECK_COMPAT_MAP.get(category)
        except Exception as e:
            logger.debug(f"Failed to fetch deck compat for {app_id}: {e}")
            return None

    def repair_content_descriptors(self, progress_callback=None) -> dict:
        """Re-fetch API data for adult games missing content_descriptors.

        Games synced before the content_descriptors fix have NULL in this
        column. This queries fresh API data which now correctly stores
        content_descriptors via _update_game_from_api, then propagates
        the repaired values to the main DB's metadata_json.

        Args:
            progress_callback: Optional callback(current, total, title)

        Returns:
            dict with keys: total, updated, failed, rate_limited
        """
        manager = self._get_manager()
        db = manager.database
        session = db.get_session()
        stats = {"total": 0, "updated": 0, "failed": 0, "rate_limited": False}
        repaired_appids = []

        try:
            # Find adult games missing content_descriptors
            games = (
                session.query(ScraperGame)
                .filter(
                    ScraperGame.required_age >= 18,
                    ScraperGame.content_descriptors.is_(None),
                )
                .all()
            )
            stats["total"] = len(games)

            if not games:
                logger.info("No games need content_descriptors repair")
                return stats

            logger.info(
                f"Repairing content_descriptors for {len(games)} adult games"
            )

            for idx, game in enumerate(games):
                if progress_callback:
                    progress_callback(idx, len(games), game.name or f"App {game.appid}")
                try:
                    manager._fetch_and_store_game(
                        game.appid,
                        existing_game=game,
                        download_images=False,
                    )
                    stats["updated"] += 1
                    repaired_appids.append(game.appid)
                except RateLimitExceededError:
                    logger.warning(
                        f"Rate limited during content_descriptors repair "
                        f"at {idx}/{len(games)}"
                    )
                    stats["rate_limited"] = True
                    break
                except Exception as e:
                    logger.debug(f"Failed to repair app {game.appid}: {e}")
                    stats["failed"] += 1

        finally:
            session.close()

        # Propagate repaired content_descriptors to main DB
        if repaired_appids:
            self._update_main_db_content_descriptors(repaired_appids)

        logger.info(
            f"Content descriptors repair: {stats['updated']}/{stats['total']} "
            f"updated, {stats['failed']} failed"
            + (", rate limited" if stats["rate_limited"] else "")
        )
        return stats

    def _update_main_db_content_descriptors(self, repaired_appids: list) -> None:
        """Propagate repaired content_descriptors from plugin DB to main DB.

        Follows the same pattern as _update_main_db_cover_urls().

        Args:
            repaired_appids: List of Steam appids that were successfully repaired
        """
        if not self.main_db:
            logger.warning("MainDbAccessor not available, skipping content_descriptors propagation")
            return

        try:
            # Read fresh content_descriptors from plugin DB
            manager = self._get_manager()
            plugin_session = manager.database.get_session()
            patches_by_id = {}
            try:
                for appid in repaired_appids:
                    game = plugin_session.query(ScraperGame).filter_by(appid=appid).first()
                    if game and game.content_descriptors:
                        patches_by_id[str(appid)] = {
                            "content_descriptors": game.content_descriptors
                        }
            finally:
                plugin_session.close()

            if not patches_by_id:
                return

            updated = self.main_db.batch_patch_metadata_json(patches_by_id)
            logger.info(
                f"Propagated content_descriptors to {updated} main DB entries"
            )

        except Exception as e:
            logger.error(f"Failed to propagate content_descriptors to main DB: {e}")

    def get_install_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return installation status for Steam games using VDF scanner.

        Scans Steam library folders for appmanifest_*.acf files to detect
        installed games and their install paths.

        Returns:
            Dict mapping app_id -> {"installed": True, "install_path": str|None}
            for installed games only. Absence means not installed.
        """
        if not self.has_local_data_consent():
            logger.debug("Skipping Steam install sync: local data consent not granted")
            return None

        steam_path = self._get_steam_path_setting()
        if steam_path:
            steam_path = Path(steam_path)
        else:
            steam_path = find_steam_path()

        if not steam_path:
            logger.debug("Steam path not found, skipping install sync")
            return None

        try:
            installed = scan_installed_games(steam_path)
            if not installed:
                return None

            result = {}
            for app_id, manifest in installed.items():
                full_path = manifest.get("full_path")
                result[str(app_id)] = {
                    "installed": True,
                    "install_path": full_path,
                }

            logger.info(f"Steam install sync: {len(result)} installed games detected")
            return result

        except Exception as e:
            logger.warning(f"Failed to scan Steam installed games: {e}")
            return None

    def get_tag_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return Steam user tag, favorite, and hidden mappings for game_service.

        Parses Steam's local sharedconfig.vdf to extract:
        - User categories/tags → luducat tags
        - "favorite" category → is_favorite
        - Hidden=1 → is_hidden

        Returns:
            Dict with "mode" and "mappings", or None if tag sync disabled
        """
        if not self.has_local_data_consent():
            logger.debug("Skipping Steam tag sync: local data consent not granted")
            return None

        steam_path = self._get_steam_path_setting()
        if steam_path:
            steam_path = Path(steam_path)
        else:
            steam_path = find_steam_path()

        if not steam_path:
            logger.debug("Steam path not found, skipping tag sync")
            return None

        # Determine which Steam account to use
        steam_id = self.get_setting("steam_id", "")
        if not steam_id:
            # Try to find the most recent login user
            users = parse_login_users(steam_path)
            for user in users:
                if user["most_recent"]:
                    steam_id = user["steam_id"]
                    break
            if not steam_id and users:
                steam_id = users[0]["steam_id"]

        if not steam_id:
            logger.debug("No Steam ID available for tag sync")
            return None

        user_config = parse_user_config(steam_path, steam_id)
        vdf_tags = user_config.get("tags", {})
        vdf_favorites = user_config.get("favorites", set())
        vdf_hidden = user_config.get("hidden", set())

        if not vdf_tags and not vdf_favorites and not vdf_hidden:
            return None

        mappings = {}
        # Collect all app_ids that have any data
        all_app_ids = set(vdf_tags.keys()) | vdf_favorites | vdf_hidden

        for app_id in all_app_ids:
            entry = {
                "tags": vdf_tags.get(app_id, []),
                "favorite": app_id in vdf_favorites,
                "hidden": app_id in vdf_hidden,
            }
            mappings[app_id] = entry

        if not mappings:
            return None

        logger.info(
            f"Steam tag sync: {len(vdf_tags)} games with tags, "
            f"{len(vdf_favorites)} favorites, {len(vdf_hidden)} hidden"
        )
        return {
            "mode": "add_only",
            "mappings": mappings,
        }

    def close(self) -> None:
        """Clean up resources"""
        if self._manager is not None:
            self._manager.close()
            self._manager = None
        logger.debug("Steam plugin closed")
