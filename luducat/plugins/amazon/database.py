# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""Amazon Games Plugin Database Models

SQLAlchemy models for the Amazon catalog database (catalog.db).
Stores game metadata from the entitlements API plus the sync state
(syncPoint, owned set, account id) that incremental sync needs to
return a complete owned list without a full refetch.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from luducat.plugins.sdk.datetime import utc_now
from luducat.plugins.sdk.json import json

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class AmazonGame(Base):
    """Amazon game metadata from the entitlements API.

    The entitlement's productDetail carries the full metadata set
    (developer, publisher, genres, ratings, images) — there is no
    separate catalog endpoint to enrich from.
    """
    __tablename__ = "amazon_games"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(255), unique=True, nullable=False, index=True)

    # Entitlement identifiers — the entitlement id is what the
    # download API (GetGameDownload) needs later.
    entitlement_id = Column(String(255), nullable=True)
    sku = Column(String(500), nullable=True)
    state = Column(String(50), nullable=True)  # LIVE etc.

    title = Column(String(500), nullable=False, index=True)
    description = Column(Text, nullable=True)
    short_description = Column(Text, nullable=True)
    release_date = Column(String(100), nullable=True)

    # JSON fields (stored as text, parsed on access)
    _developers = Column("developers", Text, default="[]")
    _publishers = Column("publishers", Text, default="[]")
    _genres = Column("genres", Text, default="[]")
    _game_modes = Column("game_modes", Text, default="[]")
    _screenshots = Column("screenshots", Text, default="[]")

    # Age ratings arrive as strings ("Everyone", "PEGI 3", "USK 16");
    # required_age is the normalized numeric maximum for the content
    # filter.
    esrb_rating = Column(String(50), nullable=True)
    pegi_rating = Column(String(50), nullable=True)
    usk_rating = Column(String(50), nullable=True)
    required_age = Column(Integer, nullable=True)

    # Image URLs from productDetail.details
    icon_url = Column(String(500), nullable=True)
    logo_url = Column(String(500), nullable=True)
    background_url = Column(String(500), nullable=True)   # backgroundUrl1
    background_url2 = Column(String(500), nullable=True)
    crown_url = Column(String(500), nullable=True)        # pgCrownImageUrl

    official_website = Column(String(500), nullable=True)

    last_updated = Column(DateTime, default=utc_now)

    # ── JSON property accessors (sentinel-cached) ─────────────────
    #
    # Each property caches the parsed result as a (raw, val) tuple in
    # __dict__. The `is` identity check on `raw` auto-invalidates when
    # SQLAlchemy expire() replaces the Column value with a new object.

    @property
    def developers(self) -> List[str]:
        cached = self.__dict__.get('_c_developers')
        raw = self._developers
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_developers'] = (raw, val)
        return val

    @developers.setter
    def developers(self, value: List[str]):
        self._developers = json.dumps(value)
        self.__dict__.pop('_c_developers', None)

    @property
    def publishers(self) -> List[str]:
        cached = self.__dict__.get('_c_publishers')
        raw = self._publishers
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_publishers'] = (raw, val)
        return val

    @publishers.setter
    def publishers(self, value: List[str]):
        self._publishers = json.dumps(value)
        self.__dict__.pop('_c_publishers', None)

    @property
    def genres(self) -> List[str]:
        cached = self.__dict__.get('_c_genres')
        raw = self._genres
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_genres'] = (raw, val)
        return val

    @genres.setter
    def genres(self, value: List[str]):
        self._genres = json.dumps(value)
        self.__dict__.pop('_c_genres', None)

    @property
    def game_modes(self) -> List[str]:
        cached = self.__dict__.get('_c_game_modes')
        raw = self._game_modes
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_game_modes'] = (raw, val)
        return val

    @game_modes.setter
    def game_modes(self, value: List[str]):
        self._game_modes = json.dumps(value)
        self.__dict__.pop('_c_game_modes', None)

    @property
    def screenshots(self) -> List[str]:
        cached = self.__dict__.get('_c_screenshots')
        raw = self._screenshots
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_screenshots'] = (raw, val)
        return val

    @screenshots.setter
    def screenshots(self, value: List[str]):
        self._screenshots = json.dumps(value)
        self.__dict__.pop('_c_screenshots', None)

    def to_dict(self, include_description: bool = True) -> Dict[str, Any]:
        """Convert to dictionary for metadata access.

        Args:
            include_description: False for bulk loads where descriptions
                are lazy-loaded on demand.
        """
        result = {
            "product_id": self.product_id,
            "entitlement_id": self.entitlement_id,
            "sku": self.sku,
            "state": self.state,
            "title": self.title,
            "short_description": self.short_description,
            "release_date": self.release_date,
            "developers": self.developers,
            "publishers": self.publishers,
            "genres": self.genres,
            "game_modes": self.game_modes,
            "screenshots": self.screenshots,
            "esrb_rating": self.esrb_rating,
            "pegi_rating": self.pegi_rating,
            "usk_rating": self.usk_rating,
            "required_age": self.required_age,
            "icon_url": self.icon_url,
            "logo_url": self.logo_url,
            "background_url": self.background_url,
            "background_url2": self.background_url2,
            "crown_url": self.crown_url,
            "official_website": self.official_website,
        }
        if include_description:
            result["description"] = self.description
        return result


