# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# versions.py

"""Internal database migrations for luducat

This module contains all migration functions for the internal migration system.
Migrations are applied sequentially based on PRAGMA user_version.

Version history:
- 0: Fresh database (no tables)
- 1: Initial schema (games, store_games, user_tags, game_tags, user_game_data, schema_meta)
- 2: Add family_shared column to store_games
- 3: Add family_shared_owner column to store_games
- 4: Expand family_shared_owner to String(100)
- 5: (removed — was downloads table, pre-release cleanup)
- 6: (removed — was archives table, pre-release cleanup)
- 7: Add runtimes table (stores platform plugins; column names kept for DB compat)
- 8: Add game_installations table (runtime_id column kept for DB compat)
- 9: Add playtime_minutes to user_game_data
- 10: Add play_sessions table
- 11: Flatten extra_metadata and rename fields to canonical names
- 12: Add FTS5 full-text search index for games
- 13: Extend user_tags (source, tag_type, external_id, description, pinned) and game_tags (assigned_by, assigned_at)
- 14: Add nsfw_override to user_tags and user_game_data (content filter overrides)
- 15: Replace pinned boolean with score integer (-99..+99) on user_tags
- 16: Add is_installed and install_path columns to store_games
- 17: Normalize links field in metadata_json
- 18: Improved normalize_title(), re-normalize games, merge newly-matching duplicates
- 19: Add launch_config column to user_game_data (per-game launch settings)
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from luducat.core.json_compat import json
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Connection

logger = logging.getLogger(__name__)

# Current schema version (increment when adding new migrations)
CURRENT_SCHEMA_VERSION = 21

# Mapping from Alembic revision IDs to internal version numbers
ALEMBIC_REVISION_MAP = {
    "001_initial": 1,
    "002_family_shared": 2,
    "003_family_shared_owner": 3,
    "004_expand_family_shared_owner": 4,
    "007_runtimes": 7,
    "008_game_installations": 8,
    "009_playtime": 9,
    "010_play_sessions": 10,
}


def migrate_000_to_001(conn: "Connection") -> None:
    """Create initial schema tables.

    Note: For fresh databases, SQLAlchemy creates all tables at once.
    This migration exists for completeness and documentation.
    """
    logger.info("Migration 0->1: Creating initial schema")

    # Games table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS games (
            id VARCHAR(36) PRIMARY KEY,
            title VARCHAR(500) NOT NULL,
            normalized_title VARCHAR(500) NOT NULL,
            primary_store VARCHAR(50) NOT NULL,
            added_at DATETIME
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_games_title ON games (title)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_games_normalized_title ON games (normalized_title)"))

    # Store games table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS store_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id VARCHAR(36) NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            store_name VARCHAR(50) NOT NULL,
            store_app_id VARCHAR(100) NOT NULL,
            launch_url VARCHAR(500) NOT NULL,
            metadata_json JSON,
            metadata_fetched DATETIME,
            UNIQUE (store_name, store_app_id)
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_store_games_game_id ON store_games (game_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_store_games_store_name ON store_games (store_name)"))

    # User tags table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL UNIQUE,
            color VARCHAR(7) NOT NULL DEFAULT '#3daee9',
            created_at DATETIME
        )
    """))

    # Game tags association table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS game_tags (
            game_id VARCHAR(36) NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES user_tags(id) ON DELETE CASCADE,
            PRIMARY KEY (game_id, tag_id)
        )
    """))

    # User game data table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS user_game_data (
            game_id VARCHAR(36) PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
            is_favorite BOOLEAN NOT NULL DEFAULT 0,
            is_hidden BOOLEAN NOT NULL DEFAULT 0,
            custom_notes TEXT,
            last_launched DATETIME,
            launch_count INTEGER NOT NULL DEFAULT 0
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_game_data_is_favorite ON user_game_data (is_favorite)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_game_data_is_hidden ON user_game_data (is_hidden)"))

    # Schema meta table
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key VARCHAR(50) PRIMARY KEY,
            value VARCHAR(500) NOT NULL,
            updated_at DATETIME
        )
    """))

    conn.commit()


