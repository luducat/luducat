# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# database.py

"""ProtonDB Plugin Database Models

SQLAlchemy models for the ProtonDB enrichment cache (protondb.db).
Stores compatibility ratings fetched from the ProtonDB API.

Tables:
- protondb_ratings: Cached tier/score data per Steam AppID
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from luducat.plugins.sdk.datetime import utc_now

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class ProtonDbRating(Base):
    """Cached ProtonDB rating for a Steam game"""

    __tablename__ = "protondb_ratings"

    steam_app_id = Column(String(20), primary_key=True)
    tier = Column(String(20), nullable=False)  # Platinum, Gold, Silver, Bronze, Borked
    score = Column(Float, nullable=True)
    confidence = Column(String(20), nullable=True)
    total_reports = Column(Integer, nullable=True)
    trending_tier = Column(String(20), nullable=True)
    best_reported_tier = Column(String(20), nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=utc_now)

    def __repr__(self) -> str:
        return f"<ProtonDbRating(app_id={self.steam_app_id}, tier={self.tier})>"


class ProtonDbNoMatch(Base):
    """Games not found on ProtonDB (404 response cache)"""

    __tablename__ = "protondb_no_match"

    steam_app_id = Column(String(20), primary_key=True)
    fetched_at = Column(DateTime, nullable=False, default=utc_now)


class ProtonDbDatabase:
    """Database manager for ProtonDB plugin"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine)

    def get_session(self) -> Session:
        return self._Session()

    def get_cached_rating(self, steam_app_id: str) -> Optional[ProtonDbRating]:
        """Get cached rating for a Steam app ID"""
        with self.get_session() as session:
            return session.get(ProtonDbRating, steam_app_id)

    def get_cached_ratings_bulk(self, app_ids: List[str]) -> Dict[str, ProtonDbRating]:
        """Get cached ratings for multiple app IDs"""
        with self.get_session() as session:
            ratings = (
                session.query(ProtonDbRating)
                .filter(ProtonDbRating.steam_app_id.in_(app_ids))
                .all()
            )
            # Detach from session before returning
            result = {}
            for r in ratings:
                session.expunge(r)
                result[r.steam_app_id] = r
            return result

    def get_no_match_ids(self, app_ids: List[str]) -> set:
        """Get set of app IDs that previously returned 404"""
        with self.get_session() as session:
            rows = (
                session.query(ProtonDbNoMatch.steam_app_id)
                .filter(ProtonDbNoMatch.steam_app_id.in_(app_ids))
                .all()
            )
            return {r[0] for r in rows}

    def save_rating(
        self,
        steam_app_id: str,
        tier: str,
        score: float,
        confidence: str,
        total_reports: int,
        trending_tier: str = "",
        best_reported_tier: str = "",
    ) -> None:
        """Save or update a ProtonDB rating"""
        with self.get_session() as session:
            existing = session.get(ProtonDbRating, steam_app_id)
            if existing:
                existing.tier = tier
                existing.score = score
                existing.confidence = confidence
                existing.total_reports = total_reports
                existing.trending_tier = trending_tier
                existing.best_reported_tier = best_reported_tier
                existing.fetched_at = utc_now()
            else:
                session.add(ProtonDbRating(
                    steam_app_id=steam_app_id,
                    tier=tier,
                    score=score,
                    confidence=confidence,
                    total_reports=total_reports,
                    trending_tier=trending_tier,
                    best_reported_tier=best_reported_tier,
                ))
            session.commit()

    def save_no_match(self, steam_app_id: str) -> None:
        """Record that a game was not found on ProtonDB"""
        with self.get_session() as session:
            existing = session.get(ProtonDbNoMatch, steam_app_id)
            if existing:
                existing.fetched_at = utc_now()
            else:
                session.add(ProtonDbNoMatch(
                    steam_app_id=steam_app_id,
                ))
            session.commit()

    def get_all_ratings(self) -> Dict[str, ProtonDbRating]:
        """Get all cached ratings"""
        with self.get_session() as session:
            ratings = session.query(ProtonDbRating).all()
            result = {}
            for r in ratings:
                session.expunge(r)
                result[r.steam_app_id] = r
            return result

    def get_all_no_match(self) -> List[ProtonDbNoMatch]:
        """Get all no-match entries for retry sync"""
        with self.get_session() as session:
            rows = session.query(ProtonDbNoMatch).all()
            for r in rows:
                session.expunge(r)
            return rows

    def delete_no_match(self, steam_app_id: str) -> None:
        """Remove a no-match entry (game now has a rating)"""
        with self.get_session() as session:
            session.query(ProtonDbNoMatch).filter(
                ProtonDbNoMatch.steam_app_id == steam_app_id
            ).delete()
            session.commit()

    def delete_rating(self, steam_app_id: str) -> None:
        """Remove a rating entry (game no longer on ProtonDB)"""
        with self.get_session() as session:
            session.query(ProtonDbRating).filter(
                ProtonDbRating.steam_app_id == steam_app_id
            ).delete()
            session.commit()
