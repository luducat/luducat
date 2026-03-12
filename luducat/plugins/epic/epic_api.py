# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# epic_api.py

# Portions adapted from Legendary (https://github.com/derrod/legendary)
# Copyright (c) Rodney and Legendary contributors
# Licensed under GPLv3+
"""Direct Epic Games API client.

Replaces both ``legendary_manager.py`` (subprocess wrapper) and
``catalog_api.py`` (epicstore_api dependency) with direct HTTP calls
through PluginHttpClient.

All authenticated endpoints require a valid bearer token from EpicSession.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── API hosts ────────────────────────────────────────────────────────

_ACCOUNT_HOST = "https://account-public-service-prod03.ol.epicgames.com"
_LAUNCHER_HOST = "https://launcher-public-service-prod06.ol.epicgames.com"
_CATALOG_HOST = "https://catalog-public-service-prod06.ol.epicgames.com"
_LIBRARY_HOST = "https://library-service.live.use1a.on.epicgames.com"
_STORE_CONTENT_HOST = "https://store-content.ak.epicgames.com"


class EpicAPI:
    """Direct Epic Games API client.

    Uses PluginHttpClient for all requests (domain firewall, rate limiting).

    Args:
        http_client: PluginHttpClient instance
        locale: API locale (default: "en-US")
        country: Country code (default: "US")
    """

    # Class-level caches — shared across instances, survives batch boundaries
    _namespace_to_slug: Optional[Dict[str, str]] = None
    _mapping_fetched: bool = False

    def __init__(
        self,
        http_client: Any,
        locale: str = "en-US",
        country: str = "US",
    ):
        self._http = http_client
        self._locale = locale
        self._country = country

    # ── Authenticated endpoints (require bearer token) ──────────────

    def get_game_assets(
        self, access_token: str, platform: str = "Windows"
    ) -> List[Dict[str, Any]]:
        """List ALL game assets the user owns for a platform.

        One HTTP call returns the entire library.

        Args:
            access_token: Valid bearer token
            platform: "Windows", "Mac", or "Linux"

        Returns:
            List of asset dicts with appName, catalogItemId, namespace, etc.
        """
        url = f"{_LAUNCHER_HOST}/launcher/api/public/assets/{platform}"
        response = self._http.get(
            url,
            headers=self._auth_headers(access_token),
            params={"label": "Live"},
            timeout=60,
        )
        self._check_response(response, "get_game_assets")
        return response.json()

    def get_game_info(
        self,
        access_token: str,
        namespace: str,
        catalog_id: str,
    ) -> Dict[str, Any]:
        """Fetch full catalog metadata for a single game.

        Uses the catalog bulk endpoint with DLC and main game details.
        Returns everything: descriptions, images, developer, publisher,
        categories, customAttributes, DLC list, etc.

        Args:
            access_token: Valid bearer token
            namespace: Game namespace
            catalog_id: Catalog item ID

        Returns:
            Catalog metadata dict (keyed by catalog_id in response,
            we return the inner dict directly)
        """
        url = (
            f"{_CATALOG_HOST}/catalog/api/shared"
            f"/namespace/{namespace}/bulk/items"
        )
        response = self._http.get(
            url,
            headers=self._auth_headers(access_token),
            params={
                "id": catalog_id,
                "includeDLCDetails": "true",
                "includeMainGameDetails": "true",
                "country": self._country,
                "locale": self._locale,
            },
            timeout=30,
        )
        self._check_response(response, "get_game_info")
        data = response.json()

        # Response is keyed by catalog_id — return the inner dict
        if catalog_id in data:
            return data[catalog_id]
        # Fallback: return first item if any
        if data:
            return next(iter(data.values()))
        return {}

    def get_library_items(
        self, access_token: str
    ) -> List[Dict[str, Any]]:
        """Fetch all library items with cursor-based pagination.

        Args:
            access_token: Valid bearer token

        Returns:
            Complete list of library records
        """
        all_records = []
        cursor = None

        while True:
            params = {"includeMetadata": "true"}
            if cursor:
                params["cursor"] = cursor

            url = f"{_LIBRARY_HOST}/library/api/public/items"
            response = self._http.get(
                url,
                headers=self._auth_headers(access_token),
                params=params,
                timeout=30,
            )
            self._check_response(response, "get_library_items")
            data = response.json()

            records = data.get("records", [])
            all_records.extend(records)

            next_cursor = (
                data.get("responseMetadata", {}).get("nextCursor")
            )
            if not next_cursor:
                break
            cursor = next_cursor

        return all_records

    def verify_token(self, access_token: str) -> Dict[str, Any]:
        """Verify access token validity.

        Args:
            access_token: Bearer token to verify

        Returns:
            Session info dict (account_id, displayName, etc.)
        """
        url = f"{_ACCOUNT_HOST}/account/api/oauth/verify"
        response = self._http.get(
            url,
            headers=self._auth_headers(access_token),
            timeout=10,
        )
        self._check_response(response, "verify_token")
        return response.json()

    # ── Unauthenticated endpoints (public catalog) ──────────────────

    def get_product_mapping(self) -> Dict[str, str]:
        """Fetch namespace→slug product mapping (class-level cache).

        Returns:
            Dict mapping namespace to product slug
        """
        if EpicAPI._mapping_fetched:
            return EpicAPI._namespace_to_slug or {}

        url = f"{_STORE_CONTENT_HOST}/api/content/productmapping"
        try:
            response = self._http.get(url, timeout=30)
            if response.status_code == 200:
                EpicAPI._namespace_to_slug = response.json()
                logger.debug(
                    "Loaded %d namespace mappings",
                    len(EpicAPI._namespace_to_slug),
                )
            else:
                logger.debug(
                    "Product mapping request returned HTTP %d",
                    response.status_code,
                )
                EpicAPI._namespace_to_slug = {}
        except Exception as e:
            logger.debug("Failed to fetch product mapping: %s", e)
            EpicAPI._namespace_to_slug = {}

        EpicAPI._mapping_fetched = True
        return EpicAPI._namespace_to_slug

    def get_product_data(self, namespace: str) -> Optional[Dict[str, Any]]:
        """Fetch product page data by namespace (via slug lookup).

        Args:
            namespace: Epic namespace

        Returns:
            Full product data dict, or None if not found
        """
        mapping = self.get_product_mapping()
        slug = mapping.get(namespace)
        if not slug:
            logger.debug("No slug found for namespace '%s'", namespace)
            return None

        url = (
            f"{_STORE_CONTENT_HOST}/api/{self._locale}"
            f"/content/products/{slug}"
        )
        try:
            response = self._http.get(url, timeout=30)
            if response.status_code == 404:
                logger.debug("Product not found: %s", slug)
                return None
            if response.status_code != 200:
                logger.debug(
                    "Product request returned HTTP %d for %s",
                    response.status_code, slug,
                )
                return None
            return response.json()
        except Exception as e:
            logger.debug("Failed to fetch product %s: %s", slug, e)
            return None

    # ── Metadata extraction ─────────────────────────────────────────

    def extract_metadata(
        self, product_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Extract metadata from product page data (REST endpoint).

        Used as FALLBACK enrichment for games where get_game_info()
        returns sparse data.

        Args:
            product_data: Full product data from get_product_data()

        Returns:
            Dict with extracted metadata fields
        """
        result: Dict[str, Any] = {}

        # Extract from productHome page
        pages = product_data.get("pages") or []
        for page in pages:
            if page.get("type") != "productHome":
                continue

            data = page.get("data", {})

            # Descriptions from "about" section
            about = data.get("about", {})
            if about.get("description"):
                result["description"] = about["description"]
            if about.get("shortDescription"):
                result["short_description"] = about["shortDescription"]

            # Developer/Publisher
            if about.get("developerAttribution"):
                result["developers"] = [about["developerAttribution"]]
            if about.get("publisherAttribution"):
                result["publishers"] = [about["publisherAttribution"]]

            # Gallery images (screenshots)
            gallery = data.get("gallery", {})
            gallery_images = gallery.get("galleryImages", [])
            if gallery_images:
                screenshots = [
                    img["src"] for img in gallery_images if img.get("src")
                ]
                if screenshots:
                    result["screenshots"] = screenshots

            # Hero/banner images
            hero = data.get("hero", {})
            if hero.get("backgroundImageUrl"):
                result["background_url"] = hero["backgroundImageUrl"]
            if hero.get("logoImageUrl"):
                result["logo_url"] = hero["logoImageUrl"]

            break

        # Key images from top level
        key_images = product_data.get("keyImages", [])
        for img in key_images:
            img_type = img.get("type", "")
            url = img.get("url", "")
            if not url:
                continue

            if img_type == "DieselGameBoxTall":
                result["cover_url"] = url
            elif img_type == "DieselGameBox":
                result["header_url"] = url
            elif img_type == "DieselGameBoxLogo":
                result["logo_url"] = result.get("logo_url") or url
            elif img_type == "OfferImageWide":
                result["header_url"] = result.get("header_url") or url
            elif img_type == "OfferImageTall":
                result["cover_url"] = result.get("cover_url") or url
            elif img_type == "Thumbnail":
                result["thumbnail_url"] = result.get("thumbnail_url") or url

        # Categories/genres
        categories = product_data.get("categories", [])
        if categories:
            genres = [
                cat.get("path", "").split("/")[-1]
                for cat in categories
                if cat.get("path")
            ]
            genres = [g for g in genres if g]
            if genres:
                result["genres"] = genres

        # Release date
        if product_data.get("releaseDate"):
            result["release_date"] = product_data["releaseDate"]
        elif product_data.get("effectiveDate"):
            result["release_date"] = product_data["effectiveDate"]

        return result

    # ── Cache management ────────────────────────────────────────────

    @classmethod
    def reset_caches(cls) -> None:
        """Reset all class-level caches (for testing)."""
        cls._namespace_to_slug = None
        cls._mapping_fetched = False

    # ── Private helpers ─────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        """Build authorization headers for authenticated requests."""
        return {
            "Authorization": f"bearer {access_token}",
            "Accept": "application/json",
        }

    def _check_response(self, response, method: str) -> None:
        """Check HTTP response and raise on error."""
        if response.status_code == 401:
            raise RuntimeError(
                f"Epic API {method}: authentication expired (HTTP 401)"
            )
        if response.status_code == 403:
            raise RuntimeError(
                f"Epic API {method}: access denied (HTTP 403)"
            )
        if response.status_code >= 400:
            detail = ""
            try:
                data = response.json()
                detail = data.get("errorMessage", data.get("message", ""))
            except Exception:
                pass
            raise RuntimeError(
                f"Epic API {method}: HTTP {response.status_code}"
                f"{f' — {detail}' if detail else ''}"
            )
