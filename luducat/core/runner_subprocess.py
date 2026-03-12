# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# runner_subprocess.py

"""Runner subprocess for game execution with session tracking.

Lightweight module spawned as a separate process to:
1. Inhibit system sleep during gameplay
2. Wait for the game process to exit
3. Record session duration in the database

Design: No SQLAlchemy, no PySide6 — uses raw sqlite3 for a single UPDATE.
Startup ~50ms vs ~2s for the full app stack.
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def run_game(args) -> int:
    """Entry point for the runner subprocess.

    Args:
        args: Parsed argparse namespace with:
            session_id: PlaySession row ID
            db_path: Path to games.db
            env_json: JSON-encoded environment variable overrides
            working_dir: Working directory for the game
            game_command: Everything after -- (the actual game command)

    Returns:
        Game process exit code, or 1 on error.
    """
    from .sleep_inhibitor import SleepInhibitor

    session_id = args.session_id
    db_path = args.db_path
    game_command = args.game_command

    # Strip leading -- from remainder args
    if game_command and game_command[0] == "--":
        game_command = game_command[1:]

    if not game_command:
        logger.error("No game command provided")
        return 1

    start_time = datetime.now(timezone.utc)

    with SleepInhibitor():
        # Build environment
        env = os.environ.copy()
        if args.env_json:
            try:
                env.update(json.loads(args.env_json))
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("Failed to parse env_json: %s", e)

        # Working directory
        cwd = args.working_dir if args.working_dir else None

        try:
            process = subprocess.Popen(
                game_command,
                env=env,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            returncode = process.wait()
        except FileNotFoundError:
            logger.error("Game executable not found: %s", game_command[0])
            _end_session(db_path, session_id, start_time)
            return 1
        except Exception as e:
            logger.error("Failed to launch game: %s", e)
            _end_session(db_path, session_id, start_time)
            return 1

    _end_session(db_path, session_id, start_time)
    return returncode if returncode is not None else 0


def _end_session(db_path: str, session_id: int, start_time: datetime) -> None:
    """Record session end in the database using raw sqlite3.

    Args:
        db_path: Path to the games.db SQLite database.
        session_id: PlaySession row ID to update.
        start_time: When the game was started (UTC).
    """
    end_time = datetime.now(timezone.utc)
    delta_seconds = (end_time - start_time).total_seconds()
    duration_minutes = max(0, int(delta_seconds / 60))

    # Store as naive UTC (consistent with dt.utc_now())
    end_time_str = end_time.replace(tzinfo=None).isoformat()

    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute(
                "UPDATE play_sessions SET end_time = ?, duration_minutes = ? "
                "WHERE id = ?",
                (end_time_str, duration_minutes, session_id),
            )
            # Update UserGameData.playtime_minutes (add duration)
            conn.execute(
                "UPDATE user_game_data SET playtime_minutes = "
                "COALESCE(playtime_minutes, 0) + ? "
                "WHERE game_id = (SELECT game_id FROM play_sessions WHERE id = ?)",
                (duration_minutes, session_id),
            )
            conn.commit()
            logger.debug(
                "Session %d ended: %d minutes", session_id, duration_minutes
            )
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to record session end: %s", e)
