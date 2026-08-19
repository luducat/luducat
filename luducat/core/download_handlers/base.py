# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Abstract base class for store download handlers.

Each store that supports downloading (GOG, ZOOM, MangaGamer, JAST USA, etc.)
implements a handler that knows how to resolve URLs and product IDs into
DownloadTarget objects that the DownloadManager (L2) can consume.

Handlers are NOT plugins — they are internal submodules registered at import
time via the handler registry.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Optional, Sequence
from urllib.parse import unquote, urlparse

from luducat.core.archivist.types import DownloadTarget, GameDownloadInfo


def detect_platform() -> str:
    """Map the current OS to the store platform vocabulary.

    Store file lists use windows/linux/mac; handlers use this for
    drop-path auto-selection of the native installer.
    """
    import platform as _platform
    system = _platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "mac"
    return "windows"


def extract_filename_from_cdn_url(cdn_url: str) -> str:
    """Extract the real filename from a resolved CDN URL.

    Stores return obfuscated downlink slugs (e.g. GOG's 'en1installer0');
    only the resolved CDN URL path carries the real filename
    (e.g. 'setup_teenagent_2.1.0.5_(15215).exe'). Used by handlers when
    resolving eagerly and by the download manager to adopt the real name
    at lazy resolve time.

    Args:
        cdn_url: Resolved CDN URL with or without query params.

    Returns:
        Decoded filename, or empty string if extraction fails.
    """
    parsed = urlparse(cdn_url)
    path = unquote(parsed.path)
    if not path or path == "/":
        return ""
    if path.endswith("/"):
        path = path[:-1]
    return path.rsplit("/", 1)[-1]


class AuthStatus:
    """Authentication state for a download handler."""

    __slots__ = ("authenticated", "message")

    def __init__(self, authenticated: bool, message: str = "") -> None:
        self.authenticated = authenticated
        self.message = message

    def __bool__(self) -> bool:
        return self.authenticated

    def __repr__(self) -> str:
        return f"AuthStatus(authenticated={self.authenticated}, message={self.message!r})"


class AbstractDownloadHandler(ABC):
    """Base class for store-specific download handlers.

    Two interaction paths converge here:

    1. **URL drop/paste** (drop target window):
       find_handler(url) → handler.resolve_url(url) → DownloadTarget

    2. **Library click** (integrated download):
       get_handler(store_name) → handler.get_available_downloads(app_id)
       → show picker → handler.resolve_downloads(info, selections)
       → DownloadTarget

    Both produce a DownloadTarget that goes to DownloadManager.submit().
    """

    @property
    @abstractmethod
    def store_name(self) -> str:
        """Short identifier for this store (e.g. 'gog', 'zoom', 'jastusa').

        Must match the store_name used elsewhere in luducat (plugin DB, etc.).
        """

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable store name (e.g. 'GOG.com', 'ZOOM Platform')."""

    @property
    @abstractmethod
    def url_patterns(self) -> Sequence[re.Pattern]:
        """Compiled regex patterns for URLs this handler accepts.

        Used by the registry to route dropped/pasted URLs. Patterns are
        matched against the full URL string.
        """

    @property
    def supports_audit(self) -> bool:
        """Whether the archive auditor can scan this store.

        Requires get_available_downloads() by app id plus stable
        downlink metadata on resolved files, so the auditor can diff
        offerings against the manifest and enqueue lazily. Default:
        not supported.
        """
        return False

    @property
    def supports_adoption(self) -> bool:
        """Whether a pre-existing on-disk archive can be adopted for this store.

        Requires adoption_slug_resolver() to return a resolver backed by
        the store's local catalog cache, so the adoption scanner can
        attribute game directories offline. Default: not supported.
        """
        return False

    def adoption_slug_resolver(self, allow_network: bool = False):
        """Build a callable mapping a store slug to an app id string.

        The resolver returns None for unknown slugs. allow_network
        permits store API lookups for slugs missing from the local
        catalog cache; the default stays offline so adopting a large
        archive needs no connection. Returns None when the handler does
        not support adoption.
        """
        return None

    @property
    def auth_required(self) -> bool:
        """Whether this handler needs authentication to download.

        Defaults to True (most stores need auth). Override to False for
        stores with public downloads.
        """
        return True

    @property
    def auth_failure_message(self) -> str:
        """Message shown when auth is missing or expired.

        Override with a store-specific hint like
        "Please log in to GOG.com in your browser first."
        """
        return f"Authentication required for {self.display_name}."

    @abstractmethod
    def check_auth(self) -> AuthStatus:
        """Check whether the handler can currently authenticate.

        Returns an AuthStatus with authenticated=True if ready,
        or authenticated=False with a message explaining what's missing.
        """

    def can_handle(self, url: str) -> bool:
        """Test whether this handler accepts the given URL.

        Default implementation checks against url_patterns. Override for
        more complex matching (e.g. domain + path structure).
        """
        return any(p.search(url) for p in self.url_patterns)

    @abstractmethod
    def resolve_url(self, url: str) -> DownloadTarget:
        """Resolve a dropped/pasted URL into a DownloadTarget.

        Called from the URL drop path. The handler must:
        1. Parse the URL to extract product/download identifiers
        2. Fetch installer/file metadata from the store API
        3. Build and return a DownloadTarget with all files

        For stores with multiple installer options (platform, language),
        the handler should either auto-select based on user preferences
        or return all options for the picker dialog.

        Raises:
            ValueError: If the URL can't be resolved (bad format, 404, etc.)
            PermissionError: If authentication is missing or expired.
        """

    @abstractmethod
    def get_available_downloads(
        self, store_app_id: str, details_cache=None,
    ) -> GameDownloadInfo:
        """Fetch available downloads for a game from the store.

        Called from the library click path. Returns structured info about
        all available installers, patches, and extras for a game. The UI
        presents this via an installer picker dialog.

        Args:
            store_app_id: The store-specific product/app ID.

        Raises:
            ValueError: If the product ID is invalid or not found.
            PermissionError: If authentication is missing or expired.
        """

    @abstractmethod
    def resolve_downloads(
        self,
        info: GameDownloadInfo,
        selections: list[dict],
    ) -> DownloadTarget:
        """Convert user-selected installers into a DownloadTarget.

        Called after the installer picker dialog. The handler resolves
        the selected items (from info.installers/patches/extras) into
        concrete download URLs and builds a DownloadTarget.

        Args:
            info: The GameDownloadInfo returned by get_available_downloads().
            selections: List of selected items (dicts from info's lists).
                Each dict has at minimum 'name', 'platform', 'downlink'.

        Raises:
            ValueError: If selections reference unknown items.
            PermissionError: If authentication expired between fetch and resolve.
        """

    def refresh_download_url(
        self, metadata: dict,
    ) -> Optional[tuple[str, dict[str, str]]]:
        """Re-resolve a stable store reference to a fresh CDN URL.

        Called by the download manager when a stored URL is empty (lazy
        bulk enqueue) or rejected with 403/410 (expired token). Handlers
        that support it read their stable reference from the download's
        metadata. Returns (cdn_url, cookies) — cookies are re-fetched so
        lazy downloads never carry stale session data. Default: not
        supported.
        """
        return None

    def get_icon_url(self, store_app_id: str) -> Optional[str]:
        """Get a cover/icon URL for the game (for download row thumbnail).

        Default returns None (no icon). Override if the store provides
        cover image URLs that can be resolved without extra API calls.
        """
        return None
