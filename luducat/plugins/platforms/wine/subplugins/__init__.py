# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# __init__.py

"""Wine Sub-Plugin Infrastructure

Sub-plugins extend Wine launch behavior without modifying the core provider.
Types: PREFIX_PROVIDER (prefix scanning), ENHANCEMENT (DXVK, vkd3d),
OVERLAY (gamescope, mangohud), RUNNER_SOURCE (custom Wine builds).
"""

import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SubPluginType(Enum):
    PREFIX_PROVIDER = "prefix_provider"
    ENHANCEMENT = "enhancement"
    OVERLAY = "overlay"
    RUNNER_SOURCE = "runner_source"


class WineSubPlugin(ABC):
    """Abstract base for Wine sub-plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name."""

    @property
    @abstractmethod
    def sub_type(self) -> SubPluginType:
        """Sub-plugin type."""

    @property
    def is_available(self) -> bool:
        """Whether this sub-plugin's dependencies are met."""
        return True

    @property
    def status(self) -> str:
        """Current status: 'active', 'stub', 'disabled'."""
        return "active"

    def contribute_env(self, env, prefix=None, **kwargs) -> None:
        """Add environment variables to the WineEnv accumulator."""

    def contribute_command_prefix(self, cmd: List[str], **kwargs) -> List[str]:
        """Wrap or extend the command prefix list. Returns modified cmd."""
        return cmd


class SubPluginRegistry:
    """Registry for Wine sub-plugins."""

    def __init__(self):
        self._plugins: List[WineSubPlugin] = []

    def register(self, plugin: WineSubPlugin) -> None:
        """Register a sub-plugin."""
        self._plugins.append(plugin)
        logger.debug("Registered Wine sub-plugin: %s (%s)", plugin.name, plugin.sub_type.value)

    def get_by_type(self, sub_type: SubPluginType) -> List[WineSubPlugin]:
        """Get all sub-plugins of a given type."""
        return [p for p in self._plugins if p.sub_type == sub_type]

    def get_available(self, sub_type: Optional[SubPluginType] = None) -> List[WineSubPlugin]:
        """Get available sub-plugins, optionally filtered by type."""
        plugins = self._plugins
        if sub_type is not None:
            plugins = [p for p in plugins if p.sub_type == sub_type]
        return [p for p in plugins if p.is_available]

    def compose_env(self, env, prefix=None, **kwargs) -> None:
        """Let all available sub-plugins contribute to the environment."""
        for plugin in self.get_available():
            try:
                plugin.contribute_env(env, prefix=prefix, **kwargs)
            except Exception:
                logger.debug("Sub-plugin %s env contribution failed", plugin.name, exc_info=True)

    def compose_command(self, cmd: List[str], **kwargs) -> List[str]:
        """Let overlay sub-plugins wrap the command."""
        for plugin in self.get_available(SubPluginType.OVERLAY):
            try:
                cmd = plugin.contribute_command_prefix(cmd, **kwargs)
            except Exception:
                logger.debug("Sub-plugin %s command contribution failed", plugin.name, exc_info=True)
        return cmd
