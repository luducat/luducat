# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Volume management for the archivist — path resolution and folder organization."""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

VALID_ORGANIZATIONS = ("flat", "store-slug", "store-title")


def _slugify(title: str) -> str:
    """Convert a game title to a filesystem-safe slug.

    Lowercases, strips accents, replaces non-alphanumeric runs with underscores,
    trims leading/trailing underscores. Matches GOG slug convention.
    """
    nfkd = unicodedata.normalize("NFKD", title)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("_", ascii_text).strip("_")
    return slug or "unnamed"


def _sanitize_title(title: str) -> str:
    """Make a title safe for use as a directory name, preserving readability."""
    safe = _UNSAFE_CHARS.sub("_", title).rstrip(". ")
    return safe or "unnamed"


class VolumeManager:
    """Manages archive storage paths and folder organization.

    Args:
        base_path: Root directory for the archive volume.
        organization: How to organize files. One of:
            - "flat": All files in base_path root.
            - "store-slug": base_path/store/slugified-title/file
            - "store-title": base_path/store/Original Title/file
    """

    def __init__(
        self,
        base_path: Path,
        organization: str = "store-slug",
    ) -> None:
        if organization not in VALID_ORGANIZATIONS:
            raise ValueError(
                f"Unknown organization {organization!r}, "
                f"expected one of {VALID_ORGANIZATIONS}"
            )
        self.base_path = Path(base_path)
        self.organization = organization

    def relative_path(
        self,
        store_name: str,
        store_app_id: str,
        game_title: str,
        filename: str,
    ) -> str:
        """Compute the POSIX relative path for a file within the volume.

        Always uses forward slashes. Stored in the archives DB table.
        """
        if self.organization == "flat":
            return filename

        if self.organization == "store-slug":
            game_dir = _slugify(game_title)
        else:  # store-title
            game_dir = _sanitize_title(game_title)

        return str(PurePosixPath(store_name, game_dir, filename))

    def resolve_path(
        self,
        store_name: str,
        store_app_id: str,
        game_title: str,
        filename: str,
    ) -> Path:
        """Resolve the absolute filesystem path for a file and create parent dirs.

        Returns a Path using the OS-native separator.
        """
        rel = self.relative_path(store_name, store_app_id, game_title, filename)
        # Convert POSIX relative path to OS path
        full = self.base_path / Path(rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def move_to_volume(
        self,
        source_path: Path,
        store_name: str,
        store_app_id: str,
        game_title: str,
        filename: str,
    ) -> tuple[Path, str]:
        """Move a completed download to its final location in the volume.

        Preserves file timestamps. Returns (absolute_path, posix_relative_path).
        Uses os.rename() for same-filesystem moves (inherently preserves
        timestamps), shutil.copy2() + os.unlink() for cross-filesystem.
        """
        rel = self.relative_path(store_name, store_app_id, game_title, filename)
        dest = self.base_path / Path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Same filesystem — atomic rename, preserves timestamps
            os.rename(source_path, dest)
        except OSError:
            # Cross-filesystem — copy2 preserves timestamps, then remove source
            shutil.copy2(source_path, dest)
            os.unlink(source_path)

        return dest, rel
