# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""IGDB Plugin Database Models

SQLAlchemy models for the IGDB metadata database (igdb.db).
Comprehensive normalized schema that mirrors IGDB's data structure.

Tables:
- Main: igdb_games (core game data)
- Lookup: genres, themes, keywords, franchises, collections, game_modes, platforms, companies
- Association: game_* (many-to-many relationships)
- Per-game: screenshots, artworks, videos, websites, external_ids, involved_companies, release_dates
- Mapping: store_matches (store ID to IGDB ID mapping)
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker, selectinload

from luducat.plugins.sdk.datetime import utc_now

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# =============================================================================
# Association Tables (Many-to-Many)
# =============================================================================

game_genres = Table(
    "game_genres",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("genre_id", Integer, ForeignKey("igdb_genres.id"), primary_key=True),
)

game_themes = Table(
    "game_themes",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("theme_id", Integer, ForeignKey("igdb_themes.id"), primary_key=True),
)

game_keywords = Table(
    "game_keywords",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("keyword_id", Integer, ForeignKey("igdb_keywords.id"), primary_key=True),
)

game_franchises = Table(
    "game_franchises",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("franchise_id", Integer, ForeignKey("igdb_franchises.id"), primary_key=True),
)

game_collections = Table(
    "game_collections",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("collection_id", Integer, ForeignKey("igdb_collections.id"), primary_key=True),
)

game_game_modes = Table(
    "game_game_modes",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("game_mode_id", Integer, ForeignKey("igdb_game_modes.id"), primary_key=True),
)

game_platforms = Table(
    "game_platforms",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("platform_id", Integer, ForeignKey("igdb_platforms.id"), primary_key=True),
)

game_player_perspectives = Table(
    "game_player_perspectives",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("igdb_games.igdb_id"), primary_key=True),
    Column("perspective_id", Integer, ForeignKey("igdb_player_perspectives.id"), primary_key=True),
)


# =============================================================================
# Lookup Tables (Reused across games)
# =============================================================================

class IgdbGenre(Base):
    """IGDB genre lookup table"""
    __tablename__ = "igdb_genres"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


class IgdbTheme(Base):
    """IGDB theme lookup table (used as tags in luducat)"""
    __tablename__ = "igdb_themes"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


class IgdbKeyword(Base):
    """IGDB keyword lookup table"""
    __tablename__ = "igdb_keywords"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


class IgdbFranchise(Base):
    """IGDB franchise lookup table"""
    __tablename__ = "igdb_franchises"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


class IgdbCollection(Base):
    """IGDB collection lookup table (game series)"""
    __tablename__ = "igdb_collections"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    collection_type = Column(Integer)  # IGDB collection type
    checksum = Column(String(100))


class IgdbGameMode(Base):
    """IGDB game mode lookup table"""
    __tablename__ = "igdb_game_modes"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


class IgdbPlatform(Base):
    """IGDB platform lookup table"""
    __tablename__ = "igdb_platforms"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    abbreviation = Column(String(50))
    alternative_name = Column(String(255))
    url = Column(String(500))
    platform_type = Column(Integer)
    checksum = Column(String(100))


class IgdbCompany(Base):
    """IGDB company lookup table"""
    __tablename__ = "igdb_companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    description = Column(Text)
    country = Column(Integer)
    checksum = Column(String(100))


class IgdbPlayerPerspective(Base):
    """IGDB player perspective lookup table"""
    __tablename__ = "igdb_player_perspectives"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))
    checksum = Column(String(100))


# =============================================================================
# Per-Game Data Tables
# =============================================================================

class IgdbScreenshot(Base):
    """IGDB screenshot per-game"""
    __tablename__ = "igdb_screenshots"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    image_id = Column(String(100), nullable=False)
    url = Column(String(500))  # Full URL to 1080p version
    width = Column(Integer)
    height = Column(Integer)
    alpha_channel = Column(Boolean, default=False)
    animated = Column(Boolean, default=False)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="screenshots")

    __table_args__ = (
        Index("ix_igdb_screenshots_game_id", "game_id"),
    )


class IgdbArtwork(Base):
    """IGDB artwork per-game"""
    __tablename__ = "igdb_artworks"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    image_id = Column(String(100), nullable=False)
    url = Column(String(500))  # Full URL to 1080p version
    width = Column(Integer)
    height = Column(Integer)
    artwork_type = Column(Integer)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="artworks")

    __table_args__ = (
        Index("ix_igdb_artworks_game_id", "game_id"),
    )


