# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""IGDB Metadata Provider

Implements AbstractMetadataProvider to provide game metadata enrichment
from IGDB (Internet Game Database).

Works like store plugins for metadata access:
- get_game_metadata(igdb_id) -> Dict
- get_games_metadata_bulk(igdb_ids) -> Dict[str, Dict]
- lookup_igdb_id(store_name, store_app_id, normalized_title) -> Optional[int]

The MetadataManager queries plugins in priority order (steam → gog → epic → igdb).
IGDB just fetches, caches, and provides data - the main app does game mapping.
"""

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from luducat.plugins.sdk.json import json

from luducat.plugins.sdk.datetime import utc_from_timestamp, utc_now

from luducat.plugins.base import (
    AbstractMetadataProvider,
    AuthenticationError,
    EnrichmentData,
    Game,
    MetadataSearchResult,
)

from .api import (
    IgdbApi,
    IgdbCancelledError,
    IgdbRateLimitError,
    ProgressCallback,
    get_pc_release_date,
    normalize_title,
)
from .database import (
    IgdbAgeRating,
    IgdbArtwork,
    IgdbDatabase,
    IgdbExternalId,
    IgdbGame,
    IgdbInvolvedCompany,
    IgdbReleaseDate,
    IgdbScreenshot,
    IgdbStoreMatch,
    IgdbVideo,
    IgdbWebsite,
    build_cover_url,
    fix_url_protocol,
)

logger = logging.getLogger(__name__)


class IgdbProvider(AbstractMetadataProvider):
    """IGDB metadata provider

    Provides game metadata from IGDB in the same format as store plugins:
    - get_game_metadata() / get_games_metadata_bulk() - standard interface
    - lookup_igdb_id() - for MetadataManager to find IGDB matches

    Supports two modes:
    - Proxy mode (default): Routes through the luducat IGDB proxy, no credentials needed
    - BYOK mode: Direct IGDB API access when user provides Twitch credentials

    The mode is auto-detected: credentials present → BYOK, else → proxy.
    """

    # Time-to-live for failed matches - retry after this period
    FAILED_MATCH_TTL = timedelta(days=60)

    def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
        super().__init__(config_dir, cache_dir, data_dir)
        self._api: Optional[IgdbApi] = None
        self._db: Optional[IgdbDatabase] = None
        self._auth_attempted: bool = False  # Track if auth was already attempted

    def set_settings(self, settings: Dict[str, Any]) -> None:
        """Override to invalidate cached API when settings change"""
        super().set_settings(settings)
        # Clear cached API so it's recreated with new credentials
        if self._api:
            self._api.close()
            self._api = None
        # Reset auth flag so new credentials can be tried
        self._auth_attempted = False

    @property
    def provider_name(self) -> str:
        return "igdb"

    @property
    def display_name(self) -> str:
        return "IGDB"

    @property
    def store_match_table(self) -> str:
        return "igdb_store_matches"

    def _get_api(self) -> IgdbApi:
        """Get or create API client

        Auto-detects mode based on credential availability:
        - Credentials present → BYOK mode (direct IGDB access)
        - No credentials → Proxy mode (zero-config via luducat proxy)
        """
        if self._api is None:
            # Migrate old token.json to keyring if it exists
            self._migrate_token_from_file()

            client_id = self.get_credential("client_id") or self.get_setting("client_id")
            client_secret = (
                self.get_credential("client_secret")
                or self.get_setting("client_secret")
            )

            has_own_credentials = bool(client_id and client_secret)

            if has_own_credentials:
                # BYOK mode — direct IGDB access with user's own Twitch credentials
                logger.debug("IGDB _get_api: BYOK mode (user credentials)")
                self._api = IgdbApi(
                    client_id=client_id,
                    client_secret=client_secret,
                    get_credential=self.get_credential,
                    set_credential=self.set_credential,
                    delete_credential=self.delete_credential,
                    http_client=self.http,
                )
            else:
                # Proxy mode — zero-config via luducat metadata proxy
                from luducat.plugins.sdk.proxy import get_proxy_url

                proxy_url = self.get_setting("proxy_url") or get_proxy_url()
                logger.debug(f"IGDB _get_api: Proxy mode ({proxy_url})")
                self._api = IgdbApi(
                    proxy_mode=True,
                    proxy_url=proxy_url,
                    get_credential=self.get_credential,
                    set_credential=self.set_credential,
                    delete_credential=self.delete_credential,
                    http_client=self.http,
                )

        return self._api

    def _migrate_token_from_file(self) -> None:
        """Migrate OAuth token from token.json to keyring

        Checks if old token.json exists in cache_dir, migrates to keyring,
        and deletes the file after successful migration.
        """
        token_file = self.cache_dir / "token.json"
        if not token_file.exists():
            return

        try:
            with open(token_file) as f:
                data = json.load(f)

            access_token = data.get("access_token")
            expires_at = data.get("expires_at")

            if access_token and expires_at:
                # Migrate to keyring
                self.set_credential("access_token", access_token)
                self.set_credential("token_expires_at", expires_at)
                logger.info("Migrated IGDB token from token.json to keyring")

            # Delete the old file after successful migration
            token_file.unlink()
            logger.info(f"Deleted old token.json: {token_file}")

        except Exception as e:
            logger.warning(f"Failed to migrate IGDB token from file: {e}")

    def _get_db(self) -> IgdbDatabase:
        """Get or create database"""
        if self._db is None:
            self._db = IgdbDatabase(self.get_database_path())
        return self._db

    def is_available(self) -> bool:
        """Check if IGDB is available (always True — proxy mode works without credentials)"""
        return True

    async def authenticate(self) -> bool:
        """Authenticate with IGDB (Twitch OAuth or proxy)"""
        try:
            api = self._get_api()
            if api._proxy_mode:
                return True  # Proxy handles auth server-side
            return await api.authenticate()
        except Exception as e:
            logger.error(f"IGDB authentication failed: {e}")
            raise AuthenticationError(f"IGDB authentication failed: {e}") from e

    def is_authenticated(self) -> bool:
        """Check if we have valid IGDB access

        In proxy mode, always returns True (no user credentials needed).
        In BYOK mode, attempts to authenticate ONCE if token is missing.
        """
        try:
            api = self._get_api()
            # Proxy mode is always "authenticated"
            if api._proxy_mode:
                return True
            # If already authenticated, return True
            if api.is_authenticated():
                return True
            # Only attempt auth once per session
            if not self._auth_attempted:
                self._auth_attempted = True
                try:
                    api.authenticate_sync()
                    logger.debug("IGDB authentication successful")
                except Exception as e:
                    logger.debug(f"IGDB authentication attempt failed: {e}")
                    return False
            return api.is_authenticated()
        except AuthenticationError as e:
            logger.debug(f"IGDB is_authenticated failed: {e}")
            return False

    def get_database_path(self) -> Path:
        return self.data_dir / "igdb.db"

    # =========================================================================
    # STORE-PLUGIN-LIKE INTERFACE
    # =========================================================================

    def get_game_metadata(self, igdb_id: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a single game in standard format

        Same interface as store plugins (get_game_metadata).
        Checks local database first, fetches from API if needed.

        Args:
            igdb_id: IGDB game ID (as string for interface compatibility)

        Returns:
            Dict with metadata in standard format or None
        """
        try:
            game_id = int(igdb_id)
        except (ValueError, TypeError):
            logger.warning(f"Invalid IGDB ID: {igdb_id}")
            return None

        db = self._get_db()

        # Check local database first
        game = db.get_game(game_id)
        if game:
            return self._game_to_metadata_dict(game)

        # Fetch from API and store
        try:
            api = self._get_api()
            game_data = api.fetch_full_game_data(game_id)
            if game_data:
                igdb_game = self._store_game_from_api_data(game_data)
                return self._game_to_metadata_dict(igdb_game)
        except Exception as e:
            logger.warning(f"Failed to fetch IGDB game {game_id}: {e}")

        return None

    def get_games_metadata_bulk(
        self,
        igdb_ids: List[str],
        status_callback: ProgressCallback = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        title_map: Optional[Dict[int, str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games in standard format

        Same interface as store plugins (get_games_metadata_bulk).
        Checks local database first, fetches missing from API.

        Args:
            igdb_ids: List of IGDB game IDs (as strings)
            status_callback: Optional callback for progress updates
            cancel_check: Optional callback that returns True if cancelled
            title_map: Optional mapping of igdb_id (int) -> game title for display

        Returns:
            Dict mapping igdb_id -> metadata dict
        """
        if not igdb_ids:
            return {}

        results = {}
        db = self._get_db()
        api = self._get_api()
        title_map = title_map or {}

        # Convert to ints and track which need fetching
        ids_to_fetch = []
        for i, igdb_id in enumerate(igdb_ids):
            try:
                game_id = int(igdb_id)
            except (ValueError, TypeError):
                continue

            # Check local database first
            game = db.get_game(game_id)
            if game:
                results[igdb_id] = self._game_to_metadata_dict(game)
            else:
                ids_to_fetch.append(game_id)

        # Fetch missing games from API
        if ids_to_fetch:
            fetch_total = len(ids_to_fetch)
            if status_callback:
                status_callback(f"Fetching {fetch_total} games from IGDB...", 0, fetch_total)

            for i, game_id in enumerate(ids_to_fetch):
                # Show game title in progress (fall back to ID if unknown)
                # Don't prefix with "IGDB" - the dialog already prepends the plugin name
                title = title_map.get(game_id, f"ID {game_id}")
                if status_callback:
                    status_callback(
                        f"({i+1}/{fetch_total}): {title}",
                        i, fetch_total
                    )

                try:
                    logger.debug(f"Fetching IGDB game [{i+1}/{fetch_total}]: {title}")
                    # Don't pass status_callback to per-game fetch — its sub-calls
                    # send (-1,-1) which makes the progress bar flicker to indeterminate
                    game_data = api.fetch_full_game_data(game_id)
                    if game_data:
                        game_name = game_data.get("name", "Unknown")
                        logger.debug(f"  -> Storing: {game_name}")
                        igdb_game = self._store_game_from_api_data(game_data)
                        results[str(game_id)] = self._game_to_metadata_dict(igdb_game)
                except Exception as e:
                    logger.warning(f"Failed to fetch IGDB game {game_id} ({title}): {e}")

                # Check cancel AFTER work — save results from current iteration
                if cancel_check and cancel_check():
                    # Signal the API layer so any in-flight request is interrupted
                    api.cancel()
                    logger.info(f"IGDB metadata fetch cancelled after {i+1}/{fetch_total}")
                    break

        return results

    def _game_to_metadata_dict(self, game: IgdbGame) -> Dict[str, Any]:
        """Convert IgdbGame to standard metadata dict format

        Matches the format used by store plugins for compatibility with
        MetadataResolver.

        Args:
            game: IgdbGame object with relationships loaded

        Returns:
            Dict in standard metadata format
        """
        # Screenshots - use full URLs from database
        screenshots = []
        if game.screenshots:
            screenshots = [s.url for s in game.screenshots if s.url]

        # Convert release timestamp to ISO date string
        release_date = ""
        if game.first_release_date:
            try:
                dt = utc_from_timestamp(game.first_release_date)
                release_date = dt.strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass

        # Website type mapping for display
        website_type_map = {
            1: "official", 3: "wikipedia", 13: "steam",
            14: "reddit", 15: "itch", 16: "epic", 17: "gog", 18: "discord",
        }
        websites = []
        for w in (game.websites or []):
            wtype = website_type_map.get(w.website_type)
            if wtype and w.url:
                websites.append({"type": wtype, "url": w.url})

        # Age rating mappings
        # IGDB API V4 renamed: category→organization, rating→rating_category
        # and uses a sequential enum across all organizations
        age_org_map = {
            1: "ESRB", 2: "PEGI", 3: "CERO", 4: "USK",
            5: "GRAC", 6: "Class_Ind", 7: "ACB",
        }
        # New rating_category enum (sequential per organization)
        esrb_map = {1: "RP", 2: "EC", 3: "E", 4: "E10+", 5: "T", 6: "M", 7: "AO"}
        pegi_map = {8: "3", 9: "7", 10: "12", 11: "16", 12: "18"}
        usk_map = {18: "0", 19: "6", 20: "12", 21: "16", 22: "18"}
        acb_map = {35: "G", 36: "PG", 37: "M", 38: "MA15+", 39: "R18+", 40: "RC"}
        age_ratings = []
        for ar in (game.age_ratings or []):
            org = ar.category  # stored from API's "organization" field
            rating_cat = ar.rating  # stored from API's "rating_category" field
            system = age_org_map.get(org)
            if not system:
                continue
            if org == 1:  # ESRB
                rating_str = esrb_map.get(rating_cat, str(rating_cat) if rating_cat else "")
            elif org == 2:  # PEGI
                rating_str = pegi_map.get(rating_cat, str(rating_cat) if rating_cat else "")
            elif org == 4:  # USK
                rating_str = usk_map.get(rating_cat, str(rating_cat) if rating_cat else "")
            elif org == 7:  # ACB
                rating_str = acb_map.get(rating_cat, str(rating_cat) if rating_cat else "")
            else:
                rating_str = str(rating_cat) if rating_cat else ""
            if rating_str:
                age_ratings.append({"system": system, "rating": rating_str})

        # Game category mapping (1=main, 2=dlc, etc.)
        category_map = {
            0: "main_game", 1: "dlc_addon", 2: "expansion", 3: "bundle",
            4: "standalone_expansion", 5: "mod", 6: "episode", 7: "season",
            8: "remake", 9: "remaster", 10: "expanded_game", 11: "port", 12: "fork",
            13: "pack", 14: "update",
        }
        category = category_map.get(getattr(game, "category", None), "")

        # Status mapping (0=released, 2=alpha, 3=beta, etc.)
        status_map = {
            0: "released", 2: "alpha", 3: "beta", 4: "early_access",
            5: "offline", 6: "cancelled", 7: "rumored", 8: "delisted",
        }
        status = status_map.get(getattr(game, "status", None), "")

        # Videos (YouTube IDs)
        videos = []
        if hasattr(game, "videos") and game.videos:
            videos = [{"id": v.video_id, "name": v.name} for v in game.videos if v.video_id]

        # Build per-platform release_date dict from release_dates relationship
        release_dates_dict: Dict[str, str] = {}
        if hasattr(game, "release_dates") and game.release_dates:
            from luducat.plugins.base import IGDB_PLATFORM_NORMALIZATION
            for rd in game.release_dates:
                platform_key = IGDB_PLATFORM_NORMALIZATION.get(rd.platform_id)
                if not platform_key and hasattr(rd, "platform") and rd.platform:
                    platform_key = getattr(rd.platform, "slug", None)
                if not platform_key:
                    continue
                if rd.date:
                    try:
                        dt_obj = utc_from_timestamp(rd.date)
                        date_str = dt_obj.strftime("%Y-%m-%d")
                    except (ValueError, OSError):
                        continue
                elif hasattr(rd, "y") and rd.y:
                    date_str = f"{rd.y}-01-01"
                else:
                    continue
                # Keep oldest date per platform
                if (platform_key not in release_dates_dict
                        or date_str < release_dates_dict[platform_key]):
                    release_dates_dict[platform_key] = date_str
        elif release_date:
            # Fallback: replicate single date across known platforms
            for pname in (game.platform_names or []):
                from luducat.plugins.base import PLATFORM_NAME_NORMALIZATION
                pkey = PLATFORM_NAME_NORMALIZATION.get(pname.lower(), pname.lower())
                if pkey not in release_dates_dict:
                    release_dates_dict[pkey] = release_date

        return {
            "title": game.name or "",
            "short_description": (game.summary[:500] if game.summary else ""),
            "description": game.summary or "",
            "storyline": game.storyline or "",
            "header_url": game.cover_url or "",
            "cover": game.cover_url or "",
            "hero": game.background_url or "",
            "screenshots": screenshots,
            "release_date": release_dates_dict if release_dates_dict else release_date,
            "developers": game.developers,
            "publishers": game.publishers,
            "genres": game.genre_names,
            "themes": game.theme_names,
            "franchise": game.franchise_names,
            "collections": game.collection_names,
            "game_modes": game.game_mode_names,
            "platforms": game.platform_names,
            "perspectives": game.player_perspective_names,
            "age_ratings": age_ratings,
            "links": websites,
            "artworks": game.artwork_urls,
            "videos": videos,
            "rating": game.rating,
            "user_rating": game.aggregated_rating,
            "user_rating_count": game.aggregated_rating_count,
            "total_rating": getattr(game, "total_rating", None),
            "category": category,
            "status": status,
            "slug": getattr(game, "slug", "") or "",
            "igdb_id": game.igdb_id,
            "igdb_url": game.url,
        }

    # =========================================================================
    # IGDB ID LOOKUP (for MetadataManager)
    # =========================================================================

    def lookup_igdb_id(
        self,
        store_name: str,
        store_app_id: str,
        normalized_title: Optional[str] = None,
        status_callback: ProgressCallback = None
    ) -> Optional[int]:
        """Look up IGDB game ID for a store game

        Called by MetadataManager to find IGDB matches.
        Uses external_games API and/or title search.
        Caches results to avoid repeated API calls.

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_app_id: Store's app ID
            normalized_title: Optional normalized title for fallback search
            status_callback: Optional callback for progress updates

        Returns:
            IGDB game ID or None if not found
        """
        db = self._get_db()

        # Check cache first
        match = db.get_store_match(store_name, str(store_app_id))
        if match:
            # Cache hit - check if we found an IGDB match
            if match.igdb_id:
                logger.debug(
                    "IGDB lookup cache hit: %s:%s -> %s",
                    store_name, store_app_id, match.igdb_id,
                )
                return match.igdb_id

            # Cached "no_match" - check if we should retry
            if match.match_method == "no_match":
                # Check TTL - retry if the failed match is old enough
                match_age = (
                    utc_now() - match.updated_at
                    if match.updated_at else timedelta(days=999)
                )
                should_retry = match_age > self.FAILED_MATCH_TTL

                # Also retry if we have a title and haven't tried title search yet
                if should_retry or normalized_title:
                    logger.debug(
                        f"IGDB cache no_match (age={match_age.days}d), "
                        f"retrying: {store_name}:{store_app_id}"
                    )
                    api = self._get_api()

                    # Try external_games first if TTL expired
                    igdb_id = None
                    if should_retry and store_name != "epic":
                        igdb_id = api.lookup_by_store_id(
                            store_name, str(store_app_id),
                            status_callback,
                        )

                    # Then try title search if we have a title
                    if igdb_id is None and normalized_title:
                        igdb_id = api.search_game_by_title(normalized_title, status_callback)

                    if igdb_id:
                        # Update cache with successful match
                        db.save_store_match(
                            store_name=store_name,
                            store_app_id=str(store_app_id),
                            igdb_id=igdb_id,
                            match_method="title_search" if normalized_title else "external_games",
                            confidence=0.9,
                            normalized_title=normalized_title
                        )
                        return igdb_id
                    elif should_retry:
                        # Update timestamp so we don't retry again immediately
                        db.save_store_match(
                            store_name=store_name,
                            store_app_id=str(store_app_id),
                            igdb_id=None,
                            match_method="no_match",
                            confidence=0.0,
                            normalized_title=normalized_title
                        )

            # True no_match (not expired and already tried available methods)
            return None

        # Cache miss - try external_games API lookup
        logger.debug(f"IGDB lookup cache miss: {store_name}:{store_app_id} - querying API...")
        api = self._get_api()

        # Epic app_names are UUIDs that don't match IGDB external_games - skip to title search
        if store_name == "epic":
            igdb_id = None
            logger.debug(f"Skipping external_games lookup for Epic (UUID app_name: {store_app_id})")
        else:
            igdb_id = api.lookup_by_store_id(store_name, str(store_app_id), status_callback)

        # Determine match method for caching
        match_method = "external_games" if igdb_id else "no_match"

        # If not found and we have a title, try search
        if igdb_id is None and normalized_title:
            igdb_id = api.search_game_by_title(normalized_title, status_callback)
            if igdb_id:
                match_method = "title_search"

        # Cache result (even if None - to avoid repeated lookups)
        db.save_store_match(
            store_name=store_name,
            store_app_id=str(store_app_id),
            igdb_id=igdb_id,
            match_method=match_method,
            confidence=1.0 if match_method == "external_games" else (0.9 if igdb_id else 0.0),
            normalized_title=normalized_title
        )

        return igdb_id

    def lookup_store_ids_batch(
        self,
        store_name: str,
        store_ids: List[str],
        status_callback: ProgressCallback = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, int]:
        """Look up IGDB IDs for multiple store games

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_ids: List of store app IDs
            status_callback: Optional callback for progress updates
            cancel_check: Optional callback that returns True if cancelled

        Returns:
            Dict mapping store_app_id -> igdb_id for found matches
        """
        db = self._get_db()
        api = self._get_api()

        # Check cache first
        results = {}
        uncached = []

        for store_id in store_ids:
            match = db.get_store_match(store_name, str(store_id))
            if match:
                if match.igdb_id:
                    results[store_id] = match.igdb_id
                elif match.match_method == "no_match":
                    # Check TTL before retrying failed matches
                    match_age = (
                        utc_now() - match.updated_at
                        if match.updated_at
                        else timedelta(days=999)
                    )
                    if match_age > self.FAILED_MATCH_TTL:
                        uncached.append(store_id)  # TTL expired, retry
                    # else: within TTL, skip (don't retry)
            else:
                uncached.append(store_id)

        # Batch lookup uncached IDs
        if uncached:
            if cancel_check and cancel_check():
                api.cancel()
                return results

            if status_callback:
                status_callback(
                    f"Looking up {len(uncached)} "
                    f"{store_name} games in IGDB...",
                    0, len(uncached),
                )

            new_matches = api.lookup_store_ids_batch(
                store_name, uncached,
                status_callback=status_callback,
            )

            # Cache results
            for store_id in uncached:
                igdb_id = new_matches.get(store_id)
                db.save_store_match(
                    store_name=store_name,
                    store_app_id=str(store_id),
                    igdb_id=igdb_id,
                    match_method="external_games" if igdb_id else "no_match",
                    confidence=1.0 if igdb_id else 0.0
                )

            results.update(new_matches)

        return results

    # =========================================================================
    # API DATA STORAGE
    # =========================================================================

    def _store_game_from_api_data(
        self,
        data: Dict[str, Any],
        status_callback: ProgressCallback = None
    ) -> IgdbGame:
        """Store game data from API response into database

        Handles the normalized schema with related tables.

        Args:
            data: Full game data from API (including release_dates, covers, etc.)
            status_callback: Optional callback for progress updates

        Returns:
            The stored IgdbGame object
        """
        db = self._get_db()
        session = db.get_session()

        try:
            # Create main game object
            game = IgdbGame(
                igdb_id=data.get("id"),
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=fix_url_protocol(data.get("url")),
                normalized_title=normalize_title(data.get("name", "")),
                summary=data.get("summary"),
                storyline=data.get("storyline"),
                category=data.get("category"),
                status=data.get("status"),
                checksum=data.get("checksum"),
                rating=data.get("rating"),
                rating_count=data.get("rating_count"),
                aggregated_rating=data.get("aggregated_rating"),
                aggregated_rating_count=data.get("aggregated_rating_count"),
                total_rating=data.get("total_rating"),
                total_rating_count=data.get("total_rating_count"),
            )

            # Process release dates - get oldest PC/DOS date
            release_dates = data.get("release_dates", [])
            pc_release = get_pc_release_date(release_dates)
            if pc_release:
                game.first_release_date = pc_release
                try:
                    game.release_year = utc_from_timestamp(pc_release).year
                except (ValueError, OSError):
                    pass
            elif data.get("first_release_date"):
                game.first_release_date = data.get("first_release_date")
                try:
                    game.release_year = utc_from_timestamp(data["first_release_date"]).year
                except (ValueError, OSError):
                    pass

            # Process covers - use first cover's full URL
            covers = data.get("covers", [])
            if covers:
                cover = covers[0]
                game.cover_id = cover.get("id")
                game.cover_image_id = cover.get("image_id")
                game.cover_url = cover.get("full_url")

            # Process artworks - use first artwork as background
            artworks = data.get("artworks", [])
            if artworks:
                game.background_url = artworks[0].get("full_url")

            # Upsert game (INSERT if new, UPDATE if exists)
            game = session.merge(game)
            session.flush()

            # Clear many-to-many relationships for clean re-fetch
            game.genres.clear()
            game.themes.clear()
            game.keywords.clear()
            game.franchises.clear()
            game.collections.clear()
            game.game_modes.clear()
            game.platforms.clear()
            game.player_perspectives.clear()

            # Process many-to-many relationships (lookup tables)
            for genre_data in data.get("genres", []):
                if isinstance(genre_data, dict) and "id" in genre_data:
                    genre = db.get_or_create_genre(genre_data, session)
                    game.genres.append(genre)

            for theme_data in data.get("themes", []):
                if isinstance(theme_data, dict) and "id" in theme_data:
                    theme = db.get_or_create_theme(theme_data, session)
                    game.themes.append(theme)

            for keyword_data in data.get("keywords", []):
                if isinstance(keyword_data, dict) and "id" in keyword_data:
                    keyword = db.get_or_create_keyword(keyword_data, session)
                    game.keywords.append(keyword)

            for franchise_data in data.get("franchises", []):
                if isinstance(franchise_data, dict) and "id" in franchise_data:
                    franchise = db.get_or_create_franchise(franchise_data, session)
                    game.franchises.append(franchise)

            for collection_data in data.get("collections", []):
                if isinstance(collection_data, dict) and "id" in collection_data:
                    collection = db.get_or_create_collection(collection_data, session)
                    game.collections.append(collection)

            for mode_data in data.get("game_modes", []):
                if isinstance(mode_data, dict) and "id" in mode_data:
                    mode = db.get_or_create_game_mode(mode_data, session)
                    game.game_modes.append(mode)

            for platform_data in data.get("platforms", []):
                if isinstance(platform_data, dict) and "id" in platform_data:
                    platform = db.get_or_create_platform(platform_data, session)
                    game.platforms.append(platform)

            for perspective_data in data.get("player_perspectives", []):
                if isinstance(perspective_data, dict) and "id" in perspective_data:
                    perspective = db.get_or_create_player_perspective(perspective_data, session)
                    game.player_perspectives.append(perspective)

            # Process one-to-many relationships (per-game data)

            # Screenshots
            for ss_data in data.get("screenshots", []):
                if isinstance(ss_data, dict) and ss_data.get("id"):
                    screenshot = IgdbScreenshot(
                        id=ss_data["id"],
                        game_id=game.igdb_id,
                        image_id=ss_data.get("image_id", ""),
                        url=ss_data.get("full_url"),
                        width=ss_data.get("width"),
                        height=ss_data.get("height"),
                        alpha_channel=ss_data.get("alpha_channel", False),
                        animated=ss_data.get("animated", False),
                        checksum=ss_data.get("checksum"),
                    )
                    session.merge(screenshot)

            # Artworks
            for art_data in data.get("artworks", []):
                if isinstance(art_data, dict) and art_data.get("id"):
                    artwork = IgdbArtwork(
                        id=art_data["id"],
                        game_id=game.igdb_id,
                        image_id=art_data.get("image_id", ""),
                        url=art_data.get("full_url"),
                        width=art_data.get("width"),
                        height=art_data.get("height"),
                        artwork_type=art_data.get("artwork_type"),
                        checksum=art_data.get("checksum"),
                    )
                    session.merge(artwork)

            # Videos
            for video_data in data.get("videos", []):
                if isinstance(video_data, dict) and video_data.get("id"):
                    video = IgdbVideo(
                        id=video_data["id"],
                        game_id=game.igdb_id,
                        video_id=video_data.get("video_id", ""),
                        name=video_data.get("name"),
                        checksum=video_data.get("checksum"),
                    )
                    session.merge(video)

            # Websites
            for web_data in data.get("websites", []):
                if isinstance(web_data, dict) and web_data.get("id"):
                    website = IgdbWebsite(
                        id=web_data["id"],
                        game_id=game.igdb_id,
                        url=fix_url_protocol(web_data.get("url", "")),
                        website_type=web_data.get("type"),
                        trusted=web_data.get("trusted", False),
                        checksum=web_data.get("checksum"),
                    )
                    session.merge(website)

            # External IDs
            for ext_data in data.get("external_games", []):
                if isinstance(ext_data, dict) and ext_data.get("id"):
                    external_id = IgdbExternalId(
                        id=ext_data["id"],
                        game_id=game.igdb_id,
                        source_id=ext_data.get("category") or ext_data.get("external_game_source"),
                        uid=ext_data.get("uid"),
                        name=ext_data.get("name"),
                        url=fix_url_protocol(ext_data.get("url")),
                        year=ext_data.get("year"),
                        checksum=ext_data.get("checksum"),
                    )
                    session.merge(external_id)

            # Involved Companies
            for ic_data in data.get("involved_companies", []):
                if isinstance(ic_data, dict) and ic_data.get("id"):
                    company_data = ic_data.get("company", {})
                    if isinstance(company_data, dict) and company_data.get("id"):
                        company = db.get_or_create_company(company_data, session)

                        involved = IgdbInvolvedCompany(
                            id=ic_data["id"],
                            game_id=game.igdb_id,
                            company_id=company.id,
                            developer=ic_data.get("developer", False),
                            publisher=ic_data.get("publisher", False),
                            porting=ic_data.get("porting", False),
                            supporting=ic_data.get("supporting", False),
                            checksum=ic_data.get("checksum"),
                        )
                        session.merge(involved)

            # Release Dates
            for rd_data in release_dates:
                if isinstance(rd_data, dict) and rd_data.get("id"):
                    platform_data = rd_data.get("platform", {})
                    platform_id = None
                    if isinstance(platform_data, dict):
                        if platform_data.get("id"):
                            platform = db.get_or_create_platform(platform_data, session)
                            platform_id = platform.id
                    elif isinstance(platform_data, int):
                        platform_id = platform_data

                    release_date = IgdbReleaseDate(
                        id=rd_data["id"],
                        game_id=game.igdb_id,
                        platform_id=platform_id,
                        date=rd_data.get("date"),
                        human=rd_data.get("human"),
                        region=rd_data.get("region"),
                        category=rd_data.get("category"),
                        checksum=rd_data.get("checksum"),
                    )
                    session.merge(release_date)

            # Age Ratings
            # IGDB API V4 renamed: category→organization, rating→rating_category
            for ar_data in data.get("age_ratings", []):
                if isinstance(ar_data, dict) and ar_data.get("id"):
                    age_rating = IgdbAgeRating(
                        id=ar_data["id"],
                        game_id=game.igdb_id,
                        category=ar_data.get("organization"),
                        rating=ar_data.get("rating_category"),
                        synopsis=ar_data.get("synopsis"),
                        checksum=ar_data.get("checksum"),
                    )
                    session.merge(age_rating)

            session.commit()
            igdb_id = game.igdb_id

        except Exception as e:
            session.rollback()
            # TOCTOU race: two threads both merge() the same game — the second
            # INSERT hits UNIQUE constraint.  Just return the already-stored row.
            from sqlalchemy.exc import IntegrityError
            if isinstance(e, IntegrityError) and (
                "UNIQUE constraint failed: igdb_games.igdb_id"
                in str(e)
            ):
                logger.debug(
                    "IGDB game %s already stored by "
                    "concurrent thread, returning existing",
                    data.get('id'),
                )
                igdb_id = data.get("id")
            else:
                logger.error(f"Failed to store IGDB game data: {e}")
                raise
        finally:
            session.close()

        # Re-fetch with relationships using a clean session
        return db.get_game(igdb_id)

    # =========================================================================
    # LEGACY INTERFACE (AbstractMetadataProvider)
    # =========================================================================

    async def lookup_by_store_id(
        self,
        store_name: str,
        store_id: str
    ) -> Optional[str]:
        """Look up IGDB game ID using store ID (legacy async interface)"""
        igdb_id = self.lookup_igdb_id(store_name, store_id)
        return str(igdb_id) if igdb_id else None

    async def search_game(
        self,
        title: str,
        year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Search for games by title"""
        api = self._get_api()
        results = api.search_games(title, limit=10)

        search_results = []
        normalized_query = normalize_title(title)

        for game in results:
            game_name = game.get("name", "")
            normalized_name = normalize_title(game_name)

            # Calculate confidence based on title similarity
            confidence = self._calculate_title_similarity(normalized_query, normalized_name)

            # Boost confidence if year matches
            release_date = game.get("first_release_date")
            release_year = None
            if release_date:
                release_year = utc_from_timestamp(release_date).year
                if year and release_year == year:
                    confidence = min(1.0, confidence + 0.2)

            # Get cover URL
            cover = game.get("cover")
            cover_url = None
            if cover and isinstance(cover, dict):
                image_id = cover.get("image_id")
                if image_id:
                    cover_url = build_cover_url(image_id, "cover_big")

            # Get platforms
            platforms = []
            for platform in game.get("platforms", []):
                if isinstance(platform, dict):
                    platforms.append(platform.get("name", ""))

            search_results.append(MetadataSearchResult(
                provider_id=str(game.get("id")),
                title=game_name,
                release_year=release_year,
                platforms=platforms,
                cover_url=cover_url,
                confidence=confidence
            ))

        # Sort by confidence
        search_results.sort(key=lambda x: x.confidence, reverse=True)
        return search_results

    def _calculate_title_similarity(self, query: str, candidate: str) -> float:
        """Calculate similarity between two titles (0.0 - 1.0)"""
        if query == candidate:
            return 1.0

        # Simple word overlap (Jaccard similarity)
        query_words = set(query.split())
        candidate_words = set(candidate.split())

        if not query_words or not candidate_words:
            return 0.0

        intersection = query_words & candidate_words
        union = query_words | candidate_words

        return len(intersection) / len(union)

    async def get_enrichment(
        self,
        provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a game by IGDB ID (legacy interface)"""
        metadata = self.get_game_metadata(provider_id)
        if not metadata:
            return None

        return self._metadata_to_enrichment(provider_id, metadata)

    def _metadata_to_enrichment(self, igdb_id: str, metadata: Dict[str, Any]) -> EnrichmentData:
        """Convert metadata dict to EnrichmentData"""
        # Determine franchise
        franchise = None
        franchise_list = metadata.get("franchise", [])
        collections = metadata.get("collections", [])
        if franchise_list:
            franchise = franchise_list[0] if isinstance(franchise_list, list) else franchise_list
        elif collections:
            franchise = collections[0]

        # Series
        series = None
        if franchise and collections and collections[0] != franchise:
            series = collections[0]

        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=igdb_id,
            genres=metadata.get("genres", []),
            tags=metadata.get("tags", []),
            franchise=franchise,
            series=series,
            developers=metadata.get("developers", []),
            publishers=metadata.get("publishers", []),
            summary=metadata.get("short_description"),
            storyline=metadata.get("storyline"),
            release_date=metadata.get("release_date"),
            cover_url=metadata.get("cover"),
            background_url=metadata.get("hero"),
            screenshots=metadata.get("screenshots", []),
            user_rating=metadata.get("user_rating"),
            user_rating_count=metadata.get("user_rating_count"),
            themes=metadata.get("themes", []),
            platforms=metadata.get("platforms", []),
            perspectives=metadata.get("perspectives", []),
            age_ratings=metadata.get("age_ratings", []),
            websites=metadata.get("links", []),
            extra={
                "keywords": metadata.get("keywords", []),
                "game_modes": metadata.get("game_modes", []),
                "artworks": metadata.get("artworks", []),
            }
        )

    def get_cached_enrichment(
        self,
        store_name: str,
        store_id: str
    ) -> Optional[EnrichmentData]:
        """Get cached enrichment data (legacy interface)"""
        igdb_id = self.lookup_igdb_id(store_name, store_id)
        if not igdb_id:
            return None

        metadata = self.get_game_metadata(str(igdb_id))
        if not metadata:
            return None

        return self._metadata_to_enrichment(str(igdb_id), metadata)

    # =========================================================================
    # BATCH ENRICHMENT (sync optimized)
    # =========================================================================

    def resolve_steam_app_ids(
        self, store_name: str, app_ids: List[str]
    ) -> Dict[str, tuple]:
        """Resolve Steam AppIDs for non-Steam games using IGDB data.

        Pure local database query, no API calls.

        Args:
            store_name: Source store ("gog", "epic")
            app_ids: List of store app IDs

        Returns:
            Dict mapping store_app_id -> (steam_app_id, reference_title)
        """
        db = self._get_db()
        return db.get_steam_ids_for_store(store_name, app_ids)

    def resolve_cross_store_id(
        self,
        source_store: str,
        source_app_id: str,
        target_store: str,
        normalized_title: str = "",
    ) -> tuple:
        """Find target store's app_id for a game known by source store's ID.

        Uses cached igdb_store_matches → IgdbGame.external_ids → target source_id.
        May trigger API lookup if IGDB match not yet cached.

        Args:
            source_store: Store the game is known in (e.g., "epic")
            source_app_id: App ID in the source store
            target_store: Store to resolve to (e.g., "steam")
            normalized_title: Normalized title for fallback search

        Returns:
            Tuple of (target_app_id, reference_title) or (None, None)
        """
        igdb_id = self.lookup_igdb_id(
            source_store, source_app_id, normalized_title
        )
        if not igdb_id:
            return None, None
        db = self._get_db()
        game = db.get_game(igdb_id)
        if not game:
            return None, None
        target_id = game.get_store_id(target_store)
        return target_id, game.name

    async def enrich_games(
        self,
        games: List[Game],
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        cross_store_ids: Optional[Dict[str, str]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Enrich multiple games during sync

        Three-phase approach:
        1. Fast batch lookup via external_games API
        2. Title search fallback for unmatched games
        3. Get metadata for all matched games
        """
        if not games:
            return {}

        results = {}
        total = len(games)
        db = self._get_db()
        api = self._get_api()

        # Reset API cancel state for this enrichment run
        api.reset_cancel()

        try:
            results = self._do_enrich_games(
                games, db, api, total, status_callback, cancel_check
            )
        except IgdbCancelledError:
            logger.info("IGDB enrichment interrupted by cancellation")
        except IgdbRateLimitError:
            logger.warning("IGDB enrichment stopped — circuit breaker open")

        if status_callback:
            status_callback(f"Enriched {len(results)} games", total, total)

        logger.info(f"IGDB enrichment complete: {len(results)}/{total} games enriched")
        return results

    def _do_enrich_games(
        self,
        games: List[Game],
        db,
        api: IgdbApi,
        total: int,
        status_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, EnrichmentData]:
        """Inner enrichment logic, separated for IgdbCancelledError handling."""
        results = {}

        # Helper that checks cancel_check and also signals the API
        def _check_cancelled() -> bool:
            if cancel_check and cancel_check():
                api.cancel()  # Wake any sleeping API waits
                return True
            return False

        # Group games by store for batch lookup
        store_games: Dict[str, List[Game]] = {}
        for game in games:
            store_name = game.store_name
            if store_name not in store_games:
                store_games[store_name] = []
            store_games[store_name].append(game)

        # Phase 1: batch lookup store IDs (fast)
        enrichment_start = utc_now()

        if status_callback:
            status_callback("Looking up games in IGDB...", 0, total)

        game_to_igdb: Dict[str, int] = {}  # store_app_id -> igdb_id
        unmatched_games: List[Game] = []  # Games not found via store ID

        for store_name, store_game_list in store_games.items():
            if _check_cancelled():
                logger.info("IGDB enrichment cancelled during phase 1")
                return results

            store_ids = [g.store_app_id for g in store_game_list]
            matches = self.lookup_store_ids_batch(
                store_name, store_ids, status_callback, cancel_check=cancel_check,
            )

            for game in store_game_list:
                igdb_id = matches.get(game.store_app_id)
                if igdb_id:
                    game_to_igdb[game.store_app_id] = igdb_id
                else:
                    unmatched_games.append(game)

        # Phase 2: title search fallback for unmatched games
        # Filter out games that have a recent no_match cache entry (within TTL)
        if unmatched_games:
            games_for_title_search = []
            for game in unmatched_games:
                match = db.get_store_match(game.store_name, str(game.store_app_id))
                if match and match.match_method == "no_match":
                    match_age = (
                        utc_now() - match.updated_at
                        if match.updated_at
                        else timedelta(days=999)
                    )
                    # Don't skip entries created during this run (by Phase 1)
                    created_this_run = (
                        match.updated_at
                        and match.updated_at >= enrichment_start
                    )
                    if match_age <= self.FAILED_MATCH_TTL and not created_this_run:
                        continue  # Skip — cached from a previous run, within TTL
                games_for_title_search.append(game)

            skipped = len(unmatched_games) - len(games_for_title_search)
            if skipped:
                logger.info(f"IGDB: Skipped {skipped} games with cached no-match within TTL")
            unmatched_games = games_for_title_search

        if unmatched_games and not _check_cancelled():
            logger.info(
                "IGDB: %d games not found via store ID,"
                " trying title search...",
                len(unmatched_games),
            )

            for i, game in enumerate(unmatched_games):
                if status_callback:
                    status_callback(
                        f"Title search ({i+1}/{len(unmatched_games)}): {game.title[:40]}...",
                        len(game_to_igdb) + i,
                        total
                    )

                # Use original title for search (preserves punctuation)
                title = game.title if hasattr(game, 'title') else game.normalized_title
                if not title:
                    continue

                igdb_id = api.search_game_by_title(title, None)

                if igdb_id:
                    game_to_igdb[game.store_app_id] = igdb_id
                    # Cache the successful match
                    db.save_store_match(
                        store_name=game.store_name,
                        store_app_id=game.store_app_id,
                        igdb_id=igdb_id,
                        match_method="title_search",
                        confidence=0.9,
                        normalized_title=(
                            game.normalized_title
                            if hasattr(game, 'normalized_title')
                            else None
                        ),
                    )
                else:
                    # Cache as no_match to avoid repeated lookups
                    db.save_store_match(
                        store_name=game.store_name,
                        store_app_id=game.store_app_id,
                        igdb_id=None,
                        match_method="no_match",
                        confidence=0.0,
                        normalized_title=(
                            game.normalized_title
                            if hasattr(game, 'normalized_title')
                            else None
                        ),
                    )

                # Check cancel AFTER work — save results from this iteration
                if _check_cancelled():
                    logger.info(
                        "IGDB enrichment cancelled during "
                        "title search after %d/%d",
                        i + 1, len(unmatched_games),
                    )
                    break

        # Phase 3: get metadata for matched games
        if _check_cancelled():
            logger.info("IGDB enrichment cancelled before metadata fetch")
            return results

        # Build igdb_id → game_title map for progress display
        igdb_to_title: Dict[int, str] = {}
        for game in games:
            igdb_id = game_to_igdb.get(game.store_app_id)
            if igdb_id and igdb_id not in igdb_to_title:
                igdb_to_title[igdb_id] = game.title or f"ID {igdb_id}"

        igdb_ids = list(set(str(igdb_id) for igdb_id in game_to_igdb.values()))
        metadata_bulk = self.get_games_metadata_bulk(
            igdb_ids, status_callback, cancel_check=cancel_check,
            title_map=igdb_to_title,
        )

        # Build results
        for game in games:
            igdb_id = game_to_igdb.get(game.store_app_id)
            if igdb_id:
                metadata = metadata_bulk.get(str(igdb_id))
                if metadata:
                    results[game.store_app_id] = (
                        self._metadata_to_enrichment(
                            str(igdb_id), metadata,
                        )
                    )

        return results

    # =========================================================================
    # ON-DEMAND METADATA (for MetadataResolver)
    # =========================================================================

    def get_metadata_for_store_game(
        self,
        store_name: str,
        store_id: str,
        normalized_title: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Get metadata for a game on-demand using store info

        Implementation of the generic AbstractMetadataProvider interface.
        Called by MetadataResolver when store plugins don't have the data.

        Args:
            store_name: Store identifier ("steam", "gog", "epic")
            store_id: Store's app ID
            normalized_title: Optional normalized title for fallback search

        Returns:
            Dict with metadata or None if game not found
        """
        try:
            # Reset cancel state — a previous sync/close may have set it,
            # but on-demand requests are independent operations.
            api = self._get_api()
            api.reset_cancel()

            # Look up IGDB ID from store info
            igdb_id = self.lookup_igdb_id(
                store_name, store_id,
                normalized_title=normalized_title
            )

            if igdb_id:
                # Get full metadata
                return self.get_game_metadata(str(igdb_id))

            return None

        except Exception as e:
            logger.debug(f"IGDB on-demand lookup failed for {store_name}:{store_id}: {e}")
            return None

    # =========================================================================
    # BULK GAME MODE QUERY
    # =========================================================================

    def get_game_modes_bulk(
        self, store_app_ids: Dict[str, List[str]]
    ) -> Dict[str, Dict[str, List[str]]]:
        """Get game modes for multiple store games at once

        Queries local database only (no API calls). Used by game_service
        to populate game mode badges at startup.

        Args:
            store_app_ids: Dict mapping store_name -> list of app_ids
                          e.g. {"steam": ["440", "730"], "gog": ["1234"]}

        Returns:
            Nested dict: {store_name: {app_id: [game_mode_names]}}
            e.g. {"steam": {"440": ["Single player", "Multiplayer"]}}
        """
        result: Dict[str, Dict[str, List[str]]] = {}
        db = self._get_db()

        try:
            with db.get_session() as session:
                # Use raw SQL for efficiency - single query with JOINs
                from sqlalchemy import text

                # Build lookup sets for fast filtering
                store_app_sets = {
                    store: set(ids) for store, ids in store_app_ids.items()
                }

                # Query all game modes for matched games in one go
                sql = text("""
                    SELECT sm.store_name, sm.store_app_id, gm.name
                    FROM igdb_store_matches sm
                    JOIN game_game_modes ggm ON sm.igdb_id = ggm.game_id
                    JOIN igdb_game_modes gm ON ggm.game_mode_id = gm.id
                    WHERE sm.igdb_id IS NOT NULL
                """)

                rows = session.execute(sql).fetchall()

                # Build result dict (filter to requested app_ids only)
                for row in rows:
                    store_name = row[0]
                    app_id = row[1]
                    mode_name = row[2]
                    if store_name not in store_app_sets:
                        continue
                    if app_id not in store_app_sets[store_name]:
                        continue
                    if store_name not in result:
                        result[store_name] = {}
                    if app_id not in result[store_name]:
                        result[store_name][app_id] = []
                    result[store_name][app_id].append(mode_name)

        except Exception as e:
            logger.warning(f"Failed to get bulk game modes: {e}")

        return result

    def get_cache_metadata_bulk(
        self, store_app_ids: Dict[str, List[str]]
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Bulk local-DB query for cache build. No API calls.

        Uses raw SQL (no ORM relationship loading) for speed.
        Fetches scalar fields from igdb_games + targeted bulk queries
        for key many-to-many fields (genres, themes, franchises,
        developers, publishers, screenshots).

        Args:
            store_app_ids: Dict mapping store_name -> list of app_ids

        Returns:
            Nested dict: {store_name: {app_id: metadata_dict}}
        """
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        db = self._get_db()

        try:
            with db.get_session() as session:
                from sqlalchemy import text

                store_app_sets = {
                    store: set(ids) for store, ids in store_app_ids.items()
                }

                # 1) Scalar fields via single JOIN (igdb_store_matches → igdb_games)
                rows = session.execute(text("""
                    SELECT sm.store_name, sm.store_app_id, sm.igdb_id,
                           g.name, g.summary, g.cover_url, g.background_url,
                           g.first_release_date, g.rating, g.aggregated_rating,
                           g.total_rating, g.category, g.status, g.slug, g.url
                    FROM igdb_store_matches sm
                    JOIN igdb_games g ON sm.igdb_id = g.igdb_id
                    WHERE sm.igdb_id IS NOT NULL
                """)).fetchall()

                # Build igdb_id → meta dict and igdb_id → store entries map
                igdb_meta: Dict[int, Dict[str, Any]] = {}
                igdb_to_stores: Dict[int, List[tuple]] = {}

                for row in rows:
                    store_name, app_id = row[0], row[1]
                    if store_name not in store_app_sets:
                        continue
                    if app_id not in store_app_sets[store_name]:
                        continue

                    igdb_id = row[2]
                    igdb_to_stores.setdefault(igdb_id, []).append(
                        (store_name, app_id)
                    )

                    if igdb_id in igdb_meta:
                        continue

                    release_date = ""
                    if row[7]:
                        try:
                            dt = utc_from_timestamp(row[7])
                            release_date = dt.strftime("%Y-%m-%d")
                        except (ValueError, OSError):
                            pass

                    category_map = {
                        0: "main_game", 1: "dlc_addon", 2: "expansion",
                        3: "bundle", 4: "standalone_expansion", 5: "mod",
                        6: "episode", 7: "season", 8: "remake", 9: "remaster",
                        10: "expanded_game", 11: "port", 12: "fork",
                        13: "pack", 14: "update",
                    }
                    status_map = {
                        0: "released", 2: "alpha", 3: "beta",
                        4: "early_access", 5: "offline", 6: "cancelled",
                        7: "rumored", 8: "delisted",
                    }

                    igdb_meta[igdb_id] = {
                        "title": row[3] or "",
                        "short_description": (row[4][:500] if row[4] else ""),
                        "description": row[4] or "",
                        "cover": row[5] or "",
                        "header_url": row[5] or "",
                        "hero": row[6] or "",
                        "release_date": release_date,
                        "rating": row[8],
                        "user_rating": row[9],
                        "total_rating": row[10],
                        "category": category_map.get(row[11], ""),
                        "status": status_map.get(row[12], ""),
                        "slug": row[13] or "",
                        "igdb_id": igdb_id,
                        "igdb_url": row[14] or "",
                    }

                if not igdb_to_stores:
                    return result

                # 2) Bulk many-to-many: genres
                genre_rows = session.execute(text("""
                    SELECT gg.game_id, g.name
                    FROM game_genres gg
                    JOIN igdb_genres g ON gg.genre_id = g.id
                """)).fetchall()
                genres_by_id: Dict[int, List[str]] = {}
                for gid, name in genre_rows:
                    if gid in igdb_to_stores:
                        genres_by_id.setdefault(gid, []).append(name)

                # 3) Bulk many-to-many: themes
                theme_rows = session.execute(text("""
                    SELECT gt.game_id, t.name
                    FROM game_themes gt
                    JOIN igdb_themes t ON gt.theme_id = t.id
                """)).fetchall()
                themes_by_id: Dict[int, List[str]] = {}
                for gid, name in theme_rows:
                    if gid in igdb_to_stores:
                        themes_by_id.setdefault(gid, []).append(name)

                # 4) Bulk many-to-many: franchises
                fran_rows = session.execute(text("""
                    SELECT gf.game_id, f.name
                    FROM game_franchises gf
                    JOIN igdb_franchises f ON gf.franchise_id = f.id
                """)).fetchall()
                franchises_by_id: Dict[int, List[str]] = {}
                for gid, name in fran_rows:
                    if gid in igdb_to_stores:
                        franchises_by_id.setdefault(gid, []).append(name)

                # 5) Bulk: developers and publishers
                company_rows = session.execute(text("""
                    SELECT ic.game_id, c.name, ic.developer, ic.publisher
                    FROM igdb_involved_companies ic
                    JOIN igdb_companies c ON ic.company_id = c.id
                """)).fetchall()
                devs_by_id: Dict[int, List[str]] = {}
                pubs_by_id: Dict[int, List[str]] = {}
                for gid, name, is_dev, is_pub in company_rows:
                    if gid not in igdb_to_stores:
                        continue
                    if is_dev and name:
                        devs_by_id.setdefault(gid, []).append(name)
                    if is_pub and name:
                        pubs_by_id.setdefault(gid, []).append(name)

                # 6) Bulk: screenshots
                ss_rows = session.execute(text("""
                    SELECT game_id, url FROM igdb_screenshots
                    WHERE url IS NOT NULL
                """)).fetchall()
                screenshots_by_id: Dict[int, List[str]] = {}
                for gid, url in ss_rows:
                    if gid in igdb_to_stores:
                        screenshots_by_id.setdefault(gid, []).append(url)

                # Merge relationship data into metadata dicts
                for igdb_id, meta in igdb_meta.items():
                    if igdb_id in genres_by_id:
                        meta["genres"] = genres_by_id[igdb_id]
                    if igdb_id in themes_by_id:
                        meta["themes"] = themes_by_id[igdb_id]
                    if igdb_id in franchises_by_id:
                        meta["franchise"] = franchises_by_id[igdb_id]
                    if igdb_id in devs_by_id:
                        meta["developers"] = devs_by_id[igdb_id]
                    if igdb_id in pubs_by_id:
                        meta["publishers"] = pubs_by_id[igdb_id]
                    if igdb_id in screenshots_by_id:
                        meta["screenshots"] = screenshots_by_id[igdb_id]

                # Map metadata to store entries
                for igdb_id, store_entries in igdb_to_stores.items():
                    meta = igdb_meta.get(igdb_id)
                    if not meta:
                        continue
                    for store_name, app_id in store_entries:
                        result.setdefault(store_name, {})[app_id] = meta

                total = sum(len(v) for v in result.values())
                logger.info(
                    f"IGDB cache bulk: {total} store entries from "
                    f"{len(igdb_meta)} IGDB games"
                )

        except Exception as e:
            logger.warning(f"Failed to get bulk cache metadata: {e}")

        return result

    # =========================================================================
    # SYNC OPERATIONS
    # =========================================================================

    def sync_failed_matches(
        self,
        progress_callback: Callable[[str, int, int, int], None] = None,
        title_lookup: Callable[[str, str], Optional[str]] = None
    ) -> Dict[str, int]:
        """Retry all failed store matches

        Re-attempts lookup for all entries with match_method='no_match'.
        Tries store ID lookup first, then falls back to title search.

        Args:
            progress_callback: Optional callback (message, current, total, success_count)
            title_lookup: Optional callback to get title for a game
                          Signature: (store_name, store_app_id) -> normalized_title or None

        Returns:
            Dict with counts: {"total": N, "success": N, "failed": N}
        """
        db = self._get_db()
        api = self._get_api()

        with db.get_session() as session:
            # Get all failed matches
            failed = (
                session.query(IgdbStoreMatch)
                .filter(IgdbStoreMatch.match_method == "no_match")
                .all()
            )

            total = len(failed)
            success = 0
            still_failed = 0

            logger.info(f"IGDB sync: Retrying {total} failed matches...")

            for i, match in enumerate(failed):
                # Look up title once — used for display and search fallback
                title = None
                if title_lookup:
                    title = title_lookup(match.store_name, match.store_app_id)
                display = title or f"{match.store_name}:{match.store_app_id}"

                if progress_callback:
                    progress_callback(
                        _("Retrying: {}").format(display),
                        i + 1,
                        total,
                        success  # Pass current success count
                    )

                # Try external_games lookup first (skip Epic - UUIDs don't work)
                igdb_id = None
                match_method = "external_games"
                if match.store_name != "epic":
                    igdb_id = api.lookup_by_store_id(
                        match.store_name, match.store_app_id, None
                    )

                # Fallback to title search if ID lookup failed
                if igdb_id is None:

                    # Fallback to cached normalized_title if callback unavailable
                    if not title:
                        title = match.normalized_title

                    if title:
                        logger.debug(f"IGDB sync: Trying title search for '{title}'")
                        igdb_id = api.search_game_by_title(title, None)
                        if igdb_id:
                            match_method = "title_search"
                            logger.debug(f"IGDB sync: Title search found IGDB ID {igdb_id}")
                        else:
                            logger.debug(f"IGDB sync: Title search found no match for '{title}'")

                if igdb_id:
                    db.save_store_match(
                        store_name=match.store_name,
                        store_app_id=match.store_app_id,
                        igdb_id=igdb_id,
                        match_method=match_method,
                        confidence=1.0 if match_method == "external_games" else 0.9,
                        normalized_title=match.normalized_title
                    )
                    success += 1
                    logger.debug(
                        "IGDB sync: Found match for "
                        "%s:%s via %s",
                        match.store_name,
                        match.store_app_id,
                        match_method,
                    )
                else:
                    # Update timestamp to reset TTL, store title if we got one
                    db.save_store_match(
                        store_name=match.store_name,
                        store_app_id=match.store_app_id,
                        igdb_id=None,
                        match_method="no_match",
                        confidence=0.0,
                        normalized_title=match.normalized_title
                    )
                    still_failed += 1

        logger.info(f"IGDB sync complete: {success} new matches, {still_failed} still failed")
        return {"total": total, "success": success, "failed": still_failed}

    def get_sync_stats(self) -> Dict[str, int]:
        """Get statistics about store matches

        Returns:
            Dict with counts: {"total": N, "matched": N, "failed": N}
        """
        db = self._get_db()

        with db.get_session() as session:
            total = session.query(IgdbStoreMatch).count()
            matched = (
                session.query(IgdbStoreMatch)
                .filter(IgdbStoreMatch.igdb_id.isnot(None))
                .count()
            )
            failed = (
                session.query(IgdbStoreMatch)
                .filter(IgdbStoreMatch.match_method == "no_match")
                .count()
            )

        return {"total": total, "matched": matched, "failed": failed}

    # LIFECYCLE
    # =========================================================================

    def on_enable(self) -> None:
        """Initialize database on enable"""
        self._get_db()
        logger.info("IGDB provider enabled")

    def on_disable(self) -> None:
        """Cleanup on disable"""
        if self._db:
            self._db.close()
            self._db = None
        logger.info("IGDB provider disabled")

    def close(self) -> None:
        """Cleanup on shutdown"""
        if self._api:
            self._api.close()
            self._api = None
        if self._db:
            self._db.close()
            self._db = None
