# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# collection_service.py

"""Collection management service for luducat.

Handles CRUD for dynamic filter collections and static game list collections.
Extracted as a standalone service, delegated from GameService.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import text

from .database import Collection, CollectionGame, Database
from .dt import utc_now

logger = logging.getLogger(__name__)


class CollectionService:
    """Manages user collections: saved filters (dynamic) and game lists (static)."""

    def __init__(self, database: Database):
        self.database = database

    # ── CRUD ───────────────────────────────────────────────────────────

    def create_collection(
        self,
        name: str,
        collection_type: str,
        filter_json: Optional[str] = None,
        color: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new collection.

        Args:
            name: Display name
            collection_type: "dynamic" or "static"
            filter_json: Serialized filter dict (dynamic only)
            color: Optional hex color
            notes: Optional user notes

        Returns:
            Dict representation of the created collection
        """
        if collection_type not in ("dynamic", "static"):
            raise ValueError(f"Invalid collection_type: {collection_type!r}")

        session = self.database.get_session()
        try:
            # Position: append after last existing collection
            max_pos = session.query(Collection.position).order_by(
                Collection.position.desc()
            ).first()
            position = (max_pos[0] + 1) if max_pos else 0

            now = utc_now()
            collection = Collection(
                name=name,
                type=collection_type,
                filter_json=filter_json,
                color=color,
                notes=notes,
                position=position,
                created_at=now,
                updated_at=now,
            )
            session.add(collection)
            session.commit()
            result = self._to_dict(collection)
            logger.info(f"Created {collection_type} collection: {name} (id={collection.id})")
            return result
        except Exception:
            session.rollback()
            raise

    def get_collections(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Get all collections ordered by position.

        Args:
            include_hidden: If True, include hidden collections

        Returns:
            List of collection dicts
        """
        session = self.database.get_session()
        query = session.query(Collection).order_by(Collection.position)
        if not include_hidden:
            query = query.filter(Collection.is_hidden == False)  # noqa: E712
        return [self._to_dict(c) for c in query.all()]

    def get_collection(self, collection_id: int) -> Optional[Dict[str, Any]]:
        """Get a single collection by ID."""
        session = self.database.get_session()
        collection = session.get(Collection, collection_id)
        if collection is None:
            return None
        return self._to_dict(collection)

    def update_collection(self, collection_id: int, **kwargs) -> bool:
        """Update collection fields.

        Accepted kwargs: name, color, notes, filter_json, position, is_hidden.

        Returns:
            True if collection was found and updated
        """
        session = self.database.get_session()
        collection = session.get(Collection, collection_id)
        if collection is None:
            return False
        try:
            allowed = {"name", "color", "notes", "filter_json", "position", "is_hidden"}
            for key, value in kwargs.items():
                if key in allowed:
                    setattr(collection, key, value)
            collection.updated_at = utc_now()
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise

    def delete_collection(self, collection_id: int) -> bool:
        """Delete a collection and its game associations.

        Returns:
            True if collection was found and deleted
        """
        session = self.database.get_session()
        collection = session.get(Collection, collection_id)
        if collection is None:
            return False
        try:
            name = collection.name
            session.delete(collection)
            session.commit()
            logger.info(f"Deleted collection: {name} (id={collection_id})")
            return True
        except Exception:
            session.rollback()
            raise

    def reorder_collections(self, id_order: List[int]) -> None:
        """Set collection positions based on ordered list of IDs.

        Args:
            id_order: List of collection IDs in desired order
        """
        session = self.database.get_session()
        try:
            for position, cid in enumerate(id_order):
                session.query(Collection).filter(
                    Collection.id == cid
                ).update({"position": position, "updated_at": utc_now()})
            session.commit()
        except Exception:
            session.rollback()
            raise

    # ── Static collection game management ──────────────────────────────

    def add_games_to_collection(
        self, collection_id: int, game_ids: Set[str]
    ) -> int:
        """Add games to a static collection.

        Args:
            collection_id: Collection ID
            game_ids: Set of game UUIDs to add

        Returns:
            Number of games actually added (excludes already-present)
        """
        session = self.database.get_session()
        try:
            existing = {
                row[0] for row in session.query(CollectionGame.game_id).filter(
                    CollectionGame.collection_id == collection_id
                ).all()
            }
            # Position: append after last existing game
            max_pos_row = session.query(CollectionGame.position).filter(
                CollectionGame.collection_id == collection_id
            ).order_by(CollectionGame.position.desc()).first()
            next_pos = (max_pos_row[0] + 1) if max_pos_row else 0

            now = utc_now()
            added = 0
            for game_id in game_ids:
                if game_id not in existing:
                    session.add(CollectionGame(
                        collection_id=collection_id,
                        game_id=game_id,
                        position=next_pos,
                        added_at=now,
                    ))
                    next_pos += 1
                    added += 1

            if added:
                session.query(Collection).filter(
                    Collection.id == collection_id
                ).update({"updated_at": now})
                session.commit()
                logger.info(f"Added {added} games to collection {collection_id}")
            return added
        except Exception:
            session.rollback()
            raise

    def remove_games_from_collection(
        self, collection_id: int, game_ids: Set[str]
    ) -> int:
        """Remove games from a static collection.

        Returns:
            Number of games actually removed
        """
        session = self.database.get_session()
        try:
            removed = session.query(CollectionGame).filter(
                CollectionGame.collection_id == collection_id,
                CollectionGame.game_id.in_(game_ids),
            ).delete(synchronize_session="fetch")
            if removed:
                session.query(Collection).filter(
                    Collection.id == collection_id
                ).update({"updated_at": utc_now()})
                session.commit()
                logger.info(f"Removed {removed} games from collection {collection_id}")
            return removed
        except Exception:
            session.rollback()
            raise

    def get_collection_game_ids(self, collection_id: int) -> Set[str]:
        """Get all game IDs in a static collection.

        Returns:
            Set of game UUID strings
        """
        session = self.database.get_session()
        rows = session.query(CollectionGame.game_id).filter(
            CollectionGame.collection_id == collection_id
        ).order_by(CollectionGame.position).all()
        return {row[0] for row in rows}

    def get_collection_game_count(self, collection_id: int) -> int:
        """Get number of games in a static collection."""
        session = self.database.get_session()
        return session.query(CollectionGame).filter(
            CollectionGame.collection_id == collection_id
        ).count()

    def get_collection_ids_for_game(self, game_id: str) -> Set[int]:
        """Get IDs of all collections containing a game (single query).

        Returns:
            Set of collection IDs the game belongs to
        """
        session = self.database.get_session()
        rows = session.query(CollectionGame.collection_id).filter(
            CollectionGame.game_id == game_id
        ).all()
        return {row[0] for row in rows}

    # ── Conversion ─────────────────────────────────────────────────────

    def convert_to_static(
        self, collection_id: int, game_ids
    ) -> bool:
        """Convert a dynamic collection to static by snapshotting game IDs.

        Args:
            collection_id: Collection to convert
            game_ids: Iterable of game IDs (order is preserved for position)

        Returns:
            True if conversion succeeded
        """
        session = self.database.get_session()
        collection = session.get(Collection, collection_id)
        if collection is None or collection.type != "dynamic":
            return False
        try:
            now = utc_now()
            collection.type = "static"
            collection.filter_json = None
            collection.updated_at = now

            seen = set()
            position = 0
            for game_id in game_ids:
                if game_id in seen:
                    continue
                seen.add(game_id)
                session.add(CollectionGame(
                    collection_id=collection_id,
                    game_id=game_id,
                    position=position,
                    added_at=now,
                ))
                position += 1
            session.commit()
            logger.info(
                f"Converted collection {collection.name} to static "
                f"with {position} games"
            )
            return True
        except Exception:
            session.rollback()
            raise

    # ── Export / Import ─────────────────────────────────────────────────

    def export_collection(
        self,
        collection_id: int,
        get_game_data: callable,
    ) -> Dict[str, Any]:
        """Export a collection to a serializable dict.

        Args:
            collection_id: Collection to export
            get_game_data: Callback (game_id) -> dict with title, normalized_title, stores

        Returns:
            Export dict suitable for JSON serialization
        """
        session = self.database.get_session()
        collection = session.get(Collection, collection_id)
        if collection is None:
            return {}

        result = {
            "format": "luducat-collection-v1",
            "name": collection.name,
            "type": collection.type,
            "notes": collection.notes,
            "color": collection.color,
            "filter": None,
            "games": None,
        }

        if collection.type == "dynamic":
            result["filter"] = collection.filter_json
        else:
            game_ids = self.get_collection_game_ids(collection_id)
            games = []
            for gid in sorted(game_ids):
                data = get_game_data(gid)
                if data:
                    games.append(data)
            result["games"] = games

        return result

    def import_collection(
        self,
        data: Dict[str, Any],
        mode: str,
        resolve_game_id: callable,
    ) -> Dict[str, Any]:
        """Import a collection from an export dict.

        Args:
            data: Parsed export dict
            mode: "new", "merge", or "overwrite"
            resolve_game_id: Callback (game_export_entry) -> game_id or None

        Returns:
            Stats dict: matched, unmatched, total, collection_id
        """
        name = data.get("name") or "Imported Collection"
        coll_type = data.get("type", "static")
        color = data.get("color")
        notes = data.get("notes")

        # Find existing collection with same name (for merge/overwrite)
        existing = None
        if mode in ("merge", "overwrite"):
            for c in self.get_collections(include_hidden=True):
                if c["name"] == name:
                    existing = c
                    break

        if mode == "new" or existing is None:
            # Always create new
            if existing is not None and mode == "new":
                name = name + " (" + _("imported") + ")"
            coll = self.create_collection(
                name=name,
                collection_type=coll_type,
                filter_json=data.get("filter") if coll_type == "dynamic" else None,
                color=color,
                notes=notes,
            )
            collection_id = coll["id"]
        elif mode == "overwrite":
            collection_id = existing["id"]
            self.update_collection(
                collection_id,
                filter_json=data.get("filter") if coll_type == "dynamic" else None,
                color=color,
                notes=notes,
            )
            if coll_type == "static":
                # Remove all existing games
                old_ids = self.get_collection_game_ids(collection_id)
                if old_ids:
                    self.remove_games_from_collection(collection_id, old_ids)
        else:
            # merge
            collection_id = existing["id"]

        # Add games for static collections
        stats = {"matched": 0, "unmatched": 0, "total": 0, "collection_id": collection_id,
                 "unmatched_titles": []}

        if coll_type == "static" and data.get("games"):
            stats["total"] = len(data["games"])
            matched_ids = set()
            for entry in data["games"]:
                game_id = resolve_game_id(entry)
                if game_id:
                    matched_ids.add(game_id)
                    stats["matched"] += 1
                else:
                    stats["unmatched"] += 1
                    stats["unmatched_titles"].append(
                        entry.get("title", entry.get("normalized_title", "?"))
                    )
            if matched_ids:
                self.add_games_to_collection(collection_id, matched_ids)

        logger.info(
            f"Imported collection '{name}' ({mode}): "
            f"{stats['matched']}/{stats['total']} games matched"
        )
        return stats

    # ── Internal ───────────────────────────────────────────────────────

    def _to_dict(self, collection: Collection) -> Dict[str, Any]:
        """Convert a Collection ORM object to a dict."""
        return {
            "id": collection.id,
            "name": collection.name,
            "type": collection.type,
            "color": collection.color,
            "filter_json": collection.filter_json,
            "position": collection.position,
            "is_hidden": collection.is_hidden,
            "notes": collection.notes,
            "created_at": collection.created_at,
            "updated_at": collection.updated_at,
        }