class IgdbVideo(Base):
    """IGDB video per-game (YouTube videos)"""
    __tablename__ = "igdb_videos"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    video_id = Column(String(50), nullable=False)  # YouTube video ID
    name = Column(String(500))
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="videos")

    __table_args__ = (
        Index("ix_igdb_videos_game_id", "game_id"),
    )


class IgdbWebsite(Base):
    """IGDB website per-game

    Website types:
    1=Official, 2=Wikia, 3=Wikipedia, 4=Facebook, 5=Twitter,
    6=Twitch, 8=Instagram, 9=YouTube, 10=iPhone, 11=iPad,
    12=Android, 13=Steam, 14=Reddit, 15=Itch, 16=Epic, 17=GOG,
    18=Discord
    """
    __tablename__ = "igdb_websites"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    url = Column(String(1000), nullable=False)
    website_type = Column(Integer)
    trusted = Column(Boolean, default=False)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="websites")

    __table_args__ = (
        Index("ix_igdb_websites_game_id", "game_id"),
        Index("ix_igdb_websites_type", "website_type"),
    )


class IgdbExternalId(Base):
    """IGDB external game IDs (store links)

    External game sources:
    1=Steam, 5=GOG, 11=Xbox, 14=Twitch, 20=Epic,
    26=Epic Games Store, 36=PlayStation
    """
    __tablename__ = "igdb_external_ids"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    source_id = Column(Integer, nullable=False)  # external_game_source
    uid = Column(String(255))  # Store-specific ID
    name = Column(String(500))
    url = Column(String(1000))
    year = Column(Integer)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="external_ids")

    __table_args__ = (
        Index("ix_igdb_external_ids_game_id", "game_id"),
        Index("ix_igdb_external_ids_source", "source_id", "uid"),
    )


class IgdbInvolvedCompany(Base):
    """IGDB involved company per-game (developer/publisher relationship)"""
    __tablename__ = "igdb_involved_companies"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    company_id = Column(Integer, ForeignKey("igdb_companies.id"), nullable=False)
    developer = Column(Boolean, default=False)
    publisher = Column(Boolean, default=False)
    porting = Column(Boolean, default=False)
    supporting = Column(Boolean, default=False)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="involved_companies")
    company = relationship("IgdbCompany", lazy="selectin")  # Eager load to avoid detached session issues

    __table_args__ = (
        Index("ix_igdb_involved_companies_game_id", "game_id"),
        Index("ix_igdb_involved_companies_company_id", "company_id"),
    )


class IgdbReleaseDate(Base):
    """IGDB release date per-game per-platform"""
    __tablename__ = "igdb_release_dates"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    platform_id = Column(Integer, ForeignKey("igdb_platforms.id"))
    date = Column(Integer)  # Unix timestamp
    human = Column(String(100))  # Human-readable date string
    region = Column(Integer)  # Region code
    category = Column(Integer)  # Date precision (YYYY, YYYYMM, etc.)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="release_dates")
    platform = relationship("IgdbPlatform", lazy="selectin")  # Eager load to avoid detached session issues

    __table_args__ = (
        Index("ix_igdb_release_dates_game_id", "game_id"),
        Index("ix_igdb_release_dates_platform_id", "platform_id"),
    )


class IgdbAgeRating(Base):
    """IGDB age rating per-game"""
    __tablename__ = "igdb_age_ratings"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=False)
    category = Column(Integer)  # Rating board (ESRB, PEGI, etc.)
    rating = Column(Integer)  # Rating value
    synopsis = Column(Text)
    checksum = Column(String(100))

    game = relationship("IgdbGame", back_populates="age_ratings")

    __table_args__ = (
        Index("ix_igdb_age_ratings_game_id", "game_id"),
    )


# =============================================================================
# Main Game Table
# =============================================================================

