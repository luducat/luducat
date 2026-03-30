# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""GOG Plugin Database Models

SQLAlchemy models for the GOG catalog database (catalog.db).
Stores game metadata from GOGdb dumps and GOG API.
"""

from luducat.plugins.sdk.json import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_now

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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


class GogGame(Base):
    """GOG game metadata

    Stores game information from GOGdb dumps and GOG API.
    """
    __tablename__ = "gog_games"

    # Primary identifier
    gogid = Column(Integer, primary_key=True)
    slug = Column(String(255), nullable=True)

    # Basic info
    title = Column(String(500), nullable=False, index=True)
    type = Column(String(50), default="game")  # game, dlc, pack

    # Descriptions
    description = Column(Text, nullable=True)
    short_description = Column(Text, nullable=True)
    description_lead = Column(Text, nullable=True)  # Products API lead text
    description_cool = Column(Text, nullable=True)  # Products API "what's cool"

    # Release info
    release_date = Column(String(100), nullable=True)

    # JSON fields (stored as text, parsed on access)
    _developers = Column("developers", Text, default="[]")
    _publishers = Column("publishers", Text, default="[]")
    _genres = Column("genres", Text, default="[]")
    _tags = Column("tags", Text, default="[]")  # Catalog tags (from GOGdb)
    _features = Column("features", Text, default="[]")
    _screenshots = Column("screenshots", Text, default="[]")
    _user_tags = Column("user_tags", Text, default="[]")  # GOG library user tags
    _languages = Column("languages", Text, default="{}")
    _downloads_json = Column("downloads_json", Text, default="{}")  # Full download structure
    _dlcs = Column("dlcs", Text, default="[]")  # Expanded DLC info
    _videos = Column("videos", Text, default="[]")
    _links = Column("links", Text, default="{}")  # Store/support/forum URLs

    # Image URLs
    cover_url = Column(String(500), nullable=True)
    background_url = Column(String(500), nullable=True)
    logo_url = Column(String(500), nullable=True)
    icon_url = Column(String(500), nullable=True)
    galaxy_background_url = Column(String(500), nullable=True)  # v2 galaxyBackgroundImage
    icon_square_url = Column(String(500), nullable=True)         # catalog icon_square
    cover_vertical_url = Column(String(500), nullable=True)      # catalog coverVertical (full URL)
    cover_horizontal_url = Column(String(500), nullable=True)    # catalog coverHorizontal

    # Platform support
    windows = Column(Boolean, default=False)
    mac = Column(Boolean, default=False)
    linux = Column(Boolean, default=False)

    # Pricing
    price = Column(Float, nullable=True)
    is_free = Column(Boolean, default=False)

    # Series/Organization
    series_name = Column(String(255), nullable=True)     # v2 series.name (franchise)
    series_id = Column(Integer, nullable=True)           # v2 series.id
    _editions = Column("editions", Text, default="[]")   # v2 [{id, name}]
    _includes_games = Column("includes_games", Text, default="[]")  # v2 pack contents
    _is_included_in = Column("is_included_in", Text, default="[]")  # v2 which packs contain this
    _required_by = Column("required_by", Text, default="[]")        # v2 DLC required by
    _requires = Column("requires", Text, default="[]")              # v2 DLC requires

    # Ratings/Rankings
    age_rating = Column(Integer, nullable=True)          # v2 gogRating.ageRating
    reviews_count = Column(Integer, nullable=True)       # catalog reviewsCount
    rank_bestselling = Column(Integer, nullable=True)    # catalog rank
    rank_trending = Column(Integer, nullable=True)       # catalog rank

    # Detailed metadata
    _localizations = Column("localizations", Text, default="[]")  # v2 [{code, name, text, audio}]
    copyright_notice = Column(String(500), nullable=True)         # v2 copyrights
    is_using_dosbox = Column(Boolean, nullable=True)              # v2 isUsingDosBox
    is_in_development = Column(Boolean, nullable=True)            # v2 early access
    store_release_date = Column(String(20), nullable=True)        # v0 store_date
    global_release_date = Column(String(20), nullable=True)       # v2 globalReleaseDate

    # Links
    store_link = Column(String(500), nullable=True)      # v0/catalog storeLink
    forum_link = Column(String(500), nullable=True)      # v0 link_forum
    support_link = Column(String(500), nullable=True)    # v0 link_support

    # Content ratings & pricing (from catalog API)
    _content_ratings = Column("content_ratings", Text, default="[]")   # [{name, ageRating}] PEGI/ESRB/USK/BR/GOG
    _price_json = Column("price_json", Text, default="{}")             # Full price object as-is
    product_state = Column(String(50), nullable=True)                  # "default", etc.

    # Status & tracking
    is_available = Column(Boolean, default=True)  # Not delisted
    gogdb_imported = Column(Boolean, default=False)  # From GOGdb dump
    data_source = Column(String(20), default="gogdb")  # gogdb, gog_api_basic, gog_api_full
    enriched = Column(Boolean, default=False)  # True when products API data fetched
    catalog_enriched = Column(Boolean, default=False)  # True when catalog API data fetched
    rating = Column(Integer, nullable=True)  # GOG rating (stars * 10)
    changelog = Column(Text, nullable=True)  # HTML changelog from products API
    last_updated = Column(DateTime, default=utc_now)

    # Indexes
    __table_args__ = (
        Index("ix_gog_games_type", "type"),
        Index("ix_gog_games_gogdb_imported", "gogdb_imported"),
        Index("ix_gog_games_enriched", "enriched"),
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
    def tags(self) -> List[str]:
        cached = self.__dict__.get('_c_tags')
        raw = self._tags
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_tags'] = (raw, val)
        return val

    @tags.setter
    def tags(self, value: List[str]):
        self._tags = json.dumps(value)
        self.__dict__.pop('_c_tags', None)

    @property
    def features(self) -> List[str]:
        cached = self.__dict__.get('_c_features')
        raw = self._features
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_features'] = (raw, val)
        return val

    @features.setter
    def features(self, value: List[str]):
        self._features = json.dumps(value)
        self.__dict__.pop('_c_features', None)

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
    def user_tags(self) -> List[str]:
        cached = self.__dict__.get('_c_user_tags')
        raw = self._user_tags
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_user_tags'] = (raw, val)
        return val

    @user_tags.setter
    def user_tags(self, value: List[str]):
        self._user_tags = json.dumps(value)
        self.__dict__.pop('_c_user_tags', None)

    @property
    def languages(self) -> Dict[str, str]:
        cached = self.__dict__.get('_c_languages')
        raw = self._languages
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else {}
        self.__dict__['_c_languages'] = (raw, val)
        return val

    @languages.setter
    def languages(self, value: Dict[str, str]):
        self._languages = json.dumps(value)
        self.__dict__.pop('_c_languages', None)

    @property
    def downloads_json(self) -> Dict[str, Any]:
        cached = self.__dict__.get('_c_downloads_json')
        raw = self._downloads_json
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else {}
        self.__dict__['_c_downloads_json'] = (raw, val)
        return val

    @downloads_json.setter
    def downloads_json(self, value: Dict[str, Any]):
        self._downloads_json = json.dumps(value)
        self.__dict__.pop('_c_downloads_json', None)

    @property
    def dlcs(self) -> List[Dict[str, Any]]:
        cached = self.__dict__.get('_c_dlcs')
        raw = self._dlcs
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_dlcs'] = (raw, val)
        return val

    @dlcs.setter
    def dlcs(self, value: List[Dict[str, Any]]):
        self._dlcs = json.dumps(value)
        self.__dict__.pop('_c_dlcs', None)

    @property
    def videos(self) -> List[Dict[str, Any]]:
        cached = self.__dict__.get('_c_videos')
        raw = self._videos
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_videos'] = (raw, val)
        return val

    @videos.setter
    def videos(self, value: List[Dict[str, Any]]):
        self._videos = json.dumps(value)
        self.__dict__.pop('_c_videos', None)

    @property
    def links(self) -> list:
        cached = self.__dict__.get('_c_links')
        raw = self._links
        if cached is not None and cached[0] is raw:
            return cached[1]
        parsed = json.loads(raw) if raw else {}
        # Normalize dict {"type": "url"} → list [{"type": ..., "url": ...}]
        if isinstance(parsed, dict):
            val = [{"type": k, "url": v} for k, v in parsed.items() if v]
        else:
            val = parsed if isinstance(parsed, list) else []
        self.__dict__['_c_links'] = (raw, val)
        return val

    @links.setter
    def links(self, value: Dict[str, str]):
        self._links = json.dumps(value)
        self.__dict__.pop('_c_links', None)

    @property
    def editions(self) -> List[Dict[str, Any]]:
        cached = self.__dict__.get('_c_editions')
        raw = self._editions
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_editions'] = (raw, val)
        return val

    @editions.setter
    def editions(self, value: List[Dict[str, Any]]):
        self._editions = json.dumps(value)
        self.__dict__.pop('_c_editions', None)

    @property
    def includes_games(self) -> List[int]:
        cached = self.__dict__.get('_c_includes_games')
        raw = self._includes_games
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_includes_games'] = (raw, val)
        return val

    @includes_games.setter
    def includes_games(self, value: List[int]):
        self._includes_games = json.dumps(value)
        self.__dict__.pop('_c_includes_games', None)

    @property
    def is_included_in(self) -> List[int]:
        cached = self.__dict__.get('_c_is_included_in')
        raw = self._is_included_in
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_is_included_in'] = (raw, val)
        return val

    @is_included_in.setter
    def is_included_in(self, value: List[int]):
        self._is_included_in = json.dumps(value)
        self.__dict__.pop('_c_is_included_in', None)

    @property
    def required_by(self) -> List[int]:
        cached = self.__dict__.get('_c_required_by')
        raw = self._required_by
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_required_by'] = (raw, val)
        return val

    @required_by.setter
    def required_by(self, value: List[int]):
        self._required_by = json.dumps(value)
        self.__dict__.pop('_c_required_by', None)

    @property
    def requires(self) -> List[int]:
        cached = self.__dict__.get('_c_requires')
        raw = self._requires
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_requires'] = (raw, val)
        return val

    @requires.setter
    def requires(self, value: List[int]):
        self._requires = json.dumps(value)
        self.__dict__.pop('_c_requires', None)

    @property
    def localizations(self) -> List[Dict[str, Any]]:
        cached = self.__dict__.get('_c_localizations')
        raw = self._localizations
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_localizations'] = (raw, val)
        return val

    @localizations.setter
    def localizations(self, value: List[Dict[str, Any]]):
        self._localizations = json.dumps(value)
        self.__dict__.pop('_c_localizations', None)

    @property
    def content_ratings(self) -> List[Dict[str, Any]]:
        cached = self.__dict__.get('_c_content_ratings')
        raw = self._content_ratings
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else []
        self.__dict__['_c_content_ratings'] = (raw, val)
        return val

    @content_ratings.setter
    def content_ratings(self, value: List[Dict[str, Any]]):
        self._content_ratings = json.dumps(value)
        self.__dict__.pop('_c_content_ratings', None)

    @property
    def price_json(self) -> Dict[str, Any]:
        cached = self.__dict__.get('_c_price_json')
        raw = self._price_json
        if cached is not None and cached[0] is raw:
            return cached[1]
        val = json.loads(raw) if raw else {}
        self.__dict__['_c_price_json'] = (raw, val)
        return val

    @price_json.setter
    def price_json(self, value: Dict[str, Any]):
        self._price_json = json.dumps(value)
        self.__dict__.pop('_c_price_json', None)

    def to_dict(self, include_description: bool = True) -> Dict[str, Any]:
        """Convert to dictionary for metadata access

        Args:
            include_description: If True, include heavy text fields
                (description, description_lead, description_cool, changelog,
                downloads_json). False for bulk loads where descriptions are
                lazy-loaded on demand.
        """
        result = {
            "gogid": self.gogid,
            "slug": self.slug,
            "title": self.title,
            "type": self.type,
            "short_description": self.short_description,
            "release_date": self.release_date,
            "developers": self.developers,
            "publishers": self.publishers,
            "genres": self.genres,
            "tags": self.tags,
            "features": self.features,
            "user_tags": self.user_tags,
            "cover_url": self.cover_url,
            "background_url": self.background_url,
            "logo_url": self.logo_url,
            "icon_url": self.icon_url,
            "galaxy_background_url": self.galaxy_background_url,
            "icon_square_url": self.icon_square_url,
            "cover_vertical_url": self.cover_vertical_url,
            "cover_horizontal_url": self.cover_horizontal_url,
            "screenshots": self.screenshots,
            "windows": self.windows,
            "mac": self.mac,
            "linux": self.linux,
            "price": self.price,
            "is_free": self.is_free,
            "is_available": self.is_available,
            "data_source": self.data_source,
            "enriched": self.enriched,
            "catalog_enriched": self.catalog_enriched,
            "rating": self.rating,
            "languages": self.languages,
            "localizations": self.localizations,
            "dlcs": self.dlcs,
            "videos": self.videos,
            "links": self.links,
            # Series/Organization
            "series_name": self.series_name,
            "series_id": self.series_id,
            "editions": self.editions,
            "includes_games": self.includes_games,
            "is_included_in": self.is_included_in,
            "required_by": self.required_by,
            "requires": self.requires,
            # Ratings/Rankings
            "age_rating": self.age_rating,
            "reviews_count": self.reviews_count,
            "rank_bestselling": self.rank_bestselling,
            "rank_trending": self.rank_trending,
            # Detailed metadata
            "copyright_notice": self.copyright_notice,
            "is_using_dosbox": self.is_using_dosbox,
            "is_in_development": self.is_in_development,
            "store_release_date": self.store_release_date,
            "global_release_date": self.global_release_date,
            # Links
            "store_link": self.store_link,
            "forum_link": self.forum_link,
            "support_link": self.support_link,
            # Content ratings & pricing
            "content_ratings": self.content_ratings,
            "price_json": self.price_json,
            "product_state": self.product_state,
        }

        if include_description:
            result["description"] = self.description
            result["description_lead"] = self.description_lead
            result["description_cool"] = self.description_cool
            result["changelog"] = self.changelog
            result["downloads_json"] = self.downloads_json

        return result


class GogImage(Base):
    """Cached image information for GOG games"""
    __tablename__ = "gog_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gogid = Column(Integer, ForeignKey("gog_games.gogid"), index=True)
    url = Column(String(500), nullable=False)
    image_type = Column(String(50), nullable=False)  # cover, background, screenshot
    image_order = Column(Integer, default=0)
    cached_path = Column(String(500), nullable=True)  # Local cache path if downloaded

    # Relationship
    game = relationship("GogGame", backref="images")


class GogDatabase:
    """Database access layer for GOG catalog

    Usage:
        db = GogDatabase(data_dir / "catalog.db")
        db.initialize()

        # Get game
        game = db.get_game(123456)

        # Add/update game
        db.upsert_game(gog_game_obj)

        db.close()
    """

    # New columns added after initial schema — for migration
    _MIGRATION_COLUMNS = {
        "gog_games": [
            ("description_lead", "TEXT"),
            ("description_cool", "TEXT"),
            ("user_tags", "TEXT DEFAULT '[]'"),
            ("languages", "TEXT DEFAULT '{}'"),
            ("downloads_json", "TEXT DEFAULT '{}'"),
            ("dlcs", "TEXT DEFAULT '[]'"),
            ("videos", "TEXT DEFAULT '[]'"),
            ("links", "TEXT DEFAULT '{}'"),
            ("data_source", "VARCHAR(20) DEFAULT 'gogdb'"),
            ("enriched", "BOOLEAN DEFAULT 0"),
            ("rating", "INTEGER"),
            ("changelog", "TEXT"),
            # v2 — images
            ("galaxy_background_url", "VARCHAR(500)"),
            ("icon_square_url", "VARCHAR(500)"),
            ("cover_vertical_url", "VARCHAR(500)"),
            ("cover_horizontal_url", "VARCHAR(500)"),
            # v2 — series/organization
            ("series_name", "VARCHAR(255)"),
            ("series_id", "INTEGER"),
            ("editions", "TEXT DEFAULT '[]'"),
            ("includes_games", "TEXT DEFAULT '[]'"),
            ("is_included_in", "TEXT DEFAULT '[]'"),
            ("required_by", "TEXT DEFAULT '[]'"),
            ("requires", "TEXT DEFAULT '[]'"),
            # v2 — ratings/rankings
            ("age_rating", "INTEGER"),
            ("reviews_count", "INTEGER"),
            ("rank_bestselling", "INTEGER"),
            ("rank_trending", "INTEGER"),
            # v2 — detailed metadata
            ("localizations", "TEXT DEFAULT '[]'"),
            ("copyright_notice", "VARCHAR(500)"),
            ("is_using_dosbox", "BOOLEAN"),
            ("is_in_development", "BOOLEAN"),
            ("store_release_date", "VARCHAR(20)"),
            ("global_release_date", "VARCHAR(20)"),
            # v0 — links
            ("store_link", "VARCHAR(500)"),
            ("forum_link", "VARCHAR(500)"),
            ("support_link", "VARCHAR(500)"),
            # enrichment flags
            ("catalog_enriched", "BOOLEAN DEFAULT 0"),
            # v3 — content ratings & pricing (from catalog API)
            ("content_ratings", "TEXT DEFAULT '[]'"),
            ("price_json", "TEXT DEFAULT '{}'"),
            ("product_state", "VARCHAR(50)"),
        ],
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self._session_factory = sessionmaker(bind=self.engine)
        self._session: Optional[Session] = None

    def initialize(self) -> None:
        """Create tables if they don't exist, then migrate new columns"""
        Base.metadata.create_all(self.engine)
        self._migrate_columns()
        self._create_bundle_table()

    def _migrate_columns(self) -> None:
        """Add any missing columns to existing tables (ALTER TABLE)"""
        insp = inspect(self.engine)
        for table_name, columns in self._MIGRATION_COLUMNS.items():
            if not insp.has_table(table_name):
                continue
            existing = {col["name"] for col in insp.get_columns(table_name)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                    with self.engine.begin() as conn:
                        conn.execute(text(sql))
                    logger.info(f"Migrated: ALTER TABLE {table_name} ADD COLUMN {col_name}")

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

    def get_game(self, gogid: int) -> Optional[GogGame]:
        """Get game by GOG ID"""
        return self.session.query(GogGame).filter(GogGame.gogid == gogid).first()

    def get_all_games(self, include_dlc: bool = False) -> List[GogGame]:
        """Get all games from catalog"""
        query = self.session.query(GogGame)
        if not include_dlc:
            query = query.filter(GogGame.type == "game")
        return query.all()

    def get_game_count(self, include_dlc: bool = False) -> int:
        """Get total game count"""
        query = self.session.query(GogGame)
        if not include_dlc:
            query = query.filter(GogGame.type == "game")
        return query.count()

    def get_all_gogids(self, include_dlc: bool = False) -> List[int]:
        """Get all GOG IDs (for sync checking)"""
        query = self.session.query(GogGame.gogid)
        if not include_dlc:
            query = query.filter(GogGame.type == "game")
        return [row[0] for row in query.all()]

    def game_exists(self, gogid: int) -> bool:
        """Check if game exists in database"""
        return self.session.query(GogGame.gogid).filter(
            GogGame.gogid == gogid
        ).first() is not None

    def upsert_game(self, game: GogGame) -> None:
        """Insert or update game"""
        existing = self.get_game(game.gogid)
        if existing:
            # Update fields
            for key, value in game.to_dict().items():
                if key != "gogid":
                    setattr(existing, key, value)
            existing.last_updated = utc_now()
        else:
            self.session.add(game)

    def get_unenriched_owned_gogids(self, owned_gogids: List[int]) -> List[int]:
        """Get owned game IDs that haven't been enriched via products API"""
        if not owned_gogids:
            return []
        results = self.session.query(GogGame.gogid).filter(
            GogGame.gogid.in_(owned_gogids),
            GogGame.enriched == False,  # noqa: E712
        ).all()
        return [row[0] for row in results]

    def get_catalog_unenriched_gogids(self, owned_gogids: List[int]) -> List[int]:
        """Get owned game IDs that haven't been enriched via catalog API.

        Args:
            owned_gogids: List of owned GOG IDs

        Returns:
            List of GOG IDs where catalog_enriched is False
        """
        if not owned_gogids:
            return []
        results = self.session.query(GogGame.gogid).filter(
            GogGame.gogid.in_(owned_gogids),
            GogGame.catalog_enriched == False,  # noqa: E712
        ).all()
        return [row[0] for row in results]

    def get_gap_games(self, owned_gogids: List[int]) -> List[int]:
        """Get owned game IDs that have no GOGdb or catalog enrichment.

        These are games from getFilteredProducts that need gap-filling.

        Args:
            owned_gogids: List of owned GOG IDs

        Returns:
            List of GOG IDs needing enrichment
        """
        if not owned_gogids:
            return []
        from sqlalchemy import and_
        results = self.session.query(GogGame.gogid).filter(
            GogGame.gogid.in_(owned_gogids),
            and_(
                GogGame.gogdb_imported == False,  # noqa: E712
                GogGame.catalog_enriched == False,  # noqa: E712
            ),
        ).all()
        return [row[0] for row in results]

    def commit(self) -> None:
        """Commit pending changes"""
        self.session.commit()

    def rollback(self) -> None:
        """Rollback pending changes"""
        self.session.rollback()

    def bulk_insert_games(self, games: List[GogGame]) -> int:
        """Bulk insert games (skip existing)

        Returns:
            Number of games inserted
        """
        # Get existing IDs
        existing_ids = set(self.get_all_gogids(include_dlc=True))

        # Filter new games
        new_games = [g for g in games if g.gogid not in existing_ids]

        if new_games:
            self.session.bulk_save_objects(new_games)
            self.session.commit()

        return len(new_games)

    def get_games_metadata_bulk(
        self, gogids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        """Get metadata for multiple games efficiently

        Defers heavy text columns (descriptions, changelog, downloads)
        that are not needed for the startup cache.

        Args:
            gogids: List of GOG IDs to fetch

        Returns:
            Dict mapping gogid -> metadata dict
        """
        from sqlalchemy.orm import defer

        games = (
            self.session.query(GogGame)
            .filter(GogGame.gogid.in_(gogids))
            .options(
                defer(GogGame.description),
                defer(GogGame.description_lead),
                defer(GogGame.description_cool),
                defer(GogGame.changelog),
                defer(GogGame._downloads_json),
                defer(GogGame.copyright_notice),
            )
            .all()
        )

        return {game.gogid: game.to_dict(include_description=False) for game in games}

    # ── Bundle mapping (for delisted detection) ──────────────────────

    def _create_bundle_table(self) -> None:
        """Create gog_bundles mapping table if it doesn't exist."""
        with self.engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS gog_bundles ("
                "  game_id INTEGER NOT NULL,"
                "  bundle_id INTEGER NOT NULL,"
                "  PRIMARY KEY (game_id, bundle_id)"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_bundles_game "
                "ON gog_bundles(game_id)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_bundles_bundle "
                "ON gog_bundles(bundle_id)"
            ))

    def rebuild_bundle_map(self) -> int:
        """Rebuild gog_bundles from is_included_in data on all GogGame rows.

        Returns:
            Number of bundle mappings created.
        """
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM gog_bundles"))

            games = self.session.query(
                GogGame.gogid, GogGame._is_included_in
            ).filter(
                GogGame._is_included_in.isnot(None),
                GogGame._is_included_in != "[]",
                GogGame._is_included_in != "",
            ).all()

            count = 0
            for gogid, included_raw in games:
                try:
                    bundle_ids = json.loads(included_raw) if included_raw else []
                except (json.JSONDecodeError, TypeError):
                    continue
                for bid in bundle_ids:
                    if isinstance(bid, int) and bid > 0:
                        conn.execute(text(
                            "INSERT OR IGNORE INTO gog_bundles (game_id, bundle_id) "
                            "VALUES (:gid, :bid)"
                        ), {"gid": gogid, "bid": bid})
                        count += 1

        logger.info("Rebuilt bundle map: %d mappings", count)
        return count

    def get_bundle_ids(self, game_id: int) -> List[int]:
        """Get bundle IDs that contain this game."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT bundle_id FROM gog_bundles WHERE game_id = :gid"),
                {"gid": game_id},
            ).fetchall()
        return [row[0] for row in rows]

    def get_games_in_bundle(self, bundle_id: int) -> List[int]:
        """Get game IDs contained in a bundle."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT game_id FROM gog_bundles WHERE bundle_id = :bid"),
                {"bid": bundle_id},
            ).fetchall()
        return [row[0] for row in rows]
