# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""IGDB metadata provider plugin for luducat

Provides game metadata enrichment from IGDB (Internet Game Database):
- Genres
- Tags (themes)
- Franchises/Series
- Ratings
- Screenshots and covers (as fallback)

Uses Twitch OAuth for authentication (IGDB is owned by Twitch).
"""

from .provider import IgdbProvider

__all__ = ["IgdbProvider"]
