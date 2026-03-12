# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# image_cache.py

"""Image cache for luducat

Provides disk-based caching and async loading of game covers and screenshots.
Uses QThread for non-blocking image fetching.
Implements LRU eviction with byte-based memory budgets.
"""

import hashlib
import os
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from PySide6.QtCore import QObject, QThread, Signal, QMutex, QMutexLocker, QUrl, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtCore import Qt

from ..core.config import get_cache_dir
from ..core.constants import USER_AGENT

logger = logging.getLogger(__name__)

# Circuit breaker for disk write failures — once tripped, skip all disk writes
# to avoid repeated error logging. Images still display from memory.
_disk_write_disabled = False
_disk_write_warn_logged = False

# 404 negative cache — URLs that returned HTTP 404 are remembered to avoid
# retrying the same broken URL thousands of times (GOG CDN 404 spam).
_not_found_urls: Set[str] = set()
_not_found_lock = threading.Lock()

# Per-domain rate limiting — prevents hammering CDNs (especially GOG Varnish).
# Maps domain → minimum seconds between requests.
_DOMAIN_RATE_LIMITS: Dict[str, float] = {
    "gog-statics.com": 0.5,       # 10 req/s max to GOG CDN
    "gog.com": 0.5,
    "steamgriddb.com": 0.4,      # ~3 req/s — SGDB CDN times out under higher rates
    "pcgamingwiki.com": 0.3,     # ~6 req/s — community wiki, be polite
}
_domain_last_request: Dict[str, float] = {}
_domain_rate_lock = threading.Lock()


_disk_write_disabled_callbacks = []  # Callbacks to notify UI of circuit breaker trip


def _set_disk_write_disabled(error: OSError) -> None:
    """Trip the circuit breaker on disk write failure. Logs once."""
    global _disk_write_disabled, _disk_write_warn_logged
    _disk_write_disabled = True
    if not _disk_write_warn_logged:
        _disk_write_warn_logged = True
        import errno
        if error.errno == errno.ENOSPC:
            reason = "disk full"
        elif error.errno in (errno.EACCES, errno.EPERM):
            reason = "permission denied"
        elif error.errno == getattr(errno, 'EROFS', None):
            reason = "read-only filesystem"
        else:
            reason = str(error)
        logger.warning(
            f"Cache disk writes disabled ({reason}). "
            "Images will load from network but not persist to disk."
        )
        # Notify registered callbacks
        for cb in _disk_write_disabled_callbacks:
            try:
                cb(reason)
            except Exception:
                pass


def register_disk_write_callback(callback) -> None:
    """Register a callback to be called when disk writes are disabled.

    The callback receives a reason string (e.g. "disk full").
    """
    _disk_write_disabled_callbacks.append(callback)


