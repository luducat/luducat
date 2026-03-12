# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# db_accessor.py

"""MainDbAccessor — controlled main DB access for plugins.

Provides dict-based interfaces so plugins don't need to import
ORM models (Database, StoreGame, etc.) directly.  Each accessor
is scoped to a single store_name.

Injected into plugins via ``AbstractGameStore.set_main_db_accessor()``.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from .config import get_data_dir
from .database import Database, StoreGame

logger = logging.getLogger(__name__)


class MainDbAccessor:
    """Dict-based proxy for controlled main DB access from plugins.

    Each instance is scoped to a single ``store_name`` so plugins
    can only interact with their own store data.
    """

    def __init__(self, game_service: Any, store_name: str):
        self._game_service = game_service
        self._store_name = store_name

    # ── Metadata patching ─────────────────────────────────────────

    def patch_metadata_json(
        self, store_app_id: str, patches: Dict[str, Any]
    ) -> bool:
        """Merge *patches* into a StoreGame's ``metadata_json``.

        Args:
            store_app_id: The store-specific app/product ID.
            patches: Key-value pairs to merge into metadata_json.

        Returns:
            True if the store game was found and patched.
        """
        try:
            session = self._game_service.database.get_session()
            store_game = session.query(StoreGame).filter_by(
                store_name=self._store_name,
                store_app_id=str(store_app_id),
            ).first()

            if not store_game:
                return False

            meta = store_game.metadata_json
            if meta is None:
                meta = {}
                store_game.metadata_json = meta

            meta.update(patches)
            flag_modified(store_game, "metadata_json")
            session.commit()
            return True

        except Exception as e:
            logger.error(
                "patch_metadata_json(%s, %s) failed: %s",
                self._store_name, store_app_id, e,
            )
            return False

    def batch_patch_metadata_json(
        self, patches_by_id: Dict[str, Dict[str, Any]]
    ) -> int:
        """Patch metadata for multiple games in a single transaction.

        Args:
            patches_by_id: ``{store_app_id: {field: value, ...}, ...}``

        Returns:
            Number of games successfully patched.
        """
        if not patches_by_id:
            return 0

        try:
            session = self._game_service.database.get_session()
            updated = 0

            for store_app_id, patches in patches_by_id.items():
                store_game = session.query(StoreGame).filter_by(
                    store_name=self._store_name,
                    store_app_id=str(store_app_id),
                ).first()

                if not store_game:
                    continue

                meta = store_game.metadata_json
                if meta is None:
                    meta = {}
                    store_game.metadata_json = meta

                meta.update(patches)
                flag_modified(store_game, "metadata_json")
                updated += 1

            session.commit()
            return updated

        except Exception as e:
            logger.error(
                "batch_patch_metadata_json(%s) failed: %s",
                self._store_name, e,
            )
            return 0

    # ── Query helpers ─────────────────────────────────────────────

    def get_store_game_count(self) -> int:
        """Count store games for this store."""
        try:
            session = self._game_service.database.get_session()
            return session.query(StoreGame).filter_by(
                store_name=self._store_name,
            ).count()
        except Exception as e:
            logger.error("get_store_game_count(%s) failed: %s",
                         self._store_name, e)
            return 0

    def get_exclusive_game_count(self) -> int:
        """Count games that exist only in this store."""
        try:
            return self._game_service.count_store_exclusive_games(
                self._store_name
            )
        except Exception as e:
            logger.error("get_exclusive_game_count(%s) failed: %s",
                         self._store_name, e)
            return 0

    def get_store_image_urls(self) -> List[str]:
        """Collect all image URLs from this store's games' metadata.

        Extracts cover, header, hero, background, and screenshot URLs.
        """
        urls: List[str] = []
        try:
            session = self._game_service.database.get_session()
            store_games = session.query(StoreGame).filter_by(
                store_name=self._store_name,
            ).all()

            for sg in store_games:
                meta = sg.metadata_json or {}
                for key in (
                    "cover", "cover_url", "header_url", "hero",
                    "background_url",
                ):
                    url = meta.get(key)
                    if url:
                        urls.append(url)
                screenshots = meta.get("screenshots", [])
                if isinstance(screenshots, list):
                    urls.extend(
                        s for s in screenshots
                        if isinstance(s, str) and s
                    )

            return urls

        except Exception as e:
            logger.error("get_store_image_urls(%s) failed: %s",
                         self._store_name, e)
            return []
