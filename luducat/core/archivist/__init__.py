# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Archivist — archive volume management and manifest tracking.

Layer 1 of the Luducat Downloader architecture. Manages where downloaded files
are stored, how they're organized, and tracks them in a manifest database.
"""

from luducat.core.archivist.types import (
    ArchiveType,
    ArchiveEntry,
    ArchiveRequest,
    GameDownloadInfo,
    DownloadTarget,
)
from luducat.core.archivist.volume import VolumeManager
from luducat.core.archivist.manager import ArchivistManager

__all__ = [
    "ArchiveType",
    "ArchiveEntry",
    "ArchiveRequest",
    "GameDownloadInfo",
    "DownloadTarget",
    "VolumeManager",
    "ArchivistManager",
]
