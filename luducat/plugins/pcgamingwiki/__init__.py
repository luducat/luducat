# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""PCGamingWiki metadata provider plugin for luducat

Provides game metadata enrichment from PCGamingWiki:
- Game modes (Singleplayer, Multiplayer, Co-op, Local)
- Multiplayer details (LAN, Online, Local, player counts, crossplay)
- Genres, Developers, Publishers (Phase 2)
"""

from .provider import PcgwProvider

__all__ = ["PcgwProvider"]
