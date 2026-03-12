# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Core functionality for luducat"""

from .constants import APP_NAME, APP_VERSION, APP_VERSION_FULL
from .config import Config, get_config_dir, get_data_dir, get_cache_dir
from .credentials import CredentialManager
from .database import Database
from .plugin_manager import PluginManager

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "APP_VERSION_FULL",
    "Config",
    "CredentialManager",
    "Database",
    "PluginManager",
    "get_config_dir",
    "get_data_dir",
    "get_cache_dir",
]
