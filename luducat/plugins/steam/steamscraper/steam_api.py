# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# steam_api.py

"""
Steam API client.

Rate limit handling:
- On 429 responses, raises RateLimitExceededError (5 min backoff).
- On 403 responses, raises RateLimitExceededError (15 min backoff).
- Every 1000 requests, proactively pauses (5 min) to avoid hitting limits.
The caller (game_service/sync_orchestrator) handles retry, wait, and UI notification.
"""

import time
import logging
from typing import Dict, Any, Optional

from luducat.plugins.sdk.json import json
from luducat.plugins.sdk.network import RequestException, RequestTimeout, Response

from .config import (
    STEAM_API_KEY, STEAM_API_BASE, STEAM_STORE_API,
    RETRY_WAIT_SECONDS, REQUEST_TIMEOUT,
    PROACTIVE_COOLDOWN_REQUESTS, PROACTIVE_COOLDOWN_SECONDS,
    FORBIDDEN_WAIT_SECONDS,
)
from .exceptions import RateLimitExceededError, SteamAPIError, AppNotFoundError

logger = logging.getLogger(__name__)


class SteamAPIClient:
    """Client for interacting with Steam API."""

    def __init__(self, api_key: str = None, http_client=None):
        """Initialize Steam API client.

        Args:
            api_key: Steam API key (required - get from steamcommunity.com/dev/apikey)
            http_client: PluginHttpClient for all HTTP requests
        """
        self.api_key = api_key or STEAM_API_KEY
        if not self.api_key:
            raise ValueError(
                "Steam API key is required. Get one from "
                "https://steamcommunity.com/dev/apikey"
            )
        self._http = http_client
        self._request_count = 0
        self._cooldown_until = 0.0  # monotonic timestamp

    def _pre_request(self) -> None:
        """Check rate limits before making a Steam API request.

        Resets the counter after a cooldown elapses. Raises proactive
        cooldown at PROACTIVE_COOLDOWN_REQUESTS to avoid hitting Steam's
        hard 403 ban.

        Raises:
            RateLimitExceededError: If in cooldown or proactive limit reached
        """
        now = time.monotonic()

        # If we're in a cooldown period, raise with remaining time
        if now < self._cooldown_until:
            remaining = int(self._cooldown_until - now) + 1
            raise RateLimitExceededError(
                f"Steam API cooldown active ({remaining}s remaining)",
                wait_seconds=remaining,
                reason="cooldown",
            )

        # Cooldown elapsed — reset counter
        if self._cooldown_until > 0:
            self._cooldown_until = 0.0
            self._request_count = 0

        self._request_count += 1

        # Proactive pause at threshold
        if self._request_count >= PROACTIVE_COOLDOWN_REQUESTS:
            self._cooldown_until = now + PROACTIVE_COOLDOWN_SECONDS
            logger.warning(
                f"Steam API: proactive cooldown after {self._request_count} requests, "
                f"pausing {PROACTIVE_COOLDOWN_SECONDS // 60} min"
            )
            raise RateLimitExceededError(
                f"Proactive rate limit after {self._request_count} requests",
                wait_seconds=PROACTIVE_COOLDOWN_SECONDS,
                reason="proactive",
            )

    def get_budget_status(self) -> dict:
        """Return current API budget status for interleave decisions.

        Returns:
            Dict with request_count, budget_limit, in_cooldown, cooldown_remaining
        """
        now = time.monotonic()
        in_cooldown = now < self._cooldown_until
        remaining = max(0, int(self._cooldown_until - now)) if in_cooldown else 0
        return {
            "request_count": self._request_count,
            "budget_limit": PROACTIVE_COOLDOWN_REQUESTS,
            "in_cooldown": in_cooldown,
            "cooldown_remaining": remaining,
        }

    def _check_response(self, response: Response) -> None:
        """Check HTTP response for rate limiting status codes.

        Args:
            response: HTTP response object

        Raises:
            RateLimitExceededError: On 429 or 403 responses
        """
        if response.status_code == 429:
            self._cooldown_until = time.monotonic() + RETRY_WAIT_SECONDS
            raise RateLimitExceededError(
                "Steam API rate limit hit (429)",
                wait_seconds=RETRY_WAIT_SECONDS,
                reason="429",
            )

        if response.status_code == 403:
            self._cooldown_until = time.monotonic() + FORBIDDEN_WAIT_SECONDS
            logger.warning(
                f"Steam API 403 Forbidden — likely rate limited, "
                f"backing off {FORBIDDEN_WAIT_SECONDS // 60} min"
            )
            raise RateLimitExceededError(
                "Steam API forbidden (rate limit)",
                wait_seconds=FORBIDDEN_WAIT_SECONDS,
                reason="403",
            )

    def _make_request(self, url: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Make HTTP request with rate limit protection.

        Args:
            url: URL to request
            params: Query parameters

        Returns:
            JSON response as dictionary

        Raises:
            RateLimitExceededError: On 429/403 or proactive cooldown
            SteamAPIError: If API returns an error
        """
        if params is None:
            params = {}

        self._pre_request()

        try:
            response = self._http.get(url, params=params, timeout=REQUEST_TIMEOUT)
            self._check_response(response)
            response.raise_for_status()
            return response.json()

        except RequestTimeout as e:
            logger.error(f"Request timeout for URL: {url}")
            raise SteamAPIError(f"Request timeout: {url}") from e
        except RequestException as e:
            logger.error(f"Request failed: {e}")
            raise SteamAPIError(f"Request failed: {e}") from e
    
    def get_app_details(self, appid: int) -> Dict[str, Any]:
        """Get detailed information about a Steam app.
        
        Args:
            appid: Steam application ID
            
        Returns:
            Dictionary containing app details
            
        Raises:
            AppNotFoundError: If app doesn't exist
            SteamAPIError: If API request fails
        """
        url = f"{STEAM_STORE_API}/appdetails"
        params = {
            'appids': appid,
            'cc': 'us',
            'l': 'english'
        }
        
        logger.info(f"Fetching app details for appid: {appid}")
        data = self._make_request(url, params)
        
        appid_str = str(appid)
        if appid_str not in data:
            raise AppNotFoundError(f"App {appid} not found in API response")
        
        app_data = data[appid_str]
        
        if not app_data.get('success', False):
            raise AppNotFoundError(f"App {appid} not found or unavailable")
        
        return app_data.get('data', {})
    
    def get_app_list(self) -> list:
        """Get list of all Steam apps.
        
        Returns:
            List of dictionaries containing appid and name
            
        Raises:
            SteamAPIError: If API request fails
        """
        url = f"{STEAM_API_BASE}/ISteamApps/GetAppList/v2/"
        
        logger.info("Fetching complete Steam app list")
        data = self._make_request(url)
        
        if 'applist' not in data or 'apps' not in data['applist']:
            raise SteamAPIError("Invalid app list response")
        
        return data['applist']['apps']
    
    def search_app_by_name(self, name: str) -> Optional[int]:
        """Search for an app by name.
        
        Args:
            name: Game name to search for
            
        Returns:
            App ID if found, None otherwise
        """
        try:
            apps = self.get_app_list()
            name_lower = name.lower()
            
            # Exact match first
            for app in apps:
                if app['name'].lower() == name_lower:
                    return app['appid']
            
            # Partial match
            for app in apps:
                if name_lower in app['name'].lower():
                    return app['appid']
            
            return None
            
        except SteamAPIError as e:
            logger.error(f"Failed to search for app '{name}': {e}")
            return None
    
    def resolve_vanity_url(self, vanity_name: str) -> Optional[str]:
        """Resolve a Steam vanity URL name to a Steam64 ID.

        Uses ISteamUser/ResolveVanityURL endpoint.

        Args:
            vanity_name: The vanity URL part (e.g., "gabelogannewell")

        Returns:
            Steam64 ID string on success, None otherwise
        """
        url = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v0001/"
        params = {
            "key": self.api_key,
            "vanityurl": vanity_name,
        }

        try:
            data = self._make_request(url, params)
            response = data.get("response", {})
            if response.get("success") == 1:
                steam_id = response.get("steamid")
                if steam_id:
                    logger.info(f"Resolved vanity '{vanity_name}' -> {steam_id}")
                    return steam_id
            return None
        except RateLimitExceededError:
            raise
        except Exception as e:
            logger.debug(f"Failed to resolve vanity URL '{vanity_name}': {e}")
            return None

    def close(self):
        """Close the HTTP session."""
        if self._http is not None:
            self._http.close()
    
    def get_store_assets(self, appid: int) -> dict:
        """Get store assets metadata from IStoreBrowseService/GetItems API.
        
        This returns the actual asset filenames and paths used by Steam,
        including old formats (portrait.png) and hash-based paths.
        
        Args:
            appid: Steam application ID
            
        Returns:
            Dictionary with asset information:
            {
                'asset_url_format': 'steam/apps/{appid}/${FILENAME}?t=timestamp',
                'header': 'header.jpg',
                'small_capsule': 'capsule_231x87.jpg',
                'main_capsule': 'capsule_616x353.jpg',
                'library_capsule': 'portrait.png' or 'library_600x900.jpg',
                'library_capsule_2x': 'library_600x900_2x.jpg',
                'library_hero': 'library_hero.jpg',
                'library_logo': 'logo.png',
                'community_icon': '{hash}.jpg',
                ...
            }
            
        Returns empty dict if API call fails.
        """
        import urllib.parse
        
        # Construct API request
        request_data = {
            "ids": [{"appid": appid}],
            "context": {"country_code": "US"},
            "data_request": {"include_assets": True}
        }
        
        # URL-encode the JSON
        input_json = urllib.parse.quote(json.dumps(request_data))
        url = f"https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json={input_json}"
        
        self._pre_request()

        try:
            response = self._http.get(url, timeout=REQUEST_TIMEOUT)
            self._check_response(response)
            response.raise_for_status()

            data = response.json()

            # Extract assets from response
            if 'response' in data and 'store_items' in data['response']:
                store_items = data['response']['store_items']
                if store_items and len(store_items) > 0:
                    assets = store_items[0].get('assets', {})
                    logger.info(f"Retrieved {len(assets)} asset fields from IStoreBrowseService for app {appid}")
                    return assets

            logger.warning(f"No assets found in IStoreBrowseService response for app {appid}")
            return {}

        except RateLimitExceededError:
            raise
        except RequestException as e:
            logger.warning(f"Failed to fetch assets from IStoreBrowseService for app {appid}: {e}")
            return {}
    
    def get_steamspy_data(self, appid: int) -> dict:
        """Get app data from SteamSpy API.
        
        SteamSpy provides review counts and user scores that aren't in the official Steam API.
        No authentication required - it's a public API.
        
        Args:
            appid: Steam application ID
            
        Returns:
            Dictionary with SteamSpy data:
            {
                'positive': 12345,      # Positive review count
                'negative': 678,        # Negative review count
                'userscore': 95,        # User score 0-100
                'score_rank': '98%',    # Score rank percentile
                'owners': '1,000,000 .. 2,000,000',
                'average_forever': 180, # Average playtime (minutes)
                'median_forever': 120,
                ...
            }
            
        Returns empty dict if API call fails.
        """
        url = "https://steamspy.com/api.php"
        params = {
            'request': 'appdetails',
            'appid': appid
        }
        
        try:
            response = self._http.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            data = response.json()
            
            if data and data.get('appid') == appid:
                logger.info(f"Retrieved SteamSpy data for app {appid}")
                return data
            else:
                logger.warning(f"No SteamSpy data found for app {appid}")
                return {}
            
        except RequestException as e:
            logger.warning(f"Failed to fetch SteamSpy data for app {appid}: {e}")
            return {}
    
    def get_community_icon_hash(self, appid: int) -> Optional[str]:
        """Get community icon hash for an app using GetOwnedGames API.
        
        The community icon hash is used to build URLs like:
        https://steamcdn-a.akamaihd.net/steamcommunity/public/images/apps/{appid}/{hash}.jpg
        
        This uses a workaround: GetOwnedGames API returns img_icon_url which contains
        the hash when include_appinfo=1 is set.
        
        Args:
            appid: Steam application ID
            
        Returns:
            Icon hash string (e.g., "69f7ebe2735c366c65c0b33dae00e12dc40edbe4") or None
        """
        # We need a steamid to query GetOwnedGames
        # Use a well-known public profile that owns many games
        # SteamID: 76561197960435530 (Robin Walker, Valve employee, public profile)
        steamid = "76561197960435530"
        
        url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
        params = {
            'key': self.api_key,
            'steamid': steamid,
            'include_appinfo': 1,
            'appids_filter[0]': appid,
            'format': 'json'
        }
        
        self._pre_request()

        try:
            response = self._http.get(url, params=params, timeout=REQUEST_TIMEOUT)
            self._check_response(response)
            response.raise_for_status()

            data = response.json()

            # Extract games list
            games = data.get('response', {}).get('games', [])

            if games and len(games) > 0:
                game = games[0]
                icon_hash = game.get('img_icon_url')

                if icon_hash:
                    logger.info(f"Retrieved community icon hash for app {appid}: {icon_hash}")
                    return icon_hash

            logger.warning(f"No community icon hash found for app {appid}")
            return None

        except RateLimitExceededError:
            raise
        except RequestException as e:
            logger.warning(f"Failed to fetch community icon hash for app {appid}: {e}")
            return None
