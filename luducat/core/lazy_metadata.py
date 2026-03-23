# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# lazy_metadata.py

"""Lazy metadata loading service for luducat.

Handles on-demand fetching of screenshots, covers, descriptions,
and metadata completion via MetadataResolver.
Extracted from GameService to reduce its responsibilities.
"""

import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from .database import (
    Database,
    Game as DbGame,
    StoreGame,
)
from .metadata_resolver import MetadataResolver
from .plugin_manager import PluginManager

logger = logging.getLogger(__name__)

_GAME_TYPE_SUFFIXES = re.compile(
    r"[\s:_-]*\(?\s*(?:demo|prologue|trial|beta|early access|playtest|benchmark)\s*\)?\s*$",
    re.IGNORECASE,
)


def _strip_game_type_suffix(title: str) -> str:
    """Strip trailing game-type suffixes like 'Demo', 'Prologue', etc.

    Handles bare, parenthesized, and separator variants:
      'demonherd demo' -> 'demonherd'
      'game - demo'    -> 'game'
      'game (demo)'    -> 'game'

    Returns the stripped title, or the original if no suffix matched.
    """
    stripped = _GAME_TYPE_SUFFIXES.sub("", title).strip()
    return stripped if stripped else title


class LazyMetadata:
    """Handles on-demand metadata fetching: screenshots, covers, descriptions."""

    def __init__(
        self,
        database: Database,
        games_cache: dict,
        description_cache: OrderedDict,
        description_cache_max: int,
        resolver: MetadataResolver,
        plugin_manager: PluginManager,
        detail_cache: Optional[OrderedDict] = None,
        detail_cache_max: int = 50,
    ):
        self.database = database
        self._games_cache = games_cache
        self._description_cache = description_cache
        self._description_cache_max = description_cache_max
        self._resolver = resolver
        self.plugin_manager = plugin_manager
        self._detail_cache: OrderedDict = (
            detail_cache if detail_cache is not None
            else OrderedDict()
        )
        self._detail_cache_max = detail_cache_max

    def clear_cache(self) -> None:
        """Flush all memoized lazy metadata so it re-resolves with current priorities."""
        self._description_cache.clear()
        self._detail_cache.clear()
        logger.debug("LazyMetadata caches cleared")

    def _is_valid_screenshot_path(self, path: str) -> bool:
        """Check if a screenshot path is valid (URL or existing local file).

        Args:
            path: Screenshot URL or local file path

        Returns:
            True if path is valid and accessible
        """
        if not path:
            return False
        if path.startswith("http://") or path.startswith("https://"):
            return True
        if path.startswith("file://"):
            from PySide6.QtCore import QUrl
            file_path = QUrl(path).toLocalFile()
            if "gamelauncher" in file_path.lower():
                return False
            return Path(file_path).exists()
        if Path(path).is_absolute():
            if "gamelauncher" in path.lower():
                return False
            return Path(path).exists()
        return False

    def _has_valid_screenshots(self, screenshots: list) -> bool:
        """Check if screenshots list contains valid URLs or existing files.

        Args:
            screenshots: List of screenshot URLs or file paths

        Returns:
            True if at least one screenshot is valid (URL or existing file)
        """
        if not screenshots:
            return False

        for ss in screenshots:
            if self._is_valid_screenshot_path(ss):
                return True

        return False

    def get_screenshots(
        self,
        game_id: str,
        exclude_sources: Optional[List[str]] = None,
    ) -> List[str]:
        """Get screenshots for a game, with lazy loading from plugins.

        Uses MetadataResolver to fetch from plugins in priority order
        (store plugins first, then metadata plugins as fallback).
        Updates both cache and database with the result.

        Args:
            game_id: Game UUID
            exclude_sources: Sources to skip (e.g. source that returned 404)

        Returns:
            List of screenshot URLs/paths
        """
        logger.debug(f"get_screenshots called for game_id={game_id}")

        # Skip cache when retrying with excluded sources
        if not exclude_sources and game_id in self._games_cache:
            screenshots = self._games_cache[game_id].screenshots
            if self._has_valid_screenshots(screenshots):
                logger.debug(f"Found {len(screenshots)} valid screenshots in cache for {game_id}")
                return screenshots
            else:
                logger.debug(
                    f"Cache has {len(screenshots)} invalid "
                    f"screenshots for {game_id}, refetching"
                )

        game_data = self._games_cache.get(game_id)
        if not game_data:
            logger.warning(f"No game data in cache for game_id={game_id}")
            return []

        store_app_ids = game_data.store_app_ids
        normalized_title = game_data.normalized_title

        screenshots, source = self._resolver.get_screenshots_on_demand(
            store_app_ids, normalized_title, exclude_sources=exclude_sources,
        )

        if not screenshots:
            logger.debug(f"No screenshots found for {game_id} from any plugin")
            return []

        session = self.database.new_session()
        try:
            store_game = session.query(StoreGame).join(DbGame).filter(
                DbGame.id == game_id
            ).first()

            if store_game:
                metadata = store_game.metadata_json or {}
                metadata["screenshots"] = screenshots
                if source:
                    sources = metadata.setdefault("_sources", {})
                    sources["screenshots"] = source
                store_game.metadata_json = metadata
                session.commit()

            if game_id in self._games_cache:
                self._games_cache[game_id].screenshots = screenshots

            logger.info(f"Lazy-loaded {len(screenshots)} screenshots for game {game_id}")
            return screenshots

        except Exception as e:
            logger.error(f"Failed to save screenshots for {game_id}: {e}", exc_info=True)
            return screenshots
        finally:
            session.close()

    def invalidate_screenshots(
        self,
        game_id: str,
        failed_urls: List[str],
    ) -> List[str]:
        """Remove broken screenshot URLs and retry with next priority source.

        Called when image loading gets HTTP 404 for a screenshot URL.
        Removes the failed URLs from stored metadata, identifies which source
        provided them, then re-fetches from the next source in priority order.

        Args:
            game_id: Game UUID
            failed_urls: URLs that returned 404

        Returns:
            New screenshot URLs from the next priority source, or []
        """
        if not game_id or not failed_urls:
            return []

        failed_set = set(failed_urls)
        failed_source = None

        session = self.database.new_session()
        try:
            store_game = session.query(StoreGame).join(DbGame).filter(
                DbGame.id == game_id
            ).first()

            if store_game:
                metadata = store_game.metadata_json or {}
                current = metadata.get("screenshots", [])
                remaining = [u for u in current if u not in failed_set]

                # Identify the source that served the broken URLs
                sources = metadata.get("_sources", {})
                failed_source = sources.get("screenshots")

                if not remaining:
                    # All URLs from this source are broken — clear source marker
                    metadata["screenshots"] = []
                    sources.pop("screenshots", None)
                else:
                    metadata["screenshots"] = remaining

                store_game.metadata_json = metadata
                flag_modified(store_game, "metadata_json")
                session.commit()

            # Update cache
            if game_id in self._games_cache:
                self._games_cache[game_id].screenshots = []

        except Exception as e:
            logger.error(f"Failed to invalidate screenshots for {game_id}: {e}")
            return []
        finally:
            session.close()

        if not failed_source:
            return []

        # Retry from next priority source, excluding the one that 404'd
        logger.info(
            f"Screenshot 404 fallback: {game_id} — skipping {failed_source}, "
            f"trying next source"
        )
        return self.get_screenshots(game_id, exclude_sources=[failed_source])

    def get_cover(self, game_id: str) -> str:
        """Get cover URL for a game, with lazy loading from plugins.

        Uses MetadataResolver to fetch from plugins in priority order
        (store plugins first, then metadata plugins as fallback).
        Updates both cache and database with the result.

        Args:
            game_id: Game UUID

        Returns:
            Cover image URL or empty string if not found
        """
        if game_id in self._games_cache:
            cover = self._games_cache[game_id].cover_image
            if cover:
                return cover

        game_data = self._games_cache.get(game_id)
        if not game_data:
            logger.debug(f"get_cover: No game data in cache for game_id={game_id}")
            return ""

        store_app_ids = game_data.store_app_ids
        normalized_title = game_data.normalized_title
        title = game_data.title or "Unknown"

        cover, source = self._resolver.get_cover_on_demand(store_app_ids, normalized_title)

        if not cover:
            stripped_title = _strip_game_type_suffix(normalized_title)
            if stripped_title and stripped_title != normalized_title:
                cover, source = self._resolver.get_cover_on_demand(
                    store_app_ids, stripped_title
                )

        if not cover:
            return ""

        if game_id in self._games_cache:
            self._games_cache[game_id].cover_image = cover

        self._persist_metadata_updates(
            game_id, {"cover": cover},
            source_map={"cover": source} if source else None,
        )

        logger.info(f"get_cover: Lazy-loaded cover for '{title}' from {source}: {cover[:60]}...")
        return cover

    def update_game_description(self, game_id: str, description: str) -> bool:
        """Update description for a game in the main database.

        Called after fetching HTML description from plugin API.

        Args:
            game_id: Game UUID
            description: New HTML description

        Returns:
            True if updated successfully
        """
        session = self.database.new_session()
        try:
            game = (
                session.query(DbGame)
                .options(selectinload(DbGame.store_games))
                .filter_by(id=game_id)
                .first()
            )
            if not game:
                logger.debug(f"Game {game_id} not found")
                return False

            store_game = None
            for sg in game.store_games:
                if sg.store_name == game.primary_store:
                    store_game = sg
                    break

            if not store_game:
                store_game = game.store_games[0] if game.store_games else None

            if not store_game:
                logger.debug(f"No store_game found for {game_id}")
                return False

            metadata = store_game.metadata_json or {}
            metadata["description"] = description
            store_game.metadata_json = metadata
            session.commit()

            self._description_cache[game_id] = description
            self._description_cache.move_to_end(game_id)
            while len(self._description_cache) > self._description_cache_max:
                self._description_cache.popitem(last=False)

            logger.debug(f"Updated description for game {game_id}")
            return True

        except Exception as e:
            logger.warning(f"Failed to update description for {game_id}: {e}")
            return False
        finally:
            session.close()

    def get_description(self, game_id: str) -> str:
        """Get description for a game (lazy loaded with LRU cache).

        Descriptions are not stored in the main games cache to save memory.
        This method uses the MetadataResolver to fetch from plugins in
        priority order (store plugins first, then metadata plugins as fallback).

        Args:
            game_id: Game UUID

        Returns:
            HTML description string, or empty string if not found
        """
        if game_id in self._description_cache:
            self._description_cache.move_to_end(game_id)
            return self._description_cache[game_id]

        game_data = self._games_cache.get(game_id)
        if not game_data:
            return ""

        store_app_ids = game_data.store_app_ids
        normalized_title = game_data.normalized_title

        description = self._resolver.get_description_on_demand(
            store_app_ids, normalized_title
        )

        if not description:
            description = game_data.short_description

        self._description_cache[game_id] = description
        while len(self._description_cache) > self._description_cache_max:
            self._description_cache.popitem(last=False)

        return description

    # Mapping from cache field names (used in _games_cache) to DB/metadata field names
    _FIELD_MAPPING = {
        "cover_image": "cover",
        "header_image": "header_url",
        "short_description": "short_description",
        "release_date": "release_date",
        "developers": "developers",
        "publishers": "publishers",
        "genres": "genres",
        "screenshots": "screenshots",
        "themes": "themes",
        "game_modes": "game_modes",
    }

    def _needs_on_demand_enrichment(self, game_id: str) -> bool:
        """Check whether on-demand enrichment should run for this game.

        Decision logic (checked in order):
        1. If ``_sources`` is absent/empty → never enriched → True
        2. If ``_sources["_priority_hash"]`` doesn't match → priorities changed → True
        3. If ``_sources`` has actual field entries AND hash matches → skip
        4. If ``_attempted_by`` has entries AND hash matches → plugins tried, skip

        Returns:
            True if enrichment should be triggered.
        """
        session = self.database.new_session()
        try:
            store_game = (
                session.query(StoreGame)
                .join(DbGame)
                .filter(DbGame.id == game_id)
                .first()
            )
            if not store_game:
                return False

            metadata = store_game.metadata_json or {}
            sources = metadata.get("_sources", {})

            if not sources:
                # Never enriched at all
                return True

            current_hash = self._resolver.compute_priority_hash()
            stored_hash = sources.get("_priority_hash", "")

            if stored_hash != current_hash:
                # Priorities changed since last enrichment
                logger.debug(
                    f"Priority hash mismatch for {game_id}: "
                    f"stored={stored_hash}, current={current_hash}"
                )
                return True

            # Check if we have actual field sources (not just meta-keys)
            _meta_keys = {"_priority_hash", "_attempted_by", "_enriched_via"}
            field_sources = {
                k: v for k, v in sources.items()
                if k not in _meta_keys and isinstance(v, str)
            }
            if field_sources:
                # Has enrichment data with matching hash → up to date
                return False

            # Check _attempted_by (plugins tried but found nothing)
            attempted = sources.get("_attempted_by", [])
            if attempted:
                # Plugins tried and hash matches → no need to retry
                return False

            # Shouldn't reach here, but be safe: enrich if unsure
            return True

        except Exception as e:
            logger.debug(f"_needs_on_demand_enrichment error for {game_id}: {e}")
            return False
        finally:
            session.close()

    def ensure_metadata_complete(self, game_id: str) -> Dict[str, Any]:
        """Ensure all metadata fields are populated for a game.

        Called when displaying game details.  On the very first click all
        configured priority sources are consulted.  Results are persisted
        with a ``_priority_hash`` so subsequent clicks don't re-fetch unless
        the user changes priority settings.

        Args:
            game_id: Game UUID

        Returns:
            Updated game data dict with filled-in metadata
        """
        game_data = self._games_cache.get(game_id)
        if not game_data:
            return {}

        store_app_ids = game_data.store_app_ids
        normalized_title = game_data.normalized_title

        # --- Decide what to do ---
        needs_enrichment = self._needs_on_demand_enrichment(game_id)

        if not needs_enrichment:
            return game_data

        logger.debug(f"Game {game_id}: on-demand enrichment triggered")

        # --- Fetch from all priority sources ---
        result = self._resolver.fetch_metadata_for_game(
            store_app_ids, normalized_title, _return_sources=True
        )
        metadata, source_map = result  # type: ignore[misc]

        if metadata:
            db_updates: Dict[str, Any] = {}

            # Full enrichment: persist ALL resolved fields to DB
            # (including detail fields like background_url, franchise, etc.)
            for db_key, value in metadata.items():
                if db_key.startswith("_"):
                    continue  # Skip internal keys
                if value:
                    db_updates[db_key] = value

            # Update _games_cache with LIST_FIELDS only
            for cache_key, db_key in self._FIELD_MAPPING.items():
                value = metadata.get(db_key)
                if value:
                    if cache_key == "release_date":
                        if isinstance(value, dict):
                            value = min(value.values()) if value else ""
                        else:
                            from .dt import parse_release_date
                            value = parse_release_date(value) or ""
                    game_data[cache_key] = value

            if db_updates:
                # game_data is already the GameEntry in _games_cache (mutated in place)
                # Add priority hash to source_map for persistence
                source_map["_priority_hash"] = self._resolver.compute_priority_hash()

                self._persist_metadata_updates(
                    game_id, db_updates, source_map=source_map
                )
                logger.info(
                    f"Enriched game {game_id} ({len(db_updates)} fields)"
                )

                # Invalidate detail cache so next get_detail_fields() reads fresh data
                self._detail_cache.pop(game_id, None)
            else:
                # Metadata returned but no usable fields — treat same as no data
                self._mark_enrichment_attempted(game_id)
        else:
            # No metadata returned — distinguish offline from online-but-nothing
            self._mark_enrichment_attempted(game_id)

        # Fetch Steam Deck compat on-demand if missing (Steam games only)
        if not game_data.steam_deck_compat and "steam" in store_app_ids:
            steam_app_id = store_app_ids.get("steam")
            if steam_app_id and self.plugin_manager:
                from .network_monitor import get_network_monitor
                if get_network_monitor().is_online:
                    try:
                        steam_plugin = self.plugin_manager.get_plugin("steam")
                        if steam_plugin and hasattr(steam_plugin, "fetch_deck_compat"):
                            compat = steam_plugin.fetch_deck_compat(steam_app_id)
                            if compat:
                                game_data.steam_deck_compat = compat
                                self._persist_metadata_updates(
                                    game_id,
                                    {"steam_deck_compat": compat},
                                )
                    except Exception as e:
                        logger.debug(f"Failed to fetch deck compat for {game_id}: {e}")

        return game_data

    def get_detail_fields(self, game_id: str) -> Dict[str, Any]:
        """Get detail fields for a game on demand (LRU cached).

        Queries StoreGame.metadata_json from the main DB for the given game,
        applies MetadataResolver priority resolution for multi-store games,
        and extracts only DETAIL_FIELDS keys.

        Args:
            game_id: Game UUID

        Returns:
            Dict of detail field values, or empty dict if game not found
        """
        # Check LRU cache first
        if game_id in self._detail_cache:
            self._detail_cache.move_to_end(game_id)
            return self._detail_cache[game_id]

        # Game must be in the list cache for us to know about it
        if game_id not in self._games_cache:
            return {}

        session = self.database.new_session()
        try:
            store_games = session.query(StoreGame).filter_by(
                game_id=game_id
            ).all()

            if not store_games:
                return {}

            # Build metadata by store for resolver
            metadata_by_store = {}
            for sg in store_games:
                if sg.metadata_json:
                    metadata_by_store[sg.store_name] = sg.metadata_json

            # Reconstruct enrichment source entries from _sources tracking
            # so resolve_game_metadata() can see steamgriddb/igdb as separate sources
            for sg in store_games:
                meta = sg.metadata_json or {}
                sources = meta.get("_sources", {})
                for field, source in sources.items():
                    if field.startswith("_") or not isinstance(source, str):
                        continue
                    if source == sg.store_name:
                        continue  # Store's own data, already in metadata_by_store
                    if source not in metadata_by_store:
                        metadata_by_store[source] = {}
                    if field in meta:
                        metadata_by_store[source][field] = meta[field]

            if metadata_by_store:
                metadata = self._resolver.resolve_game_metadata(metadata_by_store)
            else:
                metadata = {}

            # Normalize fields that may arrive as list or str depending on source
            def _as_str(val, default=""):
                if isinstance(val, list):
                    return ", ".join(str(v) for v in val) if val else default
                return val if val else default

            # Extract detail fields using the same mapping as _db_game_to_ui
            detail = {
                "background_image": metadata.get("hero") or metadata.get("background_url", ""),
                "series": _as_str(metadata.get("series")),
                "engine": _as_str(metadata.get("engine")),
                "perspectives": metadata.get("perspectives", []),
                "platforms": metadata.get("platforms", []),
                "age_ratings": metadata.get("age_ratings", []),
                "user_rating": metadata.get("user_rating"),
                "rating_positive": metadata.get("rating_positive"),
                "rating_negative": metadata.get("rating_negative"),
                "critic_rating": metadata.get("critic_rating"),
                "critic_rating_url": metadata.get("critic_rating_url", ""),
                "controller_support": metadata.get("controller_support", ""),
                "supported_languages": metadata.get("supported_languages", []),
                "full_audio_languages": metadata.get("full_audio_languages", ""),
                "features": metadata.get("features") or metadata.get("categories", []),
                "controls": metadata.get("controls", []),
                "pacing": metadata.get("pacing", []),
                "art_styles": metadata.get("art_styles", []),
                "monetization": metadata.get("monetization", []),
                "microtransactions": metadata.get("microtransactions", []),
                "achievements": metadata.get("achievements"),
                "estimated_owners": metadata.get("estimated_owners", ""),
                "recommendations": metadata.get("recommendations"),
                "peak_ccu": metadata.get("peak_ccu"),
                "average_playtime_forever": metadata.get("average_playtime_forever"),
                "links": metadata.get("links") or metadata.get("websites", []),
                "game_modes_detail": metadata.get("game_modes_detail", {}),
                "videos": metadata.get("videos", []),
                "storyline": metadata.get("storyline", ""),
                "required_age": metadata.get("required_age"),
                "protondb_score": metadata.get("protondb_score"),
                "release_dates": (
                    metadata.get("release_date")
                    if isinstance(
                        metadata.get("release_date"), dict
                    )
                    else {}
                ),
            }

            # Store in LRU cache
            self._detail_cache[game_id] = detail
            self._detail_cache.move_to_end(game_id)
            while len(self._detail_cache) > self._detail_cache_max:
                self._detail_cache.popitem(last=False)

            return detail

        except Exception as e:
            logger.warning(f"Failed to get detail fields for {game_id}: {e}")
            return {}
        finally:
            session.close()

    def _mark_enrichment_attempted(self, game_id: str) -> None:
        """Record that enrichment was attempted but produced no data.

        Only marks when online — offline failures are left unmarked so
        they retry when connectivity returns.
        """
        try:
            from .network_monitor import get_network_monitor
            is_online = get_network_monitor().is_online
        except RuntimeError:
            is_online = False

        if is_online:
            attempted_sources = list(self._resolver._get_all_priority_sources())
            attempt_map = {
                "_priority_hash": self._resolver.compute_priority_hash(),
                "_attempted_by": attempted_sources,
            }
            self._persist_metadata_updates(game_id, {}, source_map=attempt_map)
            logger.debug(
                f"Game {game_id}: enrichment attempted online, no data — marked as tried"
            )
        else:
            logger.debug(
                f"Game {game_id}: enrichment skipped (offline), will retry when online"
            )

    def _persist_metadata_updates(
        self,
        game_id: str,
        updates: Dict[str, Any],
        source_map: Optional[Dict[str, str]] = None,
    ) -> None:
        """Persist metadata updates to the database.

        Updates the metadata_json of the primary store_game entry.

        Args:
            game_id: Game UUID
            updates: Dict of field_name -> value to update
            source_map: Optional dict of field_name -> source_name for _sources tracking
        """
        if not updates and not source_map:
            return

        session = self.database.new_session()
        try:
            game = (
                session.query(DbGame)
                .options(selectinload(DbGame.store_games))
                .filter_by(id=game_id)
                .first()
            )
            if not game:
                return

            store_game = None
            for sg in game.store_games:
                if sg.store_name == game.primary_store:
                    store_game = sg
                    break

            if not store_game:
                store_game = game.store_games[0] if game.store_games else None

            if not store_game:
                return

            metadata = store_game.metadata_json or {}
            for key, value in updates.items():
                if not value:
                    continue
                metadata[key] = value

            if source_map:
                sources = metadata.setdefault("_sources", {})
                for field, src in source_map.items():
                    if src:
                        sources[field] = src

            store_game.metadata_json = metadata
            flag_modified(store_game, "metadata_json")
            session.commit()

            logger.debug(f"Persisted {len(updates)} metadata fields for game {game_id}")

        except Exception as e:
            logger.warning(f"Failed to persist metadata for {game_id}: {e}")
            session.rollback()
        finally:
            session.close()
