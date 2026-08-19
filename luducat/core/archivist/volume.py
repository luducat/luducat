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

try:
    _("")
    N_("")
except NameError:
    def _(s): return s
    def N_(s): return s

VALID_ORGANIZATIONS = ("flat", "store-slug", "store-title", "custom")

# Custom layout template variables (lgogdownloader-style %var% tokens).
# Keys are the tokens; values document them for tooltips/help text --
# N_ marks them for extraction, the settings tab translates at render.
CUSTOM_LAYOUT_VARIABLES = {
    "%library%": N_("store name, lower case (gog)"),
    "%library_upper%": N_("store name, upper case (GOG)"),
    "%slug%": N_("game slug with underscores (baldurs_gate_3)"),
    "%slug_firstletter%": N_("first letter of the slug, digits become 0"),
    "%title%": N_("game title as displayed (Baldur's Gate 3)"),
    "%appid%": N_("store product id"),
}

DEFAULT_CUSTOM_LAYOUT = "%library%/%slug%"

_TEMPLATE_TOKEN_RE = re.compile(r"%[a-z_]+%")


def _slugify(title: str) -> str:
    """Convert a game title to a filesystem-safe slug.

    Lowercases, strips accents, replaces non-alphanumeric runs with underscores,
    trims leading/trailing underscores. Matches GOG slug convention, which
    drops apostrophes entirely (Baldur's Gate 3 -> baldurs_gate_3) instead
    of turning them into separators.
    """
    nfkd = unicodedata.normalize("NFKD", title)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii").lower()
    ascii_text = ascii_text.replace("'", "")
    slug = _SLUG_STRIP.sub("_", ascii_text).strip("_")
    return slug or "unnamed"


def _sanitize_title(title: str) -> str:
    """Make a title safe for use as a directory name, preserving readability."""
    safe = _UNSAFE_CHARS.sub("_", title).rstrip(". ")
    return safe or "unnamed"


def _first_letter(slug: str) -> str:
    # A-Z sharding: digits collapse into a "0" bucket (lgogdownloader
    # convention, gamedetails.cpp makeFilepath)
    if not slug:
        return "0"
    return "0" if slug[0].isdigit() else slug[0]


def validate_custom_layout(template: str) -> "str | None":
    """Check a custom layout template. Returns an error message or None.

    Called on settings input before the value is accepted, and again by
    VolumeManager so a hand-edited config cannot smuggle in traversal.
    """
    if not template or not template.strip():
        return _("template is empty")
    if "\\" in template:
        return _("use forward slashes, not backslashes")
    if template.startswith("/"):
        return _("template must be relative to the base folder")

    unknown = [
        token for token in _TEMPLATE_TOKEN_RE.findall(template)
        if token not in CUSTOM_LAYOUT_VARIABLES
    ]
    if unknown:
        return _("unknown variable: {names}").format(
            names=", ".join(sorted(set(unknown))))
    if "%" in _TEMPLATE_TOKEN_RE.sub("", template):
        # Quoted '%' so babel does not mistake the sentence for a
        # python-format string ("% c" parses as a %c placeholder)
        return _("stray '%' (variables are written as %name%)")

    for segment in template.split("/"):
        if not segment.strip():
            return _("empty path segment (double or trailing slash)")
        if segment in (".", ".."):
            return _("path segments . and .. are not allowed")
    return None


