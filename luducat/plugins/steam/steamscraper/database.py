# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""
Database models and schema management for Steam Scraper.
"""

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, 
    Text, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker, relationship
import os
import logging

from luducat.plugins.sdk.datetime import utc_now
from luducat.plugins.sdk.json import json
from .config import DATABASE_PATH, CURRENT_SCHEMA_VERSION
from .exceptions import DatabaseError


class Base(DeclarativeBase):
    pass
logger = logging.getLogger(__name__)


class Meta(Base):
    """Metadata table for schema versioning."""
    __tablename__ = 'meta'
    
    key = Column(String(50), primary_key=True)
    value = Column(String(255))
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class Game(Base):
    """Main game information table."""
    __tablename__ = 'games'
    
    appid = Column(Integer, primary_key=True)
    
    # Basic information
    name = Column(String(500))
    type = Column(String(50))                    # game, dlc, demo, music, etc.
    release_date = Column(String(100))
    required_age = Column(Integer)
    price = Column(Float)                        # Full undiscounted price
    is_free = Column(Boolean, default=False)     # Free-to-play flag
    dlc_count = Column(Integer)
    controller_support = Column(String(50))      # full, partial, none
    achievements_available = Column(Boolean, default=False)  # Has achievements
    kaggle_imported = Column(Boolean, default=False)  # DEPRECATED: kept for schema compatibility
    
    # Descriptions
    detailed_description = Column(Text)
    about_the_game = Column(Text)
    short_description = Column(Text)
    reviews = Column(Text)
    
    # Media
    header_image = Column(String(500))
    capsule_image = Column(String(500))
    background_image = Column(String(500))
    logo_url = Column(String(500))
    
    # Library assets (Steam client library view)
    library_capsule = Column(String(500))        # 600x900 portrait
    library_capsule_2x = Column(String(500))     # 1200x1800 portrait @2x
    library_hero = Column(String(500))           # Wide hero banner
    library_logo = Column(String(500))           # Library logo
    
    # Additional capsules
    main_capsule = Column(String(500))           # 616x353 main store capsule
    small_capsule = Column(String(500))          # 231x87 small capsule
    
    # Community
    community_icon = Column(String(500))         # Community icon
    
    website = Column(String(500))
    support_url = Column(String(500))
    support_email = Column(String(255))
    
    # Platform support
    windows = Column(Boolean, default=False)
    mac = Column(Boolean, default=False)
    linux = Column(Boolean, default=False)
    
    # Metacritic
    metacritic_score = Column(Integer)
    metacritic_url = Column(String(500))
    
    # Stats
    achievements = Column(Integer)
    recommendations = Column(Integer)
    user_score = Column(Float)                   # From Kaggle/SteamSpy: user rating 0-100
    score_rank = Column(String(50))              # From Kaggle/SteamSpy
    positive = Column(Integer)                   # From Kaggle/SteamSpy: positive review count
    negative = Column(Integer)                   # From Kaggle/SteamSpy: negative review count
    
    # Content and notices
    ext_user_account_notice = Column(Text)       # 3rd party account requirements
    content_descriptors = Column(JSON)           # Violence, nudity, etc. warnings
    package_groups = Column(JSON)                # Pricing tier information
    
    # Ownership and playtime
    estimated_owners = Column(String(100))
    average_playtime_forever = Column(Integer)
    average_playtime_2weeks = Column(Integer)
    median_playtime_forever = Column(Integer)
    median_playtime_2weeks = Column(Integer)
    peak_ccu = Column(Integer)
    
    # JSON arrays
    supported_languages = Column(JSON)
    full_audio_languages = Column(JSON)
    developers = Column(JSON)
    publishers = Column(JSON)
    categories = Column(JSON)
    genres = Column(JSON)
    main_genre = Column(String(100))             # Primary genre
    tags = Column(JSON)
    packages = Column(JSON)
    screenshots = Column(JSON)                   # List of screenshot URLs (no query params)
    
    # Metadata
    notes = Column(Text)
    last_updated = Column(DateTime, default=utc_now, onupdate=utc_now)
    
    # Relationships
    images = relationship("Image", back_populates="game", cascade="all, delete-orphan")
    
    @property
    def is_complete(self):
        """Has full metadata from GetAppDetails API.

        Drives re-fetch decisions in get_games_bulk(). Incomplete games
        are re-fetched during sync. Library assets are checked separately
        via has_library_assets (IStoreBrowseService probing).
        """
        return bool(
            self.name and
            self.type and
            self.release_date and
            self.detailed_description and
            self.header_image and
            self.developers and
            self.publishers and
            self.screenshots
        )

    @property
    def has_library_assets(self):
        """Has vertical cover art from IStoreBrowseService probing."""
        return bool(self.library_capsule and self.library_hero)
    
    def __repr__(self):
        return f"<Game(appid={self.appid}, name='{self.name}')>"


class Image(Base):
    """Image metadata table."""
    __tablename__ = 'images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    appid = Column(Integer, ForeignKey('games.appid', ondelete='CASCADE'), nullable=False)
    filename = Column(String(255), nullable=False)
    image_order = Column(Integer, nullable=False)
    url = Column(String(500))  # Source URL for lazy downloading
    scraped_date = Column(DateTime, default=utc_now)  # None = not downloaded yet

    # Relationship
    game = relationship("Game", back_populates="images")

    @property
    def is_downloaded(self) -> bool:
        """Check if image has been downloaded (scraped_date is set)."""
        return self.scraped_date is not None

    def __repr__(self):
        return f"<Image(appid={self.appid}, filename='{self.filename}')>"


class Database:
    """Database manager class."""
    
    def __init__(self, db_path=None):
        """Initialize database connection."""
        self.db_path = db_path or DATABASE_PATH
        self.engine = create_engine(f'sqlite:///{self.db_path}')
        self.Session = sessionmaker(bind=self.engine)
        
        # Create tables if they don't exist
        Base.metadata.create_all(self.engine)
        
        # Initialize or check schema version
        self._init_schema_version()
    
    def _init_schema_version(self):
        """Initialize or upgrade schema version."""
        session = self.Session()
        try:
            meta = session.query(Meta).filter_by(key='schema_version').first()
            
            if not meta:
                # First time setup
                meta = Meta(key='schema_version', value=str(CURRENT_SCHEMA_VERSION))
                session.add(meta)
                session.commit()
            else:
                # Check if upgrade needed
                current_version = int(meta.value)
                if current_version < CURRENT_SCHEMA_VERSION:
                    self._upgrade_schema(session, current_version, CURRENT_SCHEMA_VERSION)
                    meta.value = str(CURRENT_SCHEMA_VERSION)
                    session.commit()
        except Exception as e:
            session.rollback()
            raise DatabaseError(f"Failed to initialize schema version: {e}") from e
        finally:
            session.close()
    
    def _upgrade_schema(self, session, from_version, to_version):
        """Upgrade database schema.
        
        Handles all upgrade paths:
        - v1 → v2: Add capsule_image, background_image, logo_url
        - v2 → v3: Add 7 library asset columns
        - v1 → v3: Run both migrations
        """
        from sqlalchemy import text
        
        logger.info(f"Upgrading schema from version {from_version} to {to_version}")
        
        # Migration from v1 to v2: Add image URL columns
        if from_version == 1 and to_version >= 2:
            logger.info("Migrating schema v1 -> v2: Adding image URL columns")
            try:
                with self.engine.begin() as conn:
                    # Add new image URL columns
                    conn.execute(text("ALTER TABLE games ADD COLUMN capsule_image VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN background_image VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN logo_url VARCHAR(500)"))
                logger.info("Successfully added image URL columns")
            except Exception as e:
                logger.error(f"Failed to add image URL columns: {e}")
                # Continue anyway - columns might already exist
        
        # Migration from v2 to v3: Add library asset columns
        if from_version <= 2 and to_version >= 3:
            logger.info("Migrating schema v2 -> v3: Adding library asset columns")
            try:
                with self.engine.begin() as conn:
                    # Add library asset columns
                    conn.execute(text("ALTER TABLE games ADD COLUMN library_capsule VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN library_capsule_2x VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN library_hero VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN library_logo VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN main_capsule VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN small_capsule VARCHAR(500)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN community_icon VARCHAR(500)"))
                logger.info("Successfully added library asset columns")
            except Exception as e:
                logger.error(f"Failed to add library asset columns: {e}")
                # Continue anyway - columns might already exist
        
        # Migration from v3 to v4: Add Steam API fields (no store page text fields)
        if from_version <= 3 and to_version >= 4:
            logger.info("Migrating schema v3 -> v4: Adding Steam API fields")
            try:
                with self.engine.begin() as conn:
                    # Steam API fields only
                    conn.execute(text("ALTER TABLE games ADD COLUMN type VARCHAR(50)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN is_free BOOLEAN DEFAULT 0"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN controller_support VARCHAR(50)"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN ext_user_account_notice TEXT"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN content_descriptors JSON"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN package_groups JSON"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN main_genre VARCHAR(100)"))
                logger.info("Successfully added Steam API fields")
            except Exception as e:
                logger.error(f"Failed to add Steam API fields: {e}")
                # Continue anyway - columns might already exist
        
        # Migration from v4 to v5: Add achievements_available and kaggle_imported flags
        # Note: kaggle_imported is deprecated but kept for schema compatibility
        if from_version <= 4 and to_version >= 5:
            logger.info("Migrating schema v4 -> v5: Adding achievements_available flag")
            try:
                with self.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE games ADD COLUMN achievements_available BOOLEAN DEFAULT 0"))
                    conn.execute(text("ALTER TABLE games ADD COLUMN kaggle_imported BOOLEAN DEFAULT 0"))
                logger.info("Successfully added flags")
            except Exception as e:
                logger.error(f"Failed to add flags: {e}")

        # Migration from v5 to v6: Add url column to images table for lazy downloading
        if from_version <= 5 and to_version >= 6:
            logger.info("Migrating schema v5 -> v6: Adding url column to images table")
            try:
                with self.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE images ADD COLUMN url VARCHAR(500)"))
                logger.info("Successfully added url column to images table")
            except Exception as e:
                logger.error(f"Failed to add url column: {e}")

        # Migration from v6 to v7: Add screenshots JSON column and migrate from images table
        if from_version <= 6 and to_version >= 7:
            logger.info("Migrating schema v6 -> v7: Adding screenshots column and migrating data")
            try:
                with self.engine.begin() as conn:
                    # Add screenshots column
                    conn.execute(text("ALTER TABLE games ADD COLUMN screenshots JSON"))
                logger.info("Successfully added screenshots column")

                # Migrate existing screenshot URLs from images table
                self._migrate_screenshots_from_images(session)

            except Exception as e:
                logger.error(f"Failed to add screenshots column: {e}")

        logger.info(f"Schema upgrade complete: v{from_version} -> v{to_version}")
    
    def _migrate_screenshots_from_images(self, session):
        """Migrate screenshot URLs from images table to games.screenshots JSON column.

        Also strips query parameters from URLs during migration.
        """
        from urllib.parse import urlparse, urlunparse

        def strip_query_params(url: str) -> str:
            """Strip query parameters from URL."""
            if not url:
                return url
            parsed = urlparse(url)
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

        logger.info("Migrating screenshots from images table...")

        try:
            # Get all unique appids that have images
            from sqlalchemy import text
            result = session.execute(text(
                "SELECT DISTINCT appid FROM images WHERE url IS NOT NULL ORDER BY appid"
            ))
            appids = [row[0] for row in result]

            migrated = 0
            for appid in appids:
                # Get all screenshot URLs for this game, ordered
                img_result = session.execute(text(
                    "SELECT url FROM images WHERE appid = :appid AND url IS NOT NULL ORDER BY image_order"
                ), {"appid": appid})

                urls = [strip_query_params(row[0]) for row in img_result if row[0]]

                if urls:
                    # Update games table with JSON array
                    session.execute(text(
                        "UPDATE games SET screenshots = :screenshots WHERE appid = :appid"
                    ), {"appid": appid, "screenshots": json.dumps(urls)})
                    migrated += 1

            session.commit()
            logger.info(f"Migrated screenshots for {migrated} games")

        except Exception as e:
            logger.error(f"Failed to migrate screenshots: {e}")
            session.rollback()

    def get_session(self):
        """Get a new database session."""
        return self.Session()

    def close(self):
        """Close database connection."""
        self.engine.dispose()
