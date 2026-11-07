# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# api.py

"""PCGamingWiki Cargo API client with rate limiting and circuit breaker

Handles:
- Cargo query construction and execution
- Batch lookups by Steam AppID and GOG ID
- Rate limiting (configurable delay between requests)
- Result parsing (Cargo API response format)
- Circuit breaker for server outages (auto-pause + probe recovery)
"""

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from luducat.plugins.sdk.constants import USER_AGENT_GW
from luducat.plugins.sdk.network import (
    ConnectionError as RequestConnectionError,
)
from luducat.plugins.sdk.network import (
    RequestException,
    Response,
)

from .wikitext import parse_page

logger = logging.getLogger(__name__)

# Type alias for progress callback: (message, current, total)
ProgressCallback = Callable[[str, int, int], None] | None
CancelCheck = Callable[[], bool] | None

# PCGamingWiki API
PCGW_API_URL = "https://www.pcgamingwiki.com/w/api.php"
# Steam AppID -> page redirect endpoint (documented, survives the
# Cargo lockdown)
PCGW_APPID_URL = "https://www.pcgamingwiki.com/api/appid.php"
# Status page (separate infrastructure, stays up when wiki is down)
PCGW_STATUS_URL = "https://status.pcgamingwiki.com"

# Defaults
DEFAULT_RATE_LIMIT_DELAY = 2.1  # seconds between requests (30 req/min limit)
MAX_RESULTS_PER_QUERY = 500
DEFAULT_BATCH_SIZE = 60  # Store IDs per batch query

# Retry settings for transient errors
MAX_ATTEMPTS = 2          # total attempts (initial + retries)
BASE_RETRY_DELAY = 1.0    # seconds (doubles each retry)
MAX_RETRY_DELAY = 8.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Circuit breaker: stop requests when server is down, probe for recovery
BREAKER_THRESHOLD = 3        # consecutive server errors to trip
BREAKER_COOLDOWN = 2 * 3600  # 2 hours in seconds
SERVER_ERROR_CODES = {500, 502, 503, 504}

# Fields to query from Infobox_game
INFOBOX_FIELDS = [
    "Infobox_game._pageID=pageID",
    "Infobox_game._pageName=pageName",
    "Infobox_game.Steam_AppID",
    "Infobox_game.GOGcom_ID",
    "Infobox_game.Cover_URL",
    "Infobox_game.Developers",
    "Infobox_game.Publishers",
    "Infobox_game.Engines",
    "Infobox_game.Released_Windows",
    "Infobox_game.Monetization",
    "Infobox_game.Microtransactions",
    "Infobox_game.Modes",
    "Infobox_game.Genres",
    "Infobox_game.Themes",
    "Infobox_game.Perspectives",
    "Infobox_game.Pacing",
    "Infobox_game.Controls",
    "Infobox_game.Art_styles",
    "Infobox_game.Sports",
    "Infobox_game.Vehicles",
    "Infobox_game.Series",
    "Infobox_game.Available_on",
    "Infobox_game.License",
]

# Fields to query from Multiplayer table
MULTIPLAYER_FIELDS = [
    "Multiplayer.Local",
    "Multiplayer.Local_players",
    "Multiplayer.Local_modes",
    "Multiplayer.LAN",
    "Multiplayer.LAN_players",
    "Multiplayer.LAN_modes",
    "Multiplayer.Online",
    "Multiplayer.Online_players",
    "Multiplayer.Online_modes",
    "Multiplayer.Asynchronous",
    "Multiplayer.Crossplay",
    "Multiplayer.Crossplay_platforms",
]

# Fields to query from Input table (controller support)
# Field names from PCGamingWiki Cargo schema (validated against actual table)
INPUT_FIELDS = [
    "Input.Controller_support",
    "Input.Full_controller_support",
    "Input.Controller_remapping",
    "Input.Controller_sensitivity",
    "Input.Controller_haptic_feedback",  # NOT Controller_haptics
    "Input.Touchscreen",  # NOT Touchscreen_support
    "Input.Key_remapping",
    "Input.Mouse_sensitivity",
    "Input.Mouse_acceleration",
    "Input.Mouse_input_in_menus",
]
# Note: Trackpad_support and Mouse_remapping don't exist in PCGW schema