def migrate_001_to_002(conn: "Connection") -> None:
    """Add family_shared column to store_games."""
    logger.info("Migration 1->2: Adding family_shared column")

    # SQLite doesn't support ADD COLUMN with constraints well, so we use a simple approach
    # Check if column exists first
    result = conn.execute(text("PRAGMA table_info(store_games)"))
    columns = [row[1] for row in result.fetchall()]

    if "family_shared" not in columns:
        conn.execute(text("ALTER TABLE store_games ADD COLUMN family_shared INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_store_games_family_shared ON store_games (family_shared)"))

    conn.commit()


def migrate_002_to_003(conn: "Connection") -> None:
    """Add family_shared_owner column to store_games."""
    logger.info("Migration 2->3: Adding family_shared_owner column")

    result = conn.execute(text("PRAGMA table_info(store_games)"))
    columns = [row[1] for row in result.fetchall()]

    if "family_shared_owner" not in columns:
        conn.execute(text("ALTER TABLE store_games ADD COLUMN family_shared_owner VARCHAR(20)"))

    conn.commit()


def migrate_003_to_004(conn: "Connection") -> None:
    """Expand family_shared_owner column to VARCHAR(100).

    SQLite doesn't actually enforce VARCHAR length, so this is a no-op for SQLite.
    The column was already created, just update metadata if needed.
    """
    logger.info("Migration 3->4: Expanding family_shared_owner (no-op for SQLite)")
    # SQLite doesn't enforce string length limits, so no actual change needed
    conn.commit()


def migrate_004_to_005(conn: "Connection") -> None:
    """Was: Create downloads table. Removed pre-release (archivist cleanup)."""
    logger.info("Migration 4->5: no-op (downloads table removed)")
    conn.commit()


def migrate_005_to_006(conn: "Connection") -> None:
    """Was: Create archives table. Removed pre-release (archivist cleanup)."""
    logger.info("Migration 5->6: no-op (archives table removed)")
    conn.commit()


