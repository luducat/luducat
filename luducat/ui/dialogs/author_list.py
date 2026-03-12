# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# author_list.py

"""SteamGridDB Author Score Dialog — re-export shim.

The canonical implementation has moved to the SteamGridDB plugin:
    luducat.plugins.steamgriddb.ui.author_dialog

This module re-exports for backward compatibility.
"""

from luducat.plugins.steamgriddb.ui.author_dialog import (  # noqa: F401
    AuthorScoreDialog,
    COL_GRIDS,
    COL_HEROES,
    COL_HITS,
    COL_ICONS,
    COL_LOGOS,
    COL_MENU,
    COL_SCORE,
    COL_STATUS,
    COL_USERNAME,
)
