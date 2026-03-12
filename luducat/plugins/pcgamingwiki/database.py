# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""PCGamingWiki Plugin Database Models

SQLAlchemy models for the PCGamingWiki metadata database (pcgamingwiki.db).
Stores Infobox_game and Multiplayer table data from PCGamingWiki Cargo API.

Tables:
- Main: pcgw_games (Infobox_game data)
- Detail: pcgw_multiplayer (multiplayer breakdown)
- Mapping: pcgw_store_matches (store ID to PCGW page ID mapping)
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_now

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# =============================================================================
# Main Game Table (Infobox_game)
# =============================================================================

class PcgwGame(Base):
    """PCGamingWiki Infobox_game data

    Stores all taxonomy and metadata fields from the Infobox_game Cargo table.
    Phase 1 uses modes + multiplayer relationship for game modes.
    Phase 2 will expose genres, developers, publishers, etc.
    """
    __tablename__ = "pcgw_games"

    page_id = Column(Integer, primary_key=True)
    page_name = Column(String(500), nullable=False)

    # Store IDs for matching
    steam_app_id = Column(Text, nullable=True)  # Can contain multiple comma-separated IDs
    gog_id = Column(Text, nullable=True)

    # Basic metadata
    cover_url = Column(String(500), nullable=True)
    developers = Column(Text, nullable=True)  # Comma-delimited
    publishers = Column(Text, nullable=True)
    engines = Column(Text, nullable=True)
    released_windows = Column(String(100), nullable=True)

    # Taxonomy (game modes basic)
    modes = Column(Text, nullable=True)  # "Singleplayer,Multiplayer"

    # Categorization
    genres = Column(Text, nullable=True)
    themes = Column(Text, nullable=True)
    perspectives = Column(Text, nullable=True)
    pacing = Column(Text, nullable=True)
    controls = Column(Text, nullable=True)
    art_styles = Column(Text, nullable=True)
    sports = Column(Text, nullable=True)
    vehicles = Column(Text, nullable=True)
    series = Column(String(500), nullable=True)

    # Business
    monetization = Column(Text, nullable=True)
    microtransactions = Column(Text, nullable=True)
    license = Column(String(100), nullable=True)
    available_on = Column(Text, nullable=True)

    # Controller/Input support
    controller_support = Column(String(50), nullable=True)
    full_controller_support = Column(String(50), nullable=True)
    controller_remapping = Column(String(50), nullable=True)
    controller_sensitivity = Column(String(50), nullable=True)
    controller_haptic_feedback = Column(String(50), nullable=True)  # NOT controller_haptics
    touchscreen = Column(String(50), nullable=True)  # NOT touchscreen_support
    key_remapping = Column(String(50), nullable=True)
    mouse_sensitivity = Column(String(50), nullable=True)
    mouse_acceleration = Column(String(50), nullable=True)
    mouse_input_in_menus = Column(String(50), nullable=True)
    # Note: Trackpad_support and Mouse_remapping don't exist in PCGW schema

    # External IDs and scores (from API table)
    metacritic_id = Column(String(100), nullable=True)
    metacritic_score = Column(Integer, nullable=True)
    opencritic_id = Column(String(100), nullable=True)
    opencritic_score = Column(Integer, nullable=True)
    igdb_id = Column(Integer, nullable=True)
    howlongtobeat_id = Column(Integer, nullable=True)
    wikipedia_id = Column(String(200), nullable=True)
    mobygames_id = Column(Integer, nullable=True)
    official_url = Column(String(500), nullable=True)

    # Tracking
    created_at = Column(DateTime, default=utc_now)
    last_updated = Column(DateTime, default=utc_now)

    # Relationship to detailed multiplayer data
    multiplayer = relationship(
        "PcgwMultiplayer",
        back_populates="game",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_pcgw_games_steam_app_id", "steam_app_id"),
        Index("ix_pcgw_games_gog_id", "gog_id"),
        Index("ix_pcgw_games_page_name", "page_name"),
    )


# =============================================================================
# Multiplayer Table (detailed breakdown)
# =============================================================================