class AmazonSyncState(Base):
    """Single-row sync state for incremental entitlement sync.

    The syncPoint is a CLIENT-side epoch timestamp — the server filters
    entitlements to changes since then and never issues sync tokens
    itself. Because incremental responses may not surface revocations,
    the complete owned set is tracked here (fetch_user_games must return
    a full list every sync for core stale-game reconciliation) together
    with the time of the last FULL fetch, which bounds ownership drift.
    """
    __tablename__ = "amazon_sync_state"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(255), nullable=True)
    sync_point = Column(Float, nullable=True)
    last_full_sync = Column(Float, nullable=True)
    _owned_ids = Column("owned_ids", Text, default="[]")
    updated = Column(DateTime, default=utc_now)


class AmazonDatabase:
    """Database access layer for the Amazon catalog.

    Usage:
        db = AmazonDatabase(data_dir / "catalog.db")
        db.initialize()
        game = db.get_game("<product uuid>")
        db.close()
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self._session_factory = sessionmaker(bind=self.engine)
        self._session: Optional[Session] = None

    def initialize(self) -> None:
        """Create tables if they don't exist."""
        Base.metadata.create_all(self.engine)

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None
        self.engine.dispose()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    # ── Games ────────────────────────────────────────────────────

    def get_game(self, product_id: str) -> Optional[AmazonGame]:
        return self.session.query(AmazonGame).filter(
            AmazonGame.product_id == product_id
        ).first()

    def get_all_games(self) -> List[AmazonGame]:
        return self.session.query(AmazonGame).all()

    def get_game_count(self) -> int:
        return self.session.query(AmazonGame).count()

    def get_all_product_ids(self) -> List[str]:
        return [
            row[0]
            for row in self.session.query(AmazonGame.product_id).all()
        ]

    # Never overwritten during upsert — managed by the caller
    _SKIP_ON_UPDATE = {"product_id"}

    def upsert_game(self, game: AmazonGame) -> None:
        """Insert or update a game keyed by product_id."""
        existing = self.get_game(game.product_id)
        if existing:
            for key, value in game.to_dict().items():
                if key not in self._SKIP_ON_UPDATE:
                    setattr(existing, key, value)
            existing.last_updated = utc_now()
        else:
            self.session.add(game)

    def get_games_metadata_bulk(
        self, product_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games, deferring descriptions."""
        from sqlalchemy.orm import defer

        games = (
            self.session.query(AmazonGame)
            .filter(AmazonGame.product_id.in_(product_ids))
            .options(defer(AmazonGame.description))
            .all()
        )
        return {
            game.product_id: game.to_dict(include_description=False)
            for game in games
        }

    # ── Sync state ───────────────────────────────────────────────

    def get_sync_state(self) -> Dict[str, Any]:
        """Return {sync_point, last_full_sync, owned_ids, user_id}."""
        row = self.session.get(AmazonSyncState, 1)
        if not row:
            return {
                "sync_point": None,
                "last_full_sync": None,
                "owned_ids": [],
                "user_id": None,
            }
        try:
            owned = json.loads(row._owned_ids) if row._owned_ids else []
        except (json.JSONDecodeError, ValueError):
            logger.warning("Corrupt owned_ids in sync state, resetting")
            owned = []
        return {
            "sync_point": row.sync_point,
            "last_full_sync": row.last_full_sync,
            "owned_ids": owned,
            "user_id": row.user_id,
        }

    def save_sync_state(
        self,
        sync_point: Optional[float],
        owned_ids: List[str],
        user_id: Optional[str],
        last_full_sync: Optional[float] = None,
    ) -> None:
        row = self.session.get(AmazonSyncState, 1)
        if not row:
            row = AmazonSyncState(id=1)
            self.session.add(row)
        row.sync_point = sync_point
        row._owned_ids = json.dumps(list(owned_ids))
        row.user_id = user_id
        if last_full_sync is not None:
            row.last_full_sync = last_full_sync
        row.updated = utc_now()
        self.session.commit()

    def clear_sync_state(self) -> None:
        row = self.session.get(AmazonSyncState, 1)
        if row:
            self.session.delete(row)
            self.session.commit()
