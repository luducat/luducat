# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""Main database for luducat

This database stores user-specific data:
- Deduplicated game registry
- Store-specific game instances
- User tags and game-tag associations
- Favorites, hidden games, custom notes

Plugin catalog data is stored in separate per-plugin databases.
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    scoped_session,
    sessionmaker,
)
from sqlalchemy.orm.attributes import flag_modified

from .config import get_data_dir
from .constants import DEFAULT_TAG_COLOR
from .dt import utc_now

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base"""
    pass


def generate_uuid() -> str:
    """Generate UUID string for game IDs"""
    return str(uuid.uuid4())


class Game(Base):
    """Deduplicated game registry

    Each unique game has one entry here, even if owned on multiple stores.
    The primary_store indicates which store's data is preferred for display.
    """
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    normalized_title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    primary_store: Mapped[str] = mapped_column(String(50), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    store_games: Mapped[List["StoreGame"]] = relationship(
        "StoreGame", back_populates="game", cascade="all, delete-orphan"
    )
    tags: Mapped[List["UserTag"]] = relationship(
        "UserTag", secondary="game_tags", back_populates="games"
    )
    user_data: Mapped[Optional["UserGameData"]] = relationship(
        "UserGameData", back_populates="game", uselist=False, cascade="all, delete-orphan"
    )
    play_sessions: Mapped[List["PlaySession"]] = relationship(
        "PlaySession", back_populates="game", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Game(id={self.id[:8]}, title='{self.title}', primary_store={self.primary_store})>"


class StoreGame(Base):
    """Platform-specific game instance

    Links a Game to a specific store with store-specific metadata.
    A game owned on both Steam and GOG has two StoreGame entries.

    Attributes:
        family_shared: Family sharing status
            0 = Not borrowed (owned or may be shared to others)
            1 = Borrowed via Family Sharing
    """
    __tablename__ = "store_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    store_app_id: Mapped[str] = mapped_column(String(100), nullable=False)
    launch_url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Family sharing status (0=owned, 1=borrowed via family sharing)
    family_shared: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    # Comma-separated SteamIDs of family members who own this game (for borrowed games)
    family_shared_owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Steam privacy status (0=normal, 1=marked private on user's Steam profile)
    is_private_app: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    # Delisted status (0=still listed, 1=not in public Steam app catalog as of last sync)
    is_delisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    # Installation status (synced from store plugins)
    is_installed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    install_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Cached metadata (from plugin, may be stale)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    metadata_fetched: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationship
    game: Mapped["Game"] = relationship("Game", back_populates="store_games")

    __table_args__ = (
        UniqueConstraint("store_name", "store_app_id", name="uq_store_app"),
    )

    def __repr__(self) -> str:
        return (
            f"<StoreGame(store={self.store_name}, "
            f"app_id={self.store_app_id}, "
            f"family_shared={self.family_shared})>"
        )


class UserTag(Base):
    """User-created tag for organizing games

    Tags are global (not per-store) and user-defined.
    Each tag has a display name and optional color.

    Attributes:
        source: Who created the tag — "native" (user), "gog", "steam", etc.
        tag_type: "user" (normal), "imported" (from store), "special" (future)
        external_id: Store's tag ID for two-way sync
        description: User-editable description shown in hover text
        score: Quick-access ranking (-99 to +99). Positive = preferred, negative = blocked.
    """
    __tablename__ = "user_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    color: Mapped[str] = mapped_column(
        String(7), nullable=False, default=DEFAULT_TAG_COLOR
    )  # Hex color
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="native")
    tag_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nsfw_override: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationship
    games: Mapped[List["Game"]] = relationship(
        "Game", secondary="game_tags", back_populates="tags"
    )

    @property
    def is_quick_access(self) -> bool:
        """Tags with positive score appear in quick-access bar."""
        return self.score > 0

    def __repr__(self) -> str:
        return f"<UserTag(id={self.id}, name='{self.name}', source='{self.source}')>"


class GameTag(Base):
    """Many-to-many association between games and tags

    Attributes:
        assigned_by: Which source assigned this tag to this game
        assigned_at: When this tag was assigned to this game
    """
    __tablename__ = "game_tags"

    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user_tags.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_by: Mapped[str] = mapped_column(String(50), nullable=False, default="native")
    assigned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=utc_now
    )


class UserGameData(Base):
    """User-specific data for a game

    Stores favorites, hidden status, custom notes, and launch history.
    One-to-one relationship with Game.
    """
    __tablename__ = "user_game_data"

    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    custom_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_launched: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    launch_count: Mapped[int] = mapped_column(Integer, default=0)
    playtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    nsfw_override: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    launch_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationship
    game: Mapped["Game"] = relationship("Game", back_populates="user_data")

    def __repr__(self) -> str:
        return f"<UserGameData(game_id={self.game_id[:8]}, favorite={self.is_favorite})>"


class PlaySession(Base):
    """Individual play session record

    Tracks when games were played, from which store, and for how long.
    Used for:
    - Local tracking when launching through luducat
    - Imported playtime data from store APIs (Steam, GOG, Epic)
    - Future infographics showing play patterns over time

    For local sessions: start_time is set, end_time set when trackable
    For imported data: start_time may be NULL, duration_minutes contains total
    """
    __tablename__ = "play_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), nullable=False, index=True
    )
    store_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationship
    game: Mapped["Game"] = relationship("Game", back_populates="play_sessions")

    def __repr__(self) -> str:
        return (
            f"<PlaySession(game_id={self.game_id[:8]}, "
            f"store={self.store_name}, source={self.source})>"
        )


class Collection(Base):
    """User-created game collection (dynamic filter or static game list)

    Dynamic collections store a serialized filter dict and compute results on the fly.
    Static collections store an explicit set of game IDs via CollectionGame.
    """
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # "dynamic" or "static"
    color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)  # hex color
    filter_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # dynamic only
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship (static collections only)
    collection_games: Mapped[List["CollectionGame"]] = relationship(
        "CollectionGame", back_populates="collection", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Collection(id={self.id}, name='{self.name}', type='{self.type}')>"


class CollectionGame(Base):
    """Many-to-many association between static collections and games"""
    __tablename__ = "collection_games"

    collection_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    game_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("games.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Relationships
    collection: Mapped["Collection"] = relationship("Collection", back_populates="collection_games")
    game: Mapped["Game"] = relationship("Game")

    def __repr__(self) -> str:
        return f"<CollectionGame(collection_id={self.collection_id}, game_id={self.game_id[:8]})>"


class SchemaMeta(Base):
    """Schema metadata for migrations"""
    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Database:
    """Main database manager

    Handles connection, session management, and schema initialization.

    Usage:
        db = Database()
        session = db.get_session()

        # Use session...
        game = session.query(Game).filter_by(title="Portal").first()

        # Don't forget to close
        db.close()

    Or use as context manager:
        with Database() as db:
            session = db.get_session()
            # Use session...
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection

        Args:
            db_path: Custom database path (for testing).
                    Defaults to XDG data directory.
        """
        if db_path is None:
            data_dir = get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "games.db"

        self.db_path = db_path
        self._closed = False
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,  # Set True for SQL debugging
            future=True,
            poolclass=NullPool,  # Disable connection pooling for thread safety
            connect_args={
                "check_same_thread": False,  # Allow cross-thread usage (DataLoaderWorker)
                "timeout": 30,  # Wait up to 30s for locks
            },
        )

        # Configure SQLite for multi-threaded access and FUSE compatibility
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for concurrent access
            cursor.execute("PRAGMA busy_timeout=30000")  # 30s timeout for busy database
            cursor.execute("PRAGMA synchronous=NORMAL")  # Balance safety vs performance
            cursor.execute("PRAGMA mmap_size=0")  # Disable mmap for FUSE compatibility (AppImage)
            cursor.close()

        # Use scoped_session for thread-safe session management
        # Each thread (main thread, DataLoaderWorker, SyncWorker) gets its own session
        session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(session_factory)

        # Initialize schema using Alembic migrations
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema using Alembic migrations.

        For fresh databases: Creates tables and stamps at head revision.
        For existing databases: Runs any pending migrations.
        """
        from .migrations import init_or_migrate
        init_or_migrate(self.engine)

    def get_session(self):
        """Get database session (thread-safe)

        Uses scoped_session which automatically provides a separate
        session per thread. Safe to call from main thread and workers.

        Returns:
            SQLAlchemy Session for the current thread
        """
        return self.Session()

    def new_session(self):
        """Create a standalone session independent of the scoped registry.

        Use this when the caller needs a session that won't interfere with
        the thread-scoped session — e.g., in callbacks that may fire during
        another session's lifetime via QApplication.processEvents().

        The caller MUST close this session when done.

        Returns:
            A new, independent SQLAlchemy Session
        """
        return self.Session.session_factory()

    def close(self) -> None:
        """Close database connection"""
        if self._closed:
            return
        self._closed = True
        self.Session.remove()  # Clean up scoped session registry
        self.engine.dispose()
        logger.debug("Database connection closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


# Utility functions for common operations

def _strip_edition_suffixes(title: str) -> str:
    """Strip trailing edition/remaster suffixes that cause cross-store mismatches.

    Conservative: only strips suffixes known to differ between stores.
    Trailing-only ($ anchor) to avoid mangling mid-title words.
    """
    import re

    _EDITION_WORDS = (
        r"definitive|enhanced|remaster(?:ed)?|goty|game of the year|gold|platinum|"
        r"deluxe|ultimate|complete|special|classic|hd|standard|"
        r"director'?s?\s*cut|redux|deathinitive|royal"
    )

    # "Game - Definitive Edition" or "Game: Enhanced Edition"
    # separator + suffix word + optional "Edition"
    title = re.sub(
        rf"\s*[-:]\s*(?:{_EDITION_WORDS})(?:\s+edition)?\s*$", "", title, flags=re.IGNORECASE
    )

    # "Game GOTY Edition" or "Game Gold Edition" (suffix word + "Edition")
    title = re.sub(
        rf"\s+(?:{_EDITION_WORDS})\s+edition\s*$", "", title, flags=re.IGNORECASE
    )

    # "Game (Special Edition)" — parenthesized edition
    title = re.sub(
        rf"\s*\(\s*(?:{_EDITION_WORDS})(?:\s+edition)?\s*\)\s*$", "", title, flags=re.IGNORECASE
    )

    # Bare trailing qualifier: "Game HD", "Game GOTY", "Game Gold", etc.
    _BARE_SUFFIXES = (
        r"hd|classic|goty|gold|platinum|redux|remastered|remaster|deluxe"
    )
    title = re.sub(rf"\s+(?:{_BARE_SUFFIXES})\s*$", "", title, flags=re.IGNORECASE)

    return title


def _roman_to_arabic(title: str) -> str:
    """Convert standalone Roman numerals (II–XX) to Arabic in a title.

    Word-boundary-safe. Single 'I' excluded (too many false positives:
    'I Am Alive', 'I Expect You To Die'). V and X included (rare as
    standalone non-numeral words in game titles).
    """
    import re

    _ROMAN_MAP = {
        "XX": "20", "XIX": "19", "XVIII": "18", "XVII": "17", "XVI": "16",
        "XV": "15", "XIV": "14", "XIII": "13", "XII": "12", "XI": "11",
        "X": "10", "IX": "9", "VIII": "8", "VII": "7", "VI": "6",
        "V": "5", "IV": "4", "III": "3", "II": "2",
    }

    # Build pattern: longest first to avoid partial matches (XVIII before XVI etc.)
    _ROMAN_PATTERN = re.compile(
        r"\b(" + "|".join(_ROMAN_MAP.keys()) + r")\b", re.IGNORECASE
    )

    def _replace(m):
        return _ROMAN_MAP[m.group(1).upper()]

    return _ROMAN_PATTERN.sub(_replace, title)


def normalize_title(title: str) -> str:
    """Normalize game title for cross-store deduplication.

    Pipeline (order matters):
    1. & → and (before punctuation strip)
    2. Strip ™®© and (tm)/(r)/(c) text markers
    3. Remove parenthesized years — (2012), (1998)
    4. Strip edition suffixes (trailing only, conservative)
    5. Strip leading articles (the, a, an)
    6. Remove mid-title "the" after : or -
    7. Roman → Arabic numerals (II–XX, word-boundary-safe)
    8. Remove punctuation + collapse whitespace
    """
    import re

    # Lowercase
    normalized = title.lower()

    # 1. & → and
    normalized = normalized.replace("&", "and")

    # 2. Strip trademark symbols and text markers
    normalized = re.sub(r"[™®©]", "", normalized)
    normalized = re.sub(r"\((?:tm|r|c)\)", "", normalized, flags=re.IGNORECASE)

    # 3. Remove parenthesized years — (2012), (1998)
    normalized = re.sub(r"\s*\(\d{4}\)\s*", " ", normalized)

    # 4. Strip edition suffixes
    normalized = _strip_edition_suffixes(normalized)

    # 5. Strip leading articles
    articles = ["the ", "a ", "an "]
    for article in articles:
        if normalized.startswith(article):
            normalized = normalized[len(article):]
            break

    # 6. Remove mid-title "the" after : or -
    normalized = re.sub(r"([:–—-])\s*the\s+", r"\1 ", normalized)

    # 7. Roman → Arabic numerals
    normalized = _roman_to_arabic(normalized)

    # 8. Remove punctuation and extra whitespace
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def _extract_parent_title(normalized: str) -> Optional[str]:
    """Extract parent title before the first subtitle separator.

    Used for secondary dedup matching: "elder scrolls 5 skyrim" → "elder scrolls 5"
    when the colon/dash was already stripped by normalize_title().

    Since normalize_title() removes punctuation, we detect subtitle boundaries by
    looking for the original colon/dash position. Instead, we work on the raw title
    before normalization. This helper is called with the *raw* title.

    Returns None if:
    - No subtitle separator (: or  - ) found
    - Parent portion has fewer than 2 words
    - Parent has exactly 2 words but no series number (too generic,
      e.g. "Dark Souls" could false-match "Dark Souls: Remastered")
    - Parent has exactly 2 words WITH a series number → valid
      (e.g. "Wizardry 6", "Wizardry VII" — franchise + number is specific enough)
    """
    import re

    # Find first subtitle separator (colon or spaced dash)
    match = re.search(r"\s*(?::\s+|\s+-\s+)", normalized)
    if not match:
        return None

    parent = normalized[:match.start()].strip()
    if not parent:
        return None

    words = parent.split()
    if len(words) < 2:
        return None

    # 2-word parents: only valid if they contain a series number (digit or Roman
    # numeral). "Wizardry 6" and "Wizardry VII" are specific enough; "Dark Souls"
    # and "Assassin's Creed" are too generic (would false-match subtitled sequels).
    if len(words) == 2:
        _HAS_SERIES_NUM = re.compile(
            r"\b(?:\d+|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV|XVI|XVII|XVIII|XIX|XX)\b",
            re.IGNORECASE,
        )
        if not _HAS_SERIES_NUM.search(parent):
            return None

    return parent


def find_or_create_game(
    session,
    title: str,
    store_name: str,
    store_app_id: str,
    launch_url: str,
    metadata: Optional[dict] = None,
    family_shared: int = 0,
    family_shared_owner: Optional[str] = None,
    is_private_app: int = 0,
    is_delisted: int = 0,
) -> Game:
    """Find existing game or create new one

    Attempts to match by normalized title to avoid duplicates
    when the same game is owned on multiple stores.

    Args:
        session: Database session
        title: Game title
        store_name: Store identifier (e.g., "steam")
        store_app_id: Store-specific app ID
        launch_url: Launch URL for this store
        metadata: Optional metadata dict to cache
        family_shared: Family sharing status (0=owned, 1=borrowed)
        family_shared_owner: SteamID of owner (for borrowed games)

    Returns:
        Game object (existing or newly created)
    """
    normalized = normalize_title(title)

    # Check if this exact store_app already exists
    existing_store_game = session.query(StoreGame).filter_by(
        store_name=store_name, store_app_id=store_app_id
    ).first()

    if existing_store_game:
        # Update metadata if provided (merge, preserving enrichment state)
        if metadata:
            existing_meta = existing_store_game.metadata_json or {}
            # Preserve enrichment state that would be lost by overwrite
            enrichment_keys = ("_sources", "_attempted_by", "_enriched_via")
            saved = {k: existing_meta[k] for k in enrichment_keys if k in existing_meta}
            # Merge fresh store data over existing
            existing_meta.update(metadata)
            # Restore enrichment state
            existing_meta.update(saved)
            existing_store_game.metadata_json = existing_meta
            flag_modified(existing_store_game, "metadata_json")
            existing_store_game.metadata_fetched = utc_now()
        # Update family_shared and steam status flags
        existing_store_game.family_shared = family_shared
        existing_store_game.family_shared_owner = family_shared_owner
        existing_store_game.is_private_app = is_private_app
        existing_store_game.is_delisted = is_delisted
        return existing_store_game.game

    # Try to find game by normalized title
    game = session.query(Game).filter_by(normalized_title=normalized).first()

    # Secondary matching: parent-title colon/dash dedup
    # "Elder Scrolls V: Skyrim" (normalized: "elder scrolls 5 skyrim") should match
    # "Elder Scrolls V" (normalized: "elder scrolls 5") across stores.
    # Safety: never match if the candidate already has a store_game on the same
    # store — that means they're different products (e.g., HL2 vs HL2: Deathmatch).
    if game is None:
        # Forward: incoming title has subtitle → try matching parent only
        parent = _extract_parent_title(title)
        if parent:
            parent_normalized = normalize_title(parent)
            candidate = session.query(Game).filter_by(
                normalized_title=parent_normalized
            ).first()
            # Only match cross-store (no overlap on incoming store)
            if candidate and not any(
                sg.store_name == store_name for sg in candidate.store_games
            ):
                game = candidate

        # Reverse: incoming title has no subtitle → check if any existing game's
        # full title has this as its parent portion
        if game is None:
            # Use LIKE query for efficiency: existing titles that start with our
            # normalized title followed by a space (the subtitle part)
            candidates = session.query(Game).filter(
                Game.normalized_title.like(normalized + " %")
            ).all()
            for candidate in candidates:
                # Verify via _extract_parent_title on the candidate's original title
                candidate_parent = _extract_parent_title(candidate.title)
                if candidate_parent and normalize_title(candidate_parent) == normalized:
                    # Only match cross-store
                    if not any(sg.store_name == store_name for sg in candidate.store_games):
                        game = candidate
                        break

    if game is None:
        # Create new game
        game = Game(
            title=title,
            normalized_title=normalized,
            primary_store=store_name,
        )
        session.add(game)
        session.flush()  # Get the ID

    # Create store_game entry
    store_game = StoreGame(
        game_id=game.id,
        store_name=store_name,
        store_app_id=store_app_id,
        launch_url=launch_url,
        family_shared=family_shared,
        family_shared_owner=family_shared_owner,
        is_private_app=is_private_app,
        is_delisted=is_delisted,
        metadata_json=metadata,
        metadata_fetched=utc_now() if metadata else None,
    )
    session.add(store_game)

    return game


def repair_parent_dedup(session) -> int:
    """One-time repair: merge games that should have been deduped via parent matching.

    Finds pairs where one game's normalized_title is a valid parent of another's,
    using the updated _extract_parent_title (2-word parents with series numbers).
    Merges by moving store_games from the shorter-title game to the longer one.

    Safety: never merges games that share a store (same store = different products).

    Returns number of merges performed.
    """
    import logging

    log = logging.getLogger(__name__)
    all_games = session.query(Game).all()
    # Build lookup: normalized_title → Game
    by_norm = {}
    for g in all_games:
        by_norm.setdefault(g.normalized_title, []).append(g)

    merged = 0
    seen_ids = set()

    for g in all_games:
        if g.id in seen_ids:
            continue
        # Try extracting parent from this game's raw title
        parent = _extract_parent_title(g.title)
        if not parent:
            continue
        parent_norm = normalize_title(parent)
        if parent_norm == g.normalized_title:
            continue  # Parent is the full title (no subtitle stripped)

        # Find games matching the parent's normalized title
        candidates = by_norm.get(parent_norm, [])
        for cand in candidates:
            if cand.id == g.id or cand.id in seen_ids:
                continue

            # Safety: if both games have store_games on the same store,
            # they're different products (e.g., HL2 vs HL2: Deathmatch on Steam)
            g_stores = {sg.store_name for sg in g.store_games}
            cand_stores = {sg.store_name for sg in cand.store_games}
            if g_stores & cand_stores:
                log.debug(
                    "Dedup repair: skipping %r / %r — overlap on store(s) %s",
                    g.title, cand.title, g_stores & cand_stores,
                )
                continue

            # Merge: keep the game with the longer (more descriptive) title
            primary = g if len(g.normalized_title) >= len(cand.normalized_title) else cand
            secondary = cand if primary is g else g

            log.info(
                "Dedup repair: merging %r (%s) into %r (%s)",
                secondary.title, secondary.id[:8], primary.title, primary.id[:8],
            )

            # Move store_games
            for sg in list(secondary.store_games):
                sg.game_id = primary.id

            # Merge tags (union)
            primary_tag_ids = {t.id for t in primary.tags}
            for tag in secondary.tags:
                if tag.id not in primary_tag_ids:
                    primary.tags.append(tag)

            # Merge user data (keep primary's, supplement from secondary)
            if secondary.user_data and not primary.user_data:
                secondary.user_data.game_id = primary.id
            elif secondary.user_data and primary.user_data:
                if secondary.user_data.custom_notes and not primary.user_data.custom_notes:
                    primary.user_data.custom_notes = secondary.user_data.custom_notes
                if secondary.user_data.is_favorite and not primary.user_data.is_favorite:
                    primary.user_data.is_favorite = True

            # Move play sessions
            for ps in list(secondary.play_sessions):
                ps.game_id = primary.id

            seen_ids.add(secondary.id)
            session.delete(secondary)
            merged += 1

    if merged:
        session.flush()
        log.info("Dedup repair: merged %d duplicate game(s)", merged)

    return merged


def get_or_create_user_data(session, game_id: str) -> UserGameData:
    """Get or create UserGameData for a game

    Args:
        session: Database session
        game_id: Game UUID

    Returns:
        UserGameData object
    """
    user_data = session.query(UserGameData).filter_by(game_id=game_id).first()

    if user_data is None:
        user_data = UserGameData(game_id=game_id)
        session.add(user_data)

    return user_data
