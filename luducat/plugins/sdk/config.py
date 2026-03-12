# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# config.py

"""SDK config access — shim delegating to core via registry.

Provides plugins with access to data/cache directories and config
values without importing ``luducat.core.config`` directly.

Usage in plugins::

    from luducat.plugins.sdk.config import get_data_dir, get_cache_dir
    data = get_data_dir()

The implementations are injected by ``PluginManager`` at startup via
``_registry.register_config()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import _registry


class SdkNotInitializedError(RuntimeError):
    """Raised when SDK shims are called before registry injection."""
    pass


def get_data_dir() -> Path:
    """Return the application data directory.

    Equivalent to ``luducat.core.config.get_data_dir()``.
    """
    if _registry._get_data_dir is None:
        raise SdkNotInitializedError(
            "SDK config not initialized — PluginManager has not injected "
            "config functions yet"
        )
    return _registry._get_data_dir()


def get_cache_dir() -> Path:
    """Return the application cache directory.

    Equivalent to ``luducat.core.config.get_cache_dir()``.
    """
    if _registry._get_cache_dir is None:
        raise SdkNotInitializedError(
            "SDK config not initialized — PluginManager has not injected "
            "config functions yet"
        )
    return _registry._get_cache_dir()


def get_config_value(key: str, default: Any = None) -> Any:
    """Read a config value by dotted key.

    Args:
        key: Dotted config key (e.g., ``"sync.interval"``)
        default: Value returned if key is missing

    Returns:
        Config value or *default*.
    """
    if _registry._get_config_value is None:
        raise SdkNotInitializedError(
            "SDK config not initialized — PluginManager has not injected "
            "config functions yet"
        )
    return _registry._get_config_value(key, default)


def set_config_value(key: str, value: Any) -> None:
    """Write a config value by dotted key.

    Args:
        key: Dotted config key (e.g., ``"gog.gogdb_import_offered"``)
        value: Value to store
    """
    if _registry._set_config_value is None:
        raise SdkNotInitializedError(
            "SDK config not initialized — PluginManager has not injected "
            "config functions yet"
        )
    _registry._set_config_value(key, value)
