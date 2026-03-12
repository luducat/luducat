# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# tag_service.py

"""Tag management service for luducat.

Handles user tag CRUD operations and tag sync from store plugins.
Extracted from GameService to reduce its responsibilities.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import func, text
from sqlalchemy.orm.attributes import flag_modified

from .database import (
    Database,
    Game as DbGame,
    GameTag,
    StoreGame,
    UserTag,
    get_or_create_user_data,
    normalize_title,
)
from .constants import DEFAULT_TAG_COLOR, TAG_SOURCE_COLORS
from .dt import utc_now

logger = logging.getLogger(__name__)


FAMILY_SHARED_TAG_NAME = "Family Shared"
FAMILY_SHARED_TAG_COLOR = "#7289da"  # Discord blurple — distinct, neutral


class TagService:
    """Manages user tags: CRUD, game assignment, and store sync."""

    def __init__(self, database: Database, games_cache: dict, config=None):
        self.database = database
        self._games_cache = games_cache
        self._config = config

    # ── Internal helpers ──────────────────────────────────────────────

    def _tag_to_dict(self, tag: UserTag) -> Dict[str, Any]:
        """Convert a UserTag ORM object to a dict with all fields."""
        return {
            "id": tag.id,
            "name": tag.name,
            "color": tag.color,
            "source": tag.source,
            "tag_type": tag.tag_type,
            "external_id": tag.external_id,
            "description": tag.description,
            "score": tag.score,
            "nsfw_override": tag.nsfw_override,
        }

    # ── Store sync ────────────────────────────────────────────────────

    def _apply_tag_sync_data(
        self, store_name: str, tag_data: Dict[str, Any]
    ) -> Dict[str, int]:
        """Apply tag and favorite sync data from a store plugin.

        Handles mapping store-side user tags to luducat UserTag entries
        and the FAVORITE tag to is_favorite. Tracks previously synced state
        in StoreGame.metadata_json["_store_synced_tags"] to avoid re-adding
        tags the user manually removed (add_only mode) and to detect
        removals (full_sync mode).

        Args:
            store_name: Store identifier (e.g. "gog")
            tag_data: Dict with "mode" and "mappings" from plugin

        Returns:
            Stats dict with counts
        """
        mode = tag_data.get("mode", "add_only")
        mappings = tag_data.get("mappings", {})

        if not mappings:
            return {}

        session = self.database.get_session()
        try:
            stats = {
                "tags_added": 0,
                "tags_removed": 0,
                "favorites_set": 0,
                "favorites_unset": 0,
                "hidden_set": 0,
                "hidden_unset": 0,
            }

            # Pre-fetch/create all needed UserTag objects to minimize queries
            all_tag_names = set()
            for data in mappings.values():
                all_tag_names.update(data.get("tags", []))

            # Build suppression set from config
            suppressed = set()
            if self._config:
                for entry in self._config.get("tags.suppressed_imports", []):
                    if isinstance(entry, dict):
                        suppressed.add((entry.get("name", ""), entry.get("source", "")))

            tag_cache: Dict[str, UserTag] = {}
            for tag_name in all_tag_names:
                # Check re-import suppression
                if (tag_name, store_name) in suppressed:
                    continue
                tag = session.query(UserTag).filter_by(name=tag_name).first()
                if not tag:
                    brand_color = TAG_SOURCE_COLORS.get(store_name) or DEFAULT_TAG_COLOR
                    tag = UserTag(
                        name=tag_name,
                        color=brand_color,
                        source=store_name,
                        tag_type="imported",
                    )
                    session.add(tag)
                    session.flush()
                tag_cache[tag_name] = tag

            for store_app_id, data in mappings.items():
                store_game = session.query(StoreGame).filter_by(
                    store_name=store_name,
                    store_app_id=store_app_id,
                ).first()
                if not store_game:
                    continue

                game_id = store_game.game_id
                game = session.query(DbGame).filter_by(id=game_id).first()
                if not game:
                    continue

                # Get previously synced state from metadata_json
                meta = dict(store_game.metadata_json or {})
                prev_synced_tags = set(meta.get("_store_synced_tags", []))
                prev_synced_fav = meta.get("_store_synced_favorite", False)

                current_tags = set(data.get("tags", []))
                current_fav = data.get("favorite", False)

                # ── Favorite sync ──
                if current_fav and not prev_synced_fav:
                    user_data = get_or_create_user_data(session, game_id)
                    if not user_data.is_favorite:
                        user_data.is_favorite = True
                        stats["favorites_set"] += 1
                elif mode == "full_sync" and prev_synced_fav and not current_fav:
                    user_data = get_or_create_user_data(session, game_id)
                    if user_data.is_favorite:
                        user_data.is_favorite = False
                        stats["favorites_unset"] += 1

                # ── Tag sync ──
                current_game_tags = {t.name for t in game.tags}

                tags_to_add = current_tags - prev_synced_tags
                for tag_name in tags_to_add:
                    if tag_name not in current_game_tags:
                        tag = tag_cache[tag_name]
                        game.tags.append(tag)
                        stats["tags_added"] += 1

                if mode == "full_sync":
                    tags_to_remove = prev_synced_tags - current_tags
                    for tag_name in tags_to_remove:
                        if tag_name in current_game_tags:
                            tag = tag_cache.get(tag_name)
                            if tag and tag in game.tags:
                                game.tags.remove(tag)
                                stats["tags_removed"] += 1

                # ── Hidden sync ──
                current_hidden = data.get("hidden", False)
                prev_synced_hidden = meta.get("_store_synced_hidden", False)

                if current_hidden and not prev_synced_hidden:
                    user_data = get_or_create_user_data(session, game_id)
                    if not user_data.is_hidden:
                        user_data.is_hidden = True
                        stats["hidden_set"] += 1
                elif mode == "full_sync" and prev_synced_hidden and not current_hidden:
                    user_data = get_or_create_user_data(session, game_id)
                    if user_data.is_hidden:
                        user_data.is_hidden = False
                        stats["hidden_unset"] += 1

                # Update synced state
                meta["_store_synced_tags"] = sorted(current_tags)
                meta["_store_synced_favorite"] = current_fav
                meta["_store_synced_hidden"] = current_hidden
                store_game.metadata_json = meta
                flag_modified(store_game, "metadata_json")

            session.commit()

            if stats["tags_added"] or stats["tags_removed"]:
                logger.info(
                    f"Tag sync for {store_name}: {stats['tags_added']} added, "
                    f"{stats['tags_removed']} removed"
                )
            if stats["favorites_set"] or stats["favorites_unset"]:
                logger.info(
                    f"Favorite sync for {store_name}: {stats['favorites_set']} set, "
                    f"{stats['favorites_unset']} unset"
                )
            if stats["hidden_set"] or stats["hidden_unset"]:
                logger.info(
                    f"Hidden sync for {store_name}: {stats['hidden_set']} set, "
                    f"{stats['hidden_unset']} unset"
                )

            return stats
        except Exception as e:
            logger.warning(f"Tag sync failed for {store_name}: {e}")
            session.rollback()
            return {}
        finally:
            session.close()

    # ── Tag CRUD ──────────────────────────────────────────────────────

    def add_tag(self, game_id: str, tag_name: str) -> None:
        """Add a tag to a game.

        Args:
            game_id: Game UUID
            tag_name: Tag name
        """
        session = self.database.get_session()
        try:
            tag = session.query(UserTag).filter_by(name=tag_name).first()
            if not tag:
                tag = UserTag(name=tag_name)
                session.add(tag)

            game = session.query(DbGame).filter_by(id=game_id).first()
            if game and tag not in game.tags:
                game.tags.append(tag)
                session.commit()

                if game_id in self._games_cache:
                    self._games_cache[game_id].tags.append(
                        {"name": tag_name, "color": tag.color, "source": tag.source}
                    )
        except Exception as e:
            logger.warning(f"Failed to add tag '{tag_name}' to game {game_id}: {e}")
            session.rollback()
        finally:
            session.close()

    def remove_tag(self, game_id: str, tag_name: str) -> None:
        """Remove a tag from a game.

        Args:
            game_id: Game UUID
            tag_name: Tag name
        """
        session = self.database.get_session()
        try:
            game = session.query(DbGame).filter_by(id=game_id).first()
            tag = session.query(UserTag).filter_by(name=tag_name).first()

            if game and tag and tag in game.tags:
                game.tags.remove(tag)
                session.commit()

                if game_id in self._games_cache:
                    tags = self._games_cache[game_id].tags
                    self._games_cache[game_id].tags = [
                        t for t in tags if t.get("name") != tag_name
                    ]
        except Exception as e:
            logger.warning(f"Failed to remove tag '{tag_name}' from game {game_id}: {e}")
            session.rollback()
        finally:
            session.close()

    def get_all_tags(self) -> List[Dict[str, Any]]:
        """Get all user tags.

        Returns:
            List of tag dicts with all fields
        """
        session = self.database.get_session()
        try:
            tags = session.query(UserTag).order_by(UserTag.name).all()
            return [self._tag_to_dict(t) for t in tags]
        except Exception as e:
            logger.warning(f"Failed to get all tags: {e}")
            return []
        finally:
            session.close()

    def get_tag_game_counts(self) -> Dict[int, int]:
        """Get the number of games assigned to each tag.

        Returns:
            Dict mapping tag_id to game count.
        """
        session = self.database.get_session()
        try:
            rows = (
                session.query(GameTag.tag_id, func.count(GameTag.game_id))
                .group_by(GameTag.tag_id)
                .all()
            )
            return {tag_id: count for tag_id, count in rows}
        except Exception as e:
            logger.warning(f"Failed to get tag game counts: {e}")
            return {}
        finally:
            session.close()

    def get_game_tags(self, game_id: str) -> List[Dict[str, Any]]:
        """Get tags for a specific game.

        Args:
            game_id: Game UUID

        Returns:
            List of tag dicts with all fields
        """
        session = self.database.get_session()
        try:
            game = session.query(DbGame).filter_by(id=game_id).first()
            if not game:
                return []
            return [self._tag_to_dict(t) for t in game.tags]
        except Exception as e:
            logger.warning(f"Failed to get tags for game {game_id}: {e}")
            return []
        finally:
            session.close()

    def create_tag(
        self,
        name: str,
        color: str = DEFAULT_TAG_COLOR,
        source: str = "native",
        tag_type: str = "user",
        external_id: Optional[str] = None,
        description: Optional[str] = None,
        nsfw_override: int = 0,
    ) -> Dict[str, Any]:
        """Create a new tag.

        Args:
            name: Tag name (must be unique)
            color: Hex color string (e.g., DEFAULT_TAG_COLOR)
            source: Who created the tag — "native", "gog", "steam", etc.
            tag_type: "user", "imported", or "special"
            external_id: Store's tag ID for two-way sync
            description: User-editable description
            nsfw_override: Content filter override (0=neutral, 1=NSFW, -1=SFW)

        Returns:
            Dict with all tag fields

        Raises:
            ValueError: If tag name already exists
        """
        session = self.database.get_session()
        try:
            existing = session.query(UserTag).filter_by(name=name).first()
            if existing:
                raise ValueError(f"Tag '{name}' already exists")

            tag = UserTag(
                name=name,
                color=color,
                source=source,
                tag_type=tag_type,
                external_id=external_id,
                description=description,
                nsfw_override=nsfw_override,
            )
            session.add(tag)
            session.commit()

            logger.info(f"Created tag: {name} ({color}, source={source})")
            return self._tag_to_dict(tag)
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Failed to create tag '{name}': {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def update_tag(
        self,
        tag_id: int,
        name: Optional[str] = None,
        color: Optional[str] = None,
        description: Optional[str] = None,
        score: Optional[int] = None,
        nsfw_override: Optional[int] = None,
    ) -> None:
        """Update a tag's properties.

        Args:
            tag_id: Tag ID
            name: New name (optional)
            color: New color (optional)
            description: New description (optional)
            score: Quick-access score -99..+99 (optional)
            nsfw_override: Content filter override (0=neutral, 1=NSFW, -1=SFW)

        Raises:
            ValueError: If tag not found or new name already exists
        """
        session = self.database.get_session()
        try:
            tag = session.query(UserTag).filter_by(id=tag_id).first()
            if not tag:
                raise ValueError(f"Tag with ID {tag_id} not found")

            if name is not None and name != tag.name:
                existing = session.query(UserTag).filter_by(name=name).first()
                if existing:
                    raise ValueError(f"Tag '{name}' already exists")
                old_name = tag.name
                tag.name = name
                logger.info(f"Renamed tag: {old_name} -> {name}")

            if color is not None:
                tag.color = color

            if description is not None:
                tag.description = description

            if score is not None:
                tag.score = max(-99, min(99, score))

            if nsfw_override is not None:
                tag.nsfw_override = nsfw_override

            session.commit()
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Failed to update tag {tag_id}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def delete_tag(self, tag_id: int) -> None:
        """Delete a tag (removes from all games via cascade).

        Args:
            tag_id: Tag ID

        Raises:
            ValueError: If tag not found
        """
        session = self.database.get_session()
        try:
            tag = session.query(UserTag).filter_by(id=tag_id).first()
            if not tag:
                raise ValueError(f"Tag with ID {tag_id} not found")

            tag_name = tag.name
            tag_source = tag.source
            tag_type = tag.tag_type

            session.delete(tag)
            session.commit()

            # Add to re-import suppression if it's an imported tag
            if tag_type == "imported" and self._config:
                if self._config.get("tags.suppress_deleted_reimport", True):
                    suppressed = self._config.get("tags.suppressed_imports", [])
                    entry = {"name": tag_name, "source": tag_source}
                    if entry not in suppressed:
                        suppressed.append(entry)
                        self._config.set("tags.suppressed_imports", suppressed)
                        self._config.save()
                        logger.debug(f"Suppressed re-import of tag '{tag_name}' from {tag_source}")

            # Invalidate cache entries that had this tag
            for game_id, game_data in self._games_cache.items():
                game_data.tags = [
                    t for t in game_data.tags
                    if t.get("name") != tag_name
                ]

            logger.info(f"Deleted tag: {tag_name}")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Failed to delete tag {tag_id}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def set_game_tags(self, game_id: str, tag_names: List[str]) -> None:
        """Set all tags for a game (replaces existing tags).

        Args:
            game_id: Game UUID
            tag_names: List of tag names to set
        """
        session = self.database.get_session()
        try:
            game = session.query(DbGame).filter_by(id=game_id).first()
            if not game:
                return

            current_tags = {t.name for t in game.tags}
            new_tags = set(tag_names)

            for tag in list(game.tags):
                if tag.name not in new_tags:
                    game.tags.remove(tag)

            for tag_name in new_tags - current_tags:
                tag = session.query(UserTag).filter_by(name=tag_name).first()
                if tag:
                    game.tags.append(tag)

            session.commit()

            if game_id in self._games_cache:
                self._games_cache[game_id].tags = [
                    {"name": t.name, "color": t.color, "source": t.source} for t in game.tags
                ]
        except Exception as e:
            logger.warning(f"Failed to set tags for game {game_id}: {e}")
            session.rollback()
        finally:
            session.close()

    # ── New methods for multi-source tag system ───────────────────────

    def get_tags_by_source(self, source: str) -> List[Dict[str, Any]]:
        """Get all tags from a specific source.

        Args:
            source: Source identifier ("native", "gog", "steam", etc.)

        Returns:
            List of tag dicts
        """
        session = self.database.get_session()
        try:
            tags = (
                session.query(UserTag)
                .filter_by(source=source)
                .order_by(UserTag.name)
                .all()
            )
            return [self._tag_to_dict(t) for t in tags]
        except Exception as e:
            logger.warning(f"Failed to get tags by source '{source}': {e}")
            return []
        finally:
            session.close()

    def get_tags_by_type(self, tag_type: str) -> List[Dict[str, Any]]:
        """Get all tags of a specific type.

        Args:
            tag_type: Tag type ("user", "imported", "special")

        Returns:
            List of tag dicts
        """
        session = self.database.get_session()
        try:
            tags = (
                session.query(UserTag)
                .filter_by(tag_type=tag_type)
                .order_by(UserTag.name)
                .all()
            )
            return [self._tag_to_dict(t) for t in tags]
        except Exception as e:
            logger.warning(f"Failed to get tags by type '{tag_type}': {e}")
            return []
        finally:
            session.close()

    def merge_tags(self, keep_id: int, absorb_id: int) -> None:
        """Merge two tags by reassigning all game_tags from absorbed to kept.

        Args:
            keep_id: Tag ID to keep
            absorb_id: Tag ID to absorb (will be deleted)

        Raises:
            ValueError: If either tag not found or IDs are the same
        """
        if keep_id == absorb_id:
            raise ValueError("Cannot merge a tag with itself")

        session = self.database.get_session()
        try:
            keep_tag = session.query(UserTag).filter_by(id=keep_id).first()
            absorb_tag = session.query(UserTag).filter_by(id=absorb_id).first()

            if not keep_tag:
                raise ValueError(f"Tag with ID {keep_id} not found")
            if not absorb_tag:
                raise ValueError(f"Tag with ID {absorb_id} not found")

            # Get all game_ids that have the absorbed tag
            absorb_game_tags = (
                session.query(GameTag)
                .filter_by(tag_id=absorb_id)
                .all()
            )

            # Get game_ids that already have the kept tag
            keep_game_ids = {
                gt.game_id
                for gt in session.query(GameTag).filter_by(tag_id=keep_id).all()
            }

            # Reassign game_tags: move to keep_id if not already present
            for gt in absorb_game_tags:
                if gt.game_id not in keep_game_ids:
                    # Update the tag_id to point to the kept tag
                    # SQLite doesn't support UPDATE on composite PK well, so delete + insert
                    session.execute(
                        text(
                            "INSERT OR IGNORE INTO game_tags (game_id, tag_id, assigned_by, assigned_at) "
                            "VALUES (:game_id, :tag_id, :assigned_by, :assigned_at)"
                        ),
                        {
                            "game_id": gt.game_id,
                            "tag_id": keep_id,
                            "assigned_by": gt.assigned_by,
                            "assigned_at": gt.assigned_at.isoformat() if gt.assigned_at else None,
                        },
                    )

            # Delete absorbed tag (CASCADE removes remaining game_tags)
            absorb_name = absorb_tag.name
            session.delete(absorb_tag)
            session.commit()

            # Invalidate cache
            for game_id, game_data in self._games_cache.items():
                tags = game_data.tags
                had_absorb = any(t.get("name") == absorb_name for t in tags)
                if had_absorb:
                    game_data.tags = [
                        t for t in tags if t.get("name") != absorb_name
                    ]
                    # Add kept tag if not already present
                    if not any(t.get("name") == keep_tag.name for t in game_data.tags):
                        game_data.tags.append(
                            {"name": keep_tag.name, "color": keep_tag.color, "source": keep_tag.source}
                        )

            logger.info(f"Merged tag '{absorb_name}' into '{keep_tag.name}'")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Failed to merge tags {keep_id} <- {absorb_id}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def set_tag_score(self, tag_id: int, score: int) -> None:
        """Set the quick-access score for a tag.

        Args:
            tag_id: Tag ID
            score: Score value, clamped to -99..+99
        """
        session = self.database.get_session()
        try:
            tag = session.query(UserTag).filter_by(id=tag_id).first()
            if tag:
                tag.score = max(-99, min(99, score))
                session.commit()
        except Exception as e:
            logger.warning(f"Failed to set score for tag {tag_id}: {e}")
            session.rollback()
        finally:
            session.close()

    def get_scored_tags(self, min_score: int = 1) -> List[Dict[str, Any]]:
        """Get tags with score at or above a threshold.

        Args:
            min_score: Minimum score to include (default 1 = all preferred)

        Returns:
            List of tag dicts ordered by score desc, name asc
        """
        session = self.database.get_session()
        try:
            tags = (
                session.query(UserTag)
                .filter(UserTag.score >= min_score)
                .order_by(UserTag.score.desc(), UserTag.name)
                .all()
            )
            return [self._tag_to_dict(t) for t in tags]
        except Exception as e:
            logger.warning(f"Failed to get scored tags: {e}")
            return []
        finally:
            session.close()

    def get_quick_access_tags(self, max_count: int = 5) -> List[Dict[str, Any]]:
        """Get quick-access tags: scored > 0 first, then by frequency.

        Algorithm:
        1. Score > 0 tags first (order by score desc, name asc)
        2. Fill remaining slots with score == 0 tags by usage frequency
        3. Score < 0 tags never appear

        Args:
            max_count: Maximum number of tags to return (0..20)

        Returns:
            List of tag dicts
        """
        if max_count <= 0:
            return []

        session = self.database.get_session()
        try:
            # Get positive-scored tags first
            scored = (
                session.query(UserTag)
                .filter(UserTag.score > 0)
                .order_by(UserTag.score.desc(), UserTag.name)
                .all()
            )
            result = [self._tag_to_dict(t) for t in scored]

            if len(result) >= max_count:
                return result[:max_count]

            # Fill remaining slots with score==0 tags by usage frequency
            scored_ids = {t.id for t in scored}
            remaining = max_count - len(result)

            freq_tags = (
                session.query(UserTag, func.count(GameTag.game_id).label("usage"))
                .outerjoin(GameTag, UserTag.id == GameTag.tag_id)
                .filter(UserTag.score == 0)
                .group_by(UserTag.id)
                .order_by(func.count(GameTag.game_id).desc(), UserTag.name)
                .limit(remaining)
                .all()
            )

            for tag, _count in freq_tags:
                if tag.id not in scored_ids:
                    result.append(self._tag_to_dict(tag))

            return result[:max_count]
        except Exception as e:
            logger.warning(f"Failed to get quick access tags: {e}")
            return []
        finally:
            session.close()

    def get_tag_usage_counts(self) -> Dict[int, int]:
        """Get game counts per tag.

        Returns:
            Dict mapping tag_id → number of games with that tag
        """
        session = self.database.get_session()
        try:
            rows = (
                session.query(GameTag.tag_id, func.count(GameTag.game_id))
                .group_by(GameTag.tag_id)
                .all()
            )
            return {tag_id: count for tag_id, count in rows}
        except Exception as e:
            logger.warning(f"Failed to get tag usage counts: {e}")
            return {}
        finally:
            session.close()

    def export_tags(self) -> List[Dict[str, Any]]:
        """Export all tags with their game assignments as JSON-serializable data.

        Each tag includes both ``game_ids`` (backward compat) and
        ``game_mappings`` with title and store identifiers for cross-install
        matching.

        Returns:
            List of tag dicts with game_ids and game_mappings lists
        """
        session = self.database.get_session()
        try:
            tags = session.query(UserTag).order_by(UserTag.name).all()
            result = []
            for tag in tags:
                tag_dict = self._tag_to_dict(tag)
                # Add game assignments
                game_tags = (
                    session.query(GameTag)
                    .filter_by(tag_id=tag.id)
                    .all()
                )
                tag_dict["game_ids"] = [gt.game_id for gt in game_tags]

                # Build rich game_mappings for cross-install matching
                game_mappings = []
                for gt in game_tags:
                    game = session.query(DbGame).filter_by(id=gt.game_id).first()
                    if not game:
                        continue
                    stores = []
                    for sg in game.store_games:
                        stores.append({
                            "store": sg.store_name,
                            "app_id": sg.store_app_id,
                        })
                    game_mappings.append({
                        "game_id": gt.game_id,
                        "title": game.title,
                        "stores": stores,
                    })
                tag_dict["game_mappings"] = game_mappings

                result.append(tag_dict)
            return result
        except Exception as e:
            logger.warning(f"Failed to export tags: {e}")
            return []
        finally:
            session.close()

    def import_tags(self, tag_data: List[Dict[str, Any]]) -> Dict[str, int]:
        """Import tags from exported data, creating or merging as needed.

        Uses multi-strategy game matching when ``game_mappings`` are present:
        1. store + app_id → StoreGame lookup (most reliable cross-install)
        2. normalized_title → Game lookup
        3. game_id UUID → Game lookup (same install only)

        Falls back to game_ids-only matching for older export formats.

        Args:
            tag_data: List of tag dicts (from export_tags format)

        Returns:
            Stats dict with counts of created, merged, assignments, unmatched
        """
        session = self.database.get_session()
        stats = {"created": 0, "merged": 0, "assignments": 0, "unmatched": 0}
        try:
            for entry in tag_data:
                name = entry.get("name", "").strip()
                if not name:
                    continue

                tag = session.query(UserTag).filter_by(name=name).first()
                if not tag:
                    tag = UserTag(
                        name=name,
                        color=entry.get("color", DEFAULT_TAG_COLOR),
                        source=entry.get("source", "native"),
                        tag_type=entry.get("tag_type", "user"),
                        external_id=entry.get("external_id"),
                        description=entry.get("description"),
                    )
                    session.add(tag)
                    session.flush()
                    stats["created"] += 1
                else:
                    stats["merged"] += 1

                # Resolve games — prefer game_mappings (rich), fall back to game_ids
                game_mappings = entry.get("game_mappings", [])
                if game_mappings:
                    for mapping in game_mappings:
                        game = self._resolve_game_from_mapping(
                            session, mapping
                        )
                        if game and tag not in game.tags:
                            game.tags.append(tag)
                            stats["assignments"] += 1
                        elif not game:
                            stats["unmatched"] += 1
                else:
                    # Legacy format: game_ids only
                    for game_id in entry.get("game_ids", []):
                        game = session.query(DbGame).filter_by(id=game_id).first()
                        if game and tag not in game.tags:
                            game.tags.append(tag)
                            stats["assignments"] += 1
                        elif not game:
                            stats["unmatched"] += 1

            session.commit()
            logger.info(
                f"Tag import: {stats['created']} created, "
                f"{stats['merged']} merged, {stats['assignments']} assignments, "
                f"{stats['unmatched']} unmatched"
            )
            return stats
        except Exception as e:
            logger.warning(f"Failed to import tags: {e}")
            session.rollback()
            return stats
        finally:
            session.close()

    def _resolve_game_from_mapping(
        self, session, mapping: Dict[str, Any]
    ) -> Optional[DbGame]:
        """Resolve a game from an export mapping using multi-strategy matching.

        Strategy order (first match wins):
        1. store + app_id → StoreGame lookup
        2. normalized_title → Game lookup
        3. game_id UUID → Game lookup (same install)

        Args:
            session: Active SQLAlchemy session
            mapping: Dict with game_id, title, stores

        Returns:
            Game object or None
        """
        # Strategy 1: store + app_id (most reliable)
        for store_ref in mapping.get("stores", []):
            sg = (
                session.query(StoreGame)
                .filter_by(
                    store_name=store_ref.get("store", ""),
                    store_app_id=store_ref.get("app_id", ""),
                )
                .first()
            )
            if sg:
                return session.query(DbGame).filter_by(id=sg.game_id).first()

        # Strategy 2: normalized title
        title = mapping.get("title", "")
        if title:
            norm = normalize_title(title)
            game = session.query(DbGame).filter_by(normalized_title=norm).first()
            if game:
                return game

        # Strategy 3: game_id UUID (same install)
        game_id = mapping.get("game_id", "")
        if game_id:
            return session.query(DbGame).filter_by(id=game_id).first()

        return None

    # ── System tag management ────────────────────────────────────────

    def _ensure_system_tag(
        self, session, name: str, color: str
    ) -> UserTag:
        """Get or create a system-sourced tag.

        Args:
            session: Active SQLAlchemy session
            name: Tag name
            color: Hex color string

        Returns:
            The existing or newly created UserTag
        """
        tag = session.query(UserTag).filter_by(name=name).first()
        if not tag:
            tag = UserTag(
                name=name,
                color=color,
                source="system",
                tag_type="special",
            )
            session.add(tag)
            session.flush()
            logger.info(f"Created system tag: {name}")
        return tag

    def sync_family_shared_tags(
        self, family_game_ids: set, all_game_ids: set
    ) -> Dict[str, int]:
        """Sync the "Family Shared" system tag to match family sharing state.

        Assigns the tag to games in family_game_ids and removes it from
        games in all_game_ids that are NOT family-shared.

        Args:
            family_game_ids: Set of game UUIDs that are family-shared
            all_game_ids: Set of all game UUIDs (for removal scope)

        Returns:
            Stats dict with added/removed counts
        """
        if not family_game_ids and not all_game_ids:
            return {}

        session = self.database.get_session()
        try:
            stats = {"added": 0, "removed": 0}

            tag = self._ensure_system_tag(
                session, FAMILY_SHARED_TAG_NAME, FAMILY_SHARED_TAG_COLOR
            )

            # Get current assignments for this tag
            current_assignments = {
                gt.game_id
                for gt in session.query(GameTag).filter_by(tag_id=tag.id).all()
            }

            # Add tag to family-shared games that don't have it
            to_add = family_game_ids - current_assignments
            for game_id in to_add:
                game = session.query(DbGame).filter_by(id=game_id).first()
                if game and tag not in game.tags:
                    game.tags.append(tag)
                    stats["added"] += 1

            # Remove tag from non-family-shared games that have it
            to_remove = (current_assignments & all_game_ids) - family_game_ids
            for game_id in to_remove:
                game = session.query(DbGame).filter_by(id=game_id).first()
                if game and tag in game.tags:
                    game.tags.remove(tag)
                    stats["removed"] += 1

            session.commit()

            if stats["added"] or stats["removed"]:
                logger.info(
                    f"Family shared tag sync: {stats['added']} added, "
                    f"{stats['removed']} removed"
                )

            return stats
        except Exception as e:
            logger.warning(f"Family shared tag sync failed: {e}")
            session.rollback()
            return {}
        finally:
            session.close()

    # ── Metadata plugin tag sync ─────────────────────────────────────

    def _apply_metadata_tag_sync(
        self, source: str, mode: str, entries: List[Dict],
        removals: Optional[List[Dict]] = None,
    ) -> Dict[str, int]:
        """Apply tags/favourites from a metadata plugin (e.g. Heroic, Lutris).

        Two-step game resolution per entry:
        1. StoreGame lookup by (store_name, store_app_id) → Game
        2. Fallback: normalize_title(title) → Game.normalized_title

        Tags are created with source=source, tag_type="imported".

        Supports delta mode:
        - entries may contain "removed_tags", "unfavorite", "unhidden"
        - removals list contains entries that disappeared from the source

        Args:
            source: Tag source identifier (e.g. "heroic", "lutris")
            mode: "add_only", "full_sync", or "delta"
            entries: List of dicts with keys:
                store, app_id, title, tags (list[str]), favorite (bool),
                hidden (bool), removed_tags (list[str]), unfavorite (bool),
                unhidden (bool)
            removals: Optional list of removal dicts (delta mode)

        Returns:
            Stats dict with counts
        """
        if not entries and not removals:
            return {}

        session = self.database.get_session()
        try:
            stats = {
                "tags_added": 0,
                "tags_removed": 0,
                "favorites_set": 0,
                "favorites_unset": 0,
                "hidden_set": 0,
                "hidden_unset": 0,
                "games_matched": 0,
                "games_unmatched": 0,
            }

            # Pre-fetch/create all needed UserTag objects
            all_tag_names: set = set()
            for entry in entries:
                all_tag_names.update(entry.get("tags", []))

            # Build suppression set from config
            suppressed = set()
            if self._config:
                for sup_entry in self._config.get("tags.suppressed_imports", []):
                    if isinstance(sup_entry, dict):
                        suppressed.add((sup_entry.get("name", ""), sup_entry.get("source", "")))

            tag_cache: Dict[str, UserTag] = {}
            for tag_name in all_tag_names:
                # Check re-import suppression
                if (tag_name, source) in suppressed:
                    continue
                tag = session.query(UserTag).filter_by(name=tag_name).first()
                if not tag:
                    brand_color = TAG_SOURCE_COLORS.get(source) or DEFAULT_TAG_COLOR
                    tag = UserTag(
                        name=tag_name,
                        color=brand_color,
                        source=source,
                        tag_type="imported",
                    )
                    session.add(tag)
                    session.flush()
                tag_cache[tag_name] = tag

            is_delta = mode == "delta"

            for entry in entries:
                game = self._resolve_game_for_entry(session, entry)
                if not game:
                    stats["games_unmatched"] += 1
                    continue

                stats["games_matched"] += 1

                # Apply tags
                current_game_tags = {t.name for t in game.tags}
                for tag_name in entry.get("tags", []):
                    if tag_name not in current_game_tags:
                        tag = tag_cache.get(tag_name)
                        if tag:
                            game.tags.append(tag)
                            current_game_tags.add(tag_name)
                            stats["tags_added"] += 1

                # Remove tags (delta mode)
                if is_delta:
                    for tag_name in entry.get("removed_tags", []):
                        if tag_name in current_game_tags:
                            tag = session.query(UserTag).filter_by(
                                name=tag_name, source=source
                            ).first()
                            if tag and tag in game.tags:
                                game.tags.remove(tag)
                                stats["tags_removed"] += 1

                # Apply favourite
                is_fav = entry.get("favorite", False)
                if is_fav:
                    user_data = get_or_create_user_data(session, game.id)
                    if not user_data.is_favorite:
                        user_data.is_favorite = True
                        stats["favorites_set"] += 1

                # Unfavorite (delta mode)
                if is_delta and entry.get("unfavorite"):
                    user_data = get_or_create_user_data(session, game.id)
                    if user_data.is_favorite:
                        user_data.is_favorite = False
                        stats["favorites_unset"] += 1

                # Apply hidden
                if entry.get("hidden"):
                    user_data = get_or_create_user_data(session, game.id)
                    if not user_data.is_hidden:
                        user_data.is_hidden = True
                        stats["hidden_set"] += 1

                # Unhide (delta mode)
                if is_delta and entry.get("unhidden"):
                    user_data = get_or_create_user_data(session, game.id)
                    if user_data.is_hidden:
                        user_data.is_hidden = False
                        stats["hidden_unset"] += 1

            # Process removals (games removed from source)
            if is_delta and removals:
                for removal in removals:
                    game = self._resolve_game_for_entry(session, removal)
                    if not game:
                        continue

                    # Remove source-owned tags
                    current_game_tags = {t.name for t in game.tags}
                    for tag_name in removal.get("tags", []):
                        if tag_name in current_game_tags:
                            tag = session.query(UserTag).filter_by(
                                name=tag_name, source=source
                            ).first()
                            if tag and tag in game.tags:
                                game.tags.remove(tag)
                                stats["tags_removed"] += 1

                    # Unfavorite
                    if removal.get("unfavorite"):
                        user_data = get_or_create_user_data(session, game.id)
                        if user_data.is_favorite:
                            user_data.is_favorite = False
                            stats["favorites_unset"] += 1

                    # Unhide
                    if removal.get("unhidden"):
                        user_data = get_or_create_user_data(session, game.id)
                        if user_data.is_hidden:
                            user_data.is_hidden = False
                            stats["hidden_unset"] += 1

            session.commit()

            if stats["tags_added"] or stats["tags_removed"]:
                logger.info(
                    f"Metadata tag sync ({source}): "
                    f"{stats['tags_added']} added, "
                    f"{stats['tags_removed']} removed across "
                    f"{stats['games_matched']} games"
                )
            if stats["favorites_set"] or stats["favorites_unset"]:
                logger.info(
                    f"Metadata favourite sync ({source}): "
                    f"{stats['favorites_set']} set, "
                    f"{stats['favorites_unset']} unset"
                )
            if stats["hidden_set"] or stats["hidden_unset"]:
                logger.info(
                    f"Metadata hidden sync ({source}): "
                    f"{stats['hidden_set']} set, "
                    f"{stats['hidden_unset']} unset"
                )
            if stats["games_unmatched"]:
                logger.debug(
                    f"Metadata tag sync ({source}): "
                    f"{stats['games_unmatched']} entries could not be matched"
                )

            return stats
        except Exception as e:
            logger.warning(f"Metadata tag sync failed for {source}: {e}")
            session.rollback()
            return {}
        finally:
            session.close()

    def _resolve_game_for_entry(
        self, session, entry: Dict[str, Any]
    ) -> Optional["DbGame"]:
        """Resolve a game from a sync entry via store+app_id or title.

        Args:
            session: SQLAlchemy session
            entry: Dict with store, app_id, title

        Returns:
            DbGame or None
        """
        store = entry.get("store", "")
        app_id = entry.get("app_id", "")
        title = entry.get("title", "")

        game = None
        if store and app_id:
            store_game = session.query(StoreGame).filter_by(
                store_name=store,
                store_app_id=app_id,
            ).first()
            if store_game:
                game = session.query(DbGame).filter_by(
                    id=store_game.game_id
                ).first()

        if not game and title:
            norm = normalize_title(title)
            if norm:
                game = session.query(DbGame).filter_by(
                    normalized_title=norm,
                ).first()

        return game
