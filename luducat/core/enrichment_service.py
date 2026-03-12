# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# enrichment_service.py

"""Enrichment persistence service for luducat.

Handles applying metadata enrichments from providers (IGDB, PCGamingWiki)
to StoreGame records, including per-field priority override logic and
force-rescan functionality.
Extracted from GameService to reduce its responsibilities.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from .database import (
    Database,
    Game as DbGame,
    StoreGame,
)
from . import enrichment_state as es
from .metadata_resolver import MetadataResolver
from ..plugins.base import EnrichmentData, Game as PluginGame

logger = logging.getLogger(__name__)


class EnrichmentService:
    """Applies enrichment data to the database with priority-based overrides."""

    def __init__(
        self,
        database: Database,
        resolver: MetadataResolver,
        games_cache: dict,
    ):
        self.database = database
        self._resolver = resolver
        self._games_cache = games_cache

    def _should_override(
        self,
        field_name: str,
        provider_name: str,
        metadata: dict,
        metadata_key: Optional[str] = None,
    ) -> bool:
        """Check if provider should override existing value based on per-field priority.

        Uses the MetadataResolver's per-field priority system to determine if
        a new provider's data should replace existing data.

        Args:
            field_name: Name of the metadata field for priority lookup (e.g., "genres")
            provider_name: Name of the provider offering new data
            metadata: Current metadata dict (contains "_sources" for tracking)
            metadata_key: Actual key in metadata dict (if different from field_name)

        Returns:
            True if provider should set/override the field, False otherwise
        """
        key = metadata_key or field_name

        existing_data = metadata.get(key)

        if existing_data is None:
            return True

        current_provider = es.get_field_source(metadata, field_name)

        if not current_provider:
            # No source tracking — any tracked provider can override
            return True

        new_rank = self._resolver.get_field_priority_rank(field_name, provider_name)
        current_rank = self._resolver.get_field_priority_rank(field_name, current_provider)
        return new_rank < current_rank

    def _apply_enrichments(
        self,
        store_name: str,
        enrichments: Dict[str, EnrichmentData],
        provider_name: str,
    ) -> int:
        """Apply enrichment data to store games in database.

        Commits in batches to ensure data is saved incrementally.
        On force-close, at most BATCH_SIZE games of enrichment will be lost.

        Args:
            store_name: Store name
            enrichments: Dict mapping store_app_id -> EnrichmentData
            provider_name: Name of the metadata provider

        Returns:
            Number of games enriched
        """
        if not enrichments:
            return 0

        BATCH_SIZE = 100
        session = self.database.get_session()
        enriched_count = 0
        batch_count = 0

        try:
            for store_app_id, enrichment in enrichments.items():
                store_game = session.query(StoreGame).filter_by(
                    store_name=store_name,
                    store_app_id=store_app_id,
                ).first()

                if not store_game:
                    continue

                metadata = store_game.metadata_json or {}

                # Per-field source map from merged enrichment (if available).
                # When a single plugin provides data, source_map is empty and
                # provider_name is used for all fields.
                smap = enrichment.source_map

                # Genres
                if enrichment.genres:
                    p = smap.get("genres", provider_name)
                    if self._should_override("genres", p, metadata):
                        metadata["genres"] = enrichment.genres
                        es.mark_field_source(metadata, "genres", p)

                # Tags (not in priority system, first wins)
                if enrichment.tags and not metadata.get("tags"):
                    metadata["tags"] = enrichment.tags

                # Developers/Publishers
                if enrichment.developers:
                    p = smap.get("developers", provider_name)
                    if self._should_override("developers", p, metadata):
                        metadata["developers"] = enrichment.developers
                        es.mark_field_source(metadata, "developers", p)
                if enrichment.publishers:
                    p = smap.get("publishers", provider_name)
                    if self._should_override("publishers", p, metadata):
                        metadata["publishers"] = enrichment.publishers
                        es.mark_field_source(metadata, "publishers", p)

                # Release date
                if enrichment.release_date:
                    p = smap.get("release_date", provider_name)
                    if self._should_override("release_date", p, metadata):
                        metadata["release_date"] = enrichment.release_date
                        es.mark_field_source(metadata, "release_date", p)

                # Cover image
                if enrichment.cover_url:
                    p = smap.get("cover", provider_name)
                    if self._should_override(
                        "cover", p, metadata,
                        metadata_key="cover_url",
                    ):
                        metadata["cover_url"] = enrichment.cover_url
                        es.mark_field_source(metadata, "cover", p)

                # Screenshots
                if enrichment.screenshots:
                    p = smap.get("screenshots", provider_name)
                    if self._should_override("screenshots", p, metadata):
                        metadata["screenshots"] = enrichment.screenshots
                        es.mark_field_source(metadata, "screenshots", p)

                # Hero banner
                if enrichment.background_url:
                    p = smap.get("hero", provider_name)
                    if self._should_override(
                        "hero", p, metadata,
                        metadata_key="background_url",
                    ):
                        metadata["background_url"] = enrichment.background_url
                        metadata["background_provider"] = p
                        es.mark_field_source(metadata, "hero", p)

                # Franchise/Series
                if enrichment.franchise:
                    p = smap.get("franchise", provider_name)
                    if self._should_override("franchise", p, metadata):
                        metadata["franchise"] = enrichment.franchise
                        es.mark_field_source(metadata, "franchise", p)
                if enrichment.series:
                    p = smap.get("series", provider_name)
                    if self._should_override("series", p, metadata):
                        metadata["series"] = enrichment.series
                        es.mark_field_source(metadata, "series", p)

                # Themes
                if enrichment.themes:
                    p = smap.get("themes", provider_name)
                    if self._should_override("themes", p, metadata):
                        metadata["themes"] = enrichment.themes
                        es.mark_field_source(metadata, "themes", p)

                # Engine
                if enrichment.engine:
                    p = smap.get("engine", provider_name)
                    if self._should_override("engine", p, metadata):
                        metadata["engine"] = enrichment.engine
                        es.mark_field_source(metadata, "engine", p)

                # Player perspectives
                if enrichment.perspectives:
                    p = smap.get("perspectives", provider_name)
                    if self._should_override(
                        "perspectives", p, metadata,
                        metadata_key="perspectives",
                    ):
                        metadata["perspectives"] = enrichment.perspectives
                        es.mark_field_source(metadata, "perspectives", p)

                # Platforms
                if enrichment.platforms:
                    p = smap.get("platforms", provider_name)
                    if self._should_override("platforms", p, metadata):
                        metadata["platforms"] = enrichment.platforms
                        es.mark_field_source(metadata, "platforms", p)

                # Age ratings
                if enrichment.age_ratings:
                    p = smap.get("age_ratings", provider_name)
                    if self._should_override("age_ratings", p, metadata):
                        metadata["age_ratings"] = enrichment.age_ratings
                        es.mark_field_source(metadata, "age_ratings", p)

                # Websites/Links
                if enrichment.websites:
                    p = smap.get("links", provider_name)
                    if self._should_override(
                        "links", p, metadata,
                        metadata_key="websites",
                    ):
                        metadata["websites"] = enrichment.websites
                        es.mark_field_source(metadata, "links", p)

                # Rating
                if enrichment.user_rating is not None:
                    p = smap.get("rating", provider_name)
                    if self._should_override(
                        "rating", p, metadata,
                        metadata_key="user_rating",
                    ):
                        metadata["user_rating"] = enrichment.user_rating
                        metadata["user_rating_count"] = enrichment.user_rating_count
                        es.mark_field_source(metadata, "rating", p)

                # Multiplayer details from extra dict (PCGamingWiki-specific)
                enrichment_extra = enrichment.extra or {}
                if enrichment_extra.get("crossplay") or enrichment_extra.get("online_players"):
                    p = smap.get("game_modes_detail", provider_name)
                    if self._should_override("game_modes_detail", p, metadata):
                        mp = metadata.get("game_modes_detail", {})
                        if enrichment_extra.get("crossplay"):
                            mp["crossplay"] = True
                            if enrichment_extra.get("crossplay_platforms"):
                                mp["crossplay_platforms"] = enrichment_extra["crossplay_platforms"]
                        if enrichment_extra.get("online_players"):
                            mp["online_players"] = enrichment_extra["online_players"]
                        if enrichment_extra.get("local_players"):
                            mp["local_players"] = enrichment_extra["local_players"]
                        if enrichment_extra.get("lan_players"):
                            mp["lan_players"] = enrichment_extra["lan_players"]
                        if mp:
                            metadata["game_modes_detail"] = mp
                            es.mark_field_source(metadata, "game_modes_detail", p)

                # Crossplay (separate field for priority)
                if enrichment_extra.get("crossplay"):
                    p = smap.get("crossplay", provider_name)
                    if self._should_override("crossplay", p, metadata):
                        metadata.setdefault("game_modes_detail", {})["crossplay"] = True
                        if enrichment_extra.get("crossplay_platforms"):
                            metadata["game_modes_detail"]["crossplay_platforms"] = enrichment_extra["crossplay_platforms"]
                        es.mark_field_source(metadata, "crossplay", p)

                # ProtonDB rating from extra dict
                if enrichment_extra.get("protondb_rating"):
                    p = smap.get("protondb_rating", provider_name)
                    if self._should_override("protondb_rating", p, metadata):
                        metadata["protondb_rating"] = enrichment_extra["protondb_rating"]
                        es.mark_field_source(metadata, "protondb_rating", p)
                if enrichment_extra.get("protondb_score") is not None:
                    p = smap.get("protondb_score", provider_name)
                    if self._should_override("protondb_score", p, metadata):
                        metadata["protondb_score"] = enrichment_extra["protondb_score"]
                        es.mark_field_source(metadata, "protondb_score", p)

                # Persist priority hash so on-demand check knows enrichment is current
                sources = metadata.setdefault("_sources", {})
                sources["_priority_hash"] = self._resolver.compute_priority_hash()

                # Update store_game
                store_game.metadata_json = metadata
                flag_modified(store_game, "metadata_json")
                enriched_count += 1
                batch_count += 1

                if batch_count >= BATCH_SIZE:
                    session.commit()
                    batch_count = 0

            if batch_count > 0:
                session.commit()

            logger.debug(f"Applied enrichments to {enriched_count} games")

        except Exception as e:
            logger.error(f"Failed to apply enrichments: {e}")
            session.rollback()

        return enriched_count

    async def force_rescan_game(
        self,
        game_id: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        refresh_cache_fn: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        """Force re-scan metadata for a single game.

        Clears cached store matches in metadata plugins so they re-fetch
        from external APIs, then re-runs enrichment for this game only.

        ALL metadata operations go through MetadataResolver - no direct plugin calls.

        Args:
            game_id: Game UUID to rescan
            progress_callback: Optional callback(message, current, total)
            refresh_cache_fn: Callback to refresh parent cache after rescan

        Returns:
            Updated game data dict, or None on failure
        """
        session = self.database.get_session()
        try:
            game = (
                session.query(DbGame)
                .options(selectinload(DbGame.store_games))
                .filter_by(id=game_id)
                .first()
            )
            if not game:
                logger.warning(f"Force rescan: game {game_id} not found")
                return None

            if not game.store_games:
                logger.warning(f"Force rescan: no store games for {game_id}")
                return None

            # Clear ALL enrichment state (force=True clears store-provided
            # fields too) so the enrichment pipeline starts from scratch
            # with the current priority settings
            for sg in game.store_games:
                meta = sg.metadata_json or {}
                es.clear_enrichment(meta, force=True)
                sg.metadata_json = meta
                flag_modified(sg, "metadata_json")
            session.commit()

            games_to_enrich = []
            for sg in game.store_games:
                pg = PluginGame(
                    store_app_id=sg.store_app_id,
                    store_name=sg.store_name,
                    title=game.title,
                    launch_url=sg.launch_url or "",
                )
                games_to_enrich.append(pg)

            enrichments = await self._resolver.force_rescan_game(
                games_to_enrich,
                progress_callback=progress_callback,
            )

            for sg in game.store_games:
                self._apply_enrichments(
                    sg.store_name, enrichments, sg.store_name
                )

            # Refresh cache via callback
            if refresh_cache_fn:
                refresh_cache_fn()

            return self._games_cache.get(game_id)

        except Exception as e:
            logger.error(f"Force rescan failed for {game_id}: {e}")
            return None
