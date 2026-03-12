# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# cache_manager.py

"""Cache manager for luducat.

Handles cache size enforcement and cleanup on startup.
Respects offline mode setting - when enabled, caches are never auto-deleted.
"""

import logging
from pathlib import Path
from typing import List, Tuple

from .config import Config, get_cache_dir

logger = logging.getLogger(__name__)


def get_dir_size(path: Path) -> int:
    """Get total size of directory in bytes.

    Args:
        path: Directory path

    Returns:
        Total size in bytes
    """
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def get_files_by_age(path: Path) -> List[Tuple[float, Path]]:
    """Get files sorted by modification time (oldest first).

    Args:
        path: Directory path

    Returns:
        List of (mtime, path) tuples, oldest first
    """
    files = []
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    mtime = f.stat().st_mtime
                    files.append((mtime, f))
                except OSError:
                    pass
    files.sort(key=lambda x: x[0])  # Sort by mtime, oldest first
    return files


def enforce_cache_limit(
    cache_dir: Path,
    max_size_mb: int,
    name: str = "cache"
) -> int:
    """Enforce cache size limit by deleting oldest files.

    Args:
        cache_dir: Path to cache directory
        max_size_mb: Maximum size in megabytes
        name: Name for logging

    Returns:
        Number of files deleted
    """
    if not cache_dir.exists():
        return 0

    max_size_bytes = max_size_mb * 1024 * 1024
    current_size = get_dir_size(cache_dir)

    if current_size <= max_size_bytes:
        logger.debug(f"{name}: {current_size / (1024*1024):.1f}MB <= {max_size_mb}MB limit")
        return 0

    logger.info(
        f"{name}: {current_size / (1024*1024):.1f}MB exceeds {max_size_mb}MB limit, "
        f"cleaning up..."
    )

    # Get files sorted by age (oldest first)
    files = get_files_by_age(cache_dir)

    deleted = 0
    freed = 0

    for mtime, file_path in files:
        if current_size - freed <= max_size_bytes:
            break

        try:
            file_size = file_path.stat().st_size
            file_path.unlink()
            freed += file_size
            deleted += 1
            logger.debug(f"Deleted: {file_path.name} ({file_size} bytes)")
        except Exception as e:
            logger.warning(f"Failed to delete {file_path}: {e}")

    logger.info(
        f"{name}: Deleted {deleted} files, freed {freed / (1024*1024):.1f}MB"
    )
    return deleted


def enforce_cache_limits(config: Config) -> dict:
    """Enforce all cache size limits based on config.

    Respects offline mode - when enabled, caches are not auto-cleaned.

    Args:
        config: Application configuration

    Returns:
        Dict with cleanup statistics
    """
    # Check offline mode - if enabled, don't auto-delete
    if config.get("cache.offline_mode", True):
        logger.debug("Offline mode enabled - skipping automatic cache cleanup")
        return {"offline_mode": True, "skipped": True}

    cache_dir = get_cache_dir()

    stats = {
        "offline_mode": False,
        "skipped": False,
        "covers_deleted": 0,
        "screenshots_deleted": 0,
    }

    # Enforce thumbnail/cover cache limit
    thumb_max = config.get("cache.thumbnail_max_size_mb", 500)
    covers_dir = cache_dir / "covers"
    stats["covers_deleted"] = enforce_cache_limit(covers_dir, thumb_max, "Covers cache")

    # Enforce screenshot cache limit
    screenshot_max = config.get("cache.screenshot_max_size_mb", 2000)
    screenshots_dir = cache_dir / "screenshots"
    stats["screenshots_deleted"] = enforce_cache_limit(
        screenshots_dir, screenshot_max, "Screenshots cache"
    )

    return stats


def get_cache_stats(config: Config) -> dict:
    """Get current cache statistics.

    Args:
        config: Application configuration

    Returns:
        Dict with cache sizes and limits
    """
    cache_dir = get_cache_dir()

    covers_dir = cache_dir / "covers"
    screenshots_dir = cache_dir / "screenshots"
    plugins_dir = cache_dir / "plugins"

    covers_size = get_dir_size(covers_dir)
    screenshots_size = get_dir_size(screenshots_dir)
    plugins_size = get_dir_size(plugins_dir)

    return {
        "covers_size_mb": covers_size / (1024 * 1024),
        "covers_limit_mb": config.get("cache.thumbnail_max_size_mb", 500),
        "screenshots_size_mb": screenshots_size / (1024 * 1024),
        "screenshots_limit_mb": config.get("cache.screenshot_max_size_mb", 2000),
        "plugins_size_mb": plugins_size / (1024 * 1024),
        "total_size_mb": (covers_size + screenshots_size + plugins_size) / (1024 * 1024),
        "offline_mode": config.get("cache.offline_mode", True),
    }
