# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# external_prefix.py

"""External Prefix Provider Sub-Plugin

Wraps PrefixDetector as a sub-plugin — the pre-release prefix provider.
Scans for prefixes created by external launchers (Steam Proton, Heroic,
Lutris, Bottles, etc.) and matches them to games by store identity.
"""

import logging
from typing import List, Optional, TYPE_CHECKING

from . import SubPluginType, WineSubPlugin

if TYPE_CHECKING:
    from ..prefix_detector import WinePrefix

logger = logging.getLogger(__name__)


class ExternalPrefixProvider(WineSubPlugin):
    """Provides Wine prefixes detected from external launchers."""

    def __init__(self, local_data_consent: bool = False):
        self._consent = local_data_consent
        self._cached_prefixes: Optional[List["WinePrefix"]] = None

    @property
    def name(self) -> str:
        return "external_prefix"

    @property
    def display_name(self) -> str:
        return "External Prefixes"

    @property
    def sub_type(self) -> SubPluginType:
        return SubPluginType.PREFIX_PROVIDER

    @property
    def status(self) -> str:
        return "active"

    def get_prefixes(self) -> List["WinePrefix"]:
        """Get all detected prefixes. Cached after first scan."""
        if not self._consent:
            return []

        if self._cached_prefixes is not None:
            return self._cached_prefixes

        from ..prefix_detector import PrefixDetector

        detector = PrefixDetector(local_data_consent=self._consent)
        self._cached_prefixes = detector.scan_all()
        return self._cached_prefixes

    def find_prefix_for_game(
        self, store_name: str, app_id: str
    ) -> Optional["WinePrefix"]:
        """Find a prefix matching a specific store game.

        Args:
            store_name: Store identifier (e.g. "steam", "gog", "epic")
            app_id: Store-specific application ID

        Returns:
            Matching WinePrefix or None
        """
        for prefix in self.get_prefixes():
            if prefix.store_name == store_name and prefix.store_app_id == app_id:
                return prefix
        return None

    def contribute_env(self, env, prefix=None, **kwargs) -> None:
        """Pass through prefix environment variables."""
        if prefix and hasattr(prefix, "environment") and prefix.environment:
            env.add_bundle(prefix.environment)

    def clear_cache(self) -> None:
        """Force re-scan on next call."""
        self._cached_prefixes = None
