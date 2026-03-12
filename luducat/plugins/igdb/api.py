# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# api.py

"""IGDB API wrapper with rate limiting and authentication

Supports two modes:
- **Proxy mode** (default): Routes requests through the luducat IGDB proxy.
  No Twitch credentials needed. Uses HMAC-TOTP signing for request validation.
- **BYOK mode**: Direct IGDB API access with user-provided Twitch credentials.
  Uses Twitch OAuth for authentication.

Both modes use the same Apicalypse query format and return identical JSON responses.

Handles:
- Twitch OAuth authentication (BYOK mode)
- HMAC-TOTP request signing (proxy mode)
- Token caching and auto-refresh
- Rate limiting with display, wait, retry
- Comprehensive IGDB queries matching full IGDB page data
"""

from luducat.plugins.sdk.json import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from luducat.plugins.sdk.network import RequestException

from luducat.plugins.sdk.datetime import utc_now

logger = logging.getLogger(__name__)

# Type alias for progress callback: (message, current, total)
ProgressCallback = Optional[Callable[[str, int, int], None]]

# IGDB API constants
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"
IGDB_API_BASE = "https://api.igdb.com/v4"

# Rate limiting (per second)
RATE_LIMIT_REQUESTS_PROXY = 4  # Conservative for shared proxy credentials
RATE_LIMIT_REQUESTS_BYOK = 5   # User's own credentials, slightly higher
RATE_LIMIT_PERIOD = 1.0  # seconds
RATE_LIMIT_RETRY_DELAY = 2.0  # seconds to wait after 429
MAX_RATE_LIMIT_RETRIES = 3

# Circuit breaker: escalating backoff on repeated proxy 429s
CIRCUIT_BREAKER_INITIAL = 600    # 10 minutes
CIRCUIT_BREAKER_MAX = 3600       # 1 hour

# Store category IDs for external_games lookups
STORE_CATEGORIES = {
    "steam": 1,
    "gog": 5,
    "epic": 26,
}

# Platform IDs for PC release date selection
PLATFORM_PC_WINDOWS = 6
PLATFORM_DOS = 13

# Image base URL
IMAGE_BASE = "https://images.igdb.com/igdb/image/upload"

# Image size presets
IMAGE_SIZES = {
    "cover_small": "t_cover_small",
    "cover_big": "t_cover_big",
    "cover_big_2x": "t_cover_big_2x",
    "screenshot_med": "t_screenshot_med",
    "screenshot_big": "t_screenshot_big",
    "screenshot_huge": "t_screenshot_huge",
    "720p": "t_720p",
    "1080p": "t_1080p",
    "thumb": "t_thumb",
}


@dataclass
class TokenInfo:
    """OAuth token information"""
    access_token: str
    expires_at: datetime

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """Check if token is expired or will expire soon"""
        return utc_now() >= (self.expires_at - timedelta(seconds=buffer_seconds))


class IgdbApiError(Exception):
    """IGDB API error"""
    pass


class IgdbAuthError(IgdbApiError):
    """Authentication error"""
    pass


class IgdbRateLimitError(IgdbApiError):
    """Rate limit exceeded"""
    pass


class IgdbCancelledError(IgdbApiError):
    """Operation cancelled by user"""
    pass


