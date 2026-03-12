# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""Epic Games Store Plugin Database Models

SQLAlchemy models for the Epic catalog database (catalog.db).
This database stores game metadata fetched from Epic APIs.
"""

from luducat.plugins.sdk.json import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_now

import logging

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class EpicGame(Base):
    """Epic game metadata

    Stores game information fetched from Epic APIs.
    """
    __tablename__ = "epic_games"

    # Primary identifier - Epic uses string app_name as ID
    id = Column(Integer, primary_key=True, autoincrement=True)
    app_name = Column(String(255), unique=True, nullable=False, index=True)

    # Additional IDs from Epic
    catalog_id = Column(String(255), nullable=True)
    namespace = Column(String(255), nullable=True)

    # Basic info
    title = Column(String(500), nullable=False, index=True)
    app_type = Column(String(50), default="game")  # game, dlc, addon

    # Descriptions
    description = Column(Text, nullable=True)
    short_description = Column(Text, nullable=True)

    # Release info
    release_date = Column(String(100), nullable=True)

    # JSON fields (stored as text, parsed on access)
    _developers = Column("developers", Text, default="[]")
    _publishers = Column("publishers", Text, default="[]")
    _genres = Column("genres", Text, default="[]")
    _categories = Column("categories", Text, default="[]")
    _screenshots = Column("screenshots", Text, default="[]")

    # Image URLs
    cover_url = Column(String(500), nullable=True)  # Vertical cover (key art)
    header_url = Column(String(500), nullable=True)  # Wide header image
    logo_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)
    background_url = Column(String(500), nullable=True)  # Background/hero image

    # Extended metadata (from catalog API customAttributes)
    third_party_store = Column(String(100), nullable=True)  # "Origin", "EA App", etc.
    cloud_save_folder = Column(String(500), nullable=True)  # Save path template
    additional_cli_args = Column(String(500), nullable=True)  # Extra launch args

    # Platform support
    windows = Column(Boolean, default=True)
    mac = Column(Boolean, default=False)

    # Status
    is_installed = Column(Boolean, default=False)
    install_path = Column(String(1000), nullable=True)
    last_updated = Column(DateTime, default=utc_now)

    # Indexes
    __table_args__ = (
        Index("ix_epic_games_app_type", "app_type"),
        Index("ix_epic_games_catalog_id", "catalog_id"),
    )

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
    def categories(self) -> List[str]:
        cached = self.__dict__.get('_c_categories')
        raw = self._categories
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_categories'] = (raw, val)
        return val

    @categories.setter
    def categories(self, value: List[str]):
        self._categories = json.dumps(value)
        self.__dict__.pop('_c_categories', None)

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

    @property
    def is_metadata_complete(self) -> bool:
        """Check if game has complete metadata (no enrichment needed).

        A game is considered complete when it has:
        - title != description (not a stub)
        - description (non-empty, >50 chars for meaningful content)
        - short_description (exists and differs from description)
        - release_date (non-empty)
        - developers (at least 1)
        - publishers (at least 1)
        - genres (at least 1)
        - screenshots (at least 1 URL)
        - cover_url (for grid view display)

        Returns:
            True if metadata is complete, False if enrichment is needed.
        """
        # if title and description are the same, that is incomplete.
        if self.title == self.description:
            return False

        # Must have a meaningful description
        if not self.description or len(self.description) < 50:
            return False

        # check for meaningful short_description, that has to not identical to description
        if not self.short_description:
            return False

        if self.description == self.short_description:
            return False

        if len(self.short_description) >= len(self.description):
            return False

        # must have a release date
        # known issue: epic does willingly hide that behind bot protection
        #              login etc. not worth the effort, if I'm anyway
        #              interested in the canonical release date and not
        #              interested in epic playing tactical games with that.
        #if not self.release_date:
        #    return False

        # developers and publishers
        if not self.developers or len(self.developers) < 1:
            return False
        if not self.publishers or len(self.publishers) < 1:
            return False

        # Must have at least one genre (resolved via GraphQL tag lookup)
        if not self.genres or len(self.genres) < 1:
            return False

        # Must have at least one screenshot for carousel
        if not self.screenshots or len(self.screenshots) < 1:
            return False

        # Must have cover image for grid view
        if not self.cover_url:
            return False

        return True

    def to_dict(self, include_description: bool = True) -> Dict[str, Any]:
        """Convert to dictionary for metadata access

        Args:
            include_description: If True, include description text.
                False for bulk loads where descriptions are lazy-loaded on demand.
        """
        result = {
            "app_name": self.app_name,
            "catalog_id": self.catalog_id,
            "namespace": self.namespace,
            "title": self.title,
            "app_type": self.app_type,
            "short_description": self.short_description,
            "release_date": self.release_date,
            "developers": self.developers,
            "publishers": self.publishers,
            "genres": self.genres,
            "categories": self.categories,
            "cover_url": self.cover_url,
            "header_url": self.header_url,
            "logo_url": self.logo_url,
            "thumbnail_url": self.thumbnail_url,
            "background_url": self.background_url,
            "screenshots": self.screenshots,
            "windows": self.windows,
            "mac": self.mac,
            "is_installed": self.is_installed,
            "install_path": self.install_path,
        }

        if include_description:
            result["description"] = self.description

        return result


class EpicImage(Base):
    """Cached image information for Epic games"""
    __tablename__ = "epic_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_name = Column(String(255), ForeignKey("epic_games.app_name"), index=True)
    url = Column(String(500), nullable=False)
    image_type = Column(String(50), nullable=False)  # cover, header, screenshot
    image_order = Column(Integer, default=0)
    cached_path = Column(String(500), nullable=True)  # Local cache path if downloaded

    # Relationship
    game = relationship("EpicGame", backref="images")


class EpicDatabase:
    """Database access layer for Epic catalog

    Usage:
        db = EpicDatabase(data_dir / "catalog.db")
        db.initialize()

        # Get game
        game = db.get_game("Fortnite")

        # Add/update game
        db.upsert_game(epic_game_obj)

        db.close()
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self._session_factory = sessionmaker(bind=self.engine)
        self._session: Optional[Session] = None

    # Columns added after initial schema — need ALTER TABLE for existing DBs
    _MIGRATION_COLUMNS = {
        "epic_games": [
            ("third_party_store", "VARCHAR(100)"),
            ("cloud_save_folder", "VARCHAR(500)"),
            ("additional_cli_args", "VARCHAR(500)"),
        ],
    }

    def initialize(self) -> None:
        """Create tables if they don't exist, add missing columns."""
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Add columns that don't exist yet (non-destructive migration)."""
        insp = inspect(self.engine)
        for table_name, columns in self._MIGRATION_COLUMNS.items():
            if not insp.has_table(table_name):
                continue
            existing = {col["name"] for col in insp.get_columns(table_name)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    with self.engine.begin() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE {table_name} "
                                f"ADD COLUMN {col_name} {col_type}"
                            )
                        )
                    logger.debug(
                        "Added column %s.%s", table_name, col_name
                    )

    @property
    def session(self) -> Session:
        """Get or create session"""
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def close(self) -> None:
        """Close database connection"""
        if self._session:
            self._session.close()
            self._session = None
        self.engine.dispose()

    def get_game(self, app_name: str) -> Optional[EpicGame]:
        """Get game by app_name"""
        return self.session.query(EpicGame).filter(
            EpicGame.app_name == app_name
        ).first()

    def get_all_games(self, include_dlc: bool = False) -> List[EpicGame]:
        """Get all games from catalog"""
        query = self.session.query(EpicGame)
        if not include_dlc:
            query = query.filter(EpicGame.app_type == "game")
        return query.all()

    def get_game_count(self, include_dlc: bool = False) -> int:
        """Get total game count"""
        query = self.session.query(EpicGame)
        if not include_dlc:
            query = query.filter(EpicGame.app_type == "game")
        return query.count()

    def get_all_app_names(self, include_dlc: bool = False) -> List[str]:
        """Get all app names (for sync checking)"""
        query = self.session.query(EpicGame.app_name)
        if not include_dlc:
            query = query.filter(EpicGame.app_type == "game")
        return [row[0] for row in query.all()]

    def game_exists(self, app_name: str) -> bool:
        """Check if game exists in database"""
        return self.session.query(EpicGame.app_name).filter(
            EpicGame.app_name == app_name
        ).first() is not None

    # Fields managed by on_sync_complete() / update_install_status() — never
    # overwrite during metadata upsert (would reset is_installed to NULL).
    _SKIP_ON_UPDATE = {"app_name", "is_installed", "install_path"}

    def upsert_game(self, game: EpicGame) -> None:
        """Insert or update game"""
        existing = self.get_game(game.app_name)
        if existing:
            # Update fields, preserving status fields managed elsewhere
            for key, value in game.to_dict().items():
                if key not in self._SKIP_ON_UPDATE:
                    setattr(existing, key, value)
            existing.last_updated = utc_now()
        else:
            self.session.add(game)

    def commit(self) -> None:
        """Commit pending changes"""
        self.session.commit()

    def rollback(self) -> None:
        """Rollback pending changes"""
        self.session.rollback()

    def bulk_insert_games(self, games: List[EpicGame]) -> int:
        """Bulk insert games (skip existing)

        Returns:
            Number of games inserted
        """
        # Get existing IDs
        existing_ids = set(self.get_all_app_names(include_dlc=True))

        # Filter new games
        new_games = [g for g in games if g.app_name not in existing_ids]

        if new_games:
            self.session.bulk_save_objects(new_games)
            self.session.commit()

        return len(new_games)

    def get_games_metadata_bulk(
        self, app_names: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Get metadata for multiple games efficiently

        Defers description column not needed for the startup cache.

        Args:
            app_names: List of app names to fetch

        Returns:
            Dict mapping app_name -> metadata dict
        """
        from sqlalchemy.orm import defer

        games = (
            self.session.query(EpicGame)
            .filter(EpicGame.app_name.in_(app_names))
            .options(defer(EpicGame.description))
            .all()
        )

        return {game.app_name: game.to_dict(include_description=False) for game in games}

    def update_install_status(
        self, app_name: str, is_installed: bool, install_path: Optional[str] = None
    ) -> None:
        """Update game installation status"""
        game = self.get_game(app_name)
        if game:
            game.is_installed = is_installed
            game.install_path = install_path
            game.last_updated = utc_now()
            self.commit()