def migrate_006_to_007(conn: "Connection") -> None:
    """Create runtimes table."""
    logger.info("Migration 6->7: Creating runtimes table")

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS runtimes (
            id VARCHAR(100) PRIMARY KEY,
            runtime_type VARCHAR(30) NOT NULL,
            name VARCHAR(200) NOT NULL,
            version VARCHAR(50),
            executable_path TEXT,
            is_default BOOLEAN NOT NULL DEFAULT 0,
            is_managed BOOLEAN NOT NULL DEFAULT 0,
            provider_name VARCHAR(50),
            capabilities_json JSON,
            metadata_json JSON,
            detected_at DATETIME NOT NULL,
            last_used_at DATETIME
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runtimes_type ON runtimes (runtime_type)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_runtimes_type_default ON runtimes (runtime_type, is_default)"))

    conn.commit()


def migrate_007_to_008(conn: "Connection") -> None:
    """Create game_installations table."""
    logger.info("Migration 7->8: Creating game_installations table")

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS game_installations (
            game_id VARCHAR(36) PRIMARY KEY,
            status VARCHAR(20) NOT NULL,
            install_path TEXT,
            version VARCHAR(50),
            checksums_json JSON,
            size_bytes BIGINT,
            runtime_id VARCHAR(100),
            settings_json JSON,
            launch_count INTEGER NOT NULL DEFAULT 0,
            installed_at DATETIME,
            last_verified_at DATETIME,
            last_played_at DATETIME,
            updated_at DATETIME
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_game_installations_status ON game_installations (status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_game_installations_runtime_id ON game_installations (runtime_id)"))

    conn.commit()


def migrate_008_to_009(conn: "Connection") -> None:
    """Add playtime_minutes column to user_game_data."""
    logger.info("Migration 8->9: Adding playtime_minutes column")

    result = conn.execute(text("PRAGMA table_info(user_game_data)"))
    columns = [row[1] for row in result.fetchall()]

    if "playtime_minutes" not in columns:
        conn.execute(text("ALTER TABLE user_game_data ADD COLUMN playtime_minutes INTEGER NOT NULL DEFAULT 0"))

    conn.commit()


def migrate_009_to_010(conn: "Connection") -> None:
    """Create play_sessions table."""
    logger.info("Migration 9->10: Creating play_sessions table")

    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS play_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id VARCHAR(36) NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            store_name VARCHAR(50) NOT NULL,
            start_time DATETIME,
            end_time DATETIME,
            duration_minutes INTEGER,
            source VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_play_sessions_game_id ON play_sessions (game_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_play_sessions_store_name ON play_sessions (store_name)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_play_sessions_source ON play_sessions (source)"))

    conn.commit()


def migrate_010_to_011(conn: "Connection") -> None:
    """Flatten extra_metadata and rename fields to canonical names.

    Moves all fields from the nested extra_metadata dict to top-level
    in metadata_json, and renames fields to canonical names:
    - cover_image_url -> cover_url
    - header_image_url -> header_url
    - background_image_url -> background_url
    - player_perspectives -> perspectives
    - igdb_rating -> user_rating
    - igdb_rating_count -> user_rating_count
    - metacritic_score -> critic_rating
    - metacritic_url -> critic_rating_url
    - multiplayer (detail dict) -> game_modes_detail
    """

    logger.info("Migration 10->11: Flattening extra_metadata + renaming fields")

    # Rename map for keys coming FROM extra_metadata to top-level
    EXTRA_RENAME_MAP = {
        "player_perspectives": "perspectives",
        "igdb_rating": "user_rating",
        "igdb_rating_count": "user_rating_count",
        "multiplayer": "game_modes_detail",
        "background_image_url": "background_url",
    }

    # Rename map for existing top-level keys
    TOP_RENAME_MAP = {
        "cover_image_url": "cover_url",
        "header_image_url": "header_url",
        "background_image_url": "background_url",
        "metacritic_score": "critic_rating",
        "metacritic_url": "critic_rating_url",
    }

    BATCH_SIZE = 500
    offset = 0
    total_updated = 0

    while True:
        rows = conn.execute(text(
            "SELECT id, metadata_json FROM store_games "
            "WHERE metadata_json IS NOT NULL "
            "ORDER BY id LIMIT :limit OFFSET :offset"
        ), {"limit": BATCH_SIZE, "offset": offset}).fetchall()

        if not rows:
            break

        for row_id, raw_json in rows:
            if raw_json is None:
                continue

            if isinstance(raw_json, str):
                try:
                    metadata = json.loads(raw_json)
                except (json.JSONDecodeError, TypeError):
                    continue
            elif isinstance(raw_json, dict):
                metadata = raw_json
            else:
                continue

            if not isinstance(metadata, dict):
                continue

            changed = False

            # Step 1: Flatten extra_metadata
            extra = metadata.pop("extra_metadata", None)
            if extra and isinstance(extra, dict):
                changed = True
                for key, value in extra.items():
                    new_key = EXTRA_RENAME_MAP.get(key, key)
                    # Extra background_url takes priority (from enrichment)
                    if new_key == "background_url" or new_key not in metadata:
                        metadata[new_key] = value

            # Step 2: Rename top-level keys
            for old_key, new_key in TOP_RENAME_MAP.items():
                if old_key in metadata:
                    value = metadata.pop(old_key)
                    if new_key not in metadata:
                        metadata[new_key] = value
                    changed = True

            if changed:
                conn.execute(
                    text("UPDATE store_games SET metadata_json = :json WHERE id = :id"),
                    {"json": json.dumps(metadata), "id": row_id}
                )
                total_updated += 1

        offset += BATCH_SIZE

    conn.commit()
    logger.info(f"Migration 10->11: Updated {total_updated} store_game rows")


def migrate_011_to_012(conn: "Connection") -> None:
    """Create FTS5 full-text search virtual table for games.

    Provides full-text search across title, description, developers,
    publishers, genres, and themes. The game_id column is stored but
    not indexed for search (UNINDEXED).

    The table is populated by GameService._rebuild_fts_index() during
    cache refresh, not during migration (metadata resolution needed).
    """
    logger.info("Migration 11->12: Creating FTS5 search index")

    conn.execute(text("""
        CREATE VIRTUAL TABLE IF NOT EXISTS games_fts USING fts5(
            game_id UNINDEXED,
            title,
            short_description,
            developers,
            publishers,
            genres,
            themes
        )
    """))

    conn.commit()


def migrate_012_to_013(conn: "Connection") -> None:
    """Extend user_tags and game_tags for multi-source tag system.

    user_tags gets: source, tag_type, external_id, description, pinned
    game_tags gets: assigned_by, assigned_at

    Backfill: orange-colored tags (#e67e22) are reclassified as imported.
    """
    logger.info("Migration 12->13: Extending tag tables for multi-source support")

    # --- user_tags columns ---
    result = conn.execute(text("PRAGMA table_info(user_tags)"))
    columns = [row[1] for row in result.fetchall()]

    if "source" not in columns:
        conn.execute(text("ALTER TABLE user_tags ADD COLUMN source TEXT NOT NULL DEFAULT 'native'"))
    if "tag_type" not in columns:
        conn.execute(text("ALTER TABLE user_tags ADD COLUMN tag_type TEXT NOT NULL DEFAULT 'user'"))
    if "external_id" not in columns:
        conn.execute(text("ALTER TABLE user_tags ADD COLUMN external_id TEXT"))
    if "description" not in columns:
        conn.execute(text("ALTER TABLE user_tags ADD COLUMN description TEXT"))
    if "pinned" not in columns:
        conn.execute(text("ALTER TABLE user_tags ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"))

    # --- game_tags columns ---
    result = conn.execute(text("PRAGMA table_info(game_tags)"))
    columns = [row[1] for row in result.fetchall()]

    if "assigned_by" not in columns:
        conn.execute(text("ALTER TABLE game_tags ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'native'"))
    if "assigned_at" not in columns:
        conn.execute(text("ALTER TABLE game_tags ADD COLUMN assigned_at TEXT"))

    # --- Backfill: reclassify orange-colored tags as imported ---
    conn.execute(text(
        "UPDATE user_tags SET source = 'imported', tag_type = 'imported' "
        "WHERE color = '#e67e22'"
    ))

    conn.commit()
    logger.info("Migration 12->13: Tag tables extended successfully")


def migrate_013_to_014(conn: "Connection") -> None:
    """Add nsfw_override column to user_tags and user_game_data.

    Tri-state content filter override:
        0 = neutral (default, use automatic detection)
        1 = force NSFW (always hide when filter is active)
       -1 = force SFW (never hide, overrides all signals)
    """
    logger.info("Migration 13->14: Adding nsfw_override columns")

    # --- user_tags ---
    result = conn.execute(text("PRAGMA table_info(user_tags)"))
    columns = [row[1] for row in result.fetchall()]

    if "nsfw_override" not in columns:
        conn.execute(text(
            "ALTER TABLE user_tags ADD COLUMN nsfw_override INTEGER NOT NULL DEFAULT 0"
        ))

    # --- user_game_data ---
    result = conn.execute(text("PRAGMA table_info(user_game_data)"))
    columns = [row[1] for row in result.fetchall()]

    if "nsfw_override" not in columns:
        conn.execute(text(
            "ALTER TABLE user_game_data ADD COLUMN nsfw_override INTEGER NOT NULL DEFAULT 0"
        ))

    conn.commit()
    logger.info("Migration 13->14: nsfw_override columns added successfully")


def migrate_014_to_015(conn: "Connection") -> None:
    """Replace pinned boolean with score integer on user_tags.

    Backfill: pinned=1 → score=50, then drop pinned column via table recreate.
    Score range: -99..+99. Positive = preferred (quick-access), negative = blocked.
    """
    logger.info("Migration 14->15: Replacing pinned with score on user_tags")

    result = conn.execute(text("PRAGMA table_info(user_tags)"))
    columns = [row[1] for row in result.fetchall()]

    has_pinned = "pinned" in columns
    has_score = "score" in columns

    if not has_score and has_pinned:
        # Add score column, backfill from pinned, then drop pinned via recreate
        conn.execute(text(
            "ALTER TABLE user_tags ADD COLUMN score INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "UPDATE user_tags SET score = 50 WHERE pinned = 1"
        ))

        # Recreate table without pinned column (SQLite limitation)
        conn.execute(text("""
            CREATE TABLE user_tags_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                color VARCHAR(7) NOT NULL DEFAULT '#3daee9',
                source TEXT NOT NULL DEFAULT 'native',
                tag_type TEXT NOT NULL DEFAULT 'user',
                external_id TEXT,
                description TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                nsfw_override INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO user_tags_new
                (id, name, color, source, tag_type, external_id, description, score, nsfw_override, created_at)
            SELECT id, name, color, source, tag_type, external_id, description, score, nsfw_override, created_at
            FROM user_tags
        """))
        conn.execute(text("DROP TABLE user_tags"))
        conn.execute(text("ALTER TABLE user_tags_new RENAME TO user_tags"))

    elif not has_score and not has_pinned:
        # Fresh DB that somehow reached v14 without pinned — just add score
        conn.execute(text(
            "ALTER TABLE user_tags ADD COLUMN score INTEGER NOT NULL DEFAULT 0"
        ))

    conn.commit()
    logger.info("Migration 14->15: pinned → score migration complete")


def migrate_015_to_016(conn: "Connection") -> None:
    """Add is_installed and install_path columns to store_games.

    Enables installation status tracking synced from store plugins
    (Steam VDF scanner, Epic Legendary CLI, GOG Galaxy DB).
    """
    logger.info("Migration 15->16: Adding installation status columns to store_games")

    result = conn.execute(text("PRAGMA table_info(store_games)"))
    columns = [row[1] for row in result.fetchall()]

    if "is_installed" not in columns:
        conn.execute(text(
            "ALTER TABLE store_games ADD COLUMN is_installed BOOLEAN NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_store_games_is_installed ON store_games (is_installed)"
        ))

    if "install_path" not in columns:
        conn.execute(text(
            "ALTER TABLE store_games ADD COLUMN install_path VARCHAR(500)"
        ))

    conn.commit()
    logger.info("Migration 15->16: Installation status columns added successfully")


def migrate_016_to_017(conn: "Connection") -> None:
    """Normalize links field in metadata_json from dict to list format.

    GOG plugin previously stored links as {"type": "url"} dicts.
    Canonical format is [{"type": ..., "url": ...}] (matching IGDB).
    """
    logger.info("Migration 16->17: Normalizing links in metadata_json")

    rows = conn.execute(text(
        "SELECT id, metadata_json FROM store_games "
        "WHERE metadata_json IS NOT NULL "
        "AND json_type(metadata_json, '$.links') = 'object'"
    )).fetchall()

    updated = 0
    for row_id, meta_raw in rows:
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            links = meta.get("links")
            if isinstance(links, dict):
                meta["links"] = [{"type": k, "url": v} for k, v in links.items() if v]
                conn.execute(
                    text("UPDATE store_games SET metadata_json = :json WHERE id = :id"),
                    {"json": json.dumps(meta), "id": row_id},
                )
                updated += 1
        except Exception as e:
            logger.debug(f"Skipping links normalization for {row_id}: {e}")

    conn.commit()
    logger.info(f"Migration 16->17: Normalized links in {updated} store_games rows")


def _pick_keeper(conn: "Connection", game_rows: list) -> str:
    """Pick the best game to keep when merging duplicates.

    Selection priority:
    1. Most StoreGame entries (already partially deduped)
    2. Has UserGameData (preserve user effort)
    3. Oldest added_at (original entry)

    Returns the game_id of the keeper.
    """
    best_id = game_rows[0]["id"]
    best_sg_count = 0
    best_has_ud = False
    best_added = None

    for row in game_rows:
        gid = row["id"]
        sg_count = conn.execute(
            text("SELECT COUNT(*) FROM store_games WHERE game_id = :gid"),
            {"gid": gid},
        ).scalar()
        has_ud = conn.execute(
            text("SELECT COUNT(*) FROM user_game_data WHERE game_id = :gid"),
            {"gid": gid},
        ).scalar() > 0
        added = row["added_at"]

        # Compare: more store_games > has user data > older added_at
        if (
            sg_count > best_sg_count
            or (sg_count == best_sg_count and has_ud and not best_has_ud)
            or (sg_count == best_sg_count and has_ud == best_has_ud
                and added is not None
                and (best_added is None or added < best_added))
        ):
            best_id = gid
            best_sg_count = sg_count
            best_has_ud = has_ud
            best_added = added

    return best_id


def _merge_games(conn: "Connection", keep_id: str, absorb_id: str) -> None:
    """Merge absorb_id game into keep_id, then delete absorb_id.

    - Reassigns store_games
    - Merges game_tags (INSERT OR IGNORE for duplicates)
    - Merges user_game_data (OR for booleans, MAX for timestamps, SUM for counts)
    - Reassigns play_sessions
    - Deletes absorbed game
    """
    # Reassign store_games
    conn.execute(
        text("UPDATE store_games SET game_id = :keep WHERE game_id = :absorb"),
        {"keep": keep_id, "absorb": absorb_id},
    )

    # Merge game_tags — skip duplicates
    conn.execute(
        text(
            "INSERT OR IGNORE INTO game_tags (game_id, tag_id, assigned_by, assigned_at) "
            "SELECT :keep, tag_id, assigned_by, assigned_at "
            "FROM game_tags WHERE game_id = :absorb"
        ),
        {"keep": keep_id, "absorb": absorb_id},
    )
    conn.execute(
        text("DELETE FROM game_tags WHERE game_id = :absorb"),
        {"absorb": absorb_id},
    )

    # Merge user_game_data
    keeper_ud = conn.execute(
        text("SELECT * FROM user_game_data WHERE game_id = :gid"),
        {"gid": keep_id},
    ).fetchone()
    absorb_ud = conn.execute(
        text("SELECT * FROM user_game_data WHERE game_id = :gid"),
        {"gid": absorb_id},
    ).fetchone()

    if absorb_ud is not None:
        if keeper_ud is not None:
            # Merge fields
            k = dict(keeper_ud._mapping)
            a = dict(absorb_ud._mapping)
            conn.execute(
                text(
                    "UPDATE user_game_data SET "
                    "is_favorite = :fav, is_hidden = :hid, "
                    "custom_notes = :notes, "
                    "last_launched = :ll, launch_count = :lc, "
                    "playtime_minutes = :pt, "
                    "nsfw_override = :nsfw "
                    "WHERE game_id = :gid"
                ),
                {
                    "fav": int(k["is_favorite"] or a["is_favorite"]),
                    "hid": int(k["is_hidden"] or a["is_hidden"]),
                    "notes": k["custom_notes"] or a["custom_notes"],
                    "ll": max(
                        k["last_launched"] or "",
                        a["last_launched"] or "",
                    ) or None,
                    "lc": (k["launch_count"] or 0) + (a["launch_count"] or 0),
                    "pt": (k["playtime_minutes"] or 0) + (a["playtime_minutes"] or 0),
                    "nsfw": k["nsfw_override"] if k["nsfw_override"] != 0 else a["nsfw_override"],
                    "gid": keep_id,
                },
            )
        else:
            # Move absorb's user data to keeper
            conn.execute(
                text("UPDATE user_game_data SET game_id = :keep WHERE game_id = :absorb"),
                {"keep": keep_id, "absorb": absorb_id},
            )

    # Delete absorbed user_game_data if it still exists (after merge case)
    conn.execute(
        text("DELETE FROM user_game_data WHERE game_id = :absorb"),
        {"absorb": absorb_id},
    )

    # Reassign play_sessions
    conn.execute(
        text("UPDATE play_sessions SET game_id = :keep WHERE game_id = :absorb"),
        {"keep": keep_id, "absorb": absorb_id},
    )

    # Delete absorbed game
    conn.execute(
        text("DELETE FROM games WHERE id = :absorb"),
        {"absorb": absorb_id},
    )


def migrate_017_to_018(conn: "Connection") -> None:
    """Improved normalize_title: re-normalize all games, merge newly-matching duplicates.

    Steps:
    1. Re-normalize all game titles with the improved pipeline
    2. Find groups where 2+ games now share a normalized_title
    3. For each group: pick keeper, verify no same-store conflict, merge others
    """
    from luducat.core.database import normalize_title

    logger.info("Migration 17->18: Re-normalizing game titles and merging duplicates")

    # Step 1: Re-normalize all titles
    rows = conn.execute(
        text("SELECT id, title, normalized_title, added_at FROM games")
    ).fetchall()

    if not rows:
        conn.commit()
        return

    updated = 0
    for row in rows:
        new_norm = normalize_title(row[1])
        if new_norm != row[2]:
            conn.execute(
                text("UPDATE games SET normalized_title = :norm WHERE id = :id"),
                {"norm": new_norm, "id": row[0]},
            )
            updated += 1

    logger.info(f"Migration 17->18: Re-normalized {updated} of {len(rows)} titles")

    # Step 2: Find collision groups (2+ games with same normalized_title)
    groups = conn.execute(
        text(
            "SELECT normalized_title, COUNT(*) as cnt "
            "FROM games GROUP BY normalized_title HAVING cnt >= 2"
        )
    ).fetchall()

    merged = 0
    skipped = 0

    for group_row in groups:
        norm_title = group_row[0]
        game_list = conn.execute(
            text(
                "SELECT id, title, normalized_title, added_at "
                "FROM games WHERE normalized_title = :norm "
                "ORDER BY added_at ASC"
            ),
            {"norm": norm_title},
        ).fetchall()

        if len(game_list) < 2:
            continue

        # Convert to dicts for _pick_keeper
        game_dicts = [dict(r._mapping) for r in game_list]

        # Same-store guard: collect all store_names across all games in group
        all_store_names = []
        for gd in game_dicts:
            stores = conn.execute(
                text("SELECT store_name FROM store_games WHERE game_id = :gid"),
                {"gid": gd["id"]},
            ).fetchall()
            all_store_names.extend(s[0] for s in stores)

        # If merging would create duplicate store_name entries, skip
        if len(all_store_names) != len(set(all_store_names)):
            logger.warning(
                f"Migration 17->18: Skipping merge for '{norm_title}' — "
                f"same-store conflict (stores: {all_store_names})"
            )
            skipped += 1
            continue

        keep_id = _pick_keeper(conn, game_dicts)

        for gd in game_dicts:
            if gd["id"] != keep_id:
                _merge_games(conn, keep_id, gd["id"])
                merged += 1

    conn.commit()
    logger.info(
        f"Migration 17->18: Merged {merged} duplicate games, "
        f"skipped {skipped} groups (same-store conflicts)"
    )


def migrate_018_to_019(conn: "Connection") -> None:
    """Add launch_config column to user_game_data.

    Stores per-game launch settings as JSON:
    {"runner": "heroic", "platform": null, "launch_args": "",
     "last_launch_result": "success", "last_launch_timestamp": "..."}
    """
    logger.info("Migration 18->19: Adding launch_config to user_game_data")

    # Check if column already exists (idempotent)
    cols = conn.execute(text("PRAGMA table_info(user_game_data)")).fetchall()
    col_names = {c[1] for c in cols}

    if "launch_config" not in col_names:
        conn.execute(text(
            "ALTER TABLE user_game_data ADD COLUMN launch_config TEXT"
        ))
        logger.info("Migration 18->19: Added launch_config column")
    else:
        logger.info("Migration 18->19: launch_config column already exists")

    conn.commit()


def migrate_019_to_020(conn: "Connection") -> None:
    """Add is_private_app and is_delisted columns to store_games.

    Steam-specific status flags:
    - is_private_app: game marked private on user's Steam profile
    - is_delisted: game no longer in the public Steam store catalog
    """
    logger.info("Migration 19->20: Adding is_private_app and is_delisted to store_games")

    cols = conn.execute(text("PRAGMA table_info(store_games)")).fetchall()
    col_names = {c[1] for c in cols}

    if "is_private_app" not in col_names:
        conn.execute(text(
            "ALTER TABLE store_games ADD COLUMN is_private_app INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_store_games_is_private_app "
            "ON store_games (is_private_app)"
        ))
        logger.info("Migration 19->20: Added is_private_app column")

    if "is_delisted" not in col_names:
        conn.execute(text(
            "ALTER TABLE store_games ADD COLUMN is_delisted INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_store_games_is_delisted "
            "ON store_games (is_delisted)"
        ))
        logger.info("Migration 19->20: Added is_delisted column")

    conn.commit()


def migrate_020_to_021(conn: "Connection") -> None:
    """Add collections and collection_games tables.

    Collections are user-created groups: either dynamic (saved filter queries)
    or static (manual game lists).
    """
    logger.info("Migration 20->21: Adding collections and collection_games tables")

    tables = {
        r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }

    if "collections" not in tables:
        conn.execute(text("""
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                color TEXT,
                filter_json TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                is_hidden BOOLEAN NOT NULL DEFAULT 0,
                notes TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """))
        logger.info("Migration 20->21: Created collections table")

    if "collection_games" not in tables:
        conn.execute(text("""
            CREATE TABLE collection_games (
                collection_id INTEGER NOT NULL,
                game_id TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                added_at DATETIME,
                PRIMARY KEY (collection_id, game_id),
                FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_collection_games_game_id "
            "ON collection_games (game_id)"
        ))
        logger.info("Migration 20->21: Created collection_games table")

    conn.commit()


# Migration registry: list of (from_version, to_version, migration_function)
MIGRATIONS = [
    (0, 1, migrate_000_to_001),
    (1, 2, migrate_001_to_002),
    (2, 3, migrate_002_to_003),
    (3, 4, migrate_003_to_004),
    (4, 5, migrate_004_to_005),
    (5, 6, migrate_005_to_006),
    (6, 7, migrate_006_to_007),
    (7, 8, migrate_007_to_008),
    (8, 9, migrate_008_to_009),
    (9, 10, migrate_009_to_010),
    (10, 11, migrate_010_to_011),
    (11, 12, migrate_011_to_012),
    (12, 13, migrate_012_to_013),
    (13, 14, migrate_013_to_014),
    (14, 15, migrate_014_to_015),
    (15, 16, migrate_015_to_016),
    (16, 17, migrate_016_to_017),
    (17, 18, migrate_017_to_018),
    (18, 19, migrate_018_to_019),
    (19, 20, migrate_019_to_020),
    (20, 21, migrate_020_to_021),
]
