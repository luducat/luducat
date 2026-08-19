# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Amazon Games Store Plugin

Provides Amazon Games (Prime Gaming) library integration via Amazon's
device-registration auth and the Animus entitlements API.

Usage:
    This plugin is automatically discovered and loaded by the plugin manager.
    Enable it in Settings > Plugins to sync your Amazon Games library.
"""

from .store import AmazonStore

__all__ = ["AmazonStore"]