class IgdbApi:
    """IGDB API client with rate limiting

    Supports two modes:
    - Proxy mode (default): Routes through luducat IGDB proxy, no Twitch creds needed
    - BYOK mode: Direct IGDB API access with user-provided Twitch credentials
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        get_credential: Optional[Callable[[str], Optional[str]]] = None,
        set_credential: Optional[Callable[[str, str], None]] = None,
        delete_credential: Optional[Callable[[str], None]] = None,
        proxy_mode: bool = False,
        proxy_url: Optional[str] = None,
        http_client=None,
    ):
        """Initialize IGDB API client

        Args:
            client_id: Twitch application Client ID (BYOK mode)
            client_secret: Twitch application Client Secret (BYOK mode)
            get_credential: Callback to retrieve credential from keyring
            set_credential: Callback to store credential in keyring
            delete_credential: Callback to delete credential from keyring
            proxy_mode: If True, route requests through the luducat proxy
            proxy_url: Proxy base URL (defaults to IGDB_PROXY_DEFAULT)
            http_client: PluginHttpClient for all HTTP requests
        """
        from luducat.plugins.sdk.proxy import get_proxy_url

        self.client_id = client_id or ""
        self.client_secret = client_secret or ""
        self._get_credential = get_credential
        self._set_credential = set_credential
        self._delete_credential = delete_credential
        self._proxy_mode = proxy_mode
        self._proxy_url = proxy_url or get_proxy_url()
        self._token: Optional[TokenInfo] = None
        self._request_times: List[float] = []
        self._http = http_client
        self._cancel_event = threading.Event()
        self._rate_limit_requests = (
            RATE_LIMIT_REQUESTS_PROXY if proxy_mode else RATE_LIMIT_REQUESTS_BYOK
        )

        # Circuit breaker for proxy 429s: escalating backoff
        self._circuit_open_until: float = 0
        self._circuit_backoff: float = CIRCUIT_BREAKER_INITIAL

    def _load_cached_token(self) -> Optional[TokenInfo]:
        """Load token from keyring"""
        if not self._get_credential:
            return None

        try:
            access_token = self._get_credential("access_token")
            expires_at_str = self._get_credential("token_expires_at")

            if not access_token or not expires_at_str:
                return None

            token = TokenInfo(
                access_token=access_token,
                expires_at=datetime.fromisoformat(expires_at_str)
            )
            if not token.is_expired():
                logger.debug("Loaded cached IGDB token from keyring")
                return token
        except Exception as e:
            logger.warning(f"Failed to load cached token: {e}")

        return None

    def _save_token_cache(self, token: TokenInfo) -> None:
        """Save token to keyring"""
        if not self._set_credential:
            return

        try:
            self._set_credential("access_token", token.access_token)
            self._set_credential("token_expires_at", token.expires_at.isoformat())
            logger.debug("Saved IGDB token to keyring")
        except Exception as e:
            logger.warning(f"Failed to save token to keyring: {e}")

    def _clear_token_cache(self) -> None:
        """Clear cached token from keyring (called on auth failure)"""
        if not self._delete_credential:
            return

        try:
            self._delete_credential("access_token")
            self._delete_credential("token_expires_at")
            logger.debug("Cleared IGDB token from keyring")
        except Exception as e:
            logger.warning(f"Failed to clear token from keyring: {e}")

    async def authenticate(self) -> bool:
        """Authenticate with Twitch OAuth

        Returns:
            True if authentication successful
        """
        return self.authenticate_sync()

    def authenticate_sync(self) -> bool:
        """Synchronous authentication for use outside async context"""
        # Try cached token first
        self._token = self._load_cached_token()
        if self._token and not self._token.is_expired():
            return True

        # Request new token
        try:
            response = self._http.post(
                TWITCH_AUTH_URL,
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials"
                }
            )
            response.raise_for_status()
            data = response.json()

            expires_in = data.get("expires_in", 3600)
            self._token = TokenInfo(
                access_token=data["access_token"],
                expires_at=utc_now() + timedelta(seconds=expires_in)
            )
            self._save_token_cache(self._token)
            logger.info("IGDB authentication successful")
            return True

        except RequestException as e:
            logger.error(f"IGDB authentication failed: {e}")
            raise IgdbAuthError(f"Authentication failed: {e}") from e

    def is_authenticated(self) -> bool:
        """Check if we have a valid token (or are in proxy mode)"""
        if self._proxy_mode:
            return True  # Proxy handles auth server-side
        return self._token is not None and not self._token.is_expired()

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid token, refreshing if needed"""
        if self._proxy_mode:
            return  # Proxy handles auth server-side
        if not self._token or self._token.is_expired():
            self.authenticate_sync()

    def _rate_limit(self, status_callback: ProgressCallback = None) -> None:
        """Enforce rate limiting (4 requests/second) with display

        Args:
            status_callback: Optional callback for progress updates
        """
        now = time.time()

        # Remove old timestamps
        self._request_times = [
            t for t in self._request_times
            if now - t < RATE_LIMIT_PERIOD
        ]

        # Wait if at limit
        if len(self._request_times) >= self._rate_limit_requests:
            oldest = self._request_times[0]
            sleep_time = RATE_LIMIT_PERIOD - (now - oldest)
            if sleep_time > 0.05:  # Only report if waiting more than 50ms
                if status_callback:
                    status_callback(f"IGDB rate limit - waiting {sleep_time:.2f}s...", -1, -1)
                logger.debug(f"Rate limit: waiting {sleep_time:.2f}s")
                # Use event.wait() instead of time.sleep() so cancellation
                # can interrupt the wait immediately
                if self._cancel_event.wait(sleep_time):
                    raise IgdbCancelledError("IGDB operation cancelled")

        self._request_times.append(time.time())

    def _request(
        self,
        endpoint: str,
        query: str,
        status_callback: ProgressCallback = None,
        retry_count: int = 0
    ) -> Any:
        """Make an API request with retry on rate limit

        Args:
            endpoint: API endpoint (e.g., "games", "external_games")
            query: Apicalypse query string
            status_callback: Optional callback for progress updates
            retry_count: Current retry attempt

        Returns:
            JSON response data
        """
        # Check cancel before making network request
        if self._cancel_event.is_set():
            raise IgdbCancelledError("IGDB operation cancelled")

        # Circuit breaker: skip request if in backoff period
        if self._circuit_open_until > 0:
            remaining = self._circuit_open_until - time.time()
            if remaining > 0:
                raise IgdbRateLimitError(
                    f"IGDB circuit breaker open, {int(remaining)}s remaining"
                )
            # Breaker expired — reset for next trip
            self._circuit_open_until = 0

        self._rate_limit(status_callback)

        if self._proxy_mode:
            return self._request_via_proxy(endpoint, query, status_callback, retry_count)
        else:
            return self._request_direct(endpoint, query, status_callback, retry_count)

    def _request_via_proxy(
        self,
        endpoint: str,
        query: str,
        status_callback: ProgressCallback = None,
        retry_count: int = 0,
    ) -> Any:
        """Make request through the luducat IGDB proxy"""
        from luducat.plugins.sdk.proxy import build_proxy_headers

        url = f"{self._proxy_url}/igdb/v4/{endpoint}"
        headers = build_proxy_headers("igdb", endpoint, query)

        try:
            response = self._http.post(url, headers=headers, data=query)

            if response.status_code == 429:
                # Trip circuit breaker immediately — no retries
                self._circuit_open_until = time.time() + self._circuit_backoff
                duration = int(self._circuit_backoff)
                logger.warning(
                    f"Proxy rate limit 429, circuit breaker open for {duration}s"
                )
                if status_callback:
                    status_callback(
                        f"IGDB proxy rate limited — pausing {duration // 60}min",
                        -1, -1,
                    )
                # Escalate for next trip (double, cap at max)
                self._circuit_backoff = min(
                    self._circuit_backoff * 2, CIRCUIT_BREAKER_MAX,
                )
                raise IgdbRateLimitError(
                    f"Proxy rate limit 429, backing off {duration}s"
                )

            if response.status_code in (401, 403):
                raise IgdbAuthError(
                    "IGDB proxy rejected the request. "
                    "You can provide your own Twitch credentials in IGDB plugin settings "
                    "to bypass the proxy."
                )

            # Retry on server errors (500, 502, 503, 504)
            if response.status_code >= 500:
                if retry_count == 0:
                    logger.debug(
                        f"IGDB {response.status_code} response for {endpoint}:\n"
                        f"{response.text[:1000]}"
                    )
                    logger.debug(f"Query was: {query[:500]}")
                if retry_count < MAX_RATE_LIMIT_RETRIES:
                    wait_time = RATE_LIMIT_RETRY_DELAY * (retry_count + 1)
                    logger.warning(
                        f"Proxy server error {response.status_code}, "
                        f"waiting {wait_time}s before retry {retry_count + 1}"
                    )
                    if self._cancel_event.wait(wait_time):
                        raise IgdbCancelledError("IGDB operation cancelled")
                    return self._request_via_proxy(endpoint, query, status_callback, retry_count + 1)

            response.raise_for_status()

            # Success — reset circuit breaker escalation
            self._circuit_backoff = CIRCUIT_BREAKER_INITIAL
            return response.json()

        except RequestException as e:
            logger.error(f"IGDB proxy request failed: {e}")
            raise IgdbApiError(f"Proxy request failed: {e}") from e

    def _request_direct(
        self,
        endpoint: str,
        query: str,
        status_callback: ProgressCallback = None,
        retry_count: int = 0,
    ) -> Any:
        """Make direct request to IGDB API (BYOK mode)"""
        self._ensure_authenticated()

        url = f"{IGDB_API_BASE}/{endpoint}"
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._token.access_token}",
            "Accept": "application/json"
        }

        try:
            response = self._http.post(url, headers=headers, data=query)

            if response.status_code == 429:
                # Rate limit hit - wait and retry
                if retry_count < MAX_RATE_LIMIT_RETRIES:
                    wait_time = RATE_LIMIT_RETRY_DELAY * (retry_count + 1)
                    if status_callback:
                        status_callback(
                            f"IGDB rate limit exceeded - retrying in {wait_time:.1f}s...",
                            -1, -1
                        )
                    logger.warning(f"Rate limit 429, waiting {wait_time}s before retry {retry_count + 1}")
                    # Use event.wait() instead of time.sleep() so cancellation
                    # can interrupt the wait immediately
                    if self._cancel_event.wait(wait_time):
                        raise IgdbCancelledError("IGDB operation cancelled")
                    return self._request_direct(endpoint, query, status_callback, retry_count + 1)
                else:
                    raise IgdbRateLimitError("Rate limit exceeded after max retries")

            if response.status_code == 401:
                # Token expired or invalid - clear cache and get fresh token
                logger.info("IGDB token rejected (401), refreshing authentication...")
                self._token = None
                self._clear_token_cache()  # Force fresh authentication
                self._ensure_authenticated()
                headers["Authorization"] = f"Bearer {self._token.access_token}"
                response = self._http.post(url, headers=headers, data=query)

                # If still 401 after fresh token, credentials are likely invalid
                if response.status_code == 401:
                    raise IgdbAuthError(
                        "IGDB authentication failed after token refresh. "
                        "Please verify your Twitch Client ID and Client Secret are correct."
                    )

            response.raise_for_status()
            return response.json()

        except RequestException as e:
            logger.error(f"IGDB API request failed: {e}")
            raise IgdbApiError(f"API request failed: {e}") from e

    # =========================================================================
    # COMPREHENSIVE FETCH METHODS
    # =========================================================================

    def fetch_full_game_data(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch comprehensive IGDB data for a game (multiple API calls)

        Fetches all data needed to populate the normalized database schema:
        - Main game data with expanded relationships
        - Release dates with platform details
        - Covers, screenshots, artworks
        - Videos

        Args:
            igdb_id: IGDB game ID
            status_callback: Optional callback for progress updates

        Returns:
            Complete game data dict or None
        """
        if status_callback:
            status_callback(f"Fetching IGDB game {igdb_id}...", -1, -1)

        game_data: Dict[str, Any] = {}

        # 1. Main game data with expanded fields
        main_data = self._fetch_game_main(igdb_id, status_callback)
        if not main_data:
            return None
        game_data.update(main_data)

        # 2. Release dates with platform details
        game_data["release_dates"] = self._fetch_release_dates(igdb_id, status_callback)

        # 3. Covers
        game_data["covers"] = self._fetch_covers(igdb_id, status_callback)

        # 4. Screenshots
        game_data["screenshots"] = self._fetch_screenshots(igdb_id, status_callback)

        # 5. Artworks
        game_data["artworks"] = self._fetch_artworks(igdb_id, status_callback)

        # 6. Videos
        game_data["videos"] = self._fetch_videos(igdb_id, status_callback)

        # Process all images to add full URLs
        self._process_images(game_data)

        return game_data

    def _fetch_game_main(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch main game data with expanded relationships"""
        query = f"""
            fields
                id, name, slug, url, summary, storyline, first_release_date, checksum,
                category, status,
                rating, rating_count, total_rating, total_rating_count,
                aggregated_rating, aggregated_rating_count,
                franchises.*, collections.*, game_modes.*, genres.*, keywords.*,
                themes.*, platforms.*, player_perspectives.*,
                involved_companies.*, involved_companies.company.*,
                external_games.*, websites.*, age_ratings.*;
            where id = {igdb_id};
            limit 1;
        """

        try:
            results = self._request("games", query, status_callback)
            if results and len(results) > 0:
                return results[0]
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch main game data for {igdb_id}: {e}")

        return None

    def _fetch_release_dates(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Fetch release dates with full platform details"""
        query = f"""
            fields *, platform.*;
            where game = {igdb_id};
            limit 200;
        """

        try:
            return self._request("release_dates", query, status_callback)
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch release dates for {igdb_id}: {e}")
            return []

    def _fetch_covers(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Fetch cover images"""
        query = f"""
            fields *;
            where game = {igdb_id};
            limit 10;
        """

        try:
            return self._request("covers", query, status_callback)
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch covers for {igdb_id}: {e}")
            return []

    def _fetch_screenshots(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Fetch screenshots"""
        query = f"""
            fields *;
            where game = {igdb_id};
            limit 50;
        """

        try:
            return self._request("screenshots", query, status_callback)
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch screenshots for {igdb_id}: {e}")
            return []

    def _fetch_artworks(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Fetch artworks"""
        query = f"""
            fields *;
            where game = {igdb_id};
            limit 50;
        """

        try:
            return self._request("artworks", query, status_callback)
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch artworks for {igdb_id}: {e}")
            return []

    def _fetch_videos(
        self,
        igdb_id: int,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Fetch videos (YouTube IDs)"""
        query = f"""
            fields *;
            where game = {igdb_id};
            limit 20;
        """

        try:
            return self._request("game_videos", query, status_callback)
        except IgdbApiError as e:
            logger.warning(f"Failed to fetch videos for {igdb_id}: {e}")
            return []

    def _process_images(self, game_data: Dict[str, Any]) -> None:
        """Process all images to add full URLs with https://

        Modifies game_data in place.
        """
        # Process covers - use cover_big_2x for best quality
        for cover in game_data.get("covers", []):
            if "image_id" in cover:
                image_id = cover["image_id"]
                # Covers use .png extension
                cover["full_url"] = f"{IMAGE_BASE}/t_cover_big_2x/{image_id}.png"

        # Process screenshots - use 1080p
        for screenshot in game_data.get("screenshots", []):
            if "image_id" in screenshot:
                image_id = screenshot["image_id"]
                screenshot["full_url"] = f"{IMAGE_BASE}/t_1080p/{image_id}.jpg"

        # Process artworks - use 1080p
        for artwork in game_data.get("artworks", []):
            if "image_id" in artwork:
                image_id = artwork["image_id"]
                artwork["full_url"] = f"{IMAGE_BASE}/t_1080p/{image_id}.jpg"

        # Fix any protocol-relative URLs in existing url fields
        for key in ["covers", "screenshots", "artworks"]:
            for item in game_data.get(key, []):
                if "url" in item and item["url"]:
                    item["url"] = fix_url_protocol(item["url"])

    # =========================================================================
    # LOOKUP METHODS
    # =========================================================================

    def lookup_by_store_id(
        self,
        store_name: str,
        store_id: str,
        status_callback: ProgressCallback = None
    ) -> Optional[int]:
        """Look up IGDB game ID by store ID

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_id: Store's app ID
            status_callback: Optional callback for progress updates

        Returns:
            IGDB game ID or None if not found
        """
        category = STORE_CATEGORIES.get(store_name.lower())
        if category is None:
            logger.warning(f"Unknown store: {store_name}")
            return None

        query = f"""
            fields game;
            where category = {category} & uid = "{store_id}";
            limit 1;
        """

        try:
            results = self._request("external_games", query, status_callback)
            if results and len(results) > 0:
                return results[0].get("game")
        except IgdbRateLimitError:
            raise  # Propagate circuit breaker to stop the batch
        except IgdbApiError as e:
            logger.warning(f"Store ID lookup failed for {store_name}:{store_id}: {e}")

        return None

    def lookup_store_ids_batch(
        self,
        store_name: str,
        store_ids: List[str],
        batch_size: int = 25,
        status_callback: ProgressCallback = None
    ) -> Dict[str, int]:
        """Look up multiple store IDs in batches

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_ids: List of store app IDs
            batch_size: Number of IDs per request
            status_callback: Optional callback for progress updates

        Returns:
            Dict mapping store_id -> igdb_id for found matches
        """
        category = STORE_CATEGORIES.get(store_name.lower())
        if category is None:
            logger.warning(f"Unknown store: {store_name}")
            return {}

        matches = {}
        total = len(store_ids)

        for i in range(0, total, batch_size):
            batch = store_ids[i:i + batch_size]

            if status_callback:
                status_callback(
                    f"Looking up IGDB IDs ({i}/{total})...",
                    i, total
                )

            # Use Apicalypse tuple syntax for multi-value matching
            uid_list = ",".join(f'"{uid}"' for uid in batch)

            query = f"""
                fields game, uid;
                where category = {category} & uid = ({uid_list});
                limit 500;
            """

            try:
                results = self._request("external_games", query, status_callback)
                for result in results:
                    uid = result.get("uid")
                    game_id = result.get("game")
                    if uid and game_id:
                        matches[uid] = game_id
            except IgdbRateLimitError:
                raise  # Propagate circuit breaker to stop the batch
            except IgdbApiError as e:
                logger.warning(f"Batch store ID lookup failed: {e}")

        return matches

    # =========================================================================
    # SEARCH METHODS
    # =========================================================================

    def search_games(
        self,
        title: str,
        limit: int = 10,
        platform_pc_only: bool = True,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Search for games by title

        Args:
            title: Game title to search
            limit: Maximum results
            platform_pc_only: Only return PC games
            status_callback: Optional callback for progress updates

        Returns:
            List of game dicts with id, name, cover, first_release_date
        """
        # Strip TM/copyright symbols and escape quotes
        safe_title = strip_trademark_symbols(title)
        safe_title = safe_title.replace('"', '\\"')

        if platform_pc_only:
            # PC platforms: Windows (6), DOS (13), Linux (3), Mac (14)
            query = f"""
                search "{safe_title}";
                fields id, name, slug, cover.image_id, first_release_date, platforms.name;
                where platforms = (6, 13, 3, 14);
                limit {limit};
            """
        else:
            query = f"""
                search "{safe_title}";
                fields id, name, slug, cover.image_id, first_release_date, platforms.name;
                limit {limit};
            """

        try:
            return self._request("games", query, status_callback)
        except IgdbRateLimitError:
            raise  # Propagate circuit breaker to stop the batch
        except IgdbApiError as e:
            logger.warning(f"Game search failed for '{title}': {e}")
            return []

    def lookup_by_slug(
        self,
        slug: str,
        status_callback: ProgressCallback = None
    ) -> Optional[int]:
        """Look up IGDB game ID by slug

        Args:
            slug: IGDB slug (e.g., "helldivers-2")
            status_callback: Optional callback for progress updates

        Returns:
            IGDB game ID or None if not found
        """
        # Ensure slug is clean (no TM symbols that might have slipped through)
        clean_slug = slugify(strip_trademark_symbols(slug)) if slug else slug

        query = f"""
            fields id, name;
            where slug = "{clean_slug}";
            limit 1;
        """

        try:
            results = self._request("games", query, status_callback)
            if results and len(results) > 0:
                logger.debug(f"Found game via slug '{clean_slug}': {results[0].get('name')}")
                return results[0].get("id")
        except IgdbRateLimitError:
            raise  # Propagate circuit breaker to stop the batch
        except IgdbApiError as e:
            logger.warning(f"Slug lookup failed for '{clean_slug}': {e}")

        return None

    def search_game_by_title(
        self,
        title: str,
        status_callback: ProgressCallback = None
    ) -> Optional[int]:
        """Search for a game by title and return best match

        Tries multiple strategies:
        1. Skip non-game content (wallpapers, toolkits, etc.)
        2. Slug lookup (slugified normalized title)
        3. Search API with normalized title + PC platform filter
        4. Search API with normalized title, no platform filter
        5. Slug lookup with literal title (preserves punctuation like colons)
        6. Search API with literal title (for games like "Death end re;Quest")

        Args:
            title: Game title (will be normalized before search)
            status_callback: Optional callback for progress updates

        Returns:
            IGDB game ID of best match or None
        """
        # Strategy 1: Skip non-game content
        if should_skip_title(title):
            logger.debug(f"Skipping non-game title: '{title}'")
            return None

        # Prepare titles for different strategies
        # Normalized: lowercase, suffixes stripped, punctuation removed
        search_title = normalize_title(title)
        # Literal: original casing, only TM/copyright stripped, punctuation preserved
        literal_title = strip_trademark_symbols(title)

        if search_title != title.lower():
            logger.debug(f"IGDB title normalized: '{title}' -> '{search_title}'")

        # Strategy 2: Try slug lookup with normalized title
        slug = slugify(search_title)
        result = self.lookup_by_slug(slug, status_callback)
        if result:
            return result

        # Strategy 3: Search API with PC platform filter (normalized title)
        results = self.search_games(
            search_title,
            limit=5,
            platform_pc_only=True,
            status_callback=status_callback
        )

        if results:
            # Try exact match first (case-insensitive)
            for game in results:
                game_name = game.get("name", "")
                if normalize_title(game_name) == search_title:
                    return game.get("id")
            # Fall back to first result
            return results[0].get("id")

        # Strategy 4: Search without platform filter (catches console-first releases)
        logger.debug(f"No PC results for '{search_title}', trying without platform filter")
        results = self.search_games(
            search_title,
            limit=5,
            platform_pc_only=False,
            status_callback=status_callback
        )

        if results:
            for game in results:
                game_name = game.get("name", "")
                if normalize_title(game_name) == search_title:
                    return game.get("id")
            return results[0].get("id")

        # Strategy 5: Try slug with literal title (preserves colons, hyphens)
        # For games like "Death end re;Quest", "Borderlands: The Pre-Sequel"
        if literal_title.lower() != search_title:
            literal_slug = slugify(literal_title)
            if literal_slug != slug:
                logger.debug(f"Trying literal slug: '{literal_slug}'")
                result = self.lookup_by_slug(literal_slug, status_callback)
                if result:
                    return result

            # Strategy 6: Search with literal title (TM stripped, suffixes stripped, punctuation preserved)
            literal_title_stripped = strip_patterns(strip_trademark_symbols(literal_title))
            logger.debug(f"Trying literal title search: '{literal_title_stripped}'")
            results = self.search_games(
                literal_title_stripped,
                limit=5,
                platform_pc_only=False,
                status_callback=status_callback
            )

            if results:
                # Check for exact match against what we searched
                stripped_lower = literal_title_stripped.lower()
                for game in results:
                    game_name = game.get("name", "")
                    if game_name.lower() == stripped_lower:
                        return game.get("id")
                return results[0].get("id")

        return None

    # =========================================================================
    # BATCH METHODS
    # =========================================================================

    def get_games_batch(
        self,
        igdb_ids: List[int],
        batch_size: int = 50,
        status_callback: ProgressCallback = None
    ) -> List[Dict[str, Any]]:
        """Get multiple games in batches (basic data only)

        For full data including media, use fetch_full_game_data() per game.

        Args:
            igdb_ids: List of IGDB game IDs
            batch_size: Number of games per request (max 500)
            status_callback: Optional callback for progress updates

        Returns:
            List of game dicts
        """
        all_results = []
        total = len(igdb_ids)

        for i in range(0, total, batch_size):
            batch = igdb_ids[i:i + batch_size]
            ids_str = ",".join(str(id) for id in batch)

            if status_callback:
                status_callback(
                    f"Fetching game metadata ({i}/{total})...",
                    i, total
                )

            query = f"""
                fields
                    id, name, slug, url, summary, storyline,
                    first_release_date, checksum, category, status,
                    rating, rating_count, aggregated_rating, aggregated_rating_count,
                    total_rating, total_rating_count,
                    cover.image_id,
                    genres.*, themes.*, keywords.*,
                    franchises.*, collections.*, game_modes.*,
                    platforms.*, player_perspectives.*,
                    involved_companies.*, involved_companies.company.*;
                where id = ({ids_str});
                limit {len(batch)};
            """

            try:
                results = self._request("games", query, status_callback)
                all_results.extend(results)
            except IgdbApiError as e:
                logger.warning(f"Batch get games failed: {e}")

        return all_results

    def cancel(self) -> None:
        """Signal cancellation to interrupt any active sleeps"""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Reset cancellation state for reuse"""
        self._cancel_event.clear()

    def close(self) -> None:
        """Close the API client"""
        self._cancel_event.set()  # Wake any sleeping waits
        # Session lifecycle managed by NetworkManager


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def fix_url_protocol(url: Optional[str]) -> Optional[str]:
    """Ensure URL has https:// protocol

    IGDB sometimes returns protocol-relative URLs like:
    //images.igdb.com/igdb/image/upload/t_thumb/co74hl.jpg

    Args:
        url: URL that may be missing protocol

    Returns:
        URL with https:// protocol or None
    """
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return "https://" + url
    return url


def build_cover_url(image_id: str, size: str = "cover_big_2x") -> str:
    """Build full cover URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (cover_small, cover_big, cover_big_2x)

    Returns:
        Full URL to cover image
    """
    return f"{IMAGE_BASE}/t_{size}/{image_id}.png"


def build_screenshot_url(image_id: str, size: str = "1080p") -> str:
    """Build full screenshot URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (screenshot_med, screenshot_big, screenshot_huge, 720p, 1080p)

    Returns:
        Full URL to screenshot image
    """
    return f"{IMAGE_BASE}/t_{size}/{image_id}.jpg"


def build_artwork_url(image_id: str, size: str = "1080p") -> str:
    """Build full artwork URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (720p, 1080p)

    Returns:
        Full URL to artwork image
    """
    return f"{IMAGE_BASE}/t_{size}/{image_id}.jpg"


def slugify(title: str) -> str:
    """Convert a game title to IGDB-style slug.

    IGDB slugs are lowercase, hyphen-separated, alphanumeric only.
    Example: "Helldivers 2" -> "helldivers-2"
    """
    # Lowercase
    slug = title.lower()
    # Replace common separators with hyphen
    slug = re.sub(r'[\s:]+', '-', slug)
    # Remove non-alphanumeric except hyphens
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    # Collapse multiple hyphens
    slug = re.sub(r'-+', '-', slug)
    # Strip leading/trailing hyphens
    return slug.strip('-')


def should_skip_title(title: str) -> bool:
    """Check if title represents a non-game entry that should be skipped.

    These are extras, tools, or other non-game content.
    """
    title_lower = title.lower()

    # Patterns that indicate non-game content (at end of title)
    skip_patterns = [
        r"\s+wallpapers?$",
        r"\s+creator\s+kit$",
        r"\s+(digital\s+)?goodies?\s+pack$",
        r"\s+toolkit$",
        r"\s+modkit(\s+\w+)?$",
        r"\s+(pregame\s+)?editor$",
        r"\s+resource\s+archive$",
        r"\s+art\s*book$",
        r"\s+soundtrack$",
        r"\s+ost$",
        r"\s+original\s+soundtrack$",
        r"\s+bonus\s+content$",
        r"\s+digital\s+comics?$",
        r"\s+content$",  # "death stranding content"
        r"\s+(soundtrack\s+and\s+)?digital\s+goods\s+bundle$",
    ]

    for pattern in skip_patterns:
        if re.search(pattern, title_lower):
            return True

    return False


def strip_trademark_symbols(title: str) -> str:
    """Remove TM, (R), (C) and similar symbols from title."""
    # Remove trademark/copyright symbols
    title = re.sub(r'[™®©]', '', title)
    title = re.sub(r'\s*\(tm\)\s*', ' ', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*\(r\)\s*', ' ', title, flags=re.IGNORECASE)
    title = re.sub(r'\s*\(c\)\s*', ' ', title, flags=re.IGNORECASE)
    # Collapse multiple spaces
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

def strip_patterns(title: str) -> str:
    """Remove patterns from title that prevent IGDB matching.

    Does NOT lowercase or remove punctuation - preserves colons, hyphens, etc.
    """
    # Cut everything after pipe (localized titles)
    # "Raiden V: Director's Cut | 雷電 V Director's Cut" -> "Raiden V: Director's Cut"
    title = re.sub(r"\s*\|.*$", "", title)

    # Remove year in parentheses at end: (1993), (1998)
    title = re.sub(r"\s*\(\d{4}\)$", "", title)

    # Remove (Legacy) at end
    title = re.sub(r"\s*\(legacy\)$", "", title, flags=re.IGNORECASE)

    # Remove (All Ages Version) - common for visual novels
    title = re.sub(r"\s*\(all\s+ages(\s+version)?\)$", "", title, flags=re.IGNORECASE)
    # Remove common edition suffixes (with various separators)
    edition_words = (
        r"goty|game\s+of\s+the\s+year|gold|premium|deluxe|ultimate|"
        r"definitive|enhanced|remaster(?:ed)?|anniversary|collector'?s?|"
        r"complete|special|limited|legacy|classic|german|adult|steam|"
        r"enchanted|singleplayer|oneclick|blüeberry|10000th\s+anniversary|"
        r"steam\s+special"
    )
    patterns = [
        rf"\s*[-:]\s*({edition_words})\s*(edition)?$",
        rf"\s+({edition_words})\s+edition$",
        r"\s*\([^)]*edition\)$",
        r"\s*\[[^\]]*edition\]$",
    ]
    for pattern in patterns:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Remove platform suffixes
    title = re.sub(r"\s*[-:]\s*(pc|windows|linux|mac|steam)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+pc\s+edition$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+mac/?linux$", "", title, flags=re.IGNORECASE)

    # Remove "Complete Pack" suffix (with optional trailing colon)
    title = re.sub(r"\s+complete\s+pack:?$", "", title, flags=re.IGNORECASE)

    # Remove "Public Beta Client" (with optional separator)
    title = re.sub(r"\s*[-:]\s*public\s+beta\s+client$", "", title, flags=re.IGNORECASE)

    # Remove "goodies collection" / "legacy collection" (not games, extras bundles)
    title = re.sub(r"\s+(legacy\s+)?collection$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+goodies\s+collection$", "", title, flags=re.IGNORECASE)

    # Remove year suffixes (1980-2029 range - covers retro games but not futuristic years like 2077)
    # Strip BEFORE classic/expansion so "constructor classic 1997" -> "constructor classic" -> "constructor"
    title = re.sub(r"\s+(19[89]\d|20[0-2]\d)$", "", title, flags=re.IGNORECASE)

    # Remove classic/expansion/dlc suffixes
    title = re.sub(r"\s+(classic|expansion|dlc)$", "", title, flags=re.IGNORECASE)

    # Remove beta/retired/test branch/alpha version suffixes
    title = re.sub(r"\s+beta(\s+obsolete)?$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+retired$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+test\s+branch$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+alpha\s+version$", "", title, flags=re.IGNORECASE)

    # Remove demo/prologue/soundtrack/artbook suffixes
    title = re.sub(r"\s+(demo|prologue|artbook|soundtrack)$", "", title, flags=re.IGNORECASE)

    # Remove version suffixes (cd version, fdd version, directors cut, gog cut)
    title = re.sub(r"\s+(cd|fdd)\s+version$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[:\s]+director'?s?\s+cut$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+gog\s+cut$", "", title, flags=re.IGNORECASE)

    # Remove season/chapter suffixes
    title = re.sub(r"\s+season\s+(\d+|one|two|three)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+chapter\s+\d+.*$", "", title, flags=re.IGNORECASE)

    # Remove trailing "game" (e.g., "Alien Breed Tower Assault game")
    title = re.sub(r"\s+game$", "", title, flags=re.IGNORECASE)

    # Clean up any trailing punctuation left over from stripping
    title = re.sub(r"[\s:,\-]+$", "", title)

    return title.strip()

def normalize_title(title: str) -> str:
    """Normalize a game title for matching

    Removes common suffixes, normalizes punctuation, etc.
    """
    # Lowercase
    title = title.lower()

    # Remove TM/copyright symbols
    title = strip_trademark_symbols(title)

    # strips patterns from string
    title = strip_patterns(title)

    # Normalize punctuation
    title = re.sub(r"[:'\"!?.,;]", "", title)
    title = re.sub(r"\s+", " ", title)

    return title.strip()


def get_pc_release_date(release_dates: List[Dict[str, Any]]) -> Optional[int]:
    """Get oldest release date for DOS or PC (Microsoft Windows)

    Platform IDs:
    - 6 = PC (Microsoft Windows)
    - 13 = DOS

    NOT included: PC-9800 Series (149), Linux (3), Mac (14)

    Args:
        release_dates: List of release date dicts from IGDB API

    Returns:
        Oldest Unix timestamp for PC/DOS release or None
    """
    pc_dates = []
    for rd in release_dates:
        platform = rd.get("platform", {})
        # Platform can be an int ID or a dict with 'id'
        if isinstance(platform, dict):
            platform_id = platform.get("id")
        else:
            platform_id = platform

        # Only DOS (13) or PC Windows (6)
        if platform_id in [PLATFORM_PC_WINDOWS, PLATFORM_DOS]:
            date = rd.get("date")
            if date:
                pc_dates.append(date)

    return min(pc_dates) if pc_dates else None
