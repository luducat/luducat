# This file is part of the luducat Project.
# License: GPL-3.0-or-later. Contact: luducat-project@trinity2k.net
#
"""Where native crash traces (faulthandler) can be written."""

from typing import Any


def stderr_usable(stream: Any) -> bool:
    """True if faulthandler can write to this stream.

    stderr is None in PyInstaller no-console builds and a stream without a
    file descriptor in Briefcase app bundles.
    """
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return True
