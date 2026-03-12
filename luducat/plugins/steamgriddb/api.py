# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# api.py

"""SteamGridDB API v2 wrapper with rate limiting

Provides authenticated access to the SteamGridDB REST API for
fetching community-sourced game images (heroes, grids, logos, icons).

Auth: API key via Bearer token header.
Rate limit: 5 requests/second (official limits undocumented, 429 retry as safety net).

API reference: https://www.steamgriddb.com/api/v2
"""

import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from luducat.plugins.sdk.network import (
    ConnectionError as RequestConnectionError,
    HTTPError,
    RequestException,
    RequestTimeout,
    Response,
)

logger = logging.getLogger(__name__)

# Type alias for progress callback: (message, current, total)
ProgressCallback = Optional[Callable[[str, int, int], None]]

# =============================================================================
# API Constants
# =============================================================================

SGDB_API_BASE = "https://www.steamgriddb.com/api/v2"

# Rate limiting: 5 requests per second (429 retry handler as safety net)
RATE_LIMIT_REQUESTS = 5
RATE_LIMIT_PERIOD = 1.0  # seconds
RATE_LIMIT_RETRY_DELAY = 3.0  # seconds to wait after 429
MAX_RATE_LIMIT_RETRIES = 3

# =============================================================================
# Valid Filter Values (from API v2 docs)
# =============================================================================

# Valid styles per asset type
HERO_STYLES = ["alternate", "blurred", "material"]
GRID_STYLES = ["alternate", "blurred", "white_logo", "material", "no_logo"]
LOGO_STYLES = ["official", "white", "black", "custom"]
ICON_STYLES = ["official", "custom"]

# Valid dimensions per asset type
HERO_DIMENSIONS = ["1920x620", "3840x1240", "1600x650"]
GRID_DIMENSIONS = [
    "460x215", "920x430", "600x900", "342x482",
    "660x930", "512x512", "1024x1024",
]
ICON_DIMENSIONS = [
    "8", "10", "14", "16", "20", "24", "28", "32", "35", "40", "48",
    "54", "56", "57", "60", "64", "72", "76", "80", "90", "96", "100",
    "114", "120", "128", "144", "150", "152", "160", "180", "192", "194",
    "256", "310", "512", "768", "1024",
]

# Valid MIME types per asset type
HERO_MIMES = ["image/png", "image/jpeg", "image/webp"]
GRID_MIMES = ["image/png", "image/jpeg", "image/webp"]
LOGO_MIMES = ["image/png", "image/webp"]
ICON_MIMES = ["image/png", "image/vnd.microsoft.icon"]

# Valid types filter values
ASSET_TYPES = ["static", "animated"]

# All supported platform strings for /games/{platform}/{id} endpoints
STORE_PLATFORMS = {
    "steam": "steam",
    "gog": "gog",
    "epic": "egs",
    "origin": "origin",
    "uplay": "uplay",
    "battlenet": "bnet",
    "eshop": "eshop",
}


# =============================================================================
# Exceptions
# =============================================================================

class SgdbApiError(Exception):
    """SteamGridDB API error"""
    pass


class SgdbAuthError(SgdbApiError):
    """Authentication/API key error"""
    pass


class SgdbRateLimitError(SgdbApiError):
    """Rate limit exceeded"""
    pass


class SgdbCancelledError(SgdbApiError):
    """Operation cancelled by user (skip/cancel)"""
    pass


# =============================================================================
# API Client
# =============================================================================

