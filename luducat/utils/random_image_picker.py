# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# random_image_picker.py

"""Random image picker for sync dialog distraction picture.

Provides O(1) image selection from cache directories with priority
for current game context.
"""

import hashlib
import random
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from ..core.config import get_cache_dir


class RandomImagePicker:
    """O(1) random image selection from cache directories.

    Priority stack:
    1. Current game screenshot (if cached)
    2. Current game cover (if cached)
    3. Random from cache
    4. App icon placeholder (fallback)

    Usage:
        picker = RandomImagePicker()
        picker.refresh_cache_index()  # Call once at dialog open

        # During sync, update context when processing each game
        picker.set_current_game(cover_url, screenshot_urls)

        # Get image to display
        image_path = picker.get_next_image()
    """

    # Supported image extensions
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def __init__(self):
        self._cache_dir = get_cache_dir()
        self._cover_dir = self._cache_dir / "covers"
        self._screenshot_dir = self._cache_dir / "screenshots"
        self._hero_dir = self._cache_dir / "heroes"

        # Flat list of all cached image paths (built once at dialog open)
        self._all_images: List[Path] = []

        # Current game context
        self._current_cover_path: Optional[Path] = None
        self._current_screenshot_paths: List[Path] = []

        # Track last returned image to avoid immediate repeats
        self._last_image: Optional[Path] = None

    def refresh_cache_index(self) -> int:
        """Build flat list of all cached images.

        Call once when sync dialog opens, not during sync.

        Returns:
            Number of cached images found
        """
        self._all_images.clear()

        for cache_dir in [self._screenshot_dir, self._cover_dir]:
            if cache_dir.exists():
                for path in cache_dir.iterdir():
                    if path.is_file() and path.suffix.lower() in self.IMAGE_EXTENSIONS:
                        self._all_images.append(path)

        return len(self._all_images)

    def set_current_game(
        self,
        cover_url: Optional[str] = None,
        screenshot_urls: Optional[List[str]] = None,
    ) -> None:
        """Set context for current game being processed.

        Args:
            cover_url: URL of the game's cover image
            screenshot_urls: List of screenshot URLs for the game
        """
        # Convert URLs to cache paths
        self._current_cover_path = None
        self._current_screenshot_paths.clear()

        if cover_url:
            cover_path = self._url_to_cache_path(cover_url, self._cover_dir)
            if cover_path and cover_path.exists():
                self._current_cover_path = cover_path

        if screenshot_urls:
            for url in screenshot_urls:
                ss_path = self._url_to_cache_path(url, self._screenshot_dir)
                if ss_path and ss_path.exists():
                    self._current_screenshot_paths.append(ss_path)

    def clear_current_game(self) -> None:
        """Clear current game context."""
        self._current_cover_path = None
        self._current_screenshot_paths.clear()

    def get_next_image(self) -> Optional[Path]:
        """Get next image path using priority stack.

        Priority:
        1. Current game screenshot (random from cached)
        2. Current game cover (if cached)
        3. Random image from cache
        4. None (caller should use app icon)

        Returns:
            Path to image file, or None if no images available
        """
        # Priority 1: Current game screenshot (random)
        if self._current_screenshot_paths:
            valid_screenshots = [p for p in self._current_screenshot_paths if p.exists()]
            if valid_screenshots:
                choice = random.choice(valid_screenshots)
                self._last_image = choice
                return choice

        # Priority 2: Current game cover
        if self._current_cover_path and self._current_cover_path.exists():
            self._last_image = self._current_cover_path
            return self._current_cover_path

        # Priority 3: Random from cache (avoid immediate repeat)
        if self._all_images:
            candidates = self._all_images
            if len(candidates) > 1 and self._last_image in candidates:
                candidates = [p for p in candidates if p != self._last_image]
            if candidates:
                choice = random.choice(candidates)
                self._last_image = choice
                return choice

        # Priority 4: No images - return None (caller uses app icon)
        return None

    def get_current_game_image(self) -> Optional[Path]:
        """Get image specifically from current game context.

        Returns:
            Path to current game's cover or screenshot, or None
        """
        if self._current_cover_path and self._current_cover_path.exists():
            return self._current_cover_path

        if self._current_screenshot_paths:
            valid = [p for p in self._current_screenshot_paths if p.exists()]
            if valid:
                return random.choice(valid)

        return None

    def has_images(self) -> bool:
        """Check if any cached images are available."""
        return bool(self._all_images)

    def get_cache_stats(self) -> dict:
        """Get statistics about cached images.

        Returns:
            Dict with counts per cache directory
        """
        def count_images(directory: Path) -> int:
            if not directory.exists():
                return 0
            return sum(
                1 for p in directory.iterdir()
                if p.is_file() and p.suffix.lower() in self.IMAGE_EXTENSIONS
            )

        return {
            "covers": count_images(self._cover_dir),
            "screenshots": count_images(self._screenshot_dir),
            "heroes": count_images(self._hero_dir),
            "total": len(self._all_images),
        }

    def _url_to_cache_path(self, url: str, cache_dir: Path) -> Optional[Path]:
        """Convert URL to cache file path.

        Uses SHA-256 hash of URL as filename (matching ImageCache behavior).
        """
        if not url:
            return None

        url_hash = hashlib.sha256(url.encode()).hexdigest()
        parsed = urlparse(url)
        ext = Path(parsed.path).suffix or ".jpg"

        return cache_dir / f"{url_hash}{ext}"
