# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# manager.py

"""
Main manager for Steam Scraper module.
Orchestrates database, API, and scraping operations.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, Union
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from luducat.plugins.sdk.datetime import utc_now
from luducat.plugins.sdk.json import json
from .database import Database, Game, Image
from .steam_api import SteamAPIClient
from .steam_scraper import SteamScraper
from .exceptions import AppNotFoundError, RateLimitExceededError, SteamScraperException

logger = logging.getLogger(__name__)


class SteamGameManager:
    """Main manager for Steam game data operations."""
    
    def __init__(self, db_path: str = None, cache_dir: str = None, api_key: str = None, http_client=None):
        """Initialize the Steam game manager.

        Args:
            db_path: Path to SQLite database (optional)
            cache_dir: Path to image cache directory (optional)
            api_key: Steam API key (optional)
            http_client: PluginHttpClient for all HTTP requests
        """
        self.database = Database(db_path)
        self.api_client = SteamAPIClient(api_key, http_client=http_client)
        self.scraper = SteamScraper(cache_dir, http_client=http_client)

    @staticmethod
    def _strip_query_params(url: str) -> str:
        """Strip query parameters from URL for consistent storage.

        Steam URLs often have timestamp params like ?t=1234567890 that change
        but point to the same image. We strip them for cache consistency.
        """
        if not url:
            return url
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def get_game(self, appid: int = None, name: str = None) -> Game:
        """Get game by appid or name, fetching from Steam if needed.
        
        This is the main query interface. It will:
        1. Check if game exists in database
        2. If missing or incomplete, scrape from Steam
        3. Return the game object
        
        Args:
            appid: Steam application ID
            name: Game name (will search for appid first)
            
        Returns:
            Game object
            
        Raises:
            AppNotFoundError: If game doesn't exist on Steam
            ValueError: If neither appid nor name provided
        """
        if not appid and not name:
            raise ValueError("Must provide either appid or name")
        
        # If name provided, search for appid
        if name and not appid:
            appid = self._find_appid_by_name(name)
            if not appid:
                raise AppNotFoundError(f"Game '{name}' not found")
        
        # Check database first
        session = self.database.get_session()
        try:
            from sqlalchemy.orm import joinedload
            
            # Eager load images to avoid lazy load issues after detaching
            game = session.query(Game).options(joinedload(Game.images)).filter_by(appid=appid).first()

            # If game doesn't exist or is incomplete, fetch from Steam
            if not game or not game.is_complete:
                logger.info(f"Game {appid} missing or incomplete, fetching from Steam...")
                # _fetch_and_store_game creates its own session and returns detached object
                session.close()
                return self._fetch_and_store_game(appid, existing_game=game)
            
            # Force load the images relationship before detaching
            _ = game.images  # This triggers the load while still in session
            
            # Detach from session before returning
            session.expunge(game)
            return game
            
        finally:
            session.close()
    
    def _find_appid_by_name(self, name: str) -> Optional[int]:
        """Find appid by game name.
        
        First checks database, then queries Steam API.
        
        Args:
            name: Game name
            
        Returns:
            App ID if found, None otherwise
        """
        # Check database first
        session = self.database.get_session()
        try:
            game = session.query(Game).filter(Game.name.ilike(f"%{name}%")).first()
            if game:
                return game.appid
        finally:
            session.close()
        
        # Search Steam API
        return self.api_client.search_app_by_name(name)
    
    def _fetch_and_store_game(
        self, appid: int, existing_game: Game = None, download_images: bool = True
    ) -> Game:
        """Fetch game data from Steam and store in database.

        Args:
            appid: Steam application ID
            existing_game: Existing game object (if updating) - can be detached
            download_images: If True, download images to cache (default True).
                            If False, only fetches metadata (faster for bulk sync).

        Returns:
            Updated Game object

        Raises:
            AppNotFoundError: If game not found on Steam
        """
        session = self.database.get_session()

        try:
            # Fetch from Steam API
            api_data = self.api_client.get_app_details(appid)

            # If existing_game provided, query it fresh in this session
            # (Don't use the passed object directly as it may be detached)
            if existing_game:
                game = session.query(Game).filter_by(appid=appid).first()
                if game:
                    self._update_game_from_api(game, api_data)
                else:
                    # Shouldn't happen, but create new if not found
                    game = self._create_game_from_api(appid, api_data)
                    session.add(game)
            else:
                game = self._create_game_from_api(appid, api_data)
                session.add(game)

            # Update timestamp
            game.last_updated = utc_now()

            session.commit()

            # Store screenshot URLs (only download if download_images=True)
            screenshot_urls = api_data.get('screenshots', [])
            if screenshot_urls:
                self._scrape_and_store_screenshots(
                    appid, screenshot_urls, session, download_images=download_images
                )

            # Always probe library asset URLs (IStoreBrowseService — URL lookup, not file downloads)
            self.probe_and_store_library_assets(appid, game=game, session=session)

            # Get SteamSpy data for review counts and user scores
            steamspy_data = self.api_client.get_steamspy_data(appid)
            if steamspy_data:
                positive = steamspy_data.get('positive', 0)
                negative = steamspy_data.get('negative', 0)
                
                # If SteamSpy has fewer reviews than threshold, use store page scraping as fallback
                from .config import STEAMSPY_MIN_REVIEWS
                if positive + negative < STEAMSPY_MIN_REVIEWS:
                    logger.info(
                        f"SteamSpy has {positive + negative} reviews for {appid} "
                        f"(< {STEAMSPY_MIN_REVIEWS} threshold), using store page fallback"
                    )
                    store_reviews = self.scraper.scrape_review_counts(appid)
                    if store_reviews:
                        positive = store_reviews.get('positive', 0)
                        negative = store_reviews.get('negative', 0)
                
                # Update with SteamSpy/store data
                game.positive = positive
                game.negative = negative
                game.user_score = float(steamspy_data.get('userscore', 0))
                game.score_rank = steamspy_data.get('score_rank', '')
                
                # Also get ownership and playtime stats
                game.estimated_owners = steamspy_data.get('owners', '')
                game.average_playtime_forever = steamspy_data.get('average_forever', 0)
                game.average_playtime_2weeks = steamspy_data.get('average_2weeks', 0)
                game.median_playtime_forever = steamspy_data.get('median_forever', 0)
                game.median_playtime_2weeks = steamspy_data.get('median_2weeks', 0)
                game.peak_ccu = steamspy_data.get('ccu', 0)
            
            # Commit library assets and SteamSpy data to database
            session.commit()

            # Download additional images (header, background, logo) if requested
            if download_images:
                self._download_additional_images(appid, game)
            
            session.refresh(game)
            
            # Force load the images relationship before detaching
            _ = game.images  # This triggers the load while still in session
            
            session.expunge(game)
            return game
            
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _create_game_from_api(self, appid: int, data: Dict[str, Any]) -> Game:
        """Create Game object from Steam API data.
        
        Args:
            appid: Steam application ID
            data: API response data
            
        Returns:
            Game object
        """
        # Extract platforms
        platforms = data.get('platforms', {})
        
        # Extract price - use initial (full/undiscounted) price
        price_data = data.get('price_overview', {})
        if price_data:
            # 'initial' is the undiscounted price, 'final' is after discount
            price = price_data.get('initial', 0) / 100.0
            if price == 0:
                # If initial is 0, might be permanently free or no discount
                price = price_data.get('final', 0) / 100.0
        else:
            price = 0.0
        
        # Extract metacritic
        metacritic = data.get('metacritic', {})
        
        # Extract main_genre from common_primary_genre field in Steam API
        # Store main_genre separately AND genres list
        main_genre = None
        genres_list = [genre.get('description') for genre in data.get('genres', [])]
        
        # Check for common_primary_genre first (this is the actual field name)
        if data.get('common_primary_genre'):
            main_genre = data['common_primary_genre'].get('description')
        elif genres_list:
            # Fallback: use first genre if common_primary_genre not available
            main_genre = genres_list[0]
        
        # Check if achievements available
        achievements_data = data.get('achievements', {})
        has_achievements = bool(achievements_data.get('total', 0) > 0)
        
        # Parse supported languages properly (not just "[*]")
        supported_langs = self._parse_languages(data.get('supported_languages', ''))
        
        game = Game(
            appid=appid,
            name=data.get('name'),
            type=data.get('type'),
            release_date=data.get('release_date', {}).get('date'),
            required_age=data.get('required_age', 0),
            price=price,
            is_free=data.get('is_free', False),
            dlc_count=len(data.get('dlc', [])),
            controller_support=data.get('controller_support'),
            achievements_available=has_achievements,
            detailed_description=data.get('detailed_description'),
            about_the_game=data.get('about_the_game'),
            short_description=data.get('short_description'),
            reviews=data.get('reviews'),
            header_image=self._strip_query_params(data.get('header_image')),
            capsule_image=self._strip_query_params(data.get('capsule_image')),
            background_image=self._strip_query_params(data.get('background')),
            logo_url=self._strip_query_params(data.get('logo')),
            website=data.get('website'),
            support_url=data.get('support_info', {}).get('url'),
            support_email=data.get('support_info', {}).get('email'),
            windows=platforms.get('windows', False),
            mac=platforms.get('mac', False),
            linux=platforms.get('linux', False),
            metacritic_score=metacritic.get('score', 0),
            metacritic_url=metacritic.get('url'),
            achievements=achievements_data.get('total', 0),
            recommendations=data.get('recommendations', {}).get('total', 0),
            supported_languages=supported_langs,
            developers=data.get('developers', []),
            publishers=data.get('publishers', []),
            categories=[cat.get('description') for cat in data.get('categories', [])],
            genres=genres_list,  # Store full genres list
            main_genre=main_genre,  # Store main_genre separately
            packages=data.get('packages', []),
            ext_user_account_notice=data.get('ext_user_account_notice'),
            content_descriptors=data.get('content_descriptors', {}).get('ids', []),
            package_groups=data.get('package_groups', []),
            last_updated=utc_now()
        )
        
        return game
    
    def _update_game_from_api(self, game: Game, data: Dict[str, Any]):
        """Update existing Game object with API data.
        
        Preserves existing data when new data is incomplete (delisted games).
        
        Args:
            game: Existing Game object
            data: API response data
        """
        # Extract platforms
        platforms = data.get('platforms', {})
        
        # Extract price - use initial (undiscounted) price
        price_data = data.get('price_overview', {})
        if price_data:
            price = price_data.get('initial', 0) / 100.0
            if price == 0:
                # Fallback if initial is 0
                price = price_data.get('final', 0) / 100.0
        else:
            price = None
        
        # Extract metacritic
        metacritic = data.get('metacritic', {})
        
        # Update fields, preserving existing if new is empty
        game.name = data.get('name') or game.name
        game.type = data.get('type') or game.type  # FIX: Add type field
        game.release_date = data.get('release_date', {}).get('date') or game.release_date
        game.required_age = data.get('required_age', game.required_age)
        
        if price is not None:
            game.price = price
        
        game.dlc_count = len(data.get('dlc', []))
        
        # Preserve descriptions if delisted
        game.detailed_description = data.get('detailed_description') or game.detailed_description
        game.about_the_game = data.get('about_the_game') or game.about_the_game
        game.short_description = data.get('short_description') or game.short_description
        
        game.reviews = data.get('reviews') or game.reviews
        game.header_image = self._strip_query_params(data.get('header_image')) or game.header_image
        game.capsule_image = self._strip_query_params(data.get('capsule_image')) or game.capsule_image
        game.background_image = self._strip_query_params(data.get('background')) or game.background_image
        game.logo_url = self._strip_query_params(data.get('logo')) or game.logo_url
        game.website = data.get('website') or game.website
        
        support_info = data.get('support_info', {})
        game.support_url = support_info.get('url') or game.support_url
        game.support_email = support_info.get('email') or game.support_email
        
        game.windows = platforms.get('windows', game.windows)
        game.mac = platforms.get('mac', game.mac)
        game.linux = platforms.get('linux', game.linux)
        
        game.metacritic_score = metacritic.get('score') or game.metacritic_score
        game.metacritic_url = metacritic.get('url') or game.metacritic_url
        
        achievements_total = data.get('achievements', {}).get('total')
        if achievements_total:
            game.achievements = achievements_total
        
        recommendations_total = data.get('recommendations', {}).get('total')
        if recommendations_total:
            game.recommendations = recommendations_total
        
        languages = self._parse_languages(data.get('supported_languages', ''))
        if languages:
            game.supported_languages = languages
        
        developers = data.get('developers', [])
        if developers:
            game.developers = developers
        
        publishers = data.get('publishers', [])
        if publishers:
            game.publishers = publishers
        
        categories = [cat.get('description') for cat in data.get('categories', [])]
        if categories:
            game.categories = categories
        
        genres = [genre.get('description') for genre in data.get('genres', [])]
        if genres:
            game.genres = genres
        
        packages = data.get('packages', [])
        if packages:
            game.packages = packages

        # Content descriptors (nudity/violence/mature markers)
        content_desc = data.get('content_descriptors', {})
        if isinstance(content_desc, dict):
            ids = content_desc.get('ids', [])
            if ids:
                game.content_descriptors = ids
        elif isinstance(content_desc, list) and content_desc:
            game.content_descriptors = content_desc

        game.last_updated = utc_now()
    
    def _parse_languages(self, languages_str: str) -> list:
        """Parse supported languages string into list.
        
        Steam API format: 
        "English<strong>*</strong>, French<strong>*</strong>, German"
        
        Where <strong>*</strong> indicates full audio support.
        We extract the language names (English, French, German), ignoring the asterisks.
        
        Args:
            languages_str: HTML string of supported languages from Steam API
            
        Returns:
            List of language names (e.g., ['English', 'French', 'German'])
        """
        if not languages_str:
            return []
        
        # Remove HTML tags and footnotes
        import re
        
        # Remove <strong>*</strong> markers (full audio indicators)
        cleaned = re.sub(r'<strong>\*</strong>', '', languages_str)
        
        # Remove <br> and any text after it (footnote text)
        cleaned = re.sub(r'<br[^>]*>.*$', '', cleaned)
        
        # Remove any remaining HTML tags
        cleaned = re.sub(r'<[^>]+>', '', cleaned)
        
        # Split by comma and clean up
        languages = []
        for lang in cleaned.split(','):
            lang = lang.strip()
            if lang and lang != '*':  # Filter out empty and asterisks
                languages.append(lang)
        
        return languages
    
    def _scrape_and_store_screenshots(
        self, appid: int, screenshots: list, session, download_images: bool = True
    ):
        """Store screenshot URLs in game.screenshots JSON field.

        Args:
            appid: Steam application ID
            screenshots: List of screenshot dictionaries from API
            session: Database session
            download_images: Ignored - we never download during sync.
                            Assets are lazy-loaded on demand.
        """
        # Extract URLs from API screenshot data, strip query params
        screenshot_urls = [
            self._strip_query_params(s.get('path_full'))
            for s in screenshots
            if s.get('path_full')
        ]

        if not screenshot_urls:
            return

        # Get the game and update screenshots field
        game = session.query(Game).filter_by(appid=appid).first()
        if game:
            game.screenshots = screenshot_urls
            session.commit()
            logger.info(f"Stored {len(screenshot_urls)} screenshot URLs for app {appid}")

        # Clean up old Image table entries (deprecated)
        session.query(Image).filter_by(appid=appid).delete()
        session.commit()
    
    def _download_additional_images(self, appid: int, game: Game):
        """Download additional image types (header, background, logo, library assets).
        
        Checks for existing files and only downloads missing ones.
        
        Args:
            appid: Steam application ID
            game: Game object with image URLs
        """
        import os
        
        cache_dir = os.path.join(self.scraper.cache_dir, str(appid))
        os.makedirs(cache_dir, exist_ok=True)
        
        images_to_download = []
        
        # Header image (capsule)
        if game.header_image:
            images_to_download.append(('header.jpg', game.header_image))
        
        # Capsule image (alternative)
        if game.capsule_image:
            images_to_download.append(('capsule.jpg', game.capsule_image))
        
        # Background/hero image
        if game.background_image:
            images_to_download.append(('background.jpg', game.background_image))
        
        # Logo (old API version)
        if game.logo_url:
            # Determine extension from URL
            ext = '.png' if '.png' in game.logo_url.lower() else '.jpg'
            images_to_download.append((f'logo{ext}', game.logo_url))
        
        # Library assets (new in v1.0.6)
        if game.library_capsule:
            images_to_download.append(('library_600x900.jpg', game.library_capsule))
        
        if game.library_capsule_2x:
            images_to_download.append(('library_600x900_2x.jpg', game.library_capsule_2x))
        
        if game.library_hero:
            images_to_download.append(('library_hero.jpg', game.library_hero))
        
        if game.library_logo:
            # Library logo is typically PNG
            ext = '.png' if '.png' in game.library_logo.lower() else '.jpg'
            images_to_download.append((f'library_logo{ext}', game.library_logo))
        
        if game.main_capsule:
            images_to_download.append(('capsule_616x353.jpg', game.main_capsule))
        
        if game.small_capsule:
            images_to_download.append(('capsule_231x87.jpg', game.small_capsule))
        
        if game.community_icon:
            images_to_download.append(('community_icon.jpg', game.community_icon))
        
        # Download only missing images
        downloaded_count = 0
        skipped_count = 0
        
        for filename, url in images_to_download:
            filepath = os.path.join(cache_dir, filename)
            
            # Check if file already exists
            if os.path.exists(filepath):
                logger.debug(f"Skipping {filename} (already exists)")
                skipped_count += 1
                continue
            
            # Download missing file
            if self.scraper.download_image(url, filepath):
                downloaded_count += 1
        
        logger.info(
            f"Downloaded {downloaded_count} images for app {appid} "
            f"({skipped_count} already existed)"
        )

    
    def probe_and_store_library_assets(self, appid: int, game: Game = None, session=None) -> dict:
        """Fetch and store library asset URLs using IStoreBrowseService/GetItems API.

        Uses actual asset metadata from Steam rather than guessing patterns.
        Handles old formats (portrait.png), modern formats (library_600x900.jpg),
        and hash-based paths.

        Args:
            appid: Steam application ID
            game: Optional Game object (for in-memory updates, not persisted)
            session: Optional SQLAlchemy session for database persistence

        Returns:
            Dict of {field_name: url} for found assets
        """
        found_assets = {}

        # Get actual asset metadata from Steam API
        assets = self.api_client.get_store_assets(appid)

        if not assets:
            logger.warning(f"No assets metadata available for app {appid}, falling back to pattern guessing")
            found_assets = self._probe_library_assets_by_pattern(appid)
        else:
            # Get asset_url_format and CDN base
            asset_url_format = assets.get('asset_url_format', '')

            # Modern CDN bases (in priority order)
            cdn_bases = [
                "https://shared.steamstatic.com/store_item_assets",
                "https://shared.akamai.steamstatic.com/store_item_assets",
                "https://cdn.cloudflare.steamstatic.com"
            ]

            # Map API asset names to database fields
            asset_mapping = {
                'library_capsule': 'library_capsule',
                'library_capsule_2x': 'library_capsule_2x',
                'library_hero': 'library_hero',
                'library_logo': 'library_logo',
                'main_capsule': 'main_capsule',
                'small_capsule': 'small_capsule',
            }

            missing_from_api = []

            # Build probe tasks for assets that have filenames and a URL format
            probe_tasks = []  # (api_field, db_field, url_path)
            for api_field, db_field in asset_mapping.items():
                filename = assets.get(api_field)
                if not filename:
                    missing_from_api.append(db_field)
                    logger.debug(f"✗ No {api_field} in API response for app {appid}")
                    continue
                if asset_url_format:
                    url_path = asset_url_format.replace('${FILENAME}', filename)
                    probe_tasks.append((api_field, db_field, url_path))
                else:
                    missing_from_api.append(db_field)

            # Probe all assets in parallel across CDN bases
            if probe_tasks:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {
                        executor.submit(
                            self._probe_single_asset, cdn_bases, url_path,
                        ): (api_field, db_field)
                        for api_field, db_field, url_path in probe_tasks
                    }
                    for future in as_completed(futures):
                        api_field, db_field = futures[future]
                        url = future.result()
                        if url:
                            found_assets[db_field] = url
                            logger.debug(f"✓ Found {api_field} for app {appid}")
                        else:
                            missing_from_api.append(db_field)
                            logger.debug(f"✗ {api_field} probe failed for app {appid}")

            # Fallback: Probe known URL patterns for missing assets
            if missing_from_api:
                logger.info(f"Probing fallback URLs for {len(missing_from_api)} missing assets")
                fallback_assets = self._probe_assets_by_pattern(appid, missing_from_api)
                found_assets.update(fallback_assets)

        valid_count = len(found_assets)
        logger.info(f"Probed library assets for app {appid}: {valid_count} valid")

        # Update game object in memory (for compatibility)
        if game is not None:
            for field, url in found_assets.items():
                setattr(game, field, url)

        # Persist to database using raw SQL UPDATE
        if session is not None and found_assets:
            from sqlalchemy import update
            stmt = update(Game).where(Game.appid == appid).values(**found_assets)
            session.execute(stmt)
            logger.debug(f"Updated database for app {appid} with {len(found_assets)} assets")

        return found_assets

    def _probe_single_asset(self, cdn_bases: list, url_path: str) -> str | None:
        """Probe one asset across CDN bases in order.

        Returns the first valid URL or None.
        """
        for cdn_base in cdn_bases:
            full_url = f"{cdn_base}/{url_path}"
            if self.scraper.probe_asset_url(full_url):
                return self._strip_query_params(full_url)
        return None

    def _probe_assets_by_pattern(self, appid: int, fields: list) -> dict:
        """Probe Fastly fallback URLs for missing asset fields.

        Only probes library_logo — other assets almost never exist on
        Fastly when missing from the primary API response.

        Args:
            appid: Steam application ID
            fields: List of database field names to probe

        Returns:
            Dict of {field_name: url} for found assets
        """
        # Only logo has meaningful hit rate on Fastly fallback
        fallback_patterns = {
            'library_logo': f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/logo.png",
        }

        found = {}
        for field in fields:
            if field not in fallback_patterns:
                continue

            url = fallback_patterns[field]
            if self.scraper.probe_asset_url(url):
                found[field] = url
                logger.debug(f"✓ Found {field} via fallback for app {appid}")
            else:
                logger.debug(f"✗ Fallback probe failed for {field} on app {appid}")

        return found

    def _probe_library_assets_by_pattern(self, appid: int) -> dict:
        """Probe all library assets using known URL patterns.

        Args:
            appid: Steam application ID

        Returns:
            Dict of {field_name: url} for found assets
        """
        all_fields = ['library_capsule', 'library_capsule_2x', 'library_hero',
                      'library_logo', 'main_capsule', 'small_capsule']
        return self._probe_assets_by_pattern(appid, all_fields)
    
    def import_steam_userdata(self, json_path: str, fetch_missing: bool = True) -> Dict[str, Any]:
        """Import owned apps from Steam dynamicstore/userdata JSON.
        
        This endpoint includes ALL owned apps (including DLC and profile-limited games)
        which GetOwnedGames API doesn't provide.
        
        Download userdata JSON from: https://store.steampowered.com/dynamicstore/userdata/
        (requires being logged into Steam in browser)
        
        Args:
            json_path: Path to dynamicstore userdata JSON file
            fetch_missing: If True, fetch missing games from Steam API
            
        Returns:
            Dictionary with import statistics:
            {
                'total_owned': int,           # Total appids in userdata
                'already_in_db': int,         # Already in database
                'fetched': int,               # Newly fetched from Steam
                'failed': int,                # Failed to fetch
                'wishlist_count': int,        # Number of wishlisted apps
                'ignored_count': int          # Number of ignored apps
            }
        """
        
        logger.info(f"Importing Steam userdata from {json_path}")
        
        # Read JSON file
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                userdata = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read userdata JSON: {e}")
            raise
        
        # Extract owned appids
        owned_appids = userdata.get('rgOwnedApps', [])
        wishlist = userdata.get('rgWishlist', [])
        ignored_apps = userdata.get('rgIgnoredApps', [])
        
        logger.info(f"Found {len(owned_appids)} owned apps in userdata")
        logger.info(f"Found {len(wishlist)} wishlisted apps")
        logger.info(f"Found {len(ignored_apps)} ignored apps")
        
        if not owned_appids:
            logger.warning("No owned apps found in userdata JSON")
            return {
                'total_owned': 0,
                'already_in_db': 0,
                'fetched': 0,
                'failed': 0,
                'wishlist_count': len(wishlist),
                'ignored_count': len(ignored_apps)
            }
        
        # Check which apps are already in database
        session = self.database.get_session()
        try:
            existing_games = session.query(Game).filter(
                Game.appid.in_(owned_appids)
            ).all()
            
            existing_appids = {game.appid for game in existing_games}
            missing_appids = [aid for aid in owned_appids if aid not in existing_appids]
            
            logger.info(f"{len(existing_appids)} apps already in database")
            logger.info(f"{len(missing_appids)} apps need to be fetched")
            
        finally:
            session.close()
        
        # Fetch missing apps if requested
        fetched_count = 0
        failed_count = 0
        
        if fetch_missing and missing_appids:
            logger.info(f"Fetching {len(missing_appids)} missing apps from Steam...")
            
            for i, appid in enumerate(missing_appids, 1):
                try:
                    logger.info(f"Fetching {i}/{len(missing_appids)}: appid {appid}")
                    self._fetch_and_store_game(appid)
                    fetched_count += 1
                    
                except Exception as e:
                    logger.error(f"Failed to fetch appid {appid}: {e}")
                    failed_count += 1
                    
                # Progress update every 50 games
                if i % 50 == 0:
                    logger.info(f"Progress: {i}/{len(missing_appids)} - {fetched_count} fetched, {failed_count} failed")
        
        results = {
            'total_owned': len(owned_appids),
            'already_in_db': len(existing_appids),
            'fetched': fetched_count,
            'failed': failed_count,
            'wishlist_count': len(wishlist),
            'ignored_count': len(ignored_apps)
        }
        
        logger.info(f"Import complete: {results}")
        return results
    
    def close(self):
        """Close all connections."""
        self.api_client.close()
        self.scraper.close()
        self.database.close()
    
    def get_games_bulk(
        self, appids: list[int], download_images: bool = True
    ) -> dict[int, Game]:
        """Get multiple games efficiently in bulk.

        Returns existing games from database immediately. For missing games,
        fetches from Steam API.

        Args:
            appids: List of Steam application IDs
            download_images: If True, download images for new games (default True)

        Returns:
            Dictionary mapping appid to Game object
            {123456: Game(...), 789012: Game(...), ...}

        Example:
            owned_games = [440, 570, 730]
            games = manager.get_games_bulk(owned_games, download_images=False)

            for appid, game in games.items():
                print(f"{game.name}: {game.developers}")
        """
        session = self.database.get_session()
        
        try:
            # Bulk query for existing games (eager load images)
            # Note: Use selectinload instead of joinedload to avoid duplicate parent objects
            from sqlalchemy.orm import selectinload
            existing_games = session.query(Game).options(
                selectinload(Game.images)
            ).filter(
                Game.appid.in_(appids)
            ).all()
            
            # Force load images relationship while in session
            for game in existing_games:
                _ = game.images
            
            # Build result dictionary
            result = {}
            for game in existing_games:
                session.expunge(game)
                result[game.appid] = game
            
            # Find missing or incomplete appids
            existing_appids = set(result.keys())
            missing_appids = [aid for aid in appids if aid not in existing_appids]
            
            # Check for incomplete games
            incomplete_games = [
                game for game in existing_games 
                if not game.is_complete
            ]
            
            # Close session before fetching (new sessions created in fetch)
            session.close()
            
            # Fetch missing games
            for appid in missing_appids:
                try:
                    logger.info(f"Fetching missing game: {appid}")
                    game = self._fetch_and_store_game(
                        appid, download_images=download_images
                    )
                    result[appid] = game
                except RateLimitExceededError:
                    raise  # Must propagate for proper backoff
                except Exception as e:
                    logger.error(f"Failed to fetch game {appid}: {e}")
                    # Don't include in result if fetch fails

            # Update incomplete games
            for game in incomplete_games:
                try:
                    logger.info(f"Updating incomplete game: {game.appid}")
                    updated_game = self._fetch_and_store_game(
                        game.appid, existing_game=game, download_images=download_images
                    )
                    result[game.appid] = updated_game
                except RateLimitExceededError:
                    raise  # Must propagate for proper backoff
                except Exception as e:
                    logger.error(f"Failed to update game {game.appid}: {e}")
                    # Keep existing incomplete game in result
            
            logger.info(f"Bulk query complete: {len(result)}/{len(appids)} games returned")
            return result
            
        except Exception:
            session.rollback()
            raise
        finally:
            if session.is_active:
                session.close()

    def refresh_images(self, appid: int) -> Dict[str, Any]:
        """Refresh image cache for a specific game.
        
        Downloads images if missing, or replaces them if online versions are newer.
        
        Args:
            appid: Steam application ID
            
        Returns:
            Dictionary with refresh statistics
            
        Raises:
            AppNotFoundError: If game not found
        """
        import os

        session = self.database.get_session()

        try:
            # Get game from database
            game = session.query(Game).filter_by(appid=appid).first()
            if not game:
                raise AppNotFoundError(f"Game {appid} not found in database")
            
            # Fetch latest data from Steam API to get screenshot URLs
            try:
                api_data = self.api_client.get_app_details(appid)
                screenshot_urls = api_data.get('screenshots', [])
            except Exception as e:
                logger.warning(f"Failed to fetch screenshots from API for {appid}: {e}")
                screenshot_urls = []
            
            if not screenshot_urls:
                logger.info(f"No screenshots available for game {appid}")
                return {'appid': appid, 'downloaded': 0, 'updated': 0, 'skipped': 0}
            
            # Extract URLs from API screenshot data
            screenshot_urls_full = [s.get('path_full') for s in screenshot_urls if s.get('path_full')]
            
            stats = {'appid': appid, 'downloaded': 0, 'updated': 0, 'skipped': 0}
            
            # Get existing images from database
            existing_images = session.query(Image).filter_by(appid=appid).all()
            existing_images_dict = {img.image_order: img for img in existing_images}
            
            # Cache directory for this game
            cache_dir = os.path.join(self.scraper.cache_dir, str(appid))
            os.makedirs(cache_dir, exist_ok=True)
            
            # Process each screenshot
            for idx, url in enumerate(screenshot_urls_full, start=1):
                # Determine file extension
                ext = '.jpg'
                if '.png' in url.lower():
                    ext = '.png'
                elif '.gif' in url.lower():
                    ext = '.gif'
                
                filename = f"{appid}_{idx}{ext}"
                filepath = os.path.join(cache_dir, filename)
                
                should_download = False
                
                # Check if file exists locally
                if not os.path.exists(filepath):
                    should_download = True
                    action = 'downloaded'
                else:
                    # Check if we should update (compare last modified time)
                    # Get local file modification time
                    # Check if there's a newer version available
                    # We'll re-download if the image was last scraped more than 7 days ago
                    if idx in existing_images_dict:
                        scraped_date = existing_images_dict[idx].scraped_date
                        days_old = (utc_now() - scraped_date).days
                        
                        if days_old > 7:
                            should_download = True
                            action = 'updated'
                        else:
                            stats['skipped'] += 1
                            continue
                    else:
                        # File exists but not in database - re-download to be safe
                        should_download = True
                        action = 'updated'
                
                if should_download:
                    # Download the image
                    if self.scraper.download_image(url, filepath):
                        stats[action] += 1
                        
                        # Update or create database record
                        if idx in existing_images_dict:
                            # Update existing record
                            existing_images_dict[idx].scraped_date = utc_now()
                        else:
                            # Create new record
                            image = Image(
                                appid=appid,
                                filename=filename,
                                image_order=idx,
                                scraped_date=utc_now()
                            )
                            session.add(image)
            
            session.commit()
            
            # Also refresh additional images (header, background, logo)
            self._download_additional_images(appid, game)
            
            logger.info(f"Refreshed images for {appid}: {stats}")
            return stats
            
        finally:
            session.close()
    
    def refresh_all_images(self, limit: int = None) -> Dict[str, Any]:
        """Refresh image cache for all games in database.
        
        Args:
            limit: Optional limit on number of games to process
            
        Returns:
            Dictionary with overall statistics
        """
        session = self.database.get_session()
        
        try:
            # Get all games
            query = session.query(Game)
            if limit:
                query = query.limit(limit)
            
            games = query.all()
            
            total_stats = {
                'games_processed': 0,
                'games_failed': 0,
                'total_downloaded': 0,
                'total_updated': 0,
                'total_skipped': 0
            }
            
            for game in games:
                try:
                    logger.info(f"Refreshing images for {game.appid}: {game.name}")
                    stats = self.refresh_images(game.appid)
                    
                    total_stats['games_processed'] += 1
                    total_stats['total_downloaded'] += stats['downloaded']
                    total_stats['total_updated'] += stats['updated']
                    total_stats['total_skipped'] += stats['skipped']
                    
                except Exception as e:
                    logger.error(f"Failed to refresh images for {game.appid}: {e}")
                    total_stats['games_failed'] += 1
            
            return total_stats
            
        finally:
            session.close()