def _domain_throttle(url: str) -> None:
    """Sleep if needed to respect per-domain rate limits."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    # Match against rate-limited domains (suffix match for CDN subdomains)
    delay = 0.0
    domain = ""
    for domain, limit in _DOMAIN_RATE_LIMITS.items():
        if hostname == domain or hostname.endswith("." + domain):
            delay = limit
            break
    if delay <= 0:
        return

    with _domain_rate_lock:
        now = time.monotonic()
        last = _domain_last_request.get(domain, 0.0)
        wait = delay - (now - last)
        if wait > 0:
            time.sleep(wait)
        _domain_last_request[domain] = time.monotonic()


def _build_request_headers(url: str) -> dict:
    """Build domain-specific request headers for image downloads.

    GOG's Varnish CDN requires browser-like headers (Referer, Accept,
    sec-fetch-*) or it returns 404s for valid image hashes.
    """
    headers = {}
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if "gog-statics.com" in hostname or "gog.com" in hostname:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.gog.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "en",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
    elif "pcgamingwiki.com" in hostname:
        headers = {
            "Referer": "https://www.pcgamingwiki.com/",
            "User-Agent": USER_AGENT,
        }

    return headers


# Register pillow-heif for AVIF support (required dependency)
# Pillow can then open AVIF files, which we convert to PNG for Qt
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    logger.debug("pillow-heif registered - AVIF support enabled")
except ImportError:
    logger.warning("pillow-heif not installed - AVIF images will fail to load")


# Default LRU cache limits (number of pixmaps to keep in memory)
# Sized for larger grids (6x3+ visible items) with scrolling buffer
DEFAULT_COVER_CACHE_SIZE = 300      # Secondary limit; byte budget is primary
DEFAULT_SCREENSHOT_CACHE_SIZE = 150  # ~500KB-2MB each = ~100-400MB (supports 10x10 grid + buffer)

# Byte-based memory budgets per cache type (primary constraint)
# Each decoded cover (400x600 RGBA) = ~0.92 MB; 200 MB fits ~218 covers.
# 100 MB only held ~109 — not enough for scrolling a large library in cover view.
DEFAULT_COVER_BUDGET_BYTES = 200 * 1024 * 1024       # 200 MB
DEFAULT_SCREENSHOT_BUDGET_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_HERO_BUDGET_BYTES = 20 * 1024 * 1024         # 20 MB
DEFAULT_DESCRIPTION_BUDGET_BYTES = 30 * 1024 * 1024  # 30 MB

# Idle trim: seconds of inactivity before trimming cache to 50%
IDLE_TRIM_SECONDS = 30
IDLE_CHECK_INTERVAL_MS = 5000  # Check every 5 seconds


def _pixmap_bytes(pixmap: QPixmap) -> int:
    """Calculate decoded memory size of a QPixmap in bytes."""
    if pixmap.isNull():
        return 0
    return pixmap.width() * pixmap.height() * pixmap.depth() // 8


# Module-level HTTP session for connection pooling (reuses TCP/SSL connections)
_http_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """Get or create the shared HTTP session with connection pooling.

    Delegates to ``NetworkManager.get_image_session()`` when available,
    falling back to a local session for test environments or early startup.
    """
    from urllib3.util.retry import Retry

    global _http_session
    if _http_session is None:
        # Try to use NetworkManager's shared image session
        try:
            from ..core.network_manager import get_network_manager
            _http_session = get_network_manager().get_image_session()
            logger.info("Image cache using NetworkManager session")
            return _http_session
        except Exception:
            pass  # NetworkManager not available — create standalone

        _http_session = requests.Session()

        # Configure retries with exponential backoff
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )

        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,  # Reduced from 32 to limit open connections
            max_retries=retry_strategy,
            pool_block=False,  # Don't block when pool full, raise instead
        )
        _http_session.mount('https://', adapter)
        _http_session.mount('http://', adapter)
        logger.info("HTTP session initialized with connection pooling (max 20 connections)")
    return _http_session


def close_session() -> None:
    """Close the HTTP session and release all connections.

    Call this during memory pressure cleanup or application shutdown.
    """
    global _http_session
    if _http_session is not None:
        _http_session.close()
        _http_session = None
        logger.info("HTTP session closed")


def _compute_max_concurrent() -> int:
    cores = os.cpu_count() or 1

    num_threads = 2

    if cores <= 4:
        num_threads = 4
    elif cores <= 6:
        num_threads = 6
    else:
        num_threads = 8

    logger.info(f"Using {num_threads} cores for threads (system has {cores} cores).")

    return num_threads


class ImageLoaderWorker(QThread):
    """Worker thread for loading images from URLs"""

    image_loaded = Signal(str, QPixmap)  # url, pixmap
    image_failed = Signal(str, str)  # url, error message
    image_not_found = Signal(str, str)  # url, error message (HTTP 404 only)

    def __init__(
        self,
        url: str,
        cache_path: Path,
        max_size: Optional[Tuple[int, int]] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._url = url
        self._cache_path = cache_path
        self._max_size = max_size  # (max_w, max_h) or None for full-res
        self._skip_download = False  # Set True for disk-only loads (no network fetch)

    def run(self) -> None:
        """Download and cache image"""
        try:
            # Check if already cached
            if self._cache_path.exists():
                pixmap = QPixmap(str(self._cache_path))
                if not pixmap.isNull():
                    pixmap = self._downscale(pixmap)
                    self.image_loaded.emit(self._url, pixmap)
                    return
                # File exists but invalid - fall through to download (if allowed)

            # Skip download if this is a disk-only load request
            if self._skip_download:
                self.image_failed.emit(self._url, "Cache file missing or invalid")
                return

            # Check 404 negative cache — don't retry known-dead URLs
            with _not_found_lock:
                if self._url in _not_found_urls:
                    self.image_not_found.emit(self._url, "404 (cached)")
                    return

            # Per-domain rate limiting (GOG Varnish etc.)
            _domain_throttle(self._url)

            # Build request headers (domain-specific for sites that block hotlinking)
            headers = _build_request_headers(self._url)

            # Download image (uses pooled connection with context manager for cleanup)
            with _get_session().get(self._url, timeout=10, headers=headers) as response:
                response.raise_for_status()
                content = response.content

            # Record image download in NetworkManager stats
            try:
                from ..core.network_manager import get_network_manager
                parsed = urlparse(self._url)
                get_network_manager().record_image_request(
                    parsed.hostname or "", len(content)
                )
            except Exception:
                pass  # NetworkManager not available

            # Save to cache (circuit breaker: skip if disk writes are disabled)
            disk_written = False
            if not _disk_write_disabled:
                try:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_bytes(content)
                    disk_written = True
                except OSError as e:
                    _set_disk_write_disabled(e)

            # Always convert AVIF to PNG - QPixmap doesn't support AVIF natively
            # pillow-heif enables Pillow to open AVIF, then we convert for Qt
            ext = self._cache_path.suffix.lower()
            if ext == '.avif':
                if disk_written:
                    try:
                        from PIL import Image
                        with Image.open(self._cache_path) as img:
                            png_path = self._cache_path.with_suffix('.png')
                            img.save(png_path, 'PNG')
                        self._cache_path.unlink(missing_ok=True)
                        self._cache_path = png_path
                    except Exception as conv_err:
                        logger.debug(f"Failed to convert {ext} image: {conv_err}")
                        self.image_failed.emit(self._url, f"Unsupported format: {ext}")
                        return
                else:
                    # Can't convert AVIF without disk — try in-memory
                    try:
                        from PIL import Image
                        from io import BytesIO
                        with Image.open(BytesIO(content)) as img:
                            buf = BytesIO()
                            img.save(buf, 'PNG')
                            content = buf.getvalue()
                    except Exception as conv_err:
                        logger.debug(f"Failed to convert {ext} in memory: {conv_err}")
                        self.image_failed.emit(self._url, f"Unsupported format: {ext}")
                        return

            # Load as pixmap from cache file if written, else from bytes
            if disk_written and self._cache_path.exists():
                pixmap = QPixmap(str(self._cache_path))
            else:
                pixmap = QPixmap()
                pixmap.loadFromData(content)

            if pixmap.isNull():
                self.image_failed.emit(self._url, "Failed to load downloaded image")
            else:
                pixmap = self._downscale(pixmap)
                self.image_loaded.emit(self._url, pixmap)

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                with _not_found_lock:
                    _not_found_urls.add(self._url)
                self.image_not_found.emit(self._url, "404 Not Found")
            else:
                self.image_failed.emit(self._url, str(e))
        except requests.RequestException as e:
            self.image_failed.emit(self._url, str(e))
        except Exception as e:
            logger.exception(f"Image loading error for {self._url}: {e}")
            self.image_failed.emit(self._url, str(e))

    def _downscale(self, pixmap: QPixmap) -> QPixmap:
        """Downscale pixmap if it exceeds max_size limits."""
        if not self._max_size:
            return pixmap
        max_w, max_h = self._max_size
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(
                max_w, max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pixmap


class ImageCache(QObject):
    """Thread-safe image cache with async loading and LRU eviction

    Uses byte-based memory budgets as the primary constraint, with item
    count as a secondary safety limit. Tracks actual decoded pixmap memory.

    Usage:
        cache = ImageCache(max_memory_items=200, max_memory_bytes=60*1024*1024)
        cache.image_loaded.connect(on_image_loaded)
        pixmap = cache.get_image(url)  # Returns cached or None, triggers async load
    """

    # Emitted when an image finishes loading
    image_loaded = Signal(str, QPixmap)  # url, pixmap
    # Emitted when an image fails to load
    image_failed = Signal(str, str)  # url, error message
    # Emitted when an image returns HTTP 404 (distinct from other failures)
    image_not_found = Signal(str, str)  # url, error message

    # Maximum concurrent downloads (8 allows faster loading of zoomed-out grids)
    MAX_CONCURRENT = 8

    def __init__(
        self,
        cache_subdir: str = "images",
        max_memory_items: int = 500,
        max_memory_bytes: int = 0,
        max_size: Optional[Tuple[int, int]] = None,
        corner_radius: int = 0,
        parent: Optional[QObject] = None
    ):
        """Initialize image cache

        Args:
            cache_subdir: Subdirectory within cache folder
            max_memory_items: Maximum number of pixmaps to keep in memory (secondary limit)
            max_memory_bytes: Maximum decoded memory in bytes (primary limit, 0 = unlimited)
            max_size: (max_w, max_h) to downscale images on load, or None for full-res
            corner_radius: Radius in cache-resolution pixels for pre-rounding corners
                at cache insertion time. 0 = no rounding. Eliminates per-paint
                QPainterPath allocation (8.95 GB churn per memray profile).
            parent: Parent QObject
        """
        super().__init__(parent)

        self._cache_dir = get_cache_dir() / cache_subdir
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _set_disk_write_disabled(e)

        # LRU cache of loaded pixmaps (OrderedDict maintains access order)
        self._pixmap_cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._max_memory_items = max_memory_items

        # Byte-based memory budget (primary constraint)
        self._max_memory_bytes = max_memory_bytes
        self._current_bytes = 0

        # Downscale limit for loaded images
        self._max_size = max_size

        # Pre-rounding: apply rounded corners at cache insertion time
        # so delegates can draw pixmaps directly without per-paint clip paths
        self._corner_radius = corner_radius

        # Track pending downloads to avoid duplicates
        self._pending: Set[str] = set()
        self._active_workers: list = []
        self._request_queue: list = []  # Queue for pending requests when at capacity

        # Thread safety
        self._mutex = QMutex()

        # Idle trimming: trim cache when idle for IDLE_TRIM_SECONDS
        self._last_access = time.monotonic()
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(IDLE_CHECK_INTERVAL_MS)
        self._idle_timer.timeout.connect(self._check_idle_trim)
        self._idle_timer.start()

        # Scroll-aware loading: when active, skip disk I/O in get_image()
        self._scroll_active = False

        budget_str = f"{max_memory_bytes / (1024*1024):.0f}MB" if max_memory_bytes else "unlimited"
        size_str = f"{max_size[0]}x{max_size[1]}" if max_size else "full-res"
        round_str = f", corner_radius={corner_radius}" if corner_radius else ""
        logger.info(
            f"ImageCache initialized: {cache_subdir}, "
            f"budget={budget_str}, max_items={max_memory_items}, max_size={size_str}{round_str}"
        )

    def _touch(self, key: str) -> None:
        """Move key to end of OrderedDict (mark as recently used)

        Must be called with mutex held.
        """
        self._pixmap_cache.move_to_end(key)
        self._last_access = time.monotonic()

    def _evict_one(self) -> bool:
        """Evict the oldest (LRU) item. Returns True if an item was evicted.

        Must be called with mutex held.
        """
        if not self._pixmap_cache:
            return False
        evicted_key, pixmap = self._pixmap_cache.popitem(last=False)
        self._current_bytes -= _pixmap_bytes(pixmap)
        pixmap.swap(QPixmap())  # Force immediate native cleanup
        return True

    def _evict_until_fits(self, new_bytes: int) -> None:
        """Evict LRU items until the new image fits within budget.

        Must be called with mutex held.
        """
        evicted_count = 0

        # Evict if over byte budget
        if self._max_memory_bytes > 0:
            while (
                self._current_bytes + new_bytes > self._max_memory_bytes
                and self._pixmap_cache
            ):
                self._evict_one()
                evicted_count += 1

        # Evict if over item count limit
        while len(self._pixmap_cache) >= self._max_memory_items:
            self._evict_one()
            evicted_count += 1

        if evicted_count > 1:
            logger.debug("LRU evicted %d items from memory cache", evicted_count)

    def _round_pixmap(self, pixmap: QPixmap) -> QPixmap:
        """Apply rounded corners to a pixmap (composited once at cache time).

        Creates a new ARGB pixmap with transparent corners. The cost of one
        QPainterPath + compositing per cache insertion replaces thousands of
        per-paint setClipPath allocations (8.95 GB churn eliminated).
        """
        rounded = QPixmap(pixmap.size())
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(
            0, 0, pixmap.width(), pixmap.height(),
            self._corner_radius, self._corner_radius,
        )
        p.setClipPath(path)
        p.drawPixmap(0, 0, pixmap)
        p.end()
        return rounded

    def _cache_put(self, key: str, pixmap: QPixmap) -> None:
        """Add item to cache with LRU eviction and optional corner rounding.

        Must be called with mutex held.
        """
        # Pre-round corners at cache insertion (eliminates per-paint
        # QPainterPath allocation — the #1 allocation churn source)
        if self._corner_radius > 0 and not pixmap.isNull():
            pixmap = self._round_pixmap(pixmap)

        new_bytes = _pixmap_bytes(pixmap)

        if key in self._pixmap_cache:
            # Already exists — subtract old size, update
            old_pixmap = self._pixmap_cache[key]
            self._current_bytes -= _pixmap_bytes(old_pixmap)
            old_pixmap.swap(QPixmap())
            self._pixmap_cache[key] = pixmap
            self._current_bytes += new_bytes
            self._touch(key)
        else:
            # New item — make room first
            self._evict_until_fits(new_bytes)
            self._pixmap_cache[key] = pixmap
            self._current_bytes += new_bytes

    def _check_idle_trim(self) -> None:
        """Timer callback: trim cache to 50% if idle for IDLE_TRIM_SECONDS."""
        if self._max_memory_bytes <= 0:
            return

        trimmed = False
        with QMutexLocker(self._mutex):
            elapsed = time.monotonic() - self._last_access
            if elapsed < IDLE_TRIM_SECONDS:
                return

            half_budget = self._max_memory_bytes // 2
            if self._current_bytes <= half_budget:
                return

            before = self._current_bytes
            while self._current_bytes > half_budget and self._pixmap_cache:
                self._evict_one()

            if before != self._current_bytes:
                logger.info(
                    f"Idle trim: {before / (1024*1024):.0f}MB → "
                    f"{self._current_bytes / (1024*1024):.0f}MB "
                    f"(idle {elapsed:.0f}s)"
                )
                trimmed = True

        if trimmed:
            # GC first so Python reference cycles release C++ wrappers,
            # then return freed pages to OS (outside mutex to avoid contention)
            import gc
            gc.collect(0)
            gc.collect(2)
            from .memory import release_memory_to_os
            release_memory_to_os()

    def set_memory_budget(self, max_bytes: int, max_items: int = 0) -> None:
        """Change the memory budget at runtime.

        Thread-safe. If the new budget is smaller than current usage,
        LRU items are evicted immediately.

        Args:
            max_bytes: New byte budget (0 = unlimited)
            max_items: New item limit (0 = keep current)
        """
        with QMutexLocker(self._mutex):
            self._max_memory_bytes = max_bytes
            if max_items > 0:
                self._max_memory_items = max_items

            # Evict until we fit the new budget
            if max_bytes > 0:
                while self._current_bytes > max_bytes and self._pixmap_cache:
                    self._evict_one()

            budget_str = f"{max_bytes / (1024*1024):.0f}MB" if max_bytes else "unlimited"
            logger.info(
                f"Memory budget updated: {budget_str}, "
                f"current={self._current_bytes / (1024*1024):.0f}MB, "
                f"items={len(self._pixmap_cache)}"
            )

    def set_scroll_active(self, active: bool) -> None:
        """Set scroll-active state for disk I/O gating.

        When active, get_image() queues async disk loads (progressive
        display during scroll). When inactive, disk-cached images load
        synchronously for instant display.
        """
        self._scroll_active = active

    def _sync_downscale(self, pixmap: QPixmap) -> QPixmap:
        """Downscale pixmap synchronously if it exceeds max_size limits."""
        if not self._max_size:
            return pixmap
        max_w, max_h = self._max_size
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(
                max_w, max_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return pixmap

    def get_image(self, url_or_path: str) -> Optional[QPixmap]:
        """Get image from cache, or start async loading

        Args:
            url_or_path: Image URL or local file path

        Returns:
            QPixmap if cached/loaded, None if loading in background
        """
        if not url_or_path:
            return None

        # Check if it's a local file path
        if url_or_path.startswith("file://") or Path(url_or_path).is_absolute():
            return self._load_local_file(url_or_path)

        # Use URL directly as cache key (query params already stripped at source)
        with QMutexLocker(self._mutex):
            self._last_access = time.monotonic()

            # Check memory cache
            if url_or_path in self._pixmap_cache:
                self._touch(url_or_path)  # Mark as recently used
                return self._pixmap_cache[url_or_path]

            # Check disk cache
            cache_path = self._get_cache_path(url_or_path)
            if cache_path.exists():
                if self._scroll_active:
                    # Scrolling: queue async disk load for progressive display.
                    # Items appear during scroll as workers complete (~5ms each),
                    # so most are in memory by the time scroll settles.
                    if url_or_path not in self._pending:
                        self._pending.add(url_or_path)
                        self._start_disk_load(url_or_path, cache_path)
                    return None
                # Not scrolling: sync load for instant display.
                # Most items already in cache from async-during-scroll;
                # only stragglers need this (well within 16ms budget).
                pixmap = QPixmap(str(cache_path))
                if not pixmap.isNull():
                    pixmap = self._sync_downscale(pixmap)
                    self._cache_put(url_or_path, pixmap)
                    self._pending.discard(url_or_path)
                    return pixmap
                # File exists but invalid — fall through to async download

            # Skip known-404 URLs entirely (no queue, no worker)
            with _not_found_lock:
                if url_or_path in _not_found_urls:
                    return None

            # Start async download if not already pending
            if url_or_path not in self._pending:
                self._pending.add(url_or_path)
                self._start_download(url_or_path, cache_path)

        return None

    def _load_local_file(self, path: str) -> Optional[QPixmap]:
        """Load image from local file path"""
        if path.startswith("file://"):
            # Use QUrl for cross-platform file:// parsing
            path = QUrl(path).toLocalFile()

        with QMutexLocker(self._mutex):
            # Check memory cache
            if path in self._pixmap_cache:
                self._touch(path)  # Mark as recently used
                return self._pixmap_cache[path]

            # Load from disk
            local_path = Path(path)
            if local_path.exists():
                pixmap = QPixmap(str(local_path))
                if not pixmap.isNull():
                    # Downscale if needed
                    if self._max_size:
                        max_w, max_h = self._max_size
                        if pixmap.width() > max_w or pixmap.height() > max_h:
                            pixmap = pixmap.scaled(
                                max_w, max_h,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                    self._cache_put(path, pixmap)
                    return pixmap
                else:
                    logger.warning(f"QPixmap is null for existing file: {path}")
            else:
                logger.debug(f"Local file does not exist: {path}")

        return None

    def get_cached_image(self, url: str) -> Optional[QPixmap]:
        """Get image only if already in MEMORY cache (no disk or async loading).

        Use this when you need a non-blocking check for already-loaded images.
        For images on disk but not in memory, use get_image() which loads async.

        Args:
            url: Image URL

        Returns:
            QPixmap if in memory cache, None otherwise
        """
        if not url:
            return None

        with QMutexLocker(self._mutex):
            if url in self._pixmap_cache:
                self._touch(url)  # Mark as recently used
                return self._pixmap_cache[url]

        return None

    def preload(self, urls: list) -> None:
        """Start loading multiple images in background

        Args:
            urls: List of image URLs to preload
        """
        for url in urls:
            if url:
                self.get_image(url)

    def clear_memory_cache(self) -> None:
        """Clear in-memory pixmap cache"""
        with QMutexLocker(self._mutex):
            # Swap-null all pixmaps to force immediate native cleanup
            for pixmap in self._pixmap_cache.values():
                pixmap.swap(QPixmap())
            self._pixmap_cache.clear()
            self._current_bytes = 0

    def remove_urls(self, urls: set) -> int:
        """Remove specific URLs from memory and disk cache.

        Args:
            urls: Set of URL strings to purge.

        Returns:
            Number of disk files deleted.
        """
        disk_deleted = 0
        with QMutexLocker(self._mutex):
            for url in urls:
                # Memory eviction
                if url in self._pixmap_cache:
                    pixmap = self._pixmap_cache.pop(url)
                    self._current_bytes -= _pixmap_bytes(pixmap)
                    pixmap.swap(QPixmap())

                # Disk deletion
                cache_path = self._get_cache_path(url)
                try:
                    if cache_path.exists():
                        cache_path.unlink()
                        disk_deleted += 1
                except OSError:
                    pass
        return disk_deleted

    def cancel_pending(self) -> None:
        """Cancel all pending/queued requests (does not stop running workers)"""
        with QMutexLocker(self._mutex):
            self._request_queue.clear()
            self._pending.clear()
            logger.debug("Cancelled all pending image requests")

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Shutdown cache and wait for running workers to complete.

        Call this before destroying the cache to prevent thread crashes.

        Args:
            timeout_ms: Max time to wait for each worker (default 5 seconds)
        """
        # Stop idle trim timer
        self._idle_timer.stop()

        # Cancel queued requests
        self.cancel_pending()

        # Wait for active workers to finish
        workers_to_wait = list(self._active_workers)
        for worker in workers_to_wait:
            if worker.isRunning():
                logger.debug("Waiting for worker to finish...")
                worker.wait(timeout_ms)
                if worker.isRunning():
                    logger.warning("Worker did not finish in time, terminating")
                    worker.terminate()
                    worker.wait(1000)

        self._active_workers.clear()
        logger.info("ImageCache shutdown complete")

    def get_cache_stats(self) -> dict:
        """Get cache statistics for debugging

        Returns:
            Dict with cache size info
        """
        with QMutexLocker(self._mutex):
            return {
                "memory_items": len(self._pixmap_cache),
                "max_memory_items": self._max_memory_items,
                "memory_bytes": self._current_bytes,
                "max_memory_bytes": self._max_memory_bytes,
                "pending_downloads": len(self._pending),
                "active_workers": len(self._active_workers),
                "queued_requests": len(self._request_queue),
            }

    def get_disk_path(self, url: str) -> Optional[Path]:
        """Get the disk cache path for a URL if the file exists.

        Returns the path without loading into memory. Useful for
        full-resolution loading (e.g. image viewer) that bypasses
        the memory cache.
        """
        if not url:
            return None
        cache_path = self._get_cache_path(url)
        return cache_path if cache_path.exists() else None

    def _get_cache_path(self, url: str) -> Path:
        """Get disk cache path for URL

        Note: Query params are stripped at source (steamscraper) so URLs
        are already clean when they reach the cache.
        """
        # Create hash-based filename to avoid path issues
        url_hash = hashlib.sha256(url.encode()).hexdigest()

        # Extract extension from URL
        parsed = urlparse(url)
        path = parsed.path
        ext = Path(path).suffix or ".jpg"

        return self._cache_dir / f"{url_hash}{ext}"

    def _start_download(self, url: str, cache_path: Path) -> None:
        """Start background download for URL, queuing if at capacity"""
        # Skip network downloads when offline
        try:
            from ..core.network_monitor import get_network_monitor
            if not get_network_monitor().is_online:
                self._pending.discard(url)
                return
        except RuntimeError:
            pass  # Monitor not initialized yet

        # Clean up finished workers
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

        # Queue if at capacity
        if len(self._active_workers) >= self.MAX_CONCURRENT:
            self._request_queue.append((url, cache_path, False))  # False = network download
            # Cap queue to prevent unbounded growth during scroll
            while len(self._request_queue) > 200:
                dropped_url, _, _ = self._request_queue.pop(0)
                self._pending.discard(dropped_url)
            if len(self._request_queue) % 50 == 0:
                logger.debug("Download queue: %d pending", len(self._request_queue))
            return

        self._start_download_internal(url, cache_path)

    def _start_download_internal(self, url: str, cache_path: Path) -> None:
        """Actually start the download worker (internal, no capacity check)"""
        # Create worker WITHOUT parent to prevent destruction while running
        worker = ImageLoaderWorker(url, cache_path, max_size=self._max_size, parent=None)
        worker.image_loaded.connect(self._on_image_loaded)
        worker.image_failed.connect(self._on_image_failed)
        worker.image_not_found.connect(self._on_image_not_found)
        worker.finished.connect(lambda: self._cleanup_worker(worker))

        self._active_workers.append(worker)
        worker.start()

    def _start_disk_load(self, url: str, cache_path: Path) -> None:
        """Load image from disk cache asynchronously (no network fetch).

        Used when file exists on disk but not in memory cache.
        Avoids blocking the UI thread with synchronous QPixmap load.
        """
        # Clean up finished workers
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

        # Queue if at capacity
        if len(self._active_workers) >= self.MAX_CONCURRENT:
            self._request_queue.append((url, cache_path, True))  # True = disk-only load
            # Cap queue to prevent unbounded growth during fast scroll
            while len(self._request_queue) > 200:
                dropped_url, _, _ = self._request_queue.pop(0)
                self._pending.discard(dropped_url)
            return

        self._start_disk_load_internal(url, cache_path)

    def _start_disk_load_internal(self, url: str, cache_path: Path) -> None:
        """Actually start the disk load worker (internal, no capacity check)"""
        # Create worker WITHOUT parent to prevent destruction while running
        worker = ImageLoaderWorker(url, cache_path, max_size=self._max_size, parent=None)
        worker._skip_download = True  # Only load from disk, don't download
        worker.image_loaded.connect(self._on_image_loaded)
        worker.image_failed.connect(self._on_image_failed)
        worker.image_not_found.connect(self._on_image_not_found)
        worker.finished.connect(lambda: self._cleanup_worker(worker))

        self._active_workers.append(worker)
        worker.start()

    def _on_image_loaded(self, url: str, pixmap: QPixmap) -> None:
        """Handle successful image load"""
        with QMutexLocker(self._mutex):
            self._cache_put(url, pixmap)
            self._pending.discard(url)

        # Emit signal for UI update
        self.image_loaded.emit(url, pixmap)

    def _on_image_failed(self, url: str, error: str) -> None:
        """Handle failed image load"""
        logger.debug(f"Failed to load image {url}: {error}")

        with QMutexLocker(self._mutex):
            self._pending.discard(url)

        self.image_failed.emit(url, error)

    def _on_image_not_found(self, url: str, error: str) -> None:
        """Handle HTTP 404 — image URL no longer exists on server"""
        logger.debug(f"Image not found (404): {url}")

        with QMutexLocker(self._mutex):
            self._pending.discard(url)

        self.image_not_found.emit(url, error)

    def _cleanup_worker(self, worker: ImageLoaderWorker) -> None:
        """Clean up finished worker and process queue"""
        if worker in self._active_workers:
            self._active_workers.remove(worker)
        worker.deleteLater()

        # Process queued request if any
        self._process_queue()

    def _process_queue(self) -> None:
        """Start next queued request if under capacity"""
        if not self._request_queue:
            return

        # Clean up finished workers first
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

        if len(self._active_workers) >= self.MAX_CONCURRENT:
            return

        url, cache_path, disk_only = self._request_queue.pop()  # LIFO: prioritize most recent

        if disk_only:
            self._start_disk_load_internal(url, cache_path)
        else:
            self._start_download_internal(url, cache_path)


