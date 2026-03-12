# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# gog_api.py

"""GOG API Client

Handles authenticated and public API requests to GOG.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from luducat.plugins.sdk.network import RequestException
from luducat.plugins.sdk.constants import USER_AGENT

logger = logging.getLogger(__name__)

_GOG_TIMEOUT = 20
_GOG_PRODUCTS_TIMEOUT = 30

# Rate limiting — 5 requests/second sliding window
_GOG_RATE_LIMIT_REQUESTS = 5
_GOG_RATE_LIMIT_PERIOD = 1.0  # seconds

# GOG API endpoints
USER_DATA_URL = "https://www.gog.com/userData.json"
OWNED_GAMES_URL = "https://www.gog.com/user/data/games"
ACCOUNT_URL = "https://www.gog.com/account/getFilteredProducts"
PRODUCTS_API_URL = "https://api.gog.com/products"
_PRODUCTS_EXPAND = "downloads,expanded_dlcs,description,screenshots,videos,related_products,changelog"


class GogApiError(Exception):
    """Raised when GOG API request fails"""
    pass


class GogApiClient:
    """GOG API client using browser cookies

    Usage:
        client = GogApiClient(plugin_instance, http_client=plugin.http)

        # After getting cookies from browser:
        client.store_cookies(cookies_dict)

        # Fetch owned games (returns IDs + product metadata):
        owned_ids, products = await client.get_owned_games()
    """

    def __init__(self, plugin, http_client=None):
        """Initialize API client

        Args:
            plugin: GOG store plugin instance (for credential access)
            http_client: PluginHttpClient for all HTTP requests
        """
        self.plugin = plugin
        self._http = http_client
        self._request_times: List[float] = []

    def _get_cookies(self) -> Dict[str, str]:
        """Get stored cookies"""
        cookies = {}

        # Get the main auth cookie
        gog_al = self.plugin.get_credential("gog_al")
        if gog_al:
            cookies["gog-al"] = gog_al

        # Get session cookie if available
        gog_lc = self.plugin.get_credential("gog_lc")
        if gog_lc:
            cookies["gog_lc"] = gog_lc

        return cookies

    def store_cookies(self, cookies: Dict[str, str]) -> None:
        """Store authentication cookies

        Args:
            cookies: Dict of cookie name -> value
        """
        if "gog-al" in cookies:
            self.plugin.set_credential("gog_al", cookies["gog-al"])

        if "gog_lc" in cookies:
            self.plugin.set_credential("gog_lc", cookies["gog_lc"])

        # Store timestamp
        self.plugin.set_credential("cookies_stored", str(int(time.time())))

        logger.info("Stored GOG cookies")

    def clear_cookies(self) -> None:
        """Clear all stored cookies (logout)"""
        self.plugin.delete_credential("gog_al")
        self.plugin.delete_credential("gog_lc")
        self.plugin.delete_credential("cookies_stored")
        logger.info("Cleared GOG cookies")

    def has_cookies(self) -> bool:
        """Check if we have stored cookies"""
        return bool(self.plugin.get_credential("gog_al"))

    def _build_cookie_header(self) -> str:
        """Build Cookie header from stored cookies"""
        cookies = self._get_cookies()
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def _rate_limit(self) -> None:
        """Enforce rate limiting (5 requests/second sliding window)."""
        now = time.time()
        self._request_times = [
            t for t in self._request_times
            if now - t < _GOG_RATE_LIMIT_PERIOD
        ]
        if len(self._request_times) >= _GOG_RATE_LIMIT_REQUESTS:
            oldest = self._request_times[0]
            sleep_time = _GOG_RATE_LIMIT_PERIOD - (now - oldest)
            if sleep_time > 0:
                logger.debug(f"GOG rate limit — waiting {sleep_time:.2f}s")
                time.sleep(sleep_time)
        self._request_times.append(time.time())

    async def get_user_data(self) -> Optional[Dict[str, Any]]:
        """Get authenticated user's profile data

        Returns:
            User data dict or None if request fails
        """
        if not self.has_cookies():
            logger.error("No cookies available for user data request")
            return None

        headers = {
            "Cookie": self._build_cookie_header(),
            "User-Agent": USER_AGENT,
        }

        try:
            self._rate_limit()
            response = self._http.get(
                USER_DATA_URL, headers=headers, timeout=_GOG_TIMEOUT
            )

            if response.status_code != 200:
                logger.error(f"Failed to get user data: {response.status_code}")
                return None

            data = response.json()
            if data.get("isLoggedIn"):
                logger.info(f"Got user data for: {data.get('username', 'unknown')}")
                return data
            else:
                logger.warning("User data response shows not logged in")
                return None

        except Exception as e:
            logger.error(f"Error fetching user data: {e}")
            return None

    async def get_owned_games(
        self,
        status_callback: Optional[Any] = None,
        cancel_check: Optional[Any] = None,
    ) -> Tuple[List[int], List[Dict[str, Any]]]:
        """Get list of owned game IDs and their basic metadata.

        Returns both the IDs and the full product dicts from getFilteredProducts,
        which include title, slug, image, platforms, category, rating, tags, etc.

        Args:
            status_callback: Optional callback(message) for progress updates
            cancel_check: Optional callback returning True if cancelled

        Returns:
            Tuple of (owned_ids, product_dicts)

        Raises:
            GogApiError: If request fails
        """
        if not self.has_cookies():
            raise GogApiError("No authentication cookies")

        headers = {
            "Cookie": self._build_cookie_header(),
            "User-Agent": USER_AGENT,
        }

        owned_ids = []
        product_dicts = []

        try:
            # GOG uses pagination, need to fetch all pages
            page = 1
            total_pages = 1

            while page <= total_pages:
                if cancel_check and cancel_check():
                    logger.info(f"GOG fetch cancelled at page {page}/{total_pages}")
                    break

                self._rate_limit()
                url = f"{ACCOUNT_URL}?mediaType=1&page={page}"

                # Report progress
                if status_callback:
                    if total_pages > 1:
                        status_callback(f"Fetching GOG library... (page {page}/{total_pages})")
                    else:
                        status_callback("Fetching GOG library...")

                response = self._http.get(
                    url, headers=headers, timeout=_GOG_TIMEOUT
                )

                if response.status_code != 200:
                    error_text = response.text[:200]
                    logger.error(f"Failed to get owned games: {response.status_code} - {error_text}")
                    raise GogApiError(f"Failed to get owned games: {response.status_code}")

                data = response.json()

                # Update total pages
                total_pages = data.get("totalPages", 1)

                # Extract game IDs and full product data
                products = data.get("products", [])
                for product in products:
                    if "id" in product:
                        owned_ids.append(product["id"])
                        product_dicts.append(product)

                logger.debug(f"Page {page}/{total_pages}: {len(products)} games")
                page += 1

            logger.info(f"Found {len(owned_ids)} owned games")
            return owned_ids, product_dicts

        except RequestException as e:
            logger.error(f"Network error fetching owned games: {e}")
            raise GogApiError(f"Network error: {e}") from e

    async def verify_login(self) -> bool:
        """Verify that the stored cookies are still valid

        Returns:
            True if logged in, False otherwise
        """
        user_data = await self.get_user_data()
        return user_data is not None and user_data.get("isLoggedIn", False)

    # Keep old methods for compatibility (they now do nothing)
    async def exchange_code(self, auth_code: str, redirect_uri: str = None) -> Dict[str, Any]:
        """Legacy method - now uses cookies instead"""
        logger.warning("exchange_code called but cookie auth is now used")
        return {}

    async def refresh_access_token(self) -> bool:
        """Legacy method - cookies don't need refresh"""
        return self.has_cookies()

    async def get_game_details(self, product_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed game information including downloads

        Uses GOG's gameDetails endpoint to get installer/patch/extra info.

        Args:
            product_id: GOG product ID

        Returns:
            Dict with game details including:
            - title: Game title
            - slug: URL slug
            - downloads: Dict with platform keys (windows/linux/mac)
            - extras: List of extra content (soundtracks, manuals, etc.)

        Raises:
            GogApiError: If request fails
        """
        if not self.has_cookies():
            raise GogApiError("No authentication cookies")

        headers = {
            "Cookie": self._build_cookie_header(),
            "User-Agent": USER_AGENT,
        }

        # GOG's game details endpoint
        url = f"https://embed.gog.com/account/gameDetails/{product_id}.json"

        try:
            self._rate_limit()
            response = self._http.get(
                url, headers=headers, timeout=_GOG_TIMEOUT
            )

            if response.status_code == 404:
                logger.warning(f"Game not found: {product_id}")
                return None

            if response.status_code != 200:
                error_text = response.text[:200]
                logger.error(
                    f"Failed to get game details for {product_id}: "
                    f"{response.status_code} - {error_text}"
                )
                raise GogApiError(f"Failed to get game details: {response.status_code}")

            data = response.json()

            # Parse the response into a normalized structure
            result = self._parse_game_details(data)
            logger.debug(f"Got game details for {result.get('title', product_id)}")
            return result

        except RequestException as e:
            logger.error(f"Network error fetching game details: {e}")
            raise GogApiError(f"Network error: {e}") from e

    def _parse_game_details(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse GOG's gameDetails response into normalized structure

        GOG's response structure varies slightly, this normalizes it.

        Args:
            data: Raw API response

        Returns:
            Normalized dict with downloads organized by platform
        """
        result = {
            "title": data.get("title", "Unknown"),
            "slug": data.get("slug", ""),
            "gogid": data.get("id"),
            "downloads": {
                "windows": [],
                "linux": [],
                "mac": [],
                "patches": [],
            },
            "extras": [],
        }

        # Parse downloads section
        # GOG returns downloads as a list with nested structures
        downloads = data.get("downloads", [])

        for platform_group in downloads:
            # Each group has a platform name and list of files
            platform = platform_group.get("platform", "").lower()
            if platform == "windows":
                platform_key = "windows"
            elif platform == "linux":
                platform_key = "linux"
            elif platform in ("mac", "osx"):
                platform_key = "mac"
            else:
                continue

            # Files for this platform
            files = platform_group.get("files", [])
            for file_info in files:
                installer = {
                    "id": file_info.get("id"),
                    "name": file_info.get("name", "Installer"),
                    "platform": platform_key,
                    "version": file_info.get("version"),
                    "size": file_info.get("size"),
                    "downlink": file_info.get("downlink"),
                }
                result["downloads"][platform_key].append(installer)

        # Parse patches (some games have these)
        patches = data.get("patches", [])
        for patch in patches:
            patch_info = {
                "id": patch.get("id"),
                "name": patch.get("name", "Patch"),
                "version": patch.get("version"),
                "size": patch.get("size"),
                "downlink": patch.get("downlink"),
            }
            result["downloads"]["patches"].append(patch_info)

        # Parse extras (soundtracks, manuals, artbooks, etc.)
        extras = data.get("extras", [])
        for extra in extras:
            extra_info = {
                "id": extra.get("id"),
                "name": extra.get("name", "Extra"),
                "type": extra.get("type", "unknown"),
                "size": extra.get("size"),
                "downlink": extra.get("manualUrl") or extra.get("downlink"),
            }
            result["extras"].append(extra_info)

        return result

    async def get_product_metadata(self, gogid: int) -> Optional[Dict[str, Any]]:
        """Fetch full product metadata from GOG's public products API.

        No authentication required. Returns description, screenshots, downloads,
        changelog, DLCs, languages, videos, links, images, and platforms.

        Note: Does NOT return developers, publishers, genres, catalog tags,
        features, or pricing — those come from GOGdb only.

        Args:
            gogid: GOG product ID

        Returns:
            Parsed metadata dict or None if not found
        """
        url = f"{PRODUCTS_API_URL}/{gogid}?expand={_PRODUCTS_EXPAND}&locale=en-US"
        headers = {
            "User-Agent": USER_AGENT,
        }

        try:
            self._rate_limit()
            response = self._http.get(
                url, headers=headers, timeout=_GOG_PRODUCTS_TIMEOUT
            )

            if response.status_code == 404:
                logger.debug(f"Product not found on GOG API: {gogid}")
                return None

            if response.status_code != 200:
                logger.warning(f"GOG products API error for {gogid}: {response.status_code}")
                return None

            data = response.json()
            return self._parse_product_metadata(data)

        except RequestException as e:
            logger.warning(f"Network error fetching product {gogid}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Error parsing product {gogid}: {e}")
            return None

    def _parse_product_metadata(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse GOG products API response into a flat metadata dict."""
        images = data.get("images", {})
        desc = data.get("description", {})
        compat = data.get("content_system_compatibility", {})
        links = data.get("links", {})

        # Parse screenshots — extract largest available resolution
        screenshots = []
        for ss in data.get("screenshots", []):
            formatted = ss.get("formatted_images", [])
            # Prefer ggvgl_2x (largest), fallback to ggvgl, then first available
            url = None
            for fmt_name in ("ggvgl_2x", "ggvgl", "ggvgm_2x", "ggvgm"):
                for fmt in formatted:
                    if fmt.get("formatter_name") == fmt_name:
                        url = fmt.get("image_url")
                        break
                if url:
                    break
            if not url and formatted:
                url = formatted[0].get("image_url")
            if url:
                screenshots.append(url)

        # Parse downloads structure (keep full structure for future use)
        downloads_raw = data.get("downloads", {})
        downloads = {
            "installers": downloads_raw.get("installers", []),
            "patches": downloads_raw.get("patches", []),
            "language_packs": downloads_raw.get("language_packs", []),
            "bonus_content": downloads_raw.get("bonus_content", []),
        }

        # Parse expanded DLCs (keep essential fields)
        dlcs = []
        for dlc in data.get("expanded_dlcs", []):
            dlcs.append({
                "id": dlc.get("id"),
                "title": dlc.get("title"),
                "slug": dlc.get("slug"),
                "game_type": dlc.get("game_type"),
            })

        # Parse videos
        videos = []
        for vid in data.get("videos", []):
            videos.append({
                "video_url": vid.get("video_url"),
                "thumbnail_url": vid.get("thumbnail_url"),
                "provider": vid.get("provider"),
            })

        # Ensure image URLs have protocol and file extension
        bg_url = images.get("background", "")
        if bg_url and bg_url.startswith("//"):
            bg_url = "https:" + bg_url
        if bg_url and "gog-statics.com/" in bg_url and not bg_url.endswith(('.jpg', '.png', '.webp')):
            bg_url += ".jpg"
        logo_url = images.get("logo2x") or images.get("logo", "")
        if logo_url and logo_url.startswith("//"):
            logo_url = "https:" + logo_url
        if logo_url and "gog-statics.com/" in logo_url and not logo_url.endswith(('.jpg', '.png', '.webp')):
            logo_url += ".png"
        icon_url = images.get("icon", "")
        if icon_url and icon_url.startswith("//"):
            icon_url = "https:" + icon_url
        if icon_url and "gog-statics.com/" in icon_url and not icon_url.endswith(('.jpg', '.png', '.webp')):
            icon_url += ".png"

        return {
            "gogid": data.get("id"),
            "title": data.get("title"),
            "slug": data.get("slug"),
            "game_type": data.get("game_type"),
            "release_date": data.get("release_date"),
            # Descriptions
            "description": desc.get("full"),
            "description_lead": desc.get("lead"),
            "description_cool": desc.get("whats_cool_about_it"),
            # Images
            "background_url": bg_url,
            "logo_url": logo_url,
            "icon_url": icon_url,
            "screenshots": screenshots,
            # Platforms
            "windows": compat.get("windows", False),
            "mac": compat.get("osx", False),
            "linux": compat.get("linux", False),
            # Rich data
            "downloads_json": downloads,
            "dlcs": dlcs,
            "videos": videos,
            "changelog": data.get("changelog"),
            "languages": data.get("languages", {}),
            "links": links,
            # Flags
            "is_installable": data.get("is_installable", False),
            "in_development": data.get("in_development", {}).get("active", False),
        }

    async def fetch_catalog_for_game(
        self, gogid: int, title: str
    ) -> Optional[Dict[str, Any]]:
        """Fetch game data from GOG's public catalog API (Tier 3 gap-filler).

        Searches by title and matches on GOG ID. No auth required.

        Args:
            gogid: GOG product ID to match
            title: Game title for search query

        Returns:
            Parsed catalog product dict or None if not found
        """
        # Use first few words of title for broader matching
        query = title[:80] if title else ""
        if not query:
            return None

        url = (
            f"https://catalog.gog.com/v1/catalog"
            f"?query={query}&limit=5"
            f"&locale=en-US&countryCode=US&currencyCode=USD"
        )

        try:
            self._rate_limit()
            response = self._http.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=_GOG_PRODUCTS_TIMEOUT,
            )

            if response.status_code != 200:
                logger.debug(f"Catalog API returned {response.status_code} for '{query}'")
                return None

            data = response.json()
            products = data.get("products", [])

            # Match by GOG ID
            for product in products:
                if product.get("id") == gogid:
                    return self._parse_catalog_product(product)

            logger.debug(f"Game {gogid} not found in catalog search for '{query}'")
            return None

        except RequestException as e:
            logger.warning(f"Network error fetching catalog for {gogid}: {e}")
            return None
        except Exception as e:
            logger.warning(f"Error parsing catalog for {gogid}: {e}")
            return None

    def _parse_catalog_product(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a catalog API product into a flat metadata dict.

        Extracts ALL useful fields from the catalog API response.
        """
        # Resolve coverVertical URL — strip _{formatter} to get bare hash
        # (original uploaded vertical image, full resolution)
        cover_v = product.get("coverVertical", "")
        if cover_v and "_{formatter}" in cover_v:
            cover_v = cover_v.replace("_{formatter}", "")

        cover_h = product.get("coverHorizontal", "")
        if cover_h and "_{formatter}" in cover_h:
            cover_h = cover_h.replace("_{formatter}", "")

        # Logo URL — strip formatter
        logo = product.get("logo", "")
        if logo and "_{formatter}" in logo:
            logo = logo.replace("_{formatter}", "")

        # Galaxy background image — strip formatter
        galaxy_bg = product.get("galaxyBackgroundImage", "")
        if galaxy_bg and "_{formatter}" in galaxy_bg:
            galaxy_bg = galaxy_bg.replace("_{formatter}", "")

        # Screenshots — resolve formatter with ggvgl_2x (~1080p)
        screenshots = []
        for ss_url in product.get("screenshots", []):
            if isinstance(ss_url, str) and "{formatter}" in ss_url:
                screenshots.append(ss_url.replace("{formatter}", "ggvgl_2x"))
            elif isinstance(ss_url, str):
                screenshots.append(ss_url)

        # Tags — list of {id, name, slug}
        tags = [t.get("name", "") for t in product.get("tags", []) if t.get("name")]

        # Features — list of {id, name, slug}
        features = [f.get("name", "") for f in product.get("features", []) if f.get("name")]

        # Genres
        genres = [g.get("name", "") for g in product.get("genres", []) if g.get("name")]

        # Developers/Publishers
        developers = product.get("developers", [])
        publishers = product.get("publishers", [])

        # Content ratings — [{name, ageRating}] (PEGI/ESRB/USK/BR/GOG)
        content_ratings = product.get("ratings", [])
        if not isinstance(content_ratings, list):
            content_ratings = []

        # Price — dump entire price object as-is
        price_json = product.get("price")
        if price_json is None:
            price_json = {}

        # Product state
        product_state = product.get("productState")

        # Editions — [{id, name, isRootEdition, ...}]
        editions = product.get("editions", [])

        # Languages — from userPreferredLanguage field
        lang_info = product.get("userPreferredLanguage")
        languages_data = {}
        if isinstance(lang_info, dict):
            languages_data = lang_info

        # Operating systems → platform booleans
        os_list = product.get("operatingSystems", [])
        windows = "windows" in os_list if os_list else False
        mac = ("osx" in os_list or "mac" in os_list) if os_list else False
        linux = "linux" in os_list if os_list else False

        # Release dates
        release_date = product.get("releaseDate")
        store_release_date = product.get("storeReleaseDate")

        return {
            "gogid": product.get("id"),
            "title": product.get("title"),
            "slug": product.get("slug"),
            "product_type": product.get("productType", "game"),
            "cover_vertical_url": cover_v,
            "cover_horizontal_url": cover_h,
            "logo_url": logo,
            "galaxy_background_url": galaxy_bg,
            "screenshots": screenshots,
            "tags": tags,
            "features": features,
            "genres": genres,
            "developers": developers if isinstance(developers, list) else [],
            "publishers": publishers if isinstance(publishers, list) else [],
            "operating_systems": os_list,
            "windows": windows,
            "mac": mac,
            "linux": linux,
            "reviews_rating": product.get("reviewsRating"),
            "reviews_count": product.get("reviewsCount"),
            "release_date": release_date,
            "store_release_date": store_release_date,
            "global_release_date": product.get("globalReleaseDate"),
            "store_link": product.get("storeLink"),
            "is_available": product.get("isAvailable", True),
            "editions": editions,
            "content_ratings": content_ratings,
            "price_json": price_json,
            "product_state": product_state,
            "languages_data": languages_data,
        }

    @staticmethod
    def _get_catalog_headers() -> Dict[str, str]:
        """Browser-like headers for catalog API requests.

        Simulates a real Chrome XHR from gog.com to avoid blocks.
        Platform is detected at runtime.
        """
        import platform as _platform

        plat = {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}.get(
            _platform.system(), "Linux"
        )
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://www.gog.com/",
            "Sec-CH-UA": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{plat}"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    async def fetch_full_catalog(
        self,
        status_callback: Optional[Any] = None,
        cancel_check: Optional[Any] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Paginate through the entire GOG catalog API.

        Returns all products keyed by GOG ID. Fetches both "game" and "pack"
        product types (packs are treated as games).

        Args:
            status_callback: Optional callback(message, current, total) for progress
            cancel_check: Optional callback returning True if cancelled

        Returns:
            Dict mapping gogid -> parsed catalog product dict
        """
        headers = self._get_catalog_headers()

        # Add auth cookies (skip csrf)
        cookie_header = self._build_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header

        catalog: Dict[int, Dict[str, Any]] = {}
        page = 1
        total_pages = 1

        try:
            while page <= total_pages:
                if cancel_check and cancel_check():
                    logger.info(f"Catalog scan cancelled at page {page}/{total_pages}")
                    break

                self._rate_limit()

                url = (
                    f"https://catalog.gog.com/v1/catalog"
                    f"?limit=50&page={page}&order=title:asc"
                    f"&productType=in:game,pack"
                    f"&locale=en-US&countryCode=US&currencyCode=USD"
                )

                if status_callback:
                    status_callback(
                        f"Fetching catalog page {page}/{total_pages}...",
                        page,
                        total_pages,
                    )

                response = self._http.get(
                    url, headers=headers, timeout=_GOG_PRODUCTS_TIMEOUT
                )

                if response.status_code != 200:
                    logger.warning(
                        f"Catalog API returned {response.status_code} on page {page}"
                    )
                    break

                data = response.json()
                total_pages = data.get("pages", 1)
                products = data.get("products", [])

                for product in products:
                    raw_id = product.get("id")
                    if raw_id:
                        # Catalog API returns string IDs — normalize to int
                        gogid = int(raw_id) if isinstance(raw_id, str) else raw_id
                        catalog[gogid] = self._parse_catalog_product(product)

                logger.debug(
                    f"Catalog page {page}/{total_pages}: "
                    f"{len(products)} items, {len(catalog)} total"
                )
                page += 1

        except RequestException as e:
            logger.error(f"Network error during catalog scan: {e}")
        except Exception as e:
            logger.error(f"Error during catalog scan: {e}")

        logger.info(f"Catalog scan complete: {len(catalog)} products fetched")
        return catalog

    async def resolve_download_link(self, downlink: str) -> Optional[str]:
        """Resolve a GOG downlink to the actual download URL

        GOG's downlinks redirect to CDN URLs with tokens.
        This follows the redirect to get the final download URL.

        Args:
            downlink: The downlink URL from game details

        Returns:
            Resolved download URL or None if resolution fails
        """
        if not downlink:
            return None

        if not self.has_cookies():
            raise GogApiError("No authentication cookies")

        headers = {
            "Cookie": self._build_cookie_header(),
            "User-Agent": USER_AGENT,
        }

        try:
            self._rate_limit()
            # Don't follow redirects - we want to capture the Location header
            response = self._http.get(
                downlink,
                headers=headers,
                allow_redirects=False,
                timeout=_GOG_TIMEOUT,
            )

            if response.status_code in (301, 302, 303, 307, 308):
                # Get redirect location
                location = response.headers.get("Location")
                if location:
                    logger.debug(f"Resolved downlink to: {location[:100]}...")
                    return location

            if response.status_code == 200:
                # Some endpoints return JSON with the URL
                try:
                    data = response.json()
                    return data.get("downlink") or data.get("url")
                except Exception:
                    pass

            logger.warning(
                f"Failed to resolve downlink: status={response.status_code}"
            )
            return None

        except RequestException as e:
            logger.error(f"Network error resolving download link: {e}")
            return None
