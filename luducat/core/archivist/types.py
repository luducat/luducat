# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Type definitions for the archivist subsystem."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class ArchiveType(enum.Enum):
    """Classification of archived files."""

    GAME_INSTALLER = "game_installer"
    GAME_PATCH = "game_patch"
    GAME_DLC = "game_dlc"
    GAME_EXTRA = "game_extra"
    PLATFORM = "platform"
    TOOL = "tool"


@dataclass(slots=True)
class ArchiveEntry:
    """A file tracked in the archive manifest (maps to `archives` table row)."""

    id: str
    archive_type: ArchiveType
    filename: str
    relative_path: str  # POSIX separators always, relative to volume root
    size_bytes: int
    checksum_sha256: str
    downloaded_at: datetime

    # Optional fields
    store_name: Optional[str] = None
    store_app_id: Optional[str] = None
    version: Optional[str] = None
    original_download_url: Optional[str] = None
    remote_timestamp: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None


@dataclass(slots=True)
class ArchiveRequest:
    """A request to download and archive a file.

    Created by store handlers (L3/L4), consumed by the download manager (L2),
    which hands the completed file to the archivist (L1) for manifest registration.
    """

    url: str
    archive_type: ArchiveType
    store_name: str
    store_app_id: str
    game_title: str
    filename: str

    # Optional
    expected_size: Optional[int] = None
    checksum_sha256: Optional[str] = None
    version: Optional[str] = None
    cookies: Optional[dict[str, str]] = None
    headers: Optional[dict[str, str]] = None
    metadata: Optional[dict[str, Any]] = None


@dataclass(slots=True)
class GameDownloadInfo:
    """Available downloads for a single game from a store.

    Produced by store handlers. Each list contains dicts with at minimum:
    - name: str (display name)
    - platform: str (windows/linux/mac)
    - size: int (bytes)
    - downlink: str (store-specific download URL, not final CDN URL)

    Store handlers may add extra fields (version, language, etc.).
    """

    game_title: str
    store_name: str
    store_app_id: str
    installers: list[dict[str, Any]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    extras: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class DownloadTarget:
    """A resolved game download — one game, multiple files.

    Produced by store handlers (L3/L4). Consumed by DownloadManager (L2).
    L2 creates one download_group row and one download row per file.
    """

    game_title: str
    store_name: str
    store_app_id: str
    icon_url: Optional[str] = None
    files: list[ArchiveRequest] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
