# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# constants.py

"""SDK-exported constants for plugin use.

Re-exports from ``luducat.core.constants``.  Third-party plugins use
this as the sole access path for application constants.

Constants that are purely UI-side (window sizes, grid density, etc.)
are intentionally NOT exported — plugins have no reason to use them.
"""

from __future__ import annotations

from luducat.core.constants import (
    APP_NAME,
    APP_VERSION,
    GAME_MODE_LABELS,
    GAME_MODE_FILTERS,
    PROTONDB_TIER_LABELS,
    PROTONDB_TIER_COLORS,
    STEAM_DECK_LABELS,
    STEAM_DECK_COLORS,
    DEFAULT_TAG_COLOR,
    TAG_SOURCE_COLORS,
)

# HTTP User-Agent strings
# USER_AGENT: browser-like UA for web requests (default for all plugins)
# USER_AGENT_GW: app-identifying UA exclusively for luducat API proxy
from luducat.core.constants import USER_AGENT, USER_AGENT_GW

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "USER_AGENT",
    "USER_AGENT_GW",
    "GAME_MODE_LABELS",
    "GAME_MODE_FILTERS",
    "PROTONDB_TIER_LABELS",
    "PROTONDB_TIER_COLORS",
    "STEAM_DECK_LABELS",
    "STEAM_DECK_COLORS",
    "DEFAULT_TAG_COLOR",
    "TAG_SOURCE_COLORS",
]
