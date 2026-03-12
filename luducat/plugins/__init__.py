# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Plugin system for luducat"""

from .base import AbstractGameStore, Game, PluginError, AuthenticationError

__all__ = ["AbstractGameStore", "Game", "PluginError", "AuthenticationError"]
