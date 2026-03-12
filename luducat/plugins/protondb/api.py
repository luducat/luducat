# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# api.py

"""ProtonDB API Client

Simple client for the ProtonDB public API.
Fetches Linux compatibility ratings for Steam games.

Endpoint: https://www.protondb.com/api/v1/reports/summaries/{appid}.json
No authentication required.

Rate limit handling: On 429 responses, raises ProtonDbRateLimitError immediately.
The caller (game_service via metadata_resolver) handles retry, wait, and UI notification.
"""

import logging
import time
from typing import Any, Dict, Optional

from luducat.plugins.sdk.network import RequestException

logger = logging.getLogger(__name__)

# Valid ProtonDB tiers in descending order (lowercase, matching API response)
VALID_TIERS = {"platinum", "gold", "silver", "bronze", "borked", "pending"}

# Suggested wait time when rate limited (passed to caller via exception)
RATE_LIMIT_RETRY_WAIT = 300  # 5 minutes


class ProtonDbApiError(Exception):
    """Error communicating with ProtonDB API"""


class ProtonDbRateLimitError(ProtonDbApiError):
    """Raised when ProtonDB returns 429 rate limit response"""
    def __init__(self, message: str = "Rate limit exceeded", wait_seconds: int = 300):
        super().__init__(message)
        self.wait_seconds = wait_seconds


class ProtonDbApi:
    """ProtonDB API client

    Fetches game compatibility summaries from the ProtonDB public API.
    Rate-limited to avoid overloading the service.
    """

    BASE_URL = "https://www.protondb.com/api/v1/reports/summaries"

    def __init__(self, http_client=None, rate_limit_delay: float = 0.1):
        self._rate_limit_delay = rate_limit_delay
        self._last_request_time = 0.0
        self._http = http_client
        self._cancelled = False

    def cancel(self):
        """Request cancellation of ongoing operations"""
        self._cancelled = True

    def reset_cancel(self):
        """Reset cancellation flag for reuse"""
        self._cancelled = False

    def _rate_limit(self) -> None:
        """Enforce rate limiting between requests"""
        if self._cancelled:
            return
        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.time()

    def get_summary(self, steam_app_id: str) -> Optional[Dict[str, Any]]:
        """Fetch ProtonDB compatibility summary for a Steam game.

        Args:
            steam_app_id: Steam application ID

        Returns:
            Dict with tier, score, confidence, total reports, etc.
            None if game not found on ProtonDB (404).

        Raises:
            ProtonDbRateLimitError: On 429 response (caller handles retry/wait)
            ProtonDbApiError: On other HTTP errors or connection failures
        """
        self._rate_limit()

        url = f"{self.BASE_URL}/{steam_app_id}.json"

        try:
            resp = self._http.get(url, headers={"Accept": "application/json"}, timeout=10)

            if resp.status_code == 404:
                return None

            if resp.status_code == 429:
                raise ProtonDbRateLimitError(
                    f"ProtonDB rate limit for appid {steam_app_id}",
                    wait_seconds=RATE_LIMIT_RETRY_WAIT,
                )

            if resp.status_code != 200:
                raise ProtonDbApiError(
                    f"ProtonDB API returned {resp.status_code} for appid {steam_app_id}"
                )

            data = resp.json()

            # Validate tier
            tier = data.get("tier", "")
            if tier not in VALID_TIERS:
                logger.warning(f"Unknown ProtonDB tier '{tier}' for appid {steam_app_id}")

            return {
                "tier": tier,
                "score": data.get("score", 0.0),
                "confidence": data.get("confidence", ""),
                "total": data.get("total", 0),
                "trending_tier": data.get("trendingTier", ""),
                "best_reported_tier": data.get("bestReportedTier", ""),
            }

        except RequestException as e:
            raise ProtonDbApiError(f"Failed to fetch ProtonDB data for {steam_app_id}: {e}") from e

    def close(self) -> None:
        """Close the API client"""
        pass  # Session lifecycle managed by NetworkManager
