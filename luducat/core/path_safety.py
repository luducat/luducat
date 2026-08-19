# This file is part of the luducat Project.
# License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#

"""Path containment checks for archive extraction and untrusted path components.

Every code path that joins an untrusted string (ZIP member name, tar
member name, backup manifest entry, CDN filename) onto a base directory
must verify the result does not escape the base. This module provides
the shared check so each call site does not reinvent it.
"""

import logging
import tarfile
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Python 3.12+ supports the filter parameter on tar.extract();
# 3.13 warns without it, 3.14 will error. Our own containment check
# runs first; the data filter is defense in depth.
_TAR_EXTRACT_KW: dict = (
    {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
)


def contained_path(base: Path, member_name: str) -> Path:
    """Resolve *member_name* against *base* and verify containment.

    Returns the resolved target path. Raises ``ValueError`` if the
    resolved path would land outside *base*, or if *member_name*
    contains null bytes.
    """
    if "\x00" in member_name:
        raise ValueError(f"Null byte in path component: {member_name!r}")
    base_resolved = base.resolve()
    target = (base_resolved / member_name).resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError(f"Path escapes target directory: {member_name}")
    return target


def safe_tar_extract(
    tar: tarfile.TarFile,
    dest: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> int:
    """Extract tar members, skipping any that would escape *dest*.

    Rejects absolute paths, traversal sequences, symlinks, hardlinks,
    and device nodes. Returns the count of extracted members.

    *progress_callback*, if given, receives ``(current_index, total)``
    every 100 members.
    """
    dest_resolved = dest.resolve()
    extracted = 0
    members = tar.getmembers()
    total = len(members)
    for i, member in enumerate(members):
        if member.issym() or member.islnk():
            logger.warning("Skipping link in archive: %s", member.name)
            continue
        if not (member.isfile() or member.isdir()):
            logger.warning("Skipping non-regular member: %s", member.name)
            continue
        try:
            contained_path(dest_resolved, member.name)
        except ValueError:
            logger.warning(
                "Skipping archive member with unsafe path: %s", member.name)
            continue
        tar.extract(member, dest, **_TAR_EXTRACT_KW)
        extracted += 1
        if progress_callback and i % 100 == 0:
            progress_callback(i, total)
    return extracted
