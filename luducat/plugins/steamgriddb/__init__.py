# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""SteamGridDB metadata provider plugin for luducat

Provides community-sourced game images from SteamGridDB:
- Hero banners (primary use — non-dimmed background images)
- Grid covers (library capsules)
- Logos (transparent game logos)
"""

from .provider import SgdbProvider

__all__ = ["SgdbProvider"]
