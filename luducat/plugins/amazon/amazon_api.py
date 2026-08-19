# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# amazon_api.py

# Protocol documented from Nile (https://github.com/imLinguin/nile)
# Copyright (c) imLinguin and Nile contributors
# Licensed under GPLv3
# Clean-room implementation from analysis notes; endpoint constants and
# request field names are functional facts required by the Amazon API.
"""Amazon Games (Animus distribution) API client.

The library is entitlement-based: GetEntitlements pages through the
owned games (50 per page via nextToken) and supports incremental sync
via a server-issued syncPoint. There is no public catalog or search
endpoint. All requests use the amz-1.0 header encoding with the
launcher's UserAgent and a hardwareHash derived from the registered
device serial.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENTITLEMENTS_URL = "https://gaming.amazon.com/api/distribution/entitlements"

_ENTITLEMENTS_TARGET = (
    "com.amazon.animusdistributionservice.entitlement."
    "AnimusEntitlementsService.GetEntitlements"
)

_USER_AGENT = "com.amazon.agslauncher.win/3.0.9202.1"

# Fixed opaque server-side value the launcher sends with every
# GetEntitlements request (functional fact, not a secret).
_KEY_ID = "d5dc8b8b-86c8-4fc4-ae93-18c0def5314d"

_PAGE_SIZE = 50

# Runaway-pagination guard: 200 pages x 50 = 10000 entitlements,
# far above any real Amazon library.
_MAX_PAGES = 200


class AmazonApi:
    """Animus distribution API client.

    Uses PluginHttpClient for all requests (domain firewall, rate
    limiting via plugin.json).

    Args:
        http_client: PluginHttpClient instance
    """

    def __init__(self, http_client: Any):
        self._http = http_client

    @staticmethod
    def hardware_hash(device_serial: str) -> str:
        """hardwareHash = SHA-256 of the device serial, uppercase hex."""
        if not device_serial:
            raise ValueError("device_serial must not be empty")
        return hashlib.sha256(device_serial.encode()).hexdigest().upper()

    def get_entitlements(
        self,
        access_token: str,
        device_serial: str,
        sync_point: Optional[float] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Fetch owned-game entitlements, following pagination.

        Args:
            access_token: Valid bearer token
            device_serial: Registered device serial (hardwareHash basis)
            sync_point: CLIENT-side epoch timestamp of the previous sync
                for an incremental fetch (the server filters to changes
                since then and never issues syncPoints itself — verified
                live 2026-07-10); None fetches the full library
            cancel_check: Optional callable; pagination stops when it
                returns True

        Returns:
            Tuple of (entitlement dicts, completed) — completed is False
            when the fetch was cancelled mid-pagination

        Raises:
            RuntimeError: On API errors or runaway pagination
        """
        headers = {
            "X-Amz-Target": _ENTITLEMENTS_TARGET,
            "x-amzn-token": access_token,
            "UserAgent": _USER_AGENT,
            "Content-Type": "application/json",
            "Content-Encoding": "amz-1.0",
        }

        entitlements: List[Dict[str, Any]] = []
        next_token: Optional[str] = None

        for page in range(1, _MAX_PAGES + 1):
            if cancel_check and cancel_check():
                logger.debug("Entitlements fetch cancelled at page %d", page)
                return entitlements, False

            body = {
                "Operation": "GetEntitlements",
                "clientId": "Sonic",
                "syncPoint": sync_point,
                "nextToken": next_token,
                "maxResults": _PAGE_SIZE,
                "productIdFilter": None,
                "keyId": _KEY_ID,
                "hardwareHash": self.hardware_hash(device_serial),
            }
            response = self._http.post(
                _ENTITLEMENTS_URL, headers=headers, json=body, timeout=30
            )
            self._check_response(response, "get_entitlements")

            data = response.json()
            page_items = data.get("entitlements", [])
            entitlements.extend(page_items)

            next_token = data.get("nextToken")
            logger.debug(
                "Entitlements page %d: +%d (total %d), more=%s",
                page, len(page_items), len(entitlements),
                bool(next_token),
            )
            if not next_token:
                return entitlements, True

        raise RuntimeError(
            f"Amazon entitlements pagination exceeded {_MAX_PAGES} pages — "
            "aborting (server kept returning nextToken)"
        )

    # ── Private helpers ─────────────────────────────────────────────

    @staticmethod
    def _check_response(response, method: str) -> None:
        if response.status_code == 401:
            raise RuntimeError(
                f"Amazon API {method}: authentication expired (HTTP 401)"
            )
        if response.status_code >= 400:
            detail = ""
            try:
                data = response.json()
                detail = data.get("message") or data.get("__type") or ""
            except Exception:
                pass
            raise RuntimeError(
                f"Amazon API {method}: HTTP {response.status_code}"
                + (f" — {detail}" if detail else "")
            )
