# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# user_data_service.py

"""User data service for luducat.

Handles per-game user state: favorites, hidden, notes, launch recording,
play sessions, and playtime imports.
Extracted from GameService to reduce its responsibilities.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .database import (
    Database,
    PlaySession,
    UserGameData,
    get_or_create_user_data,
)
from .dt import utc_now

logger = logging.getLogger(__name__)


class UserDataService:
    """Manages per-game user data: favorites, hidden, notes, playtime."""

    def __init__(self, database: Database, games_cache: dict):
        self.database = database
        self._games_cache = games_cache

    def set_favorite(self, game_id: str, is_favorite: bool) -> None:
        """Set favorite status for a game.

        Args:
            game_id: Game UUID
            is_favorite: Favorite status
        """
        session = self.database.get_session()
        user_data = get_or_create_user_data(session, game_id)
        user_data.is_favorite = is_favorite
        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].is_favorite = is_favorite

    def set_hidden(self, game_id: str, is_hidden: bool) -> None:
        """Set hidden status for a game.

        Args:
            game_id: Game UUID
            is_hidden: Hidden status
        """
        session = self.database.get_session()
        user_data = get_or_create_user_data(session, game_id)
        user_data.is_hidden = is_hidden
        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].is_hidden = is_hidden

    def set_nsfw_override(self, game_id: str, nsfw_override: int) -> None:
        """Set content filter override for a game.

        Args:
            game_id: Game UUID
            nsfw_override: Override value (0=auto, 1=NSFW, -1=SFW)
        """
        session = self.database.get_session()
        user_data = get_or_create_user_data(session, game_id)
        user_data.nsfw_override = nsfw_override
        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].nsfw_override = nsfw_override

    def set_game_notes(self, game_id: str, notes: str) -> None:
        """Set custom notes for a game.

        Args:
            game_id: Game UUID
            notes: Notes text (can be empty string to clear)
        """
        session = self.database.get_session()
        user_data = get_or_create_user_data(session, game_id)
        user_data.custom_notes = notes if notes else None
        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].notes = notes

    def record_launch(self, game_id: str, store_name: str = "") -> int:
        """Record a game launch and start a play session.

        Creates a PlaySession record to track this play session.
        The session has start_time set but end_time NULL (may be updated later
        if we can track when the game exits).

        Args:
            game_id: Game UUID
            store_name: Store used to launch (e.g., "steam", "gog", "epic")

        Returns:
            PlaySession ID for potential later update with end_time
        """
        now = utc_now()
        session = self.database.get_session()

        user_data = get_or_create_user_data(session, game_id)
        user_data.last_launched = now
        user_data.launch_count = (user_data.launch_count or 0) + 1

        play_session = PlaySession(
            game_id=game_id,
            store_name=store_name or "unknown",
            start_time=now,
            end_time=None,
            duration_minutes=None,
            source="local",
        )
        session.add(play_session)
        session.commit()

        session_id = play_session.id

        if game_id in self._games_cache:
            self._games_cache[game_id].last_launched = now.isoformat()

        logger.debug(f"Started play session {session_id} for game {game_id} via {store_name}")
        return session_id

    def get_launch_config(self, game_id: str) -> Optional[Dict[str, Any]]:
        """Get per-game launch configuration.

        Returns:
            Parsed launch config dict, or None if not set.
        """
        session = self.database.get_session()
        user_data = session.query(UserGameData).filter_by(game_id=game_id).first()
        if not user_data or not user_data.launch_config:
            return None

        import json
        try:
            return json.loads(user_data.launch_config)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_launch_config(self, game_id: str, config: Optional[Dict[str, Any]]) -> None:
        """Set per-game launch configuration.

        Args:
            game_id: Game UUID
            config: Launch config dict, or None to clear.
                Expected keys: bridge, runtime, launch_args
        """
        import json

        session = self.database.get_session()
        user_data = get_or_create_user_data(session, game_id)

        if config:
            user_data.launch_config = json.dumps(config, separators=(",", ":"))
        else:
            user_data.launch_config = None

        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].launch_config = user_data.launch_config or ""

    def end_play_session(self, session_id: int) -> None:
        """End a play session by recording the end time.

        Called when we detect a game has exited (if trackable).

        Args:
            session_id: PlaySession ID from record_launch()
        """
        session = self.database.get_session()
        play_session = session.query(PlaySession).filter_by(id=session_id).first()

        if play_session and play_session.end_time is None:
            play_session.end_time = utc_now()
            if play_session.start_time:
                delta = play_session.end_time - play_session.start_time
                play_session.duration_minutes = int(delta.total_seconds() / 60)
            session.commit()
            logger.debug(
                f"Ended play session {session_id}, duration: {play_session.duration_minutes} minutes"
            )

    def import_playtime(
        self,
        game_id: str,
        store_name: str,
        playtime_minutes: int,
        last_played: Optional[datetime] = None,
        source: str = "import",
    ) -> None:
        """Import playtime data from a store plugin.

        Creates or updates a PlaySession record for imported playtime.
        For aggregated imports (like Steam's total playtime), this creates
        a single session with duration but no start/end times.

        Also updates UserGameData for backwards compatibility.

        Args:
            game_id: Game UUID
            store_name: Store name (e.g., "steam", "gog", "epic")
            playtime_minutes: Total playtime in minutes
            last_played: Last played timestamp (optional)
            source: Source identifier (e.g., "steam_import", "gog_import")
        """
        session = self.database.get_session()

        existing = session.query(PlaySession).filter_by(
            game_id=game_id,
            store_name=store_name,
            source=source,
        ).first()

        if existing:
            if playtime_minutes > (existing.duration_minutes or 0):
                existing.duration_minutes = playtime_minutes
                logger.debug(
                    f"Updated {source} playtime for {game_id}: {playtime_minutes} minutes"
                )
        else:
            play_session = PlaySession(
                game_id=game_id,
                store_name=store_name,
                start_time=None,
                end_time=None,
                duration_minutes=playtime_minutes,
                source=source,
            )
            session.add(play_session)
            logger.debug(
                f"Created {source} session for {game_id}: {playtime_minutes} minutes"
            )

        user_data = get_or_create_user_data(session, game_id)
        if playtime_minutes > (user_data.playtime_minutes or 0):
            user_data.playtime_minutes = playtime_minutes

        if last_played:
            if user_data.last_launched is None or last_played > user_data.last_launched:
                user_data.last_launched = last_played
                if not user_data.launch_count:
                    user_data.launch_count = 1

        session.commit()

        if game_id in self._games_cache:
            self._games_cache[game_id].playtime_minutes = playtime_minutes
            if last_played:
                self._games_cache[game_id].last_launched = last_played.isoformat()

    def get_play_sessions_summary(self, game_id: str) -> list:
        """Per-store playtime breakdown for a game.

        Returns:
            List of dicts: store_name, total_minutes, session_count, first_played.
        """
        from sqlalchemy import func

        session = self.database.get_session()
        rows = (
            session.query(
                PlaySession.store_name,
                func.coalesce(func.sum(PlaySession.duration_minutes), 0).label("total"),
                func.count(PlaySession.id).label("count"),
                func.min(PlaySession.start_time).label("first"),
            )
            .filter_by(game_id=game_id)
            .group_by(PlaySession.store_name)
            .all()
        )
        return [
            {
                "store_name": r.store_name,
                "total_minutes": r.total,
                "session_count": r.count,
                "first_played": r.first,
            }
            for r in rows
        ]