class IgdbGame(Base):
    """IGDB game main table

    Stores core game data with relationships to lookup and per-game tables.
    """
    __tablename__ = "igdb_games"

    # Primary identifier
    igdb_id = Column(Integer, primary_key=True)

    # Basic info
    name = Column(String(500), nullable=False)
    slug = Column(String(255))
    url = Column(String(500))  # IGDB page URL
    normalized_title = Column(String(500))  # For matching

    # Descriptions
    summary = Column(Text)
    storyline = Column(Text)

    # Release info
    # Computed: oldest release date from DOS (13) or PC Windows (6)
    first_release_date = Column(Integer)  # Unix timestamp
    release_year = Column(Integer)  # Extracted year

    # Cover image - FULL URL
    cover_id = Column(Integer)  # IGDB cover ID
    cover_image_id = Column(String(100))  # Image ID for URL building
    cover_url = Column(String(500))  # Full URL to cover_big_2x

    # Background image - first artwork if available
    background_url = Column(String(500))  # Full URL to 1080p artwork

    # Ratings
    rating = Column(Float)  # User rating (0-100)
    rating_count = Column(Integer)
    aggregated_rating = Column(Float)  # Critic rating
    aggregated_rating_count = Column(Integer)
    total_rating = Column(Float)  # Combined rating
    total_rating_count = Column(Integer)

    # Game category
    # 0=Main game, 1=DLC, 2=Expansion, 3=Bundle, 4=Standalone expansion,
    # 5=Mod, 6=Episode, 7=Season, 8=Remake, 9=Remaster, 10=Expanded game,
    # 11=Port, 12=Fork, 13=Pack, 14=Update
    category = Column(Integer)

    # Game status
    # 0=Released, 2=Alpha, 3=Beta, 4=Early access, 5=Offline, 6=Cancelled, 7=Rumored, 8=Delisted
    status = Column(Integer)

    # Metadata
    checksum = Column(String(100))
    created_at = Column(DateTime, default=utc_now)
    last_updated = Column(DateTime, default=utc_now)

    # Relationships - Many-to-Many (lookup tables)
    genres = relationship("IgdbGenre", secondary=game_genres, lazy="selectin")
    themes = relationship("IgdbTheme", secondary=game_themes, lazy="selectin")
    keywords = relationship("IgdbKeyword", secondary=game_keywords, lazy="selectin")
    franchises = relationship("IgdbFranchise", secondary=game_franchises, lazy="selectin")
    collections = relationship("IgdbCollection", secondary=game_collections, lazy="selectin")
    game_modes = relationship("IgdbGameMode", secondary=game_game_modes, lazy="selectin")
    platforms = relationship("IgdbPlatform", secondary=game_platforms, lazy="selectin")
    player_perspectives = relationship("IgdbPlayerPerspective", secondary=game_player_perspectives, lazy="selectin")

    # Relationships - One-to-Many (per-game data)
    screenshots = relationship("IgdbScreenshot", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    artworks = relationship("IgdbArtwork", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    videos = relationship("IgdbVideo", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    websites = relationship("IgdbWebsite", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    external_ids = relationship("IgdbExternalId", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    involved_companies = relationship("IgdbInvolvedCompany", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    release_dates = relationship("IgdbReleaseDate", back_populates="game", cascade="all, delete-orphan", lazy="selectin")
    age_ratings = relationship("IgdbAgeRating", back_populates="game", cascade="all, delete-orphan", lazy="selectin")

    # Indexes
    __table_args__ = (
        Index("ix_igdb_games_name", "name"),
        Index("ix_igdb_games_slug", "slug"),
        Index("ix_igdb_games_normalized_title", "normalized_title"),
    )

    @property
    def developers(self) -> List[str]:
        """Get list of developer company names"""
        return [ic.company.name for ic in self.involved_companies if ic.developer and ic.company]

    @property
    def publishers(self) -> List[str]:
        """Get list of publisher company names"""
        return [ic.company.name for ic in self.involved_companies if ic.publisher and ic.company]

    @property
    def genre_names(self) -> List[str]:
        """Get list of genre names"""
        return [g.name for g in self.genres]

    @property
    def theme_names(self) -> List[str]:
        """Get list of theme names (used as tags)"""
        return [t.name for t in self.themes]

    @property
    def keyword_names(self) -> List[str]:
        """Get list of keyword names"""
        return [k.name for k in self.keywords]

    @property
    def franchise_names(self) -> List[str]:
        """Get list of franchise names"""
        return [f.name for f in self.franchises]

    @property
    def collection_names(self) -> List[str]:
        """Get list of collection/series names"""
        return [c.name for c in self.collections]

    @property
    def game_mode_names(self) -> List[str]:
        """Get list of game mode names"""
        return [m.name for m in self.game_modes]

    @property
    def screenshot_urls(self) -> List[str]:
        """Get list of screenshot URLs"""
        return [s.url for s in self.screenshots if s.url]

    @property
    def artwork_urls(self) -> List[str]:
        """Get list of artwork URLs"""
        return [a.url for a in self.artworks if a.url]

    @property
    def platform_names(self) -> List[str]:
        """Get list of platform names"""
        return [p.name for p in self.platforms if p.name]

    @property
    def player_perspective_names(self) -> List[str]:
        """Get list of player perspective names"""
        return [pp.name for pp in self.player_perspectives if pp.name]

    @property
    def video_youtube_ids(self) -> List[str]:
        """Get list of YouTube video IDs"""
        return [v.video_id for v in self.videos if v.video_id]

    def get_store_id(self, store_name: str) -> Optional[str]:
        """Get store-specific ID from external_ids

        Args:
            store_name: steam, gog, epic

        Returns:
            Store app ID or None
        """
        # Map store names to IGDB external_game_source IDs
        source_map = {
            "steam": 1,
            "gog": 5,
            "epic": 26,
        }
        source_id = source_map.get(store_name.lower())
        if not source_id:
            return None

        for ext in self.external_ids:
            if ext.source_id == source_id:
                return ext.uid
        return None


# =============================================================================
# Store Match Table
# =============================================================================

class IgdbStoreMatch(Base):
    """Maps store game IDs to IGDB game IDs

    Caches the result of external_games lookups and title searches
    to avoid repeated API calls.
    """
    __tablename__ = "igdb_store_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Store identification
    store_name = Column(String(50), nullable=False)  # steam, gog, epic
    store_app_id = Column(String(100), nullable=False)

    # Normalized title for retry matching (stored on first lookup attempt)
    normalized_title = Column(String(500), nullable=True)

    # IGDB match (null if no match found)
    igdb_id = Column(Integer, ForeignKey("igdb_games.igdb_id"), nullable=True)

    # Match metadata
    match_method = Column(String(50))  # external_games, title_search, manual
    match_confidence = Column(Float, default=1.0)  # 0.0-1.0
    user_confirmed = Column(Boolean, default=False)

    # Tracking
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship
    game = relationship("IgdbGame", lazy="selectin")  # Eager load to avoid detached session issues

    __table_args__ = (
        UniqueConstraint("store_name", "store_app_id", name="uq_store_match"),
        Index("ix_igdb_store_matches_store", "store_name", "store_app_id"),
        Index("ix_igdb_store_matches_igdb_id", "igdb_id"),
    )


# =============================================================================
# Database Manager
# =============================================================================

class IgdbDatabase:
    """Database manager for IGDB plugin

    Handles database creation, sessions, and common queries.
    """

    # IGDB external_game_source IDs
    SOURCE_STEAM = 1
    SOURCE_GOG = 5
    SOURCE_TWITCH = 14
    SOURCE_EPIC = 26

    # IGDB platform IDs for PC release date selection
    PLATFORM_PC_WINDOWS = 6
    PLATFORM_DOS = 13
    # Note: PC-9800 Series (149) is NOT included

    def __init__(self, db_path: Path):
        """Initialize database

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            # Improve write performance
            connect_args={"check_same_thread": False},
        )

        # Create all tables
        Base.metadata.create_all(self.engine)

        # Run schema migrations for existing databases
        self._run_migrations()

        self.Session = sessionmaker(bind=self.engine)

    def _run_migrations(self) -> None:
        """Run schema migrations for existing databases"""
        from sqlalchemy import text, inspect

        inspector = inspect(self.engine)

        # Migration: Add normalized_title to igdb_store_matches
        if "igdb_store_matches" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("igdb_store_matches")]
            if "normalized_title" not in columns:
                with self.engine.connect() as conn:
                    conn.execute(text(
                        "ALTER TABLE igdb_store_matches ADD COLUMN normalized_title VARCHAR(500)"
                    ))
                    conn.commit()
                logger.info("IGDB database migration: added normalized_title column")

    def get_session(self) -> Session:
        """Get a new database session"""
        return self.Session()

    # -------------------------------------------------------------------------
    # Game CRUD
    # -------------------------------------------------------------------------

    def get_game(self, igdb_id: int) -> Optional[IgdbGame]:
        """Get game by IGDB ID with all relationships loaded"""
        with self.get_session() as session:
            game = session.query(IgdbGame).filter_by(igdb_id=igdb_id).first()
            if game:
                # Force load all relationships while in session
                _ = game.genres, game.themes, game.keywords
                _ = game.franchises, game.collections, game.game_modes
                _ = game.platforms, game.player_perspectives
                _ = game.screenshots, game.artworks, game.videos
                _ = game.websites, game.external_ids
                _ = game.involved_companies, game.release_dates
                _ = game.age_ratings
                # Detach and return
                session.expunge(game)
            return game

    def get_games_by_ids(self, igdb_ids: List[int]) -> List[IgdbGame]:
        """Get multiple games by IGDB IDs"""
        with self.get_session() as session:
            games = session.query(IgdbGame).filter(
                IgdbGame.igdb_id.in_(igdb_ids)
            ).all()
            # Force load relationships
            for game in games:
                _ = game.genres, game.themes, game.involved_companies
                session.expunge(game)
            return games

    def game_exists(self, igdb_id: int) -> bool:
        """Check if game exists in database"""
        with self.get_session() as session:
            return session.query(IgdbGame.igdb_id).filter_by(igdb_id=igdb_id).first() is not None

    def save_game(self, game: IgdbGame, session: Optional[Session] = None) -> None:
        """Save or update an IGDB game

        Args:
            game: IgdbGame instance to save
            session: Optional existing session (for batch operations)
        """
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            existing = session.query(IgdbGame).filter_by(igdb_id=game.igdb_id).first()
            if existing:
                # Update existing - copy all attributes
                for col in IgdbGame.__table__.columns:
                    if col.name != "igdb_id":
                        setattr(existing, col.name, getattr(game, col.name))
                existing.last_updated = utc_now()
                # Merge the game to update relationships
                session.merge(game)
            else:
                session.add(game)

            if own_session:
                session.commit()
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def delete_game(self, igdb_id: int) -> bool:
        """Delete a game and all related data"""
        with self.get_session() as session:
            game = session.query(IgdbGame).filter_by(igdb_id=igdb_id).first()
            if game:
                session.delete(game)
                session.commit()
                return True
            return False

    # -------------------------------------------------------------------------
    # Store Match CRUD
    # -------------------------------------------------------------------------

    def get_store_match(self, store_name: str, store_app_id: str) -> Optional[IgdbStoreMatch]:
        """Get cached store match"""
        with self.get_session() as session:
            match = session.query(IgdbStoreMatch).filter_by(
                store_name=store_name,
                store_app_id=str(store_app_id)
            ).first()
            if match:
                session.expunge(match)
            return match

    def save_store_match(
        self,
        store_name: str,
        store_app_id: str,
        igdb_id: Optional[int],
        match_method: str = "external_games",
        confidence: float = 1.0,
        normalized_title: Optional[str] = None,
        session: Optional[Session] = None
    ) -> IgdbStoreMatch:
        """Save or update a store match

        Args:
            store_name: Store identifier (steam, gog, epic)
            store_app_id: Store's app ID
            igdb_id: IGDB game ID (None if no match found)
            match_method: How the match was found
            confidence: Match confidence (0.0-1.0)
            normalized_title: Optional normalized title for retry matching
            session: Optional existing session

        Returns:
            The saved IgdbStoreMatch
        """
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            existing = session.query(IgdbStoreMatch).filter_by(
                store_name=store_name,
                store_app_id=str(store_app_id)
            ).first()

            if existing:
                existing.igdb_id = igdb_id
                existing.match_method = match_method
                existing.match_confidence = confidence
                existing.updated_at = utc_now()
                # Only update title if provided and not already set
                if normalized_title and not existing.normalized_title:
                    existing.normalized_title = normalized_title
                match = existing
            else:
                match = IgdbStoreMatch(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                    igdb_id=igdb_id,
                    match_method=match_method,
                    match_confidence=confidence,
                    normalized_title=normalized_title,
                )
                session.add(match)

            if own_session:
                session.commit()
                session.expunge(match)

            return match
        except Exception:
            if own_session:
                session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def get_matches_for_store(self, store_name: str) -> List[IgdbStoreMatch]:
        """Get all matches for a store"""
        with self.get_session() as session:
            matches = session.query(IgdbStoreMatch).filter_by(
                store_name=store_name
            ).all()
            for m in matches:
                session.expunge(m)
            return matches

    # -------------------------------------------------------------------------
    # Lookup Table CRUD
    # -------------------------------------------------------------------------

    def get_or_create_genre(self, data: Dict[str, Any], session: Session) -> IgdbGenre:
        """Get or create a genre from API data"""
        genre = session.query(IgdbGenre).filter_by(id=data["id"]).first()
        if not genre:
            genre = IgdbGenre(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(genre)
        return genre

    def get_or_create_theme(self, data: Dict[str, Any], session: Session) -> IgdbTheme:
        """Get or create a theme from API data"""
        theme = session.query(IgdbTheme).filter_by(id=data["id"]).first()
        if not theme:
            theme = IgdbTheme(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(theme)
        return theme

    def get_or_create_keyword(self, data: Dict[str, Any], session: Session) -> IgdbKeyword:
        """Get or create a keyword from API data"""
        keyword = session.query(IgdbKeyword).filter_by(id=data["id"]).first()
        if not keyword:
            keyword = IgdbKeyword(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(keyword)
        return keyword

    def get_or_create_franchise(self, data: Dict[str, Any], session: Session) -> IgdbFranchise:
        """Get or create a franchise from API data"""
        franchise = session.query(IgdbFranchise).filter_by(id=data["id"]).first()
        if not franchise:
            franchise = IgdbFranchise(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(franchise)
        return franchise

    def get_or_create_collection(self, data: Dict[str, Any], session: Session) -> IgdbCollection:
        """Get or create a collection from API data"""
        collection = session.query(IgdbCollection).filter_by(id=data["id"]).first()
        if not collection:
            collection = IgdbCollection(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                collection_type=data.get("type"),
                checksum=data.get("checksum"),
            )
            session.add(collection)
        return collection

    def get_or_create_game_mode(self, data: Dict[str, Any], session: Session) -> IgdbGameMode:
        """Get or create a game mode from API data"""
        game_mode = session.query(IgdbGameMode).filter_by(id=data["id"]).first()
        if not game_mode:
            game_mode = IgdbGameMode(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(game_mode)
        return game_mode

    def get_or_create_platform(self, data: Dict[str, Any], session: Session) -> IgdbPlatform:
        """Get or create a platform from API data"""
        platform = session.query(IgdbPlatform).filter_by(id=data["id"]).first()
        if not platform:
            platform = IgdbPlatform(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                abbreviation=data.get("abbreviation"),
                alternative_name=data.get("alternative_name"),
                url=data.get("url"),
                platform_type=data.get("platform_type"),
                checksum=data.get("checksum"),
            )
            session.add(platform)
        return platform

    def get_or_create_company(self, data: Dict[str, Any], session: Session) -> IgdbCompany:
        """Get or create a company from API data"""
        company = session.query(IgdbCompany).filter_by(id=data["id"]).first()
        if not company:
            company = IgdbCompany(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                description=data.get("description"),
                country=data.get("country"),
                checksum=data.get("checksum"),
            )
            session.add(company)
        return company

    def get_or_create_player_perspective(self, data: Dict[str, Any], session: Session) -> IgdbPlayerPerspective:
        """Get or create a player perspective from API data"""
        perspective = session.query(IgdbPlayerPerspective).filter_by(id=data["id"]).first()
        if not perspective:
            perspective = IgdbPlayerPerspective(
                id=data["id"],
                name=data.get("name", ""),
                slug=data.get("slug"),
                url=data.get("url"),
                checksum=data.get("checksum"),
            )
            session.add(perspective)
        return perspective

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def get_steam_ids_for_store(
        self, store_name: str, app_ids: List[str]
    ) -> Dict[str, tuple]:
        """Resolve Steam AppIDs for non-Steam store games via IGDB external_ids.

        Joins igdb_store_matches → igdb_external_ids (source_id=1 for Steam)
        → igdb_games to find the Steam AppID for a GOG/Epic game.
        Pure local DB query.

        Args:
            store_name: Source store ("gog" or "epic")
            app_ids: List of store app IDs to resolve

        Returns:
            Dict mapping store_app_id -> (steam_app_id, game_name)
            Only includes entries where a Steam external ID was found.
        """
        result: Dict[str, tuple] = {}
        app_id_set = set(str(aid) for aid in app_ids)

        try:
            from sqlalchemy import text as sa_text

            with self.get_session() as session:
                sql = sa_text("""
                    SELECT sm.store_app_id, steam_ext.uid, g.name
                    FROM igdb_store_matches sm
                    JOIN igdb_external_ids steam_ext
                      ON sm.igdb_id = steam_ext.game_id
                    JOIN igdb_games g
                      ON sm.igdb_id = g.igdb_id
                    WHERE sm.store_name = :store_name
                      AND sm.igdb_id IS NOT NULL
                      AND steam_ext.source_id = 1
                """)

                rows = session.execute(
                    sql, {"store_name": store_name}
                ).fetchall()

                for row in rows:
                    app_id = row[0]
                    if app_id not in app_id_set:
                        continue

                    steam_id = (row[1] or "").strip()
                    if steam_id:
                        result[app_id] = (steam_id, row[2] or "")

        except Exception as e:
            logger.warning(f"Failed to resolve Steam IDs for {store_name}: {e}")

        return result

    def get_game_count(self) -> int:
        """Get total number of cached games"""
        with self.get_session() as session:
            return session.query(IgdbGame).count()

    def get_match_count(self, store_name: Optional[str] = None, matched_only: bool = False) -> int:
        """Get count of store matches

        Args:
            store_name: Filter by store (None for all)
            matched_only: Only count matches with igdb_id set
        """
        with self.get_session() as session:
            query = session.query(IgdbStoreMatch)
            if store_name:
                query = query.filter_by(store_name=store_name)
            if matched_only:
                query = query.filter(IgdbStoreMatch.igdb_id.isnot(None))
            return query.count()

    def search_games_by_name(self, name: str, limit: int = 10) -> List[IgdbGame]:
        """Search games by name (case-insensitive)"""
        with self.get_session() as session:
            games = session.query(IgdbGame).filter(
                IgdbGame.name.ilike(f"%{name}%")
            ).limit(limit).all()
            for game in games:
                session.expunge(game)
            return games

    def vacuum(self) -> None:
        """Optimize database (run periodically)"""
        with self.engine.connect() as conn:
            conn.execute("VACUUM")

    def close(self) -> None:
        """Close database connection"""
        self.engine.dispose()


# =============================================================================
# Helper Functions
# =============================================================================

def fix_url_protocol(url: Optional[str]) -> Optional[str]:
    """Ensure URL has https:// protocol

    IGDB sometimes returns protocol-relative URLs like:
    //images.igdb.com/igdb/image/upload/t_thumb/co74hl.jpg

    Args:
        url: URL that may be missing protocol

    Returns:
        URL with https:// protocol or None
    """
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return "https://" + url
    return url


def build_cover_url(image_id: str, size: str = "cover_big_2x") -> str:
    """Build full cover URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (cover_small, cover_big, cover_big_2x)

    Returns:
        Full URL to cover image
    """
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.png"


def build_screenshot_url(image_id: str, size: str = "1080p") -> str:
    """Build full screenshot URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (screenshot_med, screenshot_big, screenshot_huge, 720p, 1080p)

    Returns:
        Full URL to screenshot image
    """
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"


def build_artwork_url(image_id: str, size: str = "1080p") -> str:
    """Build full artwork URL with specified size

    Args:
        image_id: IGDB image ID
        size: Image size (720p, 1080p)

    Returns:
        Full URL to artwork image
    """
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"


def get_pc_release_date(release_dates: List[Dict[str, Any]]) -> Optional[int]:
    """Get oldest release date for DOS or PC (Microsoft Windows)

    Platform IDs:
    - 6 = PC (Microsoft Windows)
    - 13 = DOS

    NOT included: PC-9800 Series (149), Linux (3), Mac (14)

    Args:
        release_dates: List of release date dicts from IGDB API

    Returns:
        Oldest Unix timestamp for PC/DOS release or None
    """
    pc_dates = []
    for rd in release_dates:
        platform = rd.get("platform", {})
        # Platform can be an int ID or a dict with 'id'
        if isinstance(platform, dict):
            platform_id = platform.get("id")
        else:
            platform_id = platform

        # Only DOS (13) or PC Windows (6)
        if platform_id in [6, 13]:
            date = rd.get("date")
            if date:
                pc_dates.append(date)

    return min(pc_dates) if pc_dates else None