def render_custom_layout(
    template: str,
    store_name: str,
    store_app_id: str,
    game_title: str,
) -> str:
    """Substitute layout variables and return a clean relative POSIX path.

    Every variable value is sanitized like a directory name: store name
    and app id come from plugin data, and a hostile plugin must not be
    able to smuggle separators or dot segments into the volume path.
    """
    slug = _slugify(game_title)
    values = {
        "%library%": _sanitize_title(store_name.lower()),
        "%library_upper%": _sanitize_title(store_name.upper()),
        "%slug%": slug,
        "%slug_firstletter%": _first_letter(slug),
        "%title%": _sanitize_title(game_title),
        "%appid%": _sanitize_title(str(store_app_id or "")),
    }
    out = template
    for token, value in values.items():
        out = out.replace(token, value)
    segments = [seg.strip() for seg in out.split("/")]
    return "/".join(
        seg for seg in segments if seg and seg not in (".", ".."))


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
        custom_layout: "str | None" = None,
    ) -> None:
        if organization not in VALID_ORGANIZATIONS:
            raise ValueError(
                f"Unknown organization {organization!r}, "
                f"expected one of {VALID_ORGANIZATIONS}"
            )
        self.base_path = Path(base_path)
        self.organization = organization
        self.custom_layout = custom_layout or DEFAULT_CUSTOM_LAYOUT
        if organization == "custom":
            error = validate_custom_layout(self.custom_layout)
            if error:
                raise ValueError(f"Invalid custom layout: {error}")

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

        if self.organization == "custom":
            rel_dir = render_custom_layout(
                self.custom_layout, store_name, store_app_id, game_title)
            return str(PurePosixPath(rel_dir, filename)) if rel_dir else filename

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
        full = self._contained(self.base_path / Path(rel))
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def _contained(self, full: Path) -> Path:
        """Refuse any path that resolves outside the volume.

        Inputs are sanitized upstream, but filenames flow in from CDN
        redirects and plugin metadata -- one containment check here
        covers every write path.
        """
        base = self.base_path.resolve()
        resolved = full.resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError(
                f"Path escapes archive volume: {full}")
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

        A same-name file already in the volume is only replaced when the
        sizes match (a re-download of the same release). On a size
        mismatch the old file is parked next to the new one -- GOG
        re-signs installers without bumping the build number, and an
        archive that predates the manifest may hold variants the
        download filename cannot distinguish. Never destroy what a
        preservation archive already has.
        """
        rel = self.relative_path(store_name, store_app_id, game_title, filename)
        dest = self._contained(self.base_path / Path(rel))
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            source_size = source_path.stat().st_size
            if dest.stat().st_size != source_size:
                parked = self._parking_name(dest)
                os.rename(dest, parked)  # rename keeps the original mtime

        try:
            # Same filesystem — atomic rename, preserves timestamps
            os.rename(source_path, dest)
        except OSError:
            # Cross-filesystem — copy2 preserves timestamps, then remove source
            shutil.copy2(source_path, dest)
            os.unlink(source_path)

        return dest, rel

    @staticmethod
    def _parking_name(dest: Path) -> Path:
        """Non-colliding sibling name for a displaced file (.old, .old.1, ...).

        The .old suffix matches what lgogdownloader leaves behind, so
        existing archives already treat it as a parked previous version.
        """
        parked = dest.with_name(dest.name + ".old")
        counter = 0
        while parked.exists():
            counter += 1
            parked = dest.with_name(f"{dest.name}.old.{counter}")
        return parked


def volume_manager_from_config(config) -> VolumeManager:
    """Build a VolumeManager from the downloads.* config keys.

    A corrupt custom layout in a hand-edited config falls back to
    store-slug (logged) rather than killing every download path.
    """
    import logging

    from luducat.core.config import get_default_archive_path

    archive_path = config.get("downloads.archive_path", "")
    if not archive_path:
        archive_path = str(get_default_archive_path())
    organization = config.get("downloads.folder_organization", "store-slug")
    custom_layout = config.get("downloads.custom_layout", DEFAULT_CUSTOM_LAYOUT)

    if organization == "custom":
        error = validate_custom_layout(custom_layout)
        if error:
            logging.getLogger(__name__).warning(
                "Invalid downloads.custom_layout (%s): %r -- "
                "falling back to store-slug", error, custom_layout)
            organization = "store-slug"

    return VolumeManager(
        base_path=Path(archive_path),
        organization=organization,
        custom_layout=custom_layout if organization == "custom" else None,
    )