class PcgwMultiplayer(Base):
    """PCGamingWiki Multiplayer table data

    Stores detailed multiplayer breakdown: Local, LAN, Online,
    with player counts, mode types (Co-op/Versus/Hot seat), and crossplay.
    """
    __tablename__ = "pcgw_multiplayer"

    page_id = Column(
        Integer,
        ForeignKey("pcgw_games.page_id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Local multiplayer
    local = Column(String(50), nullable=True)  # true/false/limited/hackable/unknown
    local_players = Column(String(50), nullable=True)
    local_modes = Column(Text, nullable=True)  # "Co-op,Versus,Hot seat"

    # LAN
    lan = Column(String(50), nullable=True)
    lan_players = Column(String(50), nullable=True)
    lan_modes = Column(Text, nullable=True)

    # Online
    online = Column(String(50), nullable=True)
    online_players = Column(String(50), nullable=True)
    online_modes = Column(Text, nullable=True)

    # Other
    asynchronous = Column(String(50), nullable=True)
    crossplay = Column(String(50), nullable=True)  # true/false/limited/always on
    crossplay_platforms = Column(Text, nullable=True)

    # Relationship back
    game = relationship("PcgwGame", back_populates="multiplayer")


# =============================================================================
# Store Match Table
# =============================================================================

class PcgwStoreMatch(Base):
    """Maps store game IDs to PCGamingWiki page IDs

    Caches the result of store ID lookups to avoid repeated API calls.
    """
    __tablename__ = "pcgw_store_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Store identification
    store_name = Column(String(50), nullable=False)  # steam, gog
    store_app_id = Column(String(100), nullable=False)

    # PCGW match (null if no match found)
    pcgw_page_id = Column(Integer, ForeignKey("pcgw_games.page_id"), nullable=True)
    pcgw_page_name = Column(String(500), nullable=True)

    # Match metadata
    match_method = Column(String(50))  # store_id, no_match
    match_confidence = Column(Float, default=1.0)

    # Tracking
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship
    game = relationship("PcgwGame", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("store_name", "store_app_id", name="uq_pcgw_store_match"),
        Index("ix_pcgw_store_matches_store", "store_name", "store_app_id"),
        Index("ix_pcgw_store_matches_page_id", "pcgw_page_id"),
    )


# =============================================================================
# Database Manager
# =============================================================================

class PcgwDatabase:
    """Database manager for PCGamingWiki plugin

    Handles database creation, sessions, and common queries.
    """

    # Schema version for tracking migrations
    SCHEMA_VERSION = 2  # Version 2 adds controller/input and API fields

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # Run migrations for existing databases
        self._run_migrations()

    def _run_migrations(self) -> None:
        """Run database migrations to add new columns to existing tables"""
        # Version 2: controller/input and API fields
        # Version 3: Fixed field names (controller_haptic_feedback, touchscreen)
        new_columns = [
            # Controller/Input support (correct field names matching PCGW schema)
            ("pcgw_games", "controller_support", "VARCHAR(50)"),
            ("pcgw_games", "full_controller_support", "VARCHAR(50)"),
            ("pcgw_games", "controller_remapping", "VARCHAR(50)"),
            ("pcgw_games", "controller_sensitivity", "VARCHAR(50)"),
            ("pcgw_games", "controller_haptic_feedback", "VARCHAR(50)"),  # Correct name
            ("pcgw_games", "touchscreen", "VARCHAR(50)"),  # Correct name
            ("pcgw_games", "key_remapping", "VARCHAR(50)"),
            ("pcgw_games", "mouse_sensitivity", "VARCHAR(50)"),
            ("pcgw_games", "mouse_acceleration", "VARCHAR(50)"),
            ("pcgw_games", "mouse_input_in_menus", "VARCHAR(50)"),
            # Note: trackpad_support and mouse_remapping don't exist in PCGW
            # External IDs and scores (from API table)
            ("pcgw_games", "metacritic_id", "VARCHAR(100)"),
            ("pcgw_games", "metacritic_score", "INTEGER"),
            ("pcgw_games", "opencritic_id", "VARCHAR(100)"),
            ("pcgw_games", "opencritic_score", "INTEGER"),
            ("pcgw_games", "igdb_id", "INTEGER"),
            ("pcgw_games", "howlongtobeat_id", "INTEGER"),
            ("pcgw_games", "wikipedia_id", "VARCHAR(200)"),
            ("pcgw_games", "mobygames_id", "INTEGER"),
            ("pcgw_games", "official_url", "VARCHAR(500)"),
        ]

        with self.engine.connect() as conn:
            for table, column, col_type in new_columns:
                try:
                    # Check if column exists by querying pragma
                    result = conn.execute(
                        text(f"PRAGMA table_info({table})")
                    ).fetchall()
                    existing_columns = {row[1] for row in result}

                    if column not in existing_columns:
                        conn.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                        )
                        conn.commit()
                        logger.debug(f"Added column {column} to {table}")
                except Exception as e:
                    logger.warning(f"Failed to add column {column} to {table}: {e}")

    def get_session(self) -> Session:
        """Get a new database session"""
        return self.Session()

    # -------------------------------------------------------------------------
    # Game CRUD
    # -------------------------------------------------------------------------

    def get_game(self, page_id: int) -> Optional[PcgwGame]:
        """Get game by PCGW page ID with multiplayer data loaded"""
        with self.get_session() as session:
            game = session.query(PcgwGame).filter_by(page_id=page_id).first()
            if game:
                # Force load multiplayer relationship
                _ = game.multiplayer
                session.expunge(game)
            return game

    def save_game_from_api(
        self,
        data: Dict[str, Any],
        session: Optional[Session] = None,
    ) -> Optional[PcgwGame]:
        """Save or update a game from Cargo API response data

        Args:
            data: Raw dict from Cargo API (keys have spaces, e.g. "Steam AppID")
            session: Optional existing session for batch operations

        Returns:
            The saved PcgwGame, or None if no page_id
        """
        page_id = data.get("pageID")
        if not page_id:
            return None

        try:
            page_id = int(page_id)
        except (ValueError, TypeError):
            return None

        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            # Parse metacritic/opencritic scores as integers
            metacritic_score = None
            opencritic_score = None
            if data.get("Metacritic Score"):
                try:
                    metacritic_score = int(data.get("Metacritic Score"))
                except (ValueError, TypeError):
                    pass
            if data.get("OpenCritic Score"):
                try:
                    opencritic_score = int(data.get("OpenCritic Score"))
                except (ValueError, TypeError):
                    pass

            # Parse IGDB/HLTB/MobyGames IDs as integers
            igdb_id = None
            hltb_id = None
            mobygames_id = None
            if data.get("IGDB ID"):
                try:
                    igdb_id = int(data.get("IGDB ID"))
                except (ValueError, TypeError):
                    pass
            if data.get("HowLongToBeat ID"):
                try:
                    hltb_id = int(data.get("HowLongToBeat ID"))
                except (ValueError, TypeError):
                    pass
            if data.get("MobyGames ID"):
                try:
                    mobygames_id = int(data.get("MobyGames ID"))
                except (ValueError, TypeError):
                    pass

            game = PcgwGame(
                page_id=page_id,
                page_name=data.get("pageName", ""),
                steam_app_id=data.get("Steam AppID"),
                gog_id=data.get("GOGcom ID"),
                cover_url=data.get("Cover URL"),
                developers=data.get("Developers"),
                publishers=data.get("Publishers"),
                engines=data.get("Engines"),
                released_windows=data.get("Released Windows"),
                modes=data.get("Modes"),
                genres=data.get("Genres"),
                themes=data.get("Themes"),
                perspectives=data.get("Perspectives"),
                pacing=data.get("Pacing"),
                controls=data.get("Controls"),
                art_styles=data.get("Art styles"),
                sports=data.get("Sports"),
                vehicles=data.get("Vehicles"),
                series=data.get("Series"),
                monetization=data.get("Monetization"),
                microtransactions=data.get("Microtransactions"),
                license=data.get("License"),
                available_on=data.get("Available on"),
                # Controller/Input support (field names match PCGW Cargo schema)
                controller_support=data.get("Controller support"),
                full_controller_support=data.get("Full controller support"),
                controller_remapping=data.get("Controller remapping"),
                controller_sensitivity=data.get("Controller sensitivity"),
                controller_haptic_feedback=data.get("Controller haptic feedback"),
                touchscreen=data.get("Touchscreen"),
                key_remapping=data.get("Key remapping"),
                mouse_sensitivity=data.get("Mouse sensitivity"),
                mouse_acceleration=data.get("Mouse acceleration"),
                mouse_input_in_menus=data.get("Mouse input in menus"),
                # Note: Trackpad_support and Mouse_remapping don't exist in PCGW
                # External IDs and scores
                metacritic_id=data.get("Metacritic ID"),
                metacritic_score=metacritic_score,
                opencritic_id=data.get("OpenCritic ID"),
                opencritic_score=opencritic_score,
                igdb_id=igdb_id,
                howlongtobeat_id=hltb_id,
                wikipedia_id=data.get("Wikipedia ID"),
                mobygames_id=mobygames_id,
                official_url=data.get("Official site"),
                last_updated=utc_now(),
            )
            session.merge(game)

            # Save multiplayer data if present
            mp_data = self._extract_multiplayer_data(data)
            if mp_data:
                mp = PcgwMultiplayer(page_id=page_id, **mp_data)
                session.merge(mp)

            if own_session:
                session.commit()

            return game
        except Exception as e:
            if own_session:
                session.rollback()
            logger.warning(f"Failed to save PCGW game {page_id}: {e}")
            raise
        finally:
            if own_session:
                session.close()

    @staticmethod
    def _extract_multiplayer_data(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract multiplayer fields from Cargo API response

        Returns None if no multiplayer data present.
        """
        mp = {
            "local": data.get("Local"),
            "local_players": data.get("Local players"),
            "local_modes": data.get("Local modes"),
            "lan": data.get("LAN"),
            "lan_players": data.get("LAN players"),
            "lan_modes": data.get("LAN modes"),
            "online": data.get("Online"),
            "online_players": data.get("Online players"),
            "online_modes": data.get("Online modes"),
            "asynchronous": data.get("Asynchronous"),
            "crossplay": data.get("Crossplay"),
            "crossplay_platforms": data.get("Crossplay platforms"),
        }

        # Only return if at least one field has data
        if any(v is not None for v in mp.values()):
            return mp
        return None

    # -------------------------------------------------------------------------
    # Store Match CRUD
    # -------------------------------------------------------------------------

    def get_store_match(
        self, store_name: str, store_app_id: str
    ) -> Optional[PcgwStoreMatch]:
        """Get cached store match"""
        with self.get_session() as session:
            match = session.query(PcgwStoreMatch).filter_by(
                store_name=store_name,
                store_app_id=str(store_app_id),
            ).first()
            if match:
                session.expunge(match)
            return match

    def save_store_match(
        self,
        store_name: str,
        store_app_id: str,
        pcgw_page_id: Optional[int],
        pcgw_page_name: Optional[str] = None,
        match_method: str = "store_id",
        confidence: float = 1.0,
        session: Optional[Session] = None,
    ) -> PcgwStoreMatch:
        """Save or update a store match"""
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            existing = session.query(PcgwStoreMatch).filter_by(
                store_name=store_name,
                store_app_id=str(store_app_id),
            ).first()

            if existing:
                existing.pcgw_page_id = pcgw_page_id
                existing.pcgw_page_name = pcgw_page_name
                existing.match_method = match_method
                existing.match_confidence = confidence
                existing.updated_at = utc_now()
                match = existing
            else:
                match = PcgwStoreMatch(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                    pcgw_page_id=pcgw_page_id,
                    pcgw_page_name=pcgw_page_name,
                    match_method=match_method,
                    match_confidence=confidence,
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

    # -------------------------------------------------------------------------
    # Bulk Queries
    # -------------------------------------------------------------------------

    def get_game_modes_for_store_ids(
        self, store_name: str, app_ids: List[str]
    ) -> Dict[str, List[str]]:
        """Get normalized game modes for multiple store games

        Queries local database only (no API calls). Joins store_matches
        to games and multiplayer tables, then normalizes PCGW data
        to IGDB-compatible mode names.

        Args:
            store_name: Store identifier (steam, gog)
            app_ids: List of store app IDs

        Returns:
            Dict mapping app_id -> list of normalized game mode names
            e.g. {"440": ["Multiplayer"], "620": ["Single player", "Multiplayer", "Co-operative", "Split screen"]}
        """
        result: Dict[str, List[str]] = {}
        app_id_set = set(str(aid) for aid in app_ids)

        try:
            from sqlalchemy import text

            with self.get_session() as session:
                sql = text("""
                    SELECT sm.store_app_id,
                           g.modes,
                           mp.local, mp.local_modes,
                           mp.lan, mp.lan_modes,
                           mp.online, mp.online_modes
                    FROM pcgw_store_matches sm
                    JOIN pcgw_games g ON sm.pcgw_page_id = g.page_id
                    LEFT JOIN pcgw_multiplayer mp ON g.page_id = mp.page_id
                    WHERE sm.store_name = :store_name
                      AND sm.pcgw_page_id IS NOT NULL
                """)

                rows = session.execute(
                    sql, {"store_name": store_name}
                ).fetchall()

                for row in rows:
                    app_id = row[0]
                    if app_id not in app_id_set:
                        continue

                    modes = _normalize_game_modes(
                        basic_modes=row[1],
                        local=row[2],
                        local_modes=row[3],
                        lan=row[4],
                        lan_modes=row[5],
                        online=row[6],
                        online_modes=row[7],
                    )

                    if modes:
                        result[app_id] = modes

        except Exception as e:
            logger.warning(f"Failed to get bulk game modes for {store_name}: {e}")

        return result

    def get_steam_ids_for_store(
        self, store_name: str, app_ids: List[str]
    ) -> Dict[str, tuple]:
        """Resolve Steam AppIDs for non-Steam store games via PCGW cross-references.

        Joins pcgw_store_matches → pcgw_games to find the Steam AppID
        associated with a GOG/Epic game's PCGW page. Pure local DB query.

        Args:
            store_name: Source store ("gog" or "epic")
            app_ids: List of store app IDs to resolve

        Returns:
            Dict mapping store_app_id -> (steam_app_id, page_name)
            Only includes entries where a Steam AppID was found.
        """
        result: Dict[str, tuple] = {}
        app_id_set = set(str(aid) for aid in app_ids)

        try:
            from sqlalchemy import text

            with self.get_session() as session:
                sql = text("""
                    SELECT sm.store_app_id, g.steam_app_id, g.page_name
                    FROM pcgw_store_matches sm
                    JOIN pcgw_games g ON sm.pcgw_page_id = g.page_id
                    WHERE sm.store_name = :store_name
                      AND sm.pcgw_page_id IS NOT NULL
                      AND g.steam_app_id IS NOT NULL
                      AND g.steam_app_id != ''
                """)

                rows = session.execute(
                    sql, {"store_name": store_name}
                ).fetchall()

                for row in rows:
                    app_id = row[0]
                    if app_id not in app_id_set:
                        continue

                    # steam_app_id can be comma-separated; take first entry
                    raw_steam_id = row[1]
                    steam_id = raw_steam_id.split(",")[0].strip()
                    if steam_id:
                        result[app_id] = (steam_id, row[2] or "")

        except Exception as e:
            logger.warning(f"Failed to resolve Steam IDs for {store_name}: {e}")

        return result

    def get_match_count(self) -> Dict[str, int]:
        """Get match statistics"""
        with self.get_session() as session:
            total = session.query(PcgwStoreMatch).count()
            matched = session.query(PcgwStoreMatch).filter(
                PcgwStoreMatch.pcgw_page_id.isnot(None)
            ).count()
            return {
                "total": total,
                "matched": matched,
                "failed": total - matched,
            }

    def close(self) -> None:
        """Close database connections"""
        self.engine.dispose()


# =============================================================================
# Game Mode Normalization
# =============================================================================

def _normalize_game_modes(
    basic_modes: Optional[str],
    local: Optional[str] = None,
    local_modes: Optional[str] = None,
    lan: Optional[str] = None,
    lan_modes: Optional[str] = None,
    online: Optional[str] = None,
    online_modes: Optional[str] = None,
) -> List[str]:
    """Convert PCGamingWiki mode data to game mode names for constants.py

    Produces strings matching GAME_MODE_LABELS keys:
    - "Single player"
    - "Multiplayer"
    - "Co-operative"   (online/LAN co-op — NOT local)
    - "Online Versus"  (online versus, from online_modes)
    - "Local Co-op"    (local co-op, from local_modes)
    - "Local Versus"   (local versus, from local_modes)
    - "Split screen"   (local multiplayer, no mode detail available)
    - "PVP"            (umbrella — any versus mode, local or online)

    Note: "Massively Multiplayer Online (MMO)" and "Battle Royale"
    are NOT available from PCGamingWiki.

    Local subtype logic:
    - If local=true AND local_modes specifies Co-op/Versus → emit specific subtypes
    - If local=true AND no recognized mode detail → emit generic "Split screen"
    - "Co-operative" only emits for online or LAN co-op (not local)
    - "PVP" emits when any versus mode (local or online) is present
    """
    modes = []
    _TRUE_VALUES = ("true", "limited")

    # Parse basic Modes field (comma-delimited list from Infobox_game)
    basic_set = set()
    if basic_modes:
        for m in basic_modes.split(","):
            basic_set.add(m.strip().lower())

    # Single player
    if "singleplayer" in basic_set:
        modes.append("Single player")

    # Multiplayer detection from basic modes + detailed table
    has_multiplayer = "multiplayer" in basic_set

    if online and online.lower() in _TRUE_VALUES:
        has_multiplayer = True
    if lan and lan.lower() in _TRUE_VALUES:
        has_multiplayer = True
    if local and local.lower() in _TRUE_VALUES:
        has_multiplayer = True

    if has_multiplayer:
        modes.append("Multiplayer")

    # Co-op detection from ONLINE and LAN only (not local — that becomes
    # "Local Co-op" below for clearer couch co-op indication)
    has_remote_coop = False
    for modes_field in (lan_modes, online_modes):
        if modes_field:
            for mode in modes_field.split(","):
                if mode.strip().lower() == "co-op":
                    has_remote_coop = True
                    break
        if has_remote_coop:
            break

    if has_remote_coop:
        modes.append("Co-operative")

    # Online versus detection
    has_online_versus = False
    if online_modes:
        for mode in online_modes.split(","):
            if mode.strip().lower() == "versus":
                has_online_versus = True
                break

    if has_online_versus:
        modes.append("Online Versus")

    # Local multiplayer subtypes — granular badges for couch gaming
    is_local = local and local.lower() in _TRUE_VALUES
    has_local_versus = False
    if is_local:
        local_mode_set = set()
        if local_modes:
            for mode in local_modes.split(","):
                local_mode_set.add(mode.strip().lower())

        has_local_coop = "co-op" in local_mode_set
        has_local_versus = "versus" in local_mode_set

        if has_local_coop:
            modes.append("Local Co-op")
        if has_local_versus:
            modes.append("Local Versus")
        if not has_local_coop and not has_local_versus:
            # Fallback: local multiplayer exists but no mode detail (hot seat,
            # or PCGW page simply doesn't specify). Use generic badge.
            modes.append("Split screen")

    # LAN play badge
    has_lan = lan and lan.lower() in _TRUE_VALUES
    if has_lan:
        modes.append("LAN")

    # PVP umbrella — any versus mode (local or online)
    if has_local_versus or has_online_versus:
        modes.append("PVP")

    return modes