# Global image cache instances
_cover_cache: Optional[ImageCache] = None
_screenshot_cache: Optional[ImageCache] = None


def get_cover_cache() -> ImageCache:
    """Get global cover image cache

    Covers displayed at ~200×300, cached at 2× for HiDPI (400×600).
    Budget: 200 MB (~218 covers at ~0.92 MB each decoded).
    """
    global _cover_cache
    if _cover_cache is None:
        _cover_cache = ImageCache(
            "covers",
            max_memory_items=DEFAULT_COVER_CACHE_SIZE,
            max_memory_bytes=DEFAULT_COVER_BUDGET_BYTES,
            max_size=(400, 600),
            corner_radius=12,
        )
    return _cover_cache


def get_screenshot_cache() -> ImageCache:
    """Get global screenshot image cache

    Screenshots displayed at ~400×225, cached at 2× for HiDPI (800×450).
    Budget: 100 MB (~70 screenshots at 1.4 MB each after downscale).
    """
    global _screenshot_cache
    if _screenshot_cache is None:
        _screenshot_cache = ImageCache(
            "screenshots",
            max_memory_items=DEFAULT_SCREENSHOT_CACHE_SIZE,
            max_memory_bytes=DEFAULT_SCREENSHOT_BUDGET_BYTES,
            max_size=(800, 450),
            corner_radius=12,
        )
    return _screenshot_cache


