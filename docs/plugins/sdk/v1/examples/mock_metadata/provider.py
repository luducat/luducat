"""GamePedia -- Demo metadata plugin for the luducat Plugin SDK.

This plugin demonstrates a complete metadata provider using hardcoded
enrichment data. No real API calls are made.

Key concepts demonstrated:
- All required AbstractMetadataProvider methods
- Store ID lookup and title-based search
- EnrichmentData construction with varied field coverage
- MetadataSearchResult with confidence scoring
- Cached enrichment pattern
- provides_fields priority declarations
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from luducat.plugins.base import (
    AbstractMetadataProvider,
    AuthenticationError,
    EnrichmentData,
    MetadataSearchResult,
)

logger = logging.getLogger(__name__)


# Simulated cross-reference database: (store_name, store_id) -> provider_id
_STORE_MATCHES: Dict[tuple, str] = {
    ("steam", "440"): "gp-100",        # Team Fortress 2
    ("steam", "570"): "gp-101",        # Dota 2
    ("steam", "730"): "gp-102",        # CS2
    ("gog", "1207658924"): "gp-200",   # Baldur's Gate
    ("gamevault", "gv-1001"): "gp-300",  # Neon Circuit (from demo store)
    ("gamevault", "gv-1002"): "gp-301",  # Echoes of Eternity
}

# Enrichment database keyed by provider_id
_ENRICHMENT_DB: Dict[str, Dict] = {
    "gp-100": {
        "title": "Team Fortress 2",
        "genres": ["FPS", "Action"],
        "tags": ["Multiplayer", "Class-Based", "Free to Play"],
        "franchise": "Team Fortress",
        "themes": ["Comedy", "Warfare"],
        "perspectives": ["First person"],
        "user_rating": 92.0,
        "engine": "Source",
        "platforms": ["windows", "linux", "macos"],
    },
    "gp-101": {
        "title": "Dota 2",
        "genres": ["MOBA", "Strategy"],
        "tags": ["Competitive", "Free to Play", "Team-Based"],
        "franchise": "Dota",
        "themes": ["Fantasy", "Warfare"],
        "perspectives": ["Bird view"],
        "user_rating": 88.5,
        "engine": "Source 2",
        "platforms": ["windows", "linux", "macos"],
    },
    "gp-102": {
        "title": "Counter-Strike 2",
        "genres": ["FPS", "Action"],
        "tags": ["Competitive", "Tactical", "Multiplayer"],
        "franchise": "Counter-Strike",
        "series": "Main Series",
        "themes": ["Modern Warfare", "Terrorism"],
        "perspectives": ["First person"],
        "user_rating": 85.0,
        "engine": "Source 2",
        "platforms": ["windows", "linux"],
    },
    "gp-200": {
        "title": "Baldur's Gate",
        "genres": ["RPG", "Strategy"],
        "tags": ["Classic", "Isometric", "Party-Based"],
        "franchise": "Baldur's Gate",
        "series": "Bhaalspawn Saga",
        "themes": ["Fantasy", "Medieval"],
        "perspectives": ["Bird view"],
        "user_rating": 94.0,
        "engine": "Infinity Engine",
        "platforms": ["windows", "linux", "macos"],
    },
    "gp-300": {
        "title": "Neon Circuit",
        "genres": ["Racing", "Action", "Arcade"],
        "tags": ["Cyberpunk", "Fast-Paced", "Customization"],
        "themes": ["Cyberpunk", "Futuristic"],
        "perspectives": ["Third person"],
        "user_rating": 78.5,
        "engine": "NeonEngine 2.0",
        "platforms": ["windows", "linux"],
    },
    "gp-301": {
        "title": "Echoes of Eternity",
        "genres": ["RPG", "Adventure", "Narrative"],
        "tags": ["Story Rich", "Choices Matter", "Time Travel"],
        "franchise": "Echoes",
        "themes": ["Fantasy", "Time Travel", "Mystery"],
        "perspectives": ["Third person"],
        "user_rating": 91.0,
        "engine": "Temporal Engine",
        "platforms": ["windows", "linux", "macos"],
    },
}

# Title-based search index (normalized title -> provider_id)
_TITLE_INDEX: Dict[str, str] = {
    d["title"].lower(): pid for pid, d in _ENRICHMENT_DB.items()
}


class GamePediaProvider(AbstractMetadataProvider):
    """GamePedia metadata provider.

    Demonstrates all required and several optional methods of
    AbstractMetadataProvider using hardcoded data.
    """

    # ── Required Properties ──────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "gamepedia"

    @property
    def display_name(self) -> str:
        return "GamePedia"

    # ── Required Methods ─────────────────────────────────────────────

    def is_available(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return bool(self.get_credential("api_key"))

    async def authenticate(self) -> bool:
        api_key = self.get_credential("api_key")
        if not api_key:
            raise AuthenticationError("GamePedia API key not configured")
        # In a real plugin: validate key against API
        return True

    async def lookup_by_store_id(
        self, store_name: str, store_id: str
    ) -> Optional[str]:
        """Look up our provider ID from a store game.

        In a real plugin, this would query a cross-reference database
        or API endpoint.
        """
        key = (store_name, store_id)
        provider_id = _STORE_MATCHES.get(key)
        if provider_id:
            logger.debug("GamePedia: matched %s/%s -> %s", store_name, store_id, provider_id)
        return provider_id

    async def search_game(
        self, title: str, year: Optional[int] = None
    ) -> List[MetadataSearchResult]:
        """Search for games by title.

        In a real plugin, this would call a search API.
        """
        results = []
        query = title.lower()

        for norm_title, pid in _TITLE_INDEX.items():
            # Simple substring matching
            if query in norm_title or norm_title in query:
                data = _ENRICHMENT_DB[pid]
                # Higher confidence for exact matches
                confidence = 1.0 if query == norm_title else 0.7
                results.append(MetadataSearchResult(
                    provider_id=pid,
                    title=data["title"],
                    platforms=data.get("platforms", []),
                    confidence=confidence,
                ))

        # Sort by confidence descending
        results.sort(key=lambda r: r.confidence, reverse=True)
        return results

    async def get_enrichment(
        self, provider_id: str
    ) -> Optional[EnrichmentData]:
        """Get enrichment data for a matched game.

        In a real plugin, this would fetch from the API and cache locally.
        """
        data = _ENRICHMENT_DB.get(provider_id)
        if not data:
            return None

        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=provider_id,
            genres=data.get("genres", []),
            tags=data.get("tags", []),
            franchise=data.get("franchise"),
            series=data.get("series"),
            themes=data.get("themes", []),
            perspectives=data.get("perspectives", []),
            platforms=data.get("platforms", []),
            user_rating=data.get("user_rating"),
            extra={"engine": data.get("engine")},
        )

    def get_database_path(self) -> Path:
        return self.data_dir / "enrichment.db"

    # ── Optional Methods ─────────────────────────────────────────────

    def get_cached_enrichment(
        self, store_name: str, store_id: str
    ) -> Optional[EnrichmentData]:
        """Return cached enrichment without API calls.

        In a real plugin, this would query the local enrichment database.
        """
        key = (store_name, store_id)
        provider_id = _STORE_MATCHES.get(key)
        if not provider_id:
            return None
        data = _ENRICHMENT_DB.get(provider_id)
        if not data:
            return None
        return EnrichmentData(
            provider_name=self.provider_name,
            provider_id=provider_id,
            genres=data.get("genres", []),
            tags=data.get("tags", []),
            franchise=data.get("franchise"),
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_enable(self) -> None:
        logger.info("GamePedia provider enabled")

    def close(self) -> None:
        logger.info("GamePedia provider shutting down")
