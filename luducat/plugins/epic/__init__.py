# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Epic Games Store Plugin

Provides Epic Games Store library integration using Legendary CLI
for authentication and metadata, with Heroic/Epic Games Launcher for launching.

Usage:
    This plugin is automatically discovered and loaded by the plugin manager.
    Enable it in Settings > Plugins to sync your Epic Games library.

Requirements:
    - Heroic Games Launcher (recommended for Linux)
    - Epic Games Launcher (Windows)
    - Legendary CLI is auto-downloaded when needed
"""

from .store import EpicStore
from .config_dialog import EpicConfigDialog

__all__ = ["EpicStore", "EpicConfigDialog"]
