# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# provider.py

"""Playnite Metadata Provider.

Imports tags, favourites, and playtime from Playnite via the luducat
bridge plugin. Requires a paired bridge connection.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.base import AbstractMetadataProvider, EnrichmentData, MetadataSearchResult

logger = logging.getLogger(__name__)


class PlayniteProvider(AbstractMetadataProvider):
    """Metadata provider for Playnite bridge."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "playnite"

    @property
    def display_name(self) -> str:
        return "Playnite"

    # ── Availability ──────────────────────────────────────────────────

    def is_available(self) -> bool:
        return False

    # ── Auth (not needed — bridge handles auth) ───────────────────────

    async def authenticate(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    # ── Tag sync ──────────────────────────────────────────────────────

    def get_tag_sync_data(self, **kwargs) -> Optional[Dict[str, Any]]:
        return None

    # ── Stub enrichment methods ───────────────────────────────────────

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        return None

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        return []

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        return None

    def get_database_path(self) -> Path:
        return self.data_dir / "playnite.db"