# Note: The old API table with Metacritic/OpenCritic/IGDB/HLTB IDs was removed from PCGW.
# The current API table contains graphics API fields (Direct3D, OpenGL, Vulkan, etc.)
# which we don't need. External IDs are no longer available via Cargo API.
# Wikipedia/StrategyWiki are in Infobox_game table.


class PcgwApiError(Exception):
    """PCGamingWiki API error"""
    pass


class PcgwApi:
    """PCGamingWiki Cargo API client

    Queries the MediaWiki Cargo extension API for structured game data.
    No authentication required - public API.
    """

    def __init__(self, http_client=None, rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY):
        self._http = http_client
        self._rate_limit_delay = rate_limit_delay
        self._last_request_time = 0.0
        self._headers = {"User-Agent": USER_AGENT_GW}

        # Disable urllib3's internal retries — we handle retries in _cargo_query().
        # The shared session has Retry(total=3) which causes 4 connections × 15s
        # timeout before a ConnectionError surfaces, turning a dead-server check
        # into a 60s hang.
        self._disable_urllib3_retries()

        # Cancel support (matching IGDB API pattern)
        self._cancelled = False

        # Circuit breaker state
        self._consecutive_server_errors = 0
        self._breaker_open_until = 0.0  # Unix timestamp; 0 = closed

    def _disable_urllib3_retries(self) -> None:
        """Mount a no-retry adapter for pcgamingwiki.com on the HTTP session.

        The shared plugin session has urllib3 Retry(total=3) which causes
        4 connections × 15s timeout = 60s before ConnectionError surfaces.
        We handle retries in _cargo_query() so urllib3 retries are redundant.
        """
        if self._http is None:
            return
        try:
            session = self._http.session
            # Get the adapter class from the existing https adapter
            existing = session.get_adapter("https://www.pcgamingwiki.com")
            AdapterClass = type(existing)
            no_retry = AdapterClass(max_retries=0)
            session.mount("https://www.pcgamingwiki.com", no_retry)
            session.mount("https://status.pcgamingwiki.com", no_retry)
        except Exception as e:
            # Non-critical: urllib3 retries just make dead-server checks slow
            logger.debug("Could not mount no-retry adapter: %s", e)

    def cancel(self):
        """Signal cancellation — interrupts batch loops"""
        self._cancelled = True

    def _should_stop(self, cancel_check: CancelCheck) -> bool:
        """True when a batch loop must break: explicit cancel, open
        breaker, or the caller's cancel_check says so."""
        if self._cancelled or self.breaker_open:
            return True
        return bool(cancel_check and cancel_check())

    def reset_cancel(self):
        """Reset cancel flag for reuse"""
        self._cancelled = False

    def check_status_page(self) -> None:
        """Check the PCGW status page and pre-trip the breaker if there's an incident.

        Call this at startup or when switching from offline to online mode.
        If the status page reports an ongoing incident, the breaker is tripped
        immediately so no API calls are wasted on a known-dead server.
        """
        import re

        try:
            response = self._http.get(
                PCGW_STATUS_URL, headers=self._headers, timeout=3
            )
            if response.status_code != 200:
                return

            match = re.search(
                r'class="system-status--description"[^>]*>\s*(.*?)\s*</div>',
                response.text,
                re.DOTALL,
            )
            if match:
                status_text = match.group(1).strip()
                if "incident" in status_text.lower():
                    self._trip_breaker(f"status page: {status_text}")
                else:
                    logger.debug("PCGW status page: %s", status_text)
        except Exception as e:
            logger.debug("PCGW status page check failed: %s", e)

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    # -------------------------------------------------------------------------
    # Circuit Breaker
    # -------------------------------------------------------------------------

    @property
    def breaker_open(self) -> bool:
        """True if the circuit breaker is currently tripped"""
        return self._breaker_open_until > 0 and time.time() < self._breaker_open_until

    def _trip_breaker(self, reason: str) -> None:
        """Trip the circuit breaker, pausing all requests for BREAKER_COOLDOWN"""
        self._breaker_open_until = time.time() + BREAKER_COOLDOWN
        self._consecutive_server_errors = 0
        hours = BREAKER_COOLDOWN / 3600
        logger.warning(
            f"PCGamingWiki: Circuit breaker tripped — {reason}. "
            f"Pausing requests for {hours:.0f} hours."
        )

    def _check_breaker(self) -> bool:
        """Check circuit breaker state; probe if cooldown expired.

        Returns:
            True if breaker is open (caller should skip the request),
            False if closed/recovered (caller should proceed).
        """
        if self._breaker_open_until == 0.0:
            return False  # Closed — proceed normally

        now = time.time()
        if now < self._breaker_open_until:
            return True  # Still open — skip

        # Cooldown expired → half-open: probe to see if server recovered
        logger.info("PCGamingWiki: Circuit breaker cooldown expired, probing server...")
        if self._probe():
            # Server is back
            self._breaker_open_until = 0.0
            self._consecutive_server_errors = 0
            logger.info("PCGamingWiki: Server recovered, resuming requests.")
            return False
        else:
            # Still down — reset cooldown
            self._breaker_open_until = now + BREAKER_COOLDOWN
            logger.warning(
                "PCGamingWiki: Server still unavailable, "
                f"extending pause for {BREAKER_COOLDOWN / 3600:.0f} hours."
            )
            return True

    def _probe(self) -> bool:
        """Check the PCGW status page to see if the service has recovered.

        Uses the external status page (separate infrastructure from the wiki)
        instead of hitting the potentially dead API server. Parses the
        system-status--description div for incident indicators.

        Falls back to a lightweight Cargo API query if the status page
        itself is unreachable.

        Returns True if the service appears operational, False otherwise.
        """
        import re

        # Try status page first (fast, separate infrastructure)
        try:
            response = self._http.get(
                PCGW_STATUS_URL, headers=self._headers, timeout=3
            )
            if response.status_code == 200:
                match = re.search(
                    r'class="system-status--description"[^>]*>\s*(.*?)\s*</div>',
                    response.text,
                    re.DOTALL,
                )
                if match:
                    status_text = match.group(1).strip().lower()
                    if "incident" in status_text:
                        logger.info(
                            "PCGamingWiki status page reports incident: %s",
                            match.group(1).strip(),
                        )
                        return False
                    logger.info("PCGamingWiki status page reports operational")
                    return True
                # Div not found — page structure changed, fall through
                logger.debug("Status page div not found, falling back to API probe")
        except Exception as e:
            logger.debug("Status page unreachable (%s), falling back to API probe", e)

        # Fallback: lightweight Cargo API query
        try:
            response = self._http.get(
                PCGW_API_URL,
                params={
                    "action": "cargoquery",
                    "format": "json",
                    "tables": "Infobox_game",
                    "fields": "Infobox_game._pageID=pageID",
                    "limit": "1",
                },
                headers=self._headers,
                timeout=10,
            )
            if response.status_code != 200:
                return False
            data = response.json()
            return "cargoquery" in data
        except Exception:
            return False

    def _is_db_outage(self, response: Response) -> bool:
        """Check if a response is a confirmed database outage.

        PCGamingWiki returns an HTML error page with "Cannot access the
        database" when their DB is down. This is distinct from query-
        specific 500s and warrants immediately tripping the breaker.

        Returns True if confirmed DB outage, False otherwise.
        """
        if response.status_code != 500:
            return False
        try:
            body = response.text[:2000]
            return "Cannot access the database" in body
        except Exception:
            return False

    def _record_query_failure(self) -> None:
        """Record a query-level failure for the circuit breaker.

        Called once per failed _cargo_query() invocation (after all retries
        are exhausted), NOT per individual HTTP attempt. This ensures the
        breaker tracks distinct query failures across batches.
        """
        self._consecutive_server_errors += 1
        if self._consecutive_server_errors >= BREAKER_THRESHOLD:
            self._trip_breaker(
                f"{self._consecutive_server_errors} consecutive query failures"
            )

    def _cargo_query(
        self,
        tables: str,
        fields: str,
        where: str = "",
        join_on: str = "",
        limit: int = MAX_RESULTS_PER_QUERY,
        offset: int = 0,
        max_attempts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cargo query against PCGamingWiki API

        Includes exponential backoff on HTTP 429, 5xx, and connection errors.

        Args:
            tables: Cargo table names (e.g., "Infobox_game,Multiplayer")
            fields: Comma-separated field list
            where: WHERE clause (uses HOLDS for virtual/list fields)
            join_on: JOIN condition
            limit: Max results per page
            offset: Result offset for pagination
            max_attempts: Total attempts (initial + retries). Default MAX_ATTEMPTS.

        Returns:
            List of result dicts (each from cargoquery[].title)

        Raises:
            PcgwApiError: On API errors or circuit breaker open
        """
        params = {
            "action": "cargoquery",
            "format": "json",
            "tables": tables,
            "fields": fields,
            "limit": str(limit),
            "offset": str(offset),
        }
        if where:
            params["where"] = where
        if join_on:
            params["join_on"] = join_on

        response = self._get_with_retries(
            PCGW_API_URL, params, max_attempts=max_attempts
        )

        try:
            data = response.json()
        except ValueError as e:
            raise PcgwApiError("Invalid JSON response from API") from e

        if "error" in data:
            error_info = data["error"].get("info", str(data["error"]))
            raise PcgwApiError(f"Cargo API error: {error_info}")

        results = []
        for item in data.get("cargoquery", []):
            results.append(item.get("title", {}))
        return results

    def _get_with_retries(
        self,
        url: str,
        params: dict[str, str],
        max_attempts: int | None = None,
        allow_redirects: bool = True,
        accept_statuses: frozenset = frozenset(),
    ) -> Response:
        """HTTP GET with breaker check, pacing and exponential backoff.

        Args:
            url: Request URL
            params: Query parameters
            max_attempts: Total attempts (default MAX_ATTEMPTS)
            allow_redirects: Follow HTTP redirects
            accept_statuses: Status codes returned to the caller as-is
                (e.g. 302/404 for the appid.php redirect endpoint)

        Returns:
            The Response on success or accepted status.

        Raises:
            PcgwApiError: breaker open, DB outage, connection error, or
                attempts exhausted on server errors.
        """
        if self._check_breaker():
            raise PcgwApiError(
                "PCGamingWiki API unavailable (circuit breaker open)"
            )

        self._rate_limit()

        attempts = max_attempts if max_attempts is not None else MAX_ATTEMPTS

        for attempt in range(attempts):
            try:
                response = self._http.get(
                    url, params=params, headers=self._headers,
                    timeout=15, allow_redirects=allow_redirects,
                )

                if response.status_code in accept_statuses:
                    self._consecutive_server_errors = 0
                    return response

                # Confirmed DB outage → trip breaker immediately, don't retry
                if self._is_db_outage(response):
                    self._trip_breaker("database unavailable")
                    raise PcgwApiError(
                        "PCGamingWiki API unavailable (database outage)"
                    )

                if (
                    response.status_code in RETRYABLE_STATUS_CODES
                    and attempt < attempts - 1
                ):
                    delay = min(
                        BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY
                    )
                    logger.warning(
                        f"PCGamingWiki API returned {response.status_code}, "
                        f"attempt {attempt + 1}/{attempts}, retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    self._last_request_time = time.time()
                    continue

                response.raise_for_status()

            except RequestConnectionError as e:
                # Connection errors (including read timeouts) on a dead host
                # won't resolve within seconds — trip breaker immediately
                # to avoid blocking the UI with minutes of futile retries.
                self._trip_breaker(f"connection error: {e}")
                raise PcgwApiError(
                    f"PCGamingWiki unreachable: {e}"
                ) from e

            except RequestException as e:
                self._record_query_failure()
                raise PcgwApiError(f"HTTP request failed: {e}") from e

            # Success — reset consecutive error counter
            self._consecutive_server_errors = 0
            return response

        # All attempts exhausted with server errors — record for breaker
        self._record_query_failure()
        raise PcgwApiError(f"Request failed after {attempts} attempts")

    # -------------------------------------------------------------------------
    # Wikitext fetch path (Cargo lockdown replacement, lud-8rdr)
    # -------------------------------------------------------------------------

    def resolve_steam_appid(
        self, steam_id: str, max_attempts: int | None = None,
    ) -> str | None:
        """Resolve a Steam AppID to its PCGW page title via appid.php.

        Returns:
            Page title (spaces, unquoted) or None when no page exists.
        """
        if not steam_id:
            return None
        response = self._get_with_retries(
            PCGW_APPID_URL, {"appid": str(steam_id)},
            max_attempts=max_attempts,
            allow_redirects=False,
            accept_statuses=frozenset({301, 302, 404}),
        )
        if response.status_code == 404:
            return None
        location = response.headers.get("Location", "")
        marker = "/wiki/"
        idx = location.find(marker)
        if idx < 0:
            return None
        title = unquote(location[idx + len(marker):]).replace("_", " ").strip()
        return title or None

    def fetch_page_data(
        self, page_title: str, max_attempts: int | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a page's wikitext and parse it into a Cargo-shaped dict.

        Returns:
            Parsed data dict, or None when the page does not exist.
        """
        if not page_title:
            return None
        params = {
            "action": "parse",
            "format": "json",
            "prop": "wikitext",
            "redirects": "1",
            "page": page_title,
        }
        response = self._get_with_retries(
            PCGW_API_URL, params, max_attempts=max_attempts
        )
        try:
            data = response.json()
        except ValueError as e:
            raise PcgwApiError("Invalid JSON response from API") from e
        if "error" in data:
            if data["error"].get("code") == "missingtitle":
                return None
            error_info = data["error"].get("info", str(data["error"]))
            raise PcgwApiError(f"Parse API error: {error_info}")
        parse = data.get("parse", {})
        wikitext = parse.get("wikitext", {}).get("*", "")
        if not wikitext:
            return None
        return parse_page(
            wikitext, parse.get("pageid", ""), parse.get("title", page_title)
        )

    def search_pages(
        self, title: str, limit: int = 2,
        max_attempts: int | None = None,
    ) -> list[str]:
        """Search PCGW page titles (MediaWiki full-text search)."""
        if not title:
            return []
        params = {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": title,
            "srlimit": str(limit),
        }
        response = self._get_with_retries(
            PCGW_API_URL, params, max_attempts=max_attempts
        )
        try:
            data = response.json()
        except ValueError as e:
            raise PcgwApiError("Invalid JSON response from API") from e
        if "error" in data:
            error_info = data["error"].get("info", str(data["error"]))
            raise PcgwApiError(f"Search API error: {error_info}")
        return [
            r.get("title", "")
            for r in data.get("query", {}).get("search", [])
            if r.get("title")
        ]

    @staticmethod
    def _row_holds_gog_id(row: dict[str, Any], gog_id: str) -> bool:
        raw = row.get("GOGcom ID", "") or ""
        return gog_id in {p.strip() for p in raw.split(",") if p.strip()}

    def lookup_by_page_names(
        self,
        page_names: dict[str, str],
        status_callback: ProgressCallback = None,
        max_attempts: int | None = None,
        cancel_check: CancelCheck = None,
        attempted: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch pages by already-known PCGW page name.

        For refresh flows where the store-id -> page match is already
        established; skips the appid.php/search resolution roundtrip.

        Args:
            page_names: Dict mapping store_app_id -> PCGW page name
            status_callback: Progress callback (message, current, total)
            max_attempts: Total attempts per request (default MAX_ATTEMPTS)
            cancel_check: Returns True to stop before the next page
            attempted: Out-list; receives each store_id that got a
                definitive answer

        Returns:
            Dict mapping store_app_id -> parsed page data
        """
        results: dict[str, dict[str, Any]] = {}
        total = len(page_names)
        for index, (store_id, page_name) in enumerate(page_names.items()):
            if self._should_stop(cancel_check):
                break
            if status_callback:
                status_callback(
                    f"PCGamingWiki: Refreshing game {index + 1}/{total}...",
                    index, total,
                )
            try:
                row = self.fetch_page_data(
                    page_name, max_attempts=max_attempts
                )
                if row:
                    results[store_id] = row
                if attempted is not None:
                    attempted.append(store_id)
            except PcgwApiError as e:
                logger.warning(
                    "PCGamingWiki refresh failed for %s: %s", page_name, e,
                )
                if self.breaker_open:
                    break
        return results

    # -------------------------------------------------------------------------
    # Batch Lookup Methods
    # -------------------------------------------------------------------------

    def lookup_by_steam_ids_batch(
        self,
        steam_ids: list[str],
        batch_size: int = DEFAULT_BATCH_SIZE,
        status_callback: ProgressCallback = None,
        max_attempts: int | None = None,
        cancel_check: CancelCheck = None,
        attempted: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Look up games by Steam AppID.

        Per game: appid.php redirect resolution + one wikitext parse
        (the batched cargoquery path is disabled by PCGW's Cargo
        lockdown; the machinery is kept for the authenticated revival,
        see lud-8qvd/Phase 2).

        Args:
            steam_ids: List of Steam AppID strings
            batch_size: Unused on the wikitext path, kept for signature
                compatibility
            status_callback: Progress callback (message, current, total)
            max_attempts: Total attempts per request (default MAX_ATTEMPTS)
            cancel_check: Returns True to stop before the next game
            attempted: Out-list; receives each steam_id that got a
                definitive answer (match or confirmed no-match)

        Returns:
            Dict mapping steam_app_id -> combined Infobox+Multiplayer data
        """
        results: dict[str, dict[str, Any]] = {}
        total = len(steam_ids)
        for index, steam_id in enumerate(steam_ids):
            if self._should_stop(cancel_check):
                break
            if status_callback:
                status_callback(
                    f"PCGamingWiki: Looking up Steam game "
                    f"{index + 1}/{total}...",
                    index, total,
                )
            try:
                page_title = self.resolve_steam_appid(
                    steam_id, max_attempts=max_attempts
                )
                if page_title:
                    row = self.fetch_page_data(
                        page_title, max_attempts=max_attempts
                    )
                    if row:
                        results[steam_id] = row
                # Reached here without an API error: match or a real
                # no-match, either way a cacheable answer
                if attempted is not None:
                    attempted.append(steam_id)
            except PcgwApiError as e:
                logger.warning(
                    "PCGamingWiki lookup failed for Steam %s: %s",
                    steam_id, e,
                )
                if self.breaker_open:
                    if status_callback:
                        status_callback(
                            "PCGamingWiki: Server unavailable, "
                            "skipping remaining lookups",
                            total, total,
                        )
                    break

        if status_callback:
            status_callback(
                f"PCGamingWiki: Found {len(results)} of {total} Steam games",
                total, total,
            )
        logger.info(
            "PCGamingWiki: Matched %d/%d Steam games", len(results), total
        )
        return results

    def lookup_by_gog_ids_batch(
        self,
        gog_ids: list[str],
        batch_size: int = DEFAULT_BATCH_SIZE,
        status_callback: ProgressCallback = None,
        max_attempts: int | None = None,
        titles: dict[str, str] | None = None,
        cancel_check: CancelCheck = None,
        attempted: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Look up games by GOG ID.

        GOG IDs have no appid.php equivalent, so resolution goes title
        search -> parse -> verify the page's GOGcom ID actually holds
        the queried ID (guards against wrong search hits). IDs without
        a title in ``titles`` are skipped and not reported as attempted:
        they were never looked up, so the caller must not cache them.
        """
        results: dict[str, dict[str, Any]] = {}
        total = len(gog_ids)
        titles = titles or {}
        for index, gog_id in enumerate(gog_ids):
            if self._should_stop(cancel_check):
                break
            title = titles.get(gog_id)
            if not title:
                logger.debug(
                    "PCGamingWiki: no title for GOG id %s, skipping", gog_id
                )
                continue
            if status_callback:
                status_callback(
                    f"PCGamingWiki: Looking up GOG game "
                    f"{index + 1}/{total}...",
                    index, total,
                )
            try:
                for candidate in self.search_pages(
                    title, limit=2, max_attempts=max_attempts
                ):
                    row = self.fetch_page_data(
                        candidate, max_attempts=max_attempts
                    )
                    if row and self._row_holds_gog_id(row, gog_id):
                        results[gog_id] = row
                        break
                if attempted is not None:
                    attempted.append(gog_id)
            except PcgwApiError as e:
                logger.warning(
                    "PCGamingWiki lookup failed for GOG %s: %s", gog_id, e,
                )
                if self.breaker_open:
                    if status_callback:
                        status_callback(
                            "PCGamingWiki: Server unavailable, "
                            "skipping remaining lookups",
                            total, total,
                        )
                    break

        if status_callback:
            status_callback(
                f"PCGamingWiki: Found {len(results)} of {total} GOG games",
                total, total,
            )
        logger.info(
            "PCGamingWiki: Matched %d/%d GOG games", len(results), total
        )
        return results

    def _lookup_by_store_ids_batch(
        self,
        store_ids: list[str],
        id_field: str,
        store_label: str,
        batch_size: int,
        status_callback: ProgressCallback,
        max_attempts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Internal cargoquery batch lookup for any store ID field.

        UNREACHABLE since PCGW's Cargo lockdown (anonymous cargoquery
        returns permissiondenied). Kept for the authenticated revival:
        any logged-in account holds the runcargoqueries right (probed
        2026-08-20), see lud-8qvd / Phase 2 of the compliant-fetch plan.

        Builds OR-chained WHERE clauses using HOLDS operator
        (required for virtual/list fields in Cargo).

        Args:
            store_ids: IDs to look up
            id_field: Cargo field name (e.g., "Infobox_game.Steam_AppID")
            store_label: Display name for progress messages
            batch_size: IDs per request
            status_callback: Progress callback
            max_attempts: Total attempts for _cargo_query

        Returns:
            Dict mapping store_id -> data dict
        """
        if not store_ids:
            return {}

        all_fields = ",".join(INFOBOX_FIELDS + MULTIPLAYER_FIELDS + INPUT_FIELDS)
        tables = "Infobox_game,Multiplayer,Input"
        join_on = "Infobox_game._pageID=Multiplayer._pageID,Infobox_game._pageID=Input._pageID"

        results: dict[str, dict[str, Any]] = {}
        total = len(store_ids)
        processed = 0

        # Process in batches
        for batch_start in range(0, total, batch_size):
            if self._cancelled or self.breaker_open:
                break
            batch = store_ids[batch_start:batch_start + batch_size]

            if status_callback:
                status_callback(
                    f"PCGamingWiki: Querying {store_label} batch "
                    f"{batch_start // batch_size + 1}/"
                    f"{(total + batch_size - 1) // batch_size}...",
                    processed,
                    total,
                )

            # Build WHERE clause: field HOLDS "id1" OR field HOLDS "id2" ...
            where_clauses = [
                f'{id_field} HOLDS "{sid}"' for sid in batch
            ]
            where = " OR ".join(where_clauses)

            try:
                rows = self._cargo_query(
                    tables=tables,
                    fields=all_fields,
                    where=where,
                    join_on=join_on,
                    limit=MAX_RESULTS_PER_QUERY,
                    max_attempts=max_attempts,
                )

                # Map results back to queried store IDs
                self._map_results_to_store_ids(
                    rows=rows,
                    queried_ids=batch,
                    id_field_key=_cargo_field_display_name(id_field),
                    results=results,
                )

            except PcgwApiError as e:
                logger.warning(
                    f"PCGamingWiki batch query failed for {store_label} "
                    f"batch {batch_start // batch_size + 1}: {e}"
                )
                # If breaker tripped, skip remaining batches immediately
                if self.breaker_open:
                    if status_callback:
                        status_callback(
                            "PCGamingWiki: Server unavailable, skipping remaining lookups",
                            total,
                            total,
                        )
                    break

            processed += len(batch)

        if status_callback:
            status_callback(
                f"PCGamingWiki: Found {len(results)} of {total} {store_label} games",
                total,
                total,
            )

        logger.info(
            f"PCGamingWiki: Matched {len(results)}/{total} {store_label} games"
        )

        return results

    def _map_results_to_store_ids(
        self,
        rows: list[dict[str, Any]],
        queried_ids: list[str],
        id_field_key: str,
        results: dict[str, dict[str, Any]],
    ) -> None:
        """Map Cargo API results back to queried store IDs

        PCGamingWiki's Steam_AppID field can contain multiple comma-separated
        IDs (e.g., "1245620,2778580,2778590"). This method matches each
        queried ID against the comma-separated values in the result.
        """
        queried_set = set(queried_ids)

        for row in rows:
            raw_ids = row.get(id_field_key, "")
            if not raw_ids:
                continue

            # Parse comma-separated IDs (strip whitespace)
            page_ids = [sid.strip() for sid in raw_ids.split(",") if sid.strip()]

            # Map each queried ID that appears in this page's ID list
            for sid in page_ids:
                if sid in queried_set and sid not in results:
                    results[sid] = row

    # -------------------------------------------------------------------------
    # Single Lookups
    # -------------------------------------------------------------------------

    def lookup_by_steam_id(self, steam_id: str) -> dict[str, Any] | None:
        """Look up a single game by Steam AppID

        Uses reduced retries (1) to avoid long blocking on server errors.

        Returns:
            Game data dict or None if not found
        """
        result = self.lookup_by_steam_ids_batch(
            [steam_id], batch_size=1, max_attempts=1
        )
        return result.get(steam_id)

    def lookup_by_gog_id(
        self, gog_id: str, title: str | None = None,
    ) -> dict[str, Any] | None:
        """Look up a single game by GOG ID

        Uses reduced retries (1) to avoid long blocking on server errors.
        Without a title the wikitext path cannot resolve the page.
        """
        titles = {gog_id: title} if title else None
        result = self.lookup_by_gog_ids_batch(
            [gog_id], batch_size=1, max_attempts=1, titles=titles
        )
        return result.get(gog_id)

    def close(self) -> None:
        """Close the API client"""
        pass  # Session lifecycle managed by NetworkManager


# =============================================================================
# Helpers
# =============================================================================

def _cargo_field_display_name(field: str) -> str:
    """Convert Cargo field name to the display key returned in API results

    Cargo API returns field names with dots replaced by spaces and
    underscores kept. But aliased fields use the alias directly.

    Examples:
        "Infobox_game.Steam_AppID" -> "Steam AppID"
        "Multiplayer.Local_players" -> "Local players"
        "Infobox_game._pageID=pageID" -> "pageID"
    """
    # If aliased (contains =), use the alias
    if "=" in field:
        return field.split("=", 1)[1]

    # Strip table prefix
    if "." in field:
        field = field.split(".", 1)[1]

    # Replace underscores with spaces
    return field.replace("_", " ")
