# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# family_api.py

"""Steam Family Sharing API client

Provides access to Steam's IFamilyGroupsService API for retrieving
family group information and shared library games.

Requires browser session cookies for authentication.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class FamilyMember:
    """A member of a Steam Family group"""
    steamid: str
    role: int  # 1 = adult, 2 = child (unconfirmed)
    time_joined: int
    cooldown_seconds_remaining: int = 0
    nickname: Optional[str] = None  # Resolved via Steam profile API


@dataclass
class FamilyGroup:
    """Steam Family group information"""
    groupid: str
    name: str
    members: List[FamilyMember] = field(default_factory=list)
    free_spots: int = 0

    def get_member_name(self, steamid: str) -> str:
        """Get display name for a member by steamid"""
        for member in self.members:
            if member.steamid == steamid:
                return member.nickname or steamid
        return steamid


@dataclass
class SharedApp:
    """A game available via family sharing"""
    appid: int
    name: str
    owner_steamids: List[str] = field(default_factory=list)
    capsule_filename: Optional[str] = None
    img_icon_hash: Optional[str] = None
    exclude_reason: int = 0
    rt_time_acquired: int = 0
    rt_last_played: int = 0
    rt_playtime: int = 0
    app_type: int = 1

    @property
    def primary_owner(self) -> Optional[str]:
        """Get the primary owner steamid (first in list)"""
        return self.owner_steamids[0] if self.owner_steamids else None


# =============================================================================
# API Client
# =============================================================================

class SteamFamilyAPI:
    """Steam Family Sharing API client using browser session

    Usage:
        api = SteamFamilyAPI(steamid="76561198055778112")

        # Load cookies from browser
        if api.load_cookies_from_browser():
            # Get access token
            if api.get_access_token():
                # Get family group
                family_group = api.get_family_group()
                # Get shared apps
                shared_apps = api.get_shared_library_apps()
    """

    API_BASE = "https://api.steampowered.com"
    STORE_BASE = "https://store.steampowered.com"

    def __init__(self, steamid: str, http_client=None):
        """Initialize the API client

        Args:
            steamid: Steam64 ID of the user
            http_client: PluginHttpClient for all HTTP requests
        """
        self.steamid = steamid
        self._http = http_client
        self.access_token: Optional[str] = None
        self.family_groupid: Optional[str] = None

    def load_cookies_from_browser(self) -> bool:
        """Load Steam session cookies from system browser

        Uses centralized BrowserCookieManager to respect user's browser preference.
        Loads full cookie objects to preserve all attributes (domain, path, secure, etc.)

        Returns:
            True if session cookies were loaded successfully
        """
        try:
            from luducat.plugins.sdk.cookies import get_browser_cookie_manager

            manager = get_browser_cookie_manager()
            cookie_jar, browser_name = manager.get_cookie_jar_for_domain(
                'steampowered.com',
                required_cookies=None  # We'll check after loading
            )

            if not cookie_jar:
                logger.warning("No Steam session found in any browser")
                return False

            # Load full cookie objects to preserve all attributes
            sess = self._http.session
            cookie_names = []
            for cookie in cookie_jar:
                sess.cookies.set_cookie(cookie)
                cookie_names.append(cookie.name)

            if not cookie_names:
                logger.warning("No Steam cookies found")
                return False

            logger.debug(f"Found cookies from {browser_name}: {cookie_names}")

            # Check for critical cookies — steamLoginSecure is the actual
            # auth cookie; sessionid alone is NOT sufficient for endpoints
            # like pointssummary that return webapi_token.
            critical = ['sessionid', 'steamLoginSecure', 'browserid']
            missing = [c for c in critical if c not in cookie_names]
            if missing:
                logger.warning(f"Missing critical Steam cookies: {missing}")

            if 'steamLoginSecure' not in cookie_names:
                logger.warning(
                    "steamLoginSecure cookie not found — session is not "
                    "authenticated (cookie may be expired or decryption "
                    "failed from background thread)"
                )
                return False

            logger.info(f"Loaded Steam session from {browser_name}")
            return True

        except Exception as e:
            logger.warning(f"Failed to load browser cookies: {e}")
            return False

    def _load_cookies_fallback(self) -> bool:
        """Fallback cookie loading - delegates to load_cookies_from_browser

        This method is kept for API compatibility but now uses the same
        centralized BrowserCookieManager as load_cookies_from_browser.

        Returns:
            True if session cookies were loaded successfully
        """
        logger.debug("Using fallback cookie detection.")
        return self.load_cookies_from_browser()

    def set_cookies(self, cookies: Dict[str, str]) -> None:
        """Set cookies directly (for stored sessions)

        Args:
            cookies: Dictionary of cookie name -> value
        """
        for name, value in cookies.items():
            self._http.session.cookies.set(name, value, domain='steampowered.com')

    def verify_login(self) -> bool:
        """Verify that we're logged into Steam

        Returns:
            True if session is valid
        """
        try:
            response = self._http.get(
                f"{self.STORE_BASE}/account/",
                allow_redirects=False,
                timeout=10
            )

            if response.status_code == 302:
                logger.debug("Session expired - redirecting to login")
                return False

            if 'login' in response.url.lower():
                logger.debug("Not logged in")
                return False

            if self.steamid in response.text or 'account_name' in response.text:
                logger.debug(f"Verified login for SteamID: {self.steamid}")
                return True

            logger.debug("Could not verify login")
            return False

        except Exception as e:
            logger.error(f"Login verification failed: {e}")
            return False

    def get_access_token(self) -> Optional[str]:
        """Get webapi access token from Steam's pointssummary API

        Uses the undocumented ajaxgetasyncconfig endpoint which returns
        the webapi_token needed for IFamilyGroupsService API calls.

        Returns:
            Access token string or None if request failed
        """
        try:
            # Debug: log cookies with domains to diagnose auth issues
            cookie_info = [(c.name, c.domain) for c in self._http.session.cookies]
            logger.debug(f"Session cookies before request: {cookie_info}")

            response = self._http.get(
                f"{self.STORE_BASE}/pointssummary/ajaxgetasyncconfig",
                allow_redirects=False,
                timeout=10
            )

            logger.debug(f"pointssummary response: status={response.status_code}, body={response.text[:200]}")

            if response.status_code != 200:
                logger.warning(f"pointssummary request failed: {response.status_code}")
                return None

            data = response.json()

            # Handle unexpected response formats
            if isinstance(data, list):
                logger.warning(f"pointssummary returned list instead of dict: {data}")
                return None

            if not isinstance(data, dict):
                logger.warning(f"pointssummary returned unexpected type: {type(data)}")
                return None

            if data.get('success') == 1 and 'data' in data:
                token_data = data['data']
                if isinstance(token_data, dict):
                    token = token_data.get('webapi_token')
                    if token:
                        self.access_token = token
                        logger.info("Got webapi_token from pointssummary")
                        return self.access_token

            logger.warning(f"pointssummary response missing webapi_token: {data}")
            return None

        except Exception as e:
            logger.warning(f"Failed to get access token: {e}")
            return None

    def get_family_group(self) -> Optional[FamilyGroup]:
        """Get the user's family group information

        Returns:
            FamilyGroup object or None if not in a family
        """
        if not self.access_token:
            logger.error("No access token available")
            return None

        try:
            params = {
                'access_token': self.access_token,
                'steamid': self.steamid,
                'include_family_group_response': 'true'
            }

            response = self._http.get(
                f"{self.API_BASE}/IFamilyGroupsService/GetFamilyGroupForUser/v1/",
                params=params,
                timeout=15
            )

            if response.status_code != 200:
                logger.error(f"GetFamilyGroupForUser failed: {response.status_code}")
                return None

            data = response.json()
            resp = data.get('response', {})

            if resp.get('is_not_member_of_any_group', False):
                logger.info("User is not a member of any family group")
                return None

            self.family_groupid = resp.get('family_groupid')
            if not self.family_groupid:
                logger.warning("No family_groupid in response")
                return None

            # Parse family group details
            fg_data = resp.get('family_group', {})
            members = []
            for member_data in fg_data.get('members', []):
                members.append(FamilyMember(
                    steamid=str(member_data.get('steamid', '')),
                    role=member_data.get('role', 0),
                    time_joined=member_data.get('time_joined', 0),
                    cooldown_seconds_remaining=member_data.get('cooldown_seconds_remaining', 0),
                ))

            family_group = FamilyGroup(
                groupid=self.family_groupid,
                name=fg_data.get('name', 'Unknown Family'),
                members=members,
                free_spots=fg_data.get('free_spots', 0),
            )

            logger.info(f"Found family group '{family_group.name}' with {len(members)} members")
            logger.debug(
                "Family group overview: groupid=%s, name=%s, free_spots=%d",
                family_group.groupid, family_group.name, family_group.free_spots,
            )
            for m in members:
                logger.debug(
                    "  Member %s: role=%d, time_joined=%d, cooldown=%d",
                    m.steamid, m.role, m.time_joined, m.cooldown_seconds_remaining,
                )
            return family_group

        except Exception as e:
            logger.error(f"Failed to get family group: {e}")
            return None

    def get_shared_library_apps(
        self,
        include_own: bool = True,
        include_free: bool = False,
        include_excluded: bool = False
    ) -> List[SharedApp]:
        """Get all apps available via family sharing

        Args:
            include_own: Include games owned by the user
            include_free: Include free games
            include_excluded: Include excluded/unavailable games

        Returns:
            List of SharedApp objects
        """
        if not self.family_groupid:
            logger.error("No family_groupid available - call get_family_group first")
            return []

        try:
            logger.debug(
                "GetSharedLibraryApps: groupid=%s, steamid=%s, "
                "include_own=%s, include_free=%s, include_excluded=%s",
                self.family_groupid, self.steamid,
                include_own, include_free, include_excluded,
            )

            params = {
                'access_token': self.access_token,
                'family_groupid': self.family_groupid,
                'steamid': self.steamid,
                'include_own': str(include_own).lower(),
                'include_free': str(include_free).lower(),
                'include_non_games': 'false',
                'include_excluded': str(include_excluded).lower(),
            }

            response = self._http.get(
                f"{self.API_BASE}/IFamilyGroupsService/GetSharedLibraryApps/v1/",
                params=params,
                timeout=30
            )

            if response.status_code != 200:
                logger.error(f"GetSharedLibraryApps failed: {response.status_code}")
                return []

            data = response.json()
            apps_data = data.get('response', {}).get('apps', [])

            shared_apps = []
            for app_data in apps_data:
                # Handle both singular and plural owner fields
                owner_steamids = app_data.get('owner_steamids', [])
                if not owner_steamids and 'owner_steamid' in app_data:
                    owner_steamids = [str(app_data['owner_steamid'])]
                owner_steamids = [str(sid) for sid in owner_steamids]

                shared_apps.append(SharedApp(
                    appid=app_data.get('appid', 0),
                    name=app_data.get('name', 'Unknown'),
                    owner_steamids=owner_steamids,
                    capsule_filename=app_data.get('capsule_filename'),
                    img_icon_hash=app_data.get('img_icon_hash'),
                    exclude_reason=app_data.get('exclude_reason', 0),
                    rt_time_acquired=app_data.get('rt_time_acquired', 0),
                    rt_last_played=app_data.get('rt_last_played', 0),
                    rt_playtime=app_data.get('rt_playtime', 0),
                    app_type=app_data.get('app_type', 1),
                ))

            logger.info(f"Retrieved {len(shared_apps)} shared library apps")
            if shared_apps:
                app_ids = [str(a.appid) for a in shared_apps]
                logger.debug("Shared app IDs: %s", app_ids)
            return shared_apps

        except Exception as e:
            logger.error(f"Failed to get shared library apps: {e}")
            return []

    def get_borrowed_apps(self) -> List[SharedApp]:
        """Get only borrowed apps (not owned by current user)

        Convenience method that filters out games owned by the current user.

        Returns:
            List of SharedApp objects for borrowed games only
        """
        all_apps = self.get_shared_library_apps(include_own=True)

        # Filter to only games NOT owned by current user
        borrowed = [
            app for app in all_apps
            if self.steamid not in app.owner_steamids
        ]

        logger.info(f"Filtered to {len(borrowed)} borrowed apps (excluding owned)")
        return borrowed


def fetch_family_shared_games(
    steamid: str,
    cookies: Optional[Dict[str, str]] = None,
    http_client=None,
) -> Tuple[List[SharedApp], Optional[FamilyGroup], Dict[str, int]]:
    """Convenience function to fetch family shared games

    Args:
        steamid: Steam64 ID of the user
        cookies: Optional pre-loaded cookies dict
        http_client: PluginHttpClient for all HTTP requests

    Returns:
        Tuple of (list of borrowed SharedApp, FamilyGroup or None,
                  license_counts dict mapping app_id str -> owner count)
    """
    logger.debug("fetch_family_shared_games: starting for steamid=%s", steamid)
    api = SteamFamilyAPI(steamid, http_client=http_client)

    # Load cookies
    if cookies:
        logger.debug("fetch_family_shared_games: using provided cookies")
        api.set_cookies(cookies)
    else:
        logger.debug("fetch_family_shared_games: loading cookies from browser")
        if not api.load_cookies_from_browser():
            logger.warning("Could not load browser cookies for family sharing")
            return [], None, {}

    # Get access token
    logger.debug("fetch_family_shared_games: requesting access token")
    if not api.get_access_token():
        logger.warning("Could not get Steam access token for family sharing")
        return [], None, {}
    logger.debug("fetch_family_shared_games: access token obtained")

    # Get family group
    logger.debug("fetch_family_shared_games: querying family group")
    family_group = api.get_family_group()
    if not family_group:
        logger.debug("fetch_family_shared_games: no family group found")
        return [], None, {}

    # Get ALL shared library apps (includes user's own)
    logger.debug("fetch_family_shared_games: querying shared library apps")
    all_apps = api.get_shared_library_apps()

    # Filter to borrowed only (not owned by current user)
    borrowed_apps = [
        app for app in all_apps
        if api.steamid not in app.owner_steamids
    ]

    # Build license counts for ALL shared apps
    license_counts = {str(a.appid): len(a.owner_steamids) for a in all_apps}

    logger.info(
        f"Family sharing: {len(all_apps)} shared library apps, "
        f"{len(borrowed_apps)} borrowed, license counts for {len(license_counts)} apps"
    )
    if borrowed_apps:
        borrowed_ids = [str(a.appid) for a in borrowed_apps]
        logger.debug("Borrowed app IDs: %s", borrowed_ids)

    return borrowed_apps, family_group, license_counts