class SgdbApi:
    """SteamGridDB API v2 client with rate limiting

    Uses API key authentication via Bearer token.
    Rate limits to 5 requests/second.
    """

    def __init__(self, api_key: str, http_client=None):
        self._api_key = api_key
        self._request_times: List[float] = []
        self._cancel_event = threading.Event()
        self._http = http_client

    def cancel(self) -> None:
        """Signal cancellation — wakes any sleeping waits."""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear cancellation state for a new enrichment run."""
        self._cancel_event.clear()

    # =========================================================================
    # Rate Limiting
    # =========================================================================

    def _rate_limit(self, status_callback: ProgressCallback = None) -> None:
        """Enforce rate limiting (5 requests/second)"""
        now = time.time()

        # Remove old timestamps outside the window
        self._request_times = [
            t for t in self._request_times
            if now - t < RATE_LIMIT_PERIOD
        ]

        # Wait if at limit
        if len(self._request_times) >= RATE_LIMIT_REQUESTS:
            oldest = self._request_times[0]
            sleep_time = RATE_LIMIT_PERIOD - (now - oldest)
            if sleep_time > 0.05:
                if status_callback:
                    status_callback(
                        f"SteamGridDB rate limit - waiting {sleep_time:.2f}s...",
                        -1, -1,
                    )
                logger.debug(f"Rate limit: waiting {sleep_time:.2f}s")
                if self._cancel_event.wait(sleep_time):
                    raise SgdbCancelledError("SteamGridDB operation cancelled")

        self._request_times.append(time.time())

    # =========================================================================
    # Core Request
    # =========================================================================

    def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, str]] = None,
        status_callback: ProgressCallback = None,
        retry_count: int = 0,
    ) -> Any:
        """Make a GET request to the SteamGridDB API

        Args:
            endpoint: API endpoint path (e.g., "/games/steam/730")
            params: Optional query parameters
            status_callback: Optional progress callback
            retry_count: Current retry attempt

        Returns:
            Parsed JSON response data (the "data" field from response)

        Raises:
            SgdbApiError: On API errors
            SgdbAuthError: On authentication failures
            SgdbRateLimitError: On rate limit exceeded after retries
        """
        # Check cancel before making network request
        if self._cancel_event.is_set():
            raise SgdbCancelledError("SteamGridDB operation cancelled")

        self._rate_limit(status_callback)

        url = f"{SGDB_API_BASE}{endpoint}"

        response = None
        try:
            response = self._http.get(
                url, params=params,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                },
                timeout=15,
            )

            if response.status_code == 429:
                if retry_count < MAX_RATE_LIMIT_RETRIES:
                    wait_time = RATE_LIMIT_RETRY_DELAY * (retry_count + 1)
                    if status_callback:
                        status_callback(
                            f"SteamGridDB rate limit - retrying in {wait_time:.1f}s...",
                            -1, -1,
                        )
                    logger.warning(
                        f"Rate limit 429, waiting {wait_time}s before retry {retry_count + 1}"
                    )
                    if self._cancel_event.wait(wait_time):
                        raise SgdbCancelledError("SteamGridDB operation cancelled")
                    return self._request(
                        endpoint, params, status_callback, retry_count + 1
                    )
                else:
                    raise SgdbRateLimitError(
                        f"Rate limit exceeded after {MAX_RATE_LIMIT_RETRIES} retries"
                    )

            if response.status_code == 401:
                raise SgdbAuthError("Invalid API key")

            if response.status_code == 404:
                return None

            response.raise_for_status()

            data = response.json()
            # SteamGridDB wraps responses: {"success": true, "data": [...]}
            if isinstance(data, dict):
                if not data.get("success", True):
                    errors = data.get("errors", [])
                    raise SgdbApiError(f"API error: {errors}")
                return data.get("data")

            return data

        except RequestTimeout as e:
            raise SgdbApiError(f"Request timed out: {endpoint}") from e
        except RequestConnectionError as e:
            raise SgdbApiError(f"Connection error: {e}") from e
        except RequestException as e:
            if isinstance(e, HTTPError) and response is not None:
                raise SgdbApiError(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                ) from e
            raise SgdbApiError(f"Request failed: {e}") from e

    # =========================================================================
    # Key Validation
    # =========================================================================

    def validate_key(self) -> bool:
        """Test API key by making a simple search request"""
        try:
            self._request("/search/autocomplete/test", status_callback=None)
            return True
        except SgdbAuthError:
            return False
        except SgdbApiError:
            return True

    # =========================================================================
    # Game Lookup
    # =========================================================================

    def get_game_by_platform_id(
        self,
        store_name: str,
        platform_id: str,
        status_callback: ProgressCallback = None,
    ) -> Optional[Dict[str, Any]]:
        """Look up a game by store platform ID

        Uses the /games/{platform}/{id} endpoint for direct lookups.

        Returns:
            Game dict with keys: id, name, release_date, types, verified
            or None if not found
        """
        platform = STORE_PLATFORMS.get(store_name)
        if not platform:
            logger.debug(f"No SteamGridDB platform mapping for store: {store_name}")
            return None

        endpoint = f"/games/{platform}/{platform_id}"
        try:
            data = self._request(endpoint, status_callback=status_callback)
            if data and isinstance(data, dict):
                return data
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except SgdbApiError as e:
            logger.debug(f"SteamGridDB game lookup failed for {store_name}/{platform_id}: {e}")

        return None

    def get_game_by_id(
        self,
        sgdb_id: int,
        status_callback: ProgressCallback = None,
    ) -> Optional[Dict[str, Any]]:
        """Look up a game by SteamGridDB game ID"""
        endpoint = f"/games/id/{sgdb_id}"
        try:
            data = self._request(endpoint, status_callback=status_callback)
            if data and isinstance(data, dict):
                return data
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except SgdbApiError as e:
            logger.debug(f"SteamGridDB game lookup by ID failed for {sgdb_id}: {e}")
        return None

    def search_game(
        self,
        term: str,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Search for games by title

        Uses the /search/autocomplete endpoint.

        Returns:
            List of game dicts (id, name, release_date, types, verified)
        """
        encoded_term = quote(term, safe="")
        endpoint = f"/search/autocomplete/{encoded_term}"
        try:
            data = self._request(endpoint, status_callback=status_callback)
            if data and isinstance(data, list):
                return data
        except SgdbApiError as e:
            logger.debug(f"SteamGridDB search failed for '{term}': {e}")
        return []

    # =========================================================================
    # Asset Fetching — Core
    # =========================================================================

    def _get_assets(
        self,
        asset_type: str,
        game_id: int,
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch assets of a given type for a game

        Args:
            asset_type: heroes, grids, logos, icons
            game_id: SteamGridDB game ID
            styles: Filter by style (per asset type — see constants)
            dimensions: Filter by dimensions (per asset type — see constants)
            mimes: Filter by MIME type (per asset type — see constants)
            types: Filter by animation type ("static", "animated")
            nsfw: NSFW filter ("true", "false", "any")
            humor: Humor filter ("true", "false", "any")
            epilepsy: Epilepsy filter ("true", "false", "any")
            limit: Max results per page (max 50)
            page: Page number for pagination
            status_callback: Optional progress callback

        Returns:
            List of asset dicts with keys:
            id, score, style, url, thumb, tags, author, nsfw, humor,
            notes, language, lock, epilepsy, width, height, mime, ...
        """
        endpoint = f"/{asset_type}/game/{game_id}"
        params = {}
        if styles:
            params["styles"] = ",".join(styles)
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if mimes:
            params["mimes"] = ",".join(mimes)
        if types:
            params["types"] = ",".join(types)
        if nsfw:
            params["nsfw"] = nsfw
        if humor:
            params["humor"] = humor
        if epilepsy:
            params["epilepsy"] = epilepsy
        if limit is not None:
            params["limit"] = str(min(limit, 50))
        if page is not None:
            params["page"] = str(page)

        try:
            data = self._request(
                endpoint, params=params or None, status_callback=status_callback
            )
            if data and isinstance(data, list):
                return data
        except SgdbApiError as e:
            logger.debug(
                f"SteamGridDB {asset_type} fetch failed for game {game_id}: {e}"
            )
        return []

    def _get_assets_by_platform(
        self,
        asset_type: str,
        platform: str,
        platform_ids: List[str],
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch assets by platform IDs (batch lookup)

        Uses the /{asset_type}/{platform}/{id*} endpoint.
        Supports comma-delimited IDs for batch lookups.

        Args:
            asset_type: heroes, grids, logos, icons
            platform: Platform string (steam, gog, egs, origin, uplay, bnet, eshop)
            platform_ids: List of platform-specific game IDs
            (remaining args same as _get_assets)

        Returns:
            List of asset dicts
        """
        if not platform_ids:
            return []

        ids_str = ",".join(str(pid) for pid in platform_ids)
        endpoint = f"/{asset_type}/{platform}/{ids_str}"
        params = {}
        if styles:
            params["styles"] = ",".join(styles)
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if mimes:
            params["mimes"] = ",".join(mimes)
        if types:
            params["types"] = ",".join(types)
        if nsfw:
            params["nsfw"] = nsfw
        if humor:
            params["humor"] = humor
        if epilepsy:
            params["epilepsy"] = epilepsy
        if limit is not None:
            params["limit"] = str(min(limit, 50))
        if page is not None:
            params["page"] = str(page)

        try:
            data = self._request(
                endpoint, params=params or None, status_callback=status_callback
            )
            if data and isinstance(data, list):
                return data
        except SgdbApiError as e:
            logger.debug(
                f"SteamGridDB {asset_type} platform fetch failed for "
                f"{platform}/{ids_str}: {e}"
            )
        return []

    # =========================================================================
    # Asset Fetching — Typed Convenience Methods
    # =========================================================================

    def get_heroes(
        self,
        game_id: int,
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch hero banner images for a game

        Heroes are wide banner images used as background artwork.
        Valid styles: alternate, blurred, material
        Valid dimensions: 1920x620, 3840x1240, 1600x650
        """
        return self._get_assets(
            "heroes", game_id, styles, dimensions, mimes, types,
            nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_grids(
        self,
        game_id: int,
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch grid/cover images for a game

        Grids are cover art images (vertical and horizontal).
        Valid styles: alternate, blurred, white_logo, material, no_logo
        Valid dimensions: 460x215, 920x430, 600x900, 342x482, 660x930, 512x512, 1024x1024
        """
        return self._get_assets(
            "grids", game_id, styles, dimensions, mimes, types,
            nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_logos(
        self,
        game_id: int,
        styles: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch logo images for a game

        Logos are transparent game title/logo images.
        Valid styles: official, white, black, custom
        """
        return self._get_assets(
            "logos", game_id, styles, None, mimes, types,
            nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_icons(
        self,
        game_id: int,
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch icon images for a game

        Icons are small square game icons.
        Valid styles: official, custom
        Valid dimensions: 8-1024 (see ICON_DIMENSIONS)
        """
        return self._get_assets(
            "icons", game_id, styles, dimensions, mimes, types,
            nsfw, humor, epilepsy, limit, page, status_callback,
        )

    # =========================================================================
    # Platform-Based Asset Fetching (batch)
    # =========================================================================

    def get_heroes_by_platform(
        self,
        platform: str,
        platform_ids: List[str],
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch hero banners by platform IDs (batch)

        Uses /heroes/{platform}/{id*} with comma-delimited IDs.
        """
        return self._get_assets_by_platform(
            "heroes", platform, platform_ids, styles, dimensions, mimes,
            types, nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_grids_by_platform(
        self,
        platform: str,
        platform_ids: List[str],
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch grid/cover images by platform IDs (batch)

        Uses /grids/{platform}/{id*} with comma-delimited IDs.
        """
        return self._get_assets_by_platform(
            "grids", platform, platform_ids, styles, dimensions, mimes,
            types, nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_logos_by_platform(
        self,
        platform: str,
        platform_ids: List[str],
        styles: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch logo images by platform IDs (batch)

        Uses /logos/{platform}/{id*} with comma-delimited IDs.
        """
        return self._get_assets_by_platform(
            "logos", platform, platform_ids, styles, None, mimes,
            types, nsfw, humor, epilepsy, limit, page, status_callback,
        )

    def get_icons_by_platform(
        self,
        platform: str,
        platform_ids: List[str],
        styles: Optional[List[str]] = None,
        dimensions: Optional[List[str]] = None,
        mimes: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        nsfw: Optional[str] = None,
        humor: Optional[str] = None,
        epilepsy: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        status_callback: ProgressCallback = None,
    ) -> List[Dict[str, Any]]:
        """Fetch icon images by platform IDs (batch)

        Uses /icons/{platform}/{id*} with comma-delimited IDs.
        """
        return self._get_assets_by_platform(
            "icons", platform, platform_ids, styles, dimensions, mimes,
            types, nsfw, humor, epilepsy, limit, page, status_callback,
        )

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self) -> None:
        """Close the API client"""
        pass  # Session lifecycle managed by NetworkManager


# =============================================================================
# Public API (no auth required)
# =============================================================================


def fetch_sgdb_user_stats(
    steam64: str, timeout: int = 5, http_client=None,
) -> Optional[Dict[str, int]]:
    """Fetch author upload stats from SteamGridDB.

    Returns {"grid": N, "hero": N, "logo": N, "icon": N} or None on failure.
    Primary: GET /api/v2/users/{steam64} (no auth).
    Fallback: scrape profile page HTML for stats if API returns error.
    Respects rate limits (429) by waiting and retrying once.
    """
    import time

    if not steam64:
        return None

    if http_client is None:
        logger.warning("fetch_sgdb_user_stats called without http_client")
        return None

    # Primary: undocumented API endpoint
    try:
        resp = http_client.get(
            f"{SGDB_API_BASE}/users/{steam64}",
            timeout=timeout,
        )
        if resp.status_code == 429:
            wait = _rate_limit_wait(resp)
            logger.debug(f"SGDB user API rate-limited, waiting {wait:.1f}s")
            time.sleep(wait)
            resp = http_client.get(
                f"{SGDB_API_BASE}/users/{steam64}",
                timeout=timeout,
            )
        if resp.status_code == 200:
            data = resp.json()
            stats = data.get("data", {}).get("stats", {})
            if stats:
                return {
                    "grid": stats.get("grids", {}).get("total", 0),
                    "hero": stats.get("heroes", {}).get("total", 0),
                    "logo": stats.get("logos", {}).get("total", 0),
                    "icon": stats.get("icons", {}).get("total", 0),
                }
    except (RequestException, ValueError, KeyError) as e:
        logger.debug(f"SGDB user API failed for {steam64}: {e}")

    # Fallback: scrape profile page HTML
    return _scrape_sgdb_profile_stats(steam64, timeout, http_client=http_client)


def _rate_limit_wait(resp: Response) -> float:
    """Calculate wait time from a 429 response with jitter.

    Uses Retry-After header if present, otherwise defaults to 5-8s.
    Adds random jitter so retry timing looks organic.
    """
    import random

    base = 5.0
    try:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            base = float(retry_after)
    except (ValueError, TypeError):
        pass
    # Add 1-3s of jitter
    return base + 1.0 + random.random() * 2.0


def _scrape_sgdb_profile_stats(
    steam64: str, timeout: int = 5, http_client=None,
) -> Optional[Dict[str, int]]:
    """Scrape upload stats from SteamGridDB profile page HTML.

    The profile page embeds stats in a p.stats element with patterns like
    "521 Grids", "12 Heroes", etc.
    Respects rate limits (429) by waiting and retrying once.
    """
    import time

    if http_client is None:
        logger.warning("_scrape_sgdb_profile_stats called without http_client")
        return None

    try:
        resp = http_client.get(
            f"https://www.steamgriddb.com/profile/{steam64}",
            timeout=timeout,
        )
        if resp.status_code == 429:
            wait = _rate_limit_wait(resp)
            logger.debug(f"SGDB profile rate-limited, waiting {wait:.1f}s")
            time.sleep(wait)
            resp = http_client.get(
                f"https://www.steamgriddb.com/profile/{steam64}",
                timeout=timeout,
            )
        if resp.status_code != 200:
            return None

        html = resp.text
        result = {}
        for key, pattern in (
            ("grid", r"(\d[\d,]*)\s*Grids?"),
            ("hero", r"(\d[\d,]*)\s*Heroes?"),
            ("logo", r"(\d[\d,]*)\s*Logos?"),
            ("icon", r"(\d[\d,]*)\s*Icons?"),
        ):
            match = re.search(pattern, html)
            if match:
                result[key] = int(match.group(1).replace(",", ""))
            else:
                result[key] = 0

        # Only return if we found at least one stat
        if any(v > 0 for v in result.values()):
            return result
        return None

    except (RequestException, ValueError) as e:
        logger.debug(f"SGDB profile scrape failed for {steam64}: {e}")
        return None
