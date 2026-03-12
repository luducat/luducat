# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# store.py

# Portions adapted from Legendary (https://github.com/derrod/legendary)
# Copyright (c) Rodney and Legendary contributors
# Licensed under GPLv3+
"""Epic Games Store plugin for luducat

Provides Epic Games Store library integration using direct Epic APIs
for authentication and metadata. Game launching is handled by runner plugins
(HeroicRunner, EpicLauncherRunner) via the RuntimeManager.

Architecture:
    - OAuth session management in epic_session.py
    - Direct Epic API client in epic_api.py
    - Launching is delegated to runner plugins (not this store)
    - Install detection reads native Epic manifests (Windows/macOS)
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.base import AbstractGameStore, Game, PluginError

from .database import EpicDatabase, EpicGame
from .epic_api import EpicAPI
from .epic_session import EpicSession

logger = logging.getLogger(__name__)


class EpicStore(AbstractGameStore):
    """Epic Games Store integration using direct Epic APIs

    Uses direct API calls for:
    - Authentication (OAuth2 via EpicSession)
    - Fetching owned games list (game assets + catalog bulk)
    - Fetching game metadata (catalog bulk endpoint)

    Game launching is handled by runner plugins (HeroicRunner,
    EpicLauncherRunner) via the RuntimeManager three-tier system.
    """

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        """Initialize Epic store plugin

        Args:
            config_dir: Plugin config directory
            cache_dir: Plugin cache directory (for images)
            data_dir: Plugin data directory (for database)
        """
        super().__init__(config_dir, cache_dir, data_dir)

        self._session: Optional[EpicSession] = None
        self._api: Optional[EpicAPI] = None

        # Database (lazy initialization)
        self._db: Optional[EpicDatabase] = None
        self._title_index: Optional[Dict[str, str]] = None

        logger.debug("EpicStore initialized: data_dir=%s", data_dir)

    # =========================================================================
    # REQUIRED PROPERTIES
    # =========================================================================

    @property
    def store_name(self) -> str:
        return "epic"

    @property
    def display_name(self) -> str:
        return "Epic Games"

    # =========================================================================
    # REQUIRED METHODS - Availability & Authentication
    # =========================================================================

    def is_available(self) -> bool:
        """Check if Epic Games integration is available.

        Always available — no external binary dependency.
        """
        return True

    def is_authenticated(self) -> bool:
        """Check if user is authenticated with Epic Games."""
        return self._get_session().has_session

    async def authenticate(self) -> bool:
        """Legacy authenticate method — kept for compatibility.

        For GUI auth, use authenticate_with_code() instead.
        """
        return self.is_authenticated()

    def authenticate_with_code(self, auth_code: str) -> tuple[bool, str]:
        """Authenticate with Epic Games using an authorization code.

        Args:
            auth_code: The authorization code from Epic's login page

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            session = self._get_session()
            result = session.exchange_authorization_code(auth_code)
            display_name = result.get("display_name", "unknown")
            return True, _("Connected as {account}").format(account=display_name)
        except Exception as e:
            logger.error("Epic authentication failed: %s", e)
            return False, str(e)

    def logout(self) -> tuple[bool, str]:
        """Log out from Epic Games.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            session = self._get_session()
            session.revoke()
            return True, _("Logged out successfully")
        except Exception as e:
            logger.error("Epic logout failed: %s", e)
            return False, str(e)

    def get_account_identifier(self) -> Optional[str]:
        """Return the Epic account name identifying the current account."""
        return self._get_session().display_name

    def get_auth_status(self) -> tuple:
        """Get detailed authentication status for UI display.

        Returns:
            Tuple of (is_authenticated: bool, status_message: str)
        """
        session = self._get_session()
        if not session.has_session:
            return False, _("Not connected")

        display_name = session.display_name
        if not display_name:
            return False, _("Not connected")

        return True, _("Connected as {account}").format(account=display_name)

    def get_full_status(self, _force_refresh: bool = False) -> Dict[str, Any]:
        """Get comprehensive status for plugin settings UI.

        Args:
            force_refresh: If True, bypass cache and fetch fresh status.

        Returns:
            Dict with account info.
        """
        session = self._get_session()
        return {
            "account": session.display_name,
            "account_id": session.account_id,
            "authenticated": session.has_session,
        }

    # =========================================================================
    # REQUIRED METHODS - Game Data
    # =========================================================================

    async def fetch_user_games(
        self,
        status_callback: Optional[Callable[[str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """Fetch list of game IDs owned by user.

        Uses Epic's game assets endpoint (one HTTP call for entire library),
        then fetches full catalog metadata for NEW games in parallel batches.

        Returns:
            List of Epic app names (store-specific IDs)

        Raises:
            PluginError: If not authenticated or fetch fails
        """
        if status_callback:
            status_callback("Fetching Epic Games library...")

        if not self.is_authenticated():
            error_msg = "Not authenticated with Epic Games. Please authenticate first."
            logger.warning(error_msg)
            if status_callback:
                status_callback("Error: %s" % error_msg)
            raise PluginError(error_msg)

        # Ensure valid token (auto-refresh if needed)
        try:
            session = self._get_session()
            access_token = await asyncio.to_thread(session.ensure_valid)
        except Exception as e:
            error_msg = str(e)
            if "expired" in error_msg.lower() or "refresh" in error_msg.lower():
                error_msg = (
                    "Epic Games session has expired. Please re-authenticate "
                    "in Epic plugin settings (Settings > Plugins > Epic)."
                )
            logger.error(error_msg)
            if status_callback:
                status_callback("Error: %s" % error_msg)
            raise PluginError(error_msg) from e

        if status_callback:
            status_callback("Contacting Epic Games servers...")

        api = self._get_api()

        # Fetch all owned assets (one HTTP call per platform)
        try:
            win_assets = await asyncio.to_thread(
                api.get_game_assets, access_token, "Windows"
            )
        except RuntimeError as e:
            error_msg = str(e)
            if "401" in error_msg:
                error_msg = (
                    "Epic Games session has expired. Please re-authenticate "
                    "in Epic plugin settings (Settings > Plugins > Epic)."
                )
            logger.error(error_msg)
            if status_callback:
                status_callback("Error: %s" % error_msg)
            raise PluginError(error_msg) from e

        # Mac assets for platform detection
        try:
            mac_assets = await asyncio.to_thread(
                api.get_game_assets, access_token, "Mac"
            )
        except Exception:
            mac_assets = []

        # Build merged asset info: app_name → {asset, platforms}
        asset_map: Dict[str, Dict[str, Any]] = {}
        for asset in win_assets:
            app_name = asset.get("appName")
            if not app_name:
                continue
            asset_map[app_name] = {
                "asset": asset,
                "windows": True,
                "mac": False,
            }

        mac_app_names = set()
        for asset in mac_assets:
            app_name = asset.get("appName")
            if not app_name:
                continue
            mac_app_names.add(app_name)
            if app_name in asset_map:
                asset_map[app_name]["mac"] = True
            else:
                asset_map[app_name] = {
                    "asset": asset,
                    "windows": False,
                    "mac": True,
                }

        if cancel_check and cancel_check():
            return []

        # Incremental sync: only fetch full metadata for NEW games
        db = self._get_db()
        existing_app_names = set(db.get_all_app_names(include_dlc=True))
        app_names = list(asset_map.keys())
        new_app_names = [an for an in app_names if an not in existing_app_names]

        # Patch missing identifiers on existing games
        for app_name in app_names:
            if app_name not in existing_app_names:
                continue
            asset = asset_map[app_name]["asset"]
            existing = db.get_game(app_name)
            if existing:
                if not existing.catalog_id:
                    cid = asset.get("catalogItemId")
                    if cid:
                        existing.catalog_id = cid
                if not existing.namespace:
                    ns = asset.get("namespace")
                    if ns:
                        existing.namespace = ns

        # Fetch full catalog metadata for new games in parallel batches
        if new_app_names:
            if status_callback:
                status_callback(
                    "Fetching metadata for %d new games..." % len(new_app_names)
                )

            batch_size = 20
            new_count = 0

            for batch_start in range(0, len(new_app_names), batch_size):
                if cancel_check and cancel_check():
                    break

                batch = new_app_names[batch_start:batch_start + batch_size]
                metadata_results = await self._fetch_metadata_batch(
                    api, access_token, batch, asset_map
                )

                for app_name, metadata in metadata_results.items():
                    asset_info = asset_map[app_name]
                    db_game = self._to_db_game(
                        app_name, metadata, asset_info
                    )
                    if db_game:
                        db.session.add(db_game)
                        new_count += 1

                db.commit()
        else:
            new_count = 0

        db.commit()

        if status_callback:
            if new_count > 0:
                status_callback(
                    "Found %d Epic games (%d new)" % (len(app_names), new_count)
                )
            else:
                status_callback(
                    "Found %d Epic games (0 new)" % len(app_names)
                )

        logger.info(
            "Fetched %d games from Epic Games (%d new, %d existing)",
            len(app_names), new_count, len(app_names) - new_count,
        )
        return app_names

    async def _fetch_metadata_batch(
        self,
        api: EpicAPI,
        access_token: str,
        app_names: List[str],
        asset_map: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch catalog metadata for a batch of games in parallel.

        Args:
            api: EpicAPI instance
            access_token: Valid bearer token
            app_names: Batch of app names to fetch
            asset_map: Asset info map

        Returns:
            Dict mapping app_name → catalog metadata dict
        """
        results: Dict[str, Dict[str, Any]] = {}

        def fetch_one(app_name: str) -> tuple:
            asset = asset_map[app_name]["asset"]
            ns = asset.get("namespace", "")
            cid = asset.get("catalogItemId", "")
            if not ns or not cid:
                return app_name, {}
            try:
                return app_name, api.get_game_info(access_token, ns, cid)
            except Exception as e:
                logger.debug(
                    "Failed to fetch metadata for %s: %s", app_name, e
                )
                return app_name, {}

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                loop.run_in_executor(executor, fetch_one, an)
                for an in app_names
            ]
            for future in asyncio.as_completed(futures):
                app_name, metadata = await future
                results[app_name] = metadata

        return results

    async def fetch_game_metadata(
        self,
        app_ids: List[str],
        download_images: bool = False,
    ) -> List[Game]:
        """Fetch detailed metadata for given app IDs.

        Reads from catalog.db (populated by fetch_user_games). If metadata
        is incomplete, enriches from Epic catalog REST API + GraphQL.

        Args:
            app_ids: List of Epic app names
            download_images: If True, download images to cache (unused)

        Returns:
            List of Game objects with metadata
        """
        games = []
        db = self._get_db()
        api = self._get_api()

        for app_name in app_ids:
            try:
                db_game = db.get_game(app_name)
                if not db_game:
                    logger.warning("Game not in catalog.db: %s", app_name)
                    continue

                needs_enrichment = not db_game.is_metadata_complete

                if needs_enrichment and db_game.namespace:
                    catalog_data = await asyncio.to_thread(
                        api.get_product_data, db_game.namespace
                    )
                    if catalog_data:
                        enriched = api.extract_metadata(catalog_data)
                        self._apply_enrichment(
                            db_game, enriched, api=api
                        )
                        db.upsert_game(db_game)
                        db.commit()
                    else:
                        logger.debug(
                            "No catalog data for %s (ns: %s)",
                            app_name, db_game.namespace,
                        )
                elif needs_enrichment and not db_game.namespace:
                    logger.warning(
                        "Game %s needs enrichment but has no namespace",
                        app_name,
                    )

                game = self._db_game_to_plugin_game(db_game)
                if game:
                    games.append(game)

            except Exception as e:
                logger.error(
                    "Failed to process %s: %s", app_name, e, exc_info=True
                )

        db.commit()
        return games

    def get_database_path(self) -> Path:
        """Return path to plugin's catalog database."""
        return self.data_dir / "catalog.db"

    # =========================================================================
    # OPTIONAL METHODS - Enhanced Functionality
    # =========================================================================

    def get_store_page_url(self, app_id: str) -> str:
        """Get URL to game's store page on Epic Games Store."""
        db = self._get_db()
        game = db.get_game(app_id)
        if game and game.title:
            slug = game.title.lower().replace(" ", "-")
            return f"https://store.epicgames.com/en-US/p/{slug}"
        return f"https://store.epicgames.com/en-US/browse?q={app_id}"

    def get_game_metadata(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a single game from plugin's database."""
        db = self._get_db()
        game = db.get_game(app_id)
        if not game:
            return None

        # Build platforms list from boolean flags
        platforms = []
        if getattr(game, "windows", False):
            platforms.append("Windows")
        if getattr(game, "mac", False):
            platforms.append("macOS")

        # Build per-platform release_date dict
        release_date_str = game.release_date or ""
        release_dates_dict: Dict[str, str] = {}
        if release_date_str:
            from luducat.plugins.sdk.datetime import parse_release_date
            parsed = parse_release_date(release_date_str)
            if parsed:
                if getattr(game, "windows", False):
                    release_dates_dict["windows"] = parsed
                if getattr(game, "mac", False):
                    release_dates_dict["macos"] = parsed

        # short_description: use native or generate from description
        short_desc = game.short_description or ""
        if not short_desc and game.description:
            from luducat.plugins.base import generate_short_description
            short_desc = generate_short_description(game.description)

        return {
            "title": game.title,
            "short_description": short_desc,
            "description": game.description,
            "header_url": game.header_url,
            "cover": game.cover_url,
            "hero": getattr(game, "background_url", "") or "",
            "logo_url": getattr(game, "logo_url", "") or "",
            "screenshots": game.screenshots,
            "release_date": release_dates_dict if release_dates_dict else release_date_str,
            "developers": game.developers,
            "publishers": game.publishers,
            "genres": game.genres,
            "features": getattr(game, "categories", []) or [],
            "platforms": platforms,
            "type": getattr(game, "app_type", "") or "",
        }

    # === UNIFORM METADATA INTERFACE ===

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Resolve a game in Epic's catalog and return standardized metadata.

        Resolution strategy:
        1. If store_name == "epic" -> direct ID lookup
        2. Otherwise -> title-based search in Epic catalog

        Args:
            store_name: Store identifier
            store_id: Store's app ID
            normalized_title: Optional normalized title for cross-store search

        Returns:
            Standardized metadata dict, or None
        """
        app_name = None
        if store_name == self.store_name:
            app_name = store_id
        elif normalized_title:
            app_name = self._find_app_name_by_title(normalized_title)

        if not app_name:
            return None

        return self.get_game_metadata(app_name)

    def _find_game_by_title(self, normalized_title: str) -> Optional[Dict[str, Any]]:
        """Search Epic catalog by normalized title."""
        app_name = self._find_app_name_by_title(normalized_title)
        if app_name:
            return self.get_game_metadata(app_name)
        return None

    def _find_app_name_by_title(self, normalized_title: str) -> Optional[str]:
        """Find an Epic app_name by normalized title using lazy index."""
        if self._title_index is None:
            self._build_title_index()
        return self._title_index.get(normalized_title)

    def _build_title_index(self) -> None:
        """Build lazy normalized_title -> app_name index from all catalog games."""
        from luducat.plugins.sdk.text import normalize_title

        self._title_index = {}
        db = self._get_db()
        for game in db.get_all_games():
            nt = normalize_title(game.title)
            if nt:
                self._title_index[nt] = game.app_name
        logger.debug(
            "Built Epic title index: %d entries", len(self._title_index)
        )

    def get_games_metadata_bulk(
        self, app_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games efficiently."""
        db = self._get_db()
        raw_metadata = db.get_games_metadata_bulk(app_ids)

        result = {}
        for app_name, meta in raw_metadata.items():
            result[app_name] = {
                "title": meta.get("title"),
                "short_description": meta.get("short_description"),
                "description": meta.get("description"),
                "header_url": meta.get("header_url"),
                "cover": meta.get("cover_url"),
                "screenshots": meta.get("screenshots", []),
                "release_date": meta.get("release_date"),
                "developers": meta.get("developers", []),
                "publishers": meta.get("publishers", []),
                "genres": meta.get("genres", []),
            }
        return result

    def get_game_description(self, app_id: str) -> str:
        """Get description for a single game (lazy loading)."""
        db = self._get_db()
        game = db.get_game(app_id)
        return game.description if game and game.description else ""

    def get_screenshots_for_app(self, app_id: str) -> List[str]:
        """Get screenshot URLs for a single app."""
        db = self._get_db()
        game = db.get_game(app_id)
        return game.screenshots if game else []

    def get_all_screenshot_urls(self) -> Dict[str, List[str]]:
        """Get screenshot URLs for all games.

        Returns:
            Dict mapping app_name -> list of screenshot URLs
        """
        db = self._get_db()
        result = {}

        try:
            games = db.get_all_games(include_dlc=False)
            for game in games:
                if game.screenshots:
                    result[game.app_name] = game.screenshots
        except Exception as e:
            logger.error("Failed to get all screenshot URLs: %s", e)

        return result

    def refresh_game_description(self, app_id: str) -> Optional[str]:
        """Refresh game description from Epic catalog API.

        Args:
            app_id: Epic app name

        Returns:
            Updated description, or None if refresh failed
        """
        try:
            db = self._get_db()
            game = db.get_game(app_id)
            if not game or not game.namespace or not game.catalog_id:
                logger.debug("Cannot refresh %s: missing namespace/catalog_id", app_id)
                return None

            session = self._get_session()
            if not session.has_session:
                return None

            api = self._get_api()
            access_token = session.ensure_valid()
            metadata = api.get_game_info(
                access_token, game.namespace, game.catalog_id
            )

            description = metadata.get(
                "longDescription", metadata.get("description")
            )
            if not description:
                return None

            game.description = description
            game.short_description = metadata.get("shortDescription", "")
            db.commit()
            logger.info("Refreshed description for %s", app_id)
            return description

        except Exception as e:
            logger.error("Failed to refresh description for %s: %s", app_id, e)
            return None

    def repair_library_assets(
        self, progress_callback: Optional[Callable[[str, int, int], None]] = None
    ) -> Dict[str, int]:
        """Repair and enrich metadata for games with incomplete data.

        Uses the Epic catalog REST API + GraphQL to fetch full metadata.

        Args:
            progress_callback: Optional callback(game_name, current, total)

        Returns:
            Dict with repair stats: {probed: int, updated: int, failed: int}
        """
        stats = {"probed": 0, "updated": 0, "failed": 0}

        db = self._get_db()
        api = self._get_api()

        try:
            # Refresh namespace data from asset list if authenticated
            session = self._get_session()
            if session.has_session:
                try:
                    access_token = session.ensure_valid()
                    logger.info("Refreshing namespace data from Epic assets...")
                    if progress_callback:
                        progress_callback("Refreshing game data from Epic...", 0, 0)

                    assets = api.get_game_assets(access_token)
                    namespace_map = {}
                    for asset in assets:
                        app_name = asset.get("appName")
                        namespace = asset.get("namespace")
                        if app_name and namespace:
                            namespace_map[app_name] = namespace

                    games = db.get_all_games(include_dlc=True)
                    updated_ns = 0
                    for game in games:
                        if not game.namespace and game.app_name in namespace_map:
                            game.namespace = namespace_map[game.app_name]
                            updated_ns += 1
                        # Also patch catalog_id from assets
                        if not game.catalog_id and game.app_name in namespace_map:
                            for asset in assets:
                                if asset.get("appName") == game.app_name:
                                    cid = asset.get("catalogItemId")
                                    if cid:
                                        game.catalog_id = cid
                                    break
                    if updated_ns > 0:
                        db.commit()
                        logger.info("Updated namespace for %d games", updated_ns)
                except Exception as e:
                    logger.warning("Failed to refresh namespaces: %s", e)

            # Find games with incomplete metadata
            games = db.get_all_games(include_dlc=False)
            games_needing_repair = [
                g for g in games
                if not g.is_metadata_complete and g.namespace
            ]

            games_no_namespace = [g for g in games if not g.namespace]
            if games_no_namespace:
                logger.warning(
                    "%d games have no namespace and cannot be enriched",
                    len(games_no_namespace),
                )

            total = len(games_needing_repair)
            if total == 0:
                logger.info("No Epic games need metadata repair")
                return stats

            logger.info(
                "Repairing metadata for %d Epic games", total
            )

            for i, game in enumerate(games_needing_repair):
                if progress_callback:
                    progress_callback(game.title, i + 1, total)

                try:
                    stats["probed"] += 1
                    catalog_data = api.get_product_data(game.namespace)
                    if not catalog_data:
                        stats["failed"] += 1
                        continue

                    enriched = api.extract_metadata(catalog_data)
                    self._apply_enrichment(game, enriched, api=api)
                    db.upsert_game(game)
                    db.commit()
                    stats["updated"] += 1

                except Exception as e:
                    logger.warning(
                        "Failed to repair metadata for %s: %s",
                        game.app_name, e,
                    )
                    stats["failed"] += 1

        except Exception as e:
            logger.error("Metadata repair failed: %s", e)

        return stats

    # =========================================================================
    # LIFECYCLE HOOKS
    # =========================================================================

    def on_enable(self) -> None:
        """Called when plugin is enabled in settings."""
        logger.info("Epic Games plugin enabled")
        db = self._get_db()
        db.initialize()

    def on_disable(self) -> None:
        """Called when plugin is disabled in settings."""
        logger.info("Epic Games plugin disabled")
        self.close()

    def get_install_sync_data(self) -> Optional[Dict[str, Any]]:
        """Return installation status for Epic games.

        Reads native Epic Games Launcher manifests (Windows/macOS).

        Returns:
            Dict mapping app_name -> {"installed": True, "install_path": str|None}
            for installed games only. Absence means not installed.
        """
        result = {}

        # Native Epic manifest files (Windows/macOS only)
        native_installed = self._read_native_epic_manifests()
        if native_installed:
            result.update(native_installed)

        if result:
            logger.info(
                "Epic install sync: %d installed games detected", len(result)
            )
            return result
        return None

    def _read_native_epic_manifests(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Read native Epic Games Launcher install manifests.

        Windows: C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\Manifests\\
        macOS:   ~/Library/Application Support/Epic/.../Data/Manifests/

        Returns:
            Dict mapping app_name -> {"installed": True, "install_path": str|None}
        """
        import sys
        manifest_dir = None

        if sys.platform == "win32":
            import os
            programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            manifest_dir = (
                Path(programdata) / "Epic" / "EpicGamesLauncher"
                / "Data" / "Manifests"
            )
        elif sys.platform == "darwin":
            manifest_dir = (
                Path.home() / "Library" / "Application Support"
                / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
            )

        if not manifest_dir or not manifest_dir.is_dir():
            return None

        from luducat.plugins.sdk.json import json
        result = {}

        try:
            for manifest_file in manifest_dir.glob("*.item"):
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    app_name = data.get("AppName")
                    if not app_name:
                        continue
                    if data.get("bIsIncompleteInstall", False):
                        continue

                    result[app_name] = {
                        "installed": True,
                        "install_path": data.get("InstallLocation"),
                    }
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(
                        "Failed to read manifest %s: %s",
                        manifest_file.name, e,
                    )
        except OSError as e:
            logger.debug("Failed to scan Epic manifests: %s", e)

        if result:
            logger.info(
                "Epic native manifests: %d installed games detected",
                len(result),
            )
        return result

    def on_sync_complete(self, progress_callback=None) -> Dict[str, Any]:
        """Called after sync completes for this store."""
        return {}

    def close(self) -> None:
        """Called when application is shutting down."""
        if self._db:
            self._db.close()
            self._db = None
        self._session = None
        self._api = None
        logger.debug("Epic Games plugin closed")

    # =========================================================================
    # PRIVATE METHODS
    # =========================================================================

    def _get_db(self) -> EpicDatabase:
        """Get or create database connection."""
        if self._db is None:
            self._db = EpicDatabase(self.get_database_path())
            self._db.initialize()
        return self._db

    def _get_session(self) -> EpicSession:
        """Get or create OAuth session manager."""
        if self._session is None:
            self._session = EpicSession(
                get_credential=self.get_credential,
                set_credential=self.set_credential,
                delete_credential=self.delete_credential,
                get_setting=self.get_setting,
                http_client=self.http,
            )
        return self._session

    def _get_api(self) -> EpicAPI:
        """Get or create API client."""
        if self._api is None:
            self._api = EpicAPI(http_client=self.http)
        return self._api

    def _db_game_to_plugin_game(self, db_game: EpicGame) -> Optional[Game]:
        """Convert EpicGame DB model to plugin Game dataclass.

        Args:
            db_game: EpicGame model from catalog.db

        Returns:
            Game object for plugin interface, or None if invalid
        """
        try:
            if not db_game.app_name or not db_game.title:
                return None

            return Game(
                store_app_id=db_game.app_name,
                store_name=self.store_name,
                title=db_game.title,
                launch_url=f"heroic://launch/epic/{db_game.app_name}",
                short_description=db_game.short_description,
                description=db_game.description,
                cover_image_url=db_game.cover_url,
                header_image_url=db_game.header_url,
                background_image_url=db_game.background_url,
                screenshots=db_game.screenshots or [],
                release_date=db_game.release_date,
                developers=db_game.developers or [],
                publishers=db_game.publishers or [],
                genres=db_game.genres or [],
            )
        except Exception as e:
            logger.error("Failed to convert EpicGame to Game: %s", e)
            return None

    def _apply_enrichment(
        self,
        db_game: EpicGame,
        enriched: Dict[str, Any],
        api: Optional[EpicAPI] = None,
    ) -> None:
        """Apply enriched metadata from Epic catalog API to EpicGame.

        Args:
            db_game: EpicGame model to update
            enriched: Metadata dict from EpicAPI.extract_metadata()
            api: Optional EpicAPI instance for genre fallback via GraphQL
        """
        if enriched.get("description"):
            db_game.description = enriched["description"]
        if enriched.get("short_description"):
            db_game.short_description = enriched["short_description"]

        if enriched.get("screenshots"):
            db_game.screenshots = enriched["screenshots"]

        if enriched.get("cover_url"):
            db_game.cover_url = enriched["cover_url"]
        if enriched.get("header_url"):
            db_game.header_url = enriched["header_url"]
        if enriched.get("logo_url"):
            db_game.logo_url = enriched["logo_url"]
        if enriched.get("background_url"):
            db_game.background_url = enriched["background_url"]
        if enriched.get("thumbnail_url"):
            db_game.thumbnail_url = enriched["thumbnail_url"]

        if enriched.get("developers"):
            db_game.developers = enriched["developers"]
        if enriched.get("publishers"):
            db_game.publishers = enriched["publishers"]

        if enriched.get("genres"):
            db_game.genres = enriched["genres"]

        if enriched.get("release_date"):
            db_game.release_date = enriched["release_date"]

        self._sanitize_descriptions(db_game)

    def _markdown_to_html(self, text: str) -> str:
        """Convert markdown/plain text mixture to proper HTML.

        Handles:
        - Headers (# ## ###) -> <h1> <h2> <h3>
        - Bold (**text**) -> <strong>
        - Italic (*text* or _text_) -> <em>
        - Links [text](url) -> <a href>
        - Images ![alt](url) -> <img>
        - Line breaks -> <br> or <p>
        - Lists (- item) -> <ul><li>

        Args:
            text: Raw markdown/plain text

        Returns:
            HTML formatted string
        """
        import re

        if not text:
            return ""

        lines = text.split("\n")
        html_lines = []
        in_list = False
        in_paragraph = False

        for line in lines:
            stripped = line.strip()

            if not stripped:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                if in_paragraph:
                    html_lines.append("</p>")
                    in_paragraph = False
                continue

            # Headers
            if stripped.startswith("###"):
                if in_paragraph:
                    html_lines.append("</p>")
                    in_paragraph = False
                content = stripped[3:].strip()
                html_lines.append(f"<h3>{content}</h3>")
                continue
            elif stripped.startswith("##"):
                if in_paragraph:
                    html_lines.append("</p>")
                    in_paragraph = False
                content = stripped[2:].strip()
                html_lines.append(f"<h2>{content}</h2>")
                continue
            elif stripped.startswith("#"):
                if in_paragraph:
                    html_lines.append("</p>")
                    in_paragraph = False
                content = stripped[1:].strip()
                html_lines.append(f"<h1>{content}</h1>")
                continue

            # List items
            if stripped.startswith("- ") or stripped.startswith("* "):
                if in_paragraph:
                    html_lines.append("</p>")
                    in_paragraph = False
                if not in_list:
                    html_lines.append("<ul>")
                    in_list = True
                content = stripped[2:].strip()
                html_lines.append(f"<li>{content}</li>")
                continue

            if in_list:
                html_lines.append("</ul>")
                in_list = False

            if not in_paragraph:
                html_lines.append("<p>")
                in_paragraph = True

            # Process inline markdown
            processed = stripped

            # Images ![alt](url)
            processed = re.sub(
                r"!\[([^\]]*)\]\(([^)]+)\)",
                r'<img src="\2" alt="\1">',
                processed
            )

            # Links [text](url)
            processed = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                r'<a href="\2">\1</a>',
                processed
            )

            # Bold **text** or __text__
            processed = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", processed)
            processed = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", processed)

            # Italic *text* or _text_ (but not inside words)
            processed = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", processed)
            processed = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", processed)

            html_lines.append(processed + "<br>")

        if in_list:
            html_lines.append("</ul>")
        if in_paragraph:
            html_lines.append("</p>")

        return "\n".join(html_lines)

    def _extract_first_paragraph(self, text: str) -> str:
        """Extract first text paragraph, excluding headers and images.

        Args:
            text: Raw markdown/plain text

        Returns:
            First paragraph of plain text content
        """
        import re

        if not text:
            return ""

        lines = text.split("\n")
        paragraph_lines = []

        for line in lines:
            stripped = line.strip()

            if not stripped:
                if paragraph_lines:
                    break
                continue

            if stripped.startswith("#"):
                if paragraph_lines:
                    break
                continue

            if re.match(r"^!\[.*\]\(.*\)$", stripped):
                continue

            if stripped.startswith("- ") or stripped.startswith("* "):
                if paragraph_lines:
                    break
                continue

            # Remove inline images from the line
            cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", stripped)

            # Remove markdown formatting for plain text
            cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
            cleaned = re.sub(r"__([^_]+)__", r"\1", cleaned)
            cleaned = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", cleaned)
            cleaned = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", cleaned)
            cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)

            cleaned = cleaned.strip()
            if cleaned:
                paragraph_lines.append(cleaned)

        return " ".join(paragraph_lines)

    def _extract_first_sentence(self, text: str) -> str:
        """Extract first sentence from text.

        Args:
            text: Plain text

        Returns:
            First sentence
        """
        import re

        if not text:
            return ""

        match = re.match(r"^(.+?[.!?])(?:\s|$)", text)
        if match:
            return match.group(1).strip()

        if len(text) > 200:
            truncated = text[:200]
            last_space = truncated.rfind(" ")
            if last_space > 100:
                return truncated[:last_space] + "..."
            return truncated + "..."

        return text

    def _sanitize_descriptions(self, db_game: "EpicGame") -> None:
        """Sanitize description and short_description fields.

        - Converts description from markdown to HTML
        - If short_description is empty, extracts first paragraph from original
        - If short_description equals description, shortens to first sentence

        Args:
            db_game: EpicGame model to update in place
        """
        import re

        original_description = db_game.description or ""

        if not db_game.short_description and original_description:
            db_game.short_description = self._extract_first_paragraph(original_description)

        if db_game.short_description and original_description:
            plain_original = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", original_description)
            plain_original = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain_original)
            plain_original = re.sub(r"__([^_]+)__", r"\1", plain_original)
            plain_original = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain_original)
            plain_original = re.sub(r"#+ ", "", plain_original)
            plain_original = re.sub(r"\s+", " ", plain_original).strip()

            short_clean = re.sub(r"\s+", " ", db_game.short_description).strip()

            if short_clean == plain_original:
                db_game.short_description = self._extract_first_sentence(short_clean)

        if db_game.description:
            db_game.description = self._markdown_to_html(db_game.description)

    def _to_db_game(
        self,
        app_name: str,
        metadata: Dict[str, Any],
        asset_info: Dict[str, Any],
    ) -> Optional[EpicGame]:
        """Convert Epic API response to database model.

        Args:
            app_name: Epic app name
            metadata: Catalog metadata from get_game_info()
            asset_info: Asset info dict with platform flags

        Returns:
            EpicGame object or None if parsing fails
        """
        try:
            if not app_name:
                return None

            # Get image URLs
            key_images = metadata.get("keyImages", [])
            cover_url = None
            header_url = None
            background_url = None
            logo_url = None
            thumbnail_url = None
            screenshots = []

            for img in key_images:
                img_type = img.get("type", "")
                url = img.get("url", "")
                if not url:
                    continue

                if img_type in ("DieselGameBoxTall", "OfferImageTall"):
                    cover_url = cover_url or url
                elif img_type in ("DieselGameBox", "OfferImageWide"):
                    header_url = header_url or url
                elif img_type == "DieselStoreFrontWide":
                    background_url = background_url or url
                elif img_type == "DieselGameBoxLogo":
                    logo_url = logo_url or url
                elif img_type == "Thumbnail":
                    thumbnail_url = thumbnail_url or url
                elif img_type == "Screenshot":
                    screenshots.append(url)

            # Extract release date from releaseInfo array
            release_date = None
            release_info = metadata.get("releaseInfo", [])
            if release_info and len(release_info) > 0:
                release_date = release_info[0].get("dateAdded")

            # DLC detection
            app_type = "dlc" if "mainGameItem" in metadata else "game"

            # Third-party store detection
            custom_attrs = metadata.get("customAttributes", {})
            third_party = custom_attrs.get("ThirdPartyManagedApp", {}).get("value")
            cloud_save = custom_attrs.get("CloudSaveFolder", {}).get("value")
            cli_args = custom_attrs.get("AdditionalCommandLine", {}).get("value")

            game = EpicGame(
                app_name=app_name,
                catalog_id=metadata.get("id") or asset_info.get("asset", {}).get("catalogItemId"),
                namespace=metadata.get("namespace") or asset_info.get("asset", {}).get("namespace"),
                title=metadata.get("title", app_name),
                app_type=app_type,
                description=metadata.get("longDescription", metadata.get("description")),
                short_description=metadata.get("shortDescription"),
                release_date=release_date,
                cover_url=cover_url,
                header_url=header_url,
                background_url=background_url,
                logo_url=logo_url,
                thumbnail_url=thumbnail_url,
                windows=asset_info.get("windows", True),
                mac=asset_info.get("mac", False),
                third_party_store=third_party,
                cloud_save_folder=cloud_save,
                additional_cli_args=cli_args,
            )

            # Set JSON fields
            developers = []
            publishers = []
            for dev in metadata.get("developer", "").split(","):
                dev = dev.strip()
                if dev:
                    developers.append(dev)
            for pub in metadata.get("publisher", "").split(","):
                pub = pub.strip()
                if pub:
                    publishers.append(pub)

            game.developers = developers
            game.publishers = publishers
            game.screenshots = screenshots

            # Sanitize descriptions
            self._sanitize_descriptions(game)

            return game

        except Exception as e:
            logger.error("Failed to convert Epic game to DB model: %s", e)
            return None