_hero_cache: Optional[ImageCache] = None


def get_hero_cache() -> ImageCache:
    """Get global hero/background image cache

    Hero banners displayed at 1920×~200, cached at 1920×400.
    Budget: 20 MB (~6-7 hero images).
    """
    global _hero_cache
    if _hero_cache is None:
        _hero_cache = ImageCache(
            "heroes",
            max_memory_items=10,
            max_memory_bytes=DEFAULT_HERO_BUDGET_BYTES,
            max_size=(1920, 400),
        )
    return _hero_cache


def shutdown_all_caches() -> None:
    """Shutdown all global image caches and close HTTP session.

    Call this during application shutdown to prevent thread crashes.
    """
    global _cover_cache, _screenshot_cache, _hero_cache

    if _cover_cache is not None:
        _cover_cache.shutdown()
        _cover_cache = None

    if _screenshot_cache is not None:
        _screenshot_cache.shutdown()
        _screenshot_cache = None

    if _hero_cache is not None:
        _hero_cache.shutdown()
        _hero_cache = None

    close_session()
    logger.info("All image caches shut down")


def compute_auto_budgets(
    cover_density: int,
    screenshot_density: int,
    viewport_w: int,
    viewport_h: int,
) -> Tuple[int, int]:
    """Compute optimal and minimum RAM cache budgets in MB.

    Returns a combined total for covers + screenshots + fixed hero/description.
    Calculation matches delegate sizeHint logic (PADDING=8, TITLE_HEIGHT=24, spacing=8).

    Args:
        cover_density: Cover grid density in pixels
        screenshot_density: Screenshot grid density in pixels
        viewport_w: Viewport width in pixels
        viewport_h: Viewport height in pixels

    Returns:
        (optimal_mb, minimum_mb) tuple
    """
    SPACING = 8
    PADDING = 8
    TITLE_HEIGHT = 24
    COVER_BYTES = 400 * 600 * 4    # 0.92 MB (400x600 RGBA)
    SCREENSHOT_BYTES = 800 * 450 * 4  # 1.37 MB (800x450 RGBA)
    FIXED_MB = 50  # hero + description caches

    # Cover grid
    cover_item_w = cover_density + 2 * PADDING
    cover_item_h = int(cover_density * 1.5) + 2 * PADDING + TITLE_HEIGHT
    cover_cols = max(1, viewport_w // (cover_item_w + SPACING))
    cover_rows = max(1, viewport_h // (cover_item_h + SPACING) + 1)
    cover_visible = cover_cols * cover_rows
    cover_optimal = cover_visible * 8 * COVER_BYTES
    cover_minimum = cover_visible * COVER_BYTES

    # Screenshot grid
    ss_item_w = screenshot_density + 2 * PADDING
    ss_item_h = int(screenshot_density * 9 / 16) + 2 * PADDING + TITLE_HEIGHT
    ss_cols = max(1, viewport_w // (ss_item_w + SPACING))
    ss_rows = max(1, viewport_h // (ss_item_h + SPACING) + 1)
    ss_visible = ss_cols * ss_rows
    ss_optimal = ss_visible * 8 * SCREENSHOT_BYTES
    ss_minimum = ss_visible * SCREENSHOT_BYTES

    MAX_AUTO_BUDGET_MB = 400  # Cap to limit fragmentation from large alloc/free cycles
    optimal_mb = min(MAX_AUTO_BUDGET_MB, max(100, (cover_optimal + ss_optimal) // (1024 * 1024) + FIXED_MB))
    minimum_mb = max(64, (cover_minimum + ss_minimum) // (1024 * 1024) + FIXED_MB)

    return (optimal_mb, minimum_mb)


def apply_cache_budgets(config) -> None:
    """Apply RAM cache budgets from config to global cache singletons.

    Reads cache.ram_cache_mode and cache.ram_cache_manual_mb.
    In auto mode, computes optimal budget from grid density + window size.
    In manual mode, uses the user-specified value.
    Splits budget 2:1 between covers and screenshots.

    Args:
        config: Config instance
    """
    mode = config.get("cache.ram_cache_mode", "auto")
    manual_mb = config.get("cache.ram_cache_manual_mb", 0)

    cover_density = config.get("appearance.cover_grid_density", 250)
    screenshot_density = config.get("appearance.screenshot_grid_density", 250)
    viewport_w = config.get("ui.window_width", 1200)
    viewport_h = config.get("ui.window_height", 800)

    if mode == "manual" and manual_mb > 0:
        total_mb = manual_mb
    else:
        optimal_mb, _ = compute_auto_budgets(
            cover_density, screenshot_density, viewport_w, viewport_h
        )
        total_mb = optimal_mb

    # Subtract fixed hero+description budget (50 MB) before splitting
    fixed_mb = 50
    distributable_mb = max(32, total_mb - fixed_mb)

    # Split 2:1 between covers and screenshots
    cover_mb = int(distributable_mb * 2 / 3)
    screenshot_mb = distributable_mb - cover_mb

    cover_bytes = max(cover_mb * 1024 * 1024, DEFAULT_COVER_BUDGET_BYTES)
    screenshot_bytes = max(screenshot_mb * 1024 * 1024, DEFAULT_SCREENSHOT_BUDGET_BYTES)

    # Apply to caches (only if they've been initialized)
    if _cover_cache is not None:
        _cover_cache.set_memory_budget(cover_bytes)
    if _screenshot_cache is not None:
        _screenshot_cache.set_memory_budget(screenshot_bytes)

    logger.info(
        f"Cache budgets applied ({mode}): total={total_mb}MB, "
        f"covers={cover_mb}MB, screenshots={screenshot_mb}MB"
    )
