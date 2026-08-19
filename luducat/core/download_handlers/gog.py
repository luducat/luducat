# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""GOG download handler — resolves GOG URLs and product IDs into DownloadTargets.

Uses the existing GOG store plugin's API client for authentication and API access.
Registered at app startup when the GOG plugin is active.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Optional, Sequence

from luducat.core.archivist.types import (
    ArchiveRequest,
    ArchiveType,
    DownloadTarget,
    GameDownloadInfo,
)
from luducat.core.download_handlers.base import (
    AbstractDownloadHandler,
    AuthStatus,
    extract_filename_from_cdn_url,
)
from luducat.core.download_selection import _parse_size_string

logger = logging.getLogger(__name__)

try:
    _("")
except NameError:
    def _(s): return s


# URL parsing patterns
_GAME_PAGE_RE = re.compile(r"gog\.com/(?:\w{2}/)?game/([\w-]+)")
_DOWNLINK_RE = re.compile(r"gog\.com/downlink/file/(\d+)/([\w]+)")


_GOG_BASE_URL = "https://www.gog.com"

_DOWNLOADS_PAGE_RE = re.compile(r"gog\.com/(?:\w{2}/)?downloads/([\w-]+)")

class GogDownloadHandler(AbstractDownloadHandler):
    """Download handler for GOG.com.

    Wraps the GOG store plugin's API client for download resolution.
    Registered at app startup when the GOG plugin is active.
    """

    _URL_PATTERNS = [
        re.compile(r"https?://(?:www\.)?gog\.com/(?:\w{2}/)?game/[\w-]+"),
        re.compile(r"https?://(?:www\.)?gog\.com/downlink/"),
        re.compile(r"https?://(?:www\.)?gog\.com/(?:\w{2}/)?downloads/[\w-]+"),
    ]

    def __init__(self, gog_plugin) -> None:
        self._plugin = gog_plugin

    @property
    def store_name(self) -> str:
        return "gog"

    @property
    def display_name(self) -> str:
        return "GOG.com"

    @property
    def url_patterns(self) -> Sequence[re.Pattern]:
        return self._URL_PATTERNS

    @property
    def supports_audit(self) -> bool:
        return True

    @property
    def supports_adoption(self) -> bool:
        return True

    def adoption_slug_resolver(self, allow_network: bool = False):
        """Slug -> app id resolver for the adoption scanner.

        Backed by the plugin's catalog cache; the optional network
        fallback asks the public products API, which needs no auth.
        """
        def resolve(slug: str):
            db = self._plugin._get_db()
            game = db.get_game_by_slug(slug)
            if game:
                return str(game.gogid)
            if not allow_network:
                return None
            api = self._plugin._get_api_client()
            try:
                meta = self._run_async(api.get_product_metadata(slug))
            except Exception:
                return None
            if meta and meta.get("id"):
                return str(meta["id"])
            return None

        return resolve

    @property
    def auth_failure_message(self) -> str:
        return "Please log in to GOG.com in your browser first."

    def check_auth(self) -> AuthStatus:
        if self._plugin._has_valid_cookies():
            return AuthStatus(True)
        return AuthStatus(False, self.auth_failure_message)

    def resolve_url(self, url: str) -> DownloadTarget:
        # Try downlink URL first (more specific)
        m = _DOWNLINK_RE.search(url)
        if m:
            return self._resolve_downlink_url(m.group(1), m.group(2), url)

        # Try game page URL
        m = _GAME_PAGE_RE.search(url)
        if m:
            return self._resolve_game_page_url(m.group(1))

        # Try downloads page URL (/downloads/slug)
        m = _DOWNLOADS_PAGE_RE.search(url)
        if m:
            return self._resolve_game_page_url(m.group(1))

        raise ValueError(f"Could not resolve GOG URL: {url}")

    def get_available_downloads(
        self, store_app_id: str, details_cache=None,
    ) -> GameDownloadInfo:
        cached = details_cache.get(store_app_id) if details_cache else None
        if cached is not None:
            details = cached
        else:
            api = self._plugin._get_api_client()
            try:
                details = self._run_async(
                    api.get_game_details(store_app_id))
            except Exception as e:
                err_msg = str(e).lower()
                if "auth" in err_msg or "cookie" in err_msg:
                    raise PermissionError(self.auth_failure_message) from e
                raise ValueError(
                    f"Failed to fetch game details: {e}") from e

            if details is None:
                raise ValueError(
                    f"GOG product {store_app_id} not found")

            if details_cache is not None:
                details_cache.put(store_app_id, details)

        downloads = details.get("downloads", {})
        installers = []
        for platform in ("windows", "linux", "mac"):
            for item in downloads.get(platform, []):
                installers.append(item)

        patches = list(downloads.get("patches", []))
        extras = list(details.get("extras", []))

        return GameDownloadInfo(
            game_title=details["title"],
            store_name="gog",
            store_app_id=store_app_id,
            installers=installers,
            patches=patches,
            extras=extras,
        )

    def resolve_downloads(
        self, info: GameDownloadInfo, selections: list[dict],
    ) -> DownloadTarget:
        api = self._plugin._get_api_client()
        cookies = self._get_download_cookies()
        files: list[ArchiveRequest] = []
        skipped: list[dict[str, str]] = []

        installer_downlinks = {item["downlink"] for item in info.installers}
        patch_downlinks = {item["downlink"] for item in info.patches}

        for item in selections:
            downlink = item.get("downlink")
            if not downlink:
                continue

            full_downlink = self._to_full_url(downlink)
            try:
                cdn_url = self._run_async(api.resolve_download_link(full_downlink))
            except Exception as e:
                logger.warning(
                    "Skipping %s: %s", item.get("name", downlink), e,
                )
                skipped.append({
                    "name": item.get("name", "Unknown"),
                    "url": full_downlink,
                    "reason": str(e),
                })
                continue
            if not cdn_url:
                logger.warning(
                    "Skipping %s: could not resolve download link",
                    item.get("name", downlink),
                )
                skipped.append({
                    "name": item.get("name", "Unknown"),
                    "url": full_downlink,
                    "reason": _("Could not resolve download link"),
                })
                continue

            filename = extract_filename_from_cdn_url(cdn_url)
            if not filename:
                filename = item.get("name", "unknown")

            if downlink in installer_downlinks:
                archive_type = ArchiveType.GAME_INSTALLER
            elif downlink in patch_downlinks:
                archive_type = ArchiveType.GAME_PATCH
            else:
                archive_type = ArchiveType.GAME_EXTRA

            files.append(ArchiveRequest(
                url=cdn_url,
                archive_type=archive_type,
                store_name="gog",
                store_app_id=info.store_app_id,
                game_title=info.game_title,
                filename=filename,
                expected_size=_parse_size_string(item.get("size", "")),
                cookies=cookies,
                version=item.get("version"),
                # Stable references for the manifest: the downlink survives
                # CDN token expiry and is what update detection matches on
                # (gog-update-check-and-bulk-queue-plan.md, Phase A).
                metadata={
                    "downlink": full_downlink,
                    "language": item.get("language", ""),
                    "file_id": item.get("id", ""),
                },
            ))

        if not files and skipped:
            raise ValueError(
                _("Could not resolve any download links for {title}").format(
                    title=info.game_title
                )
            )

        return DownloadTarget(
            game_title=info.game_title,
            store_name="gog",
            store_app_id=info.store_app_id,
            files=files,
            skipped=skipped,
        )

    def _get_download_cookies(self) -> dict[str, str]:
        """Get GOG auth cookies for download requests."""
        cookies = {}
        gog_al = self._plugin.get_credential("gog_al")
        if gog_al:
            cookies["gog-al"] = gog_al
        gog_lc = self._plugin.get_credential("gog_lc")
        if gog_lc:
            cookies["gog_lc"] = gog_lc
        return cookies

    def refresh_download_url(
        self, metadata: dict,
    ) -> Optional[tuple[str, dict[str, str]]]:
        """Resolve the stored downlink to a fresh CDN URL + cookies.

        Never raises — the manager marks the download FAILED with its
        auth-failure message when this returns None.
        """
        downlink = (metadata or {}).get("downlink")
        if not downlink:
            return None

        full_downlink = self._to_full_url(downlink)
        api = self._plugin._get_api_client()
        try:
            cdn_url = self._run_async(api.resolve_download_link(full_downlink))
        except Exception as e:
            logger.warning("Could not refresh download URL %s: %s",
                           full_downlink, e)
            return None
        if not cdn_url:
            logger.warning("Downlink did not resolve to a CDN URL: %s",
                           full_downlink)
            return None
        return cdn_url, self._get_download_cookies()

    def get_icon_url(self, store_app_id: str) -> Optional[str]:
        db = self._plugin._get_db()
        game = db.get_game(int(store_app_id))
        if game:
            url = getattr(game, "cover_vertical_url", None)
            if url:
                return url
            url = getattr(game, "cover_url", None)
            if url:
                return url
        return None

    def _resolve_game_page_url(self, slug: str) -> DownloadTarget:
        """Resolve a game page URL slug to a DownloadTarget."""
        product_id = self._slug_to_product_id(slug)
        if product_id is None:
            raise ValueError(
                f"Could not resolve GOG game slug '{slug}' to a product ID"
            )

        info = self.get_available_downloads(str(product_id))

        # Auto-select: current platform installers + all extras
        current_os = self._detect_platform()
        platform_installers = [
            i for i in info.installers if i["platform"] == current_os
        ]
        if not platform_installers:
            platform_installers = info.installers

        selections = platform_installers + info.extras
        return self.resolve_downloads(info, selections)

    def _resolve_downlink_url(
        self, product_id: str, file_id: str, original_url: str,
    ) -> DownloadTarget:
        """Resolve a direct downlink URL to a DownloadTarget."""
        api = self._plugin._get_api_client()

        try:
            cdn_url = self._run_async(api.resolve_download_link(original_url))
        except Exception as e:
            err_msg = str(e).lower()
            if "auth" in err_msg or "cookie" in err_msg:
                raise PermissionError(self.auth_failure_message) from e
            raise ValueError(f"Failed to resolve downlink: {e}") from e

        if not cdn_url:
            raise ValueError(f"Could not resolve downlink URL: {original_url}")

        filename = extract_filename_from_cdn_url(cdn_url)
        if not filename:
            filename = file_id

        # Get game title from details
        game_title = f"GOG-{product_id}"
        try:
            details = self._run_async(api.get_game_details(product_id))
            if details:
                game_title = details["title"]
        except Exception:
            pass

        cookies = self._get_download_cookies()

        return DownloadTarget(
            game_title=game_title,
            store_name="gog",
            store_app_id=product_id,
            files=[
                ArchiveRequest(
                    url=cdn_url,
                    archive_type=ArchiveType.GAME_INSTALLER,
                    store_name="gog",
                    store_app_id=product_id,
                    game_title=game_title,
                    filename=filename,
                    cookies=cookies,
                ),
            ],
        )

    def _slug_to_product_id(self, slug: str) -> Optional[int]:
        """Resolve a URL slug to a GOG product ID.

        GOG extras/DLC download pages append suffixes to the base game slug
        (e.g. ``grime_score`` for the GRIME soundtrack). When the full slug
        fails, progressively strip trailing ``_``-segments and retry.
        """
        product_id = self._try_resolve_slug(slug)
        if product_id:
            return product_id

        candidate = slug
        while "_" in candidate:
            candidate = candidate.rsplit("_", 1)[0]
            product_id = self._try_resolve_slug(candidate)
            if product_id:
                logger.debug("Resolved slug '%s' via shorter form '%s'",
                             slug, candidate)
                return product_id

        return None

    def _try_resolve_slug(self, slug: str) -> Optional[int]:
        """Try resolving a single slug via local DB then public API."""
        db = self._plugin._get_db()
        game = db.get_game_by_slug(slug)
        if game:
            return game.gogid

        api = self._plugin._get_api_client()
        try:
            meta = self._run_async(api.get_product_metadata(slug))
            if meta and "id" in meta:
                return meta["id"]
        except Exception:
            pass

        return None

    @staticmethod
    def _to_full_url(downlink: str) -> str:
        """Turn a relative GOG download path into a full URL."""
        if downlink.startswith(("http://", "https://")):
            return downlink
        return _GOG_BASE_URL + downlink

    @staticmethod
    def _detect_platform() -> str:
        """Map current OS to GOG platform name."""
        import platform as _platform
        system = _platform.system()
        if system == "Linux":
            return "linux"
        elif system == "Darwin":
            return "mac"
        return "windows"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_async(result):
        """Bridge async GOG API calls to sync handler context.

        The GOG API client methods are declared async. When called,
        they return a coroutine. In test context with AsyncMock,
        calling the mock returns a coroutine wrapping the return_value.
        Either way we need to drive the coroutine to get the result.
        """
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result
