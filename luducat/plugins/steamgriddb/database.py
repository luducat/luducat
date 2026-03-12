# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""SteamGridDB Plugin Database Models

SQLAlchemy models for the SteamGridDB metadata database (steamgriddb.db).
Stores game lookups and cached image asset URLs from SteamGridDB API v2.

Tables:
- Main: sgdb_games (game records from SteamGridDB)
- Assets: sgdb_assets (heroes, grids, logos — unified)
- Mapping: sgdb_store_matches (store ID to SteamGridDB game ID mapping)
"""

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
    UniqueConstraint,
    create_engine,
    desc,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# =============================================================================
# Main Game Table
# =============================================================================

class SgdbGame(Base):
    """SteamGridDB game record

    Caches game lookup results from the SteamGridDB API.
    Used to avoid repeated API calls for game ID resolution.
    """
    __tablename__ = "sgdb_games"

    sgdb_id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False)
    release_date = Column(Integer, nullable=True)  # Unix timestamp
    verified = Column(Boolean, default=False)
    steam_app_id = Column(String(50), nullable=True)

    # Tracking
    created_at = Column(DateTime, default=utc_now)
    last_updated = Column(DateTime, default=utc_now)

    # Relationships
    assets = relationship(
        "SgdbAsset",
        back_populates="game",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_sgdb_games_name", "name"),
        Index("ix_sgdb_games_steam_app_id", "steam_app_id"),
    )


# =============================================================================
# Unified Asset Table (heroes, grids, logos)
# =============================================================================

class SgdbAsset(Base):
    """SteamGridDB image asset

    Stores cached image URLs for heroes (banners), grids (covers),
    and logos. All asset types share the same schema — differentiated
    by asset_type column.
    """
    __tablename__ = "sgdb_assets"

    id = Column(Integer, primary_key=True)  # SteamGridDB asset ID
    game_id = Column(
        Integer,
        ForeignKey("sgdb_games.sgdb_id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_type = Column(String(20), nullable=False)  # hero, grid, logo
    url = Column(String(1000), nullable=False)  # Full-size URL
    thumb = Column(String(1000), nullable=True)  # Thumbnail URL
    score = Column(Integer, default=0)  # Community score
    style = Column(String(50), nullable=True)  # alternate, blurred, material...
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    mime = Column(String(50), nullable=True)  # image/png, image/jpeg
    is_nsfw = Column(Boolean, default=False)
    is_animated = Column(Boolean, default=False)
    is_humor = Column(Boolean, default=False)
    is_epilepsy = Column(Boolean, default=False)
    author_name = Column(String(255), nullable=True)
    author_steam_id = Column(String(20), nullable=True)

    # Tracking
    created_at = Column(DateTime, default=utc_now)

    game = relationship("SgdbGame", back_populates="assets")

    __table_args__ = (
        Index("ix_sgdb_assets_game_type", "game_id", "asset_type"),
        Index("ix_sgdb_assets_score", "game_id", "asset_type", "score"),
    )


# =============================================================================
# Store Match Table
# =============================================================================

class SgdbStoreMatch(Base):
    """Maps store game IDs to SteamGridDB game IDs

    Caches the result of platform and title lookups to avoid
    repeated API calls.
    """
    __tablename__ = "sgdb_store_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Store identification
    store_name = Column(String(50), nullable=False)  # steam, gog, epic
    store_app_id = Column(String(100), nullable=False)

    # SteamGridDB match (null if no match found)
    sgdb_game_id = Column(
        Integer,
        ForeignKey("sgdb_games.sgdb_id"),
        nullable=True,
    )

    # Match metadata
    match_method = Column(String(50))  # platform_lookup, title_search, no_match
    match_confidence = Column(Float, default=1.0)

    # Tracking
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    # Relationship
    game = relationship("SgdbGame", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("store_name", "store_app_id", name="uq_sgdb_store_match"),
        Index("ix_sgdb_store_matches_store", "store_name", "store_app_id"),
        Index("ix_sgdb_store_matches_sgdb_id", "sgdb_game_id"),
    )


# =============================================================================
# Database Manager
# =============================================================================

class SgdbDatabase:
    """Database manager for SteamGridDB plugin

    Handles database creation, sessions, and common queries.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )

        Base.metadata.create_all(self.engine)
        self._migrate()
        self.Session = sessionmaker(bind=self.engine)

    def _migrate(self) -> None:
        """Add missing columns to existing databases"""
        insp = inspect(self.engine)
        columns = {c["name"] for c in insp.get_columns("sgdb_assets")}

        new_cols = {
            "is_humor": "BOOLEAN DEFAULT 0",
            "is_epilepsy": "BOOLEAN DEFAULT 0",
            "author_steam_id": "TEXT",
        }

        with self.engine.connect() as conn:
            for col_name, col_def in new_cols.items():
                if col_name not in columns:
                    conn.execute(
                        text(f"ALTER TABLE sgdb_assets ADD COLUMN {col_name} {col_def}")
                    )
                    logger.info(f"SteamGridDB DB: added column sgdb_assets.{col_name}")
            conn.commit()

    def get_session(self) -> Session:
        """Get a new database session"""
        return self.Session()

    # -------------------------------------------------------------------------
    # Game CRUD
    # -------------------------------------------------------------------------

    def get_game(self, sgdb_id: int) -> Optional[SgdbGame]:
        """Get game by SteamGridDB ID with assets loaded"""
        with self.get_session() as session:
            game = session.query(SgdbGame).filter_by(sgdb_id=sgdb_id).first()
            if game:
                _ = game.assets  # Force load
                session.expunge(game)
            return game

    def save_game(
        self,
        sgdb_id: int,
        name: str,
        release_date: Optional[int] = None,
        verified: bool = False,
        steam_app_id: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> SgdbGame:
        """Save or update a game record"""
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            game = SgdbGame(
                sgdb_id=sgdb_id,
                name=name,
                release_date=release_date,
                verified=verified,
                steam_app_id=steam_app_id,
                last_updated=utc_now(),
            )
            session.merge(game)

            if own_session:
                session.commit()

            return game
        except Exception as e:
            if own_session:
                session.rollback()
            logger.warning(f"Failed to save SteamGridDB game {sgdb_id}: {e}")
            raise
        finally:
            if own_session:
                session.close()

    # -------------------------------------------------------------------------
    # Asset CRUD
    # -------------------------------------------------------------------------

    def save_assets(
        self,
        game_id: int,
        asset_type: str,
        assets: List[Dict[str, Any]],
        session: Optional[Session] = None,
    ) -> int:
        """Save assets for a game, replacing existing ones of the same type.

        Keeps top 15 by score. Returns count saved.
        """
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            # Remove existing assets of this type for this game
            session.query(SgdbAsset).filter_by(
                game_id=game_id, asset_type=asset_type
            ).delete()

            # Sort by score descending, keep top 15
            sorted_assets = sorted(
                assets, key=lambda a: a.get("score", 0), reverse=True
            )[:15]

            count = 0
            for asset_data in sorted_assets:
                asset_id = asset_data.get("id")
                url = asset_data.get("url", "")
                if not asset_id or not url:
                    continue

                asset = SgdbAsset(
                    id=asset_id,
                    game_id=game_id,
                    asset_type=asset_type,
                    url=url,
                    thumb=asset_data.get("thumb"),
                    score=asset_data.get("score", 0),
                    style=asset_data.get("style"),
                    width=asset_data.get("width"),
                    height=asset_data.get("height"),
                    mime=asset_data.get("mime"),
                    is_nsfw=asset_data.get("nsfw", False),
                    is_animated=False,  # API has no animated field; rely on types filter at fetch time
                    is_humor=asset_data.get("humor", False),
                    is_epilepsy=asset_data.get("epilepsy", False),
                    author_name=(
                        asset_data.get("author", {}).get("name")
                        if isinstance(asset_data.get("author"), dict)
                        else None
                    ),
                    author_steam_id=(
                        str(asset_data.get("author", {}).get("steam64", ""))
                        if isinstance(asset_data.get("author"), dict)
                        and asset_data.get("author", {}).get("steam64")
                        else None
                    ),
                )
                session.merge(asset)
                count += 1

            if own_session:
                session.commit()

            return count
        except Exception as e:
            if own_session:
                session.rollback()
            logger.warning(
                f"Failed to save SteamGridDB assets for game {game_id}/{asset_type}: {e}"
            )
            raise
        finally:
            if own_session:
                session.close()

    def get_best_asset(
        self,
        game_id: int,
        asset_type: str,
        style: Optional[str] = None,
        allow_nsfw: bool = False,
        allow_humor: bool = False,
        allow_epilepsy: bool = True,
        allow_animated: bool = False,
        author_scores: Optional[Dict[str, int]] = None,
        blocked_hits: Optional[Dict[str, int]] = None,
    ) -> Optional[SgdbAsset]:
        """Get the best (highest-scored) asset matching filters.

        Author scoring:
        - author_scores: {name_lower: score} dict
        - Negative score (< 0) → author's assets excluded entirely
        - Positive score (> 0) → added to asset's community score as boost
        - Zero → no effect
        NULL author_name is never filtered.

        Args:
            blocked_hits: If not None, populated with {author_name: count}
                for each blocked author whose assets were filtered out.
        """
        scores_lower = (
            {k.lower(): v for k, v in author_scores.items()}
            if author_scores else {}
        )

        with self.get_session() as session:
            query = session.query(SgdbAsset).filter_by(
                game_id=game_id, asset_type=asset_type
            )

            if not allow_nsfw:
                query = query.filter(SgdbAsset.is_nsfw == False)  # noqa: E712

            if not allow_humor:
                query = query.filter(SgdbAsset.is_humor == False)  # noqa: E712

            if not allow_epilepsy:
                query = query.filter(SgdbAsset.is_epilepsy == False)  # noqa: E712

            if not allow_animated:
                query = query.filter(SgdbAsset.is_animated == False)  # noqa: E712

            def _select_best(candidates: List[SgdbAsset]) -> Optional[SgdbAsset]:
                """Apply author score filtering and return best asset."""
                # Filter out authors with negative scores (NULL author_name never filtered)
                if scores_lower:
                    filtered = []
                    for a in candidates:
                        if a.author_name and scores_lower.get(a.author_name.lower(), 0) < 0:
                            # Track blocked author hit
                            if blocked_hits is not None:
                                name = a.author_name
                                blocked_hits[name] = blocked_hits.get(name, 0) + 1
                        else:
                            filtered.append(a)
                else:
                    filtered = candidates

                if not filtered:
                    logger.debug(
                        f"All {len(candidates)} {asset_type} assets for game {game_id} "
                        f"were blocked by author scores"
                    )
                    return None

                # Apply author score boost and select highest effective score
                def effective_score(asset: SgdbAsset) -> int:
                    base = asset.score or 0
                    if scores_lower and asset.author_name:
                        author_boost = scores_lower.get(asset.author_name.lower(), 0)
                        if author_boost > 0:
                            return base + author_boost
                    return base

                best = max(filtered, key=effective_score)
                if scores_lower:
                    logger.debug(
                        f"Selected {asset_type} asset {best.id} by '{best.author_name}' "
                        f"(base={best.score}, effective={effective_score(best)}) "
                        f"for game {game_id}"
                    )
                return best

            if style:
                # Try preferred style first
                styled = query.filter(SgdbAsset.style == style).order_by(
                    desc(SgdbAsset.score)
                ).all()
                if styled:
                    result = _select_best(styled)
                    if result:
                        session.expunge(result)
                        return result
                # Fall back to any style

            all_candidates = query.order_by(desc(SgdbAsset.score)).all()
            result = _select_best(all_candidates) if all_candidates else None
            if result:
                session.expunge(result)
            return result

    def nuke_all_assets(self) -> int:
        """Delete ALL rows from sgdb_assets table.

        Used when author scores change to force re-evaluation of
        all cached assets.

        Returns:
            Count of deleted rows.
        """
        with self.get_session() as session:
            count = session.query(SgdbAsset).delete()
            session.commit()
            logger.info(f"SteamGridDB: nuked {count} cached assets")
            return count

    def get_all_asset_urls(self) -> List[str]:
        """Get all asset URLs (url + thumb) for cache purging.

        Returns:
            List of all non-null URL strings from sgdb_assets.
        """
        urls = []
        with self.get_session() as session:
            for row in session.query(SgdbAsset.url).filter(SgdbAsset.url.isnot(None)).all():
                urls.append(row[0])
            for row in session.query(SgdbAsset.thumb).filter(SgdbAsset.thumb.isnot(None)).all():
                urls.append(row[0])
        return urls

    def get_asset_urls_by_authors(self, authors: List[str]) -> List[str]:
        """Get all asset URLs (url + thumb) for the given authors.

        Args:
            authors: List of lowercase author names.

        Returns:
            List of all non-null URL strings from matching assets.
        """
        from sqlalchemy import func

        if not authors:
            return []
        urls = []
        with self.get_session() as session:
            assets = (
                session.query(SgdbAsset.url, SgdbAsset.thumb)
                .filter(func.lower(SgdbAsset.author_name).in_(authors))
                .all()
            )
            for url, thumb in assets:
                if url:
                    urls.append(url)
                if thumb:
                    urls.append(thumb)
        return urls

    def get_author_cover_counts(self) -> Dict[str, int]:
        """Get asset counts per author (all asset types).

        Returns:
            Dict mapping lowercase author_name -> count of assets.
        """
        from sqlalchemy import func

        with self.get_session() as session:
            rows = (
                session.query(
                    func.lower(SgdbAsset.author_name),
                    func.count(),
                )
                .filter(SgdbAsset.author_name.isnot(None))
                .group_by(func.lower(SgdbAsset.author_name))
                .all()
            )
            return {name: count for name, count in rows}

    def get_author_asset_counts_by_type(
        self, authors: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, int]]:
        """Get asset counts per author grouped by asset_type.

        Args:
            authors: Optional list of lowercase author names to filter.
                     If None, returns counts for all authors.

        Returns:
            {lowercase_author_name: {"grid": N, "hero": N, "logo": N, "icon": N}}
        """
        from sqlalchemy import func

        with self.get_session() as session:
            query = (
                session.query(
                    func.lower(SgdbAsset.author_name),
                    SgdbAsset.asset_type,
                    func.count(),
                )
                .filter(SgdbAsset.author_name.isnot(None))
            )
            if authors is not None:
                query = query.filter(
                    func.lower(SgdbAsset.author_name).in_(authors)
                )
            rows = query.group_by(
                func.lower(SgdbAsset.author_name), SgdbAsset.asset_type
            ).all()

            result: Dict[str, Dict[str, int]] = {}
            for name, asset_type, count in rows:
                if name not in result:
                    result[name] = {"grid": 0, "hero": 0, "logo": 0, "icon": 0}
                result[name][asset_type] = count
            return result

    def get_author_steam_ids(self) -> Dict[str, str]:
        """Get known Steam64 IDs for authors.

        Returns:
            Dict mapping lowercase author_name -> steam64 ID string.
        """
        from sqlalchemy import func

        with self.get_session() as session:
            rows = (
                session.query(
                    func.lower(SgdbAsset.author_name),
                    SgdbAsset.author_steam_id,
                )
                .filter(
                    SgdbAsset.author_name.isnot(None),
                    SgdbAsset.author_steam_id.isnot(None),
                )
                .distinct()
                .all()
            )
            return {name: steam_id for name, steam_id in rows}

    def get_author_by_url(self, url: str) -> Optional[str]:
        """Look up author_name for an asset by its URL."""
        with self.get_session() as session:
            row = session.query(SgdbAsset.author_name).filter(
                SgdbAsset.url == url
            ).first()
            return row[0] if row and row[0] else None

    def get_author_info_by_url(self, url: str) -> Optional[tuple]:
        """Look up author_name and author_steam_id for an asset by its URL.

        Returns (author_name, author_steam_id) or None.
        """
        with self.get_session() as session:
            row = session.query(
                SgdbAsset.author_name, SgdbAsset.author_steam_id
            ).filter(SgdbAsset.url == url).first()
            if row and row[0]:
                return (row[0], row[1] or "")
            return None

    def has_assets(self, game_id: int, asset_type: str) -> bool:
        """Check if we have cached assets for a game+type"""
        with self.get_session() as session:
            return (
                session.query(SgdbAsset)
                .filter_by(game_id=game_id, asset_type=asset_type)
                .count()
                > 0
            )

    # -------------------------------------------------------------------------
    # Store Match CRUD
    # -------------------------------------------------------------------------

    def get_store_match(
        self, store_name: str, store_app_id: str
    ) -> Optional[SgdbStoreMatch]:
        """Get cached store match"""
        with self.get_session() as session:
            match = (
                session.query(SgdbStoreMatch)
                .filter_by(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                )
                .first()
            )
            if match:
                session.expunge(match)
            return match

    def save_store_match(
        self,
        store_name: str,
        store_app_id: str,
        sgdb_game_id: Optional[int],
        match_method: str = "platform_lookup",
        confidence: float = 1.0,
        session: Optional[Session] = None,
    ) -> SgdbStoreMatch:
        """Save or update a store match"""
        own_session = session is None
        if own_session:
            session = self.get_session()

        try:
            existing = (
                session.query(SgdbStoreMatch)
                .filter_by(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                )
                .first()
            )

            if existing:
                existing.sgdb_game_id = sgdb_game_id
                existing.match_method = match_method
                existing.match_confidence = confidence
                existing.updated_at = utc_now()
                match = existing
            else:
                match = SgdbStoreMatch(
                    store_name=store_name,
                    store_app_id=str(store_app_id),
                    sgdb_game_id=sgdb_game_id,
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
    # Statistics
    # -------------------------------------------------------------------------

    def get_match_count(self) -> Dict[str, int]:
        """Get match statistics"""
        with self.get_session() as session:
            total = session.query(SgdbStoreMatch).count()
            matched = (
                session.query(SgdbStoreMatch)
                .filter(SgdbStoreMatch.sgdb_game_id.isnot(None))
                .count()
            )
            return {
                "total": total,
                "matched": matched,
                "failed": total - matched,
            }

    def close(self) -> None:
        """Close database connections"""
        self.engine.dispose()
